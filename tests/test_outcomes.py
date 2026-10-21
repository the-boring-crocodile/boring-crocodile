"""Refusals are data the caller can act on; everything else is a failure."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError

from boring_worker_kit.execution import EnvironmentExecutor
from boring_worker_kit.file_policy import FilePolicy
from boring_worker_kit.framing import read_frame, write_frame
from boring_worker_kit.host_limits import HostLimits
from boring_crocodile.jobs import ManagedJobs
from boring_crocodile.outcomes import OUR_CODES, FailureReport, Refusal, RefusalError, refuse
from boring_worker_kit.process import run_process
from boring_crocodile.settings import EnvironmentSettings
from boring_crocodile.worker import WorkerServer
from boring_crocodile.worker_client import WorkerClient, _response_result


class RefusalDataTest(unittest.TestCase):
    def test_refusal_data_and_code_shape(self):
        refusal = Refusal("stale", "stale edit: file changed", current_sha256=None)
        self.assertEqual(refusal.as_data(), {"code": "stale", "message": "stale edit: file changed",
                                             "current_sha256": None})
        self.assertEqual(refusal.code, "stale")
        # Downstream codes pass through; only the shape is checked.
        self.assertEqual(Refusal("idempotency_conflict", "x").code, "idempotency_conflict")
        for bad in ("", "Busy", "9lives", "has-dash", "x" * 65):
            with self.subTest(code=bad), self.assertRaises(ValueError):
                Refusal(bad, "message")
        self.assertEqual(set(OUR_CODES), {"invalid_request", "not_found", "forbidden",
                                          "stale", "busy", "limit", "not_admitted",
                                          "unavailable"})

    def test_refusal_error_text_is_compact_json_with_the_code(self):
        error = refuse("forbidden", "This connection cannot access that environment")
        self.assertIsInstance(error, ToolError)
        self.assertEqual(str(error), '{"code":"forbidden","message":"This connection cannot access that environment"}')
        self.assertEqual(json.loads(str(error))["code"], "forbidden")
        self.assertEqual(error.code, "forbidden")
        self.assertEqual(json.loads(str(RefusalError({"code": "busy", "message": "ünïcode"})))["message"],
                         "ünïcode")

    def test_worker_response_is_exactly_one_shape(self):
        self.assertEqual(_response_result({"ok": True, "result": {"x": 1}}), {"x": 1})
        with self.assertRaises(RefusalError) as caught:
            _response_result({"ok": False, "refusal": {"code": "busy", "message": "later", "n": 1}})
        self.assertEqual(caught.exception.data, {"code": "busy", "message": "later", "n": 1})
        with self.assertRaises(FailureReport) as failed:
            _response_result({"ok": False, "failure": "ValueError: boom"})
        # Reported with its explanation, never as a refusal.
        self.assertEqual(json.loads(str(failed.exception)), {"failure": "ValueError: boom"})
        self.assertNotIsInstance(failed.exception, RefusalError)
        for bad in ({"ok": False, "error": "x", "error_code": "busy"},
                    {"ok": False, "refusal": {"code": "busy"}},
                    {"ok": False, "refusal": {"code": "Bad Code", "message": "x"}},
                    {"ok": False, "refusal": {"code": "busy", "message": "x"}, "failure": "y"},
                    {"ok": True, "result": []}, {"ok": True}, {}):
            with self.subTest(bad=bad), self.assertRaisesRegex(FailureReport, "invalid worker response"):
                _response_result(bad)

    def test_unreachable_worker_is_a_reported_failure(self):
        settings = type("S", (), {"socket_path": Path("/nonexistent/worker.sock")})()
        client = WorkerClient({"pond": settings})
        with self.assertRaises(FailureReport) as caught:
            asyncio.run(client.request("pond", "limits"))
        self.assertIn("nothing was sent", json.loads(str(caught.exception))["failure"])


class WorkerBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "files").mkdir()
        policy = FilePolicy.parse({"roots": {"work": {"path": str(self.root / "files"),
                                                      "read": True, "write": True}}})
        self.settings = EnvironmentSettings("test", "Test", self.root / "worker.sock", self.root,
                                            (self.root,), os.getuid(), 128,
                                            command_execution_enabled=False, files=policy)
        self.worker = WorkerServer(self.settings)
        self.server = await asyncio.start_unix_server(self.worker.handle, self.settings.socket_path)
        self.client = WorkerClient({"test": self.settings})

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        self.worker.files.pool.shutdown(wait=True)

    async def raw(self, request):
        reader, writer = await asyncio.open_unix_connection(self.settings.socket_path)
        try:
            await write_frame(writer, request)
            return await read_frame(reader)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_a_bug_raising_value_error_is_a_failure_not_a_refusal(self):
        def bug(operation, request):
            raise ValueError("internal invariant broken")
        with patch.object(self.worker.files, "_call", bug), \
                self.assertLogs("boring_crocodile.worker", "ERROR"):
            raw = await self.raw({"operation": "file_read", "root": "work", "path": "x"})
        self.assertEqual(raw, {"ok": False, "failure": "ValueError: internal invariant broken"})
        with patch.object(self.worker.files, "_call", bug), \
                self.assertLogs("boring_crocodile.worker", "ERROR"), \
                self.assertRaisesRegex(RuntimeError, "internal invariant broken") as caught:
            await self.client.file_call("test", "file_read", root="work", path="x")
        # The caller gets the failure with its explanation, never as a refusal.
        self.assertIsInstance(caught.exception, FailureReport)
        self.assertNotIsInstance(caught.exception, RefusalError)
        self.assertEqual(json.loads(str(caught.exception)),
                         {"failure": "ValueError: internal invariant broken"})

    async def test_missing_operation_is_refused_and_never_runs_a_command(self):
        self.worker.executor = EnvironmentExecutor(self.settings, HostLimits(1, None))
        with patch.object(self.worker.executor, "execute") as execute:
            for request in ({"command": "touch should-not-exist"},
                            {"operation": None, "command": "true"},
                            {"operation": 7, "command": "true"}):
                with self.subTest(request=request):
                    raw = await self.raw(request)
                    self.assertEqual(raw, {"ok": False, "refusal": {
                        "code": "invalid_request", "message": "operation is required"}})
        execute.assert_not_called()
        self.assertFalse((self.root / "should-not-exist").exists())
        raw = await self.raw({"operation": "nonsense"})
        self.assertEqual(raw["refusal"]["code"], "invalid_request")


class SpawnFailureTest(unittest.IsolatedAsyncioTestCase):
    async def test_spawn_failure_is_a_failure_not_a_result(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "command could not be started") as caught:
                await run_process("true", str(Path(directory) / "absent"), 5, 1024, asyncio.Event())
            self.assertIsInstance(caught.exception.__cause__, OSError)

    async def test_spawn_failure_makes_a_job_failed_not_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = EnvironmentSettings("pond", "Pond", root / "worker.sock", root, (root,),
                                        os.getuid(), 128)
            # The executor runs locally; the job owner needs a task socket configured.
            executor = EnvironmentExecutor(local, HostLimits(1, None))
            jobs = ManagedJobs(replace(local, task_socket_path=root / "task.sock"), executor)
            try:
                started = jobs.start("true", "absent-directory", 5)
                await asyncio.gather(*jobs.running.values(), return_exceptions=True)
                output = jobs.output(started["job_id"])
            finally:
                jobs.store.close()
        self.assertEqual(output["state"], "failed")
        self.assertIn("command could not be started", output["result"]["error"])
        self.assertNotIn("exit_code", output["result"])
