"""The two explicit systemd command-task authority profiles."""
from __future__ import annotations

from typing import Callable
from .settings import EnvironmentSettings


def task_directives(no_new_privileges: bool, home: str) -> str:
    if type(no_new_privileges) is not bool:
        raise ValueError("no_new_privileges must be boolean")
    if no_new_privileges:
        # List directives append across template/drop-ins; own the complete list.
        # RestrictSUIDSGID stays off: systemd answers
        # openat2 with ENOSYS under it, which breaks tar and dpkg-deb on
        # Ubuntu 26.04, and it would refuse setgid directories. NoNewPrivileges
        # and the account remain the boundary.
        return ("NoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\n"
                "ProtectHome=read-only\nReadWritePaths=\nReadWritePaths="+home+"\n"
                "ProtectControlGroups=yes\nCapabilityBoundingSet=\n"
                "RestrictSUIDSGID=no\nLockPersonality=yes")
    # A false NNP line alone cannot enable sudo: RestrictSUIDSGID and
    # LockPersonality imply NNP; the empty capability set and mount namespace
    # would still constrain a command even after sudo changed its UID.
    return ("NoNewPrivileges=no\nPrivateTmp=no\nProtectSystem=no\n"
            "ProtectHome=no\nReadWritePaths=\nProtectControlGroups=no\n"
            "CapabilityBoundingSet=~\nRestrictSUIDSGID=no\nLockPersonality=no")


def task_dropin(no_new_privileges: bool, home: str) -> str:
    return "[Service]\n" + task_directives(no_new_privileges, home) + "\n"


def expected_properties(no_new_privileges, home):
    """Properties systemd exposes for the maintained directive mapping."""
    result = dict(line.split("=", 1) for line in task_directives(no_new_privileges, home).splitlines())
    result.pop("CapabilityBoundingSet")
    return result


def inspect_task_policy(environment: EnvironmentSettings,
                        run: Callable[[list[str]], str]) -> dict[str, object]:
    """Inspect an unstarted template instance; this does not prove sudo works."""
    import re
    from pathlib import Path
    prefix = environment.task_unit_prefix
    if prefix != "boring-crocodile-task-" + environment.name or not re.fullmatch(r"[A-Za-z0-9_-]+", prefix):
        raise ValueError("invalid fixed task unit prefix")
    mode = getattr(environment, "no_new_privileges", True)
    expected = expected_properties(mode, str(environment.default_cwd))
    # These extra restrictions can defeat the administrative profile, including
    # implicit NNP. Do not override root's settings silently.
    if not mode:
        expected.update({
            "PrivateDevices": "no", "ProtectKernelTunables": "no",
            "ProtectKernelModules": "no", "ProtectKernelLogs": "no",
            "RestrictRealtime": "no", "MemoryDenyWriteExecute": "no",
            "RestrictNamespaces": "no", "SystemCallFilter": "~",
            "RestrictAddressFamilies": "~",
        })
    unit = prefix + "@policy-probe.service"
    reply = run(["/usr/bin/systemctl", "show", "--no-pager",
                 *["--property=" + name for name in expected], unit])
    loaded = dict(line.split("=", 1) for line in reply.splitlines() if "=" in line)
    missing = sorted(set(expected) - set(loaded))
    # Numeric D-Bus representation avoids a maintained capability-name table.
    path = run(["/usr/bin/busctl", "call", "org.freedesktop.systemd1",
                "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
                "LoadUnit", "s", unit])
    match = re.fullmatch(r'o "(/org/freedesktop/systemd1/unit/[A-Za-z0-9_]+)"', path.strip())
    if not match:
        raise ValueError("cannot inspect task capability policy")
    caps = run(["/usr/bin/busctl", "get-property", "org.freedesktop.systemd1",
                match[1], "org.freedesktop.systemd1.Service", "CapabilityBoundingSet"])
    match = re.fullmatch(r"t ([0-9]+)", caps.strip())
    if not match:
        raise ValueError("cannot inspect task capability policy")
    actual_capabilities = int(match[1])
    maximum = int(Path("/proc/sys/kernel/cap_last_cap").read_text())
    if not 0 <= maximum < 64:
        raise ValueError("unsupported kernel capability range")
    kernel_mask = (1 << (maximum + 1)) - 1
    expected_capabilities = 0 if mode else kernel_mask
    loaded["CapabilityBoundingSet"] = actual_capabilities & kernel_mask
    mismatches = {name: {"expected": value, "loaded": loaded.get(name)}
                  for name, value in expected.items() if name in loaded and loaded[name] != value}
    # A site drop-in may add writable paths (a shared folder, say): deliberate,
    # so reported as an extension, not a mismatch. Missing ones still are.
    extended = {}
    if "ReadWritePaths" in mismatches:
        wanted = set(expected["ReadWritePaths"].split())
        present = loaded["ReadWritePaths"].split()
        if mode and wanted <= set(present):
            del mismatches["ReadWritePaths"]
            extended["ReadWritePaths"] = [path for path in present if path not in wanted]
    if loaded["CapabilityBoundingSet"] != expected_capabilities:
        mismatches["CapabilityBoundingSet"] = {
            "expected": expected_capabilities, "loaded": loaded["CapabilityBoundingSet"]}
    return {"configured_no_new_privileges": mode,
            "loaded_policy": "unknown" if missing else "mismatch" if mismatches else "matches",
            "missing_properties": missing, "mismatches": mismatches, "extended": extended,
            "properties": loaded, "verification_scope": "unstarted_template_instance",
            "sudo_behavior": "not_tested"}
