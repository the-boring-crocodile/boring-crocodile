"""Host facts and effects for provisioning and the one site reconciler."""
from __future__ import annotations

import base64
import grp
import json
import os
import pwd
import signal
from pathlib import Path
import stat
import subprocess
import sys
import time

from boring_deploy import root_child

from .site_layout import CONFIG_FILE


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


class SiteConflict(ValueError):
    pass



APPROVAL_SECRET_MIN = 24


def check_approval_secret(path, *, owner=None):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as error:
        raise SiteConflict(f"Cannot read approval secret {path}: {error.strerror}") from None
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096
                or owner is not None and (info.st_uid != owner or info.st_mode & 0o077)):
            raise SiteConflict("Unsafe approval secret: " + str(path))
        data = source.read(4097)
        try:
            length = len(data.decode("utf-8").strip())
        except UnicodeDecodeError:
            raise SiteConflict("Approval secret must be UTF-8: " + str(path)) from None
        if len(data) > 4096 or length < APPROVAL_SECRET_MIN:
            raise SiteConflict(f"Approval secret must have at least {APPROVAL_SECRET_MIN} characters "
                               f"and at most 4096 bytes: {path}")
    return info

class Host:
    def __init__(self, root=Path("/")) -> None:
        self.root = root
        self.owner = 0

    def path(self, absolute):
        return self.root / str(absolute).lstrip("/")

    def require_authority(self):
        if os.geteuid() != 0 or self.root != Path("/"):
            raise SiteConflict("Apply requires root on the target host")

    def account(self, name):
        return account(name)

    def group(self, name):
        return group(name)

    def identity_ids(self):
        users = pwd.getpwall()
        return ([(u.pw_uid, u.pw_name) for u in users],
                [(g.gr_gid, g.gr_name) for g in grp.getgrall()]
                + [(u.pw_gid, u.pw_name) for u in users])

    def system_id_range(self, kind):
        from .site_model import _read
        values = {}
        for line in _read(self.path("/etc/login.defs"), self.owner).decode().splitlines():
            fields = line.split("#", 1)[0].split()
            if len(fields) == 2 and fields[0] in {kind+"_MIN", "SYS_"+kind+"_MIN", "SYS_"+kind+"_MAX"}:
                values[fields[0]] = int(fields[1])
        low = values.get("SYS_"+kind+"_MIN", 101)
        high = values.get("SYS_"+kind+"_MAX", values.get(kind+"_MIN", 1000)-1)
        if not 0 < low <= high < 2**32-1:
            raise SiteConflict("Invalid system " + kind + " range in /etc/login.defs")
        return range(high, low-1, -1)

    def group_by_gid(self, gid):
        value = grp.getgrgid(gid)
        return {"name": value.gr_name, "gid": value.gr_gid}

    def create_group(self, name, gid=None):
        self.command(["/usr/sbin/groupadd", "--system",
                      *(["--gid", str(gid)] if gid is not None else []), name])

    def create_user(self, identity):
        self.command(["/usr/sbin/useradd", "--system", "--no-create-home",
                      "--uid", str(identity.uid), "--gid", str(identity.gid),
                      "--home-dir", identity.home, "--shell", identity.shell, identity.name])

    def chown(self, path, uid, gid):
        os.chown(path, uid, gid, follow_symlinks=False)

    def memberships(self, name, gid):
        return sorted(os.getgrouplist(name, gid))

    def protected(self, path):
        """Require root-owned, non-symlink, non-writable ancestry."""
        if path != self.root and self.root not in path.parents:
            raise SiteConflict("Directory is outside the target host: " + str(path))
        for current in (path, *path.parents):
            if current == self.root.parent and self.root != Path("/"):
                break
            if current == self.root or self.root in current.parents:
                if os.path.lexists(current):
                    s = current.lstat()
                    if not stat.S_ISDIR(s.st_mode) or s.st_uid != self.owner or s.st_mode & 0o022:
                        raise SiteConflict("Unprotected directory: " + str(current))
            if current == self.root:
                break

    def command(self, command, *, input=None, user=None):
        return root_child(command, user=user, input=input, check=True,
                          capture_output=True, text=True, timeout=60)

    def seed(self, user, home, payload, script):
        data = [str(home), [(name, base64.b64encode(body).decode()) for name, body in payload]]
        self.command(["/usr/bin/python3", "-c", script], user=user, input=json.dumps(data))

    def inactive(self, units):
        for unit in units:
            result = self.command(["/usr/bin/systemctl", "show", "--value", "-p", "ActiveState", unit])
            if result.stdout.strip() not in {"inactive", "failed"}:
                raise SiteConflict("Unit did not stop: " + unit)

    def running(self, units):
        for unit in units:
            result = self.command(["/usr/bin/systemctl", "is-active", unit])
            if result.stdout.strip() != "active":
                raise SiteConflict("Unit is not active: " + unit)

    def processes(self, site):
        units = ["boring-crocodile-mcp.service"] + [
            "boring-crocodile-worker-" + name + ".service"
            for name, pond in site.ponds.items() if pond.enabled]
        result = {}
        for unit in units:
            values = dict(line.split("=", 1) for line in self.command([
                "/usr/bin/systemctl", "show", "-p", "ActiveState", "-p", "MainPID", unit
            ]).stdout.splitlines() if "=" in line)
            result[unit] = (values.get("ActiveState"), int(values.get("MainPID", "0")))
        return result

    def runtime_report(self, site, deadline):
        # The child bounds the complete report, including Common's independently
        # budgeted limit inspections. Do not duplicate those inspections here.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SiteConflict("Local readiness deadline expired")
        script = ("import json, sys; from pathlib import Path; "
                  "from boring_crocodile.admin import runtime_status; "
                  "from boring_crocodile.site_model import SiteDescription; "
                  "site = SiteDescription.read(Path(sys.argv[1]), owner=0); "
                  "from boring_crocodile.site_host import Host; "
                  "report = runtime_status(site.runtime_settings(), "
                  "identities={n:p.identity for n,p in site.ponds.items()}); "
                  "report['processes'] = Host().processes(site); print(json.dumps(report))")
        command = [sys.executable, "-c", script, str(self.path(CONFIG_FILE))]
        with root_child(command, spawn=subprocess.Popen, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, start_new_session=True) as process:
            try:
                stdout, stderr = process.communicate(timeout=remaining)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, command, output=stdout, stderr=stderr)
        return json.loads(stdout)

    def wait_ready(self, site, *, window=60.0):
        """Bound the whole polling window, including each complete inspection."""
        self.prerequisites(site)
        deadline = time.monotonic() + window
        stable_since, previous = None, None
        expected = {"boring-crocodile-mcp.service"} | {
            "boring-crocodile-worker-" + name + ".service"
            for name, pond in site.ponds.items() if pond.enabled}
        while True:
            report = self.runtime_report(site, deadline)
            now = time.monotonic()
            processes = report.get("processes", {})
            active = (set(processes) == expected and all(
                state == "active" and type(pid) is int and pid > 0
                for state, pid in processes.values()))
            if report["status"] != "healthy" or not active:
                stable_since = None
            elif processes != previous or stable_since is None:
                stable_since = now
            elif now - stable_since >= 10.0 and now <= deadline:
                return report
            previous = processes
            remaining = deadline - now
            if remaining <= 0:
                failed = {row["pond"]: [key for key, ok in row["checks"].items() if not ok]
                          for row in report["ponds"] if row["status"] != "healthy"}
                raise SiteConflict("Local readiness did not settle: " + json.dumps(
                    {"site": report["site"], "ponds": failed, "processes": processes,
                     "required_stable_seconds": 10}))
            time.sleep(min(1.0, remaining))


    def description_permissions(self, site, config):
        from .site_layout import description_modes, SITE_LOCK
        rows = description_modes(site, config, owner=self.owner, credential_path=self.path)
        # An interrupted retirement/provision has a binding but may have no
        # group. Never grant a parked declaration to a freed/reused numeric GID.
        for name, pond in site.ponds.items():
            if not pond.enabled and self.group(pond.identity.group) != {"name": pond.identity.group, "gid": pond.identity.gid}:
                folder = config.parent / "ponds" / name
                for row in rows:
                    if Path(row["path"]) in (folder, folder / "config.toml"):
                        row.update(gid=0, mode=0o700 if row["directory"] else 0o600)
        managed = {Path(row["path"]) for row in rows}
        def private(path):
            info = path.lstat()
            if (info.st_uid != self.owner or info.st_mode & 0o022
                    or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
                return False
            return not info.st_mode & 0o077 or (
                stat.S_ISDIR(info.st_mode) and all(private(p) for p in path.iterdir()))
        for row in rows:
            path = Path(row["path"])
            # Apply creates its lock before replanning; absence is not a site change.
            row["actual"] = (self.description_metadata(path)
                             if path.name != SITE_LOCK and os.path.lexists(path) else None)
            if row["directory"] and path.exists():
                self.protected(path)
                for child in path.iterdir():
                    if child not in managed and not private(child):
                        raise SiteConflict("Non-private description sibling needs review: " + str(child))
        return rows

    def description_metadata(self, path):
        info = self.file_info(path)
        return dict(uid=info.st_uid, gid=info.st_gid, mode=stat.S_IMODE(info.st_mode),
                    acl=sorted(set(os.listxattr(path, follow_symlinks=False)) & {
                        "system.posix_acl_access", "system.posix_acl_default"}))

    def apply_description_permissions(self, rows):
        import errno
        # Leaves are private before any parent gains traversal.
        for row in sorted(rows, key=lambda r: len(Path(r["path"]).parts), reverse=True):
            path = Path(row["path"])
            self.protected(path.parent)
            info = path.lstat()
            valid = stat.S_ISDIR(info.st_mode) if row["directory"] else stat.S_ISREG(info.st_mode)
            if (not valid or (info.st_uid != self.owner and not row.get("credential"))
                    or (not row["directory"] and info.st_nlink != 1)):
                raise SiteConflict("Unsafe description entry: " + str(path))
            path.chmod(0o700 if row["directory"] else 0o600)
            for name in self.description_metadata(path)["acl"]:
                try:
                    os.removexattr(path, name, follow_symlinks=False)
                except OSError as error:
                    if error.errno != errno.ENODATA:
                        raise
            self.chown(path, row["uid"], row["gid"])
            path.chmod(row["mode"])

    def file_info(self, path):
        return path.lstat()

    def task_instances(self, patterns):
        if not patterns:
            return []
        result = self.command(["/usr/bin/systemctl", "list-units", "--all", "--plain",
                               "--no-legend", "--no-pager", *patterns])
        units = []
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) < 4 or not fields[0].endswith(".service"):
                raise SiteConflict("Cannot establish command task state: " + line)
            if fields[2] not in {"inactive", "failed"}:
                units.append(fields[0])
        return units

    def stop(self, units, tasks):
        loaded = []
        for unit in units:
            state = self.command(["/usr/bin/systemctl", "show", "--value",
                                  "-p", "LoadState", unit]).stdout.strip()
            if state == "loaded":
                loaded.append(unit)
            elif state != "not-found":
                raise SiteConflict("Cannot establish loaded unit: " + unit + ": " + state)
        instances = self.task_instances(tasks)
        if loaded or instances:
            self.command(["/usr/bin/systemctl", "stop", *loaded, *instances])
        self.inactive([*loaded, *instances])
        if self.task_instances(tasks):
            raise SiteConflict("Command tasks still run; no site file was written")

    def task_policy(self, environment):
        from .task_policy import inspect_task_policy
        return inspect_task_policy(environment, lambda command: self.command(command).stdout.strip())

    def dropins(self, unit, primary):
        """Read all applicable type, prefix and unit drop-ins; systemd owns precedence."""
        stem, suffix = unit.rsplit(".", 1)
        names = {unit + ".d", suffix + ".d"}
        names.update("-".join(stem.split("-")[:i]) + "-." + suffix + ".d"
                     for i in range(1, len(stem.split("-"))))
        roots = {primary, *(self.path(root) for root in (
            "/run/systemd/system", "/usr/local/lib/systemd/system", "/usr/lib/systemd/system"))}
        paths = set()
        for root in roots:
            for name in names:
                directory = root / name
                if os.path.lexists(directory):
                    self.protected(directory)
                    paths.update(directory.glob("*.conf"))
        return sorted(paths)

    def user_by_uid(self, uid):
        value = pwd.getpwuid(uid)
        return self.account(value.pw_name)

    def group_members(self, name):
        entry = grp.getgrnam(name)
        return set(entry.gr_mem) | {p.pw_name for p in pwd.getpwall() if p.pw_gid == entry.gr_gid}

    def group_plan(self, site):
        """The caller roster owns registration and derived worker client membership."""
        if site.state == "removed":
            return []
        from .site_layout import REGISTRATION_GROUP
        admitted = {}
        reserved = {site.controller.uid, *(p.identity.uid for p in site.ponds.values())}
        for uid in site.settings.pirate_service_callers.values():
            try:
                user = self.user_by_uid(uid)
            except KeyError:
                raise SiteConflict("No Unix account for admitted Pirate uid " + str(uid)) from None
            if not user or user["uid"] != uid:
                raise SiteConflict("Pirate uid no longer resolves to its account")
            if uid <= 0 or uid in reserved:
                raise SiteConflict("Pirates need distinct non-root accounts")
            admitted[user["name"]] = uid
        workers = {p.identity.name: p for p in site.ponds.values()}
        served = {pond for caller in site.settings.pirate_callers.values() for pond in caller.ponds}
        plans = []
        private_groups = {p.identity.group for p in site.ponds.values()}
        for name in (REGISTRATION_GROUP, "pirate-clients", *sorted(private_groups)):
            group = self.group(name)
            wanted = (set(admitted) if name == REGISTRATION_GROUP else {
                user for user,p in workers.items() if p.enabled and p.name in served} if name == "pirate-clients" else set())
            if group is None:
                if wanted:
                    raise SiteConflict("Explicitly provision group " + name)
                continue
            if group["gid"] <= 0:
                raise SiteConflict("Role groups must be non-root: " + name)
            members = self.group_members(name)
            scope = (members | wanted if name == REGISTRATION_GROUP else set(workers)
                     if name == "pirate-clients" else {site.controller.name})
            adding, removing = wanted - members, (members & scope) - wanted
            bindings = {}
            for user_name in sorted(members | wanted):
                user = self.account(user_name)
                if not user:
                    raise SiteConflict("Unix group contains an unknown account: " + user_name)
                bindings[user_name] = user["uid"]
                if user_name in removing and user["gid"] == group["gid"]:
                    raise SiteConflict("Cannot change an account's primary group: " + user_name)
            plans.append(dict(group=name, gid=group["gid"], members=sorted(members),
                              bindings=bindings, add=sorted(adding), remove=sorted(removing)))
        return plans

    def apply_groups(self, plans):
        for row in plans:
            for verb, names in (("-d", row["remove"]), ("-a", row["add"])):
                for name in names:
                    self.command(["/usr/bin/gpasswd", verb, name, row["group"]])

    def oauth_store(self, site):
        from .oauth_admin import OAuthAdmin
        return OAuthAdmin(site.settings.oauth_database_file, site.controller.uid)

    def database_owner(self, site):
        return site.controller.uid

    def retirement_home_state(self, path, uid):
        from .uninstall import _check_home
        if not os.path.lexists(path):
            return "absent"
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode) and info.st_uid == self.owner and stat.S_IMODE(info.st_mode) == 0o700:
            return "sealed"
        _check_home(path, uid)
        return "worker"

    def seal_home(self, path, uid):
        from .uninstall import _secure_home
        _secure_home(path, uid)

    def reserve_user(self, name):
        """Keep its numeric identity allocated; disable password and account login."""
        self.command(["/usr/sbin/usermod", "--lock", "--expiredate", "1", name])

    def prerequisites(self, site, *, registration_required=True, allow_permission_drift=False):
        if site.state == "removed":
            return
        for identity in [site.controller, *(p.identity for p in site.ponds.values() if p.enabled)]:
            if self.account(identity.name) != identity.as_user():
                raise SiteConflict("Identity binding differs or needs explicit provisioning: " + identity.name)
            group = self.group(identity.group)
            if group is None or group["gid"] != identity.gid:
                raise SiteConflict("Group binding differs: " + identity.group)
        for pond in site.ponds.values():
            if not pond.enabled:
                continue
            home = self.path(pond.identity.home)
            self.protected(home.parent)
            info = self.file_info(home)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != pond.identity.uid or info.st_mode & 0o022:
                raise SiteConflict("Unsafe worker home: " + str(home))
            secret = self.path(pond.credential_file)
            self.protected(secret.parent)
            if not os.path.lexists(secret):
                raise SiteConflict("Approval secret needs explicit provisioning: " + str(secret))
            info = check_approval_secret(secret)
            if not allow_permission_drift and (info.st_uid != self.owner or stat.S_IMODE(info.st_mode) != 0o600):
                raise SiteConflict("Approval secret permissions differ: " + str(secret)
                                   + f": uid {info.st_uid}, mode {stat.S_IMODE(info.st_mode):04o}; "
                                   + f"expected uid {self.owner}, mode 0600")
        runtime = self.path(site.runtime_root)
        self.protected(runtime.parent)
        if runtime.lstat().st_uid != self.owner:
            raise SiteConflict("Runtime path is not owned by root")
        self.protected(runtime.resolve(strict=True))
        if registration_required and any(p.enabled for p in site.ponds.values()):
            from .site_layout import REGISTRATION_GROUP
            if self.group(REGISTRATION_GROUP) is None:
                raise SiteConflict("Guest registration needs explicit group provisioning: " + REGISTRATION_GROUP)

