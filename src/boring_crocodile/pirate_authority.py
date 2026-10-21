"""Local Pirate admission into the existing service grant/token authority."""
from __future__ import annotations
import asyncio
import os
from typing import Any, cast
from collections.abc import AsyncIterator
from .settings import Settings
from .sqlite_oauth_provider import SQLiteOAuthProvider
from .unix_sockets import bound_identity, clear_stale_socket, release_socket
from contextlib import asynccontextmanager, suppress
from boring_worker_kit.local_authority import (MAX_AUTHORITY_FRAME, peer_uid, protected_path, encode)
from boring_worker_kit.protocol_json import decode_object
from boring_worker_kit.outcomes import Refusal

class PirateAuthority:
    def __init__(self, settings: Settings, provider: SQLiteOAuthProvider) -> None:
        self.path = settings.pirate_service_socket
        self.callers = {uid: "pirate/" + name for name, uid in settings.pirate_service_callers.items()}
        self.provider = provider
        self.active: set[asyncio.Task[None]] = set()

    def dispatch(self, uid: int, request: dict[str, Any]) -> dict[str, Any]:
        caller = self.callers.get(uid)
        if caller is None:
            raise Refusal("forbidden", "Unix UID is not a configured service caller")
        if request == {"operation": "list"}:
            grants = self.provider.service_inventory()
            return {"caller": caller, "services": [self.public(row) for row in grants if caller in cast(list[str], row["ponds"])]}
        if set(request) != {"operation", "service"} or request["operation"] != "token" or not isinstance(request["service"], str):
            raise Refusal("invalid_request", "authority operations are list or token(service)")
        grant = self.provider.service(request["service"])
        if grant is None or caller not in cast(list[str], grant["ponds"]):
            raise Refusal("forbidden", "caller has no grant for this service")
        token = self.provider.mint_service_token(caller, grant, 60)
        return {"caller": caller, "grant": self.public(grant), "token": token.token, "expires_at": token.expires_at}

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
                    raise Refusal("forbidden", "Unix UID is not a configured service caller")
                request = decode_object(await asyncio.wait_for(reader.readline(), 2.0))
                result = await asyncio.to_thread(self.dispatch, uid, request)
                response = {"ok": True, "result": result}
            except Refusal as error:
                response = {"ok": False, "error": error.as_data()}
            except (ValueError, asyncio.TimeoutError):
                response = {"ok": False, "error": {"code": "invalid_request", "message": "invalid authority request"}}
            except Exception:
                response = {"ok": False, "failure": "authority operation failed"}
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
        bound = bound_identity(self.path)
        os.chmod(self.path, 0o666)  # admission is SO_PEERCRED, not caller-supplied identity
        try:
            async with server:
                yield
        finally:
            tasks = list(self.active)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            release_socket(self.path, bound)
