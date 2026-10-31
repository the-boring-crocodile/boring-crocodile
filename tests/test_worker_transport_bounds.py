import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import unittest
from boring_crocodile.worker_client import WorkerClient
from boring_crocodile.outcomes import FailureReport

class TransportBounds(unittest.IsolatedAsyncioTestCase):
    async def test_permission_failure_means_no_request_sent(self):
        client = WorkerClient({"test": SimpleNamespace(socket_path="/synthetic/socket")})
        with patch("asyncio.open_unix_connection", AsyncMock(side_effect=PermissionError)):
            with pytest.raises(FailureReport, match="nothing was sent"):
                await client.request("test", "limits")

    async def test_silent_worker_has_a_bounded_unknown_outcome(self):
        client = WorkerClient({"test": SimpleNamespace(socket_path="/synthetic/socket")})
        writer = SimpleNamespace(close=lambda: None, wait_closed=AsyncMock())
        async def silent(*args):
            await asyncio.Event().wait()
        with patch("asyncio.open_unix_connection", AsyncMock(return_value=(object(), writer))), \
             patch("boring_crocodile.worker_client.write_frame", AsyncMock()), \
             patch("boring_crocodile.worker_client.read_frame", silent), \
             patch("boring_crocodile.worker_client.REPLY_MARGIN_SECONDS", 0):
            with pytest.raises(FailureReport, match="may have run"):
                await asyncio.wait_for(client.request("test", "limits", budget=.03), .3)
        writer.wait_closed.assert_awaited()
