"""Apply the reviewed task-service authority policy for one existing Pond."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Callable
from .settings import Settings
from .site_layout import site_lock, write_file

from .task_policy import task_dropin, inspect_task_policy

_POLICY_KEYS = {"NoNewPrivileges", "PrivateTmp", "ProtectSystem", "ProtectHome",
                "ReadWritePaths", "ProtectControlGroups", "CapabilityBoundingSet",
                "RestrictSUIDSGID", "LockPersonality"}

def _property(run, unit, name):
    line = run(["/usr/bin/systemctl", "show", "--no-pager", "-p", name, unit])
    if not line.startswith(name + "="):
        raise ValueError("missing systemd property: " + name)
    return line.split("=",1)[1]


UNIT_ROOT = Path('/etc/systemd/system')
POLICY_DROPIN = '50-pond-task-policy.conf'


def _run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=8).stdout.strip()


def policy_dropin(env, unit_root: Path = UNIT_ROOT) -> tuple[Path, str]:
    """Where this tool writes the task-policy drop-in of env's task unit, and its exact
    content. ValueError when the Pond can have none. site-check compares against this."""
    if not env.command_execution_enabled or env.task_socket_path is None:
        raise ValueError('Pond has no command task service')
    name = env.task_unit_prefix
    if name != "boring-crocodile-task-" + env.name or not re.fullmatch(r'[A-Za-z0-9_-]+', name):
        raise ValueError('invalid task unit prefix')
    home = str(env.default_cwd)
    if not re.fullmatch(r"/[A-Za-z0-9_/-]+", home):
        raise ValueError("task policy needs a systemd-safe home path")
    return unit_root / (name + '@.service.d') / POLICY_DROPIN, task_dropin(env.no_new_privileges, home)


def task_policy_plan(settings: Settings, pond: str, *, unit_root: Path = UNIT_ROOT,
                     run: Callable[[list[str]], str] = _run) -> tuple[dict[str, object], str]:
    if pond not in settings.ponds:
        raise ValueError('unknown Pond')
    env = settings.environments[settings.ponds[pond].environment]
    path, body = policy_dropin(env, unit_root)
    name = env.task_unit_prefix
    unit = name + '@.service'
    probe = name + '@policy-probe.service'
    active = _property(run, 'boring-crocodile-worker-'+env.name+'.service', 'ActiveState')
    loaded = _property(run, probe, 'FragmentPath')
    if not loaded or not Path(loaded).is_file():
        raise ValueError('task service template is not installed')
    trusted_path(Path(loaded))
    inputs = [str(loaded).encode(), Path(loaded).read_bytes()]
    # Refuse to claim the selected authority when another drop-in can override it.
    others = _property(run, probe, 'DropInPaths').split()
    for item in others:
        if item == str(path):
            continue
        extra = Path(item)
        trusted_path(extra)
        content = extra.read_text()
        inputs.extend([str(extra).encode(), content.encode()])
        if any(line.split('=',1)[0].strip() in _POLICY_KEYS
               for line in content.splitlines() if '=' in line and not line.lstrip().startswith(('#',';'))):
            raise ValueError('conflicting task policy drop-in')
    if path.exists() or path.is_symlink():
        trusted_path(path)
        before = path.read_bytes()
    else:
        before = None
    digest = hashlib.sha256(pond.encode()+b'\0'+str(env.no_new_privileges).encode()+b'\0'+
                            str(path).encode()+b'\0'+(before or b'')+b'\0'+body.encode()+b'\0'+b'\0'.join(inputs)).hexdigest()
    return {'pond':pond, 'unit':unit, 'worker_active':active,
            'no_new_privileges':env.no_new_privileges, 'path':str(path),
            'current': 'absent' if before is None else 'present',
            'plan_digest':digest, 'proposed_policy':body}, body


def trusted_path(path, *, directory=False):
    info = path.lstat()
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not valid or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022
            or (not directory and info.st_nlink != 1)):
        raise ValueError("unsafe task policy path: " + str(path))
    return info


def apply_task_policy(settings: Settings, pond: str, digest: str, *,
                      unit_root: Path = UNIT_ROOT,
                      run: Callable[[list[str]], str] = _run) -> dict[str, object]:
    if os.geteuid() != 0:
        raise PermissionError("task policy apply requires root")
    with site_lock():  # the drop-in is a site file, like the units uninstall removes
        return _apply_locked(settings, pond, digest, unit_root=unit_root, run=run)


def _apply_locked(settings: Settings, pond: str, digest: str, *, unit_root: Path,
                  run: Callable[[list[str]], str]) -> dict[str, object]:
    plan, body = task_policy_plan(settings, pond, unit_root=unit_root, run=run)
    if digest != plan["plan_digest"]:
        raise ValueError("task policy plan changed; review again")
    if plan["worker_active"] != "inactive":
        raise ValueError("stop the selected worker and let active/queued work settle first")
    env = settings.environments[settings.ponds[pond].environment]
    units = run(["/usr/bin/systemctl", "list-units", "--all", "--plain", "--no-legend",
                 env.task_unit_prefix + "@*.service"]).splitlines()
    if any(re.search(r"\b(active|activating|deactivating)\b", line) for line in units):
        raise ValueError("command tasks still active; retry after cleanup")
    path = Path(plan["path"])
    if path.parent.exists() or path.parent.is_symlink():
        trusted_path(path.parent, directory=True)
    else:
        path.parent.mkdir(mode=0o755)
    before = path.read_bytes() if path.exists() else None
    before_mode = stat.S_IMODE(path.stat().st_mode) if before is not None else 0o644
    previous = inspect_task_policy(env, run)
    write_file(path, body, mode=0o644)
    try:
        run(["/usr/bin/systemctl", "daemon-reload"])
        # Reinspect drop-ins after reload, including previously unloaded changes.
        task_policy_plan(settings, pond, unit_root=unit_root, run=run)
        loaded = _property(run, env.task_unit_prefix+"@policy-probe.service", "DropInPaths")
        if str(path) not in loaded.split():
            raise ValueError("systemd did not load the task policy")
        effective = inspect_task_policy(env, run)
        if effective["loaded_policy"] != "matches":
            raise ValueError("loaded task policy does not match the requested policy: " +
                             json.dumps({"missing_properties": effective["missing_properties"],
                                         "mismatches": effective["mismatches"]}, sort_keys=True))
    except BaseException:
        try:
            if before is None:
                path.unlink(missing_ok=True)
            else:
                write_file(path, before, mode=before_mode)
            run(["/usr/bin/systemctl", "daemon-reload"])
            restored = inspect_task_policy(env, run)
            if restored["properties"] != previous["properties"]:
                raise ValueError("loaded policy differs after restoring the previous drop-in")
        except BaseException as error:
            raise RuntimeError("task policy failed and rollback needs root inspection; worker remains stopped") from error
        raise
    return {"status": "applied", "pond": pond, "no_new_privileges": env.no_new_privileges,
            "effective_policy": effective,
            "next": "Start the selected worker; verify real sudo behavior in a guest before live use."}
