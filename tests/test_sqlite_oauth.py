import asyncio
import concurrent.futures
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from mcp.server.auth.provider import AuthorizationParams, RegistrationError, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
import time

from boring_service_kit.issuer import provider as provider_module  # the shared issuer holds the limits
from boring_crocodile.sqlite_oauth_provider import Denied, SQLiteOAuthProvider
from pond_config import dynamic, one_pond


class PersistenceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.secret = self.root / 'secret'
        self.secret.write_text('test-only-secret')
        self.database = self.root / 'oauth.sqlite3'
        self.provider = self.open()
        self.client = OAuthClientInformationFull(client_id='test-client', scope='mcp',
            redirect_uris=[AnyUrl('https://client.test/callback')])

    def open(self, database=None):
        return SQLiteOAuthProvider('https://server.test', database or self.database, one_pond(self.root), dynamic())

    async def code(self):
        url = await self.provider.authorize(self.client, AuthorizationParams(
            state='state', scopes=['mcp'], code_challenge='test-challenge',
            redirect_uri=AnyUrl('https://client.test/callback'),
            redirect_uri_provided_explicitly=True, resource='https://server.test/mcp/pond'))
        request = parse_qs(urlparse(url).query)['request'][0]
        reopened = self.open()
        self.assertTrue(reopened.has_pending(request))
        self.assertEqual(reopened.approve(request, 'wrong', 'pond'),
                         Denied(403, "Wrong approval secret."))
        callback = reopened.approve(request, 'test-only-secret', 'pond')
        self.assertEqual(reopened.approve(request, 'test-only-secret', 'pond').status, 400)
        code = parse_qs(urlparse(callback).query)['code'][0]
        return await self.open().load_authorization_code(self.client, code)

    async def tokens(self):
        return await self.provider.exchange_authorization_code(self.client, await self.code())

    async def test_five_wrong_secrets_cancel_the_pending_request(self):
        async def pending():
            url = await self.provider.authorize(self.client, AuthorizationParams(
                state='state', scopes=['mcp'], code_challenge='test-challenge',
                redirect_uri=AnyUrl('https://client.test/callback'),
                redirect_uri_provided_explicitly=True, resource='https://server.test/mcp/pond'))
            return parse_qs(urlparse(url).query)['request'][0]
        request, other = await pending(), await pending()
        for _ in range(4):
            self.assertEqual(self.provider.approve(request, 'wrong', 'pond'),
                             Denied(403, "Wrong approval secret."))
        self.assertTrue(self.provider.has_pending(request))
        fifth = self.provider.approve(request, 'wrong', 'pond')
        self.assertEqual(fifth.status, 403)
        self.assertIn("cancelled", fifth.message)
        self.assertIn("Start the connection again", fifth.message)
        # Gone: even the right secret no longer approves it.
        self.assertFalse(self.provider.has_pending(request))
        self.assertEqual(self.provider.approve(request, 'test-only-secret', 'pond').status, 400)
        # The count is per pending request: another one is untouched.
        self.assertEqual(self.provider.approve(other, 'wrong', 'pond'),
                         Denied(403, "Wrong approval secret."))
        self.assertIn('code=', self.provider.approve(other, 'test-only-secret', 'pond'))
        self.assertEqual(self.provider.wrong_secrets, {})

    def other_client(self, name):
        return OAuthClientInformationFull(client_id=name, scope='mcp',
            redirect_uris=[AnyUrl('https://client.test/callback')])

    async def pending_for(self, client):
        url = await self.provider.authorize(client, AuthorizationParams(
            state='state', scopes=['mcp'], code_challenge='test-challenge',
            redirect_uri=AnyUrl('https://client.test/callback'),
            redirect_uri_provided_explicitly=True, resource='https://server.test/mcp/pond'))
        return parse_qs(urlparse(url).query)['request'][0]

    async def test_unused_client_expires_after_a_day_a_used_one_stays(self):
        unused = self.other_client('unused-client')
        await self.provider.register_client(unused)
        await self.provider.register_client(self.client)
        await self.tokens()  # the first completed authorization keeps the client
        later = time.time() + provider_module.UNUSED_CLIENT_SECONDS + 60
        with patch.object(provider_module.time, 'time', return_value=later):
            self.assertIsNone(await self.provider.get_client('unused-client'))
            self.assertIsNotNone(await self.provider.get_client('test-client'))

    async def test_only_unused_clients_count_against_the_cap(self):
        await self.provider.register_client(self.client)
        await self.tokens()  # permanent now
        with patch.object(provider_module, 'MAX_UNUSED_CLIENTS', 2):
            await self.provider.register_client(self.other_client('a'))
            await self.provider.register_client(self.other_client('b'))
            with self.assertRaises(RegistrationError):
                await self.provider.register_client(self.other_client('c'))

    async def test_pending_requests_per_client_and_the_oldest_makes_room(self):
        a, b = self.other_client('a'), self.other_client('b')
        with patch.object(provider_module, 'MAX_PENDING_PER_CLIENT', 2), \
                patch.object(provider_module, 'MAX_PENDING', 3):
            first = await self.pending_for(a)
            second = await self.pending_for(a)
            third = await self.pending_for(a)  # a's quota is full: a's oldest makes room
            b_first = await self.pending_for(b)
            newest = await self.pending_for(b)  # global cap: the oldest makes room
        self.assertFalse(self.provider.has_pending(first))
        self.assertFalse(self.provider.has_pending(second))
        self.assertTrue(self.provider.has_pending(third))
        self.assertTrue(self.provider.has_pending(b_first))
        self.assertIn('code=', self.provider.approve(newest, 'test-only-secret', 'pond'))

    async def test_wrong_secrets_never_lock_out_the_right_one(self):
        for _ in range(6):
            request = await self.pending_for(self.client)
            for _ in range(5):
                self.provider.approve(request, 'wrong', 'pond')
        request = await self.pending_for(self.client)
        self.assertIn('code=', self.provider.approve(request, 'test-only-secret', 'pond'))

    async def test_process_restart(self):
        await self.provider.register_client(self.client)
        issued = await self.tokens()
        program = '''import asyncio, sys
from pathlib import Path
import time

from boring_crocodile import sqlite_oauth_provider as provider_module
from boring_crocodile.sqlite_oauth_provider import Denied, SQLiteOAuthProvider
from pond_config import PROFILE_ID, dynamic, one_pond
async def check():
 p = SQLiteOAuthProvider('https://server.test', Path(sys.argv[2]), one_pond(Path(sys.argv[1])), dynamic())
 c = await p.get_client('test-client')
 assert c is not None
 a = await p.load_access_token(sys.argv[3])
 assert a.subject == PROFILE_ID and a.resource == 'https://server.test/mcp/pond'
 assert await p.load_refresh_token(c, sys.argv[4]) is not None
asyncio.run(check())'''
        subprocess.run([sys.executable, '-c', program, str(self.root), str(self.database),
                        issued.access_token, issued.refresh_token], check=True, capture_output=True)

    async def test_code_replay_and_client_binding(self):
        code = await self.code()
        other = self.client.model_copy(update={'client_id': 'other'})
        self.assertIsNone(await self.provider.load_authorization_code(other, code.code))
        with self.assertRaises(TokenError):
            await self.provider.exchange_authorization_code(other, code)
        await self.provider.exchange_authorization_code(self.client, code)
        with self.assertRaises(TokenError):
            await self.open().exchange_authorization_code(self.client, code)

    async def test_refresh_rotation_scope_and_replay(self):
        issued = await self.tokens()
        token = await self.open().load_refresh_token(self.client, issued.refresh_token)
        with self.assertRaises(TokenError) as caught:
            await self.provider.exchange_refresh_token(self.client, token, ['admin'])
        self.assertEqual(caught.exception.error, 'invalid_scope')
        rotated = await self.open().exchange_refresh_token(self.client, token, ['mcp'])
        self.assertIsNone(await self.provider.load_access_token(issued.access_token))
        self.assertIsNone(await self.provider.load_refresh_token(self.client, issued.refresh_token))
        with self.assertRaises(TokenError):
            await self.open().exchange_refresh_token(self.client, token, [])
        access = await self.open().load_access_token(rotated.access_token)
        self.assertEqual(access.resource, token.resource)
        self.assertEqual(access.subject, token.subject)

    async def test_revocation_survives_restart_both_directions(self):
        for use_refresh in (False, True):
            issued = await self.tokens()
            token = (await self.provider.load_refresh_token(self.client, issued.refresh_token)
                     if use_refresh else await self.provider.load_access_token(issued.access_token))
            await self.provider.revoke_token(token)
            reopened = self.open()
            self.assertIsNone(await reopened.load_access_token(issued.access_token))
            self.assertIsNone(await reopened.load_refresh_token(self.client, issued.refresh_token))
            await reopened.revoke_token(token)

    async def test_expiry_rechecked_during_exchange(self):
        code = await self.code()
        with patch('boring_crocodile.sqlite_oauth_provider.time.time', return_value=code.expires_at + 1):
            with self.assertRaises(TokenError):
                await self.provider.exchange_authorization_code(self.client, code)
        issued = await self.tokens()
        refresh = await self.provider.load_refresh_token(self.client, issued.refresh_token)
        with patch('boring_crocodile.sqlite_oauth_provider.time.time', return_value=refresh.expires_at + 1):
            self.assertIsNone(await self.provider.load_access_token(issued.access_token))
            with self.assertRaises(TokenError):
                await self.provider.exchange_refresh_token(self.client, refresh, [])

    async def test_failed_issuance_rolls_back(self):
        code = await self.code()
        original = self.provider._put
        def fail_refresh(db, kind, *args):
            if kind == 'refresh':
                raise RuntimeError('simulated write failure')
            return original(db, kind, *args)
        with patch.object(self.provider, '_put', side_effect=fail_refresh):
            with self.assertRaises(RuntimeError):
                await self.provider.exchange_authorization_code(self.client, code)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM records WHERE kind='access'").fetchone()[0], 0)
        await self.open().exchange_authorization_code(self.client, code)

    async def test_concurrent_code_and_refresh_exchange(self):
        code = await self.code()
        providers = [self.open(), self.open()]
        barrier = threading.Barrier(2)
        def race(index, refresh=None):
            barrier.wait(timeout=5)
            try:
                if refresh:
                    return asyncio.run(providers[index].exchange_refresh_token(self.client, refresh, []))
                return asyncio.run(providers[index].exchange_authorization_code(self.client, code))
            except TokenError:
                return None
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(race, range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        issued = next(result for result in results if result)
        refresh = await self.provider.load_refresh_token(self.client, issued.refresh_token)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda i: race(i, refresh), range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)

    async def test_backup_restore(self):
        await self.provider.register_client(self.client)
        issued = await self.tokens()
        backup = self.root / 'backup.sqlite3'
        self.provider.backup(backup)
        restored = self.open(backup)
        self.assertIsNotNone(await restored.get_client(self.client.client_id))
        self.assertIsNotNone(await restored.load_access_token(issued.access_token))
        with self.assertRaises(FileExistsError):
            self.provider.backup(backup)

    async def test_bearer_values_are_never_stored(self):
        await self.provider.register_client(self.client)
        url = await self.provider.authorize(self.client, AuthorizationParams(
            state='state', scopes=['mcp'], code_challenge='test-challenge',
            redirect_uri=AnyUrl('https://client.test/callback'),
            redirect_uri_provided_explicitly=True, resource='https://server.test/mcp/pond'))
        request = parse_qs(urlparse(url).query)['request'][0]
        stored_with_pending = self.database.read_bytes()
        callback = self.provider.approve(request, 'test-only-secret', 'pond')
        code_value = parse_qs(urlparse(callback).query)['code'][0]
        stored_with_code = self.database.read_bytes()
        code = await self.provider.load_authorization_code(self.client, code_value)
        self.assertEqual(code.code, code_value)
        issued = await self.provider.exchange_authorization_code(self.client, code)
        stored = self.database.read_bytes()
        self.assertNotIn(request.encode(), stored_with_pending)
        self.assertNotIn(code_value.encode(), stored_with_code)
        for value in (request, code_value, issued.access_token, issued.refresh_token):
            self.assertNotIn(value.encode(), stored)
        access = await self.open().load_access_token(issued.access_token)
        self.assertEqual(access.token, issued.access_token)
        refresh = await self.open().load_refresh_token(self.client, issued.refresh_token)
        self.assertEqual(refresh.token, issued.refresh_token)

    async def test_stores_of_another_schema_are_refused_unchanged(self):
        for version in (1, 2, 3, 4):
            old = self.root / f'v{version}.sqlite3'
            with sqlite3.connect(old) as db:
                db.execute("CREATE TABLE records (kind TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL, "
                           "expires REAL, pair TEXT, PRIMARY KEY(kind, key))")
                db.execute("INSERT INTO records VALUES ('access','access_rawvalue','{}',NULL,NULL)")
                db.execute(f"PRAGMA user_version={version}")
            os.chmod(old, 0o600)
            before = old.read_bytes()
            with self.assertRaisesRegex(RuntimeError, f"schema {version}; this release reads schema 5"):
                self.open(old)
            self.assertEqual(old.read_bytes(), before)

    async def test_schema_and_permissions_fail_closed(self):
        self.assertEqual(self.database.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(self.database) as db:
            db.execute('PRAGMA user_version=99')
        with self.assertRaises(RuntimeError):
            self.open()
        os.chmod(self.database, 0o644)
        with self.assertRaises(PermissionError):
            self.open()
