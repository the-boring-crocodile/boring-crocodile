"""Real Unix peer admission and existing service authority boundaries."""
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from boring_crocodile.control_plane import create_provider
from boring_crocodile.oauth_records import Transaction, add_service
from boring_crocodile.pirate_authority import PirateAuthority
from boring_crocodile.settings import Settings, PirateCaller
from boring_worker_kit.local_authority import LocalAuthority
from boring_worker_kit.outcomes import Refused
from pond_config import one_pond_toml, runtime_settings

class PirateAuthorityTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Authority client deliberately refuses writable /tmp ancestry.
        base = Path.home() / 'lab'
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base, prefix='pa-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config = self.root/'config.toml'
        config.write_text(one_pond_toml(self.root))
        self.settings = replace(runtime_settings(config), pirate_callers={'parrot': PirateCaller(os.getuid(), ())},
                                pirate_service_socket=self.root/'a.sock')
        self.provider = create_provider(self.settings)
        with Transaction(self.settings.oauth_database_file) as db:
            self.canvas = add_service(db, 'canvas', 'https://canvas.test/mcp', {'pond': 'writer', 'pirate/parrot': 'writer'}, roles=["reader", "writer"], default_role="reader")
            add_service(db, 'private', 'https://private.test/mcp', {'pond': 'writer'}, roles=["reader", "writer"], default_role="reader")
        self.broker = PirateAuthority(self.settings, self.provider)
        self.client = LocalAuthority(self.settings.pirate_service_socket, os.getuid())

    async def test_registration_vouch_requires_the_pair_and_the_enabled_pond(self):
        with self.assertRaises(Refused) as denied:
            self.broker.dispatch(os.getuid(), {"operation": "registration", "pond": "pond"})
        self.assertEqual(denied.exception.code, "forbidden")
        admitted = replace(self.settings, pirate_callers={"parrot": PirateCaller(os.getuid(), ("pond", "garden"))})
        broker = PirateAuthority(admitted, self.provider)
        with self.assertRaises(Refused) as missing:
            broker.dispatch(os.getuid(), {"operation": "registration", "pond": "garden"})
        self.assertEqual(missing.exception.code, "forbidden")
        with self.assertRaises(Refused) as spoof:
            broker.dispatch(os.getuid(), {"operation": "registration", "pond": "pond", "uid": os.getuid()})
        self.assertEqual(spoof.exception.code, "invalid_request")

    async def test_registration_vouch_crosses_the_existing_verified_authority_socket(self):
        settings = replace(self.settings, pirate_callers={"parrot": PirateCaller(os.getuid(), ("pond",))})
        broker = PirateAuthority(settings, self.provider)
        directory = self.root / "registration" / "pond"
        directory.mkdir(parents=True)
        directory.chmod(0o2750)
        real_lstat = Path.lstat
        def root_ancestors(path):
            info = real_lstat(path)
            if path in directory.parents:
                fields = list(info)
                fields[4] = 0  # synthetic root-owned namespace; no chown/root execution
                return os.stat_result(fields)
            return info
        with patch("boring_crocodile.pirate_authority.registration_path",
                   return_value=directory / "register.sock"), \
             patch.object(Path, "lstat", root_ancestors):
            async with broker.serve():
                result = await self.client.request("registration", pond="pond")
                self.assertEqual((result["pond"], result["pirate"], result["uid"]),
                                 ("pond", "parrot", os.getuid()))
                self.assertEqual(result["socket_path"], str(directory / "register.sock"))
                self.assertNotIn("crocodile_version", result)
                with patch("boring_crocodile.pirate_authority.distribution_version",
                           side_effect=RuntimeError("metadata unavailable"), create=True) as lookup:
                    for _ in range(2):
                        self.assertEqual(await self.client.request("registration", pond="pond"), result)
                    lookup.assert_not_called()
                directory.chmod(0o2777)
                with self.assertRaises(Refused):
                    await self.client.request("registration", pond="pond")
                directory.chmod(0o2750)
                outside = directory.with_name("outside")
                directory.rename(outside)
                directory.symlink_to(outside, target_is_directory=True)
                with self.assertRaises(Refused):
                    await self.client.request("registration", pond="pond")

    async def test_real_peer_tokens_resource_identity_and_revocation(self):
        async with self.broker.serve():
            inventory = await self.client.request('list')
            self.assertEqual(inventory['caller'], 'pirate/parrot')
            self.assertEqual([x['name'] for x in inventory['services']], ['canvas'])
            response = await self.client.request('token', 'canvas')
            grant = self.provider.service('canvas')
            active = self.provider.introspect(grant, response['token'])
            self.assertTrue(active['active'])
            self.assertEqual((active['sub'], active['aud']), ('pirate/parrot', 'https://canvas.test/mcp'))
            self.assertFalse(self.provider.introspect(self.provider.service('private'), response['token'])['active'])
            self.assertIsNone(await self.provider.load_access_token(response['token']))
            with self.assertRaises(Refused):
                await self.client.request('token', 'private')
            with Transaction(self.settings.oauth_database_file) as db:
                db.execute("UPDATE services SET callers=? WHERE name='canvas'", ('{"pond":"writer"}',))
            self.assertFalse(self.provider.introspect(self.provider.service('canvas'), response['token'])['active'])
            with self.assertRaises(Refused):
                await self.client.request('token', 'canvas')
        self.assertFalse(self.settings.pirate_service_socket.exists())

    async def test_authority_failure_has_a_code_and_does_not_expose_exception_text(self):
        import asyncio
        import json
        from unittest.mock import patch
        from boring_worker_kit.local_authority import encode
        with patch.object(self.broker, "dispatch", side_effect=RuntimeError("PRIVATE-AUTHORITY-DETAIL")):
            await self._assert_authority_failure()

    async def _assert_authority_failure(self):
        import asyncio
        import json
        from boring_worker_kit.local_authority import encode
        async with self.broker.serve():
            reader, writer = await asyncio.open_unix_connection(self.client.path)
            try:
                writer.write(encode({"operation": "list"}))
                await writer.drain()
                result = json.loads(await reader.readline())
            finally:
                writer.close()
                await writer.wait_closed()
        self.assertEqual(result, {"ok": False, "outcome": {
            "status": "failed", "code": "authority_failed", "message": "RuntimeError: The Authority failed; see its log for details.",
            "component": "authority", "effects": "unknown"}})

    async def test_client_preserves_refused_and_failed_outcomes(self):
        from boring_outcomes import Failed
        from unittest.mock import patch
        async with self.broker.serve():
            for error in (Refused("busy", "try later", delay=2),
                          Failed("upstream_error", "dispatch failed", detail={"retry": [1, None]})):
                with self.subTest(status=error.status), \
                        patch.object(self.broker, "dispatch", side_effect=error):
                    with self.assertRaises(type(error)) as caught:
                        await self.client.request("list")
                    self.assertEqual(caught.exception.as_data(), error.as_data())

    async def test_unknown_uid_and_model_identity_are_denied(self):
        with self.assertRaises(Refused):
            self.broker.dispatch(os.getuid()+1, {'operation': 'token', 'service': 'canvas'})
        with self.assertRaises(Refused):
            self.broker.dispatch(os.getuid(), {'operation': 'token', 'service': 'canvas', 'caller': 'pond'})
        stranger = PirateAuthority(replace(self.settings, pirate_callers={'parrot': PirateCaller(os.getuid()+1, ())}), self.provider)
        async with stranger.serve():
            with self.assertRaisesRegex(Refused, 'Unix UID'):
                await self.client.request('list')

    async def test_wrong_authority_uid_and_unsafe_ancestry(self):
        async with self.broker.serve():
            with self.assertRaises(PermissionError):
                await LocalAuthority(self.client.path, os.getuid()+1).request('list')
        with tempfile.TemporaryDirectory() as unsafe:
            with self.assertRaises(PermissionError):
                await LocalAuthority(Path(unsafe)/'a.sock', os.getuid()).request('list')

    async def test_existing_socket_is_not_removed(self):
        self.client.path.write_text('keep')
        with self.assertRaises(FileExistsError):
            async with self.broker.serve():
                pass
        self.assertEqual(self.client.path.read_text(), 'keep')

    async def test_service_role_and_attribution_come_from_crocodile_introspection(self):
        from boring_crocodile.oauth_records import service_grants
        from boring_service_kit.http import ServiceVerifier
        from boring_service_kit.pond import ControlPlane
        import json
        import time
        token = self.broker.dispatch(os.getuid(), {'operation': 'token', 'service': 'canvas'})['token']
        verifier = ServiceVerifier(ControlPlane('https://example.test', 'service', 'secret'),
                                   'https://canvas.test/mcp')
        for role in ('reader', 'writer'):
            with Transaction(self.settings.oauth_database_file) as db:
                plan = service_grants(db, 'canvas', {'pond': 'writer', 'pirate/parrot': role})
                service_grants(db, 'canvas', plan['after'], plan['plan_digest'])
            grant = self.provider.service('canvas')
            answer = self.provider.introspect(grant, token)
            access = verifier.accept(token, answer, time.time())
            self.assertEqual(access.claims['boring_caller']['role'], role)
            self.assertEqual(json.loads(access.claims['boring_caller']['author']),
                             ['https://example.test', 'pirate/parrot'])
            self.assertEqual(access.claims['boring_caller']['subject'], 'pirate/parrot')
            self.assertIsNone(verifier.accept(token, {**answer, 'aud': 'https://other.test/mcp'}, time.time()))
        self.provider.pirate_service_principals = frozenset()
        self.assertFalse(self.provider.introspect(grant, token)['active'])
