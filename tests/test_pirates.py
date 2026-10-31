"""Pirates register their flavours on register.sock and run assignments as jobs."""
import asyncio
from contextlib import suppress
import grp
import json
import os
from pathlib import Path
import pwd
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from boring_registry import Registry

from pydantic import ValidationError

from boring_crocodile import pirates
from boring_crocodile.task_tools import PirateJob
from boring_worker_kit.inputs import MAX_JOB_SECONDS
from boring_worker_kit.host_limits import HostLimits
from boring_crocodile.outcomes import Refusal, RefusalError
from boring_crocodile.pirates import Pirates
from boring_crocodile.pirates import encode
from boring_crocodile.settings import EnvironmentSettings
from boring_crocodile.worker import WorkerServer
from boring_crocodile.worker_client import WorkerClient

ME = pwd.getpwuid(os.getuid()).pw_name


class FakePirate:
    """One flavour-serving Pirate socket; `mode` decides how `run` answers."""

    def __init__(self, path: Path):
        self.path = path
        self.requests = []
        self.mode = "result"
        self.answer = 42  # what mode "result" answers
        self.closed = asyncio.Event()  # the worker hung up on a held run

    async def start(self):
        self.server = await asyncio.start_unix_server(self.handle, self.path)

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def handle(self, reader, writer):
        try:
            request = json.loads(await reader.readline())
            self.requests.append(request)
            operation = request["operation"]
            if operation == "ping":
                reply = {"ok": True, "result": {"flavour": request["flavour"]}}
            elif operation == "describe":
                reply = {"ok": True, "result": {"flavour": request["flavour"], "model": "test"}}
            elif self.mode == "result":
                reply = {"ok": True, "result": {"answer": self.answer}}
            elif self.mode == "busy":
                reply = {"ok": False, "error": {"code": "busy", "message": "flavour is busy"}}
            elif self.mode == "failure":
                reply = {"ok": False, "failure": "provider crashed"}
            elif self.mode == "drop":
                return
            else:  # hold: answer only when the worker hangs up
                await reader.read()
                self.closed.set()
                return
            writer.write((json.dumps(reply) + "\n").encode())
            with suppress(ConnectionError):  # the worker may hang up on an oversized frame
                await writer.drain()
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()


class PirateRegistrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pirate = FakePirate(self.root / "pirate.sock")
        await self.pirate.start()
        self.now = 100.0
        self.pirates = Pirates(Registry(clock=lambda: self.now))
        # Real SO_PEERCRED and NSS membership in our own primary group.
        group = patch.object(pirates, "REGISTRATION_GROUP", grp.getgrgid(os.getgid()).gr_name)
        group.start()
        self.addCleanup(group.stop)
        self.listener = await asyncio.start_unix_server(
            self.pirates.handle, self.root / "register.sock", limit=pirates.MAX_FRAME)

    async def asyncTearDown(self):
        self.listener.close()
        await self.listener.wait_closed()
        await self.pirate.stop()
        await self.pirates.close()

    async def register(self, **change):
        reader, writer = await asyncio.open_unix_connection(self.root / "register.sock")
        try:
            writer.write(encode({"operation": "register", "kind": "pirate", "flavour": "default",
                                 "socket_path": str(self.pirate.path), **change}))
            await writer.drain()
            return json.loads(await reader.readline())
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_registration_names_the_account_and_the_heartbeat_follows_it(self):
        self.assertEqual(await self.register(),
                         {"ok": True, "result": {"ping_interval_seconds": 3.0}})
        entry = self.pirates.registry.get(f"{ME}/default")
        self.assertEqual(entry.details, {"pirate": ME, "flavour": "default",
                                         "socket_path": str(self.pirate.path),
                                         "uid": os.getuid()})
        self.assertEqual(self.pirate.requests, [])  # registration itself calls nobody
        await self.pirates.registry.ping_all()
        self.assertEqual(self.pirate.requests, [{"operation": "ping", "flavour": "default"}])
        self.assertEqual(self.pirates.list(), {"pirates": [{
            "pirate": ME, "flavour": "default", "available": True,
            "misses": 0, "last_miss": None}]})
        await self.pirate.stop()  # the Pirate went away: the next ping misses
        await self.pirates.registry.ping_all()
        listed = self.pirates.list()["pirates"][0]
        self.assertFalse(listed["available"])
        self.assertEqual(listed["misses"], 1)
        self.assertIn("not listening", listed["last_miss"])
        await self.pirate.start()

    async def test_the_socket_takes_only_pirates(self):
        # The retired service registration (no kind) and any other kind.
        for request in ({"operation": "register", "id": "canvas", "socket_path": str(self.pirate.path)},
                        {"kind": "service"}):
            with self.subTest(request=request):
                reader, writer = await asyncio.open_unix_connection(self.root / "register.sock")
                try:
                    payload = request if "operation" in request else {
                        "operation": "register", "flavour": "default",
                        "socket_path": str(self.pirate.path), **request}
                    writer.write(encode(payload))
                    await writer.drain()
                    reply = json.loads(await reader.readline())
                finally:
                    writer.close()
                    await writer.wait_closed()
                self.assertEqual(reply["error"]["code"], "invalid_request")
        self.assertEqual(self.pirates.list(), {"pirates": []})

    async def test_bad_flavours_are_refused(self):
        for flavour in ("Default", "-x", "a" * 64, "", "a/b", 7):
            with self.subTest(flavour=flavour):
                reply = await self.register(flavour=flavour)
                self.assertEqual(reply["error"]["code"], "invalid_request")
        self.assertEqual(self.pirates.list(), {"pirates": []})

    async def test_socket_must_be_an_own_socket_by_absolute_path(self):
        link = self.root / "link.sock"
        link.symlink_to(self.pirate.path)
        (self.root / "file").write_text("")
        for path, code in (("pirate.sock", "invalid_request"),
                           (str(self.root / "missing.sock"), "invalid_request"),
                           (str(self.root / "file"), "invalid_request"),
                           (str(link), "invalid_request")):
            with self.subTest(path=path):
                self.assertEqual((await self.register(socket_path=path))["error"]["code"], code)
        # A socket owned by another account than the registering one.
        root_account = {"operation": "register", "kind": "pirate", "flavour": "default",
                        "socket_path": str(self.pirate.path)}
        with self.assertRaises(Refusal) as caught:
            self.pirates.register(0, root_account)
        self.assertEqual(caught.exception.code, "forbidden")
        self.assertEqual(self.pirates.list(), {"pirates": []})

    async def test_unknown_registration_fields_are_refused(self):
        reply = await self.register(profile="review")
        self.assertEqual(reply["error"]["code"], "invalid_request")


class PirateJobTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = EnvironmentSettings(
            "pond", "Pond", self.root / "worker.sock", self.root, (self.root,),
            os.getuid(), 128, self.root / "task.sock")
        with patch("boring_crocodile.worker.read_host_limits", return_value=HostLimits(2, 290)):
            self.worker = WorkerServer(self.settings)
        self.pirate = FakePirate(self.root / "pirate.sock")
        await self.pirate.start()
        self.worker.pirates.register(os.getuid(), {
            "operation": "register", "kind": "pirate", "flavour": "default",
            "socket_path": str(self.pirate.path)})
        self.server = await asyncio.start_unix_server(self.worker.handle, self.settings.socket_path)
        self.client = WorkerClient({"pond": self.settings})

    async def asyncTearDown(self):
        await asyncio.gather(*list(self.worker.jobs.running.values()), return_exceptions=True)
        for server in (self.server, self.pirate.server):
            server.close()
            await server.wait_closed()
        self.worker.jobs.store.close()
        self.worker.files.pool.shutdown(wait=True)

    async def run_job(self, assignment="work", timeout_seconds=30):
        started = await self.client.job_start("pond", None, None, None, pirate={
            "pirate": ME, "flavour": "default", "assignment": assignment,
            "timeout_seconds": timeout_seconds})
        waited = await self.client.job_wait("pond", [started["job_id"]], 5)
        self.assertTrue(waited["settled"], waited)
        return started["job_id"], await self.client.job_action("pond", "job_output", started["job_id"])

    async def test_result_completes_the_job(self):
        job_id, output = await self.run_job()
        self.assertEqual((output["state"], output["result"]), ("completed", {"answer": 42}))
        self.assertEqual(self.pirate.requests, [{
            "operation": "run", "flavour": "default", "job_id": job_id,
            "assignment": "work", "timeout_seconds": 30}])

    async def test_an_answer_up_to_the_frame_limit_completes_the_job(self):
        # Non-ASCII text grows six-fold under JSON escaping: well past the
        # old 64 KiB reader limit, still inside the Pirate's 1 MiB frame.
        self.pirate.answer = "\u00fc" * 40000
        _, output = await self.run_job()
        self.assertEqual((output["state"], output["result"]), ("completed", {"answer": "\u00fc" * 40000}))

    async def test_an_answer_over_the_frame_limit_fails_the_job_naming_the_limit(self):
        self.assertEqual(pirates.MAX_FRAME, 1024 * 1024)
        self.pirate.answer = "x" * pirates.MAX_FRAME
        _, output = await self.run_job()
        self.assertEqual(output["state"], "failed")
        self.assertIn("exceeds the frame limit of 1048576 bytes", output["result"]["error"])

    async def test_pirate_refusal_is_the_jobs_outcome(self):
        self.pirate.mode = "busy"
        _, output = await self.run_job()
        self.assertEqual(output["state"], "refused")
        self.assertEqual(output["result"], {"refusal": {"code": "busy", "message": "flavour is busy"}})

    async def test_dropped_connection_and_pirate_failure_fail_the_job(self):
        for mode, text in (("drop", "closed the connection before answering"),
                           ("failure", "the Pirate reported a failure: provider crashed")):
            with self.subTest(mode=mode):
                self.pirate.mode = mode
                _, output = await self.run_job()
                self.assertEqual(output["state"], "failed")
                self.assertIn(text, output["result"]["error"])

    async def test_job_stop_closes_connection_without_claiming_remote_cleanup(self):
        self.pirate.mode = "hold"
        started = await self.client.job_start("pond", None, None, None, pirate={
            "pirate": ME, "flavour": "default", "assignment": "long", "timeout_seconds": None})
        while not self.pirate.requests:
            await asyncio.sleep(0.01)
        waited = await self.client.job_wait("pond", [started["job_id"]], 0)
        self.assertEqual((waited["wait_expired"], waited["jobs"][0]["state"]), (True, "running"))
        await self.client.job_action("pond", "job_stop", started["job_id"])
        await asyncio.wait_for(self.pirate.closed.wait(), 5)
        waited = await self.client.job_wait("pond", [started["job_id"]], 5)
        self.assertEqual(waited["jobs"][0]["state"], "failed")
        output = await self.client.job_action("pond", "job_output", started["job_id"])
        self.assertIn("remote cleanup unconfirmed", output["result"]["error"])
        self.assertEqual(len(self.pirate.requests), 1)  # never replay after uncertainty

    async def test_unknown_or_unavailable_flavour_is_refused_before_a_job_exists(self):
        payload = {"pirate": ME, "flavour": "other", "assignment": "x", "timeout_seconds": None}
        with self.assertRaises(RefusalError) as caught:
            await self.client.job_start("pond", None, None, None, pirate=payload)
        self.assertEqual(caught.exception.code, "not_found")
        entry = self.worker.pirates.registry.get(f"{ME}/default")
        entry.misses, entry.last_miss = 1, "no answer within 1 s"
        with self.assertRaises(RefusalError) as caught:
            await self.client.job_start("pond", None, None, None,
                                        pirate=dict(payload, flavour="default"))
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertIn("no answer within 1 s", caught.exception.data["message"])
        for bad in ({**payload, "flavour": "default", "timeout_seconds": 0},
                    {**payload, "flavour": "default", "timeout_seconds": 0.05},
                    {**payload, "flavour": "default", "timeout_seconds": 86400.5},
                    {**payload, "flavour": "default", "assignment": " "},
                    {"pirate": ME, "flavour": "default", "assignment": "x"}):
            with self.subTest(bad=bad), self.assertRaises(RefusalError) as caught:
                await self.client.job_start("pond", None, None, None, pirate=bad)
            self.assertEqual(caught.exception.code, "invalid_request")
        self.assertEqual((await self.client.list_jobs("pond"))["jobs"], [])
        self.assertEqual(self.pirate.requests, [])

    async def test_overrides_pass_through_unchanged_and_only_when_given(self):
        overrides = {"system": "You are a careful reviewer.", "tools": ["file_read"], "max_turns": 30,
                     "settings": {"max_tokens": 500}}
        started = await self.client.job_start("pond", None, None, None, pirate={
            "pirate": ME, "flavour": "default", "assignment": "work", "timeout_seconds": 30,
            "overrides": overrides})
        await self.client.job_wait("pond", [started["job_id"]], 5)
        self.assertEqual(self.pirate.requests[-1]["overrides"], overrides)
        for given in ({}, {"overrides": None}):  # absent on the wire: an older Pirate still runs it
            with self.subTest(given=given):
                await self.run_job() if not given else await self.client.job_wait("pond", [(
                    await self.client.job_start("pond", None, None, None, pirate={
                        "pirate": ME, "flavour": "default", "assignment": "work", "timeout_seconds": 30,
                        **given}))["job_id"]], 5)
                self.assertNotIn("overrides", self.pirate.requests[-1])
        payload = {"pirate": ME, "flavour": "default", "assignment": "x", "timeout_seconds": None}
        for bad, code in (({**payload, "overrides": ["system"]}, "invalid_request"),
                          ({**payload, "overrides": {"system": "x" * (64 * 1024)}}, "limit"),
                          ({**payload, "extra": 1}, "invalid_request")):
            with self.subTest(code=code), self.assertRaises(RefusalError) as caught:
                await self.client.job_start("pond", None, None, None, pirate=bad)
            self.assertEqual(caught.exception.code, code)
        PirateJob(**payload, overrides={"system": "x"})
        with self.assertRaises(ValidationError):
            PirateJob(**payload, overrides=["system"])

    async def test_null_timeout_is_the_pirates_default_but_the_wait_is_bounded(self):
        # Toward the Pirate, null stays null (its own default) ...
        job_id, output = await self.run_job(timeout_seconds=None)
        self.assertEqual(output["state"], "completed")
        self.assertIsNone(self.pirate.requests[-1]["timeout_seconds"])
        # ... but the worker never waits without a deadline: at most the
        # longest job plus the margin, or the given timeout plus the margin.
        target = {"pirate": ME, "flavour": "default", "assignment": "x",
                  "socket_path": str(self.pirate.path), "uid": os.getuid()}
        for timeout, deadline in ((None, MAX_JOB_SECONDS + pirates.RUN_MARGIN_SECONDS),
                                  (30, 30 + pirates.RUN_MARGIN_SECONDS)):
            with self.subTest(timeout=timeout), patch.object(
                    pirates, "exchange", AsyncMock(return_value={})) as exchange:
                await self.worker.pirates.run({**target, "timeout_seconds": timeout}, job_id)
                self.assertEqual(exchange.await_args.args[3], deadline)
        self.assertEqual(MAX_JOB_SECONDS, 86400)
        self.assertEqual(pirates.RUN_MARGIN_SECONDS, 15)  # cleanup reserve 10 + reply margin 5

    def test_the_tool_schema_bounds_the_timeout_like_every_job(self):
        payload = {"pirate": ME, "flavour": "default", "assignment": "x"}
        for good in (None, 0.1, 86400.0):
            PirateJob(**payload, timeout_seconds=good)
        for bad in (0.0, 0.05, 86400.5, float("inf")):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                PirateJob(**payload, timeout_seconds=bad)

    async def test_list_and_describe_through_the_worker(self):
        listed = await self.client.pirate_list("pond")
        self.assertEqual([(p["pirate"], p["flavour"]) for p in listed["pirates"]], [(ME, "default")])
        described = await self.client.pirate_describe("pond", ME, "default")
        self.assertEqual(described, {"flavour": "default", "model": "test"})
        self.assertEqual(self.pirate.requests, [{"operation": "describe", "flavour": "default"}])
        with self.assertRaises(RefusalError) as caught:
            await self.client.pirate_describe("pond", ME, "nope")
        self.assertEqual(caught.exception.code, "not_found")
        # The carried budget bounds the whole exchange; none left is refused before connecting.
        with self.assertRaises(Refusal) as refused:
            await self.worker.pirates.describe(ME, "default", 0)
        self.assertEqual(refused.exception.code, "limit")
        self.assertEqual(len(self.pirate.requests), 1)  # nothing more was sent
