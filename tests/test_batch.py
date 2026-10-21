"""Authenticated worker socket journeys through one ordered batch."""
import asyncio
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch
import unittest

from boring_worker_kit.execution import EnvironmentExecutor
from boring_worker_kit.file_policy import FilePolicy
from boring_worker_kit.host_limits import HostLimits
from boring_crocodile.jobs import ManagedJobs
from boring_worker_kit.task_client import ContainmentError
from boring_crocodile.settings import EnvironmentSettings
from boring_crocodile.worker import WorkerServer
from boring_crocodile.outcomes import RefusalError
from boring_crocodile.worker_client import WorkerClient


def step(name, method, **arguments):
    return {"id": name, "method": method, "arguments": arguments}


class BatchSocketTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "project").mkdir()
        policy = FilePolicy.parse({"roots": {"project": {"path": str(root / "project"),
            "read": True, "write": True}}})
        self.settings = EnvironmentSettings("test", "Test", root / "worker.sock",
            root, (root,), os.getuid(), 1024, command_execution_enabled=False, files=policy)
        self.worker = WorkerServer(self.settings)
        self.server = await asyncio.start_unix_server(self.worker.handle, self.settings.socket_path)
        self.client = WorkerClient({"test": self.settings})
        self.project = root / "project"

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        self.worker.files.pool.shutdown(wait=True)
        if self.worker.jobs:
            self.worker.jobs.store.close()

    async def batch(self, *steps, **kwargs):
        return await self.client.batch("test", {"operations": list(steps), **kwargs})

    async def test_preflight_rejects_late_step_before_effect(self):
        write = step("write", "file_write", root="project", path="x", content="hello",
                     expected_sha256=None)
        with self.assertRaisesRegex(RefusalError, "command execution is disabled") as caught:
            await self.batch(write, step("run", "exec", command="true"))
        self.assertEqual(caught.exception.data["code"], "forbidden")
        self.assertFalse((self.project / "x").exists())
        with self.assertRaises(RefusalError) as caught:
            await self.batch(write, step("bad", "file_read", root="foreign", path="x"))
        self.assertEqual(caught.exception.data["code"], "forbidden")
        self.assertFalse((self.project / "x").exists())
        with self.assertRaises(RefusalError) as caught:
            await self.batch(write, step("bad", "file_read", root="project", path="x", start_line=True))
        # Field names only, never the rejected values.
        self.assertEqual(caught.exception.data["code"], "invalid_request")
        self.assertTrue(any(f.endswith("start_line") for f in caught.exception.data["fields"]),
                        caught.exception.data)
        self.assertFalse((self.project / "x").exists())
        result = await self.batch(write, step("read", "file_read", root="project", path="x"))
        self.assertEqual([e["status"] for e in result["results"]], ["succeeded", "succeeded"])
        self.assertEqual(result["results"][1]["result"]["content"], "hello")

    async def test_search_limit_is_rejected_before_any_batch_write(self):
        write = step("write", "file_write", root="project", path="x",
                     content="must not happen", expected_sha256=None)
        with self.assertRaises(RefusalError):
            await self.batch(write, step("search", "file_search", root="project",
                                         query="needle", limit=101))
        self.assertFalse((self.project / "x").exists())

    async def test_refused_step_stops_or_continues_like_a_failed_one(self):
        missing = step("missing", "file_read", root="project", path="absent")
        write = step("write", "file_write", root="project", path="x", content="okay", expected_sha256=None)
        result = await self.batch(missing, write)
        self.assertEqual([e["status"] for e in result["results"]], ["refused", "not_started"])
        self.assertEqual(result["results"][0]["refusal"],
                         {"code": "not_found", "message": "file does not exist"})
        self.assertEqual(result["stop_reason"], "step_refused")
        self.assertFalse((self.project / "x").exists())
        result = await self.batch(missing, write, on_error="continue")
        self.assertEqual([e["status"] for e in result["results"]], ["refused", "succeeded"])
        self.assertIsNone(result["stop_reason"])
        self.assertEqual((self.project / "x").read_text(), "okay")

    async def test_over_budget_result_keeps_every_step_receipt(self):
        import boring_crocodile.batch as batch_module
        self.worker.executor = EnvironmentExecutor(self.settings, HostLimits(1, None))
        class Jobs:  # just enough for job_start admission
            store = type("Store", (), {"close": staticmethod(lambda: None)})
        self.worker.jobs = Jobs()
        async def large(method, args, deadline, **kwargs):
            if method == "job_start":
                return {"job_id": "j" * 32, "state": "queued", "created_at": 1.0,
                        "updated_at": 1.0, "note": "n" * 3000}
            return {"content": "c" * 3000, "sha256": "0" * 64, "bytes": 3000}
        operations = (step("start", "job_start", command="true"),
                      step("read", "file_read", root="project", path="x"))
        with patch.object(self.worker, "run_batch_step", side_effect=large), \
                patch.object(batch_module, "_RESULT_CEILING", 1500):
            result = await self.batch(*operations)
        self.assertEqual([(e["id"], e["status"]) for e in result["results"]],
                         [("start", "succeeded"), ("read", "succeeded")])
        self.assertEqual(result["results"][0]["result"], {"job_id": "j" * 32, "state": "queued"})
        self.assertTrue(result["results"][0]["result_truncated"])
        self.assertNotIn("result", result["results"][1])
        self.assertTrue(result["results"][1]["result_truncated"])
        self.assertEqual(result["stop_reason"], "output_limit")
        # Should even the receipts not fit, that is a genuine failure.
        with patch.object(self.worker, "run_batch_step", side_effect=large), \
                patch.object(batch_module, "_RESULT_CEILING", 10), \
                self.assertLogs("boring_crocodile.worker", "ERROR"), \
                self.assertRaisesRegex(RuntimeError, "even after reducing") as caught:
            await self.batch(*operations)
        self.assertNotIsInstance(caught.exception, RefusalError)

    async def test_stop_continue_and_command_execution(self):
        self.worker.executor = EnvironmentExecutor(self.settings, HostLimits(1, None))
        fail = step("fail", "exec", command="exit 7")
        write = step("write", "file_write", root="project", path="x", content="okay", expected_sha256=None)
        result = await self.batch(fail, write)
        self.assertEqual([e["status"] for e in result["results"]], ["failed", "not_started"])
        self.assertEqual(result["results"][0]["result"]["exit_code"], 7)
        result = await self.batch(fail, write, step("read", "file_read", root="project", path="x"),
                                  on_error="continue")
        self.assertEqual([e["status"] for e in result["results"]], ["failed", "succeeded", "succeeded"])
        self.assertEqual(result["results"][2]["result"]["content"], "okay")

    async def test_unconfirmed_command_cleanup_stops_continue(self):
        self.worker.executor = EnvironmentExecutor(self.settings, HostLimits(1, None))
        async def unconfirmed(method, *a, **kw):
            raise ContainmentError("synthetic cleanup failure")
        with patch.object(self.worker, "run_batch_step", side_effect=unconfirmed):
            result = await self.batch(step("bad", "exec", command="true"),
                                      step("later", "file_write", root="project", path="later",
                                           content="not dispatched", expected_sha256=None),
                                      on_error="continue")
        self.assertEqual(result["stop_reason"], "cleanup_unconfirmed")
        self.assertEqual([e["status"] for e in result["results"]], ["unknown", "not_started"])
        self.assertFalse((self.project / "later").exists())

    async def test_one_deadline_stops_later_work(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        async def controlled(method, args, deadline, **kwargs):
            if method == "file_read":
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return {"done": True}
        with patch.object(self.worker, "run_batch_step", side_effect=controlled):
            result = await self.batch(step("wait", "file_read", root="project", path="missing"),
                step("later", "file_read", root="project", path="missing"), timeout_seconds=.1)
        self.assertTrue(entered.is_set())
        self.assertTrue(cancelled.is_set())
        self.assertEqual([e["status"] for e in result["results"]], ["unknown", "not_started"])
        self.assertEqual(result["stop_reason"], "deadline")

    async def test_shared_deadline_across_two_execs_and_later_file(self):
        self.worker.executor = EnvironmentExecutor(self.settings, HostLimits(1, None))
        # First command settles; second consumes the remaining foreground budget.
        # Leave startup margin under unittest asyncio debug mode and host load.
        first = step("first", "exec", command="true")
        pid_file = self.project.parent / "sleeper.pid"
        second = step("second", "exec", command=f"echo $$ > {pid_file}; exec sleep 30")
        write = step("later", "file_write", root="project", path="later",
                     content="not dispatched", expected_sha256=None)
        # Stop on the settled command failure; continue may use cleanup margin.
        # Exhausted-deadline behavior with continue is covered separately above.
        # Each exec keeps up to half of what remains as result slack; 6 s
        # leaves the first more than a second, ample for `true` on a loaded host.
        result = await self.batch(first, second, write, timeout_seconds=6.0,
                                  on_error="stop")
        self.assertEqual(result["results"][0]["status"], "succeeded", result)
        second = result["results"][1]
        self.assertIn(second["status"], {"failed", "unknown", "refused"})
        if second["status"] == "refused":  # refused only when it never started
            self.assertEqual(second["refusal"]["code"], "not_admitted")
        if second["status"] == "failed" and "ForegroundInterrupted" in second.get("error", ""):
            self.assertIn("may have started", second["error"])
        self.assertEqual(result["results"][2]["status"], "not_started")
        self.assertFalse((self.project / "later").exists())
        # A command stopped before its echo leaves no (or an empty) pid file:
        # then nothing of it ran that could outlive the deadline.
        pid_text = pid_file.read_text().strip() if pid_file.exists() else ""
        if second["status"] != "refused" and pid_text:
            self.assertFalse(self.active(int(pid_text)))  # the sleeper did not outlive its deadline

    def active(self, pid):
        try:
            return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z"
        except (FileNotFoundError, ProcessLookupError):
            return False

    async def test_disconnect_keeps_file_owner_until_thread_finishes(self):
        import boring_worker_kit.files as files_module
        started, release = threading.Event(), threading.Event()
        original = files_module.atomic_write
        def blocked(*args, **kwargs):
            started.set()
            if not release.wait(3):
                raise TimeoutError("test barrier")
            return original(*args, **kwargs)
        first = step("first", "file_write", root="project", path="first",
                     content="done", expected_sha256=None)
        later = step("later", "file_write", root="project", path="later",
                     content="should-not-run", expected_sha256=None)
        with patch.object(files_module, "atomic_write", side_effect=blocked):
            call = asyncio.create_task(self.batch(first, later))
            self.assertTrue(await asyncio.wait_for(asyncio.to_thread(started.wait), 2))
            call.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await call
            release.set()
            await asyncio.to_thread(self.worker.files.pool.shutdown, True)
        self.assertEqual((self.project / "first").read_text(), "done")
        self.assertFalse((self.project / "later").exists())

    async def test_cancelled_job_waits_for_its_file_writer(self):
        import boring_worker_kit.files as files_module
        started, release = threading.Event(), threading.Event()
        original = files_module.atomic_write
        def blocked(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise TimeoutError("test barrier")
            return original(*args, **kwargs)
        self.worker.executor = EnvironmentExecutor(self.settings, HostLimits(1, None))
        settings = replace(self.settings, task_socket_path=self.project / "unused.sock")
        self.worker.jobs = ManagedJobs(settings, self.worker.executor, self.worker.batch)
        jobs = self.worker.jobs
        operations = [
            step("first", "file_write", root="project", path="first",
                 content="done", expected_sha256=None),
            step("later", "file_write", root="project", path="later",
                 content="must not run", expected_sha256=None),
        ]
        with patch.object(files_module, "atomic_write", side_effect=blocked):
            identifier = jobs.start(None, None, 10, operations=operations)["job_id"]
            try:
                self.assertTrue(await asyncio.wait_for(asyncio.to_thread(started.wait), 2))
                jobs.stop(identifier)
                pending = await jobs.wait([identifier], .05)
                self.assertFalse(pending["settled"])
                self.assertEqual(jobs.status(identifier)["state"], "stopping")
                jobs.stop(identifier)  # repeated cancellation must retain ownership
                self.assertFalse((await jobs.wait([identifier], .05))["settled"])
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(*list(jobs.running.values())), 2)
        self.assertEqual(jobs.status(identifier)["state"], "cancelled")
        self.assertEqual((self.project / "first").read_text(), "done")
        self.assertFalse((self.project / "later").exists())

    async def test_aggregate_output_limit_returns_prior_results(self):
        (self.project / "large").write_text(("needle " + "x" * 500 + "\n") * 101)
        operations = [step("search" + str(i), "file_search", root="project",
                           path=".", query="needle", limit=100) for i in range(24)]
        result = await self.batch(*operations)
        self.assertEqual(result["stop_reason"], "output_limit")
        self.assertEqual(len(result["results"]), 24)
        self.assertEqual(result["results"][0]["status"], "succeeded", result)
        self.assertIn("not_started", [e["status"] for e in result["results"]])

    async def test_file_batch_metadata_and_broken_observer(self):
        seen = []
        class Recorder:
            def __init__(self, *a, **kw):
                pass
            def emit(self, event, **fields):
                seen.append({"event": event, **fields})
            def close(self):
                pass
        import boring_worker_kit.files as files_module
        import boring_crocodile.batch as batch_module
        content = "PRIVATE_FILE_CONTENT_X"
        with patch.object(files_module, "Observation", Recorder), patch.object(batch_module, "Observation", Recorder):
            result = await self.batch(step("write", "file_write", root="project", path="secret.txt",
                                           content=content, expected_sha256=None),
                                      step("read", "file_read", root="project", path="secret.txt"))
        self.assertTrue(result["all_attempted"])
        self.assertEqual([e["state"] for e in seen if e["event"] == "batch_step"],
                         ["started", "succeeded", "started", "succeeded"])
        file_events = [e for e in seen if e["event"] == "file_state"]
        self.assertEqual(len(file_events), 4)
        self.assertEqual({e["batch_id"] for e in file_events}, {result["batch_id"]})
        self.assertEqual({e["step_id"] for e in file_events}, {"write", "read"})
        self.assertNotIn(content, str(seen))
        class Broken(Recorder):
            def emit(self, *a, **kw):
                raise OSError("observer unavailable")
        # Observation is best effort, including an unexpectedly broken sender.
        with patch.object(files_module, "Observation", Broken), patch.object(batch_module, "Observation", Broken):
            followup = await self.batch(step("read", "file_read", root="project", path="secret.txt"))
        self.assertEqual(followup["results"][0]["status"], "succeeded")

    async def test_job_receipt_survives_batch_completion(self):
        self.worker.executor = EnvironmentExecutor(self.settings, HostLimits(1, None))
        job_settings = replace(self.settings, task_socket_path=self.project / "unused.sock")
        self.worker.jobs = ManagedJobs(job_settings, self.worker.executor)
        result = await self.batch(step("run", "job_start", command="printf job-ok"),
                                  step("list", "job_list", limit=5))
        receipt = result["results"][0]["result"]
        self.assertEqual(result["results"][0]["status"], "succeeded", result)
        self.assertEqual(result["results"][1]["status"], "succeeded")
        for _ in range(100):
            current = self.worker.jobs.output(receipt["job_id"])
            if current["state"] in ("completed", "failed", "cancelled"):
                break
            await asyncio.sleep(.01)
        self.assertEqual(current["state"], "completed")
        self.assertEqual(current["result"]["stdout"], "job-ok")
        status = await self.batch(step("status", "job_status", job_id=receipt["job_id"]))
        self.assertEqual(status["results"][0]["result"]["state"], "completed")

    async def test_job_steps_take_a_server_minted_job_id(self):
        self.worker.executor = EnvironmentExecutor(self.settings, HostLimits(1, None))
        job_settings = replace(self.settings, task_socket_path=self.project / "unused.sock")
        self.worker.jobs = ManagedJobs(job_settings, self.worker.executor)
        for bad in (step("s", "job_status", job_id="my-key"),
                    step("s", "job_output", job_id="A" * 32),
                    step("s", "job_stop", selector="0" * 32),
                    step("s", "job_start", command="true", idempotency_key="k")):
            with self.assertRaisesRegex(RefusalError, '"invalid_request"'):
                await self.batch(bad)
        self.assertEqual(self.worker.jobs.list()["jobs"], [])
        unknown = await self.batch(step("s", "job_status", job_id="0" * 32))
        self.assertEqual(unknown["results"][0]["status"], "refused")
        self.assertEqual(unknown["results"][0]["refusal"]["code"], "not_found")

    async def test_job_wait_step_clamps_to_batch_deadline_and_reports_unknown_ids(self):
        self.worker.executor = EnvironmentExecutor(self.settings, HostLimits(1, None))
        job_settings = replace(self.settings, task_socket_path=self.project / "unused.sock")
        self.worker.jobs = ManagedJobs(job_settings, self.worker.executor)
        running = self.worker.jobs.start("sleep 30", None, 60)["job_id"]
        # The step asks for 90 s; the whole batch is capped at .2 s.
        result = await self.batch(step("wait", "job_wait", job_ids=[running], wait_seconds=90),
                                  timeout_seconds=0.2)
        self.assertEqual(result["results"][0]["status"], "succeeded", result)
        self.assertLess(result["elapsed_seconds"], 2)
        waited = result["results"][0]["result"]
        self.assertTrue(waited["wait_expired"])
        self.assertIn(waited["jobs"][0]["state"], ("queued", "running"))
        self.worker.jobs.stop(running)
        await asyncio.gather(*self.worker.jobs.running.values(), return_exceptions=True)
        settled = await self.batch(step("wait", "job_wait", job_ids=[running, "0" * 32],
                                        wait_seconds=0))
        waited = settled["results"][0]["result"]
        self.assertEqual((waited["settled"], waited["wait_expired"]), (True, False))
        self.assertEqual(waited["jobs"][0]["state"], "cancelled")
        self.assertEqual(waited["jobs"][1]["refusal"]["code"], "not_found")


class BatchServiceTest(unittest.IsolatedAsyncioTestCase):
    """Only the registry view is a batch step: a service call needs a token
    the control plane mints for that one call."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = EnvironmentSettings(
            "test", "Test", root / "worker.sock", root, (root,), os.getuid(), 1024,
            command_execution_enabled=False)
        self.worker = WorkerServer(self.settings)
        self.server = await asyncio.start_unix_server(self.worker.handle, self.settings.socket_path)
        self.client = WorkerClient({"test": self.settings})

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        await self.worker.services.close()
        self.worker.files.pool.shutdown(wait=True)

    async def batch(self, *steps, **kwargs):
        return await self.client.batch("test", {"operations": list(steps), **kwargs})

    async def test_service_list_is_a_step(self):
        async def ping():
            return True
        self.worker.services.registry.register(
            "service/example", ping, {"name": "example", "url": "http://127.0.0.1:9/mcp"})
        result = await self.batch(step("list", "service_list"))
        self.assertEqual(result["results"][0]["result"], {"services": [{
            "name": "example", "available": True, "misses": 0, "last_miss": None}]})

    async def test_service_describe_and_call_are_not_steps(self):
        for method, arguments in (("service_describe", {"service": "example"}),
                                  ("service_call", {"service": "example", "tool": "echo",
                                                    "arguments": {}})):
            with self.subTest(method=method), self.assertRaises(RefusalError) as caught:
                await self.batch(step("one", method, **arguments))
            self.assertEqual(caught.exception.code, "invalid_request")
