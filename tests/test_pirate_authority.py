"""Real Unix peer admission and existing service authority boundaries."""
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest

from boring_crocodile.control_plane import create_provider
from boring_crocodile.oauth_records import Transaction, add_service
from boring_crocodile.pirate_authority import PirateAuthority
from boring_crocodile.settings import Settings
from boring_worker_kit.local_authority import LocalAuthority
from boring_worker_kit.outcomes import Refusal
from pond_config import one_pond_toml

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
        self.settings = replace(Settings.load(config), pirate_service_callers={'parrot': os.getuid()},
                                pirate_service_socket=self.root/'a.sock')
        self.provider = create_provider(self.settings)
        with Transaction(self.settings.oauth_database_file) as db:
            self.canvas = add_service(db, 'canvas', 'https://canvas.test/mcp', ['pond', 'pirate/parrot'])
            add_service(db, 'private', 'https://private.test/mcp', ['pond'])
        self.broker = PirateAuthority(self.settings, self.provider)
        self.client = LocalAuthority(self.settings.pirate_service_socket, os.getuid())

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
            with self.assertRaises(Refusal):
                await self.client.request('token', 'private')
            with Transaction(self.settings.oauth_database_file) as db:
                db.execute("UPDATE services SET ponds='[\"pond\"]' WHERE name='canvas'")
            self.assertFalse(self.provider.introspect(self.provider.service('canvas'), response['token'])['active'])
            with self.assertRaises(Refusal):
                await self.client.request('token', 'canvas')
        self.assertFalse(self.settings.pirate_service_socket.exists())

    async def test_unknown_uid_and_model_identity_are_denied(self):
        with self.assertRaises(Refusal):
            self.broker.dispatch(os.getuid()+1, {'operation': 'token', 'service': 'canvas'})
        with self.assertRaises(Refusal):
            self.broker.dispatch(os.getuid(), {'operation': 'token', 'service': 'canvas', 'caller': 'pond'})
        stranger = PirateAuthority(replace(self.settings, pirate_service_callers={'parrot': os.getuid()+1}), self.provider)
        async with stranger.serve():
            with self.assertRaisesRegex(Refusal, 'Unix UID'):
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

    async def test_existing_service_policy_roles_and_attribution(self):
        from boring_service_kit.access import AccessPolicy
        from boring_service_kit.http import ServiceVerifier
        from boring_service_kit.pond import ControlPlane
        import time
        grant = self.provider.service('canvas')
        token = self.broker.dispatch(os.getuid(), {'operation':'token','service':'canvas'})['token']
        answer = self.provider.introspect(grant, token)
        for role in ('reader', 'writer'):
            policy = AccessPolicy('https://example.test', [{'subject':'pirate/parrot','role':role,'author':'pirate-parrot'}])
            verifier = ServiceVerifier(ControlPlane('https://example.test','service','secret'), 'https://canvas.test/mcp',
                                       'https://example.test/introspect', policy, [])
            access = verifier.accept(token, answer, time.time())
            self.assertEqual(access.claims['boring_caller']['role'], role)
            self.assertEqual(access.claims['boring_caller']['author'], 'pirate-parrot')
            self.assertEqual(access.claims['boring_caller']['subject'], 'pirate/parrot')
            self.assertIsNone(verifier.accept(token, {**answer,'sub':'pond'}, time.time()))
        self.provider.pirate_service_principals = frozenset()
        self.assertFalse(self.provider.introspect(grant, token)['active'])
