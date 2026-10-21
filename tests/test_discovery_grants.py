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
