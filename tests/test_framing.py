import asyncio
import json
import struct
import tempfile
import unittest

from boring_worker_kit.execution import prepare_command
from boring_worker_kit.framing import MAX_COMMAND_BYTES, MAX_FRAME_BYTES, read_frame, write_frame
from boring_worker_kit.host_limits import HostLimits
from boring_crocodile.settings import EnvironmentSettings
from boring_crocodile.outcomes import Refusal


class FramingBudgetTest(unittest.IsolatedAsyncioTestCase):
    async def test_largest_admitted_escaped_command_and_outputs_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = EnvironmentSettings.from_mapping("pond", dict(
                allowed_roots=[directory], socket_path=directory + "/worker.sock",
                default_cwd=directory, control_plane_uid=1000, output_limit_bytes=1048576))
            command = "\x01" * MAX_COMMAND_BYTES
            prepare_command(settings, command, None, 1, HostLimits(1, 90))
            # Invalid UTF-8 output decodes into replacement characters, which JSON escapes.
            output = (b"\xff" * settings.output_limit_bytes).decode(errors="replace")
            result = dict(command=command, cwd="/a" * 2000, stdout=output, stderr=output,
                          exit_code=0, cleanup=dict(complete=True))
            reader = asyncio.StreamReader()
            class Writer:
                def write(self, data):
                    reader.feed_data(data)
                async def drain(self):
                    pass
            await write_frame(Writer(), dict(ok=True, result=result))
            received = await read_frame(reader)
            self.assertEqual(received["result"], result)
            self.assertLessEqual(len(json.dumps(received, separators=(",", ":")).encode()),
                                 MAX_FRAME_BYTES)
            with self.assertRaisesRegex(Refusal, "^limit: command exceeds"):
                prepare_command(settings, command + "x", None, 1, HostLimits(1, 90))

    async def test_malformed_json_contract_and_nonfinite_output(self):
        for payload in (b'null', b'[]', b'{"x":NaN}', b'{"x":Infinity}',
                        b'{"x":1e999}', b'{"x":1,"x":2}'):
            reader = asyncio.StreamReader()
            reader.feed_data(struct.pack("!I", len(payload)) + payload)
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                await read_frame(reader)
        class Writer:
            def write(self, data):
                raise AssertionError("invalid output reached socket")
        with self.assertRaises(ValueError):
            await write_frame(Writer(), {"value": float("nan")})
