import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import unittest
from boring_crocodile.worker_client import WorkerClient
from boring_crocodile.outcomes import FailureReport, RefusalError

class TransportBounds(unittest.IsolatedAsyncioTestCase):
    async def test_permission_failure_means_no_request_sent(self):
        client = WorkerClient({"test": SimpleNamespace(socket_path="/synthetic/socket")})
        with patch("asyncio.open_unix_connection", AsyncMock(side_effect=PermissionError)):
            with pytest.raises(RefusalError, match="nothing was sent"):
                await client.request("test", "limits")

    async def test_invalid_response_frames_have_bad_reply_and_unknown_effects(self):
        import struct
        import tempfile
        from pathlib import Path
        from boring_worker_kit.framing import MAX_FRAME_BYTES, read_frame
        for frame in (struct.pack("!I", 1) + b"{", struct.pack("!I", MAX_FRAME_BYTES + 1)):
            with self.subTest(frame=frame), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "worker.sock"
                async def handle(reader, writer):
                    try:
                        await read_frame(reader)
                        writer.write(frame)
                        await writer.drain()
                    finally:
                        writer.close()
                        await writer.wait_closed()
                server = await asyncio.start_unix_server(handle, path)
                try:
                    client = WorkerClient({"test": SimpleNamespace(socket_path=path)})
                    with self.assertRaises(FailureReport) as caught:
                        await client.request("test", "limits")
                    self.assertEqual(caught.exception.data["status"], "failed")
                    self.assertEqual(caught.exception.code, "bad_reply")
                    self.assertEqual(caught.exception.data["effects"], "unknown")
                finally:
                    server.close()
                    await server.wait_closed()

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
