"""Synthetic disposable guest only: installed executor + real systemd task services."""
import asyncio
from importlib.metadata import version
import os
from pathlib import Path
import pwd
import signal
import subprocess
import tempfile
from unittest.mock import patch

from boring_crocodile.settings import Settings
from boring_worker_kit.execution import EnvironmentExecutor
from boring_worker_kit.host_limits import HostLimits
from boring_worker_kit.task_client import ContainmentError, TaskCgroup


async def main():
    assert os.getuid() != 0, "run as the ordinary fixture execution user"
    assert pwd.getpwuid(os.getuid()).pw_name == "test-pond", "unexpected fixture user"
    assert subprocess.check_output(["hostname"], text=True).strip() == "crocodile-shell-guest"
    assert subprocess.check_output(["systemd-detect-virt"], text=True).strip() == "qemu"
    assert Path("/sys/class/dmi/id/product_name").read_text().strip() == "Crocodile-Disposable-Shell-Guest"
    assert version("boring-worker-kit") == "0.1.5", "not the candidate wheel"
    settings = Settings.load(Path("/etc/boring-crocodile/config.toml")).environments["pond"]
    assert settings.default_cwd == Path("/home/test-pond")
    assert settings.task_unit_prefix == "boring-crocodile-task-pond"
    assert settings.task_socket_path == Path("/run/boring-crocodile/tasks/pond.sock")
    executor = EnvironmentExecutor(settings, HostLimits(2, None))
    assert executor._tasks is not None
    assert (await executor.execute("true", None, 2))["exit_code"] == 0

    stopped = {}
    original_observe = TaskCgroup.observe
    calls = []
    original_execute = executor._tasks.execute

    async def record(command, *args, **kwargs):
        calls.append(command)
        return await original_execute(command, *args, **kwargs)

    def pause_runner(hello, *args, **kwargs):
        group = original_observe(hello, *args, **kwargs)
        # Only signal the runner whose UID, cgroup and protected identity the
        # production observer has just validated. No PID scan or broad kill.
        descriptor = os.pidfd_open(hello["pid"])
        try:
            proc = Path("/proc")/str(hello["pid"])
            assert proc.stat().st_uid == os.getuid()
            assert "0::" + hello["cgroup"] in (proc/"cgroup").read_text().splitlines()
            signal.pidfd_send_signal(descriptor, signal.SIGSTOP)
        except BaseException:
            os.close(descriptor)
            group.close()
            raise
        stopped["pidfd"] = descriptor
        stopped["group"] = group
        return group

    with tempfile.TemporaryDirectory(prefix="cleanup-recovery-", dir=settings.default_cwd) as directory:
        root = Path(directory)
        neighbor_ready = root/"neighbor-ready"
        neighbor_release = root/"neighbor-release"
        uncertain_marker = root/"uncertain-marker"
        import shlex
        quote = shlex.quote
        neighbor_command = ("touch " + quote(str(neighbor_ready)) +
                            "; while [ ! -f " + quote(str(neighbor_release)) +
                            " ]; do sleep .1; done; printf neighbor-complete")
        uncertain_command = "touch " + quote(str(uncertain_marker))
        neighbor = asyncio.create_task(executor.execute(
            neighbor_command, None, 60, job_id="a"*32, on_output=lambda snapshot: None))
        try:
            deadline = asyncio.get_running_loop().time() + 20
            while not neighbor_ready.exists():
                assert not neighbor.done(), "neighbor exited before readiness"
                assert asyncio.get_running_loop().time() < deadline, "neighbor readiness timeout"
                await asyncio.sleep(.05)
            with patch.object(executor._tasks, "execute", side_effect=record):
                with patch.object(TaskCgroup, "observe", side_effect=pause_runner):
                    try:
                        await executor.execute(uncertain_command, None, 1)
                    except ContainmentError as error:
                        assert error.group is stopped["group"]
                    else:
                        raise AssertionError("paused runner cleanup unexpectedly succeeded")
                assert not uncertain_marker.exists(), "stopped runner dispatched unexpectedly"
                assert not neighbor.done(), "neighbor was interrupted"
                try:
                    await executor.execute("must-not-run", None, 1)
                except ContainmentError:
                    pass
                else:
                    raise AssertionError("admission did not block")
                assert calls == [uncertain_command], "blocked request reached task transport"
                signal.pidfd_send_signal(stopped["pidfd"], signal.SIGCONT)
                os.close(stopped.pop("pidfd"))
                await stopped["group"].wait_empty(20)
                assert (await executor.execute("printf fresh", None, 3))["stdout"] == "fresh"
                assert calls == [uncertain_command, "printf fresh"], "uncertain command was replayed"
                assert stopped["group"]._closed, "recovered handle retained"
                assert not neighbor.done(), "neighbor lost ownership during recovery"
            neighbor_release.touch()
            assert (await asyncio.wait_for(neighbor, 20))["stdout"] == "neighbor-complete"
            print("PASS: installed ordinary-UID executor; real systemd cleanup failure blocks admission")
            print("PASS: exact runner resumed; original cgroup confirmed; same executor recovers without replay")
            print("PASS: admitted neighbor completed; retained handle released")
        finally:
            if "pidfd" in stopped:
                descriptor = stopped.pop("pidfd")
                try:
                    signal.pidfd_send_signal(descriptor, signal.SIGCONT)
                except ProcessLookupError:
                    pass
                finally:
                    os.close(descriptor)
            neighbor_release.touch()
            if not neighbor.done():
                neighbor.cancel()
            await asyncio.gather(neighbor, return_exceptions=True)
            if "group" in stopped and not stopped["group"]._closed:
                await stopped["group"].wait_empty(20)
                stopped["group"].close()
    print("PASS: synthetic files and paused task cleaned up")


asyncio.run(main())
