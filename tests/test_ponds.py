"""Pond isolation through the real OAuth provider, HTTP middleware and Unix client."""
import asyncio
from async_lifespan import start_lifespan
import base64
import hashlib
from dataclasses import replace
import json
import logging
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4
from unittest.mock import AsyncMock, patch

import httpx2
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from boring_crocodile.handbook_context import ContextBootstrap
from boring_crocodile.control_plane import create_http_app
from boring_worker_kit.framing import read_frame, write_frame
from boring_crocodile.settings import Settings, PondSettings
from boring_crocodile.sqlite_oauth_provider import SQLiteOAuthProvider
from pond_config import dynamic


class PondsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        previous = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, previous)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ids = {name: str(uuid4()) for name in ("alpha", "beta")}
        config = f'''
[server]
public_base_url = "https://server.test"
bind_host = "127.0.0.1"
bind_port = 8503
allowed_hosts = ["server.test"]
[auth]
oauth_database_file = "{self.root}/oauth.sqlite3"
dynamic_registration = true
'''
        for name in self.ids:
            (self.root / name).mkdir()
            (self.root / name / "contract.md").write_text(name + " private context")
            (self.root / (name + ".secret")).write_text(name + "-test-secret")
            config += f'''
[ponds.{name}]
profile_id = "{self.ids[name]}"
display_name = "{name}"
environment = "{name}"
approval_secret_file = "{self.root}/{name}.secret"
context_root = "{self.root}/{name}"
files = ["contract.md"]
[ponds.{name}.projects.{name}]
files = ["contract.md"]
[environments.{name}]
socket_path = "{self.root}/{name}.sock"
default_cwd = "{self.root}/{name}"
allowed_roots = ["{self.root}/{name}"]
control_plane_uid = 1000
'''
        self.config = self.root / "config.toml"
        self.config.write_text(config)
        self.settings = Settings.load(self.config)
        self.provider = self.open()
        self.client = OAuthClientInformationFull(client_id="test-client", scope="mcp", token_endpoint_auth_method="none",
            redirect_uris=[AnyUrl("https://client.test/callback")])
        await self.provider.register_client(self.client)
        self.calls = []
        self.arrived = set()
        self.both_arrived = asyncio.Event()
        for name in self.ids:
            async def worker(reader, writer, name=name):
                try:
                    request = await read_frame(reader)
                    if request.get("operation") != "limits":
                        self.calls.append((name, request.get("command")))
                    if request.get("command") == "overlap":
                        self.arrived.add(name)
                        if len(self.arrived) == 2:
                            self.both_arrived.set()
                        await asyncio.wait_for(self.both_arrived.wait(), 3)
                    operation = request.get("operation")
                    result = ({"exec_timeout_seconds": 120.0, "job_timeout_seconds": None,
                               "task_slots": 1} if operation == "limits"
                              else {"worker": name})
                    if operation == "bootstrap":
                        result = ContextBootstrap.for_environment(self.settings, name).load(request.get("project"))
                        result["capabilities"] = {
                            "command_execution_enabled": True,
                            "command_limits": {"task_slots": 1, "exec_timeout_seconds": 120.0,
                                               "job_timeout_seconds": None},
                            "managed_jobs_enabled": True,
                            "pirates": ([{"pirate": "parrot", "flavour": "default", "available": True, "misses": 0, "last_miss": None}] if name == "alpha" else []),
                        }
                    elif operation == "list_pirates":
                        result = {"pirates": ([{"pirate": "parrot", "flavour": "default", "available": True, "misses": 0, "last_miss": None}] if name == "alpha" else [])}
                    elif operation == "pirate_describe":
                        result = {"flavour": request["flavour"], "limits": {"max_timeout_seconds": 60}}
                    if request.get("command") == "probe-not-admitted":
                        await write_frame(writer, {"ok": False, "refusal": {
                            "code": "not_admitted",
                            "message": "foreground admission timeout; command not started"}})
                    elif request.get("command") == "probe-interrupted":
                        await write_frame(writer, {"ok": False, "failure": (
                            "ForegroundInterrupted: foreground request deadline exceeded; "
                            "task cleanup completed; command may have started")})
                    else:
                        await write_frame(writer, {"ok": True, "result": result})
                finally:
                    writer.close()
                    await writer.wait_closed()
            server = await asyncio.start_unix_server(worker, self.root / (name + ".sock"))
            async def close(server=server):
                server.close()
                await server.wait_closed()
            self.addAsyncCleanup(close)
        self.mcp, self.app = create_http_app(self.settings)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await start_lifespan(self, self.lifespan)
        self.http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app),
                                       base_url="https://server.test")
        self.addAsyncCleanup(self.http.aclose)

    def open(self, ponds="configured"):
        return SQLiteOAuthProvider("https://server.test", self.settings.oauth_database_file,
            self.settings.ponds if ponds == "configured" else ponds, dynamic())

    async def pending(self, resource="https://server.test/mcp/alpha"):
        url = await self.provider.authorize(self.client, AuthorizationParams(
            state="state", scopes=["mcp"], code_challenge="test-challenge",
            redirect_uri=AnyUrl("https://client.test/callback"),
            redirect_uri_provided_explicitly=True, resource=resource))
        return parse_qs(urlparse(url).query)["request"][0]

    async def code(self, name, resource=None):
        request = await self.pending(resource or f"https://server.test/mcp/{name}")
        callback = self.provider.approve(request, name + "-test-secret", name)
        value = parse_qs(urlparse(callback).query)["code"][0]
        return await self.provider.load_authorization_code(self.client, value)

    async def tokens(self, name, resource=None):
        return await self.provider.exchange_authorization_code(self.client, await self.code(name, resource))

    async def call(self, token, name, arguments=None, path=None):
        # By default, the `/mcp/<pond>` path the token was issued for: the only place it works.
        path = path or urlparse((await self.provider.load_access_token(token)).resource).path
        response = await self.http.post(path, headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-03-26"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": name, "arguments": arguments or {}}})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["result"]

    async def test_http_concurrent_binding_context_and_worker(self):
        tokens = {name: (await self.tokens(name)).access_token for name in self.ids}
        guides = {}
        async def sequence(name):
            bootstrap = await self.call(tokens[name], "pond_bootstrap", {"project": name})
            self.assertFalse(bootstrap.get("isError"), bootstrap)
            self.assertEqual(bootstrap["structuredContent"]["handbook"]["documents"][0]["content"],
                             name + " private context")
            body = bootstrap["structuredContent"]
            guides[name] = body["mcp_guide"]
            self.assertIn("Where were we?", guides[name]["content"])
            self.assertEqual(body["capabilities"]["pirates"],
                             [{"pirate": "parrot", "flavour": "default", "available": True, "misses": 0, "last_miss": None}] if name == "alpha" else [])
            self.assertNotIn("mcp_guide", body["handbook"])
            executed = await self.call(tokens[name], "exec", {"command": "overlap"})
            self.assertEqual(executed["structuredContent"]["worker"], name)
            ping = await self.call(tokens[name], "pond_ping")
            self.assertEqual(ping["structuredContent"]["environments"], [name])
            self.assertEqual(ping["structuredContent"]["message"], f"authenticated {name} says <^==>")
        await asyncio.gather(*(sequence(name) for name in self.ids))
        self.assertEqual(self.arrived, set(self.ids))
        self.assertEqual(guides["alpha"], guides["beta"])
        self.assertEqual(guides["alpha"]["sha256"],
                         hashlib.sha256(guides["alpha"]["content"].encode()).hexdigest())
        before = list(self.calls)
        wrong = await self.call(tokens["alpha"], "exec", {"command": "forbidden", "environment": "beta"})
        self.assertTrue(wrong["isError"])
        self.assertEqual(self.calls, before)
        limits = await self.call(tokens["alpha"], "pond_limits")
        self.assertFalse(limits.get("isError"), limits)
        self.assertEqual(limits["structuredContent"], {
            "exec_timeout_seconds": 120.0,
            "job_timeout_seconds": None, "task_slots": 1,
        })
        for tool, arguments in (
            ("pond_limits", {"environment": "beta"}),
            ("job_start", {"command": "forbidden", "environment": "beta"}),
            ("job_list", {"environment": "beta"}),
            ("job_status", {"job_id": "0" * 32, "environment": "beta"}),
            ("job_output", {"job_id": "0" * 32, "environment": "beta"}),
            ("job_stop", {"job_id": "0" * 32, "environment": "beta"}),
        ):
            denied = await self.call(tokens["alpha"], tool, arguments)
            self.assertTrue(denied["isError"], tool)
        self.assertEqual(self.calls, before)
        wrong_project = await self.call(tokens["alpha"], "pond_bootstrap", {"project": "beta"})
        self.assertTrue(wrong_project["isError"])
        self.assertNotIn("beta private context", json.dumps(wrong_project))
        unauthenticated = await self.http.post("/mcp/alpha", json={})
        self.assertEqual(unauthenticated.status_code, 401)

    async def test_pirate_job_timeout_rejects_coercion_before_worker_dispatch(self):
        token = (await self.tokens("alpha")).access_token
        base = {"pirate": "parrot", "flavour": "default", "assignment": "synthetic"}
        for value in (True, False, "1", "NaN", float("nan"), float("inf"),
                      -float("inf"), 0, -1):
            with self.subTest(rejected=repr(value)):
                dispatch = AsyncMock(return_value={"unexpected": "dispatch"})
                with patch("boring_crocodile.worker_client.WorkerClient.job_start", dispatch):
                    # Raw bytes avoid SDK serialization turning NaN/Infinity into null.
                    response = await self.http.post("/mcp/alpha", headers={
                        "Authorization": "Bearer " + token,
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                        "MCP-Protocol-Version": "2025-03-26"},
                        content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "job_start",
                                       "arguments": {"pirate": dict(base, timeout_seconds=value)}}}))
                dispatch.assert_not_awaited()
                body = response.json()
                self.assertTrue(response.status_code >= 400 or "error" in body
                                or body.get("result", {}).get("isError"), body)
        for pirate, expected in ((base, None), (dict(base, timeout_seconds=None), None),
                                 (dict(base, timeout_seconds=1), 1.0),
                                 (dict(base, timeout_seconds=0.5), 0.5)):
            with self.subTest(accepted=pirate):
                dispatch = AsyncMock(return_value={"probe": "accepted; no work executed"})
                with patch("boring_crocodile.worker_client.WorkerClient.job_start", dispatch):
                    result = await self.call(token, "job_start", {"pirate": pirate})
                self.assertFalse(result.get("isError"), result)
                dispatch.assert_awaited_once_with(
                    "alpha", None, None, None, pirate=dict(base, timeout_seconds=expected))
        # The timeout belongs to the Pirate payload; the job-level one and cwd are refused.
        for extra in ({"timeout_seconds": 5}, {"cwd": "."}, {"command": "true"}):
            with self.subTest(refused=extra):
                response = await self.call(token, "job_start", {"pirate": base, **extra})
                self.assertEqual(response["structuredContent"]["code"], "invalid_request")

    async def test_expected_exec_deadlines_survive_public_boundary(self):
        from boring_worker_kit.execution import ForegroundAdmissionTimeout, ForegroundInterrupted
        from boring_crocodile.outcomes import Refusal
        token = (await self.tokens("alpha")).access_token
        # Never started: a refusal with its code.
        self.assertEqual(ForegroundAdmissionTimeout().code, "not_admitted")
        response = await self.call(token, "exec", {"command": "probe-not-admitted"})
        self.assertTrue(response["isError"], response)
        text = response["content"][0]["text"]
        refusal = json.loads(text)
        self.assertEqual(response["structuredContent"], refusal)
        self.assertEqual(refusal["code"], "not_admitted")
        self.assertIn("command not started", refusal["message"])
        # Started, then past the deadline: a failure, never a refusal.
        self.assertNotIsInstance(ForegroundInterrupted(), Refusal)
        self.assertIn("command may have started", ForegroundInterrupted().message)
        response = await self.call(token, "exec", {"command": "probe-interrupted"})
        self.assertTrue(response["isError"], response)
        text = response["content"][0]["text"]
        failure = json.loads(text)
        self.assertEqual(response["structuredContent"], failure)
        self.assertNotIn("code", failure)
        self.assertIn("command may have started", failure["failure"])

    async def test_control_plane_refusals_are_coded_json(self):
        token = (await self.tokens("alpha")).access_token
        read = {"id": "same", "method": "file_read", "arguments": {"root": "r", "path": "x"}}
        response = await self.call(token, "batch", {"operations": [read, read]})
        self.assertTrue(response["isError"], response)
        text = response["content"][0]["text"]
        refusal = json.loads(text)
        self.assertEqual(response["structuredContent"], refusal)
        self.assertEqual(refusal["code"], "invalid_request")
        self.assertEqual(refusal["fields"], ["operations"])
        response = await self.call(token, "job_start", {})
        self.assertEqual(response["structuredContent"]["code"], "invalid_request")

    async def test_command_timeouts_and_receipt_limits_are_strict_at_http_boundary(self):
        token = (await self.tokens("alpha")).access_token
        cases = (
            ("exec", "execute", {"command": "true"}, "timeout_seconds", (0, 0.05), (None, 0.1, 1, 1.25)),
            ("job_start", "job_start", {"command": "true"},
             "timeout_seconds", (0, 0.05, 86401), (None, 0.1, 1, 1.25, 86400)),
            ("job_list", "list_jobs", {}, "limit", (0, 101, 1.5, None), (1, 20, 100)),
            ("job_wait", "job_wait", {"job_ids": ["0" * 32]},
             "wait_seconds", (61, None), (0, 1, 45, 60)),
        )
        for tool, method, base, field, extra_invalid, valid in cases:
            for value in (True, False, "1", -1, float("nan"), float("inf"), -float("inf"), *extra_invalid):
                with self.subTest(tool=tool, rejected=repr(value)):
                    dispatch = AsyncMock(return_value={"probe": "unexpected"})
                    with patch("boring_crocodile.worker_client.WorkerClient." + method, dispatch):
                        response = await self.http.post("/mcp/alpha", headers={
                            "Authorization": "Bearer " + token,
                            "Accept": "application/json, text/event-stream",
                            "Content-Type": "application/json",
                            "MCP-Protocol-Version": "2025-03-26"},
                            content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": tool, "arguments": dict(base, **{field: value})}}))
                    dispatch.assert_not_awaited()
                    body = response.json()
                    self.assertTrue(response.status_code >= 400 or "error" in body
                                    or body.get("result", {}).get("isError"), body)
            # Include omission separately from explicit null/default requests.
            for arguments in [base, *(dict(base, **{field: v}) for v in valid)]:
                with self.subTest(tool=tool, accepted=arguments):
                    dispatch = AsyncMock(return_value={"probe": "no work executed"})
                    with patch("boring_crocodile.worker_client.WorkerClient." + method, dispatch):
                        result = await self.call(token, tool, arguments)
                    self.assertFalse(result.get("isError"), result)
                    expected = arguments.get(field, {"timeout_seconds": None, "limit": 20,
                                                     "wait_seconds": 45}[field])
                    if tool == "exec":
                        dispatch.assert_awaited_once_with("alpha", "true", None, expected)
                    elif tool == "job_start":
                        dispatch.assert_awaited_once_with("alpha", "true", None, expected,
                                                          operations=None, on_error="stop")
                    elif tool == "job_list":
                        dispatch.assert_awaited_once_with("alpha", expected)
                    else:
                        dispatch.assert_awaited_once_with("alpha", ["0" * 32], expected)

    async def test_pirate_tools_follow_authenticated_pond(self):
        alpha = (await self.tokens("alpha")).access_token
        beta = (await self.tokens("beta")).access_token
        listed = await self.call(alpha, "pirate_list")
        self.assertEqual([(p["pirate"], p["flavour"]) for p in listed["structuredContent"]["pirates"]],
                         [("parrot", "default")])
        empty = await self.call(beta, "pirate_list")
        self.assertEqual(empty["structuredContent"]["pirates"], [])
        described = await self.call(alpha, "pirate_describe", {"pirate": "parrot", "flavour": "default"})
        self.assertEqual(described["structuredContent"]["flavour"], "default")

    async def test_file_calls_bind_to_authenticated_pond_and_validate_limits(self):
        alpha = (await self.tokens("alpha")).access_token
        beta = (await self.tokens("beta")).access_token
        a = await self.call(alpha, "file_list", {"root": "work"})
        b = await self.call(beta, "file_list", {"root": "work"})
        self.assertEqual(a["structuredContent"]["worker"], "alpha")
        self.assertEqual(b["structuredContent"]["worker"], "beta")
        for invalid in (True, "10", 0):
            response = await self.call(alpha, "file_list", {"root": "work", "limit": invalid})
            self.assertTrue(response.get("isError"), response)

    async def test_batch_public_schema_and_pond_binding(self):
        alpha = (await self.tokens("alpha")).access_token
        beta = (await self.tokens("beta")).access_token
        args = {"operations": [{"id": "read", "method": "file_read",
                                "arguments": {"root": "work", "path": "note"}}]}
        for token, name in ((alpha, "alpha"), (beta, "beta")):
            result = await self.call(token, "batch", args)
            self.assertEqual(result["structuredContent"]["worker"], name)
        for invalid in (True, "3", 0):
            changed = {"operations": [{"id": "list", "method": "file_list",
                "arguments": {"root": "work", "limit": invalid}}]}
            response = await self.call(alpha, "batch", changed)
            self.assertTrue(response.get("isError"), response)
        extra = {"operations": [{"id": "bad", "method": "file_read",
            "arguments": {"root": "work", "path": "note", "environment": "beta"}}]}
        self.assertTrue((await self.call(alpha, "batch", extra)).get("isError"))

    async def test_profile_schema_and_credential_identity(self):
        issued = await self.tokens("alpha")
        response = await self.http.post("/mcp/alpha", headers={
            "Authorization": "Bearer " + issued.access_token,
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-03-26"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(response.status_code, 200)
        tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
        self.assertIn("selected context welcome", tools["pond_bootstrap"]["description"])
        self.assertNotIn("The Crocodile is ready", tools["pond_bootstrap"]["description"])
        profile = tools["pond_profile"]
        self.assertEqual(profile["_meta"], {"openai/profile": True})
        self.assertEqual(profile["inputSchema"].get("properties"), {})
        self.assertTrue(profile["annotations"]["readOnlyHint"])
        self.assertFalse(profile["outputSchema"]["additionalProperties"])
        self.assertEqual(set(profile["outputSchema"]["properties"]), {"id", "name"})
        for name in self.ids:
            result = await self.call((await self.tokens(name)).access_token, "pond_profile")
            self.assertEqual(result["structuredContent"], {"id": self.ids[name], "name": name})
            self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])

    async def test_approval_page_has_no_pond_picker_and_checks_the_pond_secret(self):
        request = await self.pending("https://server.test/mcp/beta")
        page = await self.http.get("/approve", params={"request": request})
        self.assertEqual(page.status_code, 200)
        self.assertIn("Pond: <strong>beta</strong>", page.text)
        for picker in ("<select", 'name="pond"', "Choose"):
            self.assertNotIn(picker, page.text)
        self.assertNotIn("test-secret", page.text)
        denied = await self.http.post("/approve", params={"request": request},
                                      data={"secret": "alpha-test-secret"})
        self.assertEqual((denied.status_code, "Wrong approval secret." in denied.text), (403, True))
        approved = await self.http.post("/approve", params={"request": request},
                                        data={"secret": "beta-test-secret"})
        again = await self.http.post("/approve", params={"request": request},
                                     data={"secret": "beta-test-secret"})
        self.assertEqual(again.status_code, 400)
        self.assertIn("unknown or has expired", again.text)
        self.assertEqual(approved.status_code, 302)
        self.assertFalse(self.provider.has_pending(request))

    async def test_restart_refresh_rename_and_removal_fail_closed(self):
        issued = await self.tokens("alpha")
        reopened = self.open()
        refresh = await reopened.load_refresh_token(self.client, issued.refresh_token)
        rotated = await reopened.exchange_refresh_token(self.client, refresh, [])
        self.assertIsNone(await reopened.load_access_token(issued.access_token))
        self.assertEqual((await reopened.load_access_token(rotated.access_token)).subject, self.ids["alpha"])
        renamed = dict(self.settings.ponds)
        renamed["alpha"] = replace(renamed["alpha"], display_name="Renamed")
        self.assertEqual((await self.open(renamed).load_access_token(rotated.access_token)).subject,
                         self.ids["alpha"])
        code = await self.code("alpha")
        refresh = await reopened.load_refresh_token(self.client, rotated.refresh_token)
        removed = self.open({"beta": self.settings.ponds["beta"]})
        self.assertIsNone(await removed.load_access_token(rotated.access_token))
        self.assertIsNone(await removed.load_refresh_token(self.client, rotated.refresh_token))
        self.assertIsNone(await removed.load_authorization_code(self.client, code.code))
        with self.assertRaises(TokenError):
            await removed.exchange_authorization_code(self.client, code)
        with self.assertRaises(TokenError):
            await removed.exchange_refresh_token(self.client, refresh, [])

    async def test_complete_http_oauth_flow_enforces_pkce_and_pond_binding(self):
        verifier = "a" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        start = await self.http.get("/authorize", params={
            "client_id": self.client.client_id, "response_type": "code",
            "redirect_uri": "https://client.test/callback", "scope": "mcp",
            "state": "test-state", "code_challenge": challenge, "code_challenge_method": "S256",
            "resource": "https://server.test/mcp/beta"})
        self.assertEqual(start.status_code, 302, start.text)
        request = parse_qs(urlparse(start.headers["location"]).query)["request"][0]
        approved = await self.http.post("/approve", params={"request": request},
            data={"secret": "beta-test-secret"})
        self.assertEqual(approved.status_code, 302, approved.text)
        callback = parse_qs(urlparse(approved.headers["location"]).query)
        self.assertEqual(callback["state"], ["test-state"])
        data = {"grant_type": "authorization_code", "client_id": self.client.client_id,
                "redirect_uri": "https://client.test/callback", "code": callback["code"][0],
                "code_verifier": "wrong"}
        denied = await self.http.post("/token", data=data)
        self.assertEqual(denied.status_code, 400)
        issued = await self.http.post("/token", data=data | {"code_verifier": verifier})
        self.assertEqual(issued.status_code, 200, issued.text)
        result = await self.call(issued.json()["access_token"], "pond_profile", path="/mcp/beta")
        self.assertEqual(result["structuredContent"]["id"], self.ids["beta"])

    async def test_discovered_resource_and_dynamic_client_http_flow(self):
        metadata = await self.http.get("/.well-known/oauth-protected-resource/mcp/alpha")
        self.assertEqual(metadata.status_code, 200)
        resource = metadata.json()["resource"]
        self.assertEqual(resource, "https://server.test/mcp/alpha")
        registered = await self.http.post("/register", json={
            "redirect_uris": ["https://client.test/callback"],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "scope": "mcp"})
        self.assertEqual(registered.status_code, 201)
        client_id = registered.json()["client_id"]
        verifier = "b" * 64
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        # No resource and the retired bare /mcp are refused like any foreign resource.
        for audience in (None, "https://server.test/mcp", "http://127.0.0.1:18503/mcp/alpha", resource):
            with self.subTest(audience=audience):
                params = {
                    "client_id": client_id, "response_type": "code",
                    "redirect_uri": "https://client.test/callback", "scope": "mcp",
                    "code_challenge": challenge, "code_challenge_method": "S256"}
                if audience is not None:
                    params["resource"] = audience
                start = await self.http.get("/authorize", params=params)
                self.assertEqual(start.status_code, 302)
                if audience != resource:
                    # No token is issued for a resource this server is not.
                    rejected = parse_qs(urlparse(start.headers["location"]).query)
                    self.assertEqual(rejected["error"], ["invalid_target"])
                    self.assertNotIn("request", rejected)
                    continue
                request = parse_qs(urlparse(start.headers["location"]).query)["request"][0]
                approved = await self.http.post("/approve", params={"request": request},
                    data={"secret": "alpha-test-secret"})
                self.assertEqual(approved.status_code, 302)
                code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
                issued = await self.http.post("/token", data={
                    "grant_type": "authorization_code", "client_id": client_id,
                    "redirect_uri": "https://client.test/callback",
                    "code": code, "code_verifier": verifier})
                self.assertEqual(issued.status_code, 200)
                token = issued.json()["access_token"]
                result = await self.call(token, "pond_profile", path="/mcp/alpha")
                self.assertEqual(result["structuredContent"]["id"], self.ids["alpha"])

    async def test_bare_mcp_is_gone(self):
        # Not an endpoint: a plain 404 that points no client at any sign-in.
        for method in ("GET", "POST"):
            for headers in ({}, {"Authorization": "Bearer " + (await self.tokens("alpha")).access_token}):
                response = await self.http.request(method, "/mcp", headers=headers, json={})
                self.assertEqual(response.status_code, 404, (method, headers))
                self.assertNotIn("www-authenticate", response.headers)
        self.assertEqual((await self.http.get("/.well-known/oauth-protected-resource/mcp")).status_code, 404)
        # The provider refuses to authorize it, or a request naming no resource.
        for resource in ("https://server.test/mcp", "https://server.test/mcp/", None):
            with self.subTest(resource=resource), self.assertRaises(AuthorizeError) as refused:
                await self.pending(resource)
            self.assertEqual(refused.exception.error, "invalid_target")
        # A token an older release issued for bare /mcp works nowhere.
        legacy = self.open()
        legacy.resources["https://server.test/mcp"] = None
        url = await legacy.authorize(self.client, AuthorizationParams(
            state="state", scopes=["mcp"], code_challenge="test-challenge",
            redirect_uri=AnyUrl("https://client.test/callback"),
            redirect_uri_provided_explicitly=True, resource="https://server.test/mcp"))
        callback = legacy.approve(parse_qs(urlparse(url).query)["request"][0], "alpha-test-secret", "alpha")
        code = await legacy.load_authorization_code(self.client, parse_qs(urlparse(callback).query)["code"][0])
        token = (await legacy.exchange_authorization_code(self.client, code)).access_token
        for path, status in (("/mcp/alpha", 401), ("/mcp/beta", 401), ("/mcp", 404)):
            denied = await self.http.post(path, headers={"Authorization": "Bearer " + token},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            self.assertEqual(denied.status_code, status, path)

    async def test_pond_paths_are_separate_protected_resources(self):
        for name in self.ids:
            metadata = await self.http.get(f"/.well-known/oauth-protected-resource/mcp/{name}")
            self.assertEqual(metadata.status_code, 200)
            self.assertEqual(metadata.json()["resource"], f"https://server.test/mcp/{name}")
            unauthenticated = await self.http.post(f"/mcp/{name}", json={})
            self.assertEqual(unauthenticated.status_code, 401)
            self.assertIn(f"/.well-known/oauth-protected-resource/mcp/{name}",
                          unauthenticated.headers["www-authenticate"])
        self.assertEqual((await self.http.post("/mcp/unknown", json={})).status_code, 404)

    async def test_pond_path_token_works_only_on_its_path(self):
        token = (await self.tokens("alpha")).access_token
        result = await self.call(token, "pond_profile", path="/mcp/alpha")
        self.assertEqual(result["structuredContent"]["id"], self.ids["alpha"])
        denied = await self.http.post("/mcp/beta", headers={"Authorization": "Bearer " + token},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.json()["error"], "invalid_token")
        self.assertIn("/.well-known/oauth-protected-resource/mcp/beta", denied.headers["www-authenticate"])

    async def test_pond_path_approval_is_locked_to_that_pond(self):
        request = await self.pending("https://server.test/mcp/alpha")
        page = await self.http.get("/approve", params={"request": request})
        self.assertNotIn('name="pond"', page.text)
        self.assertIn("alpha", page.text)
        # The form cannot redirect the request to another Pond.
        denied = await self.http.post("/approve", params={"request": request},
                                      data={"pond": "beta", "secret": "beta-test-secret"})
        self.assertEqual(denied.status_code, 403)
        approved = await self.http.post("/approve", params={"request": request},
                                        data={"pond": "beta", "secret": "alpha-test-secret"})
        self.assertEqual(approved.status_code, 302)
        code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
        grant = await self.provider.load_authorization_code(self.client, code)
        self.assertEqual(grant.subject, self.ids["alpha"])

    async def test_example_and_distinct_systemd_task_boundaries(self):
        example = Settings.load(Path("site/default/two-ponds/etc/boring-crocodile/config.toml"))
        self.assertEqual(set(example.ponds), {"pond", "second"})
        alpha = example.environments["pond"]
        beta = example.environments["second"]
        for field in ("socket_path", "task_socket_path", "task_unit_prefix", "task_cgroup_parent"):
            environments = dict(example.environments)
            environments["second"] = replace(beta, **{field: getattr(alpha, field)})
            with self.subTest(field=field), self.assertRaises(ValueError):
                replace(example, environments=environments)

    async def test_configuration_rejects_ambiguous_or_escaping_catalogs(self):
        ponds = self.settings.ponds
        for beta in (replace(ponds["beta"], profile_id=ponds["alpha"].profile_id),
                     replace(ponds["beta"], environment="alpha"),
                     replace(ponds["beta"], environment="missing"),
                     replace(ponds["beta"], context_root=ponds["alpha"].context_root / "nested")):
            with self.assertRaises(ValueError):
                replace(self.settings, ponds={"alpha": ponds["alpha"], "beta": beta})
        with self.assertRaises(ValueError):
            replace(self.settings, ponds={})
        for files in ([], ["../contract.md"], ["/etc/passwd"], ["x", "x"]):
            with self.assertRaises(ValueError):
                PondSettings.from_mapping(dict(profile_id=self.ids["alpha"], display_name="alpha",
                    environment="alpha", approval_secret_file=str(self.root/"alpha.secret"),
                    context_root=str(self.root/"alpha"), files=files))
