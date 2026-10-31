from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import re
from uuid import UUID

from boring_worker_kit.inputs import table, text, integer, strings
from .instructions import WELCOME
from .site_layout import CONFIG_FILE, budget_slice, task_unit_prefix
from boring_worker_kit.file_policy import FilePolicy
from boring_worker_kit.settings import validate_execution

# boring_service_kit.issuer.clients.DEFAULT_METADATA_HOSTS (a test keeps them
# equal). Not imported: that module loads the MCP SDK, and every task runner
# reads these settings (it was 0.8 of a 1 s start).
DEFAULT_METADATA_HOSTS = ("claude.ai", "chatgpt.com")

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


WATCH_ROOT = Path("/run/boring-crocodile-watch")


@dataclass(frozen=True)
class EnvironmentSettings:
    name: str
    display_name: str
    socket_path: Path
    default_cwd: Path
    allowed_roots: tuple[Path, ...]
    control_plane_uid: int
    output_limit_bytes: int
    task_socket_path: Path | None = None
    task_unit_prefix: str = "boring-crocodile-task-pond"
    task_cgroup_parent: Path = Path("/system.slice/system-crocodilepond.slice")
    command_execution_enabled: bool = True
    no_new_privileges: bool = True
    service_registration_enabled: bool = False
    files: FilePolicy = field(default_factory=lambda: FilePolicy.parse(None))
    # Admin watch sockets (boring-crocodile-admin watch), one per environment.
    watch_root: Path | None = WATCH_ROOT

    @classmethod
    def from_mapping(cls, name: str, value: dict[str, Any]) -> "EnvironmentSettings":
        value = table(value, f"environment {name}",
                      {"allowed_roots", "socket_path", "default_cwd", "control_plane_uid"},
                      {"display_name", "output_limit_bytes", "task_socket_path", "task_unit_prefix",
                       "task_cgroup_parent", "command_execution_enabled", "no_new_privileges",
                       "files", "service_registration_enabled"})
        allowed_roots = tuple(Path(item).resolve() for item in strings(value["allowed_roots"], "allowed_roots"))
        output_limit = integer(value.get("output_limit_bytes", 65536), "output_limit_bytes", 1)  # bounded by validate_execution
        task_socket = text(value["task_socket_path"], "task_socket_path") if "task_socket_path" in value else None
        if task_socket is not None and not Path(task_socket).is_absolute():
            raise ValueError("task_socket_path must be absolute")
        task_prefix = text(value.get("task_unit_prefix", task_unit_prefix(name)), "task_unit_prefix")
        enabled = value.get("command_execution_enabled", True)
        if type(enabled) is not bool:
            raise ValueError("command_execution_enabled must be boolean")
        nnp = value.get("no_new_privileges", True)
        if type(nnp) is not bool:
            raise ValueError("no_new_privileges must be boolean")
        services = value.get("service_registration_enabled", False)
        if type(services) is not bool:
            raise ValueError("service_registration_enabled must be boolean")
        if services and not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("invalid service registration environment")
        task_parent = Path(text(value.get("task_cgroup_parent", "/system.slice/" + budget_slice(name)), "task_cgroup_parent"))
        settings = cls(
            name=name,
            display_name=text(value.get("display_name", name), "display_name"),
            socket_path=Path(text(value["socket_path"], "socket_path")),
            default_cwd=Path(text(value["default_cwd"], "default_cwd")).resolve(),
            allowed_roots=allowed_roots,
            control_plane_uid=integer(value["control_plane_uid"], "control_plane_uid", 0, 2**32 - 2),
            output_limit_bytes=output_limit,
            task_socket_path=Path(task_socket) if task_socket is not None else None,
            task_unit_prefix=task_prefix,
            task_cgroup_parent=task_parent,
            command_execution_enabled=enabled,
            no_new_privileges=nnp,
            service_registration_enabled=services,
            files=FilePolicy.parse(value.get("files")),
        )
        # worker-kit owns the execution rules (name, paths, default_cwd inside an
        # allowed root, `<product>-task-<name>` prefix, slice, output limit).
        validate_execution(settings)
        return settings


def context_files(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x for x in value):
        raise ValueError("Context files must be a nonempty list of filenames")
    if len(set(value)) != len(value):
        raise ValueError("Duplicate context filename")
    if any(Path(x).is_absolute() or ".." in Path(x).parts for x in value):
        raise ValueError("Context filenames must be relative and stay within their root")
    return tuple(value)


@dataclass(frozen=True)
class PondSettings:
    profile_id: str
    display_name: str
    environment: str
    approval_secret_file: Path
    context_root: Path
    files: tuple[str, ...]
    projects: dict[str, tuple[str, ...]] = field(default_factory=dict)
    welcome: str = WELCOME  # one default welcome; a Pond normally sets welcome_file
    welcome_file: str | None = None

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "PondSettings":
        value = table(value, "Pond", {"profile_id", "display_name", "environment",
                      "approval_secret_file", "context_root", "files"},
                      {"projects", "welcome", "welcome_file"})
        profile_id = text(value["profile_id"], "profile_id")
        if str(UUID(profile_id)) != profile_id:
            raise ValueError("Pond profile_id must be a canonical persistent UUID")
        root, secret = Path(text(value["context_root"], "context_root")), Path(text(value["approval_secret_file"], "approval_secret_file"))
        if not root.is_absolute() or not secret.is_absolute():
            raise ValueError("Pond context and approval secret paths must be absolute")
        name = value["display_name"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Pond display_name must be nonempty")
        return cls(profile_id, name, text(value["environment"], "environment"), secret, root,
                   context_files(value["files"]),
                   {k: context_files(table(v, "project", {"files"})["files"])
                    for k, v in table(value.get("projects", {}), "projects").items()},
                   text(value.get("welcome", cls.welcome), "welcome", empty=True),
                   context_files([value["welcome_file"]])[0] if "welcome_file" in value else None)


def validate_ponds(ponds: dict[str, PondSettings],
                   environments: dict[str, EnvironmentSettings]) -> None:
    if not ponds:
        raise ValueError("The config needs at least one [ponds.<name>] section")
    ids, workers, sockets = set(), set(), set()
    task_sockets, task_prefixes, task_parents = set(), set(), set()
    roots: list[Path] = []
    for pond in ponds.values():
        if pond.environment not in environments:
            raise ValueError("Pond refers to an unknown environment")
        worker = environments[pond.environment]
        if pond.profile_id in ids or pond.environment in workers or worker.socket_path.resolve() in sockets:
            raise ValueError("Ponds need distinct profile IDs, environments and worker sockets")
        root = pond.context_root.resolve()
        if any(root == other or root in other.parents or other in root.parents for other in roots):
            raise ValueError("Pond context roots must not overlap")
        ids.add(pond.profile_id)
        workers.add(pond.environment)
        if not worker.socket_path.is_absolute():
            raise ValueError("Pond worker sockets must be absolute")
        sockets.add(worker.socket_path.resolve())
        if worker.task_socket_path is not None:
            task_socket = worker.task_socket_path.resolve()
            if (task_socket in task_sockets or worker.task_unit_prefix in task_prefixes
                    or worker.task_cgroup_parent in task_parents):
                raise ValueError("Ponds need distinct task sockets, unit prefixes and cgroup slices")
            task_sockets.add(task_socket)
            task_prefixes.add(worker.task_unit_prefix)
            task_parents.add(worker.task_cgroup_parent)
        roots.append(root)



@dataclass(frozen=True)
class Settings:
    public_base_url: str
    bind_host: str
    bind_port: int
    allowed_hosts: tuple[str, ...]
    oauth_database_file: Path
    environments: dict[str, EnvironmentSettings]
    ponds: dict[str, PondSettings]
    pirate_service_callers: dict[str, int] = field(default_factory=dict)
    pirate_service_socket: Path = Path("/run/boring-crocodile-authority/pirate-services.sock")
    # Who may sign in (boring_service_kit.issuer): metadata documents on these
    # hosts; open dynamic registration only when switched on.
    metadata_document_hosts: tuple[str, ...] = DEFAULT_METADATA_HOSTS
    dynamic_registration: bool = False

    def __post_init__(self) -> None:
        validate_ponds(self.ponds, self.environments)
        for name, uid in self.pirate_service_callers.items():
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name) or type(uid) is not int or not 0 < uid < 2**32-1:
                raise ValueError("Pirate service callers need a plain name and positive Unix UID")
        if len(set(self.pirate_service_callers.values())) != len(self.pirate_service_callers):
            raise ValueError("Pirate service callers must have distinct UIDs")
        if not self.pirate_service_socket.is_absolute() or ".." in self.pirate_service_socket.parts:
            raise ValueError("Pirate authority socket must be absolute")

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        selected = path or Path(os.environ.get("BORING_CROCODILE_CONFIG", str(CONFIG_FILE)))
        with selected.open("rb") as source:
            data = tomllib.load(source)
        table(data, "config", {"server", "auth", "environments"}, {"ponds", "pirate_services"})
        table(data["environments"], "environments")
        auth = table(data["auth"], "auth", {"oauth_database_file"}, {"dynamic_registration", "metadata_document_hosts"})
        dynamic = auth.get("dynamic_registration", False)
        if not isinstance(dynamic, bool):
            raise ValueError("dynamic_registration must be true or false")
        server = table(data["server"], "server", {"public_base_url", "bind_host", "bind_port", "allowed_hosts"})
        database_file = Path(text(auth["oauth_database_file"], "oauth_database_file"))
        if not database_file.is_absolute():
            raise ValueError("oauth_database_file must be absolute")
        pirates = table(data.get("pirate_services", {}), "pirate_services", set(), {"callers", "socket"})
        return cls(
            pirate_service_callers=table(pirates.get("callers", {}), "pirate_services.callers"),
            pirate_service_socket=Path(text(pirates.get("socket", str(cls.pirate_service_socket)), "pirate service socket")),
            public_base_url=text(server["public_base_url"], "public_base_url").rstrip("/"),
            bind_host=text(server["bind_host"], "bind_host"),
            bind_port=integer(server["bind_port"], "bind_port", 1, 65535),
            allowed_hosts=strings(server["allowed_hosts"], "allowed_hosts"),
            oauth_database_file=database_file,
            dynamic_registration=dynamic,
            metadata_document_hosts=strings(auth["metadata_document_hosts"], "metadata_document_hosts")
            if "metadata_document_hosts" in auth else DEFAULT_METADATA_HOSTS,
            environments={name: EnvironmentSettings.from_mapping(name, value)
                          for name, value in data["environments"].items()},
            ponds={name: PondSettings.from_mapping(value)
                   for name, value in table(data.get("ponds", {}), "ponds").items()},
        )
