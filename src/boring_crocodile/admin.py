"""Local operator inspection and foreground observation. No privileged RPC."""
from __future__ import annotations

import argparse
import base64
from collections import OrderedDict
from collections.abc import Mapping
import fcntl
import grp
import json
import os
import pwd
from pathlib import Path
import socket
import stat
import subprocess
import struct
import sys
from typing import TYPE_CHECKING, Any, BinaryIO, Callable, Literal, Protocol

from boring_deploy import root_child
from boring_worker_kit.observation import MAX_EVENT_BYTES, protected_directory, watch_path
from .settings import EnvironmentSettings, Settings
from .site_layout import CONFIG_FILE, site_lock
from .approval import confirm_apply
from .watch_views import VIEWS
from boring_worker_kit.host_limits import read_host_limits
from .task_policy import inspect_task_policy

if TYPE_CHECKING:
    from .oauth_admin import OAuthAdmin
    from .site_model import Identity
    from .site_render import ServicePortReport


def execution_identity(environment: EnvironmentSettings) -> tuple[int, int]:
    """The deployed fixed-identity task socket is owned by its execution user."""
    path = environment.task_socket_path if environment.command_execution_enabled else None
    if path is not None:
        protected_directory(path.parent)
        info = path.lstat()
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid == 0
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("expected a non-root execution-owned task socket with mode 0600")
        return info.st_uid, info.st_gid
    # Command-disabled workers have no task socket; their control socket is
    # still owned by the execution identity and provisioned in a private tree.
    path = environment.socket_path
    info = path.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid == 0
            or stat.S_IMODE(info.st_mode) != 0o660):
        raise ValueError("expected a non-root execution-owned worker socket with mode 0660")
    for parent in (path.parent, *path.parent.parents):
        parent_info = parent.lstat()
        if (not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid not in (0, info.st_uid)
                or parent_info.st_mode & 0o022):
            raise ValueError("unsafe worker socket ancestry for watch identity")
    return info.st_uid, info.st_gid


class WatchAlreadyActive(Exception):
    """Another viewer owns this Pond watch socket."""


class WatchSocket:
    """Root owns the receiving FD; SCM_CREDENTIALS authorizes senders by UID."""

    def __init__(self, environment: EnvironmentSettings) -> None:
        if os.geteuid() != 0:
            raise PermissionError("watch requires root")
        self.sock: socket.socket | None = None
        self.lock: BinaryIO | None = None
        self.bound = False
        path = watch_path(environment)
        if path is None:
            raise ValueError("this environment has no watch_root; nothing reports to a watch")
        self.path: Path = path
        self.uid, gid = execution_identity(environment)
        try:
            self.path.parent.mkdir(mode=0o755, exist_ok=True)
            protected_directory(self.path.parent)
            lock_path = self.path.with_suffix(".lock")
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            self.lock = os.fdopen(fd, "rb")
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("unsafe watch lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise WatchAlreadyActive(
                    f"Watch already active for {environment.name}; "
                    "stop the existing viewer before retrying."
                ) from error
            # Holding the root-owned lock excludes another legitimate viewer.
            if self.path.exists() or self.path.is_symlink():
                info = self.path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0:
                    raise ValueError("refusing unexpected watch path")
                self.path.unlink()
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            # Directory is protected; bind with no send permission until ready.
            old_umask = os.umask(0o777)
            try:
                self.sock.bind(str(self.path))
                self.bound = True
            finally:
                os.umask(old_umask)
            os.chown(self.path, 0, gid)
            os.chmod(self.path, 0o622)
        except BaseException:
            self.close()
            raise

    def receive(self) -> dict[str, object]:
        if self.sock is None:
            raise ValueError("watch socket is closed")
        data, ancillary, flags, _ = self.sock.recvmsg(MAX_EVENT_BYTES, socket.CMSG_SPACE(struct.calcsize("3i")))
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            raise ValueError("truncated observation event")
        identities = [struct.unpack("3i", value[:struct.calcsize("3i")])
                      for level, kind, value in ancillary
                      if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS
                      and len(value) >= struct.calcsize("3i")]
        if len(identities) != 1 or identities[0][1] != self.uid:
            raise ValueError("unexpected observation sender")
        event = json.loads(data)
        if not isinstance(event, dict) or event.get("version") != 1:
            raise ValueError("unsupported observation event")
        return event

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        if self.bound:
            self.path.unlink(missing_ok=True)
            self.bound = False
        if self.lock is not None:
            self.lock.close()
            self.lock = None


class Display:
    """Bounded bookkeeping and escaped output, including terminal controls."""

    def __init__(self) -> None:
        self.sequences: OrderedDict[str, int] = OrderedDict()

    def format(self, event: dict[str, object]) -> str:
        source, sender, sequence = event["source"], event["sender"], event["sequence"]
        if (source not in ("runner", "client", "worker") or not isinstance(sender, str)
                or len(sender) != 32 or type(sequence) is not int or sequence < 1):
            raise ValueError("invalid observation identity")
        previous = self.sequences.get(sender, 0)
        gap = sequence != previous + 1
        self.sequences[sender] = sequence
        self.sequences.move_to_end(sender)
        if len(self.sequences) > 128:
            self.sequences.popitem(last=False)
        item = dict(event)
        if item.get("event") == "output":
            data = item.pop("data")
            if not isinstance(data, str):
                raise ValueError("invalid observation output data")
            item["text"] = base64.b64decode(data, validate=True).decode("utf-8", errors="replace")
        if gap:
            item["observation_gap"] = True
        # JSON escapes newlines, ESC, bidi controls and other non-ASCII content.
        # Byte chunks may split UTF-8; raw event data remains available via --json.
        return json.dumps(item, ensure_ascii=True)


def selected(settings: Settings, pond: str) -> EnvironmentSettings:
    return settings.environments[settings.ponds[pond].environment]


def watch(environment: EnvironmentSettings, raw: bool = False,
          view: str = "compact", no_color: bool = False) -> None:
    receiver = WatchSocket(environment)
    display = Display()
    color = not (no_color or os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb") and sys.stdout.isatty()
    presentation = VIEWS[view](color=color)
    try:
        print("Watching future tasks only; best-effort observation, no replay. "
              "Missing final events mean outcome unknown. Ctrl-C stops viewing.", file=sys.stderr, flush=True)
        while True:
            try:
                event = receiver.receive()
                # Validate and update gap tracking in both display modes.
                formatted = display.format(event)
                if raw:
                    event["observation_gap"] = json.loads(formatted).get("observation_gap", False)
                    print(json.dumps(event, ensure_ascii=True), flush=True)
                else:
                    item = json.loads(formatted)
                    data = None
                    if event.get("event") == "output":
                        encoded = event["data"]
                        if not isinstance(encoded, str):
                            raise ValueError("invalid observation output data")
                        data = base64.b64decode(encoded, validate=True)
                    for row in presentation.render(item, data):
                        print(row, flush=True)
            except (ValueError, KeyError, TypeError, RecursionError):
                print("Observation incomplete: rejected malformed or untrusted event.", file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        print("\nViewer stopped; worker operations continue.", file=sys.stderr, flush=True)
    finally:
        receiver.close()


def describe(settings: Settings, name: str) -> dict[str, object]:
    env = selected(settings, name)
    pond = settings.ponds[name]
    return dict(pond=name, profile_id=pond.profile_id, display_name=pond.display_name,
                environment=env.name, default_cwd=str(env.default_cwd),
                worker_socket=str(env.socket_path),
                task_socket=str(env.task_socket_path) if env.task_socket_path else None,
                task_unit_prefix=env.task_unit_prefix,
                budget_slice=env.task_cgroup_parent.name,
                output_limit_bytes=env.output_limit_bytes,
                context_root=str(pond.context_root),
                command_execution_enabled=getattr(env, "command_execution_enabled", True),
                no_new_privileges=getattr(env, "no_new_privileges", True))


class StatusRunner(Protocol):
    def __call__(self, command: list[str], /, *, capture_output: bool, text: Literal[True],
                 check: bool, timeout: float | None = None,
                 cwd: str = "/", env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]: ...


def unit_state(unit: str, runner: StatusRunner = subprocess.run, *,
               socket_capacity: bool = False) -> dict[str, str | int | None]:
    """Ask systemd for coarse state; systemd remains the state owner."""
    try:
        result = root_child(
            ["/usr/bin/systemctl", "show", "--no-pager",
             "--property=LoadState", "--property=ActiveState",
             "--property=SubState",
             *(["--property=MaxConnections"] if socket_capacity else []), unit],
            spawn=runner, capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"unit": unit, "load": "unknown", "active": "unknown",
                "sub": "unknown", "error": str(error)}
    values = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    state: dict[str, str | int | None] = {
        "unit": unit,
        "load": values.get("LoadState", "unknown"),
        "active": values.get("ActiveState", "unknown"),
        "sub": values.get("SubState", "unknown"),
    }
    if socket_capacity:
        try:
            state["max_connections"] = int(values["MaxConnections"])
        except (KeyError, ValueError):
            state["max_connections"] = None
    if result.returncode and result.stderr.strip():
        state["error"] = result.stderr.strip()[-256:]
    return state


def path_state(path: Path | str | None) -> dict[str, object]:
    if path is None:
        return {"path": None, "kind": "unconfigured"}
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"path": str(path), "kind": "missing"}
    except OSError as error:
        return {"path": str(path), "kind": "unknown", "error": str(error)}
    kind = ("socket" if stat.S_ISSOCK(info.st_mode)
            else "directory" if stat.S_ISDIR(info.st_mode)
            else "file" if stat.S_ISREG(info.st_mode)
            else "other")
    return {"path": str(path), "kind": kind, "uid": info.st_uid,
            "gid": info.st_gid, "mode": f"{stat.S_IMODE(info.st_mode):04o}"}


def status_report(settings: Settings, pond: str | None = None,
                  runner: StatusRunner = subprocess.run,
                  site_reader: Callable[[], dict[str, Any]] | None = None,
                  ) -> dict[str, object]:
    report = runtime_status(settings, pond, runner)
    if site_reader is None:
        from .site_check import site_check
        site_reader = site_check
    try:
        site = site_reader()
        site_view: dict[str, object] = {"status": site["status"], "counts": site["counts"],
                                        "irregular": [item["what"] for item in site["irregular"]][:10]}
    except (OSError, ValueError, KeyError) as error:
        site_view = {"status": "unknown", "error": f"{type(error).__name__}: {error}"}
    # Informational: generated-file differences are separate from runtime health.
    return {**report, "site_check": site_view}


def runtime_status(settings: Settings, pond: str | None = None,
                   runner: StatusRunner = subprocess.run, *,
                   identities: Mapping[str, Identity] | None = None) -> dict[str, object]:
    """One runtime inspection owner, shared by status and render readiness."""
    names = tuple(settings.ponds)
    if pond is not None:
        if pond not in names:
            raise ValueError("Unknown Pond: " + pond)
        names = (pond,)
    target = unit_state("boring-crocodile-mcp.target", runner)
    controller = unit_state("boring-crocodile-mcp.service", runner)
    site_units_healthy = all(
        item["load"] == "loaded" and item["active"] == "active"
        for item in (target, controller)
    )
    rows = []
    for name in names:
        env = selected(settings, name)
        profile = settings.ponds[name]
        worker = unit_state(
            f"boring-crocodile-worker-{env.name}.service", runner)
        task_socket_unit = unit_state(env.task_unit_prefix + ".socket", runner, socket_capacity=True)
        budget = unit_state(env.task_cgroup_parent.name, runner)
        paths: dict[str, dict[str, object]] = {
            "worker_socket": path_state(env.socket_path),
            "task_socket": path_state(env.task_socket_path),
            "context": path_state(profile.context_root),
            # LoadCredential is intentionally visible only in the controller's
            # private mount namespace. An active controller proves systemd
            # accepted its root-managed source; admin must not expect its
            # runtime path to exist in the administrator namespace.
            "approval_credential": {
                "kind": "service_credential",
                "service_path": str(profile.approval_secret_file),
                "verified_by": "controller_active",
            },
        }
        enabled = getattr(env, "command_execution_enabled", True)

        def inspect_output(command: list[str], *, timeout: float = 8) -> str:
            result: subprocess.CompletedProcess[str] = root_child(
                command, spawn=runner, check=True, capture_output=True, text=True, timeout=timeout)
            return result.stdout.strip()

        try:
            limits = read_host_limits(env, run=inspect_output) if enabled else None
            limit_error = None
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            limits = None
            limit_error = str(error)
        authority: dict[str, object] = {"configured_no_new_privileges": getattr(env, "no_new_privileges", True),
                     "loaded_policy": "not_applicable", "sudo_behavior": "not_tested"}
        if enabled:
            try:
                authority = inspect_task_policy(env, inspect_output)
            except (OSError, ValueError, subprocess.SubprocessError):
                authority["loaded_policy"] = "unknown"
        checks = {
            "task_policy_matches": not enabled or authority["loaded_policy"] == "matches",
            "site_units_active": site_units_healthy,
            "worker_active": (worker["load"] == "loaded"
                              and worker["active"] == "active"),
            "host_limits_available": not enabled or limits is not None,
            "task_socket_capacity_usable": (
                not enabled or (limits is not None and task_socket_unit.get("max_connections") == limits.task_slots)),
            "task_socket_active": (not enabled or (
                task_socket_unit["load"] == "loaded"
                and task_socket_unit["active"] == "active")),
            "worker_socket_present": paths["worker_socket"]["kind"] == "socket",
            "task_socket_present": not enabled or paths["task_socket"]["kind"] == "socket",
            "context_present": paths["context"]["kind"] == "directory",
            "approval_credential_loaded": (
                controller["load"] == "loaded" and controller["active"] == "active"),
        }
        from .pirates import REGISTRATION_GROUP, registration_path
        paths["registration_socket"] = path_state(registration_path(env.name))
        checks["registration_socket_present"] = paths["registration_socket"]["kind"] == "socket"
        if identities is not None:
            observed = paths["registration_socket"]
            try:
                registration_gid = grp.getgrnam(REGISTRATION_GROUP).gr_gid
            except KeyError:
                registration_gid = None
            checks["registration_socket_identity"] = (
                tuple(observed.get(field) for field in ("uid", "gid", "mode"))
                == (identities[name].uid, registration_gid, "0660"))
        if identities is not None:
            identity = identities[name]
            for key, mode in (("worker_socket", "0660"), ("task_socket", "0600")):
                if key == "task_socket" and not enabled:
                    continue
                observed = paths[key]
                checks[key+"_identity"] = (
                    observed["kind"] == "socket"
                    and tuple(observed.get(field) for field in ("uid", "gid", "mode"))
                    == (identity.uid, identity.gid, mode))
        rows.append({
            **describe(settings, name),
            "limits": limits.public() if limits is not None else None,
            **({"limits_error": limit_error} if limit_error else {}),
            "status": "healthy" if all(checks.values()) else "degraded",
            "checks": checks,
            "task_authority": authority,
            "units": {"worker": worker, "task_socket": task_socket_unit,
                      "budget_slice": budget},
            "paths": paths,
        })
    healthy = site_units_healthy and all(
        row["status"] == "healthy" for row in rows)
    return {
        "status": "healthy" if healthy else "degraded",
        "site": {"target": target, "controller": controller},
        "ponds": rows,
    }



def oauth_store(settings: Settings) -> "OAuthAdmin":
    from .oauth_admin import OAuthAdmin
    # Check the installed controller identity, not a user-supplied UID.
    result = root_child(["/usr/bin/systemctl", "show", "--no-pager",
        "-P", "User", "boring-crocodile-mcp.service"], check=True,
        capture_output=True, text=True, timeout=5)
    controller = result.stdout.strip()
    if not controller:
        raise ValueError("controller unit identity unavailable")
    return OAuthAdmin(settings.oauth_database_file, pwd.getpwnam(controller).pw_uid)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    commands = parser.add_subparsers(dest="action", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--json", action="store_true",
                         help="installer inventory: enabled Pond names and admitted Pirate name-to-UID bindings")
    commands.add_parser("pirate-add", help="root-only: admit a Pirate to the running site (plan, then --apply)")
    commands.add_parser("pirate-remove", help="root-only: take a Pirate out of the running site (plan, then --apply)")
    site_check = commands.add_parser(
        "site-check", help="read-only: compare the site with what the Crocodile writes; report irregularities")
    site_check.add_argument("--json", action="store_true")
    status = commands.add_parser(
        "status", help="inspect configured Ponds and existing systemd state")
    status.add_argument("--pond")
    status.add_argument("--check", action="store_true",
                        help="exit 1 when the selected scope is degraded")
    for name in ("show", "watch"):
        sub = commands.add_parser(name)
        sub.add_argument("--pond", required=True)
        if name == "watch":
            sub.add_argument("--json", action="store_true", help="retain base64 output bytes")
            sub.add_argument("--view", choices=tuple(VIEWS), default="compact",
                             help="human-readable presentation (default: compact)")
            sub.add_argument("--no-color", action="store_true", help="plain terminal output")
    oauth = commands.add_parser("oauth", help="root-only OAuth inspection and maintenance")
    oauth_commands = oauth.add_subparsers(dest="oauth_action", required=True)
    oauth_status = oauth_commands.add_parser("status")
    oauth_status.add_argument("--pond")
    oauth_auth = oauth_commands.add_parser("authorizations")
    oauth_auth.add_argument("--pond", required=True)
    oauth_revoke = oauth_commands.add_parser("revoke")
    oauth_revoke.add_argument("--pond", required=True)
    oauth_scope = oauth_revoke.add_mutually_exclusive_group(required=True)
    oauth_scope.add_argument("--client")
    oauth_scope.add_argument("--all-clients", action="store_true")
    oauth_revoke.add_argument("--apply", action="store_true")
    oauth_commands.add_parser("clients", help="every client: origin, state, redirect hosts, live grants")
    client_add = oauth_commands.add_parser("client-add", help="pre-register a client (VS Code, Copilot Studio)")
    client_add.add_argument("--name", required=True)
    client_add.add_argument("--redirect-uri", action="append", required=True)
    client_add.add_argument("--auth", choices=["none", "client_secret_post", "client_secret_basic"], default="none")
    client_remove = oauth_commands.add_parser("client-remove", help="a client and all its grants")
    client_remove.add_argument("client_id")
    client_remove.add_argument("--apply", action="store_true")
    client_prune = oauth_commands.add_parser("client-prune", help="clients idle this long without a refresh token")
    client_prune.add_argument("--idle-days", type=float, default=90)
    client_prune.add_argument("--apply", action="store_true")
    oauth_backup = oauth_commands.add_parser("backup")
    oauth_backup.add_argument("--output", required=True, type=Path)
    commands.add_parser("service-ports", help="root-only: show rendered protected loopback service ports")
    service_add = commands.add_parser(
        "service-add", help="root-only: create service credentials, URL and explicit caller grants")
    service_add.add_argument("name")
    service_add.add_argument("--url", required=True, help="the service's MCP endpoint")
    service_add.add_argument("--apply", action="store_true")
    service_add.add_argument("--callers", required=True, metavar="NAME=ROLE", help="comma-separated caller roles")
    service_add.add_argument("--roles", required=True,
                             help="comma-separated service role names (family kit: reader and writer)")
    service_add.add_argument("--default-role", required=True, help="the service-declared default role")
    service_add.add_argument("--ponds", type=_removed_service_ponds, help=argparse.SUPPRESS)
    service_add.add_argument("--ping-timeout", type=float,
                             help="seconds a worker waits for the heartbeat (default 1; "
                                  "more for a service on another host, at most 2.5)")
    service_grants = commands.add_parser("service-grants", help="root-only: review/change callers without credential rotation")
    service_grants.add_argument("name")
    service_grants.add_argument("--callers", required=True, metavar="NAME=ROLE",
                                help="complete comma-separated caller roles; empty grants nobody")
    service_grants.add_argument("--apply", action="store_true")
    service_endpoint = commands.add_parser("service-endpoint", help="root-only: review/change audience URL without credential rotation")
    service_endpoint.add_argument("name")
    service_endpoint.add_argument("--url", required=True)
    service_endpoint.add_argument("--apply", action="store_true")
    service_remove = commands.add_parser(
        "service-remove", help="root-only: remove a service and its tokens")
    service_remove.add_argument("name")
    service_remove.add_argument("--apply", action="store_true")
    service_remove.add_argument("--expected-definition", type=json.loads,
                                help="exact secret-free service-list record; stale definitions refuse removal")
    commands.add_parser("service-list", help="root-only: list services (no secrets)")
    remove = commands.add_parser(
        "uninstall", help="plan or apply configured-site removal")
    scope = remove.add_mutually_exclusive_group(required=True)
    scope.add_argument("--pond")
    scope.add_argument("--site", action="store_true")
    remove.add_argument("--delete-data", action="store_true",
                        help="also remove credentials and OAuth grants; lock accounts and reserve UID/GID (homes are kept)")
    remove.add_argument("--apply", action="store_true")
    return parser


def _removed_service_ponds(value: str) -> str:
    raise argparse.ArgumentTypeError("replace --ponds with --callers NAME=ROLE; declare --roles and --default-role")


def _caller_roles(value: str) -> dict[str, str]:
    callers = {}
    for entry in value.split(",") if value else []:
        name, separator, role = entry.partition("=")
        if not separator or not name or not role or name in callers:
            raise ValueError("--callers needs distinct NAME=ROLE entries")
        callers[name] = role
    return callers


def _rendered_service_ports(config: Path) -> ServicePortReport:
    from .site_host import Host
    from .site_model import SiteDescription
    from .site_render import Paths, rendered_service_ports
    site = SiteDescription.read(config)
    return rendered_service_ports(site, Paths(state=config.parent), Host())


def _service_plan(args: argparse.Namespace, settings: Settings, store: OAuthAdmin,
                  config: Path) -> dict[str, object]:
    from .site_ports import local_tcp_port, require_protected_service_port
    if args.action in ("service-add", "service-endpoint"):
        require_protected_service_port(args.url, settings.protected_ports, args.name,
                                       controller_port=settings.bind_port)
        port = local_tcp_port(args.url)
        if port is not None:
            report = _rendered_service_ports(config)
            if port not in report.ports:
                reason = report.reason or "port not rendered"
                raise ValueError(f"{args.name}: loopback port {port} has no confirmed rendered bind protection "
                                 f"({reason}); review the site and apply a site render when ready")
    if args.action in ("service-add", "service-grants"):
        callers = _caller_roles(args.callers)
        known = set(settings.ponds) | {"pirate/" + name for name in settings.pirate_service_callers}
        unknown = set(callers) - known
        if unknown:
            raise ValueError("unknown service caller: " + ", ".join(sorted(unknown)))
        if args.action == "service-add":
            return store.plan_service_add(args.name, args.url, callers, args.ping_timeout,
                                          roles=args.roles.split(","), default_role=args.default_role)
        return store.service_grants(args.name, callers)
    if args.action == "service-endpoint":
        return store.service_endpoint(args.name, args.url)
    return store.plan_service_remove(args.name, args.expected_definition)


def _services(args: argparse.Namespace) -> None:
    """One confirmation/lock path; live service policy remains in SQLite."""
    if os.geteuid() != 0:
        raise PermissionError("service administration requires root")
    config = args.config or CONFIG_FILE
    if args.action == "service-ports":
        report = _rendered_service_ports(config)
        print(json.dumps({"protected_service_ports": report.ports, "reason": report.reason}, indent=2))
        return
    settings = Settings.load(config)
    store = oauth_store(settings)
    if args.action == "service-list":
        print(json.dumps(store.service_list(), indent=2))
        return
    proposal = _service_plan(args, settings, store, config)
    shown: object = proposal
    if args.apply:
        digest = confirm_apply(proposal, "change this service")
        with site_lock(config.parent):
            # Re-read the caller catalogue under the same lock as site changes;
            # the DB owner also checks the reviewed definition in its transaction.
            settings = Settings.load(config)
            store = oauth_store(settings)
            if _service_plan(args, settings, store, config) != proposal:
                raise ValueError("service plan is stale; review a fresh plan")
            if args.action == "service-add":
                shown = store.service_add(args.name, args.url, _caller_roles(args.callers),
                                          args.ping_timeout, roles=args.roles.split(","),
                                          default_role=args.default_role, digest=digest)
                print("The client secret is shown only now; it is stored as its hash.", file=sys.stderr)
            elif args.action == "service-grants":
                shown = store.service_grants(args.name, _caller_roles(args.callers), digest)
            elif args.action == "service-endpoint":
                shown = store.service_endpoint(args.name, args.url, digest)
            else:
                shown = store.service_remove(args.name, args.expected_definition, digest=digest)
    print(json.dumps(shown, indent=2))


def _oauth(args: argparse.Namespace) -> None:
    """OAuth inspection and maintenance (root)."""
    if os.geteuid() != 0:
        raise PermissionError("OAuth administration requires root")
    settings = Settings.load(args.config)
    if args.oauth_action != "backup" and getattr(args, "pond", None) and args.pond not in settings.ponds:
        if args.pond in {"pirate/" + name for name in settings.pirate_service_callers}:
            raise ValueError("oauth commands take a Pond; a Pirate caller holds only "
                             "60-second service tokens (change its service grants instead)")
        raise ValueError("unknown Pond")
    store = oauth_store(settings)
    profile = None
    if getattr(args, "pond", None):
        profile = settings.ponds[args.pond].profile_id
    report: object
    if args.oauth_action == "status":
        report = store.status(profile)
    elif args.oauth_action == "authorizations":
        if profile is None:
            raise ValueError("authorizations requires a Pond")
        report = store.authorizations(profile)
    elif args.oauth_action == "backup":
        report = store.backup(args.output)
    elif args.oauth_action == "clients":
        report = store.clients()
    elif args.oauth_action == "client-add":
        added = store.client_add(args.name, args.redirect_uri, args.auth)
        if added["client_secret"]:
            print("The client secret is shown only now.", file=sys.stderr)
        report = added
    elif args.oauth_action in ("client-remove", "client-prune"):
        proposal = (store.client_remove(args.client_id) if args.oauth_action == "client-remove"
                    else store.client_prune(args.idle_days))
        if args.apply:
            digest = confirm_apply(proposal, "remove these clients and their grants")
            report = (store.client_remove(args.client_id, digest) if args.oauth_action == "client-remove"
                      else store.client_prune(args.idle_days, digest))
        else:
            report = proposal
    else:
        if profile is None:
            raise ValueError("revocation requires a Pond")
        client = None if args.all_clients else args.client
        proposal = store.plan_revoke(profile, client)
        report = (store.revoke(profile, client,
                      confirm_apply(proposal, "revoke these sign-ins"))
                  if args.apply else proposal)
    print(json.dumps(report, indent=2))


def _uninstall(args: argparse.Namespace) -> None:
    """Plan or apply configured-site removal."""
    from .uninstall import plan, apply
    proposal, _ = plan(
        pond=args.pond, site=args.site, delete_data=args.delete_data)
    if args.apply:
        digest = confirm_apply(proposal, "remove this configured scope")
        outcome = apply(approved_digest=digest,
                        pond=args.pond, site=args.site,
                        delete_data=args.delete_data)
        print(json.dumps(outcome, indent=2))
    else:
        print(json.dumps(proposal, indent=2))


def _site_check(args: argparse.Namespace) -> None:
    """Read-only site comparison."""
    from .site_check import site_check as check_site, summary
    report = check_site(args.config or CONFIG_FILE)
    print(json.dumps(report, indent=2) if args.json else summary(report))
    if report["status"] != "regular":
        raise SystemExit(1)


def _ponds(args: argparse.Namespace) -> None:
    """List, show, watch and status of configured Ponds."""
    if args.action == "list" and args.json:
        from .site_model import SiteDescription
        site = SiteDescription.read(args.config or CONFIG_FILE)
        settings = site.runtime_settings()
        print(json.dumps({"public_base_url": settings.public_base_url,
                          "ponds": list(settings.ponds),
                          "pirates": settings.pirate_service_callers,
                          "pond_workers": {name: pond.identity.uid for name, pond in site.ponds.items()}},
                         indent=2))
        return
    settings = Settings.load(args.config)
    if args.action == "watch":
        watch(selected(settings, args.pond), args.json, args.view, args.no_color)
    elif args.action == "show":
        print(json.dumps(describe(settings, args.pond), indent=2))
    elif args.action == "status":
        report = status_report(settings, args.pond)
        print(json.dumps(report, indent=2))
        if args.check and report["status"] != "healthy":
            raise SystemExit(1)
    else:
        print(json.dumps([describe(settings, name) for name in settings.ponds], indent=2))


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("pirate-add", "pirate-remove"):
        from .site_pirates import main as pirates  # its own options and plan/apply (site_pirates.py)
        raise SystemExit(pirates(sys.argv[1].removeprefix("pirate-"), sys.argv[2:]))
    parser = build_parser()
    args = parser.parse_args()
    handlers = {"oauth": _oauth, "uninstall": _uninstall,
                "site-check": _site_check,
                **dict.fromkeys(("service-ports", "service-add", "service-grants", "service-endpoint",
                                 "service-remove", "service-list"), _services)}
    try:
        # Reject watch before reading any configuration when invoked without root.
        if args.action == "watch" and os.geteuid() != 0:
            raise PermissionError("watch requires root")
        handlers.get(args.action, _ponds)(args)
    except WatchAlreadyActive as error:
        parser.exit(2, f"{error}\n")
    except (OSError, ValueError, KeyError) as error:
        parser.exit(2, f"{type(error).__name__}: {error}\n")


if __name__ == "__main__":
    main()
