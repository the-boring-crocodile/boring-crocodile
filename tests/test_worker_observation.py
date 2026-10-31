"""Service calls and Pirate describe show up in watch: names and timing only."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from boring_worker_kit.framing import read_frame, write_frame
from boring_crocodile.outcomes import Refusal
from boring_worker_kit.file_policy import FilePolicy
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


class WorkerHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="crocodile-test-")
        root = Path(self.temp.name)
        (root / "work").mkdir()
        self.settings = EnvironmentSettings("test", "Test", root / "worker.sock", root, (root,), os.getuid(), 128,
                                            files=FilePolicy.parse({"roots": {"work": {
                                                "path": str(root / "work"), "read": True, "write": True}}}))
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


class WorkerObservationTest(WorkerHarness):
    async def test_service_call_emits_names_never_arguments_or_token(self):
        async def call(name, tool, arguments, token, **limits):
            return {"content": [{"type": "text", "text": "secret result"}]}
        with patch.object(self.worker.services, "call", call):
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
        async def describe(pirate, flavour, timeout):
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
        async def describe(name, token, **limits):
            raise RuntimeError("connection reset")
        with patch.object(self.worker.services, "describe", describe):
            answer = await self.ask({"operation": "service_describe", "service": "canvas", "token": "t"})
        self.assertIn("failure", answer)
        self.assertEqual(Recorder.made[0].events[-1]["state"], "failed")

    async def test_registration_and_listing_stay_unobserved(self):
        await self.ask({"operation": "service_list"})
        await self.ask({"operation": "list_pirates"})
        self.assertEqual(Recorder.made, [])


class WorkerBudgetTest(WorkerHarness):
    """The worker uses the carried budget_seconds (capped per operation), not only
    the control plane: the time a service gets is what is left of it."""

    async def ask_service(self, operation, budget=None, **extra):
        seen = []

        async def call(name, tool, arguments, token, **limits):
            seen.append(limits.get("timeout"))
            return {"content": [{"type": "text", "text": "ok"}]}

        async def describe(name, token, **limits):
            seen.append(limits.get("timeout"))
            return {"service": name, "tools": []}
        request = {"operation": operation, "service": "canvas", "token": "t", **extra}
        if operation == "service_call":
            request |= {"tool": "draw", "arguments": {}}
        if budget is not None:
            request["budget_seconds"] = budget
        with patch.object(self.worker.services, "call", call), \
                patch.object(self.worker.services, "describe", describe):
            return await self.ask(request), seen

    async def test_the_service_gets_the_carried_budget(self):
        answer, seen = await self.ask_service("service_call", 2)
        self.assertTrue(answer["ok"])
        self.assertLessEqual(seen[0], 2)
        answer, seen = await self.ask_service("service_describe", 3)
        self.assertTrue(answer["ok"])
        self.assertLessEqual(seen[0], 3)
        for absent_or_huge in (None, 10**400, "x"):
            with self.subTest(budget=absent_or_huge):
                answer, seen = await self.ask_service("service_call", absent_or_huge)
                self.assertEqual(seen, [30])  # the cap: never longer than the service contract

    async def test_no_time_left_is_refused_before_the_call(self):
        save_to = {"root": "work", "path": "out.txt", "expected_sha256": None}
        answer, seen = await self.ask_service("service_call", 4, save_to=save_to)
        self.assertEqual((seen, answer["refusal"]["code"]), ([], "limit"))  # 5 s are kept for the save
        answer, seen = await self.ask_service("service_call", 10, save_to=save_to)
        self.assertTrue(answer["ok"])
        self.assertLessEqual(seen[0], 5)
        self.assertEqual((Path(self.temp.name) / "work" / "out.txt").read_text(), "ok")
        answer, seen = await self.ask_service("service_call", 0)
        self.assertEqual((seen, answer["refusal"]["code"]), ([], "limit"))  # zero is no time, not the cap


if __name__ == "__main__":
    unittest.main()
