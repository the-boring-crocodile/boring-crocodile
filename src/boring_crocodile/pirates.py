"""Pirates: guests that register themselves, one registry entry per flavour.

A Pirate is a Unix account. It registers each active flavour on this Pond's
register.sock; the kernel proves the account (SO_PEERCRED), the flavour is its
own choice. The registry pings every flavour. A job runs one assignment by
holding a single `run` connection open until the Pirate answers; closing it
stops the run on the Pirate's side.

Registration socket: `/run/boring-crocodile/guests/<environment>/register.sock`,
group `pond-pirates`; only members may register, and only Pirates do (services
register with the control plane over HTTP, see services.py).

Wire protocol (newline JSON, one request per connection), both directions:
`{"ok": true, "result": {...}}`, `{"ok": false, "error": {"code", "message",
...}}` (a refusal, code passed through) or `{"ok": false, "failure": "text"}`.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import grp
import json
import math
import os
from pathlib import Path
import pwd
import re
import stat
from typing import Any

from boring_registry import Entry, Registry

from boring_worker_kit.framing import peer_uid
from boring_worker_kit.inputs import MAX_JOB_SECONDS
from .outcomes import Refusal, is_code
from .unix_sockets import bound_identity, clear_stale_socket, release_socket
from boring_worker_kit.protocol_json import decode_object

FLAVOUR = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
REGISTRATION_GROUP = "pond-pirates"
REGISTRATION_ROOT = Path("/run/boring-crocodile/guests")
# One frame (a JSON line) in either direction, the same value as the Pirate's
# boring_pirates.limits.MAX_FRAME: what one side may write, the other reads.
MAX_FRAME = 1024 * 1024
MAX_FLAVOURS = 64
MAX_REGISTRATIONS = 16
REQUEST_TIMEOUT = 5.0
# The Pirate enforces the assignment's own timeout (null: its default). The
# worker only stops holding the connection a little later, for a Pirate that
# never answers; with null it assumes the longest job, MAX_JOB_SECONDS.
RUN_MARGIN_SECONDS = 10.0
RUN_KEYS = {"pirate", "flavour", "assignment", "timeout_seconds"}


def registration_path(environment: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", environment):
        raise ValueError("invalid registration environment")
    return REGISTRATION_ROOT / environment / "register.sock"


def authorize_uid(uid: int) -> None:
    """Host account membership is policy; socket DAC also checks process groups."""
    try:
        user = pwd.getpwuid(uid)
        gid = grp.getgrnam(REGISTRATION_GROUP).gr_gid
        groups = os.getgrouplist(user.pw_name, user.pw_gid)
    except KeyError as error:
        raise PermissionError("unknown registrant identity or registration group") from error
    if uid == 0 or gid not in groups:
        raise PermissionError(f"registrant is not in {REGISTRATION_GROUP}")


def encode(value: dict[str, object]) -> bytes:
    data = (json.dumps(value, allow_nan=False, ensure_ascii=True,
                       separators=(",", ":")) + "\n").encode()
    if len(data) > MAX_FRAME:
        raise Refusal("limit", "Pirate request exceeds the frame limit")
    return data


def answer(response: dict[str, Any]) -> dict[str, Any]:
    """The result; or the Pirate's refusal (its code) or failure, raised."""
    if response.get("ok") is True and set(response) == {"ok", "result"}:
        if isinstance(response["result"], dict):
            return response["result"]
    elif response.get("ok") is False and set(response) == {"ok", "error"}:
        detail = response["error"]
        if (isinstance(detail, dict) and is_code(detail.get("code"))
                and isinstance(detail.get("message"), str)):
            extra = {key: value for key, value in detail.items() if key not in ("code", "message")}
            raise Refusal(detail["code"], detail["message"], **extra)
    elif response.get("ok") is False and set(response) == {"ok", "failure"}:
        if isinstance(response["failure"], str):
            raise RuntimeError("the Pirate reported a failure: " + response["failure"][:512])
    raise ValueError("invalid Pirate response envelope")


async def exchange(path: str, uid: int, request: dict[str, object],
                   timeout: float) -> dict[str, Any]:
    """One request, one answer, on a connection to the Pirate's socket."""
    payload = encode(request)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(path, limit=MAX_FRAME), timeout)
    except (FileNotFoundError, ConnectionRefusedError):
        raise Refusal("unavailable", "the Pirate is not listening") from None
    except asyncio.TimeoutError:
        raise Refusal("unavailable", f"the Pirate accepted no connection within {timeout:g} s; "
                                     "nothing was sent") from None
    try:
        if peer_uid(writer) != uid:
            raise PermissionError("the Pirate socket is served by another account")
        writer.write(payload)
        try:
            await asyncio.wait_for(writer.drain(), timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"the Pirate did not take the request within {timeout:g} s; "
                               "it may have received part of it") from None
        try:
            line = await asyncio.wait_for(reader.readline(), timeout)
        except ValueError:  # StreamReader's limit: the frame is too long
            raise ValueError(
                f"the Pirate's answer exceeds the frame limit of {MAX_FRAME} bytes; "
                "the request may have run (the Pirate's transcript for this job_id "
                "says what it did)") from None
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"no answer from the Pirate within {timeout:g} s; the connection was "
                "closed, which stops the run on the Pirate's side") from None
        if not line.endswith(b"\n"):
            raise ConnectionError(
                "the Pirate closed the connection before answering; the request may "
                "have run in part (the Pirate's transcript for this job_id says why)")
        return answer(decode_object(line))
    finally:
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()


class Pirates:
    """Registered Pirate flavours, kept alive by the registry's heartbeat."""

    def __init__(self, registry: Registry | None = None) -> None:
        self.registry = registry or Registry()
        self.handlers: set[asyncio.Task[None]] = set()
        self.bound: tuple[Path, tuple[int, int]] | None = None

    def register(self, uid: int, request: dict[str, object]) -> dict[str, Any]:
        if (set(request) != {"operation", "kind", "flavour", "socket_path"}
                or request["operation"] != "register" or request["kind"] != "pirate"):
            raise Refusal("invalid_request",
                          "a Pirate registers with operation register, kind pirate, "
                          "flavour and socket_path")
        flavour = request["flavour"]
        if not isinstance(flavour, str) or not FLAVOUR.fullmatch(flavour):
            raise Refusal("invalid_request", "flavour must match [a-z0-9][a-z0-9-]{0,62}")
        try:
            pirate = pwd.getpwuid(uid).pw_name
        except KeyError:
            raise Refusal("forbidden", "the registering uid has no account") from None
        path = request["socket_path"]
        if (not isinstance(path, str) or "\0" in path or not Path(path).is_absolute()
                or ".." in Path(path).parts):
            raise Refusal("invalid_request", "socket_path must be an absolute path")
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            raise Refusal("invalid_request", "there is no socket at socket_path") from None
        if not stat.S_ISSOCK(info.st_mode):
            raise Refusal("invalid_request", "socket_path is not a socket")
        if info.st_uid != uid:
            raise Refusal("forbidden", "socket_path is not owned by the registering account")
        identity = f"{pirate}/{flavour}"
        if self.registry.get(identity) is None and len(self.registry.list()) >= MAX_FLAVOURS:
            raise Refusal("limit", "the Pirate registry is full")

        async def ping() -> bool:
            # A refusal (an inactive flavour, ...) declines the ping. A Pirate
            # that is not listening raises, so last_miss says why.
            try:
                await exchange(path, uid, {"operation": "ping", "flavour": flavour},
                               REQUEST_TIMEOUT)
            except Refusal as refusal:
                if refusal.code == "unavailable":
                    raise
                return False
            return True

        return self.registry.register(identity, ping, details={
            "pirate": pirate, "flavour": flavour, "socket_path": path, "uid": uid})

    def list(self) -> dict[str, object]:
        return {"pirates": [{
            "pirate": entry.details["pirate"], "flavour": entry.details["flavour"],
            "available": entry.available, "misses": entry.misses,
            "last_miss": entry.last_miss,
        } for entry in self.registry.list()]}

    def _entry(self, pirate: object, flavour: object) -> Entry:
        if not isinstance(pirate, str) or not isinstance(flavour, str):
            raise Refusal("invalid_request", "pirate and flavour must be names")
        entry = self.registry.get(f"{pirate}/{flavour}")
        if entry is None:
            raise Refusal("not_found", "no such Pirate flavour is registered with this Pond")
        return entry

    async def describe(self, pirate: object, flavour: object) -> dict[str, Any]:
        entry = self._entry(pirate, flavour)
        return await exchange(entry.details["socket_path"], entry.details["uid"],
                              {"operation": "describe", "flavour": flavour}, REQUEST_TIMEOUT)

    def prepare_run(self, payload: object) -> dict[str, Any]:
        """Check a job's `pirate` payload before the job exists."""
        if not isinstance(payload, dict) or set(payload) != RUN_KEYS:
            raise Refusal("invalid_request",
                          "pirate needs exactly pirate, flavour, assignment and timeout_seconds")
        assignment, timeout = payload["assignment"], payload["timeout_seconds"]
        if not isinstance(assignment, str) or not assignment.strip():
            raise Refusal("invalid_request", "assignment must be nonempty text")
        if timeout is not None and (type(timeout) not in (int, float)
                                    or not math.isfinite(timeout)
                                    or not 0.1 <= timeout <= MAX_JOB_SECONDS):
            raise Refusal("invalid_request",
                          f"timeout_seconds must be 0.1 to {MAX_JOB_SECONDS:g}, or null")
        entry = self._entry(payload["pirate"], payload["flavour"])
        if not entry.available:
            raise Refusal("unavailable", f"{entry.identity} missed its last ping: {entry.last_miss}")
        target = {**payload, "socket_path": entry.details["socket_path"], "uid": entry.details["uid"]}
        self._run_request(target, "0" * 32)  # refuse an oversized assignment now
        return target

    @staticmethod
    def _run_request(target: dict[str, Any], job_id: str) -> dict[str, object]:
        request: dict[str, object] = {
            "operation": "run", "flavour": target["flavour"], "job_id": job_id,
            "assignment": target["assignment"], "timeout_seconds": target["timeout_seconds"]}
        encode(request)
        return request

    async def run(self, target: dict[str, Any], job_id: str) -> dict[str, Any]:
        """Hold the `run` connection until the Pirate's single answer."""
        timeout = target["timeout_seconds"]
        try:
            return await exchange(target["socket_path"], target["uid"],
                                  self._run_request(target, job_id),
                                  (MAX_JOB_SECONDS if timeout is None else timeout)
                                  + RUN_MARGIN_SECONDS)
        except asyncio.CancelledError:
            # Disconnect requests cancellation; this protocol has no cleanup ack.
            raise RuntimeError(
                "Pirate stop requested by disconnect; remote cleanup unconfirmed; "
                "inspect the Pirate before retrying the assignment"
            ) from None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One registration per connection on register.sock."""
        task = asyncio.current_task()
        assert task is not None
        if len(self.handlers) >= MAX_REGISTRATIONS:
            writer.close()
            await writer.wait_closed()
            return
        self.handlers.add(task)
        try:
            async def registration() -> None:
                uid = peer_uid(writer)
                authorize_uid(uid)  # Reject before reading a frame.
                line = await reader.readline()
                if not line or len(line) > MAX_FRAME or not line.endswith(b"\n"):
                    raise ValueError("empty, incomplete or excessive registration frame")
                result = self.register(uid, decode_object(line))
                writer.write(encode({"ok": True, "result": result}))
                await writer.drain()
            await asyncio.wait_for(registration(), REQUEST_TIMEOUT)
        except Refusal as refusal:
            with suppress(ConnectionError, asyncio.TimeoutError):
                writer.write(encode({"ok": False, "error": {"code": refusal.code,
                                                            "message": refusal.message[:256]}}))
                await asyncio.wait_for(writer.drain(), 1)
        except (OSError, ValueError, RuntimeError, RecursionError, asyncio.TimeoutError) as error:
            code = ("forbidden" if isinstance(error, PermissionError)
                    else "invalid_request" if isinstance(error, ValueError) else "unavailable")
            with suppress(ConnectionError, asyncio.TimeoutError):
                writer.write(encode({"ok": False, "error": {"code": code, "message": str(error)[:256]}}))
                await asyncio.wait_for(writer.drain(), 1)
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            self.handlers.discard(task)

    async def listen(self, path: Path) -> asyncio.AbstractServer:
        # Root provisions the parent setgid directory. The worker is not itself
        # a member of the registrar group and must never chown/chgrp this tree.
        gid = grp.getgrnam(REGISTRATION_GROUP).gr_gid
        info = path.parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_gid != gid or stat.S_IMODE(info.st_mode) != 0o2750):
            raise PermissionError(f"registration directory must be worker-owned, group {REGISTRATION_GROUP}, mode 2750")
        for parent in path.parent.parents:
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise PermissionError("unsafe registration directory ancestry")
        clear_stale_socket(path)
        server = await asyncio.start_unix_server(self.handle, path, limit=MAX_FRAME)
        self.bound = (path, bound_identity(path))
        try:
            os.chmod(path, 0o660)
            if path.stat().st_gid != gid:
                raise PermissionError("registration socket did not inherit service group")
        except BaseException:
            server.close()
            await server.wait_closed()
            raise
        return server

    async def close(self) -> None:
        for task in tuple(self.handlers):
            task.cancel()
        await asyncio.gather(*tuple(self.handlers), return_exceptions=True)
        if self.bound is not None:
            release_socket(*self.bound)
            self.bound = None
