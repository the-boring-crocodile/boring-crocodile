"""The controller vouches for Pirate/Pond identities and existing service grants."""
from __future__ import annotations
import asyncio
import os
import stat
from typing import Any, cast
from collections.abc import AsyncIterator
from .settings import Settings
from .site_layout import registration_path
from .site_ports import service_port_allowed
from .sqlite_oauth_provider import SQLiteOAuthProvider
from .unix_sockets import bound_identity, clear_stale_socket, release_socket
from contextlib import asynccontextmanager, suppress
from boring_worker_kit.local_authority import (MAX_AUTHORITY_FRAME, peer_uid, protected_path, encode)
from boring_worker_kit.protocol_json import decode_object
from .outcomes import Refused
from boring_worker_kit.outcomes import failure_data
import logging

class PirateAuthority:
    def __init__(self, settings: Settings, provider: SQLiteOAuthProvider) -> None:
        self.path = settings.pirate_service_socket
        self.settings = settings
        self.callers = {uid: "pirate/" + name for name, uid in settings.pirate_service_callers.items()}
        self.provider = provider
        self.active: set[asyncio.Task[None]] = set()

    def dispatch(self, uid: int, request: dict[str, Any]) -> dict[str, Any]:
        caller = self.callers.get(uid)
        if caller is None:
            raise Refused("forbidden", "Unix UID is not a configured service caller")
        if set(request) == {"operation", "pond"} and request["operation"] == "registration":
            if not isinstance(request["pond"], str):
                raise Refused("invalid_request", "registration needs a Pond name")
            name = request["pond"]
            admitted = self.settings.pirate_callers[caller.removeprefix("pirate/")]
            if name not in admitted.ponds or name not in self.settings.ponds:
                raise Refused("forbidden", "Pirate is not admitted for enabled Pond " + name)
            return self.registration(name, caller.removeprefix("pirate/"))
        if request == {"operation": "list"}:
            grants = self.provider.service_inventory()
            return {"caller": caller, "services": [
                self.public(row) for row in grants if self.granted(caller, row)
            ]}
        if set(request) != {"operation", "service"} or request["operation"] != "token" or not isinstance(request["service"], str):
            raise Refused("invalid_request", "authority operations are list, token(service) or registration(pond)")
        grant = self.provider.service(request["service"])
        if grant is None or not self.granted(caller, grant):
            raise Refused("forbidden", "caller has no grant for this service")
        token = self.provider.mint_service_token(caller, grant, 60)
        return {"caller": caller, "grant": self.public(grant), "token": token.token, "expires_at": token.expires_at}

    def granted(self, caller: str, grant: dict[str, object]) -> bool:
        return (caller in cast(dict[str, str], grant["callers"])
                and service_port_allowed(str(grant["url"]), self.settings.protected_ports,
                                         self.settings.bind_port))

    def registration(self, pond: str, pirate: str) -> dict[str, Any]:
        """Root created the named namespace; the Pirate checks the live socket/peer."""
        # The controller need not traverse the worker-owned setgid directory.
        # Root controls its name/owner; the Pirate can inspect the socket inside it.
        environment = self.settings.ponds[pond].environment
        path = registration_path(environment)
        try:
            directory = path.parent.lstat()
            if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid <= 0
                    or stat.S_IMODE(directory.st_mode) != 0o2750):
                raise Refused("forbidden", "unsafe registration directory identity")
            for ancestor in path.parent.parents:
                info = ancestor.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                    raise Refused("forbidden", "registration namespace is not controlled by root")
        except OSError as error:
            raise Refused("unavailable", "registration namespace is unavailable") from error
        return {"pond": pond, "pirate": pirate, "uid": directory.st_uid, "socket_path": str(path)}

    @staticmethod
    def public(row: dict[str, object]) -> dict[str, object]:
        return {key: row[key] for key in ("name", "url", "ping_timeout")}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.active.add(task)
        try:
            try:
                uid = peer_uid(writer)
                # Refuse strangers before accepting any request bytes.
                if uid not in self.callers:
                    raise Refused("forbidden", "Unix UID is not a configured service caller")
                try:
                    request = decode_object(await asyncio.wait_for(reader.readline(), 2.0))
                except (ValueError, asyncio.TimeoutError):
                    raise Refused("invalid_request", "invalid authority request") from None
                result = await asyncio.to_thread(self.dispatch, uid, request)
                response = {"ok": True, "result": result}
            except Refused as error:
                response = {"ok": False, "outcome": error.as_data()}
            except Exception as error:
                response = {"ok": False, "outcome": failure_data(
                    error, component="authority", fallback_code="authority_failed",
                    logger=logging.getLogger(__name__))}
            writer.write(encode(response))
            await asyncio.wait_for(writer.drain(), 2.0)
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            self.active.discard(task)

    @asynccontextmanager
    async def serve(self) -> AsyncIterator[None]:
        if not self.callers:
            yield
            return
        protected_path(self.path, os.getuid())
        clear_stale_socket(self.path)
        server = await asyncio.start_unix_server(self.handle, path=self.path, limit=MAX_AUTHORITY_FRAME)
        os.chmod(self.path, 0o666)  # admission is SO_PEERCRED, not caller-supplied identity
        bound = bound_identity(self.path)  # after chmod: it changes the change time
        try:
            async with server:
                yield
        finally:
            tasks = list(self.active)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            release_socket(self.path, bound)
