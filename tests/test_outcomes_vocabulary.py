"""Public outcome boundaries, including the required Pirate outcome envelope."""
import asyncio
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from boring_outcomes import Failed, Refused
from boring_worker_kit.execution import ForegroundInterrupted
from boring_worker_kit.file_policy import FilePolicy
from boring_worker_kit.framing import read_frame, write_frame
from boring_worker_kit.services import ServiceFailure
from boring_worker_kit.task_client import ContainmentError
from boring_crocodile.outcomes import FailureReport, RefusalError
from boring_crocodile.pirates import answer
from boring_crocodile.settings import EnvironmentSettings
from boring_crocodile.worker import WorkerServer
from boring_crocodile.worker_client import WorkerClient, _response_result



class ControllerCarrierTest(unittest.TestCase):
    def test_direct_carriers_have_matching_text_and_structured_content(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from boring_crocodile.control_plane import _present_tool
        for error in (RefusalError({"status": "refused", "code": "busy", "message": "occupied"}),
                      FailureReport("worker_failed", "reported", effects="unknown")):
            with self.subTest(code=error.code):
                tool = SimpleNamespace(name="fixture", run=AsyncMock(side_effect=error),
                                       fn_metadata=SimpleNamespace(pre_parse_json=lambda value: value))
                _present_tool(tool, SimpleNamespace(model_validate=lambda value: value))
                result = asyncio.run(tool.run({}))
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content, error.data)
                self.assertEqual(json.loads(result.content[0].text), error.data)


class PirateEnvelopeTest(unittest.TestCase):
    def test_new_outcome_keeps_both_statuses_and_details(self):
        for status, cls in (("refused", Refused), ("failed", Failed)):
            data = {"status": status, "code": "provider_problem", "message": "explanation",
                    "vendor_code": "VENDOR", "retry_after_seconds": 3, "nested": {"x": [1, None]}}
            with self.subTest(status=status), self.assertRaises(cls) as caught:
                answer({"ok": False, "outcome": data})
            self.assertEqual(caught.exception.as_data(), data)

    def test_malformed_outcomes_are_bad_reply_not_refusals(self):
        for data in (
            {"status": "other", "code": "busy", "message": "x"},
            {"status": "refused", "code": "UPPER", "message": "x"},
            {"status": "failed", "code": "busy", "message": 3},
            {"status": "refused", "code": "busy", "message": "x", "value": float("nan")},
            {"code": "busy", "message": "missing status"},
        ):
            with self.subTest(data=data), self.assertRaises(Failed) as caught:
                answer({"ok": False, "outcome": data})
            self.assertEqual(caught.exception.code, "bad_reply")


class WorkerOutcomesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = EnvironmentSettings(
            "test", "Test", self.root / "worker.sock", self.root, (self.root,),
            os.getuid(), 128, command_execution_enabled=False,
            files=FilePolicy.parse({"roots": {"work": {"path": str(self.root),
                                                      "read": True, "write": True}}}))
        self.worker = WorkerServer(self.settings)
        self.server = await asyncio.start_unix_server(self.worker.handle, self.settings.socket_path)
        self.client = WorkerClient({"test": self.settings})

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        await self.worker.services.close()
        self.worker.files.pool.shutdown(wait=True)
        self.tmp.cleanup()

    async def raw(self, request):
        reader, writer = await asyncio.open_unix_connection(self.settings.socket_path)
        try:
            await write_frame(writer, request)
            return await read_frame(reader)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_service_error_without_status_becomes_failed_upstream_error(self):
        from mcp.types import CallToolResult, TextContent
        data = {"code": "busy", "message": "old service shape"}
        async def reply(*args, **kwargs):
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(data))], is_error=True)
        with patch.object(self.worker.services, "_available", return_value=object()), \
                patch.object(self.worker.services, "_call", reply):
            with self.assertRaises(FailureReport) as caught:
                await self.client.service_call("test", "fixture", "tool", {}, "token")
        self.assertEqual(caught.exception.data, {"status": "failed", "code": "upstream_error",
                                               "message": json.dumps(data), "effects": "unknown"})

    async def test_worker_and_client_preserve_typed_service_failure(self):
        data = {"status": "failed", "code": "sql_error", "message": "query failed",
                "vendor_code": "SQL-X", "effects": "unknown", "nested": {"a": [2]}}
        async def fail(*args, **kwargs):
            raise ServiceFailure(data)
        with patch.object(self.worker.services, "call", fail):
            with self.assertRaises(FailureReport) as caught:
                await self.client.service_call("test", "fixture", "tool", {}, "token")
        self.assertEqual(caught.exception.data, data)
        self.assertEqual(json.loads(str(caught.exception)), data)

    async def test_worker_failure_codes_are_decided_by_types(self):
        for error, code in ((ForegroundInterrupted(), "foreground_interrupted"),
                            (ContainmentError("cleanup not confirmed"), "cleanup_unconfirmed"),
                            (TimeoutError("late"), "timeout"),
                            (ValueError("invariant"), "worker_failed"),
                            (Failed("vendor_failure", "typed", number=4), "vendor_failure")):
            def fail(*args):
                raise error
            with self.subTest(code=code), patch.object(self.worker.files, "_call", fail):
                raw = await self.raw({"operation": "file_read", "root": "work", "path": "x"})
            self.assertEqual(raw["failure"]["status"], "failed")
            self.assertEqual(raw["failure"]["code"], code)
            self.assertNotIn("refusal", raw)

    async def test_job_wait_response_deadline_is_refused_with_no_effects(self):
        async def never(*args, **kwargs):
            await asyncio.Event().wait()
        with patch.object(self.client, "request", never), \
                patch("boring_crocodile.worker_client.REPLY_MARGIN_SECONDS", 0):
            with self.assertRaises(RefusalError) as caught:
                await self.client.job_wait("test", ["a" * 32], .01)
        self.assertEqual(caught.exception.data["code"], "timeout")
        self.assertEqual(caught.exception.data["effects"], "none")
        self.assertEqual(caught.exception.data["operation"], "job_wait")

    async def test_invalid_worker_details_produce_bad_reply(self):
        for key, status in (("refusal", "refused"), ("failure", "failed")):
            for invalid in (Path("x"), {1}, b"bytes", datetime(2026, 1, 1)):
                with self.subTest(key=key, value=type(invalid).__name__), self.assertRaises(FailureReport) as caught:
                    _response_result({"ok": False, key: {"status": status, "code": "busy",
                                                        "message": "x", "value": invalid}})
                self.assertEqual(caught.exception.code, "bad_reply")

    async def test_prior_job_results_are_returned_unchanged(self):
        from boring_crocodile.jobs import ManagedJobs
        from boring_worker_kit.execution import EnvironmentExecutor
        from boring_worker_kit.host_limits import HostLimits
        jobs = ManagedJobs(replace(self.settings, task_socket_path=self.root / "task.sock"),
                           EnvironmentExecutor(self.settings, HostLimits(1, None)))
        try:
            old = {"error": "old failure", "refusal": {"code": "busy", "message": "old refusal"}}
            job_id = jobs.store.create("queued", {"kind": "command"})[0]
            jobs.store.update(job_id, state="failed", result=old)
            self.assertEqual(jobs.output(job_id)["result"], old)
        finally:
            jobs.store.close()
