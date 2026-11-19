"""Pirates: guests that register themselves, one registry entry per flavour.

A Pirate is a Unix account. It registers each active flavour on this Pond's
register.sock; the kernel proves the account (SO_PEERCRED), the flavour is its
own choice. The registry pings every flavour. A job runs one assignment by
holding a single `run` connection open until the Pirate answers; closing it
stops the run on the Pirate's side.

Registration socket: `/run/boring-crocodile/guests/<environment>/register.sock`,
group `pond-pirates`; membership permits a connection, while the description's
per-Pond roster and supported version range decide admission. Services register
with the control plane over HTTP, see services.py.

Worker-to-Pirate and registration protocols: success has result;
errors have a flat outcome.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from collections.abc import Mapping
import grp
import json
import math
import logging
import os
from pathlib import Path
import pwd
import re
import stat
from typing import Any, cast

from boring_registry import Entry, Registry
from boring_deploy import Failure as VersionError, satisfies
from .pirate_compatibility import PIRATES_REQUIRES
from .settings import PirateCaller

from boring_worker_kit.deadlines import REPLY_MARGIN_SECONDS
from boring_worker_kit.host_limits import TASK_OVERHEAD_SECONDS
from boring_worker_kit.framing import peer_uid
from boring_worker_kit.validation import MAX_JOB_SECONDS
from .outcomes import Refused, Failed, outcome_data
from boring_worker_kit.outcomes import failure_data
from .site_layout import (REGISTRATION_GROUP as REGISTRATION_GROUP,
                          REGISTRATION_ROOT as REGISTRATION_ROOT,
                          registration_path as registration_path)
from .unix_sockets import bound_identity, clear_stale_socket, release_socket
from boring_worker_kit.protocol_json import decode_object

logger = logging.getLogger(__name__)

FLAVOUR = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
# One frame (a JSON line) in either direction, the same value as the Pirate's
# boring_pirates.limits.MAX_FRAME: what one side may write, the other reads.
MAX_FRAME = 1024 * 1024
MAX_FLAVOURS = 64
MAX_REGISTRATIONS = 16
REQUEST_TIMEOUT = 5.0
# The Pirate enforces the assignment's own timeout (null: its default). The
# worker only stops holding the connection RUN_MARGIN_SECONDS later, for a
# Pirate that never answers; with null it assumes the longest job, MAX_JOB_SECONDS.
# At its deadline the Pirate cancels a running tool, whose command may still use
# the executor's cleanup reserve, then writes its transcript and answers: so the
# margin is that reserve plus the reply margin, not the reply margin alone.
RUN_MARGIN_SECONDS = TASK_OVERHEAD_SECONDS + REPLY_MARGIN_SECONDS
RUN_KEYS = {"pirate", "flavour", "assignment", "timeout_seconds"}
# Optional per-call overrides of a flavour (Pirates PF1), passed through unchanged:
# the Pirate checks them against the flavour (allow_overrides, its tools, its caps).
MAX_OVERRIDES_BYTES = 64 * 1024


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
        raise Refused("limit", "Pirate request exceeds the frame limit")
    return data


def answer(response: dict[str, Any]) -> dict[str, Any]:
    """Accept a result or a flat outcome; reject unexpected replies."""
    if response.get("ok") is True and set(response) == {"ok", "result"}:
        if isinstance(response["result"], dict):
            return response["result"]
    elif response.get("ok") is False:
        detail: object = None
        if set(response) == {"ok", "outcome"}:
            detail = response["outcome"]
        if isinstance(detail, dict) and detail.get("status") in ("refused", "failed"):
            try:
                data = outcome_data(detail, detail["status"])
            except (TypeError, ValueError):
                pass
            else:
                extra = {key: value for key, value in data.items()
                         if key not in ("status", "code", "message")}
                cls = Refused if data["status"] == "refused" else Failed
                raise cls(cast(str, data["code"]), cast(str, data["message"]), **extra)
    raise Failed("bad_reply", "invalid Pirate response envelope",
                 component="pirate", effects="unknown")


async def exchange(path: str, uid: int, request: dict[str, object],
                   timeout: float) -> dict[str, Any]:
    """One request, one answer, on a connection to the Pirate's socket, all within
    `timeout` seconds (one deadline for connecting, sending and the answer)."""
    if timeout <= 0:
        raise Refused("not_admitted", "no time left to ask the Pirate; nothing was sent")
    payload = encode(request)
    end = asyncio.get_running_loop().time() + timeout

    def remaining() -> float:
        return max(0.0, end - asyncio.get_running_loop().time())
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(path, limit=MAX_FRAME), remaining())
    except (FileNotFoundError, ConnectionRefusedError):
        raise Refused("unavailable", "the Pirate is not listening") from None
    except asyncio.TimeoutError:
        raise Refused("unavailable", f"the Pirate accepted no connection within {timeout:g} s; "
                                     "nothing was sent") from None
    try:
        actual_uid = peer_uid(writer)
        if actual_uid != uid:
            logger.warning("Pirate socket peer UID mismatch: expected %s, got %s", uid, actual_uid)
            raise Refused("forbidden", "the Pirate socket is served by another account")
        writer.write(payload)
        try:
            await asyncio.wait_for(writer.drain(), remaining())
        except asyncio.TimeoutError:
            raise Failed("timeout", f"the Pirate did not take the request within {timeout:g} s; "
                         "it may have received part of it", component="pirate", effects="unknown") from None
        try:
            line = await asyncio.wait_for(reader.readline(), remaining())
        except ValueError:  # StreamReader's limit: the frame is too long
            raise Failed("bad_reply",
                f"the Pirate's answer exceeds the frame limit of {MAX_FRAME} bytes; "
                "the request may have run (the Pirate's transcript for this job_id "
                "says what it did)", component="pirate", effects="unknown") from None
        except asyncio.TimeoutError:
            raise Failed("timeout",
                f"no answer from the Pirate within {timeout:g} s; the connection was "
                "closed, which stops the run on the Pirate's side", component="pirate", effects="unknown") from None
        if not line.endswith(b"\n"):
            raise Failed("unavailable",
                "the Pirate closed the connection before answering; the request may "
                "have run in part (the Pirate's transcript for this job_id says why)",
                component="pirate", effects="unknown")
        try:
            response = decode_object(line)
        except (ValueError, RecursionError):
            raise Failed("bad_reply", "invalid Pirate response JSON",
                         component="pirate", effects="unknown") from None
        return answer(response)
    except ConnectionError as error:
        raise Failed("unavailable", f"Pirate connection failed: {error}"[:512],
                     component="pirate", effects="unknown") from error
    finally:
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()


class Pirates:
    """Registered Pirate flavours, kept alive by the registry's heartbeat."""

    def __init__(self, registry: Registry | None = None, *, pond: str = "",
                 callers: Mapping[str, PirateCaller] | None = None) -> None:
        self.registry = registry or Registry()
        self.pond = pond
        self.admitted = {caller.uid: name for name, caller in (callers or {}).items()
                         if pond in caller.ponds}
        self.handlers: set[asyncio.Task[None]] = set()
        self.bound: tuple[Path, tuple[int, int, int]] | None = None

    def admitted_pirate(self, uid: int) -> str:
        try:
            return self.admitted[uid]
        except KeyError:
            raise Refused("forbidden", "Pirate account is not admitted for Pond " + (self.pond or "(unspecified)"),
                          component="registration", pond=self.pond) from None

    def register(self, uid: int, request: dict[str, object]) -> dict[str, Any]:
        pirate = self.admitted_pirate(uid)
        if (set(request) != {"operation", "kind", "flavour", "socket_path", "version"}
                or request["operation"] != "register" or request["kind"] != "pirate"):
            raise Refused("invalid_request",
                          "a Pirate registers with operation register, kind pirate, "
                          "flavour, socket_path and version")
        version = request["version"]
        if not isinstance(version, str) or not version or len(version) > 64:
            raise Refused("invalid_request", "version must be a release version string of at most 64 characters")
        try:
            compatible = satisfies(version, PIRATES_REQUIRES)
        except VersionError:
            raise Refused("invalid_request", "version must be a valid release version") from None
        if not compatible:
            raise Refused("unsupported", f"Pirate version {version} is incompatible; Crocodile requires {PIRATES_REQUIRES}",
                          component="registration", requirement=PIRATES_REQUIRES, actual_version=version)
        flavour = request["flavour"]
        if not isinstance(flavour, str) or not FLAVOUR.fullmatch(flavour):
            raise Refused("invalid_request", "flavour must match [a-z0-9][a-z0-9-]{0,62}")
        try:
            pwd.getpwuid(uid)  # The configured UID must still name an account.
        except KeyError:
            raise Refused("forbidden", "the registering uid has no account") from None
        path = request["socket_path"]
        if (not isinstance(path, str) or "\0" in path or not Path(path).is_absolute()
                or ".." in Path(path).parts):
            raise Refused("invalid_request", "socket_path must be an absolute path")
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            raise Refused("invalid_request", "there is no socket at socket_path") from None
        if not stat.S_ISSOCK(info.st_mode):
            raise Refused("invalid_request", "socket_path is not a socket")
        if info.st_uid != uid:
            raise Refused("forbidden", "socket_path is not owned by the registering account")
        identity = f"{pirate}/{flavour}"
        if self.registry.get(identity) is None and len(self.registry.list()) >= MAX_FLAVOURS:
            raise Refused("limit", "the Pirate registry is full")

        async def ping() -> bool:
            # A refusal (an inactive flavour, ...) declines the ping. A Pirate
            # that is not listening raises, so last_miss says why.
            try:
                await exchange(path, uid, {"operation": "ping", "flavour": flavour},
                               REQUEST_TIMEOUT)
            except Refused as refusal:
                if refusal.code == "unavailable":
                    raise
                return False
            return True

        ack = self.registry.register(identity, ping, details={
            "pirate": pirate, "flavour": flavour, "socket_path": path, "uid": uid})
        return {**ack, "accepted": True, "pond": self.pond, "pirate": pirate}

    def list(self) -> dict[str, object]:
        return {"pirates": [{
            "pirate": entry.details["pirate"], "flavour": entry.details["flavour"],
            "available": entry.available, "misses": entry.misses,
            "last_miss": entry.last_miss,
        } for entry in self.registry.list()]}

    def _entry(self, pirate: object, flavour: object) -> Entry:
        if not isinstance(pirate, str) or not isinstance(flavour, str):
            raise Refused("invalid_request", "pirate and flavour must be names")
        entry = self.registry.get(f"{pirate}/{flavour}")
        if entry is None:
            raise Refused("not_found", "no such Pirate flavour is registered with this Pond")
        return entry

    async def describe(self, pirate: object, flavour: object,
                       timeout: float = REQUEST_TIMEOUT) -> dict[str, Any]:
        """The flavour's own description, within `timeout` (the carried budget, at most
        REQUEST_TIMEOUT) for the whole exchange."""
        entry = self._entry(pirate, flavour)
        return await exchange(entry.details["socket_path"], entry.details["uid"],
                              {"operation": "describe", "flavour": flavour}, min(timeout, REQUEST_TIMEOUT))

    def prepare_run(self, payload: object) -> dict[str, Any]:
        """Check a job's `pirate` payload before the job exists."""
        if not isinstance(payload, dict) or not RUN_KEYS <= set(payload) <= RUN_KEYS | {"overrides"}:
            raise Refused("invalid_request",
                          "pirate needs exactly pirate, flavour, assignment and timeout_seconds "
                          "(and optionally overrides)")
        overrides = payload.get("overrides")
        if overrides is not None:
            if not isinstance(overrides, dict):
                raise Refused("invalid_request", "overrides must be an object or null")
            if len(json.dumps(overrides, ensure_ascii=False).encode()) > MAX_OVERRIDES_BYTES:
                raise Refused("limit", f"overrides exceed {MAX_OVERRIDES_BYTES} bytes")
        assignment, timeout = payload["assignment"], payload["timeout_seconds"]
        if not isinstance(assignment, str) or not assignment.strip():
            raise Refused("invalid_request", "assignment must be nonempty text")
        if timeout is not None and (type(timeout) not in (int, float)
                                    or not math.isfinite(timeout)
                                    or not 0.1 <= timeout <= MAX_JOB_SECONDS):
            raise Refused("invalid_request",
                          f"timeout_seconds must be 0.1 to {MAX_JOB_SECONDS:g}, or null")
        entry = self._entry(payload["pirate"], payload["flavour"])
        if not entry.available:
            raise Refused("unavailable", f"{entry.identity} missed its last ping: {entry.last_miss}")
        target = {**payload, "socket_path": entry.details["socket_path"], "uid": entry.details["uid"]}
        self._run_request(target, "0" * 32)  # refuse an oversized assignment now
        return target

    @staticmethod
    def _run_request(target: dict[str, Any], job_id: str) -> dict[str, object]:
        request: dict[str, object] = {
            "operation": "run", "flavour": target["flavour"], "job_id": job_id,
            "assignment": target["assignment"], "timeout_seconds": target["timeout_seconds"]}
        if target.get("overrides") is not None:
            request["overrides"] = target["overrides"]
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
            raise Failed("cleanup_unconfirmed",
                "Pirate stop requested by disconnect; remote cleanup unconfirmed; "
                "inspect the Pirate before retrying the assignment", component="pirate", effects="unknown"
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
                authorize_uid(uid)  # Socket group membership remains a transport gate.
                self.admitted_pirate(uid)  # Reject unadmitted accounts before reading a frame.
                try:
                    line = await reader.readline()
                    if not line or len(line) > MAX_FRAME or not line.endswith(b"\n"):
                        raise ValueError("empty, incomplete or excessive registration frame")
                    request = decode_object(line)
                except (ValueError, RecursionError):
                    raise Refused("invalid_request", "invalid registration frame") from None
                result = self.register(uid, request)
                writer.write(encode({"ok": True, "result": result}))
                await writer.drain()
            await asyncio.wait_for(registration(), REQUEST_TIMEOUT)
        except (Refused, PermissionError) as error:
            refusal = (error if isinstance(error, Refused) else
                       Refused("forbidden", str(error), component="registration"))
            logger.warning("Pirate registration refused: %s", refusal.as_data())
            with suppress(ConnectionError, asyncio.TimeoutError):
                writer.write(encode({"ok": False, "outcome": refusal.as_data()}))
                await asyncio.wait_for(writer.drain(), 1)
        except Exception as error:
            outcome = failure_data(error, component="registration", fallback_code="registration_failed", logger=logger)
            with suppress(ConnectionError, asyncio.TimeoutError):
                writer.write(encode({"ok": False, "outcome": outcome}))
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
            self.bound = (path, bound_identity(path))  # after chmod: it changes the change time
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
