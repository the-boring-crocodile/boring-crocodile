import asyncio
from contextlib import asynccontextmanager
import unittest
from async_lifespan import start_lifespan

class LifespanOwnershipTest(unittest.IsolatedAsyncioTestCase):
    async def test_context_keeps_one_task_across_fixture_phases(self):
        entered = []
        exited = []
        @asynccontextmanager
        async def context():
            entered.append(asyncio.current_task())
            try:
                yield
            finally:
                exited.append(asyncio.current_task())
        class Fixture:
            def addAsyncCleanup(self, callback):
                self.close = callback
        fixture = Fixture()
        await start_lifespan(fixture, context())
        self.assertNotEqual(entered[0], asyncio.current_task())
        # A separate teardown task models unittest's newer async lifecycle.
        await asyncio.create_task(fixture.close())
        self.assertEqual(entered, exited)
