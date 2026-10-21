"""Linux-only disposable process probe; run with PYTHONPATH=src.

Uses only development executor code, private temporary files and its own children.
Fixtures self-expire after 15 seconds; finally kills their groups and reaps them.
Time windows leave room for a slow interpreter start under host load.
This is an observation script, not a passing regression test for the desired fix.
"""
import asyncio
import ctypes
import json
import os
from pathlib import Path
import shlex
import select
import signal
import sys
import tempfile
import time

from boring_worker_kit.execution import EnvironmentExecutor
from boring_worker_kit.host_limits import HostLimits
from boring_crocodile.settings import EnvironmentSettings

FIXTURE = '''import json, os, signal, sys, time
from pathlib import Path
root = Path(sys.argv[1])
mode = sys.argv[2]
(root / "group").write_text(str(os.getpgrp()))
child = os.fork()
if child == 0:
    if mode == "timeout":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (root / "child.tmp").write_text(str(os.getpid()))
    os.replace(root / "child.tmp", root / "child")  # appears whole, never empty
    time.sleep(15)
    os._exit(0)
while not (root / "child").exists():
    time.sleep(0.005)
if mode != "normal_exit":
    time.sleep(15)
'''


def process_state(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1][0]
    except (FileNotFoundError, ProcessLookupError):  # gone, also mid-read
        return 'gone'


async def probe(mode):
    with tempfile.TemporaryDirectory(prefix='crocodile-cleanup-') as directory:
        root = Path(directory)
        fixture = root / 'fixture.py'
        fixture.write_text(FIXTURE)
        settings = EnvironmentSettings('probe', 'Probe', root / 'unused.sock', root,
                                       (root,), os.getuid(), 1024)
        executor = EnvironmentExecutor(settings, HostLimits(1, None))
        command = 'exec ' + shlex.join([sys.executable, str(fixture), str(root), mode])
        task = asyncio.create_task(executor.execute(command, None, 3 if mode == 'timeout' else 10))
        pidfd = None
        try:
            deadline = time.monotonic() + 10
            while not (root / 'child').exists():
                if time.monotonic() > deadline:
                    raise RuntimeError('fixture did not become ready')
                await asyncio.sleep(0.01)
            child = int((root / 'child').read_text())
            pidfd = os.pidfd_open(child)
            exited = select.poll()
            exited.register(pidfd, select.POLLIN)
            if mode == 'cancel':
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                await asyncio.sleep(0.15)
                # A slot should not admit a new command while cancelled work remains.
                second = await executor.execute('printf second', None, 1)
                observation = {'second_command_completed': second['stdout'] == 'second'}
            else:
                result = await task
                observation = {'timed_out': result['timed_out'], 'exit_code': result['exit_code']}
            state = process_state(child)
            observation.update(case=mode, child_still_running=state not in ('Z', 'gone'),
                               child_state=state, child_exit_confirmed=bool(exited.poll(0)))
            if not observation['child_exit_confirmed']:
                started = time.monotonic()
                observation['child_exit_within_100ms'] = bool(exited.poll(100))
                observation['exit_delay_ms'] = round((time.monotonic() - started) * 1000, 3)
            print(json.dumps(observation), flush=True)
        finally:
            if pidfd is not None:
                os.close(pidfd)
            if (root / 'group').exists():
                group = int((root / 'group').read_text())
                assert group != os.getpgrp(), 'refuse to signal harness process group'
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if not task.done():
                await task
            # Allow the executor thread to reap its direct child first.
            await asyncio.sleep(0.35)


async def main():
    for mode in ('timeout', 'cancel', 'normal_exit'):
        await probe(mode)


if __name__ == '__main__':
    # Adopt this harness's orphaned fixtures so cleanup leaves no zombies.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER, ordinary-user operation
        raise OSError(ctypes.get_errno(), 'cannot enable fixture reaping')
    asyncio.run(main())
    reaped = 0
    while True:
        try:
            pid, _ = os.waitpid(-1, 0)
            reaped += bool(pid)
        except ChildProcessError:
            break
    print(json.dumps({'cleanup': 'all harness children reaped', 'adopted_children': reaped}))
