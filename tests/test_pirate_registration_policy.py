"""Admission/version checks at the real registration socket, before any registry entry."""
import asyncio
from contextlib import suppress
import grp
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from boring_crocodile import pirates
from boring_crocodile.pirates import Pirates
from boring_crocodile.settings import EnvironmentSettings, PirateCaller
from boring_crocodile.worker import WorkerServer

UID = os.getuid()


class RegistrationPolicyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.bind(str(self.root / "pirate.sock"))
        self.addCleanup(self.socket.close)
        group = patch.object(pirates, "REGISTRATION_GROUP", grp.getgrgid(os.getgid()).gr_name)
        group.start()
        self.addCleanup(group.stop)
        self.servers = []

    async def asyncTearDown(self):
        for server, receiver in self.servers:
            server.close()
            await server.wait_closed()
            await receiver.close()

    async def ask(self, receiver, **changes):
        path = self.root / ("register-" + str(len(self.servers)) + ".sock")
        server = await asyncio.start_unix_server(receiver.handle, path)
        self.servers.append((server, receiver))
        reader, writer = await asyncio.open_unix_connection(path)
        try:
            request = {"operation": "register", "kind": "pirate", "flavour": "default",
                       "socket_path": str(self.root / "pirate.sock"), "version": "0.4.0", **changes}
            if request.get("version") is None:
                request.pop("version")
            writer.write(pirates.encode(request))
            await writer.drain()
            return json.loads(await reader.readline())
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    async def test_group_membership_alone_is_not_admission(self):
        receiver = Pirates()
        reply = await self.ask(receiver, version=None)  # valid legacy frame was admitted by group alone
        self.assertFalse(reply["ok"], reply)
        self.assertEqual(reply["outcome"]["code"], "forbidden")
        self.assertEqual(receiver.list(), {"pirates": []})

    async def test_roster_pair_is_scoped_to_this_pond_and_names_acceptance(self):
        callers = {"parrot": PirateCaller(UID, ("pond",))}
        denied = Pirates(pond="garden", callers=callers)
        reply = await self.ask(denied)
        self.assertEqual(reply["outcome"]["code"], "forbidden")
        self.assertIn("garden", reply["outcome"]["message"])
        allowed = Pirates(pond="pond", callers=callers)
        reply = await self.ask(allowed)
        self.assertTrue(reply["result"]["accepted"])
        self.assertEqual((reply["result"]["pond"], reply["result"]["pirate"]), ("pond", "parrot"))
        self.assertNotIn("crocodile_version", reply["result"])
        self.assertEqual(allowed.list()["pirates"][0]["pirate"], "parrot")

    async def test_incompatible_and_malformed_versions_never_enter_the_registry(self):
        receiver = Pirates(pond="pond", callers={"parrot": PirateCaller(UID, ("pond",))})
        for value in ("0.3.11", "0.4.0rc9", "0.4.0rc10", "0.5.0"):
            with self.subTest(version=value):
                reply = await self.ask(receiver, version=value)
                self.assertEqual(reply["outcome"]["status"], "refused")
                self.assertEqual(reply["outcome"]["code"], "unsupported")
                self.assertIn(value, reply["outcome"]["message"])
                self.assertIn("requires", reply["outcome"]["message"])
                self.assertEqual(receiver.list(), {"pirates": []})
        for value in (None, "garbage", True, {}, ""):
            with self.subTest(version=value):
                reply = await self.ask(receiver, version=value)
                self.assertEqual(reply["outcome"]["code"], "invalid_request")
                self.assertEqual(receiver.list(), {"pirates": []})

    async def test_removed_pair_and_reused_uid_have_no_registration_access(self):
        # A new account can have the old numeric identity; the new worker's Pond
        # name still has no pair in the root-owned roster.
        receiver = Pirates(pond="replacement", callers={"parrot": PirateCaller(UID, ())})
        reply = await self.ask(receiver)
        self.assertEqual(reply["outcome"]["code"], "forbidden")
        self.assertEqual(receiver.list(), {"pirates": []})

    async def test_registration_does_not_read_its_own_distribution_metadata(self):
        receiver = Pirates(pond="pond", callers={"parrot": PirateCaller(UID, ("pond",))})
        with patch.object(pirates, "distribution_version", side_effect=RuntimeError("missing version"), create=True) as lookup:
            for _ in range(2):
                reply = await self.ask(receiver)
                self.assertTrue(reply["ok"], reply)
                self.assertNotIn("crocodile_version", reply["result"])
            lookup.assert_not_called()
        self.assertEqual(len(receiver.list()["pirates"]), 1)

    async def test_worker_uses_only_the_selected_ponds_pairs(self):
        env = EnvironmentSettings("garden", "Garden", self.root / "worker.sock",
                                  self.root, (self.root,), UID, 1024, command_execution_enabled=False)
        worker = WorkerServer(env, pirate_callers={"parrot": PirateCaller(UID, ("pond",))})
        self.addCleanup(worker.files.pool.shutdown, wait=True)
        reply = await self.ask(worker.pirates)
        self.assertEqual(reply["outcome"]["code"], "forbidden")
