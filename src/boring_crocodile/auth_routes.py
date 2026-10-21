"""The approval page (from the Common issuer) and the service-authority routes; tools stay separate."""
from __future__ import annotations
import asyncio
import logging
from html import escape
from typing import cast
from urllib.parse import parse_qs
from boring_registry import PING_INTERVAL_SECONDS
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import Response, JSONResponse
from .http_bodies import BodyRefused, bounded_body
from .settings import Settings
from boring_service_kit.issuer import ConsentDetails
from boring_service_kit.issuer.web import approval_route
from .sqlite_oauth_provider import SQLiteOAuthProvider
from .worker_client import WorkerClient

logger = logging.getLogger("boring_crocodile.control_plane")

def service_refusal(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers={"WWW-Authenticate": 'Basic realm="services"'} if status == 401 else None)


def register_auth_routes(mcp: MCPServer, settings: Settings,
                         provider: SQLiteOAuthProvider, workers: WorkerClient) -> None:
    def pond_selector(consent: ConsentDetails) -> str:
        # Every resource is /mcp/<pond>, which locks its Pond: there is no choice to offer.
        if consent.target is None:
            raise RuntimeError("authorization request without a Pond: every resource names one")
        return f"<p>Pond: <strong>{escape(settings.ponds[consent.target].display_name)}</strong></p>"

    approve = approval_route(
        provider, "Boring Crocodile",
        "Authorize this client to use this Pond's files and configured tools, "
        "including commands when enabled.", pond_selector)

    async def register_service(request: Request) -> Response:
        """A service announces itself; every granted Pond's worker starts pinging it."""
        granted = await asyncio.to_thread(provider.authenticate_service, request.headers.get("authorization"))
        if granted is None:
            return service_refusal(401, "forbidden", "unknown service credentials")
        try:
            body = await bounded_body(request, 1024)
        except BodyRefused as error:
            return service_refusal(error.status, error.code, error.message)
        if body:
            return service_refusal(400, "invalid_request", "registration takes an empty body")
        name, url = str(granted["name"]), str(granted["url"])
        ping_timeout = float(cast(float, granted["ping_timeout"]))
        catalog = settings.ponds
        ponds = [pond for pond in cast(list[str], granted["ponds"]) if pond in catalog]
        for pond in cast(list[str], granted["ponds"]):
            if pond not in catalog and pond not in {"pirate/" + name for name in settings.pirate_service_callers}:
                logger.warning("service %s is granted to Pond %s, which is not configured", name, pond)
        answers = await asyncio.gather(*(
            workers.service_register(catalog[pond].environment, name, url, ping_timeout) for pond in ponds),
            return_exceptions=True)
        for pond, answer in zip(ponds, answers):
            if isinstance(answer, BaseException):
                logger.warning("service %s registration did not reach Pond %s: %s", name, pond, answer)
        # Every granted Pond is a source, also one whose worker did not answer:
        # the service's watchdog then registers again when that one stays silent.
        return JSONResponse({"ping_interval_seconds": PING_INTERVAL_SECONDS, "sources": ponds})

    async def introspect(request: Request) -> Response:
        """RFC 7662, for services checking the tokens our workers present."""
        granted = await asyncio.to_thread(provider.authenticate_service, request.headers.get("authorization"))
        if granted is None:
            return service_refusal(401, "forbidden", "unknown service credentials")
        try:
            body = await bounded_body(request, 16384)
        except BodyRefused as error:
            return service_refusal(error.status, error.code, error.message)
        token = parse_qs(body.decode("utf-8", errors="replace")).get("token", [""])
        if len(token) != 1 or not token[0]:
            return service_refusal(400, "invalid_request", "introspection needs one form field token")
        return JSONResponse(await asyncio.to_thread(provider.introspect, granted, token[0]))

    # MCP 2.2.0 leaves the decorator factory untyped. Register separately so
    # the approval handler retains its checked Request -> Response signature.
    mcp.custom_route("/approve", methods=["GET", "POST"])(approve)
    mcp.custom_route("/services/register", methods=["POST"])(register_service)
    mcp.custom_route("/introspect", methods=["POST"])(introspect)
