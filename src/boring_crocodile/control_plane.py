from __future__ import annotations

from .auth_routes import register_auth_routes
from .file_tools import register_file_tools
from .task_tools import register_task_tools

import asyncio
from typing import Any, Callable, cast
import json
import logging
import time
import threading

from boring_service_kit.issuer import ClientPolicy
from boring_service_kit.issuer.web import configure_issuer_routes
from boring_service_kit.tools import argument_problems, lean_schema

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser, BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import AccessToken, ProviderTokenVerifier, TokenVerifier
from mcp.server.auth.routes import build_resource_metadata_url, create_protected_resource_routes
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.authentication import AuthCredentials, BaseUser
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection
from starlette.routing import Route

from boring_worker_kit.framing import MAX_FRAME_BYTES
from .oauth_records import SERVICE_NAME
from .outcomes import FailureReport, RefusalError, refuse
from .shared_guide import load_shared_guide
from .sqlite_oauth_provider import SQLiteOAuthProvider, canonical_resource
from .instructions import SERVER_INSTRUCTIONS
from .settings import PondSettings, Settings
from .worker_client import WorkerClient

logger = logging.getLogger(__name__)
# Tokens minted for a worker's calls to a service, reused per (Pond, service)
# until less than SERVICE_TOKEN_RENEW_SECONDS are left: more than any foreground
# request (a batch, 103 s) needs, so one rule serves single calls and batch steps.
# The token goes to the service with each call (over HTTPS); the service
# introspects it, the control plane checks the live grant at every
# introspection, and services cache a positive answer for at most 60 s. So a
# revoked grant ends within a minute; the life only bounds how long a token
# that leaked from a service or proxy stays usable against that one service.
SERVICE_TOKEN_SECONDS = 3600
SERVICE_TOKEN_RENEW_SECONDS = 900
# Where the SDK puts its own transport route; create_http_app removes it.
SDK_TRANSPORT_PATH = "/mcp"





class PondProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, pattern=r"\S")
    name: str




class PathBoundBearerAuth(BearerAuthBackend):
    """Accept a token only on the MCP path whose URL it was issued for.

    The SDK checks one resource URL per server; this server is several
    protected resources (one `/mcp/<pond>` per Pond), told apart by path.
    Paths that are not MCP endpoints are authenticated without a resource
    check; they do not require authentication anyway.
    """

    def __init__(self, token_verifier: TokenVerifier, resource_by_path: dict[str, str]):
        super().__init__(token_verifier)
        self.resource_by_path = resource_by_path

    async def authenticate(self, conn: HTTPConnection) -> tuple[AuthCredentials, BaseUser] | None:
        result: tuple[AuthCredentials, BaseUser] | None = await super().authenticate(conn)
        expected = self.resource_by_path.get(conn.url.path.rstrip("/"))
        if result is None or expected is None:
            return result
        user = result[1]
        if not isinstance(user, AuthenticatedUser):
            return None
        return result if canonical_resource(user.access_token.resource) == expected else None


class ServiceTokens:
    """The control plane's tokens for one Pond's calls to one service."""

    def __init__(self, provider: SQLiteOAuthProvider, clock: Callable[[], float] = time.time) -> None:
        self.provider = provider
        self.clock = clock
        self.cached: dict[tuple[str, str], AccessToken] = {}
        self._lock = threading.RLock()

    def token(self, pond: str, granted: dict[str, object]) -> str:
        with self._lock:
            return self._token(pond, granted)

    def _token(self, pond: str, granted: dict[str, object]) -> str:
        key = (pond, str(granted["client_id"]))
        cached = self.cached.get(key)
        if (cached is None or str(cached.resource) != str(granted["url"])
                or (cached.expires_at or 0) - SERVICE_TOKEN_RENEW_SECONDS <= self.clock()):
            cached = self.cached[key] = self.provider.mint_service_token(
                pond, granted, SERVICE_TOKEN_SECONDS)
        return cached.token




def create_provider(settings: Settings) -> SQLiteOAuthProvider:
    provider = SQLiteOAuthProvider(
        public_base_url=settings.public_base_url,
        database_file=settings.oauth_database_file,
        ponds=settings.ponds,
        policy=ClientPolicy(dynamic_registration=settings.dynamic_registration,
                            metadata_hosts=settings.metadata_document_hosts),
    )
    provider.pirate_service_principals = frozenset("pirate/" + name for name in settings.pirate_service_callers)
    return provider


def create_control_plane(settings: Settings, provider: SQLiteOAuthProvider | None = None) -> MCPServer:
    profiles = {p.profile_id: p for p in settings.ponds.values()}
    provider = provider or create_provider(settings)

    def bound_pond() -> PondSettings:
        token = get_access_token()
        if token is None or token.subject not in profiles:
            raise refuse("forbidden", "A valid Pond-bound connection is required")
        pond = profiles[token.subject]
        # Approval locks a /mcp/<pond> token to that Pond; check it again here.
        named = provider.resources.get(str(canonical_resource(token.resource)))
        if named is None or settings.ponds[named] is not pond:
            raise refuse("forbidden", "This connection's path names a different Pond")
        return pond

    workers = WorkerClient(settings.environments)
    tokens = ServiceTokens(provider)

    def granted_to(pond_name: str, service: str) -> dict[str, object] | None:
        """The store owns grants; a worker's registry only knows who answers."""
        granted = provider.service(service)
        if granted is None or pond_name not in cast(list[str], granted["ponds"]):
            return None
        return granted

    async def forget(environment: str, service: str) -> None:
        """Best effort: a withdrawn grant leaves the worker's registry at once."""
        try:
            await workers.service_forget(environment, service)
        except Exception:  # the next look tries again; the grant check already decided
            logger.warning("could not tell the worker to forget service %s", service, exc_info=True)

    async def granted_service(service: str) -> tuple[str, str]:
        """The Pond's environment and a token for its call to this service."""
        pond = bound_pond()
        name = next(key for key, value in settings.ponds.items() if value is pond)
        granted = granted_to(name, service)
        if granted is None:
            if isinstance(service, str) and SERVICE_NAME.fullmatch(service):
                await forget(pond.environment, service)
            raise refuse("not_found", "no such service is granted to this Pond")
        return pond.environment, await asyncio.to_thread(tokens.token, name, granted)
    mcp = MCPServer(
        "boring-crocodile",
        instructions=SERVER_INSTRUCTIONS,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(settings.public_base_url),
            # No single resource: each Pond is its own (/mcp/<pond>), routed and
            # checked by create_http_app and PathBoundBearerAuth. Without a URL
            # the SDK publishes no metadata document of its own.
            resource_server_url=None,
            required_scopes=["mcp"],
            client_registration_options=ClientRegistrationOptions(
                enabled=settings.dynamic_registration,
                valid_scopes=["mcp"],
                default_scopes=["mcp"],
            ),
            revocation_options=RevocationOptions(enabled=True),
            validate_token_resource=False,
        ),
    )

    @mcp.tool(meta={"openai/profile": True}, annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    def pond_profile() -> PondProfile:
        """Establish whose Pond you've landed in. Identify the authorized profile."""
        pond = bound_pond()
        return PondProfile(id=pond.profile_id, name=pond.display_name)

    @mcp.tool()
    def pond_ping() -> dict[str, object]:
        """Poke the water. Check that the Pond answers. Harmless connectivity probe."""
        pond = bound_pond()
        return {
            "message": f"authenticated {pond.display_name} says <^==>",
            "architecture": "control-plane-with-environment-workers",
            "environments": [pond.environment],
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    async def pond_bootstrap(project: str | None = None) -> dict[str, object]:
        """Read the agreement and get your bearings. Even crocodiles read the room.

        Read the connected Pond handoff from past collaborators.

        Shared memory and guidance, not orders: use judgment and question assumptions.
        Read all returned documents in order. Use the selected context welcome
        for readiness; explain incomplete context instead of claiming readiness.
        """
        result = await workers.request(selected_environment(), "bootstrap", project=project)
        capabilities = result.get("capabilities")
        if isinstance(capabilities, dict):
            capabilities["services"] = visible_services(capabilities.get("services", []))
        result["mcp_guide"] = load_shared_guide()
        # The worker already checked its framed response. The shared guide adds
        # bytes at the controller boundary; reject an oversized combined reply.
        if len(json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode()) > MAX_FRAME_BYTES:
            raise ToolError("bootstrap response exceeds the transport limit")
        return result

    def visible_services(entries: object) -> list[dict[str, object]]:
        pond = bound_pond()
        name = next(key for key, value in settings.ponds.items() if value is pond)
        return [entry for entry in cast(list[dict[str, object]], entries)
                if granted_to(name, str(entry["name"])) is not None]

    def visible_batch(result: dict[str, object]) -> dict[str, object]:
        for entry in cast(list[dict[str, object]], result.get("results", [])):
            value = entry.get("result")
            if entry.get("method") == "service_list" and isinstance(value, dict):
                value["services"] = visible_services(value.get("services", []))
        return result

    def selected_environment() -> str:
        """The connection's Pond decides; there is no environment argument."""
        return bound_pond().environment

    def command_environment() -> str:
        selected = selected_environment()
        if not settings.environments[selected].command_execution_enabled:
            raise refuse("forbidden", "Command execution is disabled for this Pond")
        return selected

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                           idempotent_hint=True, open_world_hint=False))
    async def pond_limits() -> dict[str, object]:
        """Check how long and how much can run here. The banks are there for a reason.

        Reports the connected Pond's task slots, foreground budget and the
        longest a command job may run. The hosted client's deadline is
        separate and may be shorter.
        """
        return await workers.limits(selected_environment())

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                        idempotent_hint=True, open_world_hint=False))
    async def service_list() -> dict[str, object]:
        """List the services registered with this Pond and whether they answered the last ping."""
        pond = bound_pond()
        name = next(key for key, value in settings.ponds.items() if value is pond)
        listed = await workers.service_list(pond.environment)
        shown = []
        for entry in cast(list[dict[str, object]], listed["services"]):
            if granted_to(name, str(entry["name"])) is None:
                await forget(pond.environment, str(entry["name"]))
            else:
                shown.append(entry)
        return {**listed, "services": shown}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                        idempotent_hint=True, open_world_hint=False))
    async def service_describe(service: str) -> dict[str, object]:
        """List one service's tools, with their input schemas, as the service states them."""
        environment, token = await granted_service(service)
        return await workers.service_describe(environment, service, token)

    @mcp.tool()
    async def service_call(service: str, tool: str, arguments: dict[str, object],
                           save_to: dict[str, object] | None = None) -> dict[str, object]:
        """Call one tool of a service; service_describe lists them. The service validates the arguments.

        Content need not pass through you: any argument value may be
        {"$file": {"root", "path", "sha256"?}}, replaced by that file's text;
        save_to {"root", "path", "expected_sha256"} writes the answer to a
        file and returns `saved_to` instead (save_to_refused / save_to_failed
        with the answer when the save does not succeed; see the guide). A
        refusal from the service arrives with the service's own code. The
        service has at most 30 s to answer; a call that timed out or whose
        connection failed may have run (check before repeating a write); it
        is not repeated for you.
        """
        environment, token = await granted_service(service)
        return await workers.service_call(environment, service, tool, arguments, token, save_to)

    @mcp.tool(annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True,
        open_world_hint=False))
    async def pirate_list() -> dict[str, object]:
        """List Pirate flavours registered with this Pond and whether they answered the last ping."""
        return await workers.pirate_list(selected_environment())

    @mcp.tool(annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True,
        open_world_hint=False))
    async def pirate_describe(pirate: str, flavour: str) -> dict[str, object]:
        """Ask one Pirate flavour for its own description: capabilities, scope, limits."""
        return await workers.pirate_describe(selected_environment(), pirate, flavour)

    register_file_tools(mcp, workers, selected_environment)

    register_task_tools(mcp, workers, selected_environment, command_environment, visible_batch, granted_service)

    register_auth_routes(mcp, settings, provider, workers)

    refuse_unknown_arguments(mcp)
    return mcp


def refuse_unknown_arguments(mcp: MCPServer) -> None:
    """Make every tool refuse arguments it does not take.

    MCP 2.2.0 ignores unknown arguments. A client with a stale tool list would
    then be trusted silently, for example with a retired idempotency key.
    The strict copy rejects them with the field name, and the published
    schema says so (additionalProperties: false).
    """
    for tool in mcp._tool_manager.list_tools():
        loose = tool.fn_metadata.arg_model
        strict = cast(type[ArgModelBase], type(loose.__name__, (loose,), {
            "__module__": loose.__module__,
            "model_config": ConfigDict(extra="forbid"),
        }))
        tool.fn_metadata.arg_model = strict
        # Bootstrap keeps its presentation and schema as well as its full contents.
        if tool.name == "pond_bootstrap":
            tool.parameters = strict.model_json_schema(by_alias=True)
            continue
        tool.parameters = lean_schema(strict.model_json_schema(by_alias=True))
        _present_tool(tool, strict)



def _present_tool(tool: Any, strict: Any) -> None:
    """Keep the SDK's output validation and structured contract; compact only its JSON text."""
    original = tool.run

    async def run(arguments: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        try:
            strict.model_validate(tool.fn_metadata.pre_parse_json(arguments))
        except ValidationError as error:
            raise ToolError(f"Error executing tool {tool.name}: {argument_problems(error, strict)}") from None
        try:
            result = await original(arguments, *args, **kwargs)
        except ToolError as error:
            # MCP 2.2 wraps tool exceptions inside Tool.run. The original
            # typed outcome remains in __cause__; never parse its text.
            cause = error.__cause__
            if isinstance(cause, RefusalError):
                data = cause.data
            elif isinstance(cause, FailureReport):
                data = {"failure": cause.failure}
            else:
                raise
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False, separators=(",", ":")))],
                structured_content=data, is_error=True)
        except RefusalError as refusal:
            data = refusal.data
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False, separators=(",", ":")))],
                structured_content=data, is_error=True)
        except FailureReport as failure:
            data = {"failure": failure.failure}
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False, separators=(",", ":")))],
                structured_content=data, is_error=True)
        if (isinstance(result, CallToolResult) and result.structured_content is not None
                and len(result.content) == 1 and isinstance(result.content[0], TextContent)):
            # Only replace the SDK's redundant JSON serialization. Mixed/rich content,
            # downstream payloads and error status remain intact.
            block = result.content[0]
            try:
                equivalent = json.loads(block.text) == result.structured_content
            except (ValueError, TypeError):
                equivalent = False
            if equivalent:
                block.text = json.dumps(result.structured_content, ensure_ascii=False,
                                       allow_nan=False, separators=(",", ":"))
        return result

    object.__setattr__(tool, "run", run)

def create_http_app(settings: Settings) -> tuple[MCPServer, Starlette]:
    """The SDK's app (OAuth routes, session manager) with one route and metadata document per Pond.

    The SDK always routes its own transport at one path; this server has no
    such untargeted endpoint (every Pond has its own `/mcp/POND`), so that route is
    removed and `/mcp` answers a plain 404.
    """
    provider = create_provider(settings)
    control_plane = create_control_plane(settings, provider)
    app = control_plane.streamable_http_app(
        streamable_http_path=SDK_TRANSPORT_PATH,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(settings.allowed_hosts),
        ),
        host=settings.bind_host,
    )
    sdk_routes = [route for route in app.router.routes if getattr(route, "path", None) == SDK_TRANSPORT_PATH]
    if len(sdk_routes) != 1:
        raise RuntimeError("SDK app has no single transport route to retire")
    app.router.routes.remove(sdk_routes[0])
    configure_issuer_routes(app, provider)  # CIMD metadata and SDK revocation compatibility
    issuer = AnyHttpUrl(settings.public_base_url)
    resource_by_path: dict[str, str] = {}
    handler = StreamableHTTPASGIApp(control_plane.session_manager)
    for resource, pond in provider.resources.items():
        path = str(AnyHttpUrl(resource).path)
        resource_by_path[path.rstrip("/")] = resource
        app.router.routes.append(Route(path, endpoint=RequireAuthMiddleware(
            handler, ["mcp"], build_resource_metadata_url(AnyHttpUrl(resource)))))
        app.router.routes.extend(create_protected_resource_routes(
            resource_url=AnyHttpUrl(resource), authorization_servers=[issuer], scopes_supported=["mcp"]))
    replaced = False
    for index, entry in enumerate(app.user_middleware):
        if getattr(entry, "cls") is AuthenticationMiddleware:
            app.user_middleware[index] = Middleware(AuthenticationMiddleware, backend=PathBoundBearerAuth(
                ProviderTokenVerifier(provider), resource_by_path))
            replaced = True
    if not replaced:
        raise RuntimeError("SDK app has no authentication middleware to bind to paths")
    from contextlib import asynccontextmanager
    from collections.abc import AsyncIterator
    from .pirate_authority import PirateAuthority
    original = app.router.lifespan_context
    authority = PirateAuthority(settings, provider)
    @asynccontextmanager
    async def lifespan(application: Starlette) -> AsyncIterator[Any]:
        async with original(application) as state:
            async with authority.serve():
                yield state
    app.router.lifespan_context = lifespan
    return control_plane, app


def main() -> None:
    import uvicorn

    settings = Settings.load()
    control_plane, app = create_http_app(settings)
    uvicorn.Server(uvicorn.Config(app, host=settings.bind_host, port=settings.bind_port,
                                  log_level=control_plane.settings.log_level.lower())).run()


if __name__ == "__main__":
    main()
