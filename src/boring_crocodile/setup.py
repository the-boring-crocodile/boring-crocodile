"""Shared wizard/file planning; privileged apply requires typed confirmation."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
from importlib.resources import files as resource_files
import json
import os
from pathlib import Path
import pwd
import re
import sys
import subprocess

from boring_deploy import Failure, root_child, root_input

from .site_layout import CONFIG_FILE
from .site_host import account as account, group as group
from .approval import confirm_apply, plan_digest as plan_digest

from .site_model import check_account_name as check_account_name, validate_home as validate_home

FIELDS = {"execution_user", "control_user", "home",
          "context_mode", "context_source", "context_files"}
# Optional: the name people see for the Pond on the sign-in page.
# Without it: "The Pond" for the first Pond, else its name.
OPTIONAL_FIELDS = {"display_name"}
DISPLAY_NAME = re.compile(r"[^\x00-\x1f\x7f]{1,64}")
HANDBOOKS = ("starter", "engineer", "consultant")


def default_home(execution_user, runner=subprocess.run):
    """Read the default base from the account tool we actually use."""
    result = root_child(["/usr/sbin/useradd", "-D"], spawn=runner,
                        check=True, capture_output=True, text=True)
    defaults = [line[5:] for line in result.stdout.splitlines() if line.startswith("HOME=")]
    if len(defaults) != 1:
        raise ValueError("Cannot determine useradd home base; enter the execution home explicitly")
    proposed = str(Path(defaults[0]) / execution_user)
    try:
        return validate_home(proposed, execution_user)
    except ValueError as error:
        raise ValueError("Unsafe useradd home base; enter the execution home explicitly") from error


@dataclass(frozen=True)
class SetupInputs:
    execution_user: str
    control_user: str
    home: str
    context_mode: str
    context_source: str | None
    context_files: tuple[str, ...]
    display_name: str | None = None

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or not FIELDS <= set(data) <= FIELDS | OPTIONAL_FIELDS:
            raise ValueError("Setup input must contain exactly: " + ", ".join(sorted(FIELDS))
                             + " (optional: " + ", ".join(sorted(OPTIONAL_FIELDS)) + ")")
        display_name = data.get("display_name")
        if display_name is not None and (not isinstance(display_name, str) or display_name != display_name.strip()
                                         or not DISPLAY_NAME.fullmatch(display_name)):
            raise ValueError("display_name must be 1 to 64 printable characters without surrounding spaces")
        for key in ("execution_user", "control_user"):
            check_account_name(data[key], key)
        if data["execution_user"] == data["control_user"]:
            raise ValueError("Execution and controller accounts must have distinct names")
        home = data["home"]
        validate_home(home, data["execution_user"])
        mode = data["context_mode"]
        files = data["context_files"]
        if mode not in (*HANDBOOKS, "import"):
            raise ValueError("context_mode must be starter, engineer, consultant or import")
        if not isinstance(files, list) or any(not isinstance(f, str) or not f for f in files):
            raise ValueError("context_files must be a list of relative filenames")
        if len(set(files)) != len(files):
            raise ValueError("Duplicate context filenames")
        for f in files:
            if Path(f).is_absolute() or ".." in Path(f).parts or str(Path(f)) != f or f == ".":
                raise ValueError("Context filenames must be normalized relative paths")
        source = data["context_source"]
        if mode in HANDBOOKS:
            if source is not None or files:
                raise ValueError("Packaged handbooks accept no import source or files")
        elif not isinstance(source, str) or not Path(source).is_absolute() or ".." in Path(source).parts or not files:
            raise ValueError("Import needs an absolute source directory and an explicit nonempty file list")
        return cls(data["execution_user"], data["control_user"],
                   home, mode, source, tuple(files), display_name)

    def as_input(self):
        value = asdict(self)
        value["context_files"] = list(self.context_files)
        if self.display_name is None:
            del value["display_name"]
        return value


def context_payload(inputs, *, metadata=None):
    if inputs.context_mode in HANDBOOKS:
        root = resource_files("boring_crocodile").joinpath("handbooks", inputs.context_mode)
        payload = [(name, root.joinpath(name).read_bytes()) for name in ("HANDBOOK.md", "WELCOME.md")]
        shared = resource_files("boring_crocodile").joinpath("handbooks", "OPERATING_TOGETHER.md")
        return [*payload, ("OPERATING_TOGETHER.md", shared.read_bytes())]
    root = Path(inputs.context_source)
    result = []
    if len(inputs.context_files) > 64:
        raise ValueError("At most 64 initial context files")
    with root_input(root, directory=True) as folder:
        if metadata is not None:
            metadata.extend(folder.describe())
        for name in inputs.context_files:
            with root_input(root / name) as source:
                # Bound the read on the admitted descriptor, never reopen its name.
                with os.fdopen(os.dup(source.fd), "rb") as stream:
                    raw = stream.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError("Initial context document exceeds 1 MiB")
                raw.decode("utf-8")
                result.append((name, raw))
                if metadata is not None:
                    metadata.extend(row for row in source.describe() if row not in metadata)
    if "WELCOME.md" not in inputs.context_files:
        from .instructions import WELCOME
        result.append(("WELCOME.md", WELCOME.encode()))
    return result


def inspect(inputs, config_path=CONFIG_FILE):
    from .setup_apply import plan
    return plan(inputs, config_path=config_path)

def wizard(ask=input, tell=None, *, pond=None, controller=None):
    """The same answers as a setup file, asked one by one; each answer is
    checked as it is given, and asked again with the reason."""
    tell = tell or (lambda text: print(text, file=sys.stderr))

    def field(label, default="", check=None):
        while True:
            answer = ask(label + (f" [{default}]" if default else "") + ": ").strip() or default
            if check is None:
                return answer
            try:
                check(answer)
                return answer
            except ValueError as error:
                tell(f"  {error}. Again, please.")

    def distinct(name):
        check_account_name(name, "The Crocodile's account")
        if name == execution:
            raise ValueError("The Crocodile's account must differ from the Pond's")

    def directory(path):
        if not path.startswith("/") or not Path(path).is_dir():
            raise ValueError("Give an absolute path to an existing folder")

    def document(name):
        if name in files:
            raise ValueError(name + " is already in the list")
        if Path(name).is_absolute() or ".." in Path(name).parts or str(Path(name)) != name or name == ".":
            raise ValueError("Give a path relative to " + source + ", without '..'")
        if not (Path(source) / name).is_file():
            raise ValueError("There is no file " + name + " in " + source)

    def display(name):
        if name and not DISPLAY_NAME.fullmatch(name):
            raise ValueError("At most 64 printable characters")

    execution = field("The Pond's account (commands run as it)",
                      f"{pond}-user" if pond else "boring-crocodile",
                      lambda name: check_account_name(name, "The Pond's account"))
    if controller:
        tell(f"The Crocodile's account: {controller} (the site's).")
        control = controller
    else:
        control = field("The Crocodile's account", "boring-crocodile-mcp", distinct)
    try:
        proposed_home = default_home(execution)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        tell(f"  No home to propose ({error}).")
        proposed_home = ""
    def safe_home(path):
        validate_home(path, execution)
        parent = Path(path).parent
        if not parent.is_dir():
            raise ValueError(f"The folder {parent} does not exist; create it first "
                             "(owned by root, writable by root only), or choose another home")
        for folder in (parent, *parent.parents):
            info = folder.lstat()
            if folder.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError(f"{folder} must belong to root and be writable by root only")

    home = field("The Pond's home", proposed_home, safe_home)
    shown = field("The name people see on sign-in", pond or "The Pond", display)
    def handbook(value):
        if value not in (*HANDBOOKS, "import"):
            raise ValueError("Answer starter, engineer, consultant or import")

    mode = field("Handbook: starter, engineer (code/systems), consultant (research), or import",
                 "starter", handbook)
    source, files = None, []
    if mode == "import":
        source = field("Folder to import from", check=directory)
        while True:
            name = field("A file in it, in reading order (empty when done)" if files
                         else "The first file in it, the one read first",
                         check=lambda value: None if not value and files else document(value))
            if not name:
                break
            files.append(name)
    return SetupInputs.parse(dict(execution_user=execution, control_user=control, home=home,
                                  context_mode=mode, context_source=source, context_files=files)
                             | ({"display_name": shown} if shown != (pond or "The Pond") else {}))


def terminal_ask(prompt):
    """One answer from the terminal, with line editing (Backspace, arrows) where
    Python has readline. The prompt goes to the terminal, never into stdout
    that is redirected to a file."""
    try:
        import readline  # noqa: F401  (input() edits lines once it is loaded)
    except ImportError:
        pass
    if sys.stdout.isatty():
        return input(prompt)
    print(prompt, end="", file=sys.stderr, flush=True)
    return input()


def site_controller(config=CONFIG_FILE):
    from .site_model import SiteDescription
    return SiteDescription.read(Path(config)).controller.name

def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def read_input_json(path):
    """Read proposed setup JSON once through Common's protected-input boundary."""
    with root_input(Path(path).absolute()) as source:
        data = json.loads(source.read_text(), object_pairs_hook=unique_object)
        print("\n".join(source.describe()), file=sys.stderr)
        return data


def save_inputs(path, inputs):
    # Exclusive creation avoids clobbering even a symlink to an existing document.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(inputs.as_input(), stream, indent=2)
        stream.write("\n")


def review_summary(plan):
    names = ", ".join(item["filename"] for item in plan["documents"])
    return (f"Provision Pond {plan['pond']}: {plan['inputs']['execution_user']} and "
            f"controller {plan['inputs']['control_user']}.\n"
            f"Seed missing handbook files: {names}. Existing files and secrets are kept.\n"
            "Description access: main config to controller/all Ponds; each Pond file to its worker/controller only.\n"
            "The declaration stays parked; provisioning starts nothing.\n"
            + "\n".join(plan.get("operator_inputs", [])))

def main(argv=None):
    parser = argparse.ArgumentParser(description="Plan Pond provisioning or site configuration; apply after typed confirmation.",
                                     allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--wizard", action="store_true")
    mode.add_argument("--site-wizard", action="store_true",
                      help="Configure the controller, listener and service policy after provisioning")
    mode.add_argument("--site-config", type=Path,
                      help="Site policy JSON after provisioning")
    mode.add_argument("--config", type=Path, help="JSON inputs; --apply still requires typed confirmation")
    mode.add_argument("--site-extend", action="store_true",
                      help="Enable explicitly named Ponds through the common site render")
    mode.add_argument("--site-render", action="store_true",
                      help="Reconcile the desired site and run its repair tail")
    parser.add_argument("--add-pond", metavar="NAME", help="Create a new Pond's accounts and home for the site's controller; then --site-extend")
    parser.add_argument("--save-inputs", type=Path, help="Create a reusable input file without overwriting")
    parser.add_argument("--apply", action="store_true", help="Apply the selected operation as root after reviewing its plan")
    parser.add_argument("--grant-services", metavar="NAMES",
                        help="with --site-extend: 'all' or comma-separated services the new Pond may call")
    for flag, text in (("--memory", "memory ceiling per new Pond, e.g. 2G"),
                       ("--swap", "swap ceiling per new Pond, e.g. 512M"),
                       ("--tasks", "process/thread ceiling per new Pond"),
                       ("--bring-back", "explicit Ponds to enable or repair, comma-separated; omission selects none")):
        parser.add_argument(flag, help="with --site-extend: " + text)
    args = parser.parse_args(argv)

    extend_only = [("--grant-services", args.grant_services), ("--memory", args.memory),
                   ("--swap", args.swap), ("--tasks", args.tasks), ("--bring-back", args.bring_back)]
    if not args.site_extend and any(value for _, value in extend_only):
        parser.error("--grant-services, --memory, --swap, --tasks and --bring-back belong to --site-extend")
    if args.site_render:
        if args.add_pond or args.save_inputs or any(value for _, value in extend_only):
            parser.error("--site-render takes only --apply")
        from .site_render import main as render_site_files
        forwarded = ["--apply"] if args.apply else []
        return render_site_files(forwarded)
    if args.site_extend:
        if args.add_pond or args.save_inputs:
            parser.error("--site-extend does not accept account setup options")
        from .site_extend import main as extend_site
        forwarded = [item for flag, value in extend_only if value for item in (flag, value)]
        if args.apply:
            forwarded.append("--apply")
        return extend_site(forwarded)

    if args.site_wizard or args.site_config:
        if args.add_pond or args.save_inputs:
            parser.error("Service setup does not accept account setup options")
        from .site_setup import main as configure_site
        forwarded = (["--wizard"] if args.site_wizard
                     else ["--config", str(args.site_config)])
        if args.apply:
            forwarded.append("--apply")
        return configure_site(forwarded)

    try:
        if args.wizard:
            if not sys.stdin.isatty():
                raise ValueError("--wizard requires a terminal; use --config to read inputs from a file")
            print("Account/context setup. Existing data will not be overwritten.", file=sys.stderr)
            inputs = wizard(terminal_ask, pond=args.add_pond,
                            controller=site_controller() if args.add_pond else None)
        else:
            inputs = SetupInputs.parse(read_input_json(args.config))
        if args.add_pond:
            from .setup_apply import plan_additional
            result = plan_additional(inputs, args.add_pond)
        else:
            result = inspect(inputs)
        if args.save_inputs:
            save_inputs(args.save_inputs, inputs)
        result["plan_digest"] = plan_digest(result)
        if args.apply:
            from .setup_apply import apply
            if args.wizard:
                print("\n" + review_summary(result) + "\n", file=sys.stderr)
            if result["review"]:
                print(json.dumps(result, indent=2), file=sys.stderr)
                print("Nothing was changed. Settle these, then run setup again.", file=sys.stderr)
                return 3
            digest = confirm_apply(result, "create these accounts and the home")
            if args.add_pond:
                from .setup_apply import apply_additional
                outcome = apply_additional(inputs, args.add_pond, digest)
            else:
                outcome = apply(inputs, digest)
            if args.wizard:
                print("Done: the accounts and the home are in place; nothing runs yet.", file=sys.stderr)
                print("Next: " + ("boring-crocodile-setup --site-extend --bring-back " + args.add_pond + " --apply" if args.add_pond else
                                  "boring-crocodile-setup --site-wizard --apply, or --site-config FILE --apply; "
                                  "configure the controller, listener and service policy."),
                      file=sys.stderr)
            else:
                print(json.dumps(outcome, indent=2))
            return 0
        print(json.dumps(result, indent=2))
        return 3 if result["review"] else 0
    except (Failure, ValueError, OSError, EOFError, subprocess.SubprocessError) as error:
        print("Setup failed: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
