"""The control plane's side of services: registration, introspection, call tokens."""
import asyncio
from async_lifespan import start_lifespan
import base64
import logging
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx2
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from boring_crocodile.control_plane import (SERVICE_TOKEN_RENEW_SECONDS, SERVICE_TOKEN_SECONDS,
                                            ServiceTokens, create_http_app)
from boring_worker_kit.framing import read_frame, write_frame
from boring_crocodile.oauth_records import Transaction, add_service
from boring_crocodile.settings import Settings
from boring_crocodile.sqlite_oauth_provider import SQLiteOAuthProvider
from pond_config import dynamic

URL = "http://127.0.0.1:18511/mcp"


def basic(client_id, secret):
    return "Basic " + base64.b64encode(f"{client_id}:{secret}".encode()).decode()


class ServiceRoutesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
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
        for name in ("alpha", "beta"):
            (self.root / name).mkdir()
            (self.root / name / "contract.md").write_text(name)
            (self.root / (name + ".secret")).write_text(name + "-test-secret")
            # Environment names differ from Pond names on purpose.
            config += f'''
[ponds.{name}]
profile_id = "{uuid4()}"
display_name = "{name}"
environment = "{name}-env"
approval_secret_file = "{self.root}/{name}.secret"
context_root = "{self.root}/{name}"
files = ["contract.md"]
[environments.{name}-env]
socket_path = "{self.root}/{name}.sock"
default_cwd = "{self.root}/{name}"
allowed_roots = ["{self.root}/{name}"]
control_plane_uid = 1000
'''
        (self.root / "config.toml").write_text(config)
        self.settings = Settings.load(self.root / "config.toml")
        self.provider = SQLiteOAuthProvider("https://server.test", self.settings.oauth_database_file,
                                            self.settings.ponds, dynamic())
        with Transaction(self.settings.oauth_database_file) as db:
            self.canvas = add_service(db, "canvas", URL, ["alpha", "beta"])
            self.other = add_service(db, "other", "http://127.0.0.1:18600/mcp", ["alpha"])
        self.requests = {"alpha": [], "beta": []}
        self.servers = {}
        for name in self.requests:
            await self.start_worker(name)
        self.mcp, self.app = create_http_app(self.settings)
        lifespan = self.app.router.lifespan_context(self.app)
        await start_lifespan(self, lifespan)
        self.http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app),
                                       base_url="https://server.test")
        self.addAsyncCleanup(self.http.aclose)

    async def start_worker(self, name):
        async def worker(reader, writer):
            try:
                request = await read_frame(reader)
                self.requests[name].append(request)
                result = ({"ping_interval_seconds": 3.0} if request["operation"] == "service_register"
                          else {"services": [{"name": listed, "available": True, "misses": 0,
                                              "last_miss": None} for listed in ("canvas", "other", "gone")]}
                          if request["operation"] == "service_list" else {"worker": name})
                await write_frame(writer, {"ok": True, "result": result})
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_unix_server(worker, self.root / (name + ".sock"))
        self.servers[name] = server
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)

    async def register(self, authorization=None, content=b""):
        headers = {"Authorization": authorization} if authorization else {}
        return await self.http.post("/services/register", headers=headers, content=content)

    async def introspect(self, token, authorization=None):
        authorization = authorization or basic(self.canvas["client_id"], self.canvas["client_secret"])
        return await self.http.post("/introspect", headers={"Authorization": authorization},
                                    data={"token": token})

    async def test_registration_reaches_every_granted_pond(self):
        response = await self.register(basic(self.canvas["client_id"], self.canvas["client_secret"]))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"ping_interval_seconds": 3.0, "sources": ["alpha", "beta"]})
        for name in ("alpha", "beta"):
            [request] = self.requests[name]
            self.assertEqual({key: request[key] for key in ("operation", "name", "url", "ping_timeout_seconds")},
                             {"operation": "service_register", "name": "canvas", "url": URL,
                              "ping_timeout_seconds": 1.0})
        response = await self.register(basic(self.other["client_id"], self.other["client_secret"]))
        self.assertEqual(response.json()["sources"], ["alpha"])
        self.assertEqual(len(self.requests["beta"]), 1)

    async def test_bad_credentials_are_refused_before_anything_is_forwarded(self):
        for authorization in (None, basic(self.canvas["client_id"], "wrong"),
                              basic("svc_unknown", self.canvas["client_secret"]),
                              basic(self.other["client_id"], self.canvas["client_secret"]),
                              "Bearer " + self.canvas["client_secret"], "Basic !!!"):
            with self.subTest(authorization=authorization):
                response = await self.register(authorization)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error"]["code"], "forbidden")
                response = await self.introspect("x", authorization or "Basic ")
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error"]["code"], "forbidden")
        self.assertEqual(self.requests, {"alpha": [], "beta": []})
        response = await self.register(basic(self.canvas["client_id"], self.canvas["client_secret"]), b"{}")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    async def test_a_worker_that_is_down_is_still_a_source(self):
        self.servers["beta"].close()
        await self.servers["beta"].wait_closed()
        (self.root / "beta.sock").unlink(missing_ok=True)
        with self.assertLogs("boring_crocodile.control_plane", logging.WARNING) as logs:
            response = await self.register(basic(self.canvas["client_id"], self.canvas["client_secret"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sources"], ["alpha", "beta"])
        self.assertEqual(len(self.requests["alpha"]), 1)
        self.assertIn("Pond beta", "\n".join(logs.output))

    async def test_introspection_is_active_only_for_the_services_own_live_tokens(self):
        token = self.provider.mint_service_token("alpha", self.canvas, SERVICE_TOKEN_SECONDS)
        response = await self.introspect(token.token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"active": True, "sub": "alpha", "aud": URL,
                                           "exp": token.expires_at, "client_id": self.canvas["client_id"]})
        # Another service cannot use (or learn about) canvas's token.
        response = await self.introspect(token.token, basic(self.other["client_id"], self.other["client_secret"]))
        self.assertEqual(response.json(), {"active": False})
        expired = self.provider.mint_service_token("alpha", self.canvas, 0)
        self.assertEqual((await self.introspect(expired.token)).json(), {"active": False})
        self.assertEqual((await self.introspect("service_unknown")).json(), {"active": False})
        # An agent's own access token is never a service token.
        client = OAuthClientInformationFull(client_id="agent", scope="mcp", token_endpoint_auth_method="none",
                                            redirect_uris=[AnyUrl("https://client.test/callback")])
        await self.provider.register_client(client)
        agent = await self.agent_token(client, "alpha")
        self.assertEqual((await self.introspect(agent)).json(), {"active": False})
        response = await self.http.post("/introspect", data={"nottoken": "x"}, headers={
            "Authorization": basic(self.canvas["client_id"], self.canvas["client_secret"])})
        self.assertEqual(response.status_code, 400)

    async def agent_token(self, client, pond, resource=None):
        resource = resource or f"https://server.test/mcp/{pond}"
        url = await self.provider.authorize(client, AuthorizationParams(
            state="s", scopes=["mcp"], code_challenge="test-challenge",
            redirect_uri=AnyUrl("https://client.test/callback"),
            redirect_uri_provided_explicitly=True, resource=resource))
        request = parse_qs(urlparse(url).query)["request"][0]
        callback = self.provider.approve(request, pond + "-test-secret", pond)
        code = await self.provider.load_authorization_code(client, parse_qs(urlparse(callback).query)["code"][0])
        return (await self.provider.exchange_authorization_code(client, code)).access_token

    async def test_call_tokens_are_reused_then_renewed(self):
        now = [1000.0]
        tokens = ServiceTokens(self.provider, clock=lambda: now[0])
        first = tokens.token("alpha", self.canvas)
        expires = tokens.cached[("alpha", self.canvas["client_id"])].expires_at
        now[0] = expires - SERVICE_TOKEN_RENEW_SECONDS - 1
        self.assertEqual(tokens.token("alpha", self.canvas), first)
        self.assertNotEqual(tokens.token("beta", self.canvas), first)
        now[0] = expires - SERVICE_TOKEN_RENEW_SECONDS
        renewed = tokens.token("alpha", self.canvas)
        self.assertNotEqual(renewed, first)
        self.assertEqual((await self.introspect(renewed)).json()["sub"], "alpha")

    async def test_endpoint_change_renews_cached_audience_without_restarting(self):
        import os
        from boring_crocodile.oauth_admin import OAuthAdmin
        tokens = ServiceTokens(self.provider)
        old = tokens.token("alpha", self.canvas)
        admin = OAuthAdmin(self.provider.database_file, os.getuid())
        plan = admin.service_endpoint("canvas", "https://new-canvas.test/mcp")
        admin.service_endpoint("canvas", plan["after"], plan["plan_digest"])
        grant = self.provider.service("canvas")
        self.assertEqual(self.provider.introspect(grant, old), {"active": False})
        new = tokens.token("alpha", grant)
        self.assertNotEqual(new, old)
        self.assertEqual(self.provider.introspect(grant, new)["aud"], plan["after"])
        self.assertEqual(tokens.token("alpha", grant), new)

    async def test_tools_mint_a_token_for_the_pond_never_passing_the_agents(self):
        client = OAuthClientInformationFull(client_id="agent", scope="mcp", token_endpoint_auth_method="none",
                                            redirect_uris=[AnyUrl("https://client.test/callback")])
        await self.provider.register_client(client)
        agent = await self.agent_token(client, "alpha")
        for name, arguments in (("service_describe", {"service": "canvas"}),
                                ("service_call", {"service": "canvas", "tool": "draw", "arguments": {"x": 1}}),
                                ("service_call", {"service": "canvas", "tool": "draw", "arguments": {}})):
            result = await self.call(agent, name, arguments)
            self.assertFalse(result.get("isError"), result)
        sent = self.requests["alpha"]
        self.assertEqual([request["operation"] for request in sent],
                         ["service_describe", "service_call", "service_call"])
        self.assertEqual((sent[1]["tool"], sent[1]["arguments"]), ("draw", {"x": 1}))
        self.assertEqual(len({request["token"] for request in sent}), 1)  # reused
        self.assertNotEqual(sent[0]["token"], agent)
        self.assertEqual((await self.introspect(sent[0]["token"])).json()["sub"], "alpha")
        # Not granted to beta, or not a service at all: not_found, nothing forwarded.
        beta = await self.agent_token(client, "beta")
        for token, service in ((beta, "other"), (agent, "ghost")):
            result = await self.call(token, "service_call", {"service": service, "tool": "x", "arguments": {}})
            self.assertTrue(result["isError"])
            self.assertIn('"code":"not_found"', result["content"][0]["text"])
        # Only the worker's registry hears of it: forget, never a call.
        self.assertEqual({request["operation"] for request in self.requests["beta"]}, {"service_forget"})

    async def test_list_shows_only_granted_services_and_forgets_the_rest(self):
        client = OAuthClientInformationFull(client_id="agent", scope="mcp", token_endpoint_auth_method="none",
                                            redirect_uris=[AnyUrl("https://client.test/callback")])
        await self.provider.register_client(client)
        for pond, shown, forgotten in (("alpha", ["canvas", "other"], ["gone"]),
                                       ("beta", ["canvas"], ["other", "gone"])):
            with self.subTest(pond=pond):
                result = await self.call(await self.agent_token(client, pond), "service_list", {})
                listed = result["structuredContent"]["services"]
                self.assertEqual([entry["name"] for entry in listed], shown)
                self.assertEqual([request["name"] for request in self.requests[pond]
                                  if request["operation"] == "service_forget"], forgotten)

    async def call(self, token, name, arguments):
        # The token works only on the /mcp/<pond> path it was issued for.
        path = urlparse((await self.provider.load_access_token(token)).resource).path
        response = await self.http.post(path, headers={
            "Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-03-26"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": name, "arguments": arguments}})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["result"]
