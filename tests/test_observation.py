import asyncio
import base64
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from boring_crocodile import admin
from boring_worker_kit import observation
from boring_worker_kit.observation import Observation
from boring_worker_kit.process import run_process

# Child code: wait (at most 30 s) until the test creates the release file.
RELEASE_WAIT = "\nimport os,time\nt=time.monotonic()\nwhile not os.path.exists(%r) and time.monotonic()-t<30: time.sleep(.01)"


class OutputObservationTest(unittest.IsolatedAsyncioTestCase):
    async def test_cap_streams_timeout_and_observer_failure(self):
        class Sink:
            def __init__(self):
                self.data = {1: bytearray(), 2: bytearray()}
                self.events = []
            def output(self, fd, data):
                self.data[fd].extend(data)
            def emit(self, event, **fields):
                self.events.append((event, fields))
        sink = Sink()
        # The child is a fresh interpreter: under host load its start alone can
        # exceed a few hundred milliseconds, so the deadline leaves room to write.
        cmd = shlex.join([sys.executable, "-c", "import os,time; os.write(1,b'a'*10000); os.write(2,b'b'*10000); time.sleep(30)"])
        result = await run_process(cmd, "/tmp", 2.0, 100, asyncio.Event(), observer=sink)
        self.assertTrue(result["timed_out"])
        self.assertEqual(len(result["stdout"]), 100)
        self.assertIn("output truncated", result["stdout"])
        self.assertEqual(len(result["stderr"]), 100)
        self.assertIn("output truncated", result["stderr"])
        self.assertEqual(sink.data, {1: b"a"*100, 2: b"b"*100})
        self.assertEqual(sum(e[0] == "output_truncated" for e in sink.events), 2)
        class Broken:
            def output(self, *args):
                raise RuntimeError("viewer failed")
        result = await run_process("printf alive", "/tmp", 2, 100, asyncio.Event(), observer=Broken())
        self.assertEqual(result["stdout"], "alive")
        self.assertEqual(result["exit_code"], 0)

    async def test_output_observed_before_process_exits(self):
        received = asyncio.Event()
        class Sink:
            def output(self, fd, data):
                received.set()
            def emit(self, *args, **kwargs):
                pass
        # The child prints, then waits for the test's release: "before exit" by construction.
        release = Path(tempfile.mkdtemp()) / "release"
        self.addCleanup(shutil.rmtree, release.parent)
        cmd = shlex.join([sys.executable, "-u", "-c", "print('early'); " + RELEASE_WAIT % str(release)])
        task = asyncio.create_task(run_process(cmd, "/tmp", 60, 100, asyncio.Event(), observer=Sink()))
        await asyncio.wait_for(received.wait(), 10)
        self.assertFalse(task.done())
        release.touch()
        self.assertEqual((await task)["stdout"], "early\n")

    async def test_job_snapshots_are_bounded_and_arrive_before_completion(self):
        updates = []
        ready = asyncio.Event()
        def capture(snapshot):
            updates.append(snapshot)
            ready.set()
        release = Path(tempfile.mkdtemp()) / "release"
        self.addCleanup(shutil.rmtree, release.parent)
        cmd = shlex.join([sys.executable, "-u", "-c",
                            "import os; os.write(1,b'a'*10000); os.write(2,b'b'*10000)\n"
                            + RELEASE_WAIT % str(release)])
        running = asyncio.create_task(run_process(cmd, "/tmp", 60, 100,
                                                   asyncio.Event(), on_output=capture))
        await asyncio.wait_for(ready.wait(), 10)
        self.assertFalse(running.done())
        release.touch()
        result = await running
        self.assertEqual(len(result["stdout"]), 100)
        self.assertIn("output truncated", result["stdout"])
        self.assertEqual(len(result["stderr"]), 100)
        self.assertIn("output truncated", result["stderr"])
        self.assertEqual(updates[-1]["stdout"], result["stdout"])
        self.assertEqual(updates[-1]["stderr"], result["stderr"])
        self.assertTrue(updates[-1]["stdout_truncated"])
        self.assertTrue(updates[-1]["stderr_truncated"])
        silent = []
        await run_process("true", "/tmp", 2, 100, asyncio.Event(), on_output=silent.append)
        self.assertEqual(silent[-1]["stdout"], "")

    async def test_parallel_senders_and_slow_disconnected_receiver(self):
        # Real kernel datagrams; only endpoint validation is separate below.
        receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        sender.setblocking(False)
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        receiver.settimeout(.1)
        settings = SimpleNamespace(name="test", task_unit_prefix="missing-test")
        sink = Observation(settings, "one", "runner")
        sink.sock = sender
        result = await run_process("head -c 200000 /dev/zero", "/tmp", 10, 65536, asyncio.Event(), observer=sink)
        # The full receiver dropped rows but never stalled the command: it ran to its end.
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout_bytes"], 200000)
        self.assertEqual(len(result["stdout"]), 65536)
        self.assertGreater(sink.dropped, 0)
        while True:
            try:
                receiver.recv(8192)
            except TimeoutError:
                break
        sink.emit("after_gap")
        row = json.loads(receiver.recv(8192))
        self.assertGreater(row["dropped"], 0)
        self.assertTrue(json.loads(admin.Display().format(row))["observation_gap"])
        receiver.close()
        sink.emit("disconnected")
        sink.close()
        self.assertEqual((await run_process("printf recovered", "/tmp", 2, 100, asyncio.Event(), observer=sink))["stdout"], "recovered")

    async def test_concurrent_output_stays_with_task(self):
        class Sink:
            def __init__(self):
                self.data = bytearray()
            def output(self, fd, data):
                self.data.extend(data)
            def emit(self, *a, **kw):
                pass
        sinks = [Sink(), Sink()]
        results = await asyncio.gather(*(run_process(f"printf {i}; sleep .1; printf {i}", "/tmp", 2, 100,
            asyncio.Event(), observer=sinks[i]) for i in range(2)))
        self.assertEqual([bytes(s.data) for s in sinks], [b"00", b"11"])
        self.assertEqual([r["stdout"] for r in results], ["00", "11"])


class ViewerTest(unittest.TestCase):
    def test_nonroot_refused_before_reading_config(self):
        with patch.object(admin.os, "geteuid", return_value=1003), patch.object(admin.Settings, "load") as load:
            with patch.object(sys, "argv", ["admin", "watch", "--pond", "pond"]):
                with self.assertRaises(SystemExit) as caught:
                    admin.main()
            self.assertEqual(caught.exception.code, 2)
            load.assert_not_called()

    def test_display_is_bounded_escaped_and_detects_gaps(self):
        view = admin.Display()
        for i in range(130):
            row = dict(version=1, source="runner", sender=f"{i:032x}", sequence=3,
                       event="output", data=base64.b64encode(b"hello\x1b[2J\n").decode())
            rendered = view.format(row)
            self.assertNotIn("\x1b", rendered)
            self.assertTrue(json.loads(rendered)["observation_gap"])
        self.assertEqual(len(view.sequences), 128)

    def test_display_rejects_nontext_json_output(self):
        for data in (None, 42, [], {}):
            with self.subTest(data=data):
                row = dict(source="runner", sender="a" * 32, sequence=1,
                           event="output", data=data)
                with self.assertRaisesRegex(ValueError, "invalid observation output"):
                    admin.Display().format(row)

    def test_closed_watch_socket_has_explicit_error(self):
        receiver = admin.WatchSocket.__new__(admin.WatchSocket)
        receiver.sock = None
        with self.assertRaisesRegex(ValueError, "watch socket is closed"):
            receiver.receive()

    def test_unsafe_destination_and_missing_viewer_disable_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            for root in (None, Path(tmp)):  # no watch root, and one with no viewer bound
                sink = Observation(SimpleNamespace(name="test",task_unit_prefix="task",watch_root=root),"test","runner")
                self.assertIsNone(sink.sock)
                sink.emit("ignored")


@unittest.skipUnless(os.geteuid() == 0, "real root/UID boundary test; run in isolated root test environment")
class RootBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="crocodile-watch-test-", dir="/run")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o755)
        # The whole package, readable by the unprivileged sender (it cannot read the
        # source tree); the shared observation code comes from the installed kit.
        code = self.root/"code"/"boring_crocodile"
        shutil.copytree(Path(admin.__file__).parent, code, ignore=shutil.ignore_patterns("__pycache__"))
        for path in [code.parent, *code.rglob("*")]:
            path.chmod(0o755 if path.is_dir() else 0o644)
        self.task = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.task_path = self.root/"task.sock"
        self.task.bind(str(self.task_path))
        os.chown(self.task_path, 65534, 65534)
        self.task_path.chmod(0o600)
        self.settings = SimpleNamespace(name="test",task_unit_prefix="task-test",task_socket_path=self.task_path,
                                        command_execution_enabled=True,
                                        watch_root=self.root)
        self.patches = []
        for p in self.patches: p.start()
        self.receiver = admin.WatchSocket(self.settings)
        self.receiver.sock.settimeout(3)

    def tearDown(self):
        self.receiver.close()
        self.task.close()
        for p in self.patches: p.stop()
        self.temp.cleanup()

    def child(self, body, uid=65534, gid=65533):
        source = ("from pathlib import Path\nfrom types import SimpleNamespace\n"
                  "from boring_worker_kit import observation\n"
                  f"s=SimpleNamespace(name='test',task_unit_prefix='task-test',watch_root=Path({str(self.root)!r}))\n" + body)
        return subprocess.run([sys.executable, "-c", source], user=uid, group=gid, extra_groups=[],
                              capture_output=True,text=True,timeout=5,
                              env=dict(os.environ, PYTHONPATH=str(self.root/"code")))

    def test_real_sender_access_single_viewer_and_disconnect(self):
        result = self.child("o=observation.Observation(s,'task-one','runner'); o.emit('started'); o.close()")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.receiver.receive()["task"], "task-one")
        with self.assertRaisesRegex(admin.WatchAlreadyActive, "Watch already active for test"):
            admin.WatchSocket(self.settings)
        self.assertTrue(self.receiver.path.exists())
        # Foreign UID is rejected even though it can send a datagram.
        result = self.child("o=observation.Observation(s,'foreign','runner'); o.emit('started'); o.close()", uid=65533)
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.assertRaisesRegex(ValueError, "sender"):
            self.receiver.receive()
        self.receiver.close()
        self.assertFalse(self.receiver.path.exists())

    def test_process_output_reaches_root_viewer_from_execution_uid(self):
        result = self.child("import asyncio\nfrom boring_worker_kit.process import run_process\n"
                            "o=observation.Observation(s,'task-live','runner')\n"
                            "async def run():\n"
                            " r=await run_process('printf observed','/tmp',2,100,asyncio.Event(),observer=o)\n"
                            " assert r['stdout']=='observed'\n"
                            "asyncio.run(run())\no.close()")
        self.assertEqual(result.returncode, 0, result.stderr)
        for _ in range(8):
            row = self.receiver.receive()
            if row["event"] == "output" and row["stream"] == "stdout":
                self.assertEqual(base64.b64decode(row["data"]), b"observed")
                break
        else:
            self.fail("no stdout event")

    def test_unprivileged_cannot_replace_socket_or_watch(self):
        result = self.child("from boring_crocodile.admin import WatchSocket\n"
                           "try:\n WatchSocket(s)\nexcept PermissionError:\n pass\nelse:\n raise AssertionError('root check failed')\n"
                           "try:\n observation.watch_path(s).unlink()\nexcept PermissionError:\n pass\nelse:\n raise AssertionError('socket replaceable')\n")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_wrong_mode_receiver_rejected_and_oversize_input_rejected(self):
        self.receiver.path.chmod(0o666)
        result = self.child("o=observation.Observation(s,'task','runner'); assert o.sock is None")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.receiver.path.chmod(0o622)
        result = self.child("import socket\nsock=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM)\n"
                           "sock.sendto(b'x'*9000,str(observation.watch_path(s)))")
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.assertRaisesRegex(ValueError,"truncated"):
            self.receiver.receive()


if __name__ == "__main__":
    unittest.main()
