"""Explicit create-once provisioning. Desired declarations replace progress receipts."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import secrets
import stat
import tomllib
from uuid import uuid4

from .approval import plan_digest
from .setup import context_payload
from .site_host import Host as Host, SiteConflict as SetupConflict, check_approval_secret
from .site_layout import CONFIG_FILE, INITIAL_POND, STATE_DIR, approval_credential, site_lock, write_file
from .site_model import Identity, SiteDescription, toml_document

SEED_SCRIPT = r"""
import base64, json, os, stat, sys, uuid
home, payload = json.load(sys.stdin)
def folder(fd, parts):
    current = os.dup(fd)
    try:
        for part in parts:
            try:
                os.mkdir(part, 0o700, dir_fd=current)
            except FileExistsError:
                pass
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise
root = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    for name in ("work", "lab", "handbook"):
        os.close(folder(root, [name]))
    for name, encoded in payload:
        parts = ["handbook", *name.split("/")]
        parent = folder(root, parts[:-1])
        try:
            try:
                info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            if info is not None:
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
                    raise ValueError("Unsafe existing context document: " + name)
                continue
            temporary = "." + parts[-1] + "." + uuid.uuid4().hex
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(fd, "wb") as stream:
                stream.write(base64.b64decode(encoded, validate=True))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            finally:
                os.unlink(temporary, dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(parent)
finally:
    os.close(root)
"""


def _read_decl(path, owner):
    from .site_model import _read
    return tomllib.loads(_read(path, owner).decode())


def _policy():
    ram = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    return dict(memory_bytes=max(512*1024**2, min(8*1024**3, ram//4)),
                swap_bytes=0, tasks_max=256, task_tasks_max=128)


def _prepare(inputs, name, host, config, *, context=None):
    if name != INITIAL_POND:
        pond_name(name)
    if {inputs.control_user, inputs.execution_user} & {"pond-pirates", "pirate-clients"}:
        raise SetupConflict("Dedicated identities cannot use the site's shared role-group names")
    state = config.parent
    host.protected(state)
    host.protected(host.path(Path(inputs.home).parent))
    if not host.path(Path(inputs.home).parent).is_dir():
        raise SetupConflict("Create the root-owned home parent first")
    known = SiteDescription.read(config, owner=host.owner) if os.path.lexists(config) else None
    declaration = state / "ponds" / name / "config.toml"
    existing = _read_decl(declaration, host.owner) if os.path.lexists(declaration) else None
    if known:
        if inputs.control_user != known.controller.name:
            raise SetupConflict("The site's controller binding differs")
        for other, pond in known.ponds.items():
            if other != name and (inputs.execution_user == pond.identity.name
                                  or Path(inputs.home) == Path(pond.identity.home)
                                  or Path(inputs.home) in Path(pond.identity.home).parents
                                  or Path(pond.identity.home) in Path(inputs.home).parents):
                raise SetupConflict("New Pond overlaps an existing worker or home")
    elif name != INITIAL_POND:
        raise SetupConflict("Provision the initial Pond first")
    elif os.path.lexists(state / "initial-setup.json") or any(
            host.path("/etc/systemd/system").glob("boring-crocodile-mcp.*")):
        raise SetupConflict("A legacy site needs the one-time adoption importer")
    if existing and (existing["identity"]["name"] != inputs.execution_user
                     or existing["identity"]["home"] != inputs.home):
        raise SetupConflict("Provisioning input differs from the declared worker")
    identities = {p.identity.name: p.identity for p in known.ponds.values()} if known else {}
    if known:
        identities[known.controller.name] = known.controller
    if existing:
        worker = Identity.parse(existing["identity"])
        identities[worker.name] = worker
    uid_rows, gid_rows = host.identity_ids()
    used_uids = {uid for uid, _ in uid_rows} | {i.uid for i in identities.values()}
    used_gids = {gid for gid, _ in gid_rows} | {i.gid for i in identities.values()}

    def available(kind, used, lookup):
        for value in host.system_id_range(kind):
            if value in used:
                continue
            try:
                lookup(value)
            except KeyError:
                used.add(value)
                return value
        raise SetupConflict("No free system " + kind + " for a dedicated identity")

    accounts, groups = {}, {}
    for role, user_name, home in (("controller", inputs.control_user, "/nonexistent"),
                                  ("worker", inputs.execution_user, inputs.home)):
        user, group = host.account(user_name), host.group(user_name)
        bound = identities.get(user_name)
        if bound is None:
            if user or group:
                raise SetupConflict("Undeclared account or primary group needs explicit adoption: " + user_name)
            bound = Identity(user_name, available("UID", used_uids, host.user_by_uid), user_name,
                             available("GID", used_gids, host.group_by_gid), home, "/usr/sbin/nologin")
            identities[user_name] = bound
        if bound.home != home or bound.shell != "/usr/sbin/nologin":
            raise SetupConflict("Requested identity differs from the declaration: " + user_name)
        if user and user != bound.as_user():
            raise SetupConflict("Declared identity changed: " + user_name)
        if group and group != {"name": bound.group, "gid": bound.gid}:
            raise SetupConflict("Declared primary group changed: " + user_name)
        for value, rows, lookup in ((bound.uid, uid_rows, host.user_by_uid),
                                    (bound.gid, gid_rows, host.group_by_gid)):
            if any(number == value and occupant != user_name for number, occupant in rows):
                raise SetupConflict("Declared numeric identity is occupied: " + user_name)
            try:
                occupant = lookup(value)
            except KeyError:
                pass
            else:
                if occupant["name"] != user_name:
                    raise SetupConflict("Declared numeric identity is occupied: " + user_name)
        if user:
            if not group:
                raise SetupConflict("Declared account has no matching primary group: " + user_name)
            allowed = {bound.gid}
            if known and role == "controller":
                allowed.update(p.identity.gid for p in known.ponds.values())
            if existing and role == "worker" and (clients := host.group("pirate-clients")):
                allowed.add(clients["gid"])
            if not set(host.memberships(user_name, bound.gid)) <= allowed:
                raise SetupConflict("Account has additional Unix authority; review it explicitly")
        accounts[role] = {"name": user_name, "home": home, "identity": asdict(bound), "current": user,
                          "action": "keep" if user else "create"}
        groups[user_name] = {"gid": bound.gid, "current": group, "action": "keep" if group else "create"}
    if any(len({getattr(i, field) for i in identities.values()}) != len(identities)
           for field in ("uid", "gid")):
        raise SetupConflict("Identity bindings overlap existing declarations")
    home = host.path(inputs.home)
    if os.path.lexists(home):
        info = host.file_info(home)
        worker = accounts["worker"]["current"]
        empty_root_home = (stat.S_ISDIR(info.st_mode) and info.st_uid == host.owner
                           and not info.st_mode & 0o077 and not any(home.iterdir()))
        if (not stat.S_ISDIR(info.st_mode) or not worker
                or (info.st_uid != worker["uid"] and not empty_root_home) or info.st_mode & 0o077):
            raise SetupConflict("Existing home is not the dedicated private worker directory")
        if not existing and any(home.iterdir()):
            raise SetupConflict("A home with data needs explicit adoption, not provisioning")
    for role_group in ("pond-pirates", "pirate-clients"):
        current = host.group(role_group)
        if current and current["gid"] <= 0:
            raise SetupConflict("Unsafe role group: " + role_group)
        groups[role_group] = {"gid": None, "current": current, "action": "keep" if current else "create"}
    credential = host.path(existing["credential_file"] if existing else
                           STATE_DIR / "credentials" / approval_credential(name))
    credential_state = None
    host.protected(credential.parent)
    if os.path.lexists(credential):
        info = check_approval_secret(credential, owner=host.owner)
        credential_state = [info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns]
    if context is None:
        operator_inputs = []
        payload = context_payload(inputs, metadata=operator_inputs)
    else:
        payload, operator_inputs = context
    proposal = {"kind": "provision", "pond": name, "inputs": inputs.as_input(),
                "accounts": accounts, "groups": groups, "review": [],
                "operator_inputs": operator_inputs,
                "home": "keep or finish empty private directory" if home.exists() else "create",
                "credential": {"path": str(credential), "action": "keep" if credential_state else "create",
                               "metadata": credential_state},
                "documents": [{"filename": n, "sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b)}
                              for n,b in payload],
                "sources": known.fingerprints() if known else {},
                "existing_declaration": hashlib.sha256(declaration.read_bytes()).hexdigest() if existing else None,
                "limits": existing["limits"] if existing else _policy(),
                "status": "proposed", "runtime": "parked; provisioning starts nothing"}
    proposal["plan_digest"] = plan_digest(proposal)
    return proposal, payload, known, existing


def plan(inputs, name=INITIAL_POND, *, host=None, config_path=None):
    host = host or Host()
    config = config_path or host.path(CONFIG_FILE)
    return _prepare(inputs, name, host, config)[0]


def _directory(path, host, mode=0o755):
    host.protected(path.parent)
    if not path.exists():
        path.mkdir(mode=mode)
    host.protected(path)


def apply(inputs, approved_digest, *, host=None, name=INITIAL_POND):
    host = host or Host()
    host.require_authority()
    # Admit and capture the seed bytes before creating even the site/lock directory.
    operator_inputs = []
    payload = context_payload(inputs, metadata=operator_inputs)
    config = host.path(CONFIG_FILE)
    _directory(config.parent, host)
    with site_lock(config.parent, owner=host.owner):
        proposal, payload, known, existing = _prepare(
            inputs, name, host, config, context=(payload, operator_inputs))
        if proposal["plan_digest"] != approved_digest:
            raise SetupConflict("Host, declaration or seed input changed since the reviewed plan")
        controller, worker = (Identity.parse(proposal["accounts"][role]["identity"])
                              for role in ("controller", "worker"))
        for folder in (config.parent / "ponds", config.parent / "ponds" / name,
                       config.parent / "credentials"):
            _directory(folder, host, mode=0o700 if folder.name == "credentials" else 0o755)
        declaration = config.parent / "ponds" / name / "config.toml"
        document = existing or dict(
            enabled=False, identity=asdict(worker),
            credential_file=str(STATE_DIR / "credentials" / approval_credential(name)),
            limits=proposal["limits"],
            pond=dict(profile_id=str(uuid4()), display_name=inputs.display_name or (
                "The Pond" if name == INITIAL_POND else name),
                files=[n for n,_ in payload], welcome_file="WELCOME.md"),
            execution=dict(files={"roots": {"home": {"read": True, "write": True}}}))
        main = known.document if known else dict(
            site=dict(schema=1, state="stopped", runtime_root="/opt/boring-crocodile/current",
                      controller=asdict(controller)),
            server=dict(public_base_url="https://localhost", bind_host="127.0.0.1", bind_port=8503,
                        allowed_hosts=["localhost"]),
            auth=dict(oauth_database_file="/var/lib/boring-crocodile/oauth.sqlite3"))
        descriptions = {n: p.document for n,p in known.ponds.items()} if known else {}
        descriptions[name] = document
        site = SiteDescription.parse(main, descriptions)
        if not existing:
            write_file(declaration, toml_document(document), mode=0o600, create=True)
        if not known:
            write_file(config, toml_document(main), mode=0o600, create=True)
        # Publish the reviewed numeric bindings before creating any identity.
        # Native account tools refuse an ID claimed by another actor after planning.
        for group_name, row in proposal["groups"].items():
            if row["action"] == "create":
                host.create_group(group_name, row["gid"])
        for role, identity in (("controller", controller), ("worker", worker)):
            if host.group(identity.group) != {"name": identity.group, "gid": identity.gid}:
                raise SetupConflict("Primary group changed during provisioning: " + identity.name)
            if proposal["accounts"][role]["action"] == "create":
                host.create_user(identity)
            if host.account(identity.name) != identity.as_user():
                raise SetupConflict("Account changed during provisioning: " + identity.name)
        host.apply_description_permissions(host.description_permissions(site, config))
        home = host.path(inputs.home)
        if not home.exists():
            home.mkdir(mode=0o700)
            host.chown(home, worker.uid, worker.gid)
        elif host.file_info(home).st_uid == host.owner and not any(home.iterdir()):
            host.chown(home, worker.uid, worker.gid)
        credential = host.path(document["credential_file"])
        if os.path.lexists(credential):
            check_approval_secret(credential, owner=host.owner)
        else:
            write_file(credential, secrets.token_urlsafe(24)+"\n", mode=0o600, create=True)
        from .site_templates import registration_directories
        host.command(["/usr/bin/systemd-tmpfiles", "--create", "-"],
                     input=registration_directories(name, worker.name))
        host.seed(worker.name, home, payload, SEED_SCRIPT)
        return {"status": "provisioned", "pond": name, "profile_id": document["pond"]["profile_id"],
                "config": str(declaration), "state": "parked" if not document["enabled"] else "enabled",
                "next": "Review the site policy, then plan and APPLY the common render"}


def plan_additional(inputs, name, *, host=None):
    return plan(inputs, name, host=host)


def apply_additional(inputs, name, approved_digest, *, host=None):
    return apply(inputs, approved_digest, name=name, host=host)


def pond_name(value):
    import re
    from .site_layout import POND_NAME
    if not isinstance(value, str) or not re.fullmatch(POND_NAME, value) or value == INITIAL_POND:
        raise SetupConflict(f"Additional Pond name must be 1..24 lowercase letters, digits and inner hyphens, not {INITIAL_POND}")
    return value
