import asyncio
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from boring_worker_kit.execution import EnvironmentExecutor
from boring_worker_kit.host_limits import HostLimits
from boring_crocodile.jobs import ManagedJobs
from boring_crocodile.settings import EnvironmentSettings
from boring_crocodile.outcomes import Refusal
from boring_worker_kit.task_client import ContainmentError


class FakeExecutor:
    def __init__(self):
        self.limits = HostLimits(2, 290)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def execute(self, command, cwd, timeout, on_admitted=None, *, job_id=None,
                      on_output=None, foreground_deadline=None):
        if on_admitted is not None:
            on_admitted()
        self.calls += 1
        self.started.set()
        if on_output is not None:
            on_output({"stdout": "so far", "stderr": "", "stdout_truncated": False,
                       "stderr_truncated": False})
        await self.release.wait()
        return {"exit_code": 0, "stdout": "done", "stderr": ""}


class ManagedJobsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = EnvironmentSettings(
            "pond", "Pond", self.root / "worker.sock", self.root, (self.root,),
            os.getuid(), 128, self.root / "task.sock")
        self.executor = FakeExecutor()
        self.jobs = ManagedJobs(self.settings, self.executor)

    async def asyncTearDown(self):
        self.executor.release.set()
        await asyncio.gather(*list(self.jobs.running.values()), return_exceptions=True)
        self.jobs.store.close()
        self.temp.cleanup()

    async def test_watch_emits_job_lifecycle(self):
        sent = []
        class Sink:
            def __init__(inner, settings, task, source, job_id):
                inner.job_id = job_id
            def emit(inner, event, **fields):
                sent.append((inner.job_id, event, fields))
            def close(inner):
                pass
        with patch("boring_crocodile.jobs.Observation", Sink):
            first = self.jobs.start("printf ready", None, 5)
            await self.executor.started.wait()
            self.executor.release.set()
            await asyncio.gather(*self.jobs.running.values())
        self.assertEqual([row[2]["state"] for row in sent],
                         ["queued", "running", "completed"])
        self.assertTrue(all(row[0] == first["job_id"] for row in sent))
        self.assertEqual(sent[-1][2]["exit_code"], 0)

    async def test_unconfirmed_cleanup_is_the_jobs_failure(self):
        # Results never carry `cleanup`: a returned result means it was
        # confirmed; an unconfirmed one is a failure the caller is told about.
        async def unconfirmed(*args, **kwargs):
            raise ContainmentError("task cleanup not confirmed; environment admission disabled")
        self.executor.execute = unconfirmed
        job = self.jobs.start("true", None, 5)
        await asyncio.gather(*self.jobs.running.values())
        output = self.jobs.output(job["job_id"])
        self.assertEqual(output["state"], "failed")
        self.assertIn("cleanup not confirmed", output["result"]["error"])

    async def test_start_status_output_and_recovery(self):
        first = self.jobs.start("printf done", None, 5)
        self.assertEqual(first["state"], "queued")
        self.assertRegex(first["job_id"], r"\A[a-f0-9]{32}\Z")
        await self.executor.started.wait()
        self.assertEqual(self.executor.calls, 1)
        self.assertEqual(self.jobs.status(first["job_id"])["state"], "running")
        running = self.jobs.output(first["job_id"])
        self.assertTrue(running["partial"])
        self.assertEqual(running["result"]["stdout"], "so far")
        self.assertNotIn("result", self.jobs.status(first["job_id"]))
        self.executor.release.set()
        await asyncio.gather(*self.jobs.running.values())
        result = self.jobs.output(first["job_id"])
        self.assertEqual(result["state"], "completed")
        self.assertFalse(result["partial"])
        self.assertEqual(result["result"]["stdout"], "done")
        self.jobs.store.close()
        self.jobs = ManagedJobs(self.settings, self.executor)
        self.assertEqual(self.jobs.output(first["job_id"]), result)

    async def test_identical_starts_create_two_jobs(self):
        first = self.jobs.start("printf same", None, 5)
        second = self.jobs.start("printf same", None, 5)
        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertEqual(len(self.jobs.list()["jobs"]), 2)
        self.executor.release.set()
        await asyncio.gather(*self.jobs.running.values())
        self.assertEqual(self.executor.calls, 2)

    async def test_lookup_requires_a_well_formed_job_id(self):
        first = self.jobs.start("printf done", None, 5)
        for bad in ("", "key", first["job_id"].upper(), first["job_id"] + "0",
                    first["job_id"][:-1], None, 7):
            for method in (self.jobs.status, self.jobs.output, self.jobs.stop):
                with self.assertRaisesRegex(
                        Refusal, "^invalid_request: job_id must be the 32-character id"):
                    method(bad)
        for method in (self.jobs.status, self.jobs.output, self.jobs.stop):
            with self.assertRaisesRegex(Refusal, "^not_found: unknown job"):
                method("0" * 32)
        self.assertFalse(self.jobs.running[first["job_id"]].cancelled())

    async def test_max_unfinished_is_a_limit_refusal(self):
        from boring_crocodile import jobs as jobs_module
        with patch.object(jobs_module, "MAX_UNFINISHED", 2):
            self.jobs.start("one", None, 5)
            self.jobs.start("two", None, 5)
            with self.assertRaisesRegex(Refusal, "^limit: too many unfinished jobs"):
                self.jobs.start("three", None, 5)
        self.assertEqual(len(self.jobs.list()["jobs"]), 2)

    async def test_pre_030_store_is_refused(self):
        import sqlite3
        self.jobs.store.close()
        path = self.root / ".boring-crocodile" / "jobs.sqlite3"
        path.unlink()
        db = sqlite3.connect(path)
        db.execute("""CREATE TABLE jobs (
            id TEXT PRIMARY KEY, key TEXT NOT NULL, digest TEXT NOT NULL,
            state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
            result TEXT, owner TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}')""")
        db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,'','{}')",
                   ("b" * 32, "run-key", "job-v1:b", "running", 3.0, 3.0, None))
        db.commit()
        db.close()
        with self.assertRaisesRegex(ValueError, "unsupported schema"):
            ManagedJobs(self.settings, self.executor)
        # Nothing was rewritten: the row is still there, still running.
        db = sqlite3.connect(path)
        self.assertEqual(db.execute("SELECT state FROM jobs").fetchall(), [("running",)])
        db.close()
        path.unlink()
        self.jobs = ManagedJobs(self.settings, self.executor)

    async def test_list_recent_receipts_without_output(self):
        first = self.jobs.start("printf secret", None, None)
        second = self.jobs.start("printf newer", None, None)
        listed = self.jobs.list(1)["jobs"]
        self.assertEqual([item["job_id"] for item in listed], [second["job_id"]])
        self.assertEqual(set(listed[0]), {"job_id", "state", "created_at", "updated_at"})
        self.assertNotIn("printf", str(listed))
        self.assertEqual(len(self.jobs.list()["jobs"]), 2)
        with self.assertRaisesRegex(ValueError, "limit"):
            self.jobs.list(101)
        with self.assertRaisesRegex(ValueError, "limit"):
            self.jobs.list(True)
        self.assertEqual(self.jobs.status(first["job_id"])["state"], "queued")

    async def test_stop_waits_for_owner_and_does_not_repeat(self):
        first = self.jobs.start("sleep 100", None, 10)
        await self.executor.started.wait()
        self.assertEqual(self.jobs.stop(first["job_id"])["state"], "stopping")
        await asyncio.gather(*self.jobs.running.values(), return_exceptions=True)
        self.assertEqual(self.jobs.status(first["job_id"])["state"], "cancelled")
        cancelled = self.jobs.output(first["job_id"])
        self.assertFalse(cancelled["partial"])
        self.assertEqual(cancelled["result"]["stdout"], "so far")
        self.assertTrue(cancelled["result"]["output_incomplete"])
        self.assertEqual(self.jobs.stop(first["job_id"])["state"], "cancelled")

    async def test_prestart_cancellation_and_interrupted_recovery(self):
        first = self.jobs.start("sleep 100", None, 10)
        self.jobs.stop(first["job_id"])
        await asyncio.gather(*self.jobs.running.values(), return_exceptions=True)
        await asyncio.sleep(0)
        self.assertEqual(self.jobs.status(first["job_id"])["state"], "cancelled")
        second = self.jobs.start("sleep 100", None, 10)
        await self.executor.started.wait()
        # Simulate a fresh worker's ledger read, without taking over the old owner.
        recovered = ManagedJobs(self.settings, FakeExecutor())
        self.assertEqual(recovered.status(second["job_id"])["state"], "interrupted")
        recovered.store.close()
        self.jobs.stop(second["job_id"])
        await asyncio.gather(*self.jobs.running.values(), return_exceptions=True)

    async def test_rejects_out_of_root_and_requires_containment(self):
        with self.assertRaisesRegex(Refusal, "^invalid_request: .*outside allowed roots"):
            self.jobs.start("true", "/tmp", 1)
        from dataclasses import replace
        with self.assertRaisesRegex(ValueError, "require systemd"):
            ManagedJobs(replace(self.settings, task_socket_path=None), self.executor)

    async def test_queue_state_tracks_shared_managed_and_foreground_slots(self):
        class BlockingTaskClient:
            def __init__(inner):
                inner.entered = 0
                inner.active = 0
                inner.peak = 0
                inner.two_entered = asyncio.Event()
                inner.release = asyncio.Event()

            async def execute(inner, command, cwd, timeout, stop, *, job_id=None, on_output=None):
                inner.entered += 1
                inner.active += 1
                inner.peak = max(inner.peak, inner.active)
                if inner.entered == 2:
                    inner.two_entered.set()
                try:
                    await inner.release.wait()
                    return {"exit_code": 0, "stdout": "done", "stderr": ""}
                finally:
                    inner.active -= 1

        executor = EnvironmentExecutor(self.settings, HostLimits(2, 290))
        blocker = BlockingTaskClient()
        executor._tasks = blocker
        self.jobs.executor = executor
        starts = [self.jobs.start(f"managed-{n}", None, 5)
                  for n in range(8)]
        await asyncio.wait_for(blocker.two_entered.wait(), 2)
        states = [self.jobs.status(item["job_id"])["state"] for item in starts]
        self.assertEqual(states.count("running"), 2)
        self.assertEqual(states.count("queued"), 6)
        self.assertEqual(blocker.entered, 2)

        foreground = asyncio.create_task(executor.execute("foreground", None, 5))
        await asyncio.sleep(0)
        self.assertEqual(blocker.entered, 2)
        owners = list(self.jobs.running.values())
        blocker.release.set()
        await asyncio.gather(*owners, foreground)

        self.assertEqual(blocker.peak, 2)
        self.assertEqual(blocker.entered, 9)
        self.assertTrue(all(self.jobs.status(item["job_id"])["state"] == "completed"
                            for item in starts))


class JobProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from boring_crocodile.worker import WorkerServer
        from boring_crocodile.worker_client import WorkerClient
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = EnvironmentSettings(
            "pond", "Pond", self.root / "worker.sock", self.root, (self.root,),
            os.getuid(), 128, self.root / "task.sock")
        with patch("boring_crocodile.worker.read_host_limits", return_value=HostLimits(2, 290)):
            self.worker = WorkerServer(self.settings)
        self.fake = FakeExecutor()
        self.worker.jobs.executor = self.fake
        self.server = await asyncio.start_unix_server(self.worker.handle, self.settings.socket_path)
        self.client = WorkerClient({"pond": self.settings})

    async def asyncTearDown(self):
        self.fake.release.set()
        await asyncio.gather(*list(self.worker.jobs.running.values()), return_exceptions=True)
        self.server.close()
        await self.server.wait_closed()
        self.worker.jobs.store.close()
        self.temp.cleanup()

    async def test_lost_start_response_is_found_in_job_list(self):
        # Sending the request without reading its reply simulates a lost public
        # response: the outcome is unknown until the caller checks job_list.
        from boring_worker_kit.framing import write_frame
        reader, writer = await asyncio.open_unix_connection(self.settings.socket_path)
        await write_frame(writer, {"operation": "job_start", "command": "printf done",
                                   "cwd": None, "timeout_seconds": 5})
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(self.fake.started.wait(), 2)
        listed = (await self.client.list_jobs("pond"))["jobs"]
        self.assertEqual(len(listed), 1)
        job_id = listed[0]["job_id"]
        status = await self.client.job_action("pond", "job_status", job_id)
        self.assertEqual(status["state"], "running")
        self.fake.release.set()
        await asyncio.gather(*self.worker.jobs.running.values())
        result = await self.client.job_action("pond", "job_output", job_id)
        self.assertEqual(result["result"]["stdout"], "done")
        self.assertEqual(self.fake.calls, 1)

    async def test_job_id_validation_through_worker(self):
        from boring_crocodile.outcomes import RefusalError
        with self.assertRaises(RefusalError) as malformed:
            await self.client.job_action("pond", "job_status", "not-a-job-id")
        self.assertEqual(malformed.exception.code, "invalid_request")
        with self.assertRaises(RefusalError) as unknown:
            await self.client.job_action("pond", "job_output", "f" * 32)
        self.assertEqual(unknown.exception.code, "not_found")

    async def test_list_through_worker(self):
        started = await self.client.job_start("pond", "printf done", None, None)
        self.assertRegex(started["job_id"], r"\A[a-f0-9]{32}\Z")
        listed = await self.client.list_jobs("pond")
        self.assertEqual([item["job_id"] for item in listed["jobs"]], [started["job_id"]])
        self.assertNotIn("printf done", str(listed))

    async def test_foreground_runner_disconnect_returns_error_frame(self):
        class BrokenExecutor:
            async def execute(self, command, cwd, timeout, **deadline):
                raise asyncio.IncompleteReadError(b"", 4)

        self.worker.executor = BrokenExecutor()
        with self.assertRaisesRegex(RuntimeError, "IncompleteReadError"):
            await self.client.execute("pond", "true", timeout_seconds=2)

    async def test_stop_through_worker(self):
        started = await self.client.job_start("pond", "sleep 100", None, 5)
        await self.fake.started.wait()
        stopping = await self.client.job_action("pond", "job_stop", started["job_id"])
        self.assertIn(stopping["state"], ("stopping", "cancelled"))
        await asyncio.gather(*list(self.worker.jobs.running.values()), return_exceptions=True)
        self.assertEqual((await self.client.job_action("pond", "job_status", started["job_id"]))["state"],
                         "cancelled")


class JobOperationsTest(unittest.IsolatedAsyncioTestCase):
    """`job_start(operations=...)`: one lifecycle shared with command jobs
    (unified, not a second start/_run pair)."""

    async def asyncSetUp(self):
        from boring_crocodile.worker import WorkerServer
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = EnvironmentSettings(
            "pond", "Pond", self.root / "worker.sock", self.root, (self.root,),
            os.getuid(), 128, self.root / "task.sock")
        with patch("boring_crocodile.worker.read_host_limits", return_value=HostLimits(2, 290)):
            self.worker = WorkerServer(self.settings)
        self.fake = FakeExecutor()
        self.worker.executor = self.fake
        self.worker.jobs.executor = self.fake
        self.jobs = self.worker.jobs

    async def asyncTearDown(self):
        self.fake.release.set()
        await asyncio.gather(*list(self.jobs.running.values()), return_exceptions=True)
        self.jobs.store.close()

    def _op(self, command="printf op-ok"):
        return [{"id": "s1", "method": "exec", "arguments": {"command": command}}]

    async def test_operations_job_runs_through_batch_and_completes(self):
        first = self.jobs.start(None, None, None, operations=self._op())
        self.assertEqual(first["state"], "queued")
        await self.fake.started.wait()
        self.fake.release.set()
        await asyncio.gather(*self.jobs.running.values())
        result = self.jobs.output(first["job_id"])
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["result"]["stop_reason"], None)
        self.assertEqual(result["result"]["results"][0]["result"]["stdout"], "done")

    async def test_command_and_operations_share_the_unfinished_pool(self):
        command_job = self.jobs.start("sleep 100", None, 10)
        ops_job = self.jobs.start(None, None, None, operations=self._op())
        self.assertEqual(command_job["state"], "queued")
        self.assertEqual(ops_job["state"], "queued")
        # Both rows count against the one MAX_UNFINISHED pool.
        self.assertEqual(self.jobs.store.count(("queued", "running", "stopping")), 2)

    async def test_exactly_one_of_command_or_operations_is_required(self):
        with self.assertRaisesRegex(Refusal, "^invalid_request: exactly one"):
            self.jobs.start(None, None, None)
        with self.assertRaisesRegex(Refusal, "^invalid_request: exactly one"):
            self.jobs.start("printf both", None, None, operations=self._op())

    async def test_operations_job_stop_cancels_mid_run(self):
        first = self.jobs.start(None, None, None, operations=self._op())
        await self.fake.started.wait()
        self.assertEqual(self.jobs.stop(first["job_id"])["state"], "stopping")
        await asyncio.gather(*self.jobs.running.values(), return_exceptions=True)
        self.assertEqual(self.jobs.status(first["job_id"])["state"], "cancelled")

    async def test_operations_require_a_batch_runner(self):
        bare = ManagedJobs(self.settings, self.fake)  # no batch: command-only caller
        with self.assertRaisesRegex(RuntimeError, "cannot run operations-based jobs"):
            bare.start(None, None, None, operations=self._op())
        bare.store.close()

    async def test_job_timeout_has_no_zero_and_one_maximum(self):
        # No "0 = unlimited": a command or operations job refuses 0 and
        # anything above MAX_JOB_SECONDS; an operations job is not bound by
        # the host's lifetime cap (HostLimits(2, 290) here).
        for timeout in (0, 86401):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(Refusal, "^invalid_request: "):
                    self.jobs.start("printf no", None, timeout)
                with self.assertRaisesRegex(Refusal, "^invalid_request: "):
                    self.jobs.start(None, None, timeout, operations=self._op())
        accepted = self.jobs.start(None, None, 86400, operations=self._op())
        self.assertEqual(accepted["state"], "queued")


class JobWaitTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        settings = EnvironmentSettings("pond", "Pond", root / "worker.sock", root, (root,),
                                       os.getuid(), 128, root / "task.sock")
        self.executor = FakeExecutor()
        self.jobs = ManagedJobs(settings, self.executor)

    async def asyncTearDown(self):
        self.executor.release.set()
        await asyncio.gather(*list(self.jobs.running.values()), return_exceptions=True)
        self.jobs.store.close()

    async def test_wait_reports_state_until_settled(self):
        job_id = self.jobs.start("printf done", None, 5)["job_id"]
        await self.executor.started.wait()
        looked = await self.jobs.wait([job_id], 0)
        self.assertEqual((looked["settled"], looked["wait_expired"]), (False, True))
        self.assertEqual(looked["jobs"][0]["state"], "running")
        self.assertNotIn("result", looked["jobs"][0])
        self.executor.release.set()
        # Wakes when the job settles, not when the wait runs out.
        settled = await asyncio.wait_for(self.jobs.wait([job_id], 60), 5)
        self.assertEqual((settled["settled"], settled["wait_expired"]), (True, False))
        self.assertEqual(settled["jobs"][0]["state"], "completed")

    async def test_unknown_and_malformed_ids_are_reported_per_id(self):
        job_id = self.jobs.start("printf done", None, 5)["job_id"]
        self.executor.release.set()
        result = await self.jobs.wait([job_id, "f" * 32, "nope"], 5)
        self.assertTrue(result["settled"])
        self.assertEqual(result["jobs"][0]["state"], "completed")
        self.assertEqual(result["jobs"][1], {"job_id": "f" * 32, "refusal": {
            "code": "not_found", "message": "unknown job"}})
        self.assertEqual(result["jobs"][2]["refusal"]["code"], "invalid_request")

    async def test_rejects_empty_or_oversized_lists_and_bad_wait_seconds(self):
        for job_ids, wait_seconds in (([], 0), (["f" * 32] * 33, 0), ("f" * 32, 0),
                                      (["f" * 32], -1), (["f" * 32], float("nan")),
                                      (["f" * 32], True)):
            with self.subTest(job_ids=str(job_ids)[:20], wait_seconds=wait_seconds):
                with self.assertRaisesRegex(Refusal, "^invalid_request"):
                    await self.jobs.wait(job_ids, wait_seconds)
