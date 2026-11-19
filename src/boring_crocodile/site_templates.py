"""Pure unit generation from explicit identities and policy; no receipt or host reads."""
from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
import re
from typing import TYPE_CHECKING

from .site_layout import (CONFIG_FILE, POND_NAME, approval_credential, budget_slice, tmpfiles_name)
from .site_ports import socket_bind_denials
from .task_policy import task_directives

if TYPE_CHECKING:
    from .site_policy import ServiceInputs
    from .site_model import SiteDescription


@dataclass(frozen=True)
class UnitInputs:
    pond: str
    worker: str
    controller: str
    home: str
    policy: ServiceInputs
    credential_file: Path


def pond_units(entry: UnitInputs, configuration: Path = CONFIG_FILE) -> dict[str, str]:
    name, policy = entry.pond, entry.policy
    if not re.fullmatch(POND_NAME, name):
        raise ValueError("invalid Pond unit name")
    for path in (entry.home, str(configuration), str(entry.credential_file)):
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", path) or ".." in Path(path).parts:
            raise ValueError("unit paths must contain only simple normalized characters")
    if not re.fullmatch(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+){1,6}", entry.home):
        raise ValueError("Service home contains unsupported unit-file characters")
    bind = "[" + policy.bind_host + "]" if ":" in policy.bind_host else policy.bind_host
    values = {
        "POND": name, "BUDGET": budget_slice(name), "LISTEN_ADDRESS": f"{bind}:{policy.bind_port}",
        "SOCKET_BIND_DENY": socket_bind_denials(policy.bind_port, policy.protected_ports),
        "RUNTIME": policy.runtime_root, "EXECUTION_USER": entry.worker, "CONTROL_USER": entry.controller,
        "HOME": entry.home, "MEMORY": str(policy.memory_bytes), "SWAP": str(policy.swap_bytes),
        "TASKS": str(policy.tasks_max), "TASK_TASKS": str(policy.task_tasks_max),
        "TASK_POLICY": task_directives(policy.no_new_privileges, entry.home)}
    output = {}
    root = files("boring_crocodile").joinpath("templates/services")
    for item in root.iterdir():
        if not item.name.endswith(".in"):
            continue
        body = item.read_text()
        for key, value in values.items():
            body = body.replace("@" + key + "@", value)
        if re.search(r"@[A-Z_]+@", body):
            raise ValueError("Unresolved service template parameter")
        if item.name == "boring-crocodile-mcp.service.in":
            body = body.replace("LoadCredential=approval-secret:/etc/boring-crocodile/credentials/approval-secret",
                                f"LoadCredential={approval_credential(name)}:{entry.credential_file}")
        body = body.replace(str(CONFIG_FILE), str(configuration))
        filename = item.name[:-3].replace("pond", name)
        if item.name == "system-crocodilepond.slice.in":
            filename = budget_slice(name)
        if filename == "boring-crocodile.conf":
            filename = tmpfiles_name(name)
        output[filename] = body
    from .site_layout import registration_path
    parent = registration_path(name).parent
    worker = f"boring-crocodile-worker-{name}.service"
    output[worker] = output[worker].replace("ReadWritePaths=", f"ReadWritePaths={parent} ", 1)
    output[tmpfiles_name(name)] += registration_directories(name, entry.worker)
    return output


def registration_directories(name: str, worker: str) -> str:
    """One directory definition shared by provisioning and render."""
    from .site_layout import REGISTRATION_GROUP, REGISTRATION_ROOT, registration_path
    return (f"d {REGISTRATION_ROOT} 0755 root root - -\n"
            f"d {registration_path(name).parent} 2750 {worker} {REGISTRATION_GROUP} - -\n")


def site_units(entries: list[UnitInputs], configuration: Path = CONFIG_FILE) -> dict[str, str]:
    if not entries:
        return {}
    output: dict[str, str] = {}
    workers, access, credentials = [], [], []
    first = entries[0]
    controller_files = ("boring-crocodile-mcp.service", "boring-crocodile-mcp.socket",
                        "boring-crocodile-mcp.target")
    first_part: dict[str, str] = {}
    for entry in entries:
        part = pond_units(entry, configuration)
        if not first_part:
            first_part = dict(part)
        for name in controller_files:
            part.pop(name)
        if output.keys() & part.keys():
            raise ValueError("Rendered Pond artifacts collide")
        output.update(part)
        workers.append(f"boring-crocodile-worker-{entry.pond}.service")
        access.append(entry.worker)
        credentials.append(f"LoadCredential={approval_credential(entry.pond)}:{entry.credential_file}")
    control = first_part[controller_files[0]]
    control = _replace(control, "/bin/boring-crocodile-mcp\n",
                       "/bin/boring-crocodile-mcp --ponds " + " ".join(e.pond for e in entries) + "\n")
    control = _replace(control, f"boring-crocodile-worker-{first.pond}.service", " ".join(workers))
    control = _replace(control, "SupplementaryGroups=" + first.worker, "SupplementaryGroups=" + " ".join(access))
    control = _replace(control, f"LoadCredential={approval_credential(first.pond)}:{first.credential_file}",
                       "\n".join(credentials))
    output.update({name: first_part[name] for name in controller_files})
    output[controller_files[0]] = control
    return output


def _replace(body: str, old: str, new: str) -> str:
    if old not in body:
        raise ValueError("Service template no longer contains: " + old)
    return body.replace(old, new)


def render_description(site: SiteDescription, configuration: Path = CONFIG_FILE) -> dict[str, str]:
    if site.state == "removed":
        return {}
    return site_units([UnitInputs(p.name, p.identity.name, site.controller.name, p.identity.home,
                                  p.policy, p.credential_file)
                       for p in site.ponds.values() if p.enabled], configuration)
