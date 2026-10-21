import asyncio
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from boring_crocodile.handbook_context import ContextBootstrap
from boring_worker_kit.execution import EnvironmentExecutor
from boring_worker_kit.host_limits import HostLimits
from boring_crocodile.settings import EnvironmentSettings
from boring_crocodile.outcomes import Refusal, RefusalError
from boring_crocodile.worker import WorkerServer
from boring_crocodile.worker_client import WorkerClient


class LifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='crocodile-test-')
        self.root = Path(self.temp.name)
        self.settings = EnvironmentSettings('test', 'Test', self.root/'worker.sock',
            self.root, (self.root,), os.getuid(), 128)
        self.worker = WorkerServer(self.settings)
        self.worker.executor = EnvironmentExecutor(self.settings, HostLimits(2, None))
        self.handlers = set()
        async def handle(reader, writer):
            task = asyncio.current_task()
            self.handlers.add(task)
            try:
                await self.worker.handle(reader, writer)
            finally:
                self.handlers.remove(task)
        self.server = await asyncio.start_unix_server(handle, self.settings.socket_path)
        self.client = WorkerClient({'test': self.settings})
        self.calls = []

    async def asyncTearDown(self):
        for task in self.calls:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.calls, return_exceptions=True)
        self.server.close()
        await self.server.wait_closed()
        await asyncio.gather(*list(self.handlers), return_exceptions=True)
        self.temp.cleanup()

    def command(self, source):
        return 'exec ' + shlex.join([sys.executable, '-c', source])

    def submit(self, command, timeout=None):
        task = asyncio.create_task(self.client.execute('test', command, None, timeout))
        self.calls.append(task)
        return task

    async def until(self, condition, seconds=10):
        # Generous: a loaded host can take seconds to start a Python child.
        deadline = time.monotonic() + seconds
        while not condition():
            if time.monotonic() > deadline:
                self.fail('condition not reached before deadline')
            await asyncio.sleep(0.01)

    def active(self, pid):
        try:
            return Path(f'/proc/{pid}/stat').read_text().split(') ',1)[1][0] != 'Z'
        except (FileNotFoundError, ProcessLookupError):  # gone, also mid-read
            return False

    def sleeper(self, filename, seconds=30):
        # Long enough that only the code under test can end it inside a test;
        # the pid file appears whole (replace), never empty.
        return self.command(f'import os,signal,time; from pathlib import Path; signal.alarm({seconds+1}); signal.signal(signal.SIGTERM,signal.SIG_IGN); t=Path({str(self.root/filename)!r}+".tmp"); t.write_text(str(os.getpid())); os.replace(t,{str(self.root/filename)!r}); time.sleep({seconds})')

    async def test_unknown_project_is_a_refusal(self):
        self.worker.context = ContextBootstrap(self.root, ("missing.md",))
        with self.assertRaises(RefusalError) as caught:
            await self.client.request("test", "bootstrap", project="unknown")
        self.assertEqual(caught.exception.code, "not_found")

    async def test_bootstrap_uses_fixed_context_and_rereads_without_task_slot(self):
        doc = self.root / "agreement.md"
        doc.write_text("first")
        welcome = self.root / "WELCOME.md"
        welcome.write_text("hello")
        self.worker.context = ContextBootstrap(self.root, ("agreement.md",),
                                               welcome_file="WELCOME.md")
        with patch.object(self.worker.executor, "execute", side_effect=AssertionError("exec called")):
            result = await asyncio.wait_for(self.client.request(
                "test", "bootstrap", context_root="/does-not-exist"), 2)
            self.assertEqual(result["handbook"]["documents"][0]["content"], "first")
            doc.write_text("second")
            welcome.write_text("new greeting")
            result = await self.client.request("test", "bootstrap")
            self.assertEqual(result["welcome"], "new greeting")
            self.assertEqual(result["handbook"]["documents"][0]["content"], "second")
            with self.assertRaises(RefusalError) as caught:
                await self.client.request("test", "bootstrap", project="foreign")
            self.assertEqual(caught.exception.code, "not_found")
            doc.unlink()
            with self.assertRaisesRegex(RuntimeError, "FileNotFoundError"):
                await self.client.request("test", "bootstrap")

    async def test_bootstrap_capabilities_reuse_loaded_limits_and_registration(self):
        self.worker.context = ContextBootstrap(self.root, ("agreement.md",))
        (self.root/"agreement.md").write_text("local")
        self.worker.limits = HostLimits(3, 40)

        async def ping():
            raise AssertionError("Pirate contacted during bootstrap")
        self.worker.pirates.registry.register("parrot/default", ping, details={
            "pirate": "parrot", "flavour": "default", "socket_path": "/x", "uid": os.getuid()})
        result = await self.client.request("test", "bootstrap")
        self.assertEqual(result["handbook"]["documents"][0]["content"], "local")
        self.assertEqual(result["capabilities"]["command_limits"],
                         self.worker.limits.public())
        self.assertTrue(result["capabilities"]["command_execution_enabled"])
        self.assertFalse(result["capabilities"]["managed_jobs_enabled"])
        self.assertEqual(result["capabilities"]["pirates"], [{
            "pirate": "parrot", "flavour": "default", "available": True,
            "misses": 0, "last_miss": None}])

    async def test_bootstrap_rejects_wrong_peer_before_reading(self):
        # Another caller than the control plane is refused (forbidden), as
        # data, not reported as a worker failure.
        self.worker.context = ContextBootstrap(self.root, ("absent",))
        with patch("boring_crocodile.worker.peer_uid", return_value=os.getuid()+1), \
                patch.object(self.worker.context, "load") as load:
            with self.assertRaises(RefusalError) as caught:
                await self.client.request("test", "bootstrap")
        self.assertEqual(caught.exception.code, "forbidden")
        self.assertIn("control plane", caught.exception.data["message"])
        load.assert_not_called()

    async def test_four_requests_use_two_slots_and_route_outputs(self):
        for n in range(4):
            source = f'''import json,os,time
from pathlib import Path
root=Path({str(self.root)!r})
a=time.monotonic()
(root/"start{n}").write_text(str(os.getpid()))
deadline=a+30  # the test releases it; this only bounds a failed run
while not (root/"release").exists() and time.monotonic()<deadline:
 time.sleep(0.01)
print(json.dumps(dict(n={n},start=a,end=time.monotonic())))
'''
            self.submit(self.command(source))
        await self.until(lambda: len(list(self.root.glob('start*'))) == 2)
        await asyncio.sleep(0.1)
        self.assertEqual(len(list(self.root.glob('start*'))), 2)
        (self.root/'release').touch()
        results = await asyncio.gather(*self.calls)
        rows = [json.loads(r['stdout']) for r in results]
        self.assertEqual([r['n'] for r in rows], list(range(4)))
        events = sorted([(r['start'],1) for r in rows]+[(r['end'],-1) for r in rows])
        count=peak=0
        for _, change in events:
            count += change
            peak=max(peak,count)
        self.assertEqual(peak, 2)
        self.assertTrue(all(r['exit_code']==0 for r in results))

    async def test_disconnect_stops_running_command(self):
        task=self.submit(self.sleeper('pid'))
        await self.until(lambda: (self.root/'pid').exists())
        pid=int((self.root/'pid').read_text())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.until(lambda: not self.active(pid))

    async def test_queued_disconnect_never_launches(self):
        self.worker.executor=EnvironmentExecutor(self.settings, HostLimits(1, None))
        first=self.submit(self.sleeper('first'))
        await self.until(lambda: (self.root/'first').exists())
        queued=self.submit(self.command(f'from pathlib import Path; Path({str(self.root/"queued")!r}).touch()'))
        await asyncio.sleep(0.1)
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await self.until(lambda: not self.handlers)
        self.assertFalse((self.root/'queued').exists())

    async def test_timeout_does_not_stop_neighbor(self):
        # 5 s: long enough for the sleeper to start and write its pid on a loaded host,
        # far short of its 30 s sleep, so the timeout (not the sleep) ends it.
        short=self.submit(self.sleeper('timeout'),5)
        neighbor=self.submit(self.command('import time; time.sleep(0.7); print("neighbor")'))
        a,b=await asyncio.gather(short,neighbor)
        self.assertTrue(a['timed_out'])
        self.assertEqual(b['stdout'],'neighbor\n')
        self.assertEqual(b['exit_code'],0)
        self.assertFalse(b['timed_out'])
        pid=int((self.root/'timeout').read_text())
        self.assertFalse(self.active(pid))  # stopped despite ignoring SIGTERM, well before its alarm

    async def test_output_bounds_and_nonzero_exit(self):
        result=await self.client.execute('test',self.command('import os; os.write(1,b"a"*4096); os.write(2,b"b"*4096); raise SystemExit(7)'))
        self.assertEqual(result['exit_code'],7)
        self.assertEqual(len(result['stdout']),128)
        self.assertIn('output truncated',result['stdout'])
        self.assertEqual(result['stdout_bytes'],4096)
        self.assertEqual(len(result['stderr']),128)
        self.assertIn('output truncated',result['stderr'])
        self.assertEqual(result['stderr_bytes'],4096)
        self.assertTrue(result['stdout_truncated'] and result['stderr_truncated'])

    async def test_repeated_cancellation_holds_slot_until_cleanup(self):
        executor=EnvironmentExecutor(self.settings, HostLimits(1, None))
        first=asyncio.create_task(executor.execute(self.sleeper('pid'),None,None))
        self.calls.append(first)
        await self.until(lambda: (self.root/'pid').exists())
        pid=int((self.root/'pid').read_text())
        first.cancel()
        second=asyncio.create_task(executor.execute('printf next',None,None))
        self.calls.append(second)
        await asyncio.sleep(0.05)
        first.cancel()
        self.assertFalse(second.done())
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertFalse(self.active(pid))
        self.assertEqual((await second)['stdout'],'next')

    async def test_cancellation_during_spawn_still_cleans_process(self):
        executor=EnvironmentExecutor(self.settings, HostLimits(1, None))
        loop=asyncio.get_running_loop()
        real_spawn=loop.subprocess_exec
        spawned=asyncio.Event()
        release=asyncio.Event()
        async def delayed(*args,**kwargs):
            result=await real_spawn(*args,**kwargs)
            spawned.set()
            await release.wait()
            return result
        with patch.object(loop,'subprocess_exec',delayed):
            task=asyncio.create_task(executor.execute(self.sleeper('pid'),None,None))
            self.calls.append(task)
            try:
                await asyncio.wait_for(spawned.wait(),10)
                await self.until(lambda: (self.root/'pid').exists())
                pid=int((self.root/'pid').read_text())
                task.cancel()
            finally:
                # Never leave the spawn parked: a failed wait above would
                # otherwise hang teardown, which waits for the task.
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(self.active(pid))

    async def test_invalid_timeout_fails_without_launch(self):
        for value in (float('nan'),float('inf')):
            with self.assertRaisesRegex(Refusal,'finite'):
                await self.worker.executor.execute('true',None,value)


class DescendantTest(unittest.TestCase):
    # The probe watches its fixture through a pidfd. Distribution Pythons on
    # Linux have os.pidfd_open; portable builds compiled against old kernel
    # headers (uv's python-build-standalone 3.10) do not.
    @unittest.skipUnless(hasattr(os, "pidfd_open"), "this Python lacks os.pidfd_open")
    def test_cleanup_probe_has_no_surviving_children(self):
        # Separate subreaper process keeps fixture adoption out of the test runner.
        result=subprocess.run([sys.executable,'tests/fixtures/cleanup_probe.py'],
                              capture_output=True,text=True,timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        rows=[json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual({r['case'] for r in rows if 'case' in r},
                         {'timeout','cancel','normal_exit'})
        self.assertTrue(all(not r['child_still_running'] for r in rows if 'case' in r), rows)
        self.assertEqual(rows[-1]['cleanup'],'all harness children reaped')
