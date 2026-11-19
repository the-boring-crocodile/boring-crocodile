"""One synthetic host for description, render and later provisioning journeys."""
import os
from fnmatch import fnmatchcase
from pathlib import Path
import subprocess
from types import SimpleNamespace

from boring_crocodile.site_model import SiteDescription, toml_document
from boring_crocodile.site_render import Host, Paths
from test_site_model import description


class FakeHost(Host):
    def __init__(self, root, *, provisioned=True):
        super().__init__(root)
        self.owner = os.getuid()
        self.calls, self.active, self.enabled, self.fail = [], set(), set(), None
        self.loaded, self.properties, self.stubborn = {}, {}, set()
        self.owned, self.group_owned, self.extra_groups = {}, {}, {}
        self.role_members = {"pond-pirates": set(), "pirate-clients": set()}
        self.main, self.ponds = description()
        self.paths = Paths(root / "etc/boring-crocodile", root / "etc/systemd/system",
                           root / "etc/tmpfiles.d", self.owner)
        for path in (self.paths.state, self.paths.units, self.paths.tmpfiles,
                     self.path("/opt/boring-crocodile/current")):
            path.mkdir(parents=True, exist_ok=True)
        if provisioned:
            self.extra_groups["pond-pirates"] = {"name": "pond-pirates", "gid": 1301}
        if not provisioned:
            self.identities = {}
            self.path("/home").mkdir(exist_ok=True)
            return
        self.identities = {i.name: i for i in [self.site.controller, *(p.identity for p in self.site.ponds.values())]}
        from boring_crocodile.site_model import Identity
        self.identities["pirate-parrot"] = Identity("pirate-parrot", 1300, "pirate-parrot", 1300,
                                                   "/nonexistent", "/usr/sbin/nologin")
        for identity in self.identities.values():
            self.extra_groups[identity.group] = {"name": identity.group, "gid": identity.gid}
        self.extra_groups["pirate-clients"] = {"name": "pirate-clients", "gid": 1302}
        self.role_members["pond-pirates"].add("pirate-parrot")
        for pond in self.site.ponds.values():
            self.path(pond.identity.home).mkdir(parents=True)
            self.owned[str(self.path(pond.identity.home))] = pond.identity.uid
            self.path(pond.credential_file).parent.mkdir(parents=True, exist_ok=True)
            self.path(pond.credential_file).write_text("fixture-only-approval-secret-123456")
            self.path(pond.credential_file).chmod(0o600)
        self.save()

    @property
    def site(self):
        return SiteDescription.parse(self.main, self.ponds)

    def save(self):
        self.paths.config.write_text(toml_document(self.main))
        for name, document in self.ponds.items():
            target = self.paths.state / "ponds" / name / "config.toml"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(toml_document(document))

    def task_policy(self, environment):
        from boring_crocodile.task_policy import expected_properties
        from unittest.mock import patch
        mode = environment.no_new_privileges
        def run(command):
            if command[0] == "/usr/bin/busctl":
                if command[1] == "call":
                    return 'o "/org/freedesktop/systemd1/unit/probe"'
                maximum = int(Path("/proc/sys/kernel/cap_last_cap").read_text())
                return "t " + str(0 if mode else (1 << (maximum + 1)) - 1)
            values = expected_properties(mode, str(environment.default_cwd))
            values.update(getattr(self, "task_policy_values", {}))
            return "\n".join(key + "=" + value for key, value in values.items())
        with patch.object(self, "command", side_effect=lambda command: SimpleNamespace(stdout=run(command))):
            return super().task_policy(environment)

    def require_authority(self):
        assert self.root != Path("/")

    def account(self, name):
        identity = self.identities.get(name)
        return identity.as_user() if identity else None

    def group(self, name):
        identity = self.identities.get(name)
        return {"name": name, "gid": identity.gid} if identity else self.extra_groups.get(name)

    def file_info(self, path):
        info = path.lstat()
        return SimpleNamespace(st_mode=info.st_mode, st_uid=self.owned.get(str(path), info.st_uid),
                               st_gid=self.group_owned.get(str(path), info.st_gid))

    def identity_ids(self):
        groups = self.extra_groups
        return ([(i.uid, i.name) for i in self.identities.values()],
                [(g["gid"], name) for name,g in groups.items()]
                + [(i.gid, i.name) for i in self.identities.values()])

    def system_id_range(self, kind):
        return range(999, 100, -1)

    def group_by_gid(self, gid):
        return next((self.group(name) for number,name in self.identity_ids()[1] if number == gid),
                    None) or self._missing_uid(gid)

    def create_group(self, name, gid=None):
        if gid is None:
            occupied = {number for number,_ in self.identity_ids()[1]}
            gid = next(i for i in self.system_id_range("GID") if i not in occupied)
        assert not self.group(name)
        assert all(number != gid for number,_ in self.identity_ids()[1])
        self.extra_groups[name] = {"name": name, "gid": gid}

    def create_user(self, identity):
        assert not self.account(identity.name)
        assert all(i.uid != identity.uid for i in self.identities.values())
        assert self.group(identity.group)["gid"] == identity.gid
        self.identities[identity.name] = identity

    def memberships(self, name, gid):
        return sorted({gid, *(self.group(g)["gid"] for g,members in self.role_members.items() if name in members)})

    def user_by_uid(self, uid):
        return next((i.as_user() for i in self.identities.values() if i.uid == uid), None) or self._missing_uid(uid)

    def _missing_uid(self, uid):
        raise KeyError(uid)

    def group_members(self, name):
        return set(self.role_members.get(name, set()))

    def oauth_store(self, site):
        if hasattr(self, "store"):
            return self.store
        from boring_crocodile.oauth_admin import OAuthAdmin
        return OAuthAdmin(self.path(site.settings.oauth_database_file), self.owner)

    def chown(self, path, uid, gid):
        self.owned[str(path)] = uid
        self.group_owned[str(path)] = gid

    def seed(self, user, home, payload, script):
        import base64
        import json
        import sys
        if self.fail == "seed":
            self.fail = None
            raise OSError("synthetic seed interruption")
        # Same unprivileged seed program as production, inside the temporary host.
        data = [str(home), [(name, base64.b64encode(body).decode()) for name, body in payload]]
        subprocess.run([sys.executable, "-c", script], input=json.dumps(data),
                       text=True, check=True, capture_output=True)

    def wait_ready(self, site, *, window=60.0):
        # Journeys cover sequencing; Host.wait_ready logic has a controlled-clock test.
        return self.runtime_report(site, None)

    def runtime_report(self, site, deadline):
        self.calls.append(["readiness"])
        ready = self.fail != "readiness"
        return {"status": "healthy" if ready else "degraded",
                "site": {"controller": {"active": "active"}},
                "processes": {"boring-crocodile-mcp.service": ("active", 101), **{
                    "boring-crocodile-worker-" + p.name + ".service": ("active", 102+i)
                    for i, p in enumerate(site.ponds.values()) if p.enabled}},
                "ponds": [{"pond": p.name, "status": "healthy" if ready else "degraded",
                           "checks": {"synthetic_runtime": ready}}
                          for p in site.ponds.values() if p.enabled]}

    def database_owner(self, site):
        return self.owner

    def retirement_home_state(self, path, uid):
        from boring_crocodile.uninstall import _mounted_descendant, UninstallConflict
        import stat
        if not os.path.lexists(path):
            return "absent"
        info = self.file_info(path)
        if not stat.S_ISDIR(info.st_mode) or _mounted_descendant(path):
            raise UninstallConflict("Unsafe home: link or mounted subtree")
        if info.st_uid == self.owner and stat.S_IMODE(info.st_mode) == 0o700:
            return "sealed"
        if info.st_uid != uid:
            raise UninstallConflict("Home uid changed")
        return "worker"

    def seal_home(self, path, uid):
        if self.retirement_home_state(path, uid) != "worker":
            raise ValueError("Home changed")
        self.owned[str(path)] = self.owner
        path.chmod(0o700)

    def reserve_user(self, name):
        if self.fail == "reserve_user":
            self.fail = None
            raise OSError("synthetic account reservation failure")
        super().reserve_user(name)

    def delete_user(self, name):
        if self.fail == "delete_user":
            self.fail = None
            raise OSError("synthetic user deletion failure")
        self.identities.pop(name)
        for members in self.role_members.values():
            members.discard(name)

    def command(self, command, *, input=None):
        self.calls.append(command)
        if input is not None:
            self.calls.append(["stdin", input])
        if self.fail and self.fail in command:
            self.fail = None
            raise subprocess.CalledProcessError(1, command, stderr="synthetic failure")
        if command[0].endswith("groupdel"):
            self.extra_groups.pop(command[-1], None)
        if command[0].endswith("gpasswd"):
            _, verb, user, group = command
            if verb == "-a":
                self.role_members[group].add(user)
            else:
                self.role_members[group].discard(user)
        if command[0].endswith("systemctl"):
            verb, *units = command[1:]
            if verb == "show":
                unit = units[-1]
                prop = units[units.index("-p") + 1]
                value = self.properties.get((prop, unit))
                if value is None:
                    value = ("active" if unit in self.active else "inactive") if prop == "ActiveState" else (
                        "loaded" if unit in self.loaded or (self.paths.units / unit).exists() else "not-found")
                return SimpleNamespace(stdout=value)
            if verb == "list-units":
                patterns = [u for u in units if not u.startswith("--")]
                rows = [f"{u} loaded active running Synthetic task" for u in sorted(self.active)
                        if any(fnmatchcase(u, pattern) for pattern in patterns)]
                return SimpleNamespace(stdout="\n".join(rows))
            if verb == "daemon-reload":
                self.loaded = {p.name: p.read_bytes() for p in self.paths.units.iterdir() if p.is_file()}
            elif verb == "enable":
                self.enabled.update(u for u in units if not u.startswith("--"))
            elif verb == "disable":
                self.enabled.difference_update(units)
            elif verb in {"start", "restart"}:
                self.active.update(units)
            elif verb == "stop":
                self.active.difference_update(set(units) - self.stubborn)
            elif verb == "is-active":
                if not set(units) <= self.active:
                    raise subprocess.CalledProcessError(3, command)
                return SimpleNamespace(stdout="active", returncode=0)
        return SimpleNamespace(stdout="", returncode=0)
