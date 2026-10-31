"""File references in service calls and save_to, through the worker socket, against
a real in-process MCP service (test_services.ExampleService)."""
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from boring_worker_kit.file_policy import FilePolicy
from boring_worker_kit import file_refs
from boring_crocodile.outcomes import RefusalError
from boring_crocodile.settings import EnvironmentSettings
from boring_crocodile.worker import WorkerServer
from boring_crocodile.worker_client import WorkerClient
from test_services import ExampleService


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class FileReferenceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.work = root / "work"
        self.work.mkdir()
        (root / "notes").mkdir()
        self.service = ExampleService()
        await self.service.start()
        self.addAsyncCleanup(self.service.stop)
        files = FilePolicy.parse({"roots": {
            "work": {"path": str(self.work), "read": True, "write": True},
            "notes": {"path": str(root / "notes"), "read": True, "write": False}}})
        env = EnvironmentSettings("test", "Test", root / "control.sock", root, (root,), os.getuid(),
                                  1024, command_execution_enabled=False, files=files)
        self.worker = WorkerServer(env, pond="alpha")
        self.addAsyncCleanup(self.worker.services.close)
        self.control = await asyncio.start_unix_server(self.worker.handle, env.socket_path)
        self.addAsyncCleanup(self.control.wait_closed)
        self.addCleanup(self.control.close)
        self.client = WorkerClient({"test": env})
        await self.client.service_register("test", "canvas", self.service.url, 1.0)
        await self.worker.services.registry.ping_all()

    async def call(self, arguments, save_to=None, tool="echo"):
        return await self.client.service_call("test", "canvas", tool, arguments, "good", save_to)

    def sent(self):
        """The arguments the service received in its last tools/call."""
        for _, _, body in reversed(self.service.requests):
            message = json.loads(b"".join(body) or b"{}")
            if message.get("method") == "tools/call":
                return message["params"]["arguments"]
        raise AssertionError("no tools/call")

    async def test_a_reference_is_replaced_by_the_files_text(self):
        svg = "<svg>\n  <rect/>\n</svg>\n"
        (self.work / "a.svg").write_text(svg)
        called = await self.call({"text": {"$file": {"root": "work", "path": "a.svg"}}})
        self.assertEqual(called["structuredContent"], {"echo": svg})
        self.assertEqual(self.sent(), {"text": svg})  # the service saw plain text
        pinned = await self.call({"text": {"$file": {"root": "work", "path": "a.svg", "sha256": sha(svg)}}})
        self.assertEqual(pinned["structuredContent"]["echo"], svg)

    async def test_a_long_file_is_read_in_parts(self):
        text = "".join(f"line {n}\n" for n in range(25_000))  # ~240 KB: several byte ranges
        (self.work / "long.txt").write_text(text)
        called = await self.call({"text": {"$file": {"root": "work", "path": "long.txt"}}})
        self.assertEqual(called["structuredContent"]["echo"], text)

    async def test_exact_bytes_long_lines_and_line_ends(self):
        minified = "<svg>" + "<rect/>" * 15_000 + "</svg>"  # one line of ~105 KB (file_read caps lines at 16 KiB)
        (self.work / "min.svg").write_text(minified)
        called = await self.call({"text": {"$file": {"root": "work", "path": "min.svg"}}})
        self.assertEqual(called["structuredContent"]["echo"], minified)
        (self.work / "crlf.txt").write_bytes("\ufeffa\r\nb\r\n".encode())
        called = await self.call({"text": {"$file": {"root": "work", "path": "crlf.txt"}}})
        self.assertEqual(called["structuredContent"]["echo"], "a\r\nb\r\n")  # line ends kept, BOM dropped
        (self.work / "latin.txt").write_bytes("caf\xe9".encode("latin-1"))
        with self.assertRaises(RefusalError) as caught:
            await self.call({"text": {"$file": {"root": "work", "path": "latin.txt"}}})
        self.assertEqual(caught.exception.data["code"], "invalid_request")

    async def test_a_file_that_changes_while_read_is_refused(self):
        (self.work / "moving.txt").write_text("a" * 200_000)
        real = self.worker.files.call
        calls = []

        async def changing(operation, request, **kw):
            result = await real(operation, request, **kw)
            calls.append(operation)
            if operation == "file_read_bytes" and len(calls) == 1:
                (self.work / "moving.txt").write_text("b" * 200_000)
            return result
        with patch.object(self.worker.files, "call", changing), self.assertRaises(RefusalError) as caught:
            await self.call({"text": {"$file": {"root": "work", "path": "moving.txt"}}})
        self.assertEqual(caught.exception.data["code"], "stale")
        self.assertIn("changed while it was read", caught.exception.data["message"])

    async def test_nested_references_and_the_budget_across_them(self):
        (self.work / "a.txt").write_text("0123456789")
        (self.work / "b.txt").write_text("abcdefghij")
        ref = lambda name: {"$file": {"root": "work", "path": name}}
        resolved, used = await file_refs.resolve({"x": [{"y": ref("a.txt")}, 3, "z"], "w": ref("b.txt")},
                                                 self.worker.files)
        self.assertEqual(resolved, {"x": [{"y": "0123456789"}, 3, "z"], "w": "abcdefghij"})
        self.assertEqual([u["path"] for u in used], ["a.txt", "b.txt"])
        with patch.object(file_refs, "MAX_REFERENCE_BYTES", 15), self.assertRaises(file_refs.Refusal) as caught:
            await file_refs.resolve({"x": ref("a.txt"), "w": ref("b.txt")}, self.worker.files)
        self.assertEqual(caught.exception.code, "limit")  # 10 + 10 > 15

    async def test_references_obey_the_file_rules(self):
        (self.work / "a.svg").write_text("x")
        cases = [({"root": "work", "path": "missing.svg"}, "not_found"),
                 ({"root": "nowhere", "path": "a.svg"}, None),
                 ({"root": "work", "path": "../a.svg"}, None),
                 ({"root": "work", "path": "a.svg", "sha256": "0" * 64}, "stale"),
                 ({"root": "work", "path": "a.svg", "extra": "x"}, "invalid_request")]
        before = len(self.service.requests)
        for target, code in cases:
            with self.subTest(target=target), self.assertRaises(RefusalError) as caught:
                await self.call({"text": {"$file": target}})
            if code is not None:
                self.assertEqual(caught.exception.data["code"], code)
        self.assertEqual(len(self.service.requests), before)  # nothing reached the service
        with patch.object(file_refs, "MAX_REFERENCE_BYTES", 10):
            (self.work / "big.txt").write_text("y" * 50)
            with self.assertRaises(RefusalError) as caught:
                await self.call({"text": {"$file": {"root": "work", "path": "big.txt"}}})
            self.assertEqual(caught.exception.data["code"], "limit")

    async def test_other_values_pass_unchanged(self):
        value = {"$file": "not a reference", "x": [{"$file": 1, "y": 2}]}
        called = await self.call({"text": json.dumps(value)})
        self.assertEqual(json.loads(called["structuredContent"]["echo"]), value)
        with self.assertRaises(RefusalError) as caught:  # the exact marker with a bad target is refused
            await self.call({"text": {"$file": "a.svg"}})
        self.assertEqual(caught.exception.data["code"], "invalid_request")

    async def test_save_to_writes_the_answer_and_returns_a_receipt(self):
        (self.work / "in.txt").write_text("from a file")
        receipt = await self.call({"text": {"$file": {"root": "work", "path": "in.txt"}}},
                                  {"root": "work", "path": "out.json", "expected_sha256": None})
        saved = (self.work / "out.json").read_text()
        self.assertEqual(json.loads(saved)["structuredContent"], {"echo": "from a file"})
        self.assertEqual(receipt["saved_to"]["sha256"], sha(saved))
        self.assertEqual((receipt["saved_to"]["root"], receipt["saved_to"]["path"]), ("work", "out.json"))
        self.assertEqual(receipt["file_references"], [{"root": "work", "path": "in.txt", "bytes": 11}])
        self.assertNotIn("content", receipt)  # the answer did not come back
        (self.work / "sub").mkdir()
        receipt = await self.call({"text": "deep"}, {"root": "work", "path": "sub/deep.json", "expected_sha256": None})
        self.assertEqual(receipt["saved_to"]["path"], "sub/deep.json")
        # replacing needs the file's sha256
        receipt = await self.call({"text": "second"}, {"root": "work", "path": "out.json",
                                                       "expected_sha256": sha(saved)})
        self.assertEqual(json.loads((self.work / "out.json").read_text())["structuredContent"], {"echo": "second"})

    async def test_a_stale_or_forbidden_target_is_refused_before_the_call(self):
        (self.work / "out.txt").write_text("keep me")
        before = len(self.service.requests)
        for save_to, code in (({"root": "work", "path": "out.txt", "expected_sha256": None}, "stale"),
                              ({"root": "work", "path": "new.txt", "expected_sha256": "0" * 64}, "stale"),
                              ({"root": "work", "path": "x.txt"}, "invalid_request"),
                              ({"root": "work", "path": "no/such/dir/x.txt", "expected_sha256": None}, "not_found")):
            with self.subTest(save_to=save_to), self.assertRaises(RefusalError) as caught:
                await self.call({"text": "x"}, save_to)
            self.assertEqual(caught.exception.data["code"], code)
        with self.assertRaises(RefusalError):  # read-only root
            await self.call({"text": "x"}, {"root": "notes", "path": "n.txt", "expected_sha256": None})
        self.assertEqual(len(self.service.requests), before)  # the service was never called
        self.assertEqual((self.work / "out.txt").read_text(), "keep me")

    async def test_an_answer_is_never_lost_when_the_write_fails_after_the_call(self):
        real = self.worker.files.call

        async def refuse_writes(operation, request, **kw):
            if operation == "file_write":
                (self.work / "late.txt").write_text("someone was faster")
                return await real(operation, request, **kw)
            return await real(operation, request, **kw)
        with patch.object(self.worker.files, "call", refuse_writes):
            result = await self.call({"text": "precious"}, {"root": "work", "path": "late.txt",
                                                              "expected_sha256": None})
        self.assertEqual(result["structuredContent"], {"echo": "precious"})
        self.assertEqual(result["save_to_refused"]["code"], "stale")

    async def test_an_os_error_on_save_returns_the_answer(self):
        real = self.worker.files.call

        async def disk_full(operation, request, **kw):
            if operation == "file_write":
                raise OSError(28, "No space left on device")
            return await real(operation, request, **kw)
        with patch.object(self.worker.files, "call", disk_full):
            result = await self.call({"text": "precious"}, {"root": "work", "path": "full.txt", "expected_sha256": None})
        self.assertEqual(result["structuredContent"], {"echo": "precious"})
        self.assertEqual(result["save_to_failed"]["effects"], "unknown")  # a failure, not a refusal
        self.assertIn("No space left", result["save_to_failed"]["message"])
        with patch.object(self.worker.files, "call", disk_full), patch.object(file_refs, "MAX_INLINE_ANSWER", 10):
            result = await self.call({"text": "precious"}, {"root": "work", "path": "full.txt", "expected_sha256": None})
        self.assertNotIn("structuredContent", result)  # too large to return: said so, not a generic failure
        self.assertTrue(result["service_ran"])
        self.assertGreater(result["answer_bytes"], 10)

    async def test_invalid_service_file_work_refuses_the_whole_batch(self):
        cases = [
            ({"arguments": {"text": "x"}, "save_to": {
                "root": "notes", "path": "no.txt", "expected_sha256": None}}, "forbidden"),
            ({"arguments": {"text": {"$file": {"root": "foreign", "path": "x.txt"}}}}, "forbidden"),
            ({"arguments": {"text": {"$file": "not-an-object"}}}, "invalid_request"),
            ({"arguments": {"text": [{"$file": {"root": "work", "path": "later.txt"}}] * 17}}, "limit"),
            ({"arguments": {"text": "x"}, "save_to": {
                "root": "work", "path": "no.txt", "expected_sha256": "bad"}}, "invalid_request"),
        ]
        for index, (extra, code) in enumerate(cases):
            with self.subTest(code=code, case=index):
                marker = f"earlier-{index}.txt"
                before = len(self.service.requests)
                batch = {"operations": [
                    {"id": "write", "method": "file_write", "arguments": {
                        "root": "work", "path": marker, "content": "earlier effect", "expected_sha256": None}},
                    {"id": "bad", "method": "service_call", "arguments": {
                        "service": "canvas", "tool": "echo", **extra}},
                ]}
                with self.assertRaises(RefusalError) as caught:
                    await self.client.batch("test", batch, {"bad": "good"})
                self.assertEqual(caught.exception.data["code"], code)
                self.assertFalse((self.work / marker).exists())
                self.assertEqual(len(self.service.requests), before)

    async def test_a_batch_chains_files_and_service_steps(self):
        batch = {"operations": [
            {"id": "write", "method": "file_write",
             "arguments": {"root": "work", "path": "in.txt", "content": "made in step 1", "expected_sha256": None}},
            {"id": "push", "method": "service_call",
             "arguments": {"service": "canvas", "tool": "echo",
                           "arguments": {"text": {"$file": {"root": "work", "path": "in.txt"}}},
                           "save_to": {"root": "work", "path": "echo.json", "expected_sha256": None}}},
            {"id": "read", "method": "file_read", "arguments": {"root": "work", "path": "echo.json"}}]}
        result = await self.client.batch("test", batch, {"push": "good"})
        statuses = {entry["id"]: entry["status"] for entry in result["results"]}
        self.assertEqual(statuses, {"write": "succeeded", "push": "succeeded", "read": "succeeded"})
        push, read = result["results"][1]["result"], result["results"][2]["result"]
        self.assertEqual(push["saved_to"]["path"], "echo.json")  # a receipt, not the answer
        self.assertEqual(json.loads(read["content"])["structuredContent"], {"echo": "made in step 1"})
        self.assertNotIn("good", json.dumps(result))  # the token never comes back

    async def test_a_slow_save_in_a_batch_keeps_the_answer(self):
        # The step ends inside the batch deadline, so a save still running is reported by
        # the step (answer + save_to_failed), not cut off as "batch deadline".
        import threading
        from boring_crocodile import worker as worker_module
        release, real = threading.Event(), self.worker.files._file_call

        def slow_write(operation, request):
            if operation == "file_write":
                release.wait(5)
            return real(operation, request)
        batch = {"timeout_seconds": 2, "operations": [
            {"id": "push", "method": "service_call",
             "arguments": {"service": "canvas", "tool": "echo", "arguments": {"text": "kept"},
                           "save_to": {"root": "work", "path": "slow.json", "expected_sha256": None}}}]}
        try:
            with patch.object(worker_module, "REPLY_MARGIN_SECONDS", 0.5), \
                    patch.object(file_refs, "REPLY_MARGIN_SECONDS", 0.5), \
                    patch.object(self.worker.files, "_file_call", slow_write):
                result = await self.client.batch("test", batch, {"push": "good"})
        finally:
            release.set()
        [push] = result["results"]
        # The answer is kept, but a save that may not have landed is not a success
        # (review finding F1): the step fails with the save's code and stop applies.
        self.assertEqual((push["status"], push["code"]), ("failed", "save_to_failed"))
        self.assertEqual(push["result"]["structuredContent"], {"echo": "kept"})
        self.assertEqual(push["result"]["save_to_failed"]["effects"], "unknown")
        self.assertEqual(result["stop_reason"], "step_failed")

    def _save_then_write(self, on_error):
        return {"on_error": on_error, "operations": [
            {"id": "push", "method": "service_call",
             "arguments": {"service": "canvas", "tool": "echo", "arguments": {"text": "kept"},
                           "save_to": {"root": "work", "path": "answer.json", "expected_sha256": None}}},
            {"id": "after", "method": "file_write",
             "arguments": {"root": "work", "path": "after.txt", "content": "x", "expected_sha256": None}}]}

    def _disk_full_on(self, name):
        real = self.worker.files._file_call

        def full(operation, request):
            if operation == "file_write" and request.get("path") == name:
                raise OSError(28, "No space left on device")
            return real(operation, request)
        return patch.object(self.worker.files, "_file_call", full)

    async def test_a_failed_save_stops_the_batch_before_a_dependent_step(self):
        with self._disk_full_on("answer.json"):
            result = await self.client.batch("test", self._save_then_write("stop"), {"push": "good"})
        push, after = result["results"]
        self.assertEqual((push["status"], push["code"]), ("failed", "save_to_failed"))
        self.assertEqual(push["result"]["structuredContent"], {"echo": "kept"})  # the call ran: answer kept
        self.assertNotIn("refusal", push)
        self.assertEqual(after["status"], "not_started")
        self.assertEqual(result["stop_reason"], "step_failed")
        self.assertFalse((self.work / "after.txt").exists())

    async def test_a_failed_save_with_continue_runs_later_steps_and_stays_failed(self):
        with self._disk_full_on("answer.json"):
            result = await self.client.batch("test", self._save_then_write("continue"), {"push": "good"})
        push, after = result["results"]
        self.assertEqual((push["status"], push["code"]), ("failed", "save_to_failed"))
        self.assertEqual(after["status"], "succeeded")
        self.assertIsNone(result["stop_reason"])
        self.assertTrue((self.work / "after.txt").exists())

    async def test_a_target_changed_while_the_service_ran_is_a_refused_save_and_a_failed_step(self):
        (self.work / "answer.json").write_text("old")
        import hashlib
        old = hashlib.sha256(b"old").hexdigest()
        real = self.worker.files._file_call

        def meddle(operation, request):
            if operation == "file_write" and request.get("path") == "answer.json":
                (self.work / "answer.json").write_text("changed meanwhile")
            return real(operation, request)
        batch = self._save_then_write("stop")
        batch["operations"][0]["arguments"]["save_to"]["expected_sha256"] = old
        with patch.object(self.worker.files, "_file_call", meddle):
            result = await self.client.batch("test", batch, {"push": "good"})
        push, after = result["results"]
        self.assertEqual((push["status"], push["code"]), ("failed", "save_to_refused"))
        self.assertEqual(push["result"]["save_to_refused"]["code"], "stale")
        self.assertEqual(push["result"]["structuredContent"], {"echo": "kept"})
        self.assertEqual(after["status"], "not_started")
        self.assertEqual((self.work / "answer.json").read_text(), "changed meanwhile")

    async def test_a_saved_answer_is_still_a_succeeded_step(self):
        result = await self.client.batch("test", self._save_then_write("stop"), {"push": "good"})
        push, after = result["results"]
        self.assertEqual((push["status"], after["status"]), ("succeeded", "succeeded"))
        self.assertNotIn("code", push)
        self.assertIn("saved_to", push["result"])

    async def test_a_refused_service_step_stops_the_batch(self):
        batch = {"operations": [
            {"id": "call", "method": "service_call", "arguments": {"service": "canvas", "tool": "refuse"}},
            {"id": "after", "method": "file_write",
             "arguments": {"root": "work", "path": "after.txt", "content": "x", "expected_sha256": None}}]}
        result = await self.client.batch("test", batch, {"call": "good"})
        self.assertEqual([e["status"] for e in result["results"]], ["refused", "not_started"])
        self.assertEqual(result["results"][0]["refusal"]["code"], "quota_exceeded")
        self.assertFalse((self.work / "after.txt").exists())

    async def test_a_service_timeout_in_a_batch_is_the_steps_not_the_batchs(self):
        from boring_worker_kit import services
        batch = {"on_error": "continue", "operations": [
            {"id": "slow", "method": "service_call", "arguments": {"service": "canvas", "tool": "slow",
                                                                    "arguments": {"seconds": 1}}},
            {"id": "after", "method": "file_write",
             "arguments": {"root": "work", "path": "after.txt", "content": "x", "expected_sha256": None}}]}
        with patch.object(services, "CALL_TIMEOUT_SECONDS", 0.2):
            result = await self.client.batch("test", batch, {"slow": "good"})
        self.assertEqual([e["status"] for e in result["results"]], ["unknown", "succeeded"])
        self.assertIn("may have run", result["results"][0]["error"])
        self.assertNotIn("batch deadline", result["results"][0]["error"])
        self.assertIsNone(result["stop_reason"])
        batch["on_error"] = "stop"
        (self.work / "after.txt").unlink()
        with patch.object(services, "CALL_TIMEOUT_SECONDS", 0.2):
            result = await self.client.batch("test", batch, {"slow": "good"})
        self.assertEqual([e["status"] for e in result["results"]], ["unknown", "not_started"])

    async def test_a_service_failure_in_a_batch_keeps_its_data(self):
        from boring_worker_kit.services import ServiceFailure
        data = {"status": "failed", "code": "sql_error", "message": "x" * 400, "effects": "unknown"}

        async def failing(*args, **kwargs):
            raise ServiceFailure(data)
        with patch.object(self.worker.services, "call", failing):
            result = await self.client.batch("test", {"operations": [
                {"id": "f", "method": "service_call", "arguments": {"service": "canvas", "tool": "echo"}}]},
                {"f": "good"})
        self.assertEqual(result["results"][0]["status"], "failed")
        self.assertEqual(result["results"][0]["failure"], data)  # whole, not cut at 256 characters

    def test_answer_text(self):
        self.assertEqual(file_refs.answer_text({"content": [{"type": "text", "text": "plain"}]}), "plain")
        both = {"content": [{"type": "text", "text": "{}"}], "structuredContent": {}}
        self.assertEqual(json.loads(file_refs.answer_text(both)), both)
