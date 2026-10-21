"""Admit a Pirate to the running site, or take one out again:
boring-crocodile-admin pirate-add NAME --account ACCOUNT --ponds all|a,b / pirate-remove NAME.

A Pirate is a Unix account that serves flavours to Ponds (pirates.py). Its own
files are its installer's (boring-pirates); what the site gives it is here:

- config.toml: `[pirate_services.callers] NAME = <the account's uid>`, so it may
  call the services it is granted;
- group pond-pirates: the account, so it may register with the Ponds;
- group pirate-clients (the Pirate's socket group): the worker account of each
  Pond it serves, so they may call it.

Like --site-extend: site-check first, stop on anything irregular; the plan shows
every change; apply plans again and compares the digest, backs up config.toml
(/var/lib/boring-crocodile-site/site-backups/<time>/), changes the groups, writes
config.toml, restarts the workers of the Ponds it serves and the controller,
runs site-check again and logs one line to site-log.jsonl. Every served worker
restarts, also one that was in pirate-clients already: a worker gets a new group
only when it starts, and a re-run after an apply that stopped after the group
step cannot tell which workers joined then. So a re-run finishes the admission.

The answer names what the Pirate's own config.json needs: the workers' uids
(`client_uids`) and the service authority's socket and uid.

pirate-add for a Pirate the site has already, with the same account, offers it
more Ponds (a Pond added later): it plans only the Ponds whose worker cannot
call it yet, judged from the host as it is: the worker is not in
pirate-clients, or its running process started before it joined (a process
gets its groups when it starts). Those workers join and restart; config.toml
and the controller stay as they are. So a re-run after an interruption between
the group step and the restart finishes it. Nothing to add: "already serves",
no change. Either way the answer names the Pirate's side: its config.json's
`client_uids` must include the workers' uids, then the Pirate restarts (it
reads config.json at start and registers where a Pond's register.sock belongs
to one of them). pirate-add never takes a Pond away: pirate-remove does, for
the whole Pirate. Another account under the same name is refused.

pirate-remove takes the caller and the pond-pirates membership back and restarts
the controller and the worker of every Pond that accepts guests: membership in
pond-pirates is site-wide, so the Pirate may have registered with any of them,
and a worker keeps a registration until it restarts. Workers stay in
pirate-clients (other Pirates may serve the same Ponds); it says so.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from . import site_setup
from .pirates import REGISTRATION_GROUP
from .settings import Settings
from .setup import plan_digest
from .site_check import CONTROLLER, Reality, _diff, _values, summary
from .site_extend import Paths, _sha, backup_folder
from .site_layout import append_record
from .site_layout import site_lock, write_file
from .site_setup import SiteConflict

CLIENTS_GROUP = "pirate-clients"
NAME = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")
CALLERS = "[pirate_services.callers]"


def _site(paths: Paths, reality: Reality) -> tuple[str, Settings, dict]:
    report = paths.check(reality).run()
    if report["status"] != "regular":
        raise SiteConflict("site-check found what this command cannot handle; resolve it first:\n"
                           + summary(report))
    return paths.config.read_text(), Settings.load(paths.config), report


def _guests(settings: Settings, pond: str) -> bool:
    """The Pond accepts guests (Pirates register, services enroll)."""
    return settings.environments[settings.ponds[pond].environment].service_registration_enabled


def _worker_unit(pond: str) -> str:
    return f"boring-crocodile-worker-{pond}.service"


def _workers(paths: Paths, settings: Settings, ponds: list[str]) -> dict[str, str]:
    """Pond -> its worker's account, as its unit says. "all": every Pond that accepts guests."""
    chosen = sorted(p for p in settings.ponds if _guests(settings, p)) if ponds == ["all"] else ponds
    unknown = [name for name in chosen if name not in settings.ponds]
    if unknown:
        raise SiteConflict("Not a Pond of this site: " + ", ".join(unknown))
    closed = [name for name in chosen if not _guests(settings, name)]
    if closed or not chosen:
        raise SiteConflict("A Pirate registers with a Pond that accepts guests (service_registration_enabled); "
                           + ("not: " + ", ".join(closed) if closed else "no Pond here does"))
    workers = {}
    for pond in chosen:
        unit = paths.units / _worker_unit(pond)
        user = _values(unit.read_text()).get("User", "")
        if not user:
            raise SiteConflict(f"{unit} names no User=")
        workers[pond] = user
    return workers


def _members(reality: Reality, group: str, accounts: list[str]) -> list[str]:
    """The accounts already in group (supplementary or primary)."""
    entry = reality.group(group)
    if entry is None:
        return []
    return [account for account in accounts if entry["gid"] in reality.groups(account)]


def _with_caller(text: str, name: str, uid: int) -> str:
    """config.toml keeps every line; the caller joins [pirate_services.callers]."""
    lines = text.splitlines()
    if CALLERS in (line.strip() for line in lines):
        at = [line.strip() for line in lines].index(CALLERS)
        lines.insert(at + 1, f"{name} = {uid}")
        return "\n".join(lines) + "\n"
    return (text if text.endswith("\n") else text + "\n") + f"\n{CALLERS}\n{name} = {uid}\n"


def _without_caller(text: str, name: str) -> str:
    lines, inside, kept = text.splitlines(), False, []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            inside = stripped == CALLERS
        if inside and re.match(rf"{re.escape(name)}\s*=", stripped):
            continue
        kept.append(line)
    return "\n".join(kept) + "\n"


def _loaded(text: str) -> Settings:
    with tempfile.TemporaryDirectory(prefix="crocodile-config-") as directory:
        candidate = Path(directory) / "config.toml"
        candidate.write_text(text)
        return Settings.load(candidate)


def _authority(settings: Settings, reality: Reality, paths: Paths) -> dict:
    socket = settings.pirate_service_socket
    try:
        owner = reality.owner(socket)
    except OSError:  # not created yet: the controller makes it
        owner = _values((paths.units / CONTROLLER).read_text()).get("User", "")
    return {"socket": str(socket), "uid": reality.user(owner)["uid"]}


def _pirate_side(name: str, client_uids: list[int]) -> str:
    """What the Pirate's own configuration needs (its installer's files, boring-pirates)."""
    return (f"The Pirate's config.json (boring-pirates: /etc/boring-pirates/{name}/config.json): client_uids "
            f"must include {', '.join(map(str, client_uids))}; then systemctl restart boring-pirate@{name}.service")


def _plan_extend(name: str, account: str, ponds: list[str], paths: Paths, reality: Reality,
                 settings: Settings, report: dict) -> dict:
    """An admitted Pirate offered (more) Ponds: only what the host does not have yet."""
    admitted = settings.pirate_service_callers[name]
    try:
        uid = reality.user(account)["uid"]
    except KeyError:
        raise SiteConflict(f"No account {account}") from None
    if uid != admitted:
        try:
            known = f"account {reality.uid_name(admitted)}, uid {admitted}"
        except KeyError:
            known = f"uid {admitted}, no account"
        raise SiteConflict(f"The site's Pirate {name} is {known}, not {account}: one name, one account "
                           f"(pirate-remove {name} first to admit another)")
    clients = reality.group(CLIENTS_GROUP)
    for group, entry in ((REGISTRATION_GROUP, reality.group(REGISTRATION_GROUP)), (CLIENTS_GROUP, clients)):
        if entry is None:
            raise SiteConflict(f"No group {group}")
    workers = _workers(paths, settings, ponds)
    members = set(_members(reality, CLIENTS_GROUP, sorted(set(workers.values()))))
    adding = []
    for pond, user in sorted(workers.items()):
        running = reality.running_groups(_worker_unit(pond))  # None: starts with what the group file says
        if user not in members or (running is not None and clients["gid"] not in running):
            adding.append(pond)
    groups = []
    if not _members(reality, REGISTRATION_GROUP, [account]):
        groups.append({"group": REGISTRATION_GROUP, "add": [account]})
    joining = sorted({workers[pond] for pond in adding} - members)
    if joining:
        groups.append({"group": CLIENTS_GROUP, "add": joining})
    client_uids = sorted({reality.user(user)["uid"] for user in workers.values()})
    extension = {
        "schema_version": 1,
        "kind": "pirate_extension",
        "pirate": name, "account": account, "uid": uid,
        "ponds": adding, "served": sorted(set(workers) - set(adding)),
        "site_check": report["counts"],
        "changes": [],
        "groups": groups,
        "restart": sorted(map(_worker_unit, adding)),
        "for_site_json": {"client_uids": client_uids,
                          "service_authority": _authority(settings, reality, paths)},
        "pirate_side": _pirate_side(name, client_uids),
        "status": "proposed" if adding or groups else "already_serves",
    }
    extension["plan_digest"] = plan_digest(extension)
    return extension


def plan_add(name: str, account: str, ponds: list[str], *, paths: Paths | None = None,
             reality: Reality | None = None) -> tuple[dict, str]:
    """A new Pirate's admission, or for one the site has already, its extension."""
    paths, reality = paths or Paths(), reality or Reality()
    if not NAME.match(name):
        raise SiteConflict("A Pirate's name: lowercase letters, digits and dashes, starting with a letter")
    text, settings, report = _site(paths, reality)
    if name in settings.pirate_service_callers:
        return _plan_extend(name, account, ponds, paths, reality, settings, report), text
    try:
        uid = reality.user(account)["uid"]
    except KeyError:
        raise SiteConflict(f"No account {account}: the Pirate's installer creates it first") from None
    if uid in settings.pirate_service_callers.values():
        raise SiteConflict(f"Account {account} is a Pirate of this site already")
    for group in (REGISTRATION_GROUP, CLIENTS_GROUP):
        if reality.group(group) is None:
            raise SiteConflict(f"No group {group}" + (": the Pirate's installer creates it"
                                                      if group == CLIENTS_GROUP else ""))
    workers = _workers(paths, settings, ponds)
    joining = sorted(set(workers.values()) - set(_members(reality, CLIENTS_GROUP, sorted(set(workers.values())))))
    after = _with_caller(text, name, uid)
    if _loaded(after).pirate_service_callers.get(name) != uid:
        raise SiteConflict("The changed config.toml does not name the new caller")
    groups = []
    if not _members(reality, REGISTRATION_GROUP, [account]):
        groups.append({"group": REGISTRATION_GROUP, "add": [account]})
    if joining:
        groups.append({"group": CLIENTS_GROUP, "add": joining})
    admission = {
        "schema_version": 1,
        "kind": "pirate_admission",
        "pirate": name, "account": account, "uid": uid, "ponds": sorted(workers),
        "site_check": report["counts"],
        "changes": [{"path": str(paths.config), "action": "replace", "sha256_before": _sha(text),
                     "diff": _diff(text, after)}],
        "groups": groups,
        "restart": sorted(map(_worker_unit, workers)) + [CONTROLLER],
        "for_site_json": {"client_uids": sorted(reality.user(user)["uid"] for user in set(workers.values())),
                          "service_authority": _authority(settings, reality, paths)},
        "status": "proposed",
    }
    admission["plan_digest"] = plan_digest(admission)
    return admission, after


def plan_remove(name: str, *, paths: Paths | None = None, reality: Reality | None = None) -> tuple[dict, str]:
    paths, reality = paths or Paths(), reality or Reality()
    text, settings, report = _site(paths, reality)
    if name not in settings.pirate_service_callers:
        raise SiteConflict(f"The site has no Pirate named {name}")
    uid = settings.pirate_service_callers[name]
    try:
        account = reality.uid_name(uid)
    except KeyError:
        account = None
    after = _without_caller(text, name)
    if name in _loaded(after).pirate_service_callers:
        raise SiteConflict("The changed config.toml still names the caller")
    groups = ([{"group": REGISTRATION_GROUP, "remove": [account]}]
              if account and _members(reality, REGISTRATION_GROUP, [account]) else [])
    guests = sorted(pond for pond in settings.ponds if _guests(settings, pond))
    removal = {
        "schema_version": 2,
        "kind": "pirate_removal",
        "pirate": name, "account": account, "uid": uid, "ponds": guests,
        "site_check": report["counts"],
        "changes": [{"path": str(paths.config), "action": "replace", "sha256_before": _sha(text),
                     "diff": _diff(text, after)}],
        "groups": groups,
        "restart": sorted(map(_worker_unit, guests)) + [CONTROLLER],
        "kept": f"the Ponds' workers stay in {CLIENTS_GROUP} (other Pirates may serve them)",
        "status": "proposed",
    }
    removal["plan_digest"] = plan_digest(removal)
    return removal, after


def apply(kind: str, approved_digest: str, *args, paths: Paths | None = None, reality: Reality | None = None,
          runner=subprocess.run, settled=site_setup._settled_local_state) -> dict:
    paths, reality = paths or Paths(), reality or Reality()
    with site_lock(paths.state, owner=paths.owner):
        planner = plan_add if kind == "add" else plan_remove
        change, after = planner(*args, paths=paths, reality=reality)
        if change["plan_digest"] != approved_digest:
            raise SiteConflict("Host or site changed since the reviewed plan")
        if change["status"] == "already_serves":
            return _already(change)
        stamp, backup = backup_folder(paths)
        if change["changes"]:
            (backup / "config.toml").write_bytes(paths.config.read_bytes())
        for step in change["groups"]:
            for account in step.get("add", []):
                runner(["/usr/bin/gpasswd", "-a", account, step["group"]], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            for account in step.get("remove", []):
                runner(["/usr/bin/gpasswd", "-d", account, step["group"]], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if change["changes"]:
            write_file(paths.config, after)  # keeps its mode and group
        if change["restart"]:
            runner(["/usr/bin/systemctl", "restart", *change["restart"]], check=True)
        check_after = paths.check(reality).run()
        append_record(json.dumps({"at": stamp, "kind": change["kind"], "pirate": change["pirate"],
                                  "ponds": change["ponds"], "plan_digest": approved_digest,
                                  "backup": str(backup), "site_check_after": check_after["status"]}),
                      paths.var, owner=paths.owner)
    health = settled(None)
    good = health == "healthy" and check_after["status"] == "regular"
    done = {"pirate_admission": "admitted", "pirate_extension": "extended", "pirate_removal": "removed"}
    outcome = {"status": done[change["kind"]] if good else "changed_but_degraded",
               "pirate": change["pirate"], "backup": str(backup), "local_health": health,
               "site_check": check_after["status"]}
    if change["kind"] == "pirate_extension":
        outcome.update(ponds=change["ponds"], restarted=change["restart"], for_site_json=change["for_site_json"],
                       pirate_side=change["pirate_side"])
    elif kind == "add":
        outcome["for_site_json"] = change["for_site_json"]
    else:
        outcome["kept"] = change["kept"]
    return outcome


def _already(change: dict) -> dict:
    """The answer when the Pirate serves every Pond asked for: nothing changed."""
    return {"status": "already_serves", "pirate": change["pirate"], "ponds": change["served"],
            "for_site_json": change["for_site_json"], "pirate_side": change["pirate_side"]}


def review(change: dict) -> str:
    if change["kind"] == "pirate_extension":
        return _review_extension(change)
    verb = "Admit" if change["kind"] == "pirate_admission" else "Take out"
    lines = [f"{verb} the Pirate {change['pirate']} (account {change['account']}, uid {change['uid']})"]
    if change.get("ponds"):
        lines.append(("Serving the Ponds: " if change["kind"] == "pirate_admission"
                      else "Ponds that accept guests (it may have registered there): ")
                     + ", ".join(change["ponds"]))
    for item in change["changes"]:
        lines.append(f"{item['action']:8} {item['path']}")
        lines.extend("         " + line for line in item["diff"])
    for step in change["groups"]:
        for verb_, key in (("add to", "add"), ("remove from", "remove")):
            if step.get(key):
                lines.append(f"{verb_} group {step['group']}: {', '.join(step[key])}")
    lines.append("Restarts: " + ", ".join(change["restart"]))
    if change.get("kept"):
        lines.append("Kept: " + change["kept"])
    return "\n".join(lines)


def _review_extension(change: dict) -> str:
    who = f"The Pirate {change['pirate']} (account {change['account']}, uid {change['uid']})"
    if change["status"] == "already_serves":
        lines = [f"{who} already serves: {', '.join(change['served'])}. Nothing to change on the site."]
    else:
        lines = [f"{who}: offer it the Ponds: {', '.join(change['ponds']) or 'none (groups only)'}"]
        if change["served"]:
            lines.append("Already served: " + ", ".join(change["served"]))
        for step in change["groups"]:
            lines.append(f"add to group {step['group']}: {', '.join(step['add'])}")
        lines.append("Restarts: " + (", ".join(change["restart"]) or "none") + " (config.toml stays as it is)")
    lines.append(change["pirate_side"])
    return "\n".join(lines)


def main(kind: str, argv=None) -> int:
    parser = argparse.ArgumentParser(prog=f"boring-crocodile-admin pirate-{kind}", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name", help="the Pirate's name in this site (its caller name), e.g. parrot")
    if kind == "add":
        parser.add_argument("--account", required=True, help="the Pirate's Unix account")
        parser.add_argument("--ponds", required=True, help="'all' or comma-separated Ponds it serves")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approve-digest")
    args = parser.parse_args(argv)
    planned = (args.name, args.account, args.ponds.split(",")) if kind == "add" else (args.name,)
    try:
        if os.geteuid() != 0:
            raise SiteConflict("Changing the site requires root (site-check reads the credentials)")
        change, _ = (plan_add if kind == "add" else plan_remove)(*planned)
        if change["status"] == "already_serves":  # with or without --apply: nothing to change
            print(review(change), file=sys.stderr)
            print(json.dumps(_already(change), indent=2))
            return 0
        if not args.apply:
            print(review(change), file=sys.stderr)
            print(json.dumps(change, indent=2))
            return 0
        digest = args.approve_digest
        if not digest:
            if not sys.stdin.isatty():
                raise SiteConflict("Unattended apply requires --approve-digest from the plan")
            print(review(change), file=sys.stderr)
            print("Type APPLY to change the running site: ", end="", file=sys.stderr, flush=True)
            if sys.stdin.readline().strip() != "APPLY":
                raise SiteConflict("Not confirmed")
            digest = change["plan_digest"]
        outcome = apply(kind, digest, *planned)
        print(json.dumps(outcome, indent=2))
        return 0 if outcome["status"] in ("admitted", "extended", "removed", "already_serves") else 1
    except (SiteConflict, ValueError, KeyError, OSError, EOFError, subprocess.SubprocessError) as error:
        print(f"pirate-{kind} failed: {error}", file=sys.stderr)
        return 2
