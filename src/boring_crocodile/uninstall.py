"""Park through render; retire access/data and reserve identities with --delete-data."""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from typing import Any

from boring_deploy import Failure
from boring_deploy.install import seal_check, seal_folders
from . import oauth_records, site_render
from .approval import plan_digest
from .site_host import Host, SiteConflict as UninstallConflict
from .site_layout import STATE_DIR, OAUTH_DATABASE, POND_NAME, approval_credential, site_lock
from .site_layout import REGISTRATION_ROOT
from .site_model import SiteDescription

HOME_NOTE = "Homes are kept. Retirement seals their top folders to root 0700 and locks/expires accounts; UID/GID stay reserved."


def _safe_file(path, owner=0):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner
            or info.st_mode & 0o022 or info.st_nlink != 1):
        raise UninstallConflict("Unsafe owned file: " + str(path))


def _delete_path(path):
    if not os.path.lexists(path):
        return
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
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
    """Give the home's top folder to root, mode 0700, before retiring access: boring-deploy's seal_folders, the one implementation of this step,
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



def _retirement_group(identity, host, kind):
    group = host.group(identity.group)
    if group is None and any(gid == identity.gid and name != identity.group
                             for gid, name in host.identity_ids()[1]):
        raise UninstallConflict(kind + " numeric GID was reused: " + str(identity.gid))
    if group is None:
        raise UninstallConflict(kind + " group is missing; restore its reserved binding: " + identity.group)
    if group is not None:
        if group["gid"] != identity.gid:
            raise UninstallConflict(kind + " group changed: " + identity.group)
        if host.group_members(identity.group) - {identity.name}:
            raise UninstallConflict(kind + " group contains another identity: " + identity.group)
    return group


def _prepare(pond, whole, delete_data, paths, host):
    if (pond is None) == (not whole):
        raise UninstallConflict("Select exactly one of --pond or --site")
    current = SiteDescription.read(paths.config, owner=paths.owner)
    if pond is not None and not re.fullmatch(POND_NAME, pond):
        raise UninstallConflict("Invalid Pond name")
    if pond == "pond":
        raise UninstallConflict("The initial Pond owns the site; use --site")
    chosen = list(current.ponds) if whole else [pond] if pond in current.ponds else []
    main = deepcopy(current.document)
    if whole:
        main["site"]["state"] = "removed"
    documents = {n: deepcopy(p.document) for n,p in current.ponds.items()}
    for name in chosen:
        documents[name]["enabled"] = False
    from .site_pirates import pond_admission
    pond_admission(main, chosen, "retire" if delete_data else "park")
    candidate = SiteDescription.parse(main, documents)
    rendered, prepared = (site_render.plan(paths=paths, host=host, candidate=candidate)
                          if chosen or whole else (None, None))
    data = []
    database = host.path(current.settings.oauth_database_file)
    if delete_data and (chosen or whole):
        retiring_groups = {value for name in chosen for value in (
            current.ponds[name].identity.group, str(current.ponds[name].identity.gid))}
        if whole:
            retiring_groups.update((current.controller.group, str(current.controller.gid)))
        for adjustment in rendered["adjustments"]:
            if adjustment.get("directive") == "SupplementaryGroups" and set(
                    token.strip("\'\"") for token in adjustment["value"].split()) & retiring_groups:
                raise UninstallConflict("An external drop-in names a retiring group; update it explicitly")
        if current.settings.oauth_database_file != OAUTH_DATABASE:
            raise UninstallConflict("Standard retirement requires the site's standard OAuth store")
        if os.path.lexists(database):
            _safe_file(database, host.database_owner(current))
        for name in chosen:
            p = current.ponds[name]
            expected_secret = STATE_DIR / "credentials" / approval_credential(name)
            if p.credential_file != expected_secret:
                raise UninstallConflict("Review external credential ownership before retirement")
            user = host.account(p.identity.name)
            group = _retirement_group(p.identity, host, "Worker")
            if user != p.identity.as_user():
                raise UninstallConflict("Worker identity changed: " + p.identity.name)
            home = host.path(p.identity.home)
            state = host.retirement_home_state(home, p.identity.uid)
            credential = host.path(p.credential_file)
            if os.path.lexists(credential):
                _safe_file(credential, host.owner)
            runtime = [host.path(path) for path in (
                f"/run/boring-crocodile/workers/{name}", f"/run/boring-crocodile/tasks/{name}.sock",
                f"/run/boring-crocodile-watch/{name}.sock", f"/run/boring-crocodile-watch/{name}.lock",
                str(REGISTRATION_ROOT / name))]
            for path in runtime:
                if os.path.lexists(path) and (path.is_symlink() or (path.is_dir() and _mounted_descendant(path))):
                    raise UninstallConflict("Unsafe runtime path: " + str(path))
            keys = _oauth_keys(database, p.pond.profile_id, host.database_owner(current), caller=name) if database.exists() else []
            data.append(dict(pond=name, user=user, identity=p.document["identity"], group=group,
                             home=str(home), home_state=state, credential=str(credential),
                             runtime=list(map(str, runtime)), profile_id=p.pond.profile_id, records=len(keys),
                             records_digest=hashlib.sha256(json.dumps(keys).encode()).hexdigest()))
        if whole:
            actual = host.account(current.controller.name)
            if actual != current.controller.as_user():
                raise UninstallConflict("Controller identity changed")
            group = _retirement_group(current.controller, host, "Controller")
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(database) + suffix)
            if whole and os.path.lexists(path):
                _safe_file(path, host.database_owner(current))
    proposal = dict(kind="retirement" if delete_data else "park", scope="site" if whole else "pond",
                    pond=pond, selected=chosen, delete_data=delete_data, render=rendered, data=data,
                    homes_kept=[row["home"] for row in data], note=HOME_NOTE,
                    metadata="site description retained for explicit archive after cleanup" if whole else "selected declaration removed last on retirement")
    proposal["plan_digest"] = plan_digest(proposal)
    return proposal, (current, rendered, prepared)


def plan(*, pond: str | None = None, site: bool = False, delete_data: bool = False,
         paths: site_render.Paths | None = None, host: Host | None = None) -> tuple[dict[str, Any], Any]:
    return _prepare(pond, site, delete_data, paths or site_render.Paths(), host or Host())


def apply(*, approved_digest: str, pond: str | None = None, site: bool = False,
          delete_data: bool = False, paths: site_render.Paths | None = None,
          host: Host | None = None) -> dict[str, Any]:
    paths, host = paths or site_render.Paths(), host or Host()
    host.require_authority()
    with site_lock(paths.state, owner=paths.owner):
        proposal, (description, rendered, prepared) = _prepare(pond, site, delete_data, paths, host)
        if proposal["plan_digest"] != approved_digest:
            raise UninstallConflict("Host or retirement inputs changed since the reviewed plan")
        if rendered is not None:
            site_render._apply_locked(rendered, prepared, paths, host)
        if delete_data:
            try:
                database = host.path(description.settings.oauth_database_file)
                for row in proposal["data"]:
                    identity = description.ponds[row["pond"]].identity
                    if host.account(identity.name) != row["user"]:
                        raise UninstallConflict("Worker identity changed before retirement")
                    declaration = paths.state / "ponds" / row["pond"] / "config.toml"
                    host.apply_description_permissions([
                        dict(path=str(declaration.parent), directory=True, uid=host.owner, gid=0, mode=0o700),
                        dict(path=str(declaration), directory=False, uid=host.owner, gid=0, mode=0o600)])
                    home = Path(row["home"])
                    if host.retirement_home_state(home, identity.uid) != row["home_state"]:
                        raise UninstallConflict("Home changed since the retirement plan")
                    if row["home_state"] == "worker":
                        host.seal_home(home, identity.uid)
                    if database.exists():
                        _revoke(database, row["profile_id"], host.database_owner(description), caller=row["pond"])
                    credential = Path(row["credential"])
                    if os.path.lexists(credential):
                        _safe_file(credential, host.owner)
                        credential.unlink()
                    for runtime in row["runtime"]:
                        _delete_path(Path(runtime))
                    if row["user"] is not None:
                        if host.account(identity.name) != row["user"]:
                            raise UninstallConflict("Worker identity changed immediately before reservation")
                        _retirement_group(identity, host, "Worker")
                        host.reserve_user(identity.name)
                    _retirement_group(identity, host, "Worker")
                if site:
                    for suffix in ("", "-wal", "-shm"):
                        path = Path(str(database) + suffix)
                        if os.path.lexists(path):
                            _safe_file(path, host.database_owner(description))
                            path.unlink()
                    user = host.account(description.controller.name)
                    if user is not None:
                        if user != description.controller.as_user():
                            raise UninstallConflict("Controller identity changed")
                        _retirement_group(description.controller, host, "Controller")
                        host.reserve_user(description.controller.name)
                    _retirement_group(description.controller, host, "Controller")
                else:
                    # Security/data work is complete before removing its binding.
                    for name in proposal["selected"]:
                        declaration = paths.state / "ponds" / name / "config.toml"
                        _safe_file(declaration, host.owner)
                        declaration.unlink()
                        if not any(declaration.parent.iterdir()):
                            declaration.parent.rmdir()
            except (ValueError, OSError, subprocess.SubprocessError) as error:
                raise UninstallConflict("Retirement did not finish; declarations remain until cleanup completes. "
                                        "Rerun the same uninstall --delete-data command. " + str(error)) from error
    return dict(status="retired" if delete_data else "parked", scope=proposal["scope"], pond=pond,
                homes_kept=proposal["homes_kept"], note=HOME_NOTE, metadata=proposal["metadata"])
