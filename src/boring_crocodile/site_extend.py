"""Bring Ponds created with --add-pond into the running site: boring-crocodile-setup --site-extend.

The site is read as it is, never from a record of the past. The plan starts
with site-check and stops on anything irregular: this command only extends a
site it fully understands. A new Pond is a folder /etc/boring-crocodile/ponds/NAME
whose --add-pond run completed and which config.toml does not name yet; its
accounts and home are checked against the host. Then:

- the files of the existing Ponds are rendered from the values the site uses
  (as site-check does) and must come out unchanged, except the controller
  unit, which gains the new worker, group and credential;
- the new Pond's worker, task socket and unit, slice and tmpfiles file are new;
- config.toml keeps every line it has; the new Pond's section is appended;
- with --grant-services, the new Pond joins the callers of existing services;
- Pirates the site has are not offered the new Pond: when it accepts guests,
  the plan and the answer name each Pirate and the pirate-add command that
  offers it the new Pond (an admission stays the operator's decision).

The plan shows every change as a diff. Apply plans again, compares the digest,
verifies the candidate, backs up the files it replaces
(/var/lib/boring-crocodile-site/site-backups/<time>/), grants the services, installs
(config.toml last), restarts the controller and runs site-check again. One line per extension
goes to /var/lib/boring-crocodile-site/site-log.jsonl: a log for people, never read
back as authority.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

from . import site_setup
from .settings import Settings
from .setup import SetupInputs, plan_digest
from .setup_apply import SetupConflict, pond_name, read_receipt
from .setup_services import ServiceInputs, render, render_site
from .site_check import CONTROLLER, TMPFILES, UNITS, Check, Reality, _diff, _size, pond_order, summary
from .site_layout import SITE_RECORDS, STATE_DIR, append_record, approval_credential, records_dir, site_lock, write_file
from .site_setup import SiteConflict

VAR = SITE_RECORDS
UNIT_SUFFIXES = (".service", ".socket", ".slice", ".target")


class Paths:
    """Where the site lives and how credentials are made; replaceable in tests."""

    def __init__(self, state: Path = STATE_DIR, units: Path = UNITS, tmpfiles: Path = TMPFILES,
                 var: Path = VAR, owner: int = 0):
        self.state, self.units, self.tmpfiles, self.var, self.owner = state, units, tmpfiles, var, owner
        self.config = state / "config.toml"
        self.credentials = state / "credentials"
        self.ponds = state / "ponds"

    def location(self, name: str) -> Path:
        if name == "config.toml":
            return self.config
        return self.tmpfiles / name if name.endswith(".conf") else self.units / name

    def provision(self, names: list[str]) -> None:
        site_setup.provision_credentials(names)

    def check(self, reality: Reality) -> Check:
        return Check(self.config, self.units, self.tmpfiles, self.credentials, reality)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _store(paths: Paths):
    from .admin import oauth_store
    return oauth_store(Settings.load(paths.config))


def _grant_plans(paths: Paths, added: list[str], grant: list[str] | None) -> list[dict[str, object]]:
    """The new Ponds join the callers of the chosen services ("all" or names)."""
    if not grant or not added:
        return []
    store = _store(paths)
    available = {entry["name"]: entry for entry in store.service_list()}
    names = sorted(available) if grant == ["all"] else grant
    unknown = [name for name in names if name not in available]
    if unknown:
        raise SiteConflict("Unknown service: " + ", ".join(unknown))
    plans = []
    for name in names:
        before = sorted(available[name]["ponds"])
        after = sorted(set(before) | set(added))
        if after != before:
            plans.append(store.service_grants(name, after))
    return plans


def budget(memory: str | None = None, swap: str | None = None, tasks: int | None = None,
           task_tasks: int | None = None) -> dict[str, int]:
    """A new Pond's ceilings: the setup defaults unless given (sizes like 2G)."""
    base = site_setup.defaults()
    result = dict(memory_bytes=_size(memory) if memory else base["memory_bytes"],
                  swap_bytes=_size(swap) if swap else base["swap_bytes"],
                  tasks_max=tasks or base["tasks_max"],
                  task_tasks_max=task_tasks or base["task_tasks_max"])
    result["task_tasks_max"] = min(result["task_tasks_max"], result["tasks_max"])
    return result


def _new_ponds(paths: Paths, known: set[str]) -> tuple[list[tuple[str, dict]], list[str]]:
    """Completed --add-pond folders that config.toml does not name yet."""
    complete, incomplete = [], []
    if not paths.ponds.is_dir():
        return complete, incomplete
    for folder in sorted(paths.ponds.iterdir()):
        if folder.name in known:
            continue
        name = pond_name(folder.name)
        journal = read_receipt(folder / "initial-setup.json", paths.owner)
        if journal.get("status") != "complete":
            incomplete.append(name)
        else:
            complete.append((name, journal))
    return complete, incomplete


def _verify_new(name: str, journal: dict, control: str, check: Check, reality: Reality) -> SetupInputs:
    """The accounts --add-pond created must still be what the host has."""
    setup = SetupInputs.parse(journal["inputs"])
    if journal.get("pond_name") != name:
        raise SiteConflict(f"{name}: the --add-pond folder names another Pond")
    if setup.control_user != control:
        raise SiteConflict(f"{name}: made for controller {setup.control_user}, the site runs {control}")
    for account in (setup.execution_user, control):
        try:
            actual = reality.user(account)
        except KeyError as error:
            raise SiteConflict(f"{name}: account {account} not found") from error
        if actual != journal["users"].get(account):
            raise SiteConflict(f"{name}: account {account} differs from what --add-pond created")
    try:
        owner = reality.owner(Path(setup.home))
    except OSError as error:
        raise SiteConflict(f"{name}: home {setup.home} not found") from error
    if owner != setup.execution_user:
        raise SiteConflict(f"{name}: home {setup.home} belongs to {owner}, not {setup.execution_user}")
    gid = journal["users"][setup.execution_user]["gid"]
    if gid not in reality.groups(control):
        raise SiteConflict(f"{name}: the controller is not a member of group {setup.execution_user}")
    for path in sorted(check.pond_files(name)):
        if os.path.lexists(path):
            raise SiteConflict(f"{name}: a file of this Pond is already there: {path}")
    return setup


def _pirate_offers(settings: Settings, extended: Settings, added: list[str], reality: Reality) -> list[dict]:
    """For each Pirate of the site, the command that offers it the new Ponds that accept guests."""
    guests = [name for name in added
              if extended.environments[extended.ponds[name].environment].service_registration_enabled]
    offers = []
    for pirate, uid in sorted(settings.pirate_service_callers.items()) if guests else ():
        try:
            account = reality.uid_name(uid)
        except KeyError:  # site-check found nothing irregular, but the caller's account is the host's
            account = None
        offers.append({"pirate": pirate, "account": account,
                       "command": f"boring-crocodile-admin pirate-add {pirate} --account {account or 'ACCOUNT'} "
                                  f"--ponds {','.join(guests)}"})
    return offers


def _parked(paths: Paths, name: str) -> bool:
    """A Pond taken out with `uninstall --pond` (data kept) still has its credential."""
    return os.path.lexists(paths.credentials / approval_credential(name))


def plan(grant: list[str] | None = None, ceilings: dict[str, int] | None = None, *,
         paths: Paths | None = None, reality: Reality | None = None, bring_back: list[str] | None = None):
    paths, reality = paths or Paths(), reality or Reality()
    report = paths.check(reality).run()
    if report["status"] != "regular":
        raise SiteConflict("site-check found what this command cannot handle; "
                           "resolve it first:\n" + summary(report))
    text = paths.config.read_text()
    settings, raw = Settings.load(paths.config), tomllib.loads(text)
    check = paths.check(reality)
    entries = check.entries(raw, settings)
    first_setup, first_policy, _ = entries[0]
    new, incomplete = _new_ponds(paths, set(settings.ponds))
    wanted = set(bring_back or [])
    parked = [name for name, _ in new if _parked(paths, name)]
    unknown = sorted(wanted - set(parked))
    if unknown:
        raise SiteConflict("Not a parked Pond (taken out with uninstall --pond, data kept): " + ", ".join(unknown))
    new = [(name, journal) for name, journal in new if name not in parked or name in wanted]
    ceilings = ceilings or budget()
    policy = ServiceInputs.parse(vars(first_policy) | ceilings)
    added = []
    for name, journal in new:
        setup = _verify_new(name, journal, first_setup.control_user, check, reality)
        added.append((setup, policy, journal))
    # The same order site-check renders in, wherever the new names sort.
    combined = sorted(entries + added, key=lambda entry: pond_order(entry[2].get("pond_name", "pond")))
    rendered = render_site(combined) if added else {}
    changes, files, offers = [], {}, []
    new_files = {path for name, _ in new for path in check.pond_files(name)}
    for name, body in sorted(rendered.items()):
        if name == "config.toml":
            continue
        path = paths.location(name)
        if os.path.lexists(path):
            current = path.read_text()
            if current == body:
                continue
            if name != CONTROLLER:
                raise SiteConflict("Extending would change a file of an existing Pond: " + str(path))
            changes.append({"path": str(path), "action": "replace", "sha256_before": _sha(current),
                            "diff": _diff(current, body)})
        elif path in new_files:
            changes.append({"path": str(path), "action": "new", "sha256": _sha(body)})
        else:
            raise SiteConflict("Unexpected new file: " + str(path))
        files[str(path)] = body
    if added:
        # config.toml keeps its own lines; each new Pond's section is appended.
        appended = text if text.endswith("\n") else text + "\n"
        for setup, pond_policy, journal in added:
            fragment = render(setup, pond_policy, journal)["config.toml"]
            appended += "\n" + fragment[fragment.index("[ponds."):]
        with tempfile.TemporaryDirectory(prefix="crocodile-config-") as directory:
            candidate = Path(directory) / "config.toml"
            candidate.write_text(appended)
            loaded = Settings.load(candidate)
        if set(loaded.ponds) != set(settings.ponds) | {name for name, _ in new}:
            raise SiteConflict("The extended config.toml does not name exactly the old and new Ponds")
        offers = _pirate_offers(settings, loaded, [name for name, _ in new], reality)
        changes.append({"path": str(paths.config), "action": "append", "sha256_before": _sha(text),
                        "diff": _diff(text, appended)})
        files[str(paths.config)] = appended
    names = [name for name, _ in new]
    extension = {
        "schema_version": 3,
        "kind": "site_extension",
        "added_ponds": names,
        "incomplete_ponds": incomplete,
        "parked_ponds": sorted(set(parked) - wanted),
        "brought_back": sorted(wanted),
        "ceilings": ceilings if names else {},
        "site_check": report["counts"],
        "changes": changes,
        "new_credentials": [approval_credential(name) for name in names if name not in wanted],
        "service_grants": _grant_plans(paths, names, grant),
        "pirates": offers,
        "restart": [CONTROLLER] if names else [],
        "status": "proposed" if names else "nothing_to_add",
    }
    extension["plan_digest"] = plan_digest(extension)
    return extension, files


def backup_folder(paths: Paths) -> tuple[str, Path]:
    """(stamp, a new folder under site-backups/), distinct even within one second."""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backups = records_dir(records_dir(paths.var, owner=paths.owner) / "site-backups", owner=paths.owner)
    backup, attempt = backups / stamp, 1
    while True:
        try:
            backup.mkdir(mode=0o700)
            return stamp, backup
        except FileExistsError:
            attempt += 1
            backup = backups / f"{stamp}-{attempt}"


def apply(approved_digest: str, grant: list[str] | None = None, ceilings: dict[str, int] | None = None, *,
          paths: Paths | None = None, reality: Reality | None = None, runner=subprocess.run,
          settled=site_setup._settled_local_state, bring_back: list[str] | None = None):
    paths, reality = paths or Paths(), reality or Reality()
    with site_lock(paths.state, owner=paths.owner):
        extension, files = plan(grant, ceilings, paths=paths, reality=reality, bring_back=bring_back)
        if extension["plan_digest"] != approved_digest:
            raise SiteConflict("Host or site changed since the reviewed plan")
        if extension["status"] == "nothing_to_add":
            return {**extension, "applied": False}
        with tempfile.TemporaryDirectory(prefix="crocodile-site-") as directory:
            units = []
            for path, body in files.items():
                if path.endswith(UNIT_SUFFIXES):
                    output = Path(directory) / Path(path).name
                    output.write_text(body)
                    units.append(str(output))
            runner(["/usr/bin/systemd-analyze", "verify", *units], check=True,
                   capture_output=True, text=True)
        stamp, backup = backup_folder(paths)
        for change in extension["changes"]:
            if change["action"] != "new":
                shutil.copy2(change["path"], backup / Path(change["path"]).name)
        # Grants first, while the new Ponds are still new: a re-run after a later
        # step failed plans only the grants still missing; once config.toml names
        # the Ponds a re-run finds nothing to add, so nothing must follow it but
        # the restart (see OPERATIONS: recovery).
        if extension["service_grants"]:
            store = _store(paths)
            for grant_plan in extension["service_grants"]:
                store.service_grants(grant_plan["name"], grant_plan["after"], grant_plan["plan_digest"])
        paths.provision(extension["new_credentials"])
        # config.toml last: until it names the new Pond, its files are only extra files.
        ordered = sorted(extension["changes"], key=lambda change: change["path"] == str(paths.config))
        for change in ordered:
            target = Path(change["path"])
            if change["action"] == "new":
                write_file(target, files[change["path"]], mode=0o644, create=True)
            else:  # the file keeps its mode and group
                write_file(target, files[change["path"]])
        for change in extension["changes"]:
            if change["action"] == "new" and change["path"].endswith(".conf"):
                runner(["/usr/bin/systemd-tmpfiles", "--create", change["path"]], check=True)
        runner(["/usr/bin/systemctl", "daemon-reload"], check=True)
        sockets = sorted(Path(c["path"]).name for c in extension["changes"]
                         if c["action"] == "new" and c["path"].endswith(".socket"))
        runner(["/usr/bin/systemctl", "enable", "--now", *sockets], check=True)
        # The controller loads the new configuration, its new group membership
        # and credential, and pulls in the new worker (Requires=).
        runner(["/usr/bin/systemctl", "restart", CONTROLLER], check=True)
        after = paths.check(reality).run()
        append_record(json.dumps({"at": stamp, "kind": "site_extension", "added_ponds": extension["added_ponds"],
                                  "plan_digest": approved_digest, "backup": str(backup),
                                  "site_check_after": after["status"]}), paths.var, owner=paths.owner)
    health = settled(None)
    good = health == "healthy" and after["status"] == "regular"
    return {"status": "extended" if good else "extended_but_degraded",
            "added_ponds": extension["added_ponds"],
            "service_grants": [g["name"] for g in extension["service_grants"]],
            "backup": str(backup), "local_health": health, "site_check": after["status"],
            "pirates": extension["pirates"],
            "next": ("Authorize a client for each new Pond at the public /mcp/<pond> endpoint "
                     "with its approval credential." if good else
                     "Inspect boring-crocodile-admin site-check and status --check.")
                    + "".join(f" To offer it to the Pirate {offer['pirate']}: sudo {offer['command']}"
                              for offer in extension["pirates"])}


def told(outcome, config=STATE_DIR / "config.toml") -> str:
    """The outcome in plain lines, for someone who just typed APPLY."""
    if outcome.get("status") == "nothing_to_add" or "local_health" not in outcome:
        return "Nothing to add; the site is unchanged."
    if outcome["status"] != "extended":
        return (f"The site is {outcome['status'].replace('_', ' ')} "
                f"(health {outcome['local_health']}, site-check {outcome['site_check']}). " + outcome["next"])
    try:
        url = tomllib.loads(Path(config).read_text())["server"]["public_base_url"]
    except (OSError, KeyError, ValueError):
        url = "https://YOUR-ADDRESS"
    lines = ["The site is running with " + ", ".join(outcome["added_ponds"]) + "; site-check is regular."]
    for pond in outcome["added_ponds"]:
        lines.append(f"{pond}: connect a client to {url}/mcp/{pond}; its approval secret: "
                     f"sudo cat {STATE_DIR / 'credentials' / approval_credential(pond)}")
    lines.append(f"The files it replaced are kept in {outcome['backup']}.")
    lines += [f"To offer it to the Pirate {offer['pirate']}: sudo {offer['command']}"
              for offer in outcome["pirates"]]
    return "\n".join(lines)


def review(extension) -> str:
    lines = [f"Add to the running site: {', '.join(extension['added_ponds']) or 'no new Pond'}"]
    if extension["parked_ponds"]:
        lines.append("Parked (taken out with uninstall --pond; bring back with --bring-back NAME): "
                     + ", ".join(extension["parked_ponds"]))
    if extension["brought_back"]:
        lines.append("Brought back with their credential and grants: " + ", ".join(extension["brought_back"]))
    if extension["incomplete_ponds"]:
        lines.append("Not ready (--add-pond did not complete): " + ", ".join(extension["incomplete_ponds"]))
    if extension["ceilings"]:
        c = extension["ceilings"]
        lines.append(f"Ceilings per new Pond: memory {c['memory_bytes'] // 1024**2} MiB, swap "
                     f"{c['swap_bytes'] // 1024**2} MiB, tasks {c['tasks_max']} (per command {c['task_tasks_max']}); "
                     "--memory, --swap and --tasks change them")
    for change in extension["changes"]:
        lines.append(f"{change['action']:8} {change['path']}")
        lines.extend("         " + line for line in change.get("diff", []))
    grants = ", ".join(g["name"] for g in extension["service_grants"]) or "none"
    lines.append(f"New credentials: {len(extension['new_credentials'])}. Service access: {grants}. "
                 f"Restarts: {', '.join(extension['restart']) or 'none'}.")
    for offer in extension["pirates"]:
        lines.append(f"The Pirate {offer['pirate']} is not offered the new Pond; afterwards, to offer it: "
                     f"sudo {offer['command']}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="boring-crocodile-setup --site-extend", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--grant-services", help="'all' or comma-separated service names")
    parser.add_argument("--memory", help="memory ceiling per new Pond, e.g. 2G (default: setup's)")
    parser.add_argument("--swap", help="swap ceiling per new Pond, e.g. 512M (default: 0)")
    parser.add_argument("--tasks", type=int, help="process/thread ceiling per new Pond (default: 256)")
    parser.add_argument("--bring-back", help="comma-separated parked Ponds to take into the site again")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approve-digest")
    args = parser.parse_args(argv)
    grant = args.grant_services.split(",") if args.grant_services else None
    try:
        if os.geteuid() != 0:
            raise SiteConflict("Extending the site requires root (site-check reads the credentials)")
        ceilings = budget(args.memory, args.swap, args.tasks)
        back = args.bring_back.split(",") if args.bring_back else None
        extension, _ = plan(grant, ceilings, bring_back=back)
        if not args.apply:
            print(review(extension), file=sys.stderr)
            print(json.dumps(extension, indent=2))
            return 0
        digest = args.approve_digest
        if not digest:
            if not sys.stdin.isatty():
                raise SiteConflict("Unattended apply requires --approve-digest from the plan")
            print(review(extension), file=sys.stderr)
            print("Type APPLY to extend the running site: ", end="", file=sys.stderr, flush=True)
            if sys.stdin.readline().strip() != "APPLY":
                raise SiteConflict("Extension was not confirmed")
            digest = extension["plan_digest"]
        outcome = apply(digest, grant, ceilings, bring_back=back)
        if args.approve_digest:
            print(json.dumps(outcome, indent=2))
        else:
            print(told(outcome), file=sys.stderr)
        return 0 if outcome["status"] in ("extended", "nothing_to_add") else 1
    except (SetupConflict, ValueError, KeyError, OSError, EOFError, subprocess.SubprocessError) as error:
        print("Site extension failed: " + str(error), file=sys.stderr)
        return 2
