"""Service calls and Pirate describe show up in watch: names and timing only."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from boring_worker_kit.framing import read_frame, write_frame
from boring_crocodile.outcomes import Refusal
from boring_crocodile.settings import EnvironmentSettings
from boring_crocodile.worker import WorkerServer


class Recorder:
    made = []

    def __init__(self, settings, task, source, job_id=None):
        self.task, self.events, self.closed = task, [], False
        Recorder.made.append(self)

    def emit(self, event, **fields):
        self.events.append(dict(fields, event=event))

    def close(self):
        self.closed = True


class WorkerObservationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="crocodile-test-")
        root = Path(self.temp.name)
        self.settings = EnvironmentSettings("test", "Test", root / "worker.sock",
                                            root, (root,), os.getuid(), 128)
        self.worker = WorkerServer(self.settings)
        self.server = await asyncio.start_unix_server(self.worker.handle, self.settings.socket_path)
        Recorder.made = []
        self.patch = patch("boring_crocodile.worker.Observation", Recorder)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        self.server.close()
        await self.server.wait_closed()
        await self.worker.services.close()
        self.temp.cleanup()

    async def ask(self, request):
        reader, writer = await asyncio.open_unix_connection(str(self.settings.socket_path))
        await write_frame(writer, request)
        answer = await read_frame(reader)
        writer.close()
        await writer.wait_closed()
        return answer

    async def test_service_call_emits_names_never_arguments_or_token(self):
        async def dispatch(request):
            return {"content": [{"type": "text", "text": "secret result"}]}
        with patch.object(self.worker.services, "dispatch", dispatch):
            answer = await self.ask({"operation": "service_call", "service": "canvas", "tool": "draw",
                                     "arguments": {"svg": "secret argument"}, "token": "secret token"})
        self.assertTrue(answer["ok"])
        [observer] = Recorder.made
        self.assertEqual(observer.task, "service:canvas")
        self.assertTrue(observer.closed)
        self.assertEqual([e["state"] for e in observer.events], ["started", "completed"])
        self.assertEqual(observer.events[1]["tool"], "draw")
        self.assertIn("duration_ms", observer.events[1])
        self.assertNotIn("secret", repr(observer.events))

    async def test_refusal_keeps_its_code_and_stays_the_outcome(self):
        async def describe(pirate, flavour):
            raise Refusal("busy", "Parrot is busy")
        with patch.object(self.worker.pirates, "describe", describe):
            answer = await self.ask({"operation": "pirate_describe", "pirate": "parrot", "flavour": "paint"})
        self.assertEqual(answer["refusal"]["code"], "busy")
        [observer] = Recorder.made
        self.assertEqual(observer.task, "pirate:parrot")
        self.assertEqual(observer.events[-1]["state"], "refused")
        self.assertEqual(observer.events[-1]["code"], "busy")
        self.assertEqual(observer.events[-1]["flavour"], "paint")
        self.assertNotIn("tool", observer.events[-1])

    async def test_failure_is_observed_as_failed(self):
        async def dispatch(request):
            raise RuntimeError("connection reset")
        with patch.object(self.worker.services, "dispatch", dispatch):
            answer = await self.ask({"operation": "service_describe", "service": "canvas", "token": "t"})
        self.assertIn("failure", answer)
        self.assertEqual(Recorder.made[0].events[-1]["state"], "failed")

    async def test_registration_and_listing_stay_unobserved(self):
        await self.ask({"operation": "service_list"})
        await self.ask({"operation": "list_pirates"})
        self.assertEqual(Recorder.made, [])


if __name__ == "__main__":
    unittest.main()
