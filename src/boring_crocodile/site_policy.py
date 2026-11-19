"""Validated explicit listener, runtime and per-Pond resource policy."""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit
from .site_ports import DEFAULT_PROTECTED_PORTS, protected_ports

def parse_size(value: str) -> int:
    units = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    return int(value[:-1]) * units[value[-1]] if value and value[-1] in units else int(value)


@dataclass(frozen=True)
class ServiceInputs:
    public_base_url: str
    bind_host: str
    bind_port: int
    runtime_root: str
    memory_bytes: int
    swap_bytes: int
    tasks_max: int
    task_tasks_max: int
    no_new_privileges: bool = True
    protected_ports: tuple[int, ...] = DEFAULT_PROTECTED_PORTS

    @classmethod
    def parse(cls, value: Any) -> ServiceInputs:
        names = set(cls.__dataclass_fields__)
        optional = {"no_new_privileges", "protected_ports"}
        if not isinstance(value, dict) or set(value) - names or names - set(value) - optional:
            raise ValueError("Service inputs require: " + ", ".join(sorted(names - optional)))
        if type(value.get("no_new_privileges", True)) is not bool:
            raise ValueError("no_new_privileges must be boolean")
        for key, minimum, maximum in (
            ("bind_port", 1024, 65535),
            ("memory_bytes", 64*1024*1024, 2**60), ("swap_bytes", 0, 2**60),
            ("tasks_max", 16, 1048576), ("task_tasks_max", 16, 1048576)):
            if type(value[key]) is not int or not minimum <= value[key] <= maximum:
                raise ValueError("Invalid service input: " + key)
        if value["task_tasks_max"] > value["tasks_max"]:
            raise ValueError("Per-task task limit exceeds shared limit")
        host = value["bind_host"]
        if not isinstance(host, str):
            raise ValueError("bind_host must be an IP literal")
        ipaddress.ip_address(host)
        url = value["public_base_url"]
        if not isinstance(url, str):
            raise ValueError("Public URL must be HTTPS")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ValueError("Public URL must be an HTTPS origin without credentials/path/query")
        if not re.fullmatch(r"[A-Za-z0-9.:-]+|\[[A-Fa-f0-9:]+\](?::[0-9]+)?", parsed.netloc):
            raise ValueError("Invalid public authority")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("Invalid public port")
        runtime = value["runtime_root"]
        if not isinstance(runtime, str) or not re.fullmatch(r"/opt/[A-Za-z0-9_./-]+", runtime) or ".." in Path(runtime).parts or str(Path(runtime)) != runtime:
            raise ValueError("Runtime must be a normalized path below /opt")
        return cls(**(value | {"public_base_url":url.rstrip("/"),
                              "protected_ports": protected_ports(value.get("protected_ports", DEFAULT_PROTECTED_PORTS))}))
