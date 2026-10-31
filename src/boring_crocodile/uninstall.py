"""Reviewed removal of a Pond or the whole site: boring-crocodile-admin uninstall.

The site is read as it is: site-check first (stops on anything irregular);
the Ponds, their accounts and homes come from config.toml and the host.
Removing a Pond rewrites config.toml without its sections and renders the
controller unit for the remaining Ponds; its units, their drop-ins, tmpfiles
file and runtime folders go. --delete-data also removes its credential, OAuth
grants, its --add-pond folder (last) and, after showing them, its account and
group (the site: all of them, the controller's, the state folder, the OAuth
store). Homes are never removed: they are listed as no longer live, for a
person to delete once nothing in them is needed; with --delete-data each
home's top folder is first given to root (0700), so an account created later
with the freed uid does not inherit it. --site without --delete-data marks
the activation receipt uninstalled: site activation installs the site again
from the kept data.

A Pond config.toml no longer names (parked, or left so by an apply that
stopped part-way) is read from its --add-pond folder, the one record left of
it: `--pond NAME --delete-data` then removes what is still there.
`--site --delete-data` first writes what it will remove to uninstall.json in
the state folder (removed last), so a re-run after an apply that stopped
part-way finishes from it. Stopping the site waits for the controller and
every worker to stop, not only for the target. Plan, digest, apply.
"""
from __future__ import annotations

import grp
import hashlib
import json
import os
import re
from pathlib import Path
import pwd
import shutil
from contextlib import closing
import stat
import subprocess
import sys
import time
from typing import Protocol
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from boring_deploy import Failure
from boring_deploy.install import seal_check, seal_folders

from . import oauth_records
from .pirates import REGISTRATION_ROOT
from .settings import Settings
from .setup_services import render_site
from .site_check import Check, Reality, summary
from .setup_apply import read_receipt, write_receipt
from .site_setup import SITE_RECEIPT, UNINSTALLED
from .site_layout import (CREDENTIALS_DIR, INITIAL_POND, STATE_DIR, approval_credential, budget_slice,
                          site_lock, tmpfiles_name, write_file)


class UninstallConflict(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _safe_file(path, owner=0):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner
            or info.st_mode & 0o022 or info.st_nlink != 1):
        raise UninstallConflict("Unsafe root-owned file: " + str(path))


def _remove_sections(text, name, allow_absent=False):
    targets = {f"[ponds.{name}]", f"[environments.{name}]"}
    found, output, dropping = set(), [], False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            # The Pond's own sub-tables go with it: its file roots
            # ([environments.NAME.files.roots.home], written by setup since
            # 0.4.11) and its projects ([ponds.NAME.projects.X]).
            sub = stripped.startswith((f"[ponds.{name}.", f"[environments.{name}."))
            dropping = stripped in targets or sub
            if stripped in targets:
                found.add(stripped)
            if dropping:
                continue
        if not dropping:
            output.append(line)
    if found != targets:
        if not found and allow_absent:
            parsed = tomllib.loads("".join(output))
            if (name not in parsed.get("ponds", {})
                    and name not in parsed.get("environments", {})):
                return "".join(output)
        raise UninstallConflict("Configuration does not contain the selected Pond")
    result = "".join(output)
    parsed = tomllib.loads(result)
    if name in parsed.get("ponds", {}) or name in parsed.get("environments", {}):
        raise UninstallConflict("Filtered configuration still contains selected Pond")
    return result


def _at(root, absolute):
    return root / absolute.lstrip("/")


def _remove(path):
    return {"kind": "remove", "path": str(path)}


def _site_target_present(root):
    return any(_at(root, path).exists() for path in (
        "/etc/systemd/system/boring-crocodile-mcp.target",
        "/lib/systemd/system/boring-crocodile-mcp.target",
        "/usr/lib/systemd/system/boring-crocodile-mcp.target",
    ))


HOME_NOTE = ("Homes are never removed by uninstall. These are no longer live in the site; "
             "delete them by hand once you are sure nothing in them is needed. With --delete-data "
             "each home's top folder belongs to root (mode 0700) before its account goes, so a later "
             "account with the same uid does not inherit it.")
TARGET_UNIT = "boring-crocodile-mcp.target"
# Stopped together with the target, so `systemctl stop` returns only once they
# are down: the target alone stops first (its units are ordered before it) and
# the controller can take its stop timeout to follow.
# Patterns: only loaded units match, and a pattern matching none is no error.
SITE_UNITS = ("boring-crocodile-mcp*.service", "boring-crocodile-worker-*.service", "boring-crocodile-task-*")
REMOVAL_RECORD = "uninstall.json"


def _view(root: Path, reality: Reality):
    """The site as it is: site-check must find it regular."""
    state = _at(root, str(STATE_DIR))
    config = state / "config.toml"
    if not config.exists():
        raise UninstallConflict("No site here: " + str(config) + " is missing")
    units, tmpfiles = _at(root, "/etc/systemd/system"), _at(root, "/etc/tmpfiles.d")
    report = Check(config, units, tmpfiles, state / CREDENTIALS_DIR.name, reality).run()
    if report["status"] != "regular":
        raise UninstallConflict("site-check found what uninstall cannot handle; resolve it first:\n"
                                + summary(report))
    settings = Settings.load(config)
    raw = tomllib.loads(config.read_text())
    entries = Check(config, units, tmpfiles, state / CREDENTIALS_DIR.name, reality).entries(raw, settings)
    return raw, {entry[2]["pond_name"]: entry for entry in entries}


def _pond_files(root: Path, name: str) -> list[Path]:
    """A Pond's units with their drop-ins (a later Pond of the same name must not
    inherit them), its tmpfiles file, runtime folders and registration folder."""
    units = _at(root, "/etc/systemd/system")
    files = []
    for unit in (f"boring-crocodile-worker-{name}.service", f"boring-crocodile-task-{name}.socket",
                 f"boring-crocodile-task-{name}@.service", budget_slice(name)):
        files += [units / unit, units / (unit + ".d")]
    return files + [_at(root, "/etc/tmpfiles.d/" + tmpfiles_name(name)),
                    _at(root, f"/run/boring-crocodile/workers/{name}"),
                    _at(root, f"/run/boring-crocodile/tasks/{name}.sock"),
                    _at(root, f"/run/boring-crocodile-watch/{name}.sock"),
                    _at(root, f"/run/boring-crocodile-watch/{name}.lock"),
                    _at(root, f"{REGISTRATION_ROOT}/{name}")]


def _user(reality: Reality, name: str) -> dict | None:
    try:
        return reality.user(name)
    except KeyError:
        return None


def _account_actions(root: Path, reality: Reality, worker: str, home: str) -> list[dict]:
    """The worker's home goes to root first, then its account and group."""
    actions: list[dict] = []
    account = _user(reality, worker)
    if account is not None:
        if os.path.lexists(_at(root, home)) and not _handed_to_root(_at(root, home)):
            actions.append({"kind": "secure_home", "path": str(_at(root, home)), "expected_uid": account["uid"]})
        actions.append({"kind": "delete_user", "name": worker, "expected": account})
    group = reality.group(worker)
    if group is not None:
        actions.append({"kind": "delete_group", "name": worker, "expected": group})
    return actions


def _handed_to_root(path: Path) -> bool:
    """A home that is root's already: handed over by an earlier run of this step
    (root, mode 0700, a folder). A home root owns otherwise is not the worker's
    home as setup made it, perhaps a shared folder: refused, never chmodded."""
    info = path.lstat()
    if info.st_uid != 0:
        return False
    if stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700:
        return True
    raise UninstallConflict("A home belongs to root but was not handed over by uninstall; "
                            "inspect it: " + str(path))


def _pond_data_actions(root: Path, reality: Reality, raw: dict, control: str, pond: str,
                       worker: str, home: str, profile_id: str) -> list[dict]:
    """--delete-data for one Pond, each step only while there is something to
    remove, so a re-run after an apply that stopped part-way finishes it. The
    --add-pond folder goes last: it is what a re-run reads the Pond from."""
    state = _at(root, str(STATE_DIR))
    actions = []
    credential = state / CREDENTIALS_DIR.name / approval_credential(pond)
    if os.path.lexists(credential):
        actions.append(_remove(credential))
    # Revoking finds nothing on a re-run; a missing OAuth store stops the apply (preflight).
    actions.append({"kind": "revoke_profile", "path": str(_at(root, raw["auth"]["oauth_database_file"])),
                    "owner": reality.user(control)["uid"], "profile_id": profile_id, "caller": pond})
    group = reality.group(worker)
    if group is not None and group["gid"] in reality.groups(control):
        actions.append({"kind": "remove_membership", "user": control, "group": worker})
    actions += _account_actions(root, reality, worker, home)
    folder = state / "ponds" / pond
    if os.path.lexists(folder):
        actions.append(_remove(folder))
    return actions


def _proposal(site: bool, pond: str | None, delete_data: bool, actions: list[dict],
              homes: list[dict]) -> dict[str, object]:
    proposal = {"schema_version": 3, "scope": "site" if site else "pond", "pond": pond,
                "delete_data": delete_data, "actions": actions, "homes_kept": homes, "note": HOME_NOTE}
    proposal["plan_digest"] = _digest(proposal)
    return proposal


def _home(pond: str, home: str, worker: str, delete_data: bool) -> dict:
    return {"pond": pond, "path": home, "owner": "root" if delete_data else worker,
            "owner_removed": delete_data}


def _taken_out(root: Path, reality: Reality, raw: dict, control: str, pond: str,
               delete_data: bool) -> dict[str, object]:
    """A Pond config.toml no longer names: what is left of it, read from its
    --add-pond folder (parked, or an earlier apply stopped after config.toml)."""
    receipt_path = _at(root, str(STATE_DIR)) / "ponds" / pond / "initial-setup.json"
    if not os.path.lexists(receipt_path):
        raise UninstallConflict("Unknown Pond: " + pond)
    try:  # root's on a host; the test root's owner in tests
        receipt = read_receipt(receipt_path, root.lstat().st_uid)
    except ValueError as error:
        raise UninstallConflict(f"{pond}: {error}") from None
    inputs = receipt.get("inputs") or {}
    if receipt.get("pond_name") != pond or inputs.get("control_user") != control:
        raise UninstallConflict(f"{pond}: its --add-pond folder names another Pond or controller")
    worker, home = inputs["execution_user"], inputs["home"]
    leftovers = [path for path in _pond_files(root, pond) if os.path.lexists(path)]
    if not leftovers and not delete_data:
        raise UninstallConflict(f"{pond} is out of the site already (parked); --delete-data removes its data")
    actions = []
    if leftovers:
        actions += [{"kind": "stop_site", "unit": TARGET_UNIT}, *map(_remove, leftovers), {"kind": "daemon_reload"}]
    if delete_data:
        actions += _pond_data_actions(root, reality, raw, control, pond, worker, home, receipt["profile_id"])
    actions.append({"kind": "start_site", "unit": TARGET_UNIT})
    return _proposal(False, pond, delete_data, actions, [_home(pond, home, worker, delete_data)])


def _uninstalled(root: Path) -> bool:
    """The activation receipt says an `uninstall --site` (data kept) has begun."""
    path = _at(root, str(STATE_DIR)) / SITE_RECEIPT.name
    if not os.path.lexists(path):
        return False
    try:  # root's on a host; the test root's owner in tests
        return read_receipt(path, root.lstat().st_uid).get("status") == UNINSTALLED
    except ValueError as error:
        raise UninstallConflict(f"{path}: {error}") from None


def _finish_site(root: Path) -> dict[str, object]:
    """A re-run of `uninstall --site` after one that stopped part-way: the receipt
    is marked already, site-check cannot be regular any more; remove what is
    left of the site's files (the names site-check knows as the Crocodile's),
    config.toml last."""
    state, units = _at(root, str(STATE_DIR)), _at(root, "/etc/systemd/system")
    config = state / "config.toml"
    names: list[str] = []
    if config.exists():
        try:
            names = sorted(tomllib.loads(config.read_text()).get("ponds", {}))
        except ValueError:
            names = []
    paths = [path for name in names for path in _pond_files(root, name)]
    paths += sorted(units.glob("boring-crocodile-*")) + sorted(units.glob("system-crocodile*.slice"))
    paths += sorted(_at(root, "/etc/tmpfiles.d").glob("boring-crocodile*.conf")) + [config]
    left = [path for path in dict.fromkeys(paths) if os.path.lexists(path)]
    if not left:
        raise UninstallConflict("The site is uninstalled already; nothing of it is left to remove")
    actions = ([{"kind": "stop_site", "unit": TARGET_UNIT}, {"kind": "disable_site", "unit": TARGET_UNIT}]
               if _site_target_present(root) else [])
    actions += [*map(_remove, left), {"kind": "daemon_reload"}]
    return _proposal(True, None, False, actions, [])


def _finish_removal(root: Path, reality: Reality) -> dict[str, object]:
    """A re-run of `uninstall --site --delete-data` after one that stopped
    part-way: config.toml may be gone, so the site is read from the record the
    first run wrote (REMOVAL_RECORD). Everything still there goes, the state
    folder (with the record) last."""
    state = _at(root, str(STATE_DIR))
    try:  # root's on a host; the test root's owner in tests
        record = read_receipt(state / REMOVAL_RECORD, root.lstat().st_uid)
    except ValueError as error:
        raise UninstallConflict(f"{state / REMOVAL_RECORD}: {error}") from None
    units = _at(root, "/etc/systemd/system")
    paths = [path for pond in record["ponds"] for path in _pond_files(root, pond["name"])]
    paths += sorted(units.glob("boring-crocodile-*")) + sorted(units.glob("system-crocodile*.slice"))
    paths += sorted(_at(root, "/etc/tmpfiles.d").glob("boring-crocodile*.conf"))
    paths += [state / "config.toml", state / CREDENTIALS_DIR.name, _at(root, record["oauth"])]
    actions = ([{"kind": "stop_site", "unit": TARGET_UNIT}, {"kind": "disable_site", "unit": TARGET_UNIT}]
               if _site_target_present(root) else [])
    actions += [_remove(path) for path in dict.fromkeys(paths) if os.path.lexists(path)]
    actions.append({"kind": "daemon_reload"})
    for pond in record["ponds"]:
        actions += _membership_actions(reality, record["control"], pond["worker"])
        actions += _account_actions(root, reality, pond["worker"], pond["home"])
    actions += _control_actions(reality, record["control"])
    actions.append(_remove(state))
    homes = [_home(pond["name"], pond["home"], pond["worker"], True) for pond in record["ponds"]]
    return _proposal(True, None, True, actions, homes)


def _membership_actions(reality: Reality, control: str, worker: str) -> list[dict]:
    """The controller leaves the worker's group first, so userdel can take the
    group with the account instead of warning that it still has members."""
    group = reality.group(worker)
    try:
        member = group is not None and group["gid"] in reality.groups(control)
    except KeyError:
        member = False
    return [{"kind": "remove_membership", "user": control, "group": worker}] if member else []


def _control_actions(reality: Reality, control: str) -> list[dict]:
    """The controller's account and group, while they exist."""
    actions = []
    account = _user(reality, control)
    if account is not None:
        actions.append({"kind": "delete_user", "name": control, "expected": account})
    group = reality.group(control)
    if group is not None:
        actions.append({"kind": "delete_group", "name": control, "expected": group})
    return actions


def _kept(raw: dict, ponds: dict) -> dict[str, object]:
    """What activation needs to bring this site back as it is: the Ponds live now
    (a parked one stays parked), their ceilings and the Pirate callers."""
    keys = ("memory_bytes", "swap_bytes", "tasks_max", "task_tasks_max")
    return {"live_ponds": sorted(ponds),
            "ceilings": {name: {key: getattr(entry[1], key) for key in keys} for name, entry in ponds.items()},
            "pirate_callers": dict(raw.get("pirate_services", {}).get("callers", {}))}


def plan(*, pond: str | None = None, site: bool = False, delete_data: bool = False,
         root: Path = Path("/"), reality: Reality | None = None) -> tuple[dict[str, object], dict[str, str]]:
    if (pond is None) == (not site):
        raise UninstallConflict("Select exactly one of --pond or --site")
    root, reality = Path(root), reality or Reality()
    if site and not delete_data and _uninstalled(root):
        return _finish_site(root), {}
    if site and delete_data and os.path.lexists(_at(root, str(STATE_DIR)) / REMOVAL_RECORD):
        return _finish_removal(root, reality), {}
    raw, ponds = _view(root, reality)
    control = ponds[INITIAL_POND][0].control_user
    target_present = _site_target_present(root)
    if pond is not None:
        if pond == INITIAL_POND:
            raise UninstallConflict("The initial Pond owns the site; use --site")
        if not target_present:
            raise UninstallConflict("Cannot preserve neighboring Ponds without the site target")
        if pond not in ponds:
            return _taken_out(root, reality, raw, control, pond, delete_data), {}
        selected = [pond]
    else:
        selected = list(ponds)
    state = _at(root, str(STATE_DIR))
    config = state / "config.toml"
    units = _at(root, "/etc/systemd/system")
    controller = units / "boring-crocodile-mcp.service"
    actions = ([{"kind": "stop_site", "unit": TARGET_UNIT}] if target_present else [])
    if site and target_present:
        actions.append({"kind": "disable_site", "unit": TARGET_UNIT})
    if site and delete_data:
        # Before anything goes: what a re-run needs once config.toml is gone.
        record = {"control": control, "oauth": raw["auth"]["oauth_database_file"],
                  "ponds": [{"name": name, "worker": entry[0].execution_user, "home": entry[0].home}
                            for name, entry in ponds.items()]}
        actions.append({"kind": "record_removal", "path": str(state / REMOVAL_RECORD), "record": record})
    activation = state / SITE_RECEIPT.name
    if pond is None and not delete_data and os.path.lexists(activation):
        # The way back, marked first: activation installs the site again from the
        # kept data; a re-run after a stop below finishes the removal (_finish_site).
        actions.append({"kind": "mark_uninstalled", "path": str(activation), "kept": _kept(raw, ponds)})
    replacements: dict[str, str] = {}
    if pond is not None:
        remaining = [entry for name, entry in ponds.items() if name != pond]
        changed = {config: _remove_sections(config.read_text(), pond),
                   controller: render_site(remaining)["boring-crocodile-mcp.service"]}
        for path, content in changed.items():
            if content != path.read_text():
                actions.append({"kind": "replace", "path": str(path),
                                "before": hashlib.sha256(path.read_bytes()).hexdigest(),
                                "after": hashlib.sha256(content.encode()).hexdigest()})
                replacements[str(path)] = content
    for name in selected:
        actions += [_remove(path) for path in _pond_files(root, name) if os.path.lexists(path)]
    if pond is None:  # config.toml last: until it goes, a re-run can read the site
        target = units / TARGET_UNIT
        actions += [_remove(path) for path in (controller, controller.with_name(controller.name + ".d"),
                                               target, target.with_name(target.name + ".wants"), config)
                    if os.path.lexists(path)]
    actions.append({"kind": "daemon_reload"})
    if delete_data:
        if pond is not None:
            setup = ponds[pond][0]
            actions += _pond_data_actions(root, reality, raw, control, pond, setup.execution_user,
                                          setup.home, raw["ponds"][pond]["profile_id"])
        else:
            oauth = _at(root, raw["auth"]["oauth_database_file"])
            actions += [_remove(path) for path in (state / CREDENTIALS_DIR.name, oauth) if path.exists()]
            for name in selected:
                actions += _membership_actions(reality, control, ponds[name][0].execution_user)
                actions += _account_actions(root, reality, ponds[name][0].execution_user, ponds[name][0].home)
            # The state folder last: it holds the record a re-run finishes from.
            actions += [*_control_actions(reality, control), _remove(state)]
    if pond is not None:
        # Last: a site that fails to start has had its data steps done already.
        actions.append({"kind": "start_site", "unit": TARGET_UNIT})
    homes = [_home(name, ponds[name][0].home, ponds[name][0].execution_user, delete_data) for name in selected]
    return _proposal(site, pond, delete_data, actions, homes), replacements


def _actual_user(name):
    try:
        value = pwd.getpwnam(name)
    except KeyError:
        return None
    return {"name": value.pw_name, "uid": value.pw_uid, "gid": value.pw_gid,
            "home": value.pw_dir, "shell": value.pw_shell}


def _actual_group(name):
    try:
        value = grp.getgrnam(name)
    except KeyError:
        return None
    return {"name": value.gr_name, "gid": value.gr_gid}


def _delete_path(path):
    if not os.path.lexists(path):
        return
    if path.is_symlink():
        raise UninstallConflict("Refusing symlink removal: " + str(path))
    if path.is_dir():
        mount = _mounted_descendant(path)
        if mount:
            raise UninstallConflict("Refusing mounted subtree: " + mount)
        shutil.rmtree(path)
    else:
        path.unlink()


def _check_home(path, expected_uid):
    """A home to give to root: boring-deploy's seal checks (a real folder, no
    symlink, no mount point, still the account's), without a change. The plan
    made this step for a home the account owned, so a home that is gone or
    root's by now changed under us: refused, not skipped (seal_check would
    skip both). A home root had at planning time is no step (_handed_to_root)."""
    try:
        if not seal_check([path], expected_uid):
            raise UninstallConflict("A home changed since the plan (gone, or root's now); "
                                    "inspect it, then plan again: " + str(path))
    except Failure as error:
        raise UninstallConflict(str(error)) from None


def _secure_home(path, expected_uid):
    """Give the home's top folder to root, mode 0700, before userdel frees the
    uid: boring-deploy's seal_folders, the one implementation of this step,
    after the same checks as the preflight."""
    _check_home(path, expected_uid)
    try:
        seal_folders([path], expected_uid, lambda line: print(line, file=sys.stderr))
    except Failure as error:
        raise UninstallConflict(str(error)) from None


def _oauth_keys(path, profile_id, owner, *, caller=None):
    _safe_file(path, owner)
    try:
        with closing(oauth_records.open_readonly(path)) as db:
            chosen = oauth_records.selected(oauth_records.rows(db), profile_id, caller=caller)
            if caller is not None:
                oauth_records.caller_services(db, caller)
            return [(row[0], row[1]) for row in chosen]
    except ValueError as error:
        raise UninstallConflict(str(error)) from None


def _revoke(path, profile_id, owner, *, caller=None):
    _safe_file(path, owner)
    try:
        with oauth_records.Transaction(path) as db:
            chosen = oauth_records.selected(oauth_records.rows(db), profile_id, caller=caller)
            oauth_records.delete(db, chosen)
            if caller is not None:
                oauth_records.revoke_caller(db, caller)
    except ValueError as error:
        raise UninstallConflict(str(error)) from None


def _delete_user(name, runner, sleep=time.sleep):
    """Allow systemd's just-stopped cgroup a brief, bounded exit window."""
    for attempt in range(50):
        try:
            runner(["/usr/sbin/userdel", name], check=True)
            return
        except subprocess.CalledProcessError as error:
            if error.returncode != 8:
                raise
            if attempt == 49:
                raise UninstallConflict(
                    "Recorded user still has processes after site stop: " + name) from error
            sleep(0.1)


def _mount_points():
    """Every mount point, bind mounts included."""
    with open("/proc/self/mountinfo", encoding="utf-8") as entries:
        return [re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), line.split(" ", 5)[4])
                for line in entries]


def _mounted_descendant(path):
    """Do not let recursive removal cross a mount, including a bind mount."""
    target = str(path)
    return next((mounted for mounted in _mount_points()
                 if mounted == target or mounted.startswith(target + "/")), None)


def _preflight(proposal):
    """Resolve every destructive target before the first mutation."""
    for action in proposal["actions"]:
        kind = action["kind"]
        if kind == "remove":
            path = Path(action["path"])
            if os.path.lexists(path):
                if path.is_symlink():
                    raise UninstallConflict("Refusing symlink removal: " + str(path))
                if path.is_dir():
                    mount = _mounted_descendant(path)
                    if mount:
                        raise UninstallConflict("Refusing mounted subtree: " + mount)
        elif kind == "secure_home":
            _check_home(Path(action["path"]), action["expected_uid"])
        elif kind == "revoke_profile":
            _oauth_keys(Path(action["path"]), action["profile_id"], action["owner"], caller=action["caller"])
        elif kind == "delete_user":
            if _actual_user(action["name"]) != action["expected"]:
                raise UninstallConflict("User changed since the plan: " + action["name"])
        elif kind == "delete_group":
            actual = _actual_group(action["name"])
            if actual is not None and actual != action["expected"]:
                raise UninstallConflict("Group changed since the plan: " + action["name"])
        elif kind == "remove_membership":
            try:
                members = grp.getgrnam(action["group"]).gr_mem
            except KeyError as error:
                raise UninstallConflict("Group disappeared since the plan") from error
            if action["user"] not in members:
                raise UninstallConflict("Controller membership changed since the plan")


def _apply_locked(*, approved_digest, pond, site, delete_data, root, runner):
    current, replacements = plan(
        pond=pond, site=site, delete_data=delete_data, root=root)
    if current["plan_digest"] != approved_digest:
        raise UninstallConflict("Host changed since the approved uninstall plan")
    _preflight(current)
    for action in current["actions"]:
        kind = action["kind"]
        if kind == "disable_site":
            runner(["/usr/bin/systemctl", "disable", action["unit"]], check=True)
        elif kind == "stop_site":
            runner(["/usr/bin/systemctl", "stop", action["unit"], *SITE_UNITS], check=True)
        elif kind == "start_site":
            runner(["/usr/bin/systemctl", "start", action["unit"]], check=True)
        elif kind == "record_removal":
            write_receipt(Path(action["path"]), action["record"])
        elif kind == "daemon_reload":
            runner(["/usr/bin/systemctl", "daemon-reload"], check=True)
        elif kind == "replace":
            path = Path(action["path"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != action["before"]:
                raise UninstallConflict("Replacement input changed: " + str(path))
            write_file(path, replacements[str(path)])  # keeps its mode and group
        elif kind == "remove":
            _delete_path(Path(action["path"]))
        elif kind == "secure_home":
            _secure_home(Path(action["path"]), action["expected_uid"])
        elif kind == "mark_uninstalled":
            path = Path(action["path"])
            receipt = read_receipt(path, os.geteuid())
            write_receipt(path, receipt | {"status": UNINSTALLED, "kept": action["kept"]})
        elif kind == "revoke_profile":
            _revoke(Path(action["path"]), action["profile_id"], action["owner"], caller=action["caller"])
        elif kind == "remove_membership":
            runner(["/usr/bin/gpasswd", "--delete", action["user"], action["group"]], check=True)
        elif kind == "delete_user":
            if _actual_user(action["name"]) != action["expected"]:
                raise UninstallConflict("User changed since the plan: " + action["name"])
            _delete_user(action["name"], runner)
        elif kind == "delete_group":
            actual = _actual_group(action["name"])
            if actual is not None:
                if actual != action["expected"]:
                    raise UninstallConflict("Group changed since the plan: " + action["name"])
                runner(["/usr/sbin/groupdel", action["name"]], check=True)
    return {"status": "complete", "scope": current["scope"],
            "pond": pond, "delete_data": delete_data,
            "homes_kept": current["homes_kept"], "note": HOME_NOTE}


class UninstallRunner(Protocol):
    def __call__(self, command: list[str], /, *, check: bool) -> subprocess.CompletedProcess[bytes]: ...


def _run(command: list[str], /, *, check: bool) -> subprocess.CompletedProcess[bytes]:
    """Host tools talk to stderr: stdout carries only the JSON result
    (gpasswd, for one, announces what it did on stdout)."""
    return subprocess.run(command, check=check, stdout=sys.stderr)


def apply(*, approved_digest: str, pond: str | None = None, site: bool = False,
          delete_data: bool = False, root: Path = Path("/"),
          runner: UninstallRunner = _run) -> dict[str, object]:
    if os.geteuid() != 0 or Path(root) != Path("/"):
        raise UninstallConflict("Apply requires root on the target host")
    with site_lock(STATE_DIR):
        return _apply_locked(approved_digest=approved_digest, pond=pond,
                             site=site, delete_data=delete_data, root=Path(root),
                             runner=runner)
