"""Token/client reads never take a writer lock; SDK writes do not block asyncio."""
import asyncio
import sqlite3
import unittest
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
import test_service_routes as fixtures


class OAuthContentionTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ServiceRoutesTest.asyncSetUp
    start_worker = fixtures.ServiceRoutesTest.start_worker

    def client(self, name):
        return OAuthClientInformationFull(client_id=name, scope="mcp",
            token_endpoint_auth_method="none",
            redirect_uris=[AnyUrl("https://client.test/callback")])

    async def test_lookup_succeeds_while_another_connection_holds_writer_lock(self):
        await self.provider.register_client(self.client("saved"))
        blocker = sqlite3.connect(self.settings.oauth_database_file, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            result = await asyncio.wait_for(self.provider.get_client("saved"), .2)
            self.assertEqual(result.client_id, "saved")
        finally:
            blocker.close()

    async def test_waiting_sdk_writer_leaves_event_loop_responsive(self):
        blocker = sqlite3.connect(self.settings.oauth_database_file, isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        pending = asyncio.create_task(self.provider.register_client(self.client("next")))
        try:
            await asyncio.sleep(.02)
            self.assertFalse(pending.done())
            blocker.execute("ROLLBACK")
            await asyncio.wait_for(pending, 1)
        finally:
            blocker.close()
            if not pending.done():
                pending.cancel()
