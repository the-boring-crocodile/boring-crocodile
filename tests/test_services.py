"""Services are MCP servers; the worker is their stateless MCP client.

Runs a real in-process MCP server (the SDK's MCPServer with a TokenVerifier)
on a temporary TCP port. Like a real service it exempts exactly the requests
whose Mcp-Method header is server/discover from authentication.
"""
import asyncio
import json
import logging
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from starlette.routing import Route
import uvicorn
from mcp import types

from boring_worker_kit import services
from boring_crocodile.outcomes import Refusal, RefusalError, FailureReport
from boring_worker_kit.services import ServiceFailure, Services
from boring_crocodile.settings import EnvironmentSettings
from boring_crocodile.worker import WorkerServer
from boring_crocodile.worker_client import WorkerClient


class ExampleService:
    """A small service: tools echo, refuse, broken and slow; token "good" only.

    Any other token is rejected with HTTP 401; "narrow" lacks the scope (403).
    """

    def __init__(self):
        self.tokens_seen = []
        self.requests = []  # (Mcp-Method, Authorization, body) per HTTP request
        outer = self

        class Verifier:
            async def verify_token(self, token):
                if token not in ("good", "narrow"):
                    return None
                return AccessToken(token=token, client_id="svc", scopes=["mcp"] if token == "good" else [],
                                   expires_at=int(time.time()) + 60, subject="pond")

        mcp = MCPServer("test-service", token_verifier=Verifier(), auth=AuthSettings(
            issuer_url="http://127.0.0.1", resource_server_url="http://127.0.0.1/mcp",
            required_scopes=["mcp"], validate_token_resource=False))

        @mcp.tool()
        def echo(text: str) -> dict[str, str]:
            outer.tokens_seen.append(get_access_token().token)
            return {"echo": text}

        @mcp.tool()
        def refuse() -> dict:
            raise ToolError(json.dumps({"code": "quota_exceeded", "message": "try tomorrow", "retry": 1}))

        @mcp.tool()
        def broken() -> dict:
            raise ToolError("the paint ran out")

        @mcp.tool()
        async def slow(seconds: float = 1) -> dict:
            await asyncio.sleep(seconds)
            return {}

        app = mcp.streamable_http_app(stateless_http=True, json_response=True)
        index = next(i for i, route in enumerate(app.router.routes) if getattr(route, "path", None) == "/mcp")
        guarded, open_ = app.router.routes[index].endpoint, StreamableHTTPASGIApp(mcp.session_manager)

        class Endpoint:
            async def __call__(self, scope, receive, send):
                headers = dict(scope["headers"])
                body = []

                async def recording():
                    message = await receive()
                    body.append(message.get("body", b""))
                    return message
                method = headers.get(b"mcp-method")
                outer.requests.append((method, headers.get(b"authorization"), body))
                target = open_ if method == b"server/discover" else guarded
                await target(scope, recording, send)

        app.router.routes[index] = Route("/mcp", endpoint=Endpoint())
        self.app = app

    async def start(self, port=0):
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level="critical", lifespan="on"))
        self.task = asyncio.create_task(self.server.serve(sockets=[sock]))
        while not self.server.started:
            if self.task.done():
                await self.task  # a startup failure, raised here
            await asyncio.sleep(0.01)

    async def stop(self):
        self.server.should_exit = True
        await self.task


class ServicesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.service = ExampleService()
        await self.service.start()
        self.services = Services("pond")
        self.addAsyncCleanup(self.services.close)

    async def asyncTearDown(self):
        if self.service.server.started and not self.service.task.done():
            await self.service.stop()

    def register(self):
        return self.services.register({"name": "canvas", "url": self.service.url})

    async def test_ping_is_server_discover_without_token_naming_the_pond(self):
        self.assertEqual(self.register(), {"ping_interval_seconds": 3.0})
        self.assertEqual(self.service.requests, [])  # registration itself calls nobody
        await self.services.registry.ping_all()
        self.assertEqual(self.services.list(), {"services": [{
            "name": "canvas", "available": True, "misses": 0, "last_miss": None}]})
        [(method, authorization, body)] = self.service.requests
        self.assertEqual((method, authorization), (b"server/discover", None))
        meta = json.loads(b"".join(body))["params"]["_meta"]
        self.assertEqual(meta["io.modelcontextprotocol/clientInfo"]["name"], "pond")

    async def test_describe_and_call_carry_the_token(self):
        self.register()
        described = await self.services.describe("canvas", "good")
        self.assertEqual(described["service"], "canvas")
        self.assertEqual({tool["name"] for tool in described["tools"]}, {"echo", "refuse", "broken", "slow"})
        echo = next(tool for tool in described["tools"] if tool["name"] == "echo")
        self.assertIn("text", echo["inputSchema"]["properties"])
        result = await self.services.call("canvas", "echo", {"text": "hi"}, "good")
        self.assertEqual(result["structuredContent"], {"echo": "hi"})
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertEqual(self.service.tokens_seen, ["good"])
        self.assertEqual({auth for _, auth, _ in self.service.requests}, {b"Bearer good"})
        self.assertEqual([method for method, _, _ in self.service.requests],
                         [b"tools/list", b"tools/call"])

    async def test_tool_errors_are_the_services_refusals(self):
        self.register()
        with self.assertRaises(Refusal) as caught:
            await self.services.call("canvas", "refuse", {}, "good")
        self.assertEqual(caught.exception.as_data(),
                         {"code": "quota_exceeded", "message": "try tomorrow", "retry": 1})
        with self.assertRaises(ServiceFailure) as caught:
            await self.services.call("canvas", "broken", {}, "good")
        self.assertEqual(caught.exception.data["code"], "tool_error")
        self.assertIn("the paint ran out", caught.exception.data["message"])
        with self.assertRaises(ServiceFailure) as caught:
            await self.services.call("canvas", "missing", {}, "good")
        self.assertEqual(caught.exception.data["code"], "tool_error")

    async def test_a_rejected_token_is_forbidden(self):
        self.register()
        for token in ("stolen", "narrow"):  # HTTP 401, 403
            for request in (self.services.describe("canvas", token),
                            self.services.call("canvas", "echo", {"text": "hi"}, token)):
                with self.subTest(token=token), self.assertRaises(Refusal) as caught:
                    await request
                self.assertEqual(caught.exception.as_data(), {
                    "code": "forbidden", "message": "the service rejected the call token"})
        self.assertEqual(self.service.tokens_seen, [])

    async def test_unknown_unavailable_and_malformed(self):
        with self.assertRaises(Refusal) as caught:
            await self.services.call("canvas", "echo", {}, "good")
        self.assertEqual(caught.exception.code, "not_found")
        for request in ({"name": "Canvas", "url": self.service.url}, {"name": "canvas", "url": "ftp://x/"},
                        {"name": "canvas"}):
            with self.subTest(request=request), self.assertRaises(Refusal) as caught:
                self.services.register(request)
            self.assertEqual(caught.exception.code, "invalid_request")
        self.register()
        for tool, arguments, token in (("", {}, "good"), ("echo", [], "good"), ("echo", {}, "")):
            with self.assertRaises(Refusal) as caught:
                await self.services.call("canvas", tool, arguments, token)
            self.assertEqual(caught.exception.code, "invalid_request")
        with patch.object(services, "MAX_SERVICES", 1):
            with self.assertRaises(Refusal) as caught:
                self.services.register({"name": "other", "url": self.service.url})
            self.assertEqual(caught.exception.code, "limit")
            self.register()  # re-registering a known service is no new entry

    async def test_service_gone_is_unavailable_after_a_missed_ping(self):
        self.register()
        await self.services.registry.ping_all()
        await self.service.stop()
        # Before the next ping: the connection is refused, nothing was sent.
        with self.assertRaises(Refusal) as caught:
            await self.services.call("canvas", "echo", {"text": "hi"}, "good")
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertIn("nothing was sent", caught.exception.message)
        await self.services.registry.ping_all()
        listed = self.services.list()["services"][0]
        self.assertEqual((listed["available"], listed["misses"]), (False, 1))
        self.assertIn("ConnectError", listed["last_miss"])
        with self.assertRaises(Refusal) as caught:
            await self.services.describe("canvas", "good")
        self.assertEqual(caught.exception.code, "unavailable")
        # A restarted service on the same URL: the next ping finds it again.
        self.service, port = ExampleService(), self.service.port
        await self.service.start(port)
        await self.services.registry.ping_all()
        self.assertTrue(self.services.list()["services"][0]["available"])

    async def test_call_can_exceed_old_five_second_limit(self):
        self.register()
        result = await self.services.call("canvas", "slow", {"seconds": 5.2}, "good")
        self.assertIsInstance(result, dict)
        self.assertEqual(services.CALL_TIMEOUT_SECONDS, 30.0)

    async def test_no_answer_in_time_may_have_run(self):
        self.register()
        with patch.object(services, "CALL_TIMEOUT_SECONDS", 0.2):
            with self.assertRaisesRegex(TimeoutError, "may have run"):
                await self.services.call("canvas", "slow", {}, "good")

    async def test_dropped_connection_may_have_run(self):
        async def drop(reader, writer):
            await reader.read(1)
            writer.close()
        server = await asyncio.start_server(drop, "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        self.services.register({"name": "dropper", "url": f"http://127.0.0.1:{port}/mcp"})
        with self.assertRaisesRegex(ConnectionError, "may have run"):
            await self.services.call("dropper", "echo", {}, "good")


class WorkerServiceTest(unittest.IsolatedAsyncioTestCase):
    """The control plane's requests, through the worker socket."""

    async def asyncSetUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.service = ExampleService()
        await self.service.start()
        self.addAsyncCleanup(self.service.stop)
        env = EnvironmentSettings("test", "Test", root / "control.sock", root, (root,), os.getuid(),
                                  1024, command_execution_enabled=False)
        self.worker = WorkerServer(env, pond="alpha")
        self.addAsyncCleanup(self.worker.services.close)
        self.control = await asyncio.start_unix_server(self.worker.handle, env.socket_path)
        self.addAsyncCleanup(self.control.wait_closed)
        self.addCleanup(self.control.close)
        self.client = WorkerClient({"test": env})

    async def test_forget_drops_the_entry_at_once(self):
        await self.client.service_register("test", "canvas", self.service.url, 1.0)
        self.assertEqual(await self.client.service_forget("test", "canvas"), {"forgotten": True})
        self.assertEqual(await self.client.service_list("test"), {"services": []})
        self.assertEqual(await self.client.service_forget("test", "canvas"), {"forgotten": False})
        with self.assertRaises(RefusalError) as caught:
            await self.client.service_forget("test", "../x")
        self.assertEqual(caught.exception.data["code"], "invalid_request")

    async def test_each_service_brings_its_own_ping_timeout(self):
        await self.client.service_register("test", "canvas", self.service.url, 2.5)
        self.assertEqual(self.worker.services.registry.get("service/canvas").timeout, 2.5)
        for bad in (0, 3.0, True, "1"):
            with self.subTest(timeout=bad), self.assertRaises(RefusalError) as caught:
                await self.client.service_register("test", "far", self.service.url, bad)
            self.assertEqual(caught.exception.data["code"], "invalid_request")
        with self.assertRaises(RefusalError) as caught:  # the envelope names the timeout
            await self.client.request("test", "service_register", name="far", url=self.service.url)
        self.assertEqual(caught.exception.code, "invalid_request")
        self.assertIsNone(self.worker.services.registry.get("service/far"))

    async def test_register_list_describe_call(self):
        self.assertEqual(await self.client.service_list("test"), {"services": []})
        self.assertEqual(await self.client.service_register("test", "canvas", self.service.url, 1.0),
                         {"ping_interval_seconds": 3.0})
        await self.worker.services.registry.ping_all()
        meta = json.loads(b"".join(self.service.requests[0][2]))["params"]["_meta"]
        self.assertEqual(meta["io.modelcontextprotocol/clientInfo"]["name"], "alpha")
        listed = await self.client.service_list("test")
        self.assertTrue(listed["services"][0]["available"])
        described = await self.client.service_describe("test", "canvas", "good")
        self.assertIn("echo", {tool["name"] for tool in described["tools"]})
        called = await self.client.service_call("test", "canvas", "echo", {"text": "x"}, "good")
        self.assertEqual(called["structuredContent"], {"echo": "x"})
        with self.assertRaises(RefusalError) as caught:
            await self.client.service_call("test", "canvas", "refuse", {}, "good")
        self.assertEqual(caught.exception.data["code"], "quota_exceeded")
        with self.assertRaises(RefusalError) as caught:
            await self.client.request("test", "service_call", service="canvas", service_operation="echo",
                                      arguments={}, token="good")
        self.assertEqual(caught.exception.code, "invalid_request")

    async def test_a_timeout_reaches_the_caller_as_a_failure(self):
        await self.client.service_register("test", "canvas", self.service.url, 1.0)
        with patch.object(services, "CALL_TIMEOUT_SECONDS", 0.2):
            with self.assertRaisesRegex(FailureReport, "may have run"):
                await self.client.service_call("test", "canvas", "slow", {}, "good")


class ServiceDeadlineTest(unittest.IsolatedAsyncioTestCase):
    async def test_requests_carry_their_budget_and_the_caller_waits_one_margin_more(self):
        # The worker's own deadline (which says the call may have run) must reach
        # the caller: the control plane waits the budget plus REPLY_MARGIN_SECONDS.
        from boring_crocodile import worker_client
        from boring_worker_kit.deadlines import REPLY_MARGIN_SECONDS, SERVICE_OPERATION_SECONDS
        from boring_worker_kit.services import CALL_TIMEOUT_SECONDS
        seen = []
        client = worker_client.WorkerClient({})

        async def fake(environment, operation, **arguments):
            seen.append((operation, arguments.get("budget_seconds")))
            return {}
        waits = []
        real_wait_for = asyncio.wait_for

        async def recording(awaitable, timeout):
            waits.append(timeout)
            return await real_wait_for(awaitable, timeout)
        with patch.object(client, "_request", fake), patch.object(worker_client.asyncio, "wait_for", recording):
            await client.service_call("e", "canvas", "x", {}, "t")
            await client.service_describe("e", "canvas", "t")
            await client.request("e", "exec", command="true")
        self.assertEqual([b for _, b in seen], [SERVICE_OPERATION_SECONDS, CALL_TIMEOUT_SECONDS, 103])
        self.assertEqual(waits, [SERVICE_OPERATION_SECONDS + REPLY_MARGIN_SECONDS,
                                 CALL_TIMEOUT_SECONDS + REPLY_MARGIN_SECONDS, 103 + REPLY_MARGIN_SECONDS])


class FailedServiceOutcomeTest(unittest.TestCase):
    def test_failed_effect_details_are_not_a_refusal(self):
        from boring_worker_kit.services import _tool_refusal
        data = {"status": "failed", "code": "sql_error", "message": "second batch failed",
                "details": {"batches_done": 1, "rolled_back": False}}
        result = types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(data))],
                                      is_error=True)
        outcome = _tool_refusal(result)
        self.assertIsInstance(outcome, ServiceFailure)
        self.assertEqual(outcome.data, data)
