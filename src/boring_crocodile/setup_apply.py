"""Privileged initial account/context provisioning; no service activation.

System account operations use host tools. A small receipt records setup progress,
not package ownership. Existing installations/accounts are never adopted here.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from uuid import uuid4

from .setup import account, group, inspect, plan_digest, context_payload
from .site_layout import CONFIG_FILE, INITIAL_POND, STATE_DIR, site_lock, write_file

class SetupConflict(ValueError):
    pass


class Host:
    def __init__(self, root=Path("/")):
        self.root = root
        self.owner = 0

    def path(self, absolute):
        return self.root / str(absolute).lstrip("/")

    def require_authority(self):
        if os.geteuid() != 0 or self.root != Path("/"):
            raise SetupConflict("Apply requires root on the target host")

    def account(self, name):
        return account(name)

    def group(self, name):
        return group(name)

    def create_group(self, name):
        subprocess.run(["/usr/sbin/groupadd", "--system", name], check=True)

    def create_user(self, name, home, access):
        subprocess.run(["/usr/sbin/useradd", "--system", "--no-create-home",
                        "--gid", name, *(["--groups", access] if access else []), "--home-dir", home,
                        "--shell", "/usr/sbin/nologin", name], check=True)

    def add_membership(self, name, access):
        subprocess.run(["/usr/sbin/usermod", "--append", "--groups", access, name], check=True)

    def chown(self, path, uid, gid):
        os.chown(path, uid, gid, follow_symlinks=False)

    def memberships(self, name, gid):
        return sorted(os.getgrouplist(name, gid))

    def fresh_plan(self, inputs):
        return inspect(inputs)

    def site_check(self):
        from .site_check import site_check
        return site_check()

    def controller_user(self):
        from .site_check import CONTROLLER, UNITS, _values
        return _values((UNITS / CONTROLLER).read_text()).get("User")

    def protected(self, path):
        """Require root-owned, non-symlink, non-writable ancestry."""
        for current in (path, *path.parents):
            if current == self.root.parent and self.root != Path("/"):
                break
            if current == self.root or self.root in current.parents:
                if os.path.lexists(current):
                    s = current.lstat()
                    if not stat.S_ISDIR(s.st_mode) or s.st_uid != self.owner or s.st_mode & 0o022:
                        raise SetupConflict("Unprotected directory: " + str(current))
            if current == self.root:
                break


def write_receipt(path, data):
    """A receipt is private (0600) and written whole."""
    write_file(path, json.dumps(data, indent=2) + "\n", mode=0o600)


def read_receipt(path, owner):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_mode & 0o077 or info.st_nlink != 1:
        raise SetupConflict("Setup receipt is not a private owned regular file")
    return json.loads(path.read_text())


def verify_identity(host, kind, name, expected):
    actual = host.account(name) if kind == "users" else host.group(name)
    if actual != expected:
        raise SetupConflict("Recorded identity changed: " + name)
    if kind == "users":
        if host.memberships(name, actual["gid"]) != expected_memberships(host, actual):
            raise SetupConflict("Recorded account memberships changed: " + name)


def expected_memberships(host, user):
    # Each worker has only its primary group; controller joins the Pond groups.
    if user["name"] == getattr(host, "shared_control_name", None):
        return sorted(set(host.shared_control_gids) | {host.access_gid})
    if user["home"] != "/nonexistent":
        return [user["gid"]]
    return sorted({user["gid"], host.access_gid})


def apply(inputs, approved_digest, *, resume=False, host=None, _addition=None):
    host = host or Host()
    host.require_authority()
    state_dir = host.path(STATE_DIR)
    if _addition is not None:
        state_dir = state_dir / "ponds" / _addition["name"]
    receipt_path = state_dir / "initial-setup.json"
    home = host.path(inputs.home)
    # Before any writes, reject symlinks/writable parents and live installations.
    host.protected(state_dir)
    host.protected(home.parent)
    if not home.parent.is_dir():
        raise SetupConflict("Execution home parent must already exist: " + str(home.parent))
    if _addition is None and os.path.lexists(state_dir / "config.toml"):
        raise SetupConflict("This host already has a site: add a Pond with --add-pond")
    if not os.path.lexists(receipt_path):
        _prepare(host, inputs, approved_digest, resume, _addition, state_dir, receipt_path, home)
    else:
        _check_receipt(host, inputs, approved_digest, resume, receipt_path)
    # The site lock: the initial setup takes it here, once its state folder
    # exists (made by the exclusive mkdir in _prepare); --add-pond holds it
    # already (apply_additional), and a second flock in this process would refuse.
    lock = (site_lock(host.path(STATE_DIR), owner=host.owner) if _addition is None
            else contextlib.nullcontext())
    with lock:
        receipt = read_receipt(receipt_path, host.owner)
        payload = _staged_payload(host, state_dir, receipt)

        def save():
            write_receipt(receipt_path, receipt)
        _ensure_groups(host, inputs, receipt, save)
        host.access_gid = receipt["groups"][inputs.execution_user]["gid"]
        if _addition is not None:
            _join_controller(host, inputs, receipt, save, _addition)
        _ensure_users(host, inputs, receipt, save)
        if receipt.get("schema_version") != 2:
            raise SetupConflict(f"Unsupported setup receipt (schema {receipt.get('schema_version')})")
        if receipt["status"] == "complete":
            # Editable context is owned by the member; a setup retry must not
            # compare it to seed bytes or overwrite subsequent collaboration.
            return {"status": "already_complete", "context_root": receipt["context_root"],
                    "service_activated": False}
        _build_workspace(host, home, receipt, payload, save)
        worker = receipt["users"][inputs.execution_user]
        _transfer_ownership(host, home, receipt, payload, worker, save)
        return {"status": "complete", "context_root": receipt["context_root"],
                "execution_uid": worker["uid"], "control_uid": receipt["users"][inputs.control_user]["uid"],
                "service_activated": False}


def _prepare(host, inputs, approved_digest, resume, _addition, state_dir, receipt_path, home):
    """First run: check the approved plan, write the receipt and stage the context."""
    fresh = (plan_additional(inputs, _addition["name"], host=host)
             if _addition is not None else host.fresh_plan(inputs))
    if fresh["review"]:
        raise SetupConflict("Existing identities or paths require review; no automatic adoption")
    if plan_digest(fresh) != approved_digest:
        raise SetupConflict("Host or context changed since the approved plan")
    if resume:
        raise SetupConflict("No initial-setup receipt to resume")
    if os.path.lexists(state_dir) or os.path.lexists(home):
        raise SetupConflict("Initial setup requires unused setup and home paths")
    payload = context_payload(inputs)
    expected = fresh["context"]["documents"]
    if [{"filename": name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
            for name, raw in payload] != expected:
        raise SetupConflict("Context changed while preparing apply")
    if _addition is not None:
        parent = state_dir.parent
        if not parent.exists():
            parent.mkdir(mode=0o755)
        host.protected(parent)
    state_dir.mkdir(mode=0o755)
    receipt = {"schema_version": 2, "context_root": inputs.home + "/handbook", "inputs": inputs.as_input(),
               "profile_id": str(uuid4()),
               "plan_digest": approved_digest, "status": "preparing",
               "groups": {}, "users": {}, "directories": [], "documents": expected}
    if _addition is not None:
        control = _addition["control"]
        receipt["pond_name"] = _addition["name"]
        receipt["users"][control["name"]] = control
        receipt["groups"][control["name"]] = _addition["control_group"]
    write_receipt(receipt_path, receipt)
    # Keep approved bytes in protected staging to support interrupted retries.
    staged = state_dir / "initial-context"
    staged.mkdir(mode=0o700)
    for index, (_, raw) in enumerate(payload):
        with (staged / str(index)).open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    receipt["status"] = "prepared"
    write_receipt(receipt_path, receipt)


def _check_receipt(host, inputs, approved_digest, resume, receipt_path):
    """A later run: the receipt must match these inputs and allow continuing."""
    receipt = read_receipt(receipt_path, host.owner)
    if receipt["inputs"] != inputs.as_input():
        raise SetupConflict("Inputs differ from the recorded setup")
    # A completed receipt is revalidated later. Its first approved digest is
    # authoritative; a repeat plan necessarily observes existing accounts.
    if receipt["status"] != "complete" and receipt["plan_digest"] != approved_digest:
        raise SetupConflict("Inputs or approved digest differ from the recorded setup")
    if receipt["status"] != "complete" and not resume:
        raise SetupConflict("Interrupted setup: inspect receipt, then explicitly use --resume")
    if receipt["status"] in {"preparing", "transferring"}:
        raise SetupConflict("Context staging or ownership transfer interrupted; manual inspection required")


def _staged_payload(host, state_dir, receipt):
    staged = state_dir / "initial-context"
    host.protected(staged)
    payload = []
    for index, document in enumerate(receipt["documents"]):
        path = staged / str(index)
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_uid != host.owner or st.st_mode & 0o077:
            raise SetupConflict("Unsafe staged context")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != document["sha256"]:
            raise SetupConflict("Staged context changed")
        payload.append((document["filename"], raw))
    return payload


def _ensure_groups(host, inputs, receipt, save):
    for name in (inputs.execution_user, inputs.control_user):
        if name in receipt["groups"]:
            if host.group(name) != receipt["groups"][name]:
                raise SetupConflict("Recorded group changed: " + name)
            continue
        if host.group(name):
            raise SetupConflict("Unrecorded existing group; inspect interrupted account operation")
        host.create_group(name)
        current = host.group(name)
        if current is None or current["gid"] == 0:
            raise SetupConflict("Group creation did not produce a safe identity")
        receipt["groups"][name] = current
        save()


def _join_controller(host, inputs, receipt, save, _addition):
    """--add-pond: the shared controller joins the new Pond's access group."""
    control = _addition["control"]
    host.shared_control_name = control["name"]
    host.shared_control_gids = _addition["control_gids"]
    expected = set(host.shared_control_gids)
    current = set(host.memberships(control["name"], control["gid"]))
    target = expected | {host.access_gid}
    if current != target:
        if current != expected:
            raise SetupConflict("Controller memberships changed during apply")
        receipt["membership_pending"] = True
        save()
        host.add_membership(control["name"], inputs.execution_user)
        if set(host.memberships(control["name"], control["gid"])) != target:
            raise SetupConflict("Controller membership update did not match the plan")
    receipt["membership_pending"] = False
    receipt["membership_added"] = True
    save()


def _ensure_users(host, inputs, receipt, save):
    for name, user_home in ((inputs.execution_user, inputs.home),
                            (inputs.control_user, "/nonexistent")):
        if name not in receipt["users"]:
            if host.account(name):
                raise SetupConflict("Unrecorded existing account; inspect interrupted account operation")
            host.create_user(name, user_home, inputs.execution_user if name == inputs.control_user else None)
            current = host.account(name)
            if (current is None or current["uid"] == 0 or current["gid"] != receipt["groups"][name]["gid"]
                    or current["home"] != user_home or current["shell"] != "/usr/sbin/nologin"):
                raise SetupConflict("Account creation did not match requested identity")
            receipt["users"][name] = current
            save()
        verify_identity(host, "users", name, receipt["users"][name])


def _recorded_dir(host, path, receipt, save, unrecorded):
    """Make a root-private directory once, recorded; refuse one nobody recorded."""
    if not os.path.lexists(path):
        path.mkdir(mode=0o700)
        receipt["directories"].append(str(path))
        save()
    elif str(path) not in receipt["directories"]:
        raise SetupConflict(unrecorded)
    host.protected(path)


def _build_workspace(host, home, receipt, payload, save):
    # Directory construction is root-private until all files are copied.
    for path in (home, home / "handbook", home / "work", home / "lab"):
        _recorded_dir(host, path, receipt, save, "Unrecorded directory appeared; manual inspection required")
    root = home / "handbook"
    for name, raw in payload:
        path = root / name
        parent = root
        for component in Path(name).parts[:-1]:
            parent = parent / component
            _recorded_dir(host, parent, receipt, save, "Unrecorded context directory")
        host.protected(path.parent)
        if os.path.lexists(path):
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != host.owner or info.st_nlink != 1 or path.read_bytes() != raw:
                raise SetupConflict("Existing document differs; will not overwrite")
        else:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())


def _transfer_ownership(host, home, receipt, payload, worker, save):
    # Only planned documents/directories may be handed to the execution user.
    expected_paths = {str(home / "handbook" / name) for name, _ in payload}
    expected_paths.update(path for path in receipt["directories"]
                          if path == str(home) or path.startswith(str(home) + "/"))
    actual_paths = {str(home), *(str(path) for path in home.rglob("*"))}
    if actual_paths != expected_paths:
        raise SetupConflict("Unexpected files in workspace; no ownership transfer")
    # Ownership transfer is last; interruption during this phase needs review.
    receipt["status"] = "transferring"
    save()
    for path in [*home.rglob("*"), home]:
        if path.is_symlink():
            raise SetupConflict("Unexpected workspace symlink")
        host.chown(path, worker["uid"], worker["gid"])
    receipt["status"] = "complete"
    save()


def pond_name(value):
    import re
    from .site_layout import POND_NAME
    if not isinstance(value, str) or not re.fullmatch(POND_NAME, value) or value == INITIAL_POND:
        raise SetupConflict(f"Additional Pond name must be 1..24 lowercase letters, digits and inner hyphens, not {INITIAL_POND}")
    return value


def site_receipts(host):
    """Read only protected setup provenance, never discover or adopt arbitrary users."""
    state = host.path(STATE_DIR)
    host.protected(state)
    initial = read_receipt(state / "initial-setup.json", host.owner)
    if initial.get("status") != "complete" or not initial.get("profile_id"):
        raise SetupConflict("Additional Ponds require completed profile-aware initial setup")
    result = {"pond": initial}
    parent = state / "ponds"
    host.protected(parent)
    if parent.exists():
        for entry in sorted(parent.iterdir()):
            name = pond_name(entry.name)
            host.protected(entry)
            result[name] = read_receipt(entry / "initial-setup.json", host.owner)
    return result


def validate_site(host, receipts, adding):
    initial = receipts[INITIAL_POND]
    control_name = initial["inputs"]["control_user"]
    control = initial["users"][control_name]
    control_group = initial["groups"][control_name]
    if host.account(control_name) != control or host.group(control_name) != control_group:
        raise SetupConflict("Recorded controller identity changed")
    expected = {control["gid"]}
    optional = set()
    for name, receipt in receipts.items():
        if name != adding and receipt.get("status") != "complete":
            raise SetupConflict("Another Pond has incomplete setup; inspect it first")
        if receipt["inputs"]["control_user"] != control_name or receipt["users"].get(control_name) != control:
            raise SetupConflict("Pond receipt refers to another controller")
        for group_name, value in receipt["groups"].items():
            if host.group(group_name) != value:
                raise SetupConflict("Recorded group changed: " + group_name)
        access = receipt["groups"].get(receipt["inputs"]["execution_user"])
        if access:
            if receipt.get("status") == "complete" or receipt.get("membership_added"):
                expected.add(access["gid"])
            elif name == adding and receipt.get("membership_pending"):
                optional.add(access["gid"])
        worker_name = receipt["inputs"]["execution_user"]
        worker = receipt["users"].get(worker_name)
        if worker:
            if host.account(worker_name) != worker or not access:
                raise SetupConflict("Recorded worker identity changed")
            if set(host.memberships(worker_name, worker["gid"])) != {worker["gid"], access["gid"]}:
                raise SetupConflict("Recorded worker memberships changed")
    current = set(host.memberships(control_name, control["gid"]))
    if current not in (expected, expected | optional):
        raise SetupConflict("Controller memberships differ from recorded Pond groups")
    # Apply handles the target membership separately, including a recorded pending update.
    target = receipts.get(adding)
    if target and target["groups"].get(target["inputs"]["execution_user"]):
        expected.discard(target["groups"][target["inputs"]["execution_user"]]["gid"])
    return control, control_group, expected


def running_site(host, adding):
    """A running site (config.toml exists) is read as it is, not from receipts:
    site-check must find it regular; the controller is the account its unit
    runs as; its group memberships are the host's."""
    from .site_check import summary
    report = host.site_check()
    if report["status"] != "regular":
        raise SetupConflict("site-check found what --add-pond cannot handle; resolve it first:\n"
                            + summary(report))
    control_name = host.controller_user()
    control, control_group = host.account(control_name), host.group(control_name)
    if not control or not control_group:
        raise SetupConflict("Controller account of the running site not found: " + str(control_name))
    parent = host.path(STATE_DIR / "ponds")
    if parent.exists():
        host.protected(parent)
        for entry in sorted(parent.iterdir()):
            other = pond_name(entry.name)
            host.protected(entry)
            if other != adding and read_receipt(entry / "initial-setup.json", host.owner).get("status") != "complete":
                raise SetupConflict("Another Pond has incomplete setup; inspect it first: " + other)
    gids = set(host.memberships(control_name, control["gid"]))
    site = {"running": True, "site_check": report["counts"], "controller": control,
            "controller_groups": sorted(gids)}
    return control, control_group, gids, site


def _site_for(host, name):
    if os.path.lexists(host.path(CONFIG_FILE)):
        return running_site(host, name)
    receipts = site_receipts(host)  # before activation the receipts are all there is
    control, control_group, gids = validate_site(host, receipts, name)
    return control, control_group, gids, receipts


def plan_additional(inputs, name, *, host=None):
    host = host or Host()
    name = pond_name(name)
    control, _, gids, site = _site_for(host, name)
    if inputs.control_user != control["name"]:
        raise SetupConflict("Additional Pond must use the site's controller")
    review = []
    for user in (inputs.execution_user,):
        if host.account(user):
            review.append(f"The account {user} already exists; setup never takes over an existing account")
    for group_name in (inputs.execution_user,):
        if host.group(group_name):
            review.append(f"The group {group_name} already exists; setup never takes over an existing group")
    paths = [host.path(inputs.home), host.path(STATE_DIR / "ponds") / name]
    for path in paths:
        host.protected(path.parent)
        if os.path.lexists(path):
            review.append(f"{path} already exists; setup never writes over it")
    config = host.path(CONFIG_FILE)
    config_hash = None
    if os.path.lexists(config):
        info = config.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != host.owner or info.st_mode & 0o022:
            raise SetupConflict("Existing configuration must be protected")
        config_hash = hashlib.sha256(config.read_bytes()).hexdigest()
    documents = [{"filename": filename, "sha256": hashlib.sha256(raw).hexdigest(),
                  "bytes": len(raw)} for filename, raw in context_payload(inputs)]
    return {"schema_version": 1, "stage": "plan_only", "pond_name": name,
            "status": "review_required" if review else "proposed",
            "inputs": inputs.as_input(), "review": review,
            "site": site, "existing_config_sha256": config_hash,
            "controller_groups_before": sorted(gids),
            "controller_group_to_add": inputs.execution_user,
            "context": {"documents": documents, "overwrite_existing": False},
            "pending": ["Then --site-extend: the new Pond's services and approval secret, "
                        "and the Crocodile restarted to join its group"]}


def apply_additional(inputs, name, approved_digest, *, resume=False, host=None):
    host = host or Host()
    host.require_authority()
    name = pond_name(name)
    state = host.path(STATE_DIR)
    host.protected(state)
    with site_lock(state, owner=host.owner):
        control, control_group, gids, _ = _site_for(host, name)
        if inputs.control_user != control["name"]:
            raise SetupConflict("Additional Pond must use the site's controller")
        return apply(inputs, approved_digest, resume=resume, host=host,
                     _addition={"name": name, "control": control,
                                "control_group": control_group, "control_gids": gids})
