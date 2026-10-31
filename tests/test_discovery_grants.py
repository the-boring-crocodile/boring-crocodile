import asyncio
import unittest
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
import test_service_routes as fixture
from boring_worker_kit.framing import read_frame, write_frame

class DiscoveryGrants(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ServiceRoutesTest.asyncSetUp
    agent_token = fixture.ServiceRoutesTest.agent_token
    call = fixture.ServiceRoutesTest.call

    async def start_worker(self, name):
        async def worker(reader, writer):
            try:
                request = await read_frame(reader)
                entries = [{"name": item, "available": True} for item in ("canvas", "other", "gone")]
                if request["operation"] == "bootstrap":
                    result = {"capabilities": {"services": entries}, "status": "loaded"}
                elif request["operation"] in {"batch", "job_output"}:
                    result = {"results": [{"id": "list", "method": "service_list",
                              "status": "completed", "result": {"services": entries}}]}
                    if request["operation"] == "job_output":
                        result = {"state": "completed", "result": result}
                else:
                    result = {}
                await write_frame(writer, {"ok": True, "result": result})
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_unix_server(worker, self.root / (name + ".sock"))
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)

    async def test_bootstrap_and_batch_show_only_granted_services(self):
        client = OAuthClientInformationFull(client_id="agent", scope="mcp",
            token_endpoint_auth_method="none", redirect_uris=[AnyUrl("https://client.test/callback")])
        await self.provider.register_client(client)
        token = await self.agent_token(client, "beta")
        bootstrap = await self.call(token, "pond_bootstrap", {})
        self.assertFalse(bootstrap.get("isError"), bootstrap)
        self.assertEqual([e["name"] for e in bootstrap["structuredContent"]["capabilities"]["services"]], ["canvas"])
        batch = await self.call(token, "batch", {"operations": [{"id": "list", "method": "service_list"}]})
        self.assertFalse(batch.get("isError"), batch)
        self.assertEqual([e["name"] for e in batch["structuredContent"]["results"][0]["result"]["services"]], ["canvas"])
        output = await self.call(token, "job_output", {"job_id": "a" * 32})
        self.assertFalse(output.get("isError"), output)
        self.assertEqual([e["name"] for e in output["structuredContent"]["result"]["results"][0]["result"]["services"]], ["canvas"])


class BatchServiceTokens(unittest.IsolatedAsyncioTestCase):
    """The control plane mints a token per service_call step, after validating the
    batch and before the worker sees it; the tokens travel beside the steps."""
    asyncSetUp = fixture.ServiceRoutesTest.asyncSetUp
    agent_token = fixture.ServiceRoutesTest.agent_token
    call = fixture.ServiceRoutesTest.call

    async def start_worker(self, name):
        self.seen = getattr(self, "seen", [])

        async def worker(reader, writer):
            try:
                request = await read_frame(reader)
                self.seen.append(request)
                result = {"results": []} if request["operation"] == "batch" else {
                    "capabilities": {"services": []}, "status": "loaded"}
                await write_frame(writer, {"ok": True, "result": result})
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_unix_server(worker, self.root / (name + ".sock"))
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)

    async def token_for(self, pond):
        client = OAuthClientInformationFull(client_id="agent", scope="mcp",
            token_endpoint_auth_method="none", redirect_uris=[AnyUrl("https://client.test/callback")])
        await self.provider.register_client(client)
        return await self.agent_token(client, pond)

    async def test_a_granted_service_step_gets_a_token_beside_the_steps(self):
        token = await self.token_for("beta")
        step = {"id": "push", "method": "service_call",
                "arguments": {"service": "canvas", "tool": "push_scene", "arguments": {"name": "x"}}}
        result = await self.call(token, "batch", {"operations": [step]})
        self.assertFalse(result.get("isError"), result)
        (sent,) = [r for r in self.seen if r["operation"] == "batch"]
        self.assertEqual(list(sent["service_tokens"]), ["push"])
        self.assertTrue(sent["service_tokens"]["push"])
        self.assertNotIn("token", sent["batch"]["operations"][0]["arguments"])  # never inside the steps
        plain = await self.call(token, "batch", {"operations": [{"id": "list", "method": "service_list"}]})
        self.assertFalse(plain.get("isError"), plain)
        self.assertNotIn("service_tokens", [r for r in self.seen if r["operation"] == "batch"][-1])

    async def test_an_ungranted_service_refuses_the_whole_batch_before_the_worker(self):
        token = await self.token_for("beta")
        steps = [{"id": "list", "method": "service_list"},
                 {"id": "call", "method": "service_call", "arguments": {"service": "other", "tool": "x"}}]
        result = await self.call(token, "batch", {"operations": steps})
        self.assertTrue(result.get("isError"), result)
        self.assertEqual([r for r in self.seen if r["operation"] == "batch"], [])

    async def test_clients_cannot_bring_tokens_or_put_service_steps_in_jobs(self):
        token = await self.token_for("beta")
        smuggled = {"id": "push", "method": "service_call",
                    "arguments": {"service": "canvas", "tool": "x", "token": "mine"}}
        result = await self.call(token, "batch", {"operations": [smuggled]})
        self.assertTrue(result.get("isError"), result)
        job = await self.call(token, "job_start", {"operations": [
            {"id": "push", "method": "service_call", "arguments": {"service": "canvas", "tool": "x"}}]})
        self.assertTrue(job.get("isError"), job)
        self.assertEqual([r for r in self.seen if r["operation"] in ("batch", "job_start")], [])


class ServiceTokenLife(unittest.TestCase):
    def test_one_renewal_rule_covers_a_batch(self):
        from types import SimpleNamespace
        from boring_worker_kit.deadlines import REPLY_MARGIN_SECONDS
        from boring_worker_kit.host_limits import FOREGROUND_REQUEST_SECONDS
        from boring_crocodile.control_plane import SERVICE_TOKEN_RENEW_SECONDS, SERVICE_TOKEN_SECONDS, ServiceTokens
        now = [1000.0]
        minted = []

        class Provider:
            def mint_service_token(self, pond, granted, seconds):
                minted.append(now[0])
                return SimpleNamespace(token=f"t{len(minted)}", resource=granted["url"], expires_at=now[0] + seconds)
        tokens = ServiceTokens(Provider(), clock=lambda: now[0])
        granted = {"client_id": "canvas", "url": "https://svc/mcp"}
        self.assertEqual(tokens.token("beta", granted), "t1")
        now[0] += SERVICE_TOKEN_SECONDS - SERVICE_TOKEN_RENEW_SECONDS - 1  # just over the renewal floor left
        self.assertEqual(tokens.token("beta", granted), "t1")
        now[0] += 2
        self.assertEqual(tokens.token("beta", granted), "t2")  # renewed
        # Whatever a reused token has left outlives any foreground request.
        self.assertGreater(SERVICE_TOKEN_RENEW_SECONDS, FOREGROUND_REQUEST_SECONDS + REPLY_MARGIN_SECONDS)
