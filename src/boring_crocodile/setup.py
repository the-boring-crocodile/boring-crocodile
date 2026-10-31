"""Shared wizard/unattended planning; privileged apply requires explicit intent."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import grp
import hashlib
from importlib.resources import files as resource_files
import json
import os
from pathlib import Path
import pwd
import re
import sys
import subprocess
from .site_layout import CONFIG_FILE

NAME = re.compile(r"[a-z_][a-z0-9_-]{0,30}")
HOME_PART = re.compile(r"[A-Za-z0-9_-]+")
RESERVED_HOME_PARENTS = {"bin", "boot", "dev", "etc", "lib", "lib64",
                         "proc", "root", "run", "sbin", "sys", "tmp", "usr", "var"}
FIELDS = {"execution_user", "control_user", "home",
          "context_mode", "context_source", "context_files"}
# Optional: the name people see for the Pond (the sign-in page, pond_ping and
# pond_profile). Without it: "The Pond" for the first Pond, else its name.
OPTIONAL_FIELDS = {"display_name"}
DISPLAY_NAME = re.compile(r"[^\x00-\x1f\x7f]{1,64}")

def validate_home(home, execution_user):
    """A dedicated leaf under a protected host-owned parent."""
    if (not isinstance(home, str) or not home.startswith("/")
            or str(Path(home)) != home or any(part in ("", ".", "..") for part in home.split("/")[1:])
            or len(Path(home).parts) < 3 or len(Path(home).parts) > 8
            or any(not HOME_PART.fullmatch(part) for part in Path(home).parts[1:])
            or Path(home).parts[1] in RESERVED_HOME_PARENTS
            or Path(home).name != execution_user):
        raise ValueError(f"The home must be an absolute path ending in /{execution_user}, two to seven "
                         "folders deep, under a parent like /home or /srv (not /etc, /root, /tmp, /usr, "
                         "/var or another system folder)")
    return home

def check_account_name(name, key="account"):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError(key + " must be a Unix account name: lower-case letters, digits, '-' and '_', "
                         "starting with a letter or '_', at most 31 characters")
    if name in {"root", "nobody", "nogroup"}:
        raise ValueError(key + " is reserved: " + name)
    return name


def default_home(execution_user, runner=subprocess.run):
    """Read the default base from the account tool we actually use."""
    result = runner(["/usr/sbin/useradd", "-D"], check=True, capture_output=True, text=True)
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
        if mode not in ("starter", "import"):
            raise ValueError("context_mode must be starter or import")
        if not isinstance(files, list) or any(not isinstance(f, str) or not f for f in files):
            raise ValueError("context_files must be a list of relative filenames")
        if len(set(files)) != len(files):
            raise ValueError("Duplicate context filenames")
        for f in files:
            if Path(f).is_absolute() or ".." in Path(f).parts or str(Path(f)) != f or f == ".":
                raise ValueError("Context filenames must be normalized relative paths")
        source = data["context_source"]
        if mode == "starter":
            if source is not None or files:
                raise ValueError("Starter mode accepts no import source or files")
        elif not isinstance(source, str) or not Path(source).is_absolute() or ".." in Path(source).parts or not files:
            raise ValueError("Import needs an absolute source directory and an explicit nonempty file list")
        return cls(data["execution_user"], data["control_user"],
                   home, mode, source, tuple(files), display_name)

    def as_input(self):
        value = asdict(self)
        value["context_files"] = list(self.context_files)
        if self.display_name is None:  # receipts written before the field match unchanged
            del value["display_name"]
        return value


def account(name):
    try:
        value = pwd.getpwnam(name)
    except KeyError:
        return None
    return {"name": value.pw_name, "uid": value.pw_uid, "gid": value.pw_gid,
            "home": value.pw_dir, "shell": value.pw_shell}


def group(name):
    try:
        value = grp.getgrnam(name)
    except KeyError:
        return None
    return {"name": value.gr_name, "gid": value.gr_gid}


def context_payload(inputs):
    if inputs.context_mode == "starter":
        raw = resource_files("boring_crocodile").joinpath("templates/HANDBOOK.md").read_bytes()
        from .instructions import WELCOME
        return [("HANDBOOK.md", raw), ("WELCOME.md", WELCOME.encode())]
    root = Path(inputs.context_source).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Context source must be a directory")
    result = []
    if len(inputs.context_files) > 64:
        raise ValueError("At most 64 initial context files")
    for name in inputs.context_files:
        path = (root / name).resolve(strict=True)
        if root not in path.parents or not path.is_file():
            raise ValueError("Context import escapes source or is not a regular file")
        with path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Initial context document exceeds 1 MiB")
        raw.decode("utf-8")
        result.append((name, raw))
    if "WELCOME.md" not in inputs.context_files:
        from .instructions import WELCOME
        result.append(("WELCOME.md", WELCOME.encode()))
    return result


def plan_digest(plan):
    value = {k: v for k, v in plan.items() if k != "plan_digest"}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def inspect(inputs, config_path=CONFIG_FILE):
    review = []
    accounts = {}
    for role, name in (("execution", inputs.execution_user), ("control", inputs.control_user)):
        current = account(name)
        accounts[role] = {"requested_name": name, "current": current,
                          "action": "review_existing" if current else "create"}
        if current:
            review.append(f"The account {name} already exists; setup never takes over an existing account")
    groups = {}
    for name in (inputs.execution_user, inputs.control_user):
        current = group(name)
        groups[name] = {"current": current, "action": "review_existing" if current else "create"}
        if current:
            review.append(f"The group {name} already exists; setup never takes over an existing group")
    home = Path(inputs.home)
    # lexists also detects dangling symlinks; never reinterpret these as absent.
    for path in (config_path, home, home / "handbook"):
        if os.path.lexists(path):
            review.append(f"{path} already exists; setup never writes over it")
    for path in (home, *home.parents):
        if path.is_symlink():
            review.append(f"{path} is a symlink; the home's path must not contain one")
    parent = home.parent
    if not parent.is_dir():
        review.append(f"The folder {parent} does not exist; create it first (owned by root, "
                      "writable by root only)")
    else:
        for path in (parent, *parent.parents):
            info = path.lstat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                review.append(f"{path} must belong to root and be writable by root only")
                break
    documents = [{"filename": name, "sha256": hashlib.sha256(raw).hexdigest(),
                  "bytes": len(raw)} for name, raw in context_payload(inputs)]
    return {
        "schema_version": 1,
        "stage": "plan_only",
        "status": "review_required" if review else "proposed",
        "inputs": inputs.as_input(),
        "accounts": accounts,
        "groups": groups,
        "memberships": [
            {"user": inputs.control_user, "group": inputs.execution_user},
        ],
        "directories": [
            {"path": inputs.home, "owner": inputs.execution_user, "mode": "0700"},
            {"path": str(home / "handbook"), "owner": inputs.execution_user, "mode": "0700"},
            {"path": str(home / "work"), "owner": inputs.execution_user, "mode": "0700"},
            {"path": str(home / "lab"), "owner": inputs.execution_user, "mode": "0700"},
        ],
        "context": {"mode": inputs.context_mode, "documents": documents,
                    "starter": "HANDBOOK.md" if inputs.context_mode == "starter" else None,
                    "overwrite_existing": False, "publish_automatically": False},
        "review": review,
        "pending": ["Apply checks the host again before it creates anything",
                    "The handbook files are written as the Pond's account",
                    "Then the site: services, the approval secret, a health check"],
    }


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
    def starter_or_import(value):
        if value not in ("starter", "import"):
            raise ValueError("Answer starter or import")

    mode = field("Handbook: the packaged starter, or import your own files (starter/import)",
                 "starter", starter_or_import)
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


def site_controller(config=CONFIG_FILE, receipt=Path("/etc/boring-crocodile/initial-setup.json")):
    """The Crocodile's account of an existing site, for --add-pond; None if unknown."""
    try:
        if Path(config).exists():
            try:
                import tomllib
            except ImportError:  # Python 3.10
                import tomli as tomllib
            environments = tomllib.loads(Path(config).read_text()).get("environments", {})
            uids = {value.get("control_plane_uid") for value in environments.values()}
            if len(uids) == 1 and None not in uids:
                return pwd.getpwuid(uids.pop()).pw_name
        return json.loads(Path(receipt).read_text())["inputs"]["control_user"]
    except (OSError, KeyError, ValueError, TypeError):
        return None


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def save_inputs(path, inputs):
    # Exclusive creation avoids clobbering even a symlink to an existing document.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(inputs.as_input(), stream, indent=2)
        stream.write("\n")


def review_summary(plan):
    inputs = plan["inputs"]
    names = ", ".join(item["filename"] for item in plan["context"]["documents"])
    source = ("the packaged starter" if inputs["context_mode"] == "starter"
              else "imported from " + str(inputs["context_source"]))
    lines = [f"The Pond's account: {inputs['execution_user']}. Commands and files run as it.",
             f"The Crocodile's account: {inputs['control_user']}, joining the group {inputs['execution_user']}.",
             f"Home: {inputs['home']}, with handbook/, work/ and lab/.",
             f"Handbook: {source} ({names}).",
             "Name people see: " + inputs.get("display_name", plan.get("pond_name") or "The Pond") + ".",
             "Nothing starts yet; the site is the next step."]
    if plan["review"]:
        lines += ["", "Setup cannot go on:"] + ["  - " + item for item in plan["review"]]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Plan initial accounts/context, or explicitly apply on a clean target.",
                                     allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--wizard", action="store_true")
    mode.add_argument("--site-wizard", action="store_true",
                      help="Configure local services after account/context setup")
    mode.add_argument("--site-config", type=Path,
                      help="Unattended service policy JSON after account/context setup")
    mode.add_argument("--config", type=Path, help="Unattended JSON input; no prompts")
    mode.add_argument("--site-extend", action="store_true",
                      help="Bring Ponds created with --add-pond into the running site (site-check first)")
    mode.add_argument("--site-render", action="store_true",
                      help="Write the site's units and tmpfiles as this release renders them; with --guests, "
                           "let running Ponds accept guests (site-check first)")
    parser.add_argument("--guests", metavar="PONDS", help="with --site-render: Ponds that start accepting guests")
    parser.add_argument("--add-pond", metavar="NAME", help="Create a new Pond's accounts and home for the site's controller; then --site-extend")
    parser.add_argument("--save-inputs", type=Path, help="Create a reusable input file without overwriting")
    parser.add_argument("--apply", action="store_true", help="Root-only initial account/context provisioning")
    parser.add_argument("--approve-digest", help="Digest from an explicitly reviewed plan")
    parser.add_argument("--resume", action="store_true", help="Resume recorded initial setup; requires --apply")
    parser.add_argument("--grant-services", metavar="NAMES",
                        help="with --site-extend: 'all' or comma-separated services the new Pond may call")
    for flag, text in (("--memory", "memory ceiling per new Pond, e.g. 2G"),
                       ("--swap", "swap ceiling per new Pond, e.g. 512M"),
                       ("--tasks", "process/thread ceiling per new Pond"),
                       ("--bring-back", "comma-separated parked Ponds to take into the site again")):
        parser.add_argument(flag, help="with --site-extend: " + text)
    args = parser.parse_args(argv)
    if args.resume and not args.apply:
        parser.error("--resume requires --apply")
    if args.approve_digest and not args.apply:
        parser.error("--approve-digest requires --apply")
    if args.apply and args.config and not args.approve_digest:
        parser.error("Unattended apply requires --approve-digest")

    extend_only = [("--grant-services", args.grant_services), ("--memory", args.memory),
                   ("--swap", args.swap), ("--tasks", args.tasks), ("--bring-back", args.bring_back)]
    if not args.site_extend and any(value for _, value in extend_only):
        parser.error("--grant-services, --memory, --swap, --tasks and --bring-back belong to --site-extend")
    if args.guests and not args.site_render:
        parser.error("--guests belongs to --site-render")
    if args.site_render:
        if args.add_pond or args.save_inputs or args.resume or any(value for _, value in extend_only):
            parser.error("--site-render takes only --guests, --apply and --approve-digest")
        from .site_render import main as render_site_files
        forwarded = (["--guests", args.guests] if args.guests else []) + (["--apply"] if args.apply else [])
        if args.approve_digest:
            forwarded += ["--approve-digest", args.approve_digest]
        return render_site_files(forwarded)
    if args.site_extend:
        if args.add_pond or args.save_inputs or args.resume:
            parser.error("--site-extend does not accept account setup options")
        from .site_extend import main as extend_site
        forwarded = [item for flag, value in extend_only if value for item in (flag, value)]
        if args.apply:
            forwarded.append("--apply")
        if args.approve_digest:
            forwarded += ["--approve-digest", args.approve_digest]
        return extend_site(forwarded)

    if args.site_wizard or args.site_config:
        if args.add_pond or args.save_inputs:
            parser.error("Service setup does not accept account setup options")
        from .site_setup import main as configure_site
        forwarded = (["--wizard"] if args.site_wizard
                     else ["--config", str(args.site_config)])
        if args.apply:
            forwarded.append("--apply")
        if args.approve_digest:
            forwarded += ["--approve-digest", args.approve_digest]
        if args.resume:
            forwarded.append("--resume")
        return configure_site(forwarded)

    try:
        if args.wizard:
            if not sys.stdin.isatty():
                raise ValueError("--wizard requires a terminal; use --config for unattended operation")
            print("Account/context setup. Existing data will not be overwritten.", file=sys.stderr)
            inputs = wizard(terminal_ask, pond=args.add_pond,
                            controller=site_controller() if args.add_pond else None)
        else:
            inputs = SetupInputs.parse(json.loads(args.config.read_text(), object_pairs_hook=unique_object))
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
            digest = args.approve_digest
            if args.wizard and not digest:
                print("\n" + review_summary(result) + "\n", file=sys.stderr)
                if result["review"]:
                    print("Nothing was changed. Settle these, then run the wizard again.", file=sys.stderr)
                    return 3
                print("Type APPLY to create these accounts and the home: ",
                      file=sys.stderr, end="", flush=True)
                if sys.stdin.readline().strip() != "APPLY":
                    raise ValueError("Apply was not confirmed")
                digest = result["plan_digest"]
            if args.add_pond:
                from .setup_apply import apply_additional
                outcome = apply_additional(inputs, args.add_pond, digest, resume=args.resume)
            else:
                outcome = apply(inputs, digest, resume=args.resume)
            if args.wizard:
                print("Done: the accounts and the home are in place; nothing runs yet.", file=sys.stderr)
                print("Next: " + ("boring-crocodile-setup --site-extend --apply" if args.add_pond else
                                  "boring-crocodile-setup --site-wizard --apply, or --site-config FILE "
                                  "for a site that will take Pirates (INSTALL, Activate the site)."),
                      file=sys.stderr)
            else:
                print(json.dumps(outcome, indent=2))
            return 0
        print(json.dumps(result, indent=2))
        return 3 if result["review"] else 0
    except (ValueError, OSError, EOFError, subprocess.SubprocessError) as error:
        print("Setup failed: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
