"""Write the site's files as this release renders them: boring-crocodile-setup --site-render.

A deploy changes no site file. When a release renders a unit or tmpfiles file
differently, site-check reports the file as differing from the template; this
command writes it. With --guests POND[,POND] it also switches running Ponds to
accept guests (Pirates, `service_registration_enabled`), which is a different
value in the same rendering: its config.toml line, the worker's write path and
the registration folder in its tmpfiles file.

The site is read as it is, as site-check reads it. The plan stops on anything
irregular except files that differ from (or are missing against) what this
release renders: those are what it writes. Local intent belongs in drop-ins,
which it never touches; a hand edit of a base file shows as a diff in the plan
and is replaced only when the plan is approved.

The plan shows every change as a diff, with a digest. Apply plans again,
compares the digest, verifies the units (systemd-analyze), backs up the files it
replaces (/var/lib/boring-crocodile-site/site-backups/<time>/), writes them
(config.toml last), runs systemd-tmpfiles for changed tmpfiles files, reloads
systemd, restarts changed task sockets, the controller and every worker (as a
deploy does: running jobs are interrupted), and runs site-check again. One line
goes to /var/lib/boring-crocodile-site/site-log.jsonl.
"""
from __future__ import annotations

import argparse
import difflib
import grp
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

from . import site_setup
from .pirates import REGISTRATION_GROUP
from .settings import Settings
from .setup import plan_digest
from .setup_apply import SetupConflict
from .setup_services import render_site
from .site_check import CONTROLLER, Reality, _diff, summary
from .setup_apply import read_receipt
from .site_extend import UNIT_SUFFIXES, Paths, _pirate_offers, _sha, backup_folder
from .site_layout import INITIAL_POND, append_record, site_lock, write_file
from .site_setup import SITE_RECEIPT, UNINSTALLED, SiteConflict

# What this command writes; any other irregular finding stops it.
RENDERED_FINDINGS = {"differs from the Crocodile's template", "file missing"}


def _with_guests(text: str, raw: dict, ponds: list[str]) -> str:
    """config.toml with `service_registration_enabled = true` in each named Pond's
    environment section (an existing `= false` line there is replaced), every other
    line as it was."""
    for pond in ponds:
        environment = raw["ponds"][pond]["environment"]
        header = re.compile(r"^\[environments\." + re.escape(environment) + r"\]\s*$", re.M)
        match = header.search(text)
        if match is None:
            raise SiteConflict(f"config.toml has no plain [environments.{environment}] header for {pond}; "
                               "add `service_registration_enabled = true` there by hand")
        following = re.compile(r"^\[", re.M).search(text, match.end())
        end = following.start() if following else len(text)
        section = text[match.end():end]
        existing = re.compile(r"^service_registration_enabled\s*=\s*false[ \t]*$", re.M)
        if existing.search(section):
            section = existing.sub("service_registration_enabled = true", section, count=1)
            text = text[:match.end()] + section + text[end:]
        else:
            text = text[:match.end()] + "\nservice_registration_enabled = true" + text[match.end():]
    return text


def _regular_target(path: Path, owner: int) -> None:
    """A file this command replaces must be a regular file of the site's owner: a
    symlink (a masked unit: /dev/null) or a foreign file is not overwritten."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != owner:
        raise SiteConflict(f"{path} is not a regular file owned by root (masked or linked?); resolve it first")


def _uninstalled(paths: Paths) -> bool:
    receipt = paths.state / SITE_RECEIPT.name
    if not os.path.lexists(receipt):
        return False
    return read_receipt(receipt, paths.owner).get("status") == UNINSTALLED


def _changed_lines(before: str, after: str) -> int:
    lines = difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
    return sum(1 for line in lines if line[:1] in "+-" and not line.startswith(("+++", "---")))


def plan(guests: list[str] | None = None, *, paths: Paths | None = None, reality: Reality | None = None):
    paths, reality = paths or Paths(), reality or Reality()
    if _uninstalled(paths):
        raise SiteConflict("The site is being uninstalled (activation receipt); finish or undo that first")
    report = paths.check(reality).run()
    stopping = [item for item in report["irregular"] if item["what"] not in RENDERED_FINDINGS]
    if stopping:
        raise SiteConflict("site-check found what this command cannot handle; resolve it first:\n"
                           + summary({**report, "irregular": stopping}))
    text = paths.config.read_text()
    settings, raw = Settings.load(paths.config), tomllib.loads(text)
    guests = sorted(set(guests or []))
    unknown = [pond for pond in guests if pond not in settings.ponds]
    if unknown:
        raise SiteConflict("Not a Pond of this site: " + ", ".join(unknown))
    already = [pond for pond in guests
               if settings.environments[settings.ponds[pond].environment].service_registration_enabled]
    if already:
        raise SiteConflict("Already accepts guests: " + ", ".join(already))
    if guests:
        try:
            grp.getgrnam(REGISTRATION_GROUP)
        except KeyError:
            raise SiteConflict(f"Create {REGISTRATION_GROUP} and review guest account membership first") from None
    candidate_text = _with_guests(text, raw, guests) if guests else text
    with tempfile.TemporaryDirectory(prefix="crocodile-render-") as directory:
        candidate = Path(directory) / "config.toml"
        candidate.write_text(candidate_text)
        loaded = Settings.load(candidate)  # the controller must accept it
        check = Paths(candidate.parent, paths.units, paths.tmpfiles, paths.var, owner=paths.owner).check(reality)
        entries = check.entries(tomllib.loads(candidate_text), loaded)
    if set(loaded.ponds) != set(settings.ponds) or not entries or entries[0][2]["pond_name"] != INITIAL_POND:
        raise SiteConflict("The site cannot be rendered from its own values")
    rendered = render_site(entries)
    changes, files = [], {}
    for name, body in sorted(rendered.items()):
        if name == "config.toml":
            continue
        path = paths.location(name)
        if os.path.lexists(path):
            _regular_target(path, paths.owner)
            current = path.read_text()
            if current == body:
                continue
            changes.append({"path": str(path), "action": "replace", "sha256_before": _sha(current),
                            "sha256_after": _sha(body), "diff": _diff(current, body),
                            "diff_lines": _changed_lines(current, body)})
        else:
            changes.append({"path": str(path), "action": "new", "sha256_after": _sha(body),
                            "lines": len(body.splitlines())})
        files[str(path)] = body
    if candidate_text != text:
        _regular_target(paths.config, paths.owner)
        changes.append({"path": str(paths.config), "action": "replace", "sha256_before": _sha(text),
                        "sha256_after": _sha(candidate_text), "diff": _diff(text, candidate_text),
                        "diff_lines": _changed_lines(text, candidate_text)})
        files[str(paths.config)] = candidate_text
    sockets = sorted(Path(c["path"]).name for c in changes if c["path"].endswith(".socket"))
    rendering = {
        "schema_version": 1,
        "kind": "site_render",
        "guests": guests,
        "site_check": report["counts"],
        "changes": changes,
        "restart": ([*sockets, CONTROLLER, *(f"boring-crocodile-worker-{p}.service" for p in sorted(settings.ponds))]
                    if changes else []),
        "pirates": _pirate_offers(settings, loaded, guests, reality) if guests else [],
        "status": "proposed" if changes else "nothing_to_render",
    }
    rendering["plan_digest"] = plan_digest(rendering)
    return rendering, files


def _controller_activity(runner) -> str:
    try:
        result = runner(["/usr/bin/systemctl", "is-active", CONTROLLER],
                        check=False, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise SiteConflict(f"Cannot establish controller activity: {error}") from None
    state = getattr(result, "stdout", "").strip()
    if (result.returncode, state) not in ((0, "active"), (3, "inactive")):
        raise SiteConflict(f"Cannot establish controller activity: {state or 'no state'} "
                           f"(exit {result.returncode})")
    return state


def apply(approved_digest: str, guests: list[str] | None = None, *, paths: Paths | None = None,
          reality: Reality | None = None, runner=subprocess.run, settled=site_setup._settled_local_state):
    paths, reality = paths or Paths(), reality or Reality()
    with site_lock(paths.state, owner=paths.owner):
        rendering, files = plan(guests, paths=paths, reality=reality)
        if rendering["plan_digest"] != approved_digest:
            raise SiteConflict("Host or site changed since the reviewed plan")
        if rendering["status"] == "nothing_to_render":
            return {**rendering, "applied": False}
        before_activity = _controller_activity(runner)
        with tempfile.TemporaryDirectory(prefix="crocodile-site-") as directory:
            units = []
            for path, body in files.items():
                if path.endswith(UNIT_SUFFIXES):
                    output = Path(directory) / Path(path).name
                    output.write_text(body)
                    units.append(str(output))
            if units:
                try:
                    runner(["/usr/bin/systemd-analyze", "verify", *units], check=True, capture_output=True, text=True)
                except subprocess.CalledProcessError as error:
                    raise SiteConflict("systemd-analyze refuses the rendered units; nothing was written:\n"
                                       + ((error.stderr or "") + (error.stdout or "")).strip()[-2000:]) from None
        stamp, backup = backup_folder(paths)
        for change in rendering["changes"]:
            if change["action"] != "new":
                (backup / Path(change["path"]).name).write_bytes(Path(change["path"]).read_bytes())
        for change in sorted(rendering["changes"], key=lambda c: c["path"] == str(paths.config)):
            target = Path(change["path"])
            if change["action"] == "new":
                write_file(target, files[change["path"]], mode=0o644, create=True)
            else:  # the file keeps its mode and group
                write_file(target, files[change["path"]])
        # Written: recorded now, so a failure below still leaves the log line and the
        # backup's name. A rerun plans nothing (the files already match), so the
        # steps below are said in full when one fails (OPERATIONS: recovery).
        append_record(json.dumps({"at": stamp, "kind": "site_render", "guests": rendering["guests"],
                                  "files": [c["path"] for c in rendering["changes"]],
                                  "plan_digest": approved_digest, "backup": str(backup)}),
                      paths.var, owner=paths.owner)
        tmpfiles = [c["path"] for c in rendering["changes"] if c["path"].endswith(".conf")]
        steps = ([["/usr/bin/systemd-tmpfiles", "--create", *tmpfiles]] if tmpfiles else []) + [
            ["/usr/bin/systemctl", "daemon-reload"],
            # try-restart: what runs restarts (with the new files), a stopped site stays stopped.
            ["/usr/bin/systemctl", "try-restart", *rendering["restart"]]]
        for index, command in enumerate(steps):
            try:
                runner(command, check=True)
            except (OSError, subprocess.SubprocessError) as error:
                remaining = "; ".join(" ".join(step) for step in steps[index:])
                raise SiteConflict(f"The files are written (old ones in {backup}), but `{' '.join(command)}` "
                                   f"failed: {error}. Finish by hand: {remaining}; then "
                                   "boring-crocodile-admin site-check") from None
        activity_error = None
        try:
            activity = _controller_activity(runner)
        except SiteConflict as error:
            activity_error = str(error)
            activity = "unknown"  # files were written; report degraded with the recovery guidance
        after = paths.check(reality).run()
    stopped = before_activity == activity == "inactive"
    health = "stopped" if activity == "inactive" else settled(None) if activity == "active" else "unknown"
    good = (stopped or health == "healthy") and after["status"] == "regular"
    offers = " ".join(f"To offer it to the Pirate {o['pirate']}: sudo {o['command']}." for o in rendering["pirates"])
    return {"status": "rendered" if good else "rendered_but_degraded", "guests": rendering["guests"],
            "files": [c["path"] for c in rendering["changes"]], "backup": str(backup),
            "local_health": health, "site_check": after["status"], "pirates": rendering["pirates"],
            **({"activity_error": activity_error} if activity_error else {}),
            "next": (offers if good else "Inspect boring-crocodile-admin site-check and status --check.")}


def review(rendering) -> str:
    lines = [f"Render the site's files as this release writes them"
             + (f"; accept guests in: {', '.join(rendering['guests'])}" if rendering["guests"] else "")]
    for change in rendering["changes"]:
        lines.append(f"{change['action']:8} {change['path']}"
                     + (f" ({change['lines']} lines)" if change["action"] == "new" else ""))
        lines.extend("         " + line for line in change.get("diff", []))
        shown, total = len(change.get("diff", [])), change.get("diff_lines", 0)
        if total > shown:
            lines.append(f"         ... {total - shown} more changed lines (sha256 after: {change['sha256_after']})")
    if any(c["action"] == "new" and c["path"].endswith(UNIT_SUFFIXES) for c in rendering["changes"]):
        lines.append("New units are written, not enabled: enable them as the release notes say.")
    lines.append(f"Restarts, if running: {', '.join(rendering['restart']) or 'none'} "
                 "(running jobs are interrupted, as by a deploy)."
                 if rendering["changes"] else "Nothing to write; the site is as this release renders it.")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="boring-crocodile-setup --site-render", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--guests", help="comma-separated running Ponds that start accepting guests (Pirates)")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approve-digest")
    args = parser.parse_args(argv)
    guests = args.guests.split(",") if args.guests else None
    try:
        if os.geteuid() != 0:
            raise SiteConflict("Rendering the site requires root (site-check reads the credentials)")
        rendering, _ = plan(guests)
        if not args.apply:
            print(review(rendering), file=sys.stderr)
            print(json.dumps(rendering, indent=2))
            return 0
        digest = args.approve_digest
        if not digest:
            if not sys.stdin.isatty():
                raise SiteConflict("Unattended apply requires --approve-digest from the plan")
            print(review(rendering), file=sys.stderr)
            print("Type APPLY to write these files and restart the site: ", end="", file=sys.stderr, flush=True)
            if sys.stdin.readline().strip() != "APPLY":
                raise SiteConflict("Rendering was not confirmed")
            digest = rendering["plan_digest"]
        outcome = apply(digest, guests)
        print(json.dumps(outcome, indent=2))
        return 0 if outcome["status"] in ("rendered", "nothing_to_render") else 1
    except (SetupConflict, ValueError, KeyError, OSError, EOFError, subprocess.SubprocessError) as error:
        print("Site render failed: " + str(error), file=sys.stderr)
        return 2
