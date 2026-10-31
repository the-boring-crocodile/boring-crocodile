"""Look at the site as it is: boring-crocodile-admin site-check.

The site is described by what is on the host, never by a record of the past:
config.toml (the controller runs on it), the unit files and their drop-ins,
the accounts, homes and credentials. Every finding is one of three kinds:

- understood: a Crocodile file that is exactly the Crocodile's template,
  filled with the values this site uses (taken from the files themselves:
  memory and task limits from the slice, runtime from the controller's
  ExecStart, URL and bind from config.toml, accounts from the homes);
  The task-policy drop-in counts only when it is a trusted root file with
  exactly the bytes task-policy writes for that unit's Pond;
- adjustment: a drop-in line the Crocodile knows (write paths, limits,
  groups, runtime caps, umask); listed, not judged;
- irregular: everything the Crocodile cannot vouch for: a base unit that
  differs from its template, an unknown drop-in directive, a missing file, an
  account, home or credential that does not fit config.toml, a Crocodile
  unit that belongs to no Pond. Changing commands stop on these.

Read-only. Without root, what only root may read is listed as unchecked.
"""
from __future__ import annotations

import argparse
import difflib
import grp
import json
import os
import pwd
import stat
import subprocess
import sys
try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib
from pathlib import Path

from .settings import Settings
from .setup import SetupInputs
from .setup_services import ServiceInputs, render_site
from .site_layout import (CONFIG_FILE, CREDENTIALS_DIR, INITIAL_POND, approval_credential,
                          budget_slice, task_unit_prefix, tmpfiles_name)
from .task_policy_admin import POLICY_DROPIN, policy_dropin, trusted_path

UNITS = Path("/etc/systemd/system")
TMPFILES = Path("/etc/tmpfiles.d")
CONTROLLER = "boring-crocodile-mcp.service"
TARGET = "boring-crocodile-mcp.target"
# Drop-in lines a site may set: listed as adjustments. Anything else is irregular.
KNOWN_DIRECTIVES = {
    "ReadWritePaths", "ReadOnlyPaths", "InaccessiblePaths", "RuntimeMaxSec",
    "RestrictSUIDSGID", "SupplementaryGroups", "Group", "UMask", "MemoryMax",
    "MemorySwapMax", "MemoryHigh", "TasksMax", "CPUQuota", "CPUWeight", "IOWeight",
    "MaxConnections", "LimitNOFILE", "Nice",
}


def pond_order(name: str) -> tuple[bool, str]:
    """The order of Ponds in combined files: the first Pond, then by name."""
    return (name != INITIAL_POND, name)


class Reality:
    """The host lookups, replaceable in tests."""

    def owner(self, path: Path) -> str:
        return pwd.getpwuid(os.stat(path).st_uid).pw_name

    def user(self, name: str) -> dict:
        entry = pwd.getpwnam(name)
        return dict(name=name, uid=entry.pw_uid, gid=entry.pw_gid, home=entry.pw_dir, shell=entry.pw_shell)

    def group(self, name: str) -> dict | None:
        try:
            entry = grp.getgrnam(name)
        except KeyError:
            return None
        return {"name": entry.gr_name, "gid": entry.gr_gid}

    def groups(self, name: str) -> list[int]:
        return os.getgrouplist(name, pwd.getpwnam(name).pw_gid)

    def uid_name(self, uid: int) -> str:
        return pwd.getpwuid(uid).pw_name

    def running_groups(self, unit: str) -> list[int] | None:
        """The supplementary groups of the unit's running main process, or None when
        it runs none. A process gets its groups when it starts, so this is what a
        group membership changed since then does not show yet."""
        shown = subprocess.run(["/usr/bin/systemctl", "show", "-p", "MainPID", "--value", unit],
                               check=True, capture_output=True, text=True).stdout.strip()
        pid = int(shown or "0")
        if pid == 0:
            return None
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("Groups:"):
                return [int(gid) for gid in line.split()[1:]]
        return []

    def is_root(self) -> bool:
        return os.geteuid() == 0


def _values(text: str) -> dict[str, str]:
    """Last value per key of a unit file (enough for the parameters read here)."""
    found = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith(("#", ";", "[")):
            key, value = line.split("=", 1)
            found[key.strip()] = value.strip()
    return found


def _size(value: str) -> int:
    units = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    return int(value[:-1]) * units[value[-1]] if value and value[-1] in units else int(value)


def _diff(expected: str, actual: str) -> list[str]:
    lines = difflib.unified_diff(expected.splitlines(), actual.splitlines(), "template", "on host",
                                 lineterm="", n=0)
    return [line for line in lines if line[:1] in "+-" and not line.startswith(("+++", "---"))][:40]


class Check:
    def __init__(self, config: Path, units: Path, tmpfiles: Path, credentials: Path, reality: Reality):
        self.config, self.units, self.tmpfiles, self.credentials = config, units, tmpfiles, credentials
        self.reality = reality
        self.understood: list[dict] = []
        self.adjustments: list[dict] = []
        self.irregular: list[dict] = []
        self.unchecked: list[dict] = []
        self.policies: dict[Path, str] = {}  # task-policy drop-in path -> the bytes task-policy writes

    def odd(self, what: str, **details) -> None:
        self.irregular.append({"what": what, **details})

    def location(self, name: str) -> Path:
        if name == "config.toml":
            return self.config
        return self.tmpfiles / name if name.endswith(".conf") else self.units / name

    def read(self, path: Path) -> str | None:
        try:
            return path.read_text()
        except FileNotFoundError:
            return None
        except PermissionError:
            self.unchecked.append({"path": str(path), "why": "needs root"})
            return None

    # -- reading the site -------------------------------------------------
    def entries(self, raw: dict, settings: Settings) -> list[tuple]:
        """Render inputs per Pond, taken from the files on the host."""
        controller = self.read(self.units / CONTROLLER)
        if controller is None:
            self.odd("controller unit missing", path=str(self.units / CONTROLLER))
            return []
        runtime = _values(controller).get("ExecStart", "").split("/bin/", 1)[0]
        server = raw["server"]
        names = sorted(settings.ponds, key=pond_order)
        if INITIAL_POND not in settings.ponds:
            self.odd("no Pond named 'pond': the site's first Pond carries the shared names")
            return []
        result = []
        for name in names:
            pond = raw["ponds"][name]
            environment = settings.environments[pond["environment"]]
            env_raw = raw["environments"][pond["environment"]]
            home = environment.default_cwd
            try:
                execution = self.reality.owner(home)
                control = self.reality.uid_name(environment.control_plane_uid)
            except (OSError, KeyError) as error:
                self.odd("home or controller account not found", pond=name, detail=str(error))
                continue
            # Limits come from the slice config.toml names (reality); a name
            # that differs from budget_slice() is reported by consistency().
            configured_slice = self.units / environment.task_cgroup_parent.name
            slice_text = self.read(configured_slice)
            task_text = self.read(self.units / f"boring-crocodile-task-{name}@.service")
            if slice_text is None or task_text is None:
                if slice_text is None:
                    self.odd("budget slice missing", pond=name, path=str(configured_slice))
                if task_text is None:
                    self.odd("task unit missing", pond=name)
                continue
            limits = _values(slice_text)
            try:
                memory, swap = _size(limits["MemoryMax"]), _size(limits["MemorySwapMax"])
                tasks, task_tasks = int(limits["TasksMax"]), int(_values(task_text)["TasksMax"])
            except (KeyError, ValueError) as error:
                self.odd("limits not readable", pond=name, detail=f"{type(error).__name__}: {error}")
                continue
            if task_tasks > tasks:
                self.odd("a command may use more tasks than its Pond's budget", pond=name,
                         detail=f"task TasksMax={task_tasks} > slice TasksMax={tasks}")
                task_tasks = tasks
            try:
                policy = ServiceInputs.parse(dict(
                    public_base_url=server["public_base_url"], bind_host=server["bind_host"],
                    bind_port=server["bind_port"], runtime_root=runtime, memory_bytes=memory,
                    swap_bytes=swap, tasks_max=tasks, task_tasks_max=task_tasks,
                    no_new_privileges=env_raw.get("no_new_privileges", True),
                    service_registration_enabled=env_raw.get("service_registration_enabled", False)))
                setup = SetupInputs.parse(dict(execution_user=execution, control_user=control,
                                               home=str(home), context_mode="starter",
                                               context_source=None, context_files=[]))
                users = {control: self.reality.user(control), execution: self.reality.user(execution)}
            except (ValueError, KeyError) as error:
                self.odd("site values not usable for the template", pond=name, detail=str(error))
                continue
            # Units do not depend on where the handbook lives; config.toml is
            # judged by the controller's own reading of it, not by bytes.
            receipt = dict(pond_name=name, status="complete", inputs=setup.as_input(), users=users,
                           context_root=str(home) + "/handbook", profile_id=pond["profile_id"],
                           documents=[{"filename": f} for f in pond.get("files", [])])
            result.append((setup, policy, receipt))
        return result

    def consistency(self, settings: Settings) -> None:
        for name, pond in settings.ponds.items():
            environment = settings.environments[pond.environment]
            expected = {
                "socket_path": Path(f"/run/boring-crocodile/workers/{name}/control.sock"),
                "task_socket_path": Path(f"/run/boring-crocodile/tasks/{name}.sock"),
                "task_unit_prefix": task_unit_prefix(name),
                "task_cgroup_parent": Path("/system.slice") / budget_slice(name),
            }
            for key, value in expected.items():
                actual = getattr(environment, key)
                if actual != value:
                    self.odd("config.toml names differ from the Crocodile's naming", pond=name,
                             key=key, expected=str(value), actual=str(actual))
            worker = self.read(self.units / f"boring-crocodile-worker-{name}.service")
            if worker is not None:
                try:
                    owner = self.reality.owner(environment.default_cwd)
                except OSError:
                    owner = None
                if _values(worker).get("User") != owner:
                    self.odd("worker account is not the home's owner", pond=name,
                             worker_user=_values(worker).get("User"), home_owner=owner)
            credential = self.credentials / approval_credential(name)
            if not self.reality.is_root():
                self.unchecked.append({"path": str(credential), "why": "needs root"})
                continue
            try:
                info = credential.lstat()
            except FileNotFoundError:
                self.odd("approval credential missing", pond=name, path=str(credential))
                continue
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                    or stat.S_IMODE(info.st_mode) != 0o600):
                self.odd("approval credential is not a private root file", pond=name, path=str(credential))

    def compare(self, rendered: dict[str, str]) -> None:
        for name, body in sorted(rendered.items()):
            if name == "config.toml":
                continue
            path = self.location(name)
            actual = self.read(path)
            if actual is None:
                if not any(entry["path"] == str(path) for entry in self.unchecked):
                    self.odd("file missing", path=str(path))
                continue
            if actual == body:
                self.understood.append({"path": str(path)})
            else:
                self.odd("differs from the Crocodile's template", path=str(path), diff=_diff(body, actual))
            if not name.endswith(".conf"):
                self.dropins(path)

    def dropins(self, unit: Path) -> None:
        directory = unit.with_name(unit.name + ".d")
        try:
            entries = sorted(directory.iterdir())
        except FileNotFoundError:
            return
        except PermissionError:
            self.unchecked.append({"path": str(directory), "why": "needs root"})
            return
        for dropin in entries:
            if dropin.suffix != ".conf":
                continue
            if dropin.name == POLICY_DROPIN:
                self.task_policy(dropin)
                continue
            text = self.read(dropin)
            if text is None:
                continue
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith(("#", ";", "[")):
                    continue
                key, _, value = stripped.partition("=")
                entry = {"path": str(dropin), "directive": key.strip(), "value": value.strip()}
                if key.strip() in KNOWN_DIRECTIVES:
                    self.adjustments.append(entry)
                else:
                    self.odd("drop-in sets a directive the Crocodile does not know", **entry)

    def task_policy(self, dropin: Path) -> None:
        """The name only selects the check: the file must be where task-policy writes
        for a Pond of config.toml, a trusted root file (no symlink), and exactly
        what task-policy renders for that Pond today."""
        expected = self.policies.get(dropin)
        if expected is None:
            self.odd("task-policy drop-in on a unit task-policy does not manage", path=str(dropin))
            return
        try:
            trusted_path(dropin)
            actual = dropin.read_bytes()
        except (OSError, ValueError) as error:
            self.odd("task-policy drop-in is not a trusted root file", path=str(dropin), detail=str(error))
            return
        if actual == expected.encode():
            self.understood.append({"path": str(dropin), "note": "exactly the task-policy tool's drop-in"})
        else:
            self.odd("task-policy drop-in differs from what task-policy writes", path=str(dropin),
                     diff=_diff(expected, actual.decode(errors="replace")))

    def pond_files(self, name: str) -> set[Path]:
        return {self.units / f"boring-crocodile-worker-{name}.service",
                self.units / f"boring-crocodile-task-{name}.socket",
                self.units / f"boring-crocodile-task-{name}@.service",
                self.units / budget_slice(name), self.tmpfiles / tmpfiles_name(name)}

    def orphans(self, rendered: dict[str, str], settings: Settings) -> None:
        rendered_paths = {self.location(name) for name in rendered}
        expected = rendered_paths | {self.units / CONTROLLER, self.units / TARGET}
        for name in settings.ponds:  # a Pond's files are its own even when it cannot be rendered
            files = self.pond_files(name)
            expected |= files
            for path in sorted(files - rendered_paths):
                if not path.exists() and not any(i.get("path") == str(path) for i in self.irregular):
                    self.odd("file missing", pond=name, path=str(path))
        candidates = [path for path in self.units.glob("boring-crocodile-*") if path.is_file()]
        candidates += [path for path in self.units.glob("system-crocodile*.slice") if path.is_file()]
        candidates += [path for path in self.tmpfiles.glob("boring-crocodile*.conf") if path.is_file()]
        for path in sorted(set(candidates) - expected):
            self.odd("Crocodile file that belongs to no Pond of config.toml", path=str(path))

    def run(self) -> dict:
        text = self.read(self.config)
        if text is None:
            self.odd("config.toml missing or unreadable", path=str(self.config))
            return self.report()
        try:
            settings = Settings.load(self.config)
            raw = tomllib.loads(text)
        except (ValueError, KeyError, OSError) as error:
            self.odd("the controller does not accept config.toml", detail=str(error))
            return self.report()
        self.understood.append({"path": str(self.config), "note": "accepted by the controller"})
        self.consistency(settings)
        for pond in settings.ponds.values():
            try:
                path, body = policy_dropin(settings.environments[pond.environment], self.units)
            except ValueError:
                continue  # task-policy refuses this Pond: no drop-in of it can be its
            self.policies[path] = body
        entries = self.entries(raw, settings)
        rendered: dict[str, str] = {}
        if entries and entries[0][2]["pond_name"] == INITIAL_POND:
            try:
                rendered = render_site(entries)
            except ValueError as error:
                self.odd("the site cannot be rendered from its own values", detail=str(error))
        self.compare(rendered)
        self.orphans(rendered, settings)
        return self.report()

    def report(self) -> dict:
        return {"status": "irregular" if self.irregular else "regular",
                "counts": {"understood": len(self.understood), "adjustments": len(self.adjustments),
                           "irregular": len(self.irregular), "unchecked": len(self.unchecked)},
                "irregular": self.irregular, "adjustments": self.adjustments,
                "understood": self.understood, "unchecked": self.unchecked}


def site_check(config: Path = CONFIG_FILE, *, units: Path = UNITS, tmpfiles: Path = TMPFILES,
               credentials: Path = CREDENTIALS_DIR, reality: Reality | None = None) -> dict:
    return Check(config, units, tmpfiles, credentials, reality or Reality()).run()


def summary(report: dict) -> str:
    counts = report["counts"]
    lines = [f"site-check: {report['status']} ({counts['understood']} understood, "
             f"{counts['adjustments']} site adjustments, {counts['irregular']} irregular, "
             f"{counts['unchecked']} unchecked)"]
    for item in report["irregular"]:
        where = item.get("path") or item.get("pond") or ""
        lines.append(f"  IRREGULAR  {item['what']}: {where}")
        for key in ("detail", "directive", "key"):
            if key in item:
                lines.append(f"             {key}: {item[key]}" + (f" = {item.get('value')}" if key == "directive" else ""))
        for diff in item.get("diff", [])[:12]:
            lines.append(f"             {diff}")
    for item in report["adjustments"]:
        lines.append(f"  adjustment {Path(item['path']).parent.name}/{Path(item['path']).name}: "
                     f"{item['directive']}={item['value']}")
    for item in report["unchecked"]:
        lines.append(f"  unchecked  {item['path']} ({item['why']})")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="boring-crocodile-admin site-check", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=CONFIG_FILE)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = site_check(args.config)
    print(json.dumps(report, indent=2) if args.json else summary(report))
    return 0 if report["status"] == "regular" else 1
