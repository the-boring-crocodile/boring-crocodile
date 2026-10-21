"""Local development cleanup cannot use pipe EOF as descendant exit."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from boring_worker_kit.execution import EnvironmentExecutor
from boring_worker_kit.host_limits import HostLimits
from boring_crocodile.settings import EnvironmentSettings
from boring_worker_kit.process import ProcessCleanupError, run_process, wait_group_exit
from boring_worker_kit.task_client import ContainmentError


class GroupSettlementTest(unittest.IsolatedAsyncioTestCase):
    async def test_exit_wait_retains_ownership_until_observation_clears(self):
        running = True
        with patch("boring_worker_kit.process.group_running", side_effect=lambda _: running):
            waiting = asyncio.create_task(wait_group_exit(123))
            try:
                await asyncio.sleep(.025)
                self.assertFalse(waiting.done())
                running = False
                await asyncio.wait_for(waiting, 1)
            finally:
                running = False
                await waiting

    async def test_lingering_group_is_failure_not_success(self):
        with patch("boring_worker_kit.process.group_running", return_value=True):
            with self.assertRaisesRegex(ProcessCleanupError, "not confirmed"):
                await wait_group_exit(123)

    async def test_unreadable_members_are_unknown(self):
        with patch("boring_worker_kit.process.group_running", side_effect=PermissionError):
            with self.assertRaisesRegex(ProcessCleanupError, "could not be observed"):
                await wait_group_exit(123)

    async def test_unconfirmed_local_cleanup_blocks_next_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = EnvironmentSettings("fixture", "Fixture", root / "unused.sock",
                                           root, (root,), 1000, 1024)
            executor = EnvironmentExecutor(settings, HostLimits(1, None))
            with patch("boring_worker_kit.execution.run_process",
                       new=AsyncMock(side_effect=ProcessCleanupError("unconfirmed"))) as run:
                with self.assertRaises(ContainmentError):
                    await executor.execute("synthetic", None, 1, job_id="one")
                with self.assertRaisesRegex(ContainmentError, "admission disabled"):
                    await executor.execute("must-not-run", None, 1, job_id="two")
                self.assertEqual(run.await_count, 1)

    async def test_systemd_runner_does_not_add_proc_group_observer(self):
        with patch("boring_worker_kit.process.group_running",
                   side_effect=AssertionError("systemd owns containment")):
            result = await run_process("printf ok", "/tmp", 1, 64, asyncio.Event(),
                                       systemd_contained=True)
        self.assertEqual(result["stdout"], "ok")
