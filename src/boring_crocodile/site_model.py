"""The standard site's desired description; no host mutation or progress state."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tomllib
from typing import Any, Iterator, Literal, cast

from boring_worker_kit.validation import integer, table, text
from .settings import EnvironmentSettings, PondSettings, Settings
from .site_layout import (CONTROL_CREDENTIALS, POND_NAME, approval_credential,
                          budget_slice, task_unit_prefix)
from .site_policy import ServiceInputs


NAME = re.compile(r"[a-z_][a-z0-9_-]{0,30}")
HOME_PART = re.compile(r"[A-Za-z0-9_-]+")
RESERVED_HOME_PARENTS = {"bin", "boot", "dev", "etc", "lib", "lib64",
                         "proc", "root", "run", "sbin", "sys", "tmp", "usr", "var"}

class DescriptionError(ValueError):
    """A description error with its file context already attached."""


class LegacyDescription(DescriptionError):
    """A pre-description site needs offline adoption."""


@contextmanager
def _describing(*paths: Path | None) -> Iterator[None]:
    try:
        yield
    except DescriptionError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        files = ", ".join(str(path) for path in paths if path is not None)
        if not files:
            raise
        raise DescriptionError(f"{files}: {error}") from None


def _parse_toml(path: Path, raw: bytes) -> dict[str, Any]:
    with _describing(path):
        document = raw.decode("utf-8")
        try:
            return tomllib.loads(document)
        except tomllib.TOMLDecodeError as error:
            if str(error).endswith("(at end of document)"):
                line = document.count("\n") + 1
                column = len(document.rsplit("\n", 1)[-1]) + 1
                raise ValueError(f"{error} (at line {line}, column {column})") from None
            raise


def validate_home(home: Any, execution_user: str) -> str:
    """A dedicated leaf under a protected host-owned parent."""
    if (not isinstance(home, str) or not home.startswith("/")
            or str(Path(home)) != home or any(part in ("", ".", "..") for part in home.split("/")[1:])
            or len(Path(home).parts) < 3 or len(Path(home).parts) > 8
            or any(not HOME_PART.fullmatch(part) for part in Path(home).parts[1:])
            or Path(home).parts[1] in RESERVED_HOME_PARENTS
            or Path(home).name != execution_user):
        raise ValueError(f"The home must be an absolute path ending in /{execution_user}, two to seven "
                         "folders deep, under a parent like /home or /srv (not /etc, /root, /tmp, /usr, "
                         "/var or another system folder)")
    return home

def check_account_name(name: Any, key: str = "account") -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError(key + " must be a Unix account name: lower-case letters, digits, '-' and '_', "
                         "starting with a letter or '_', at most 31 characters")
    if name in {"root", "nobody", "nogroup"}:
        raise ValueError(key + " is reserved: " + name)
    return name



@dataclass(frozen=True)
class Identity:
    name: str
    uid: int
    group: str
    gid: int
    home: str
    shell: str

    @classmethod
    def parse(cls, raw: Any, *, context: str = "identity") -> Identity:
        value = table(raw, context, {"name", "uid", "group", "gid", "home", "shell"})
        name = check_account_name(value["name"])
        group = check_account_name(value["group"], "group")
        if group != name:
            raise ValueError("standard site identities require a same-named primary group")
        for key in ("home", "shell"):
            path = text(value[key], key)
            if (not path.startswith("/") or str(Path(path)) != path or ".." in Path(path).parts
                    or not re.fullmatch(r"/[A-Za-z0-9_./-]+", path)):
                raise ValueError(f"identity {key} must be a normalized absolute path")
        return cls(name, integer(value["uid"], "uid", 1, 2**32-2),
                   group, integer(value["gid"], "gid", 1, 2**32-2), value["home"], value["shell"])

    def as_user(self) -> dict[str, object]:
        return dict(name=self.name, uid=self.uid, gid=self.gid, home=self.home, shell=self.shell)


@dataclass(frozen=True)
class PondDescription:
    name: str
    enabled: bool
    identity: Identity
    credential_file: Path
    pond: PondSettings
    execution: EnvironmentSettings
    policy: ServiceInputs
    document: dict[str, Any]

    @classmethod
    def parse(cls, name: str, raw: Any, main: dict[str, Any], controller: Identity,
              runtime: str, *,
              main_path: Path | None = None, path: Path | None = None) -> PondDescription:
        if not re.fullmatch(POND_NAME, name):
            raise ValueError("invalid Pond description name")
        value = table(deepcopy(raw), f"ponds.{name}",
                      {"enabled", "identity", "credential_file", "limits", "pond", "execution"})
        if type(value["enabled"]) is not bool:
            raise ValueError("Pond enabled must be boolean")
        identity = Identity.parse(value["identity"], context=f"ponds.{name}.identity")
        validate_home(identity.home, identity.name)
        source = text(value["credential_file"], "credential_file")
        if (not re.fullmatch(r"/[A-Za-z0-9_./-]+", source)
                or ".." in Path(source).parts or str(Path(source)) != source):
            raise ValueError("credential_file must be a normalized absolute path")
        limits = table(value["limits"], f"ponds.{name}.limits", {"memory_bytes", "swap_bytes", "tasks_max", "task_tasks_max"})
        pond = table(value["pond"], f"ponds.{name}.pond", {"profile_id", "display_name", "files"},
                     {"context_root", "projects", "welcome", "welcome_file"})
        execution = table(value["execution"], f"ponds.{name}.execution", set(),
                          {"allowed_roots", "default_cwd", "display_name", "output_limit_bytes",
                           "command_execution_enabled", "no_new_privileges", "files"})
        def redundant(key: str) -> None:
            raise ValueError(f"ponds.{name}.{key} is derived from identity.home or pond.display_name; "
                             "remove that key (published 0.4.21 sites use adopt-site.py)")
        for mapping, key, default, prefix in (
                (pond, "context_root", identity.home + "/handbook", "pond"),
                (execution, "default_cwd", identity.home, "execution"),
                (execution, "allowed_roots", [identity.home], "execution")):
            if key in mapping and mapping[key] == default:
                redundant(prefix + "." + key)
        if "display_name" in execution:
            redundant("execution.display_name")
        files = deepcopy(execution.get("files"))
        if isinstance(files, dict) and isinstance(files.get("roots"), dict):
            home = files["roots"].get("home")
            if isinstance(home, dict):
                if "path" in home:
                    redundant("execution.files.roots.home.path")
                home["path"] = identity.home
        parsed_pond = PondSettings.from_mapping({"context_root": identity.home + "/handbook", **pond,
            "environment": name,
            "approval_secret_file": str(CONTROL_CREDENTIALS / approval_credential(name))})
        parsed_execution = EnvironmentSettings.from_mapping(name, {
            "default_cwd": identity.home, "allowed_roots": [identity.home], **execution,
            "display_name": parsed_pond.display_name, "files": files,
            "socket_path": f"/run/boring-crocodile/workers/{name}/control.sock",
            "task_socket_path": f"/run/boring-crocodile/tasks/{name}.sock",
            "task_unit_prefix": task_unit_prefix(name),
            "task_cgroup_parent": f"/system.slice/{budget_slice(name)}",
            "control_plane_uid": controller.uid})
        with _describing(main_path, path):
            server = main["server"]
            policy = ServiceInputs.parse(dict(
                public_base_url=server["public_base_url"], bind_host=server["bind_host"],
                bind_port=server["bind_port"], protected_ports=server.get("protected_ports", ()),
                runtime_root=runtime, **limits,
                no_new_privileges=parsed_execution.no_new_privileges))
        return cls(name, value["enabled"], identity, Path(source), parsed_pond, parsed_execution, policy, value)


@dataclass(frozen=True)
class SiteDescription:
    state: Literal["running", "stopped", "removed"]
    runtime_root: str
    controller: Identity
    ponds: dict[str, PondDescription]
    settings: Settings
    document: dict[str, Any]
    sources: dict[Path, bytes]

    @classmethod
    def parse(cls, main: Any, ponds: dict[str, Any], *,
              sources: dict[Path, bytes] | None = None, selected: tuple[str, ...] | None = None,
              path: Path | None = None) -> SiteDescription:
        main = table(deepcopy(main), "site description", {"site", "server", "auth"}, {"pirate_services"})
        site = table(main["site"], "site", {"schema", "state", "runtime_root", "controller"})
        if type(site["schema"]) is not int or site["schema"] != 1:
            raise ValueError("unsupported site description schema")
        state = site["state"]
        if state not in ("running", "stopped", "removed") or not isinstance(state, str):
            raise ValueError("site state must be running, stopped or removed")
        controller = Identity.parse(site["controller"])
        runtime = text(site["runtime_root"], "runtime_root")
        if selected is None and "pond" not in ponds:
            raise ValueError("site description needs the initial Pond 'pond'")
        if selected is not None and set(ponds) != set(selected):
            raise ValueError("scoped description needs exactly its selected Ponds")
        parsed = {}
        for name in sorted(ponds, key=lambda name: (name != "pond", name)):
            pond_path = path.parent / "ponds" / name / "config.toml" if path is not None else None
            with _describing(pond_path):
                parsed[name] = PondDescription.parse(name, ponds[name], main, controller, runtime,
                                                       main_path=path, path=pond_path)
        identities = [controller, *(p.identity for p in parsed.values())]
        for field in ("name", "uid", "group", "gid"):
            if len({getattr(identity, field) for identity in identities}) != len(identities):
                raise ValueError(f"site identities need distinct {field} bindings")
        homes = [Path(p.identity.home) for p in parsed.values()]
        if any(a == b or a in b.parents or b in a.parents for i, a in enumerate(homes) for b in homes[i+1:]):
            raise ValueError("Pond homes must not overlap")
        if len({p.credential_file for p in parsed.values()}) != len(parsed):
            raise ValueError("Ponds need distinct credential files")
        # Reuse the runtime contract also for parked descriptions: parking must
        # not hide invalid file roots, duplicate profile IDs or overlapping context.
        settings = Settings.from_site(
            {key: main[key] for key in ("server", "auth", "pirate_services") if key in main},
            environments={name: p.execution for name, p in parsed.items()},
            ponds={name: p.pond for name, p in parsed.items()})
        if selected is None:
            for name, caller in settings.pirate_callers.items():
                if (set(caller.ponds) | set(caller.parked_ponds)) - set(parsed):
                    raise ValueError("pirate_services.callers." + name + ": unknown Pond")
        if selected is not None and any(not parsed[name].enabled for name in selected):
            raise ValueError("selected Pond is parked")
        if selected is None and state == "running" and not parsed["pond"].enabled:
            raise ValueError("a running site needs its initial Pond enabled")
        return cls(cast(Literal["running", "stopped", "removed"], state), runtime,
                   controller, parsed, settings, main, dict(sources or {}))

    @classmethod
    def read(cls, path: Path, *, owner: int | None = None,
             pond: str | None = None, ponds: tuple[str, ...] | None = None) -> SiteDescription:
        if pond is not None and ponds is not None:
            raise ValueError("select one worker or the controller Ponds, not both")
        selected = (pond,) if pond is not None else ponds
        if selected is not None and (not selected or len(set(selected)) != len(selected)
                                     or any(not re.fullmatch(POND_NAME, name) for name in selected)):
            raise ValueError("invalid Pond description selection")
        path = Path(path)
        _directory(path.parent, owner)
        sources = {path: _read(path, owner)}
        main = _parse_toml(path, sources[path])
        if "site" not in main:
            raise LegacyDescription(f"{path}: Legacy site configuration requires the one-time adoption importer")
        directory = path.parent / "ponds"
        _directory(directory, owner)
        descriptions = {}
        folders = [directory / name for name in selected] if selected is not None else sorted(directory.iterdir())
        for folder in folders:
            _directory(folder, owner)
            target = folder / "config.toml"
            if not os.path.lexists(target):
                continue  # Empty/unpublished folders are not declarations.
            sources[target] = _read(target, owner)
            descriptions[folder.name] = _parse_toml(target, sources[target])
        with _describing(path):
            return cls.parse(main, descriptions, sources=sources, selected=selected, path=path)

    def runtime_settings(self) -> Settings:
        if self.state == "removed":
            raise ValueError("site integration is removed; activate it explicitly")
        active = {name: p for name, p in self.ponds.items() if p.enabled}
        return replace(self.settings, ponds={name: p.pond for name, p in active.items()},
                       environments={name: p.execution for name, p in active.items()})

    def fingerprints(self) -> dict[str, str]:
        return {str(path): hashlib.sha256(raw).hexdigest() for path, raw in self.sources.items()}


def _directory(path: Path, owner: int | None) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or (owner is not None and info.st_uid != owner) or info.st_mode & 0o022:
        raise ValueError(f"unsafe site directory: {path}")


def _read(path: Path, owner: int | None) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (owner is not None and info.st_uid != owner) or info.st_mode & 0o022):
            raise ValueError(f"unsafe site file: {path}")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError(f"site document exceeds 1 MiB: {path}")
        return raw
    finally:
        os.close(fd)


def toml_document(value: dict[str, Any]) -> str:
    """Write supported description values; tomllib remains the parser/validator."""
    lines: list[str] = []
    def quote(item: Any) -> str:
        # JSON escapes C0 controls; TOML also forbids a literal DEL. Keep
        # Unicode scalar values intact instead of emitting surrogate pairs.
        return json.dumps(item, ensure_ascii=False).replace("\x7f", "\\u007f")
    def key_name(key: str) -> str:
        return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else quote(key)
    def emit(mapping: dict[str, Any], prefix: tuple[str, ...]) -> None:
        if prefix:
            lines.append("[" + ".".join(key_name(part) for part in prefix) + "]")
        for key, item in mapping.items():
            if not isinstance(item, dict):
                def supported(part: Any) -> bool:
                    return (type(part) in (str, int, bool) or
                            isinstance(part, (list, tuple)) and all(supported(child) for child in part))
                if not supported(item):
                    raise ValueError(f"unsupported TOML value: {key}")
                lines.append(key_name(key) + " = " + quote(item))
        lines.append("")
        for key, item in mapping.items():
            if isinstance(item, dict):
                emit(item, (*prefix, key))
    emit(value, ())
    result = "\n".join(lines)
    tomllib.loads(result.encode("utf-8").decode("utf-8"))
    return result
