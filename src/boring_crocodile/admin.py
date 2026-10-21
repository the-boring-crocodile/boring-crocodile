"""Local operator inspection and foreground observation. No privileged RPC."""
from __future__ import annotations

import argparse
import base64
from collections import OrderedDict
import fcntl
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

from boring_worker_kit.observation import MAX_EVENT_BYTES, protected_directory, watch_path
from .settings import EnvironmentSettings, Settings
from .site_layout import CONFIG_FILE
from .watch_views import VIEWS
from boring_worker_kit.host_limits import read_host_limits
from .task_policy import inspect_task_policy

if TYPE_CHECKING:
    from .oauth_admin import OAuthAdmin


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
                 check: bool, timeout: int | None = None) -> subprocess.CompletedProcess[str]: ...


def unit_state(unit: str, runner: StatusRunner = subprocess.run, *,
               socket_capacity: bool = False) -> dict[str, str | int | None]:
    """Ask systemd for coarse state; systemd remains the state owner."""
    try:
        result = runner(
            ["/usr/bin/systemctl", "show", "--no-pager",
             "--property=LoadState", "--property=ActiveState",
             "--property=SubState",
             *(["--property=MaxConnections"] if socket_capacity else []), unit],
            capture_output=True, text=True, check=False, timeout=5,
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
    names = tuple(settings.ponds)
    if pond is not None:
        if pond not in names:
            raise ValueError("Unknown Pond: " + pond)
        names = (pond,)
    if site_reader is None:
        from .site_check import site_check
        site_reader = site_check
    try:
        site = site_reader()
        site_view: dict[str, object] = {"status": site["status"], "counts": site["counts"],
                                        "irregular": [item["what"] for item in site["irregular"]][:10]}
    except (OSError, ValueError, KeyError) as error:
        site_view = {"status": "unknown", "error": f"{type(error).__name__}: {error}"}

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
        try:
            limits = read_host_limits(env) if enabled else None
            limit_error = None
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            limits = None
            limit_error = str(error)
        authority: dict[str, object] = {"configured_no_new_privileges": getattr(env, "no_new_privileges", True),
                     "loaded_policy": "not_applicable", "sudo_behavior": "not_tested"}
        if enabled:
            def policy_run(command: list[str]) -> str:
                return runner(command, check=True, capture_output=True, text=True,
                              timeout=8).stdout.strip()
            try:
                authority = inspect_task_policy(env, policy_run)
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
        # Informational: site-check says what changing commands would stop on.
        "site_check": site_view,
        "ponds": rows,
    }



def protected_task_config(path: Path | None) -> Path:
    """A worker must not be able to choose its own root-applied authority."""
    path = path or Path(os.environ.get("BORING_CROCODILE_CONFIG", str(CONFIG_FILE)))
    for item, directory in ((path.parent, True), (path, False)):
        info = item.lstat()
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022 or (
            not stat.S_ISDIR(info.st_mode) if directory else
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
        ):
            raise ValueError("task policy requires root-owned protected config")
    return path


def oauth_store(settings: Settings) -> "OAuthAdmin":
    from .oauth_admin import OAuthAdmin
    # Check the installed controller identity, not a user-supplied UID.
    result = subprocess.run(["/usr/bin/systemctl", "show", "--no-pager",
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
    commands.add_parser("list")
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
    policy = commands.add_parser("task-policy", help="review per-Pond task authority")
    policy.add_argument("--pond", required=True)
    policy.add_argument("--apply", action="store_true")
    policy.add_argument("--approve-digest")
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
    oauth_revoke.add_argument("--approve-digest")
    oauth_commands.add_parser("clients", help="every client: origin, state, redirect hosts, live grants")
    client_add = oauth_commands.add_parser("client-add", help="pre-register a client (VS Code, Copilot Studio)")
    client_add.add_argument("--name", required=True)
    client_add.add_argument("--redirect-uri", action="append", required=True)
    client_add.add_argument("--auth", choices=["none", "client_secret_post", "client_secret_basic"], default="none")
    client_remove = oauth_commands.add_parser("client-remove", help="a client and all its grants")
    client_remove.add_argument("client_id")
    client_remove.add_argument("--apply", action="store_true")
    client_remove.add_argument("--approve-digest", help="the plan digest, required with --apply")
    client_prune = oauth_commands.add_parser("client-prune", help="clients idle this long without a refresh token")
    client_prune.add_argument("--idle-days", type=float, default=90)
    client_prune.add_argument("--apply", action="store_true")
    client_prune.add_argument("--approve-digest", help="the plan digest, required with --apply")
    oauth_backup = oauth_commands.add_parser("backup")
    oauth_backup.add_argument("--output", required=True, type=Path)
    service_add = commands.add_parser(
        "service-add", help="root-only: create service credentials, URL and explicit caller grants")
    service_add.add_argument("name")
    service_add.add_argument("--url", required=True, help="the service's MCP endpoint")
    service_add.add_argument("--ponds", required=True, help="comma-separated configured Pond or pirate/name callers")
    service_add.add_argument("--ping-timeout", type=float,
                             help="seconds a worker waits for the heartbeat (default 1; "
                                  "more for a service on another host, at most 2.5)")
    service_grants = commands.add_parser("service-grants", help="root-only: review/change callers without credential rotation")
    service_grants.add_argument("name")
    service_grants.add_argument("--callers", required=True, help="complete comma-separated caller set; preserve intended existing callers")
    service_grants.add_argument("--apply", action="store_true")
    service_grants.add_argument("--approve-digest", help="reviewed plan digest required with --apply")
    service_endpoint = commands.add_parser("service-endpoint", help="root-only: review/change audience URL without credential rotation")
    service_endpoint.add_argument("name")
    service_endpoint.add_argument("--url", required=True)
    service_endpoint.add_argument("--apply", action="store_true")
    service_endpoint.add_argument("--approve-digest", help="reviewed endpoint plan digest required with --apply")
    service_remove = commands.add_parser(
        "service-remove", help="root-only: remove a service and its tokens")
    service_remove.add_argument("name")
    service_remove.add_argument("--expected-definition", type=json.loads,
                                help="exact secret-free service-list record; stale definitions refuse removal")
    commands.add_parser("service-list", help="root-only: list services (no secrets)")
    remove = commands.add_parser(
        "uninstall", help="plan or apply configured-site removal")
    scope = remove.add_mutually_exclusive_group(required=True)
    scope.add_argument("--pond")
    scope.add_argument("--site", action="store_true")
    remove.add_argument("--delete-data", action="store_true",
                        help="also remove credentials, OAuth grants, accounts and groups (homes are never removed)")
    remove.add_argument("--apply", action="store_true")
    remove.add_argument("--approve-digest")
    return parser


def _task_policy(args: argparse.Namespace) -> None:
    """Per-Pond task authority (root)."""
    if os.geteuid() != 0:
        raise PermissionError("task policy administration requires root")
    if args.apply != bool(args.approve_digest):
        raise ValueError("--apply requires --approve-digest from a fresh plan")
    from .task_policy_admin import apply_task_policy, task_policy_plan
    settings = Settings.load(protected_task_config(args.config))
    answer = (apply_task_policy(settings,args.pond,args.approve_digest)
              if args.apply else task_policy_plan(settings,args.pond)[0])
    print(json.dumps(answer, indent=2))


def _services(args: argparse.Namespace) -> None:
    """Service credentials, URLs and caller grants (root)."""
    if os.geteuid() != 0:
        raise PermissionError("service administration requires root")
    settings = Settings.load(args.config)
    store = oauth_store(settings)
    if args.action in ("service-add", "service-grants"):
        ponds = (args.ponds if args.action == "service-add" else args.callers).split(",")
        callers = set(settings.ponds) | {"pirate/" + name for name in settings.pirate_service_callers}
        unknown = [pond for pond in ponds if pond not in callers]
        if unknown:
            raise ValueError("unknown service caller: " + ", ".join(unknown))
        shown: object
        if args.action == "service-grants":
            if args.apply != bool(args.approve_digest):
                raise ValueError("--apply and --approve-digest must be supplied together")
            shown = store.service_grants(args.name, ponds, args.approve_digest if args.apply else None)
        else:
            shown = store.service_add(args.name, args.url, ponds, args.ping_timeout)
            print("The client secret is shown only now; it is stored as its hash.", file=sys.stderr)
    elif args.action == "service-endpoint":
        if args.apply != bool(args.approve_digest):
            raise ValueError("--apply and --approve-digest must be supplied together")
        shown = store.service_endpoint(args.name, args.url, args.approve_digest if args.apply else None)
    elif args.action == "service-remove":
        shown = store.service_remove(args.name, args.expected_definition)
    else:
        shown = store.service_list()
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
        if args.apply != bool(args.approve_digest):
            raise ValueError("--apply requires --approve-digest from a fresh plan")
        digest = args.approve_digest if args.apply else None
        report = (store.client_remove(args.client_id, digest) if args.oauth_action == "client-remove"
                  else store.client_prune(args.idle_days, digest))
    else:
        if args.apply != bool(args.approve_digest):
            raise ValueError("--apply requires --approve-digest from a fresh plan")
        if profile is None:
            raise ValueError("revocation requires a Pond")
        client = None if args.all_clients else args.client
        report = (store.revoke(profile, client, args.approve_digest) if args.apply
                  else store.plan_revoke(profile, client))
    print(json.dumps(report, indent=2))


def _uninstall(args: argparse.Namespace) -> None:
    """Plan or apply configured-site removal."""
    from .uninstall import plan, apply
    if args.apply and not args.approve_digest:
        raise ValueError("--apply requires --approve-digest from a fresh plan")
    if not args.apply and args.approve_digest:
        raise ValueError("--approve-digest is only valid with --apply")
    proposal, _ = plan(
        pond=args.pond, site=args.site, delete_data=args.delete_data)
    if args.apply:
        outcome = apply(approved_digest=args.approve_digest,
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
    handlers = {"task-policy": _task_policy, "oauth": _oauth, "uninstall": _uninstall,
                "site-check": _site_check,
                **dict.fromkeys(("service-add", "service-grants", "service-endpoint",
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
