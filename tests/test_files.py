import asyncio
import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
import tempfile
import unittest

from boring_worker_kit.file_policy import FilePolicy
from boring_crocodile.settings import EnvironmentSettings
from boring_crocodile.worker import WorkerServer
from boring_crocodile.worker_client import WorkerClient
from boring_crocodile.outcomes import RefusalError
from mcp.server.mcpserver.exceptions import ToolError
from boring_worker_kit.framing import read_frame, write_frame


class FileBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'allowed').mkdir()
        (self.root / 'allowed' / 'nested').mkdir()
        (self.root / 'allowed' / 'denied').mkdir()
        self.policy = FilePolicy.parse({'roots': {'work': {'path': str(self.root / 'allowed'),
            'read': True, 'write': True, 'deny': ['denied']}}})
        self.settings = EnvironmentSettings('test', 'Test', self.root/'worker.sock',
            self.root, (self.root,), os.getuid(), 128,
            command_execution_enabled=False, files=self.policy)
        self.worker = WorkerServer(self.settings)
        self.server = await asyncio.start_unix_server(self.worker.handle, self.settings.socket_path)
        self.client = WorkerClient({'test': self.settings})

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        self.worker.files.pool.shutdown(wait=True)

    async def call(self, method, **kwargs):
        return await self.client.file_call('test', method, root='work', **kwargs)

    async def refused(self, code, method, **kwargs):
        with self.assertRaises(RefusalError) as caught:
            await self.call(method, **kwargs)
        self.assertEqual(caught.exception.data['code'], code, caught.exception.data)
        return caught.exception.data

    async def raw(self, request):
        reader, writer = await asyncio.open_unix_connection(self.settings.socket_path)
        try:
            await write_frame(writer, request)
            return await read_frame(reader)
        finally:
            writer.close()
            await writer.wait_closed()


    @unittest.skipUnless(os.name == "posix" and shutil.which("setfacl"), "POSIX ACL tools needed")
    async def test_text_and_byte_creation_follow_parent_acl(self):
        nested = self.root / 'allowed/nested'
        subprocess.run(["setfacl", "-m", f"d:u::rwx,d:g::---,d:g:{os.getgid()}:r--,d:m::r--,d:o::---",
                        str(nested)], check=True)
        for method, payload in (("file_write", {"content": "first"}),
                                ("file_write_bytes", {"data": "Zmlyc3Q="})):
            name = 'nested/' + method
            result = await self.call(method, path=name, expected_sha256=None, **payload)
            target = self.root / 'allowed' / name
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
            acl = os.getxattr(target, "system.posix_acl_access")
            self.assertTrue(acl)
            await self.call(method, path=name, expected_sha256=result['sha256'], **payload)
            self.assertEqual(os.getxattr(target, "system.posix_acl_access"), acl)
            await self.refused('stale', method, path=name, expected_sha256=None, **payload)

    async def test_format_roundtrips_and_search(self):
        import codecs
        for index, (encoding, prefix, newline) in enumerate((
            ("utf-8", b"", "\n"), ("utf-8", codecs.BOM_UTF8, "\r\n"),
            ("utf-16-le", codecs.BOM_UTF16_LE, "\r\n"),
            ("utf-16-be", codecs.BOM_UTF16_BE, "\n"))):
            with self.subTest(encoding=encoding, prefix=prefix):
                path = f"nested/format-{index}"
                disk = self.root / "allowed" / path
                original = prefix + ("Grüße" + newline + "old").encode(encoding)
                disk.write_bytes(original)
                read = await self.call("file_read", path=path)
                self.assertEqual(read["content"], "Grüße\nold")
                self.assertEqual(read["encoding"], encoding)
                self.assertEqual(read["bom"], bool(prefix))
                self.assertEqual(read["bytes"], len(original))
                changed = await self.call("file_patch", path=path, expected_sha256=read["sha256"],
                                          edits=[{"old_text": "old", "new_text": "new\nlast"}])
                expected = prefix + ("Grüße" + newline + "new" + newline + "last").encode(encoding)
                self.assertEqual(disk.read_bytes(), expected)
                search = await self.call("file_search", query="Grüße")
                self.assertIn(path, [m["path"] for m in search["matches"]])
                await self.refused("stale", "file_write", path=path, content="lost",
                                   expected_sha256=read["sha256"])
                await self.call("file_write", path=path, content="replacement\n",
                                expected_sha256=changed["sha256"])
                self.assertEqual(disk.read_bytes(), prefix + ("replacement" + newline).encode(encoding))

    async def test_mixed_format_requires_choice_and_conversion_is_explicit(self):
        import codecs
        disk = self.root / "allowed/nested/mixed"
        original = b"a\r\nb\n"
        disk.write_bytes(original)
        read = await self.call("file_read", path="nested/mixed")
        self.assertEqual(read["newline"], "mixed")
        self.assertEqual(read["content"], "a\nb\n")
        await self.refused("invalid_request", "file_patch", path="nested/mixed",
                           expected_sha256=read["sha256"], edits=[{"old_text": "a", "new_text": "z"}])
        self.assertEqual(disk.read_bytes(), original)
        changed = await self.call("file_patch", path="nested/mixed", newline="lf",
                                  expected_sha256=read["sha256"], edits=[{"old_text": "a", "new_text": "z"}])
        self.assertEqual(disk.read_bytes(), b"z\nb\n")
        await self.call("file_write", path="nested/mixed", content="wide\n", encoding="utf-16-be",
                        newline="crlf", expected_sha256=changed["sha256"])
        self.assertEqual(disk.read_bytes(), codecs.BOM_UTF16_BE + "wide\r\n".encode("utf-16-be"))
        await self.refused("invalid_request", "file_write", path="nested/invalid", content="x",
                           expected_sha256=None, encoding="utf-16-le", bom=False)
        self.assertFalse((self.root / "allowed/nested/invalid").exists())

    async def test_binary_ranges_hashes_and_write_boundary(self):
        import base64
        encode = lambda b: base64.b64encode(b).decode()
        original = b"\x00\xffPNG\r\n\x80"
        created = await self.call("file_write_bytes", path="nested/binary", data=encode(original),
                                  expected_sha256=None)
        await self.refused("invalid_request", "file_read", path="nested/binary")
        read = await self.call("file_read_bytes", path="nested/binary", offset=2, length=3)
        self.assertEqual(base64.b64decode(read["data"]), original[2:5])
        self.assertEqual(read["sha256"], created["sha256"])
        self.assertFalse(read["eof"])
        await self.refused("stale", "file_write_bytes", path="nested/binary", data="",
                           expected_sha256=None)
        await self.refused("invalid_request", "file_write_bytes", path="nested/binary", data="!!",
                           expected_sha256=created["sha256"])
        await self.refused("invalid_request", "file_write_bytes", path="nested/binary", data="",
                           expected_sha256=created["sha256"], offset=len(original)+1)
        changed = await self.call("file_write_bytes", path="nested/binary", data=encode(b"tail"),
                                  expected_sha256=created["sha256"], offset=len(original))
        self.assertEqual((self.root / "allowed/nested/binary").read_bytes(), original+b"tail")
        await self.refused("stale", "file_write_bytes", path="nested/binary", data="",
                           expected_sha256=created["sha256"])
        await self.call("file_write_bytes", path="nested/binary", data="", expected_sha256=changed["sha256"])
        empty = await self.call("file_read_bytes", path="nested/binary")
        self.assertEqual(empty["bytes"], 0)
        self.assertTrue(empty["eof"])

    async def test_binary_policy_preflight_and_batch(self):
        import base64
        encode = lambda b: base64.b64encode(b).decode()
        disk = self.root / "allowed/nested/original"
        disk.write_bytes(b"original")
        digest = (await self.call("file_read_bytes", path="nested/original"))["sha256"]
        (self.root / "allowed/nested/link").symlink_to(disk)
        os.link(disk, self.root / "allowed/nested/hard")
        os.mkfifo(self.root / "allowed/nested/fifo")
        for path in ("denied/secret", "../outside", "nested/link", "nested/fifo"):
            with self.subTest(path=path):
                await self.refused("forbidden", "file_read_bytes", path=path)
                await self.refused("forbidden", "file_write_bytes", path=path, data="",
                                   expected_sha256=digest)
        await self.refused("forbidden", "file_write_bytes", path="nested/hard", data="",
                           expected_sha256=digest)
        await self.refused("invalid_request", "file_write_bytes", path="nested/new", data="",
                           expected_sha256=None, offset=-1)
        await self.refused("invalid_request", "file_read_bytes", path="nested/original", offset=True)
        await self.refused("invalid_request", "file_read_bytes", path="nested/original", length=65537)
        self.assertEqual(disk.read_bytes(), b"original")
        steps = [
            {"id": "write", "method": "file_write_bytes", "arguments": {
                "root": "work", "path": "nested/batch-bytes", "data": encode(b"\xff\x00"),
                "expected_sha256": None}},
            {"id": "read", "method": "file_read_bytes", "arguments": {
                "root": "work", "path": "nested/batch-bytes"}}]
        result = await self.client.batch("test", {"operations": steps})
        self.assertEqual((self.root / "allowed/nested/batch-bytes").read_bytes(), b"\xff\x00")
        self.assertIn(encode(b"\xff\x00"), json.dumps(result))
        steps[0]["arguments"]["path"] = "nested/never-created"
        steps[1] = {"id": "bad", "method": "file_write_bytes", "arguments": {
            "root": "work", "path": "nested/bad", "data": "not-base64!", "expected_sha256": None}}
        with self.assertRaises(RefusalError):
            await self.client.batch("test", {"operations": steps})
        self.assertFalse((self.root / "allowed/nested/never-created").exists())

    @unittest.skipUnless(os.name == "posix" and shutil.which("setfacl"), "POSIX ACL tools needed")
    async def test_inherited_lab_acl_is_editable_without_widening_it(self):
        nested = self.root / "allowed/nested"
        subprocess.run(["setfacl", "-m", f"d:g:{os.getgid()}:r--", str(nested)],
                       check=True, capture_output=True)
        target = nested / "lab.txt"
        target.write_text("first")
        before_acl = os.getxattr(target, "system.posix_acl_access")
        before = target.stat()
        read = await self.call("file_read", path="nested/lab.txt")
        await self.call("file_patch", path="nested/lab.txt", expected_sha256=read["sha256"],
                        edits=[{"old_text": "first", "new_text": "second"}])
        self.assertEqual(target.read_text(), "second")
        self.assertEqual(os.getxattr(target, "system.posix_acl_access"), before_acl)
        self.assertEqual((target.stat().st_gid, stat.S_IMODE(target.stat().st_mode)),
                         (before.st_gid, stat.S_IMODE(before.st_mode)))

    async def test_unsupported_extended_metadata_is_still_refused(self):
        path = self.root / "allowed/nested/opaque.txt"
        path.write_text("original")
        os.setxattr(path, "user.opaque", b"keep")
        read = await self.call("file_read", path="nested/opaque.txt")
        await self.refused("forbidden", "file_write", path="nested/opaque.txt",
                           content="no", expected_sha256=read["sha256"])
        self.assertEqual(path.read_text(), "original")
        self.assertEqual(os.getxattr(path, "user.opaque"), b"keep")

    async def test_expected_errors_have_stable_raw_and_public_codes(self):
        raw = await self.raw({"operation": "file_read", "root": "work", "path": "missing"})
        self.assertEqual(raw, {"ok": False, "refusal": {"code": "not_found", "message": "file does not exist"}})
        with self.assertRaises(RefusalError) as caught:
            await self.call('file_read', path='missing')
        self.assertIsInstance(caught.exception, ToolError)
        # The public text is the refusal itself, as JSON data.
        self.assertEqual(json.loads(str(caught.exception)),
                         {"code": "not_found", "message": "file does not exist"})
        await self.refused('not_found', 'file_list', path='nowhere')
        created = await self.call('file_write', path='nested/version', content='a', expected_sha256=None)
        await self.refused('stale', 'file_write', path='nested/version', content='b', expected_sha256='0'*64)
        self.assertEqual((await self.call('file_read', path='nested/version'))['sha256'], created['sha256'])

    async def test_stale_edit_carries_the_current_hash(self):
        created = await self.call('file_write', path='nested/s.txt', content='one', expected_sha256=None)
        (self.root/'allowed/nested/s.txt').write_text('changed elsewhere')
        current = (await self.call('file_read', path='nested/s.txt'))['sha256']
        stale = await self.refused('stale', 'file_patch', path='nested/s.txt', expected_sha256=created['sha256'],
                                   edits=[{'old_text': 'one', 'new_text': 'two'}])
        self.assertEqual(stale['current_sha256'], current)
        self.assertNotEqual(current, created['sha256'])
        (self.root/'allowed/nested/s.txt').unlink()
        stale = await self.refused('stale', 'file_write', path='nested/s.txt', content='x', expected_sha256=current)
        self.assertIn('current_sha256', stale)
        self.assertIsNone(stale['current_sha256'])
        # Creation over a file that appeared: the hash of what is there now.
        (self.root/'allowed/nested/s.txt').write_text('appeared')
        stale = await self.refused('stale', 'file_write', path='nested/s.txt', content='x', expected_sha256=None)
        self.assertEqual(stale['current_sha256'],
                         (await self.call('file_read', path='nested/s.txt'))['sha256'])

    async def test_stale_race_inside_the_core_carries_the_current_hash(self):
        import boring_worker_kit.files as files_module
        created = await self.call('file_write', path='nested/r.txt', content='one', expected_sha256=None)
        original = files_module.verify_target
        def racing(parent, name, expected, previous, maximum, metadata=None):
            (self.root/'allowed/nested/r.txt').write_text('raced')
            return original(parent, name, expected, previous, maximum, metadata)
        files_module.verify_target = racing
        try:
            stale = await self.refused('stale', 'file_write', path='nested/r.txt', content='two',
                                       expected_sha256=created['sha256'])
        finally:
            files_module.verify_target = original
        self.assertEqual(stale['message'], 'stale edit: file changed')
        self.assertEqual(stale['current_sha256'],
                         (await self.call('file_read', path='nested/r.txt'))['sha256'])

    async def test_anything_raced_into_the_files_place_is_stale_not_a_failure(self):
        # A FIFO, symlink, second
        # link or directory raced in before the write is `stale`, never a
        # failure; nothing outside is touched.
        import boring_worker_kit.files as files_module
        outside = self.root / 'outside.txt'
        outside.write_text('outside')
        target = self.root / 'allowed/nested/r.txt'
        original = files_module.verify_target
        for kind in ('fifo', 'symlink', 'link', 'directory'):
            with self.subTest(raced=kind):
                created = await self.call('file_write', path='nested/r.txt', content='one',
                                          expected_sha256=None)
                def racing(parent, name, expected, previous, maximum, metadata=None, kind=kind):
                    if kind == 'link':
                        os.link(target, self.root / 'allowed/nested/r2.txt')
                    else:
                        target.unlink()
                        if kind == 'fifo':
                            os.mkfifo(target)
                        elif kind == 'symlink':
                            target.symlink_to(outside)
                        else:
                            target.mkdir()
                    return original(parent, name, expected, previous, maximum, metadata)
                files_module.verify_target = racing
                try:
                    stale = await self.refused('stale', 'file_write', path='nested/r.txt',
                                               content='two', expected_sha256=created['sha256'])
                finally:
                    files_module.verify_target = original
                self.assertIn('current_sha256', stale)
                self.assertEqual(outside.read_text(), 'outside')
                for name in ('r.txt', 'r2.txt'):
                    path = self.root / 'allowed/nested' / name
                    if path.is_dir() and not path.is_symlink():
                        path.rmdir()
                    elif path.exists() or path.is_symlink():
                        path.unlink()

    async def test_listing_and_search_survive_odd_names_and_vanishing_entries(self):
        nested = self.root / 'allowed/nested'
        (nested / 'ok.txt').write_text('needle')
        os.close(os.open(os.fsencode(nested) + b'/bad\xff.txt', os.O_CREAT | os.O_WRONLY, 0o600))
        listed = await self.call('file_list', path='nested')
        self.assertEqual([e['name'] for e in listed['entries']], ['ok.txt'])
        self.assertEqual(listed['skipped'], 1)
        found = await self.call('file_search', query='needle', path='nested')
        self.assertEqual([m['path'] for m in found['matches']], ['nested/ok.txt'])
        self.assertEqual(found['files_skipped'], 1)
        self.assertEqual((await self.refused('invalid_request', 'file_read', path='x' * 256))['message'],
                         'path component is too long')

    async def test_text_limit_and_escaped_frame_do_not_create_partial_files(self):
        # The configured 512 KiB measures UTF-8 file bytes, not JSON encoding.
        content = 'x' * (512 * 1024)
        written = await self.call('file_write', path='nested/large', content=content,
                                  expected_sha256=None)
        self.assertEqual(written['bytes'], len(content))
        large = self.root/'allowed/nested/large'
        self.assertEqual(large.stat().st_size, len(content))
        self.assertEqual(written['sha256'], hashlib.sha256(large.read_bytes()).hexdigest())
        # Control characters can expand sixfold in a JSON frame. Reject before effects.
        with self.assertRaisesRegex(ValueError, 'frame exceeds'):
            await self.call('file_write', path='nested/escaped', content='\x01' * (512 * 1024),
                            expected_sha256=None)
        self.assertFalse((self.root/'allowed/nested/escaped').exists())

    async def test_patch_rejects_overlapping_occurrences_of_one_match(self):
        first = await self.call("file_write", path="nested/ambiguous",
                                content="aaa", expected_sha256=None)
        await self.refused("invalid_request", "file_patch", path="nested/ambiguous",
                           expected_sha256=first["sha256"],
                           edits=[{"old_text": "aa", "new_text": "b"}])
        self.assertEqual((self.root / "allowed/nested/ambiguous").read_bytes(), b"aaa")

    async def test_journey_and_partial_edit_rejection(self):
        first = await self.call('file_write', path='nested/a.txt', content='alpha\r\nbeta\n', expected_sha256=None)
        read = await self.call('file_read', path='nested/a.txt', start_line=2, line_count=1)
        self.assertEqual(read['content'], 'beta\n')
        self.assertEqual(read['sha256'], first['sha256'])
        refusal = await self.refused('invalid_request', 'file_patch', path='nested/a.txt',
            expected_sha256=first['sha256'], edits=[
                {'old_text': 'alpha', 'new_text': 'gamma'}, {'old_text': 'missing', 'new_text': 'x'}])
        self.assertEqual(refusal['message'], 'edit text must occur exactly once')
        self.assertEqual((self.root/'allowed/nested/a.txt').read_bytes(), b'alpha\nbeta\n')
        patched = await self.call('file_patch', path='nested/a.txt', expected_sha256=first['sha256'], edits=[
            {'old_text': 'alpha', 'new_text': 'gamma'}, {'old_text': 'beta', 'new_text': 'delta'}])
        self.assertEqual(patched['applied_edits'], 2)
        self.assertEqual((await self.call('file_list', path='nested'))['entries'][0]['name'], 'a.txt')
        self.assertEqual((await self.call('file_search', query='delta'))['matches'][0]['line'], 2)
        self.assertIn('single-line', (await self.refused('invalid_request', 'file_search',
                                                         query='two\nlines'))['message'])
        stale = await self.refused('stale', 'file_write', path='nested/a.txt', content='lost',
                                   expected_sha256=first['sha256'])
        self.assertEqual(stale['current_sha256'], patched['sha256'])
        self.assertEqual((await self.call('file_read', path='nested/a.txt'))['sha256'], patched['sha256'])
        (self.root/'allowed/nested/a.txt').chmod(0o750)
        next_edit = await self.call('file_write', path='nested/a.txt', content='script', expected_sha256=patched['sha256'])
        self.assertEqual((self.root/'allowed/nested/a.txt').stat().st_mode & 0o777, 0o750)
        self.assertEqual((await self.call('file_read', path='nested/a.txt'))['sha256'], next_edit['sha256'])

    async def test_unauthorized_paths_and_strict_raw_inputs(self):
        (self.root/'allowed/denied/secret').write_text('secret')
        (self.root/'allowed/nested/link').symlink_to('/etc/passwd')
        os.mkfifo(self.root/'allowed/nested/pipe')
        for path in ('../outside', 'denied/secret', 'nested/link', 'nested/pipe'):
            with self.subTest(path=path):
                await self.refused('forbidden', 'file_read', path=path)
        listing = await self.call('file_list')
        self.assertNotIn('denied', [x['name'] for x in listing['entries']])
        for invalid in (True, '2', 0):
            with self.subTest(invalid=invalid):
                refusal = await self.refused('invalid_request', 'file_read', path='nested/link',
                                             start_line=invalid)
                self.assertTrue(any(f.endswith('start_line') for f in refusal['fields']), refusal)
        with self.assertRaises(RefusalError) as caught:
            await self.client.file_call('test', 'file_write', root='foreign', path='x', content='x', expected_sha256=None)
        self.assertEqual(caught.exception.data['code'], 'forbidden')
        with self.assertRaises(RefusalError) as caught:
            await self.client.execute('test', 'true')
        self.assertEqual(caught.exception.data['code'], 'forbidden')
        self.assertFalse((self.root/'allowed/nested/a.txt').exists())

    async def test_read_only_limits_hardlink_and_symlink_parent(self):
        (self.root/'allowed/nested/a.txt').write_text('one one')
        os.link(self.root/'allowed/nested/a.txt', self.root/'allowed/nested/hardlink')
        (self.root/'allowed/dirlink').symlink_to(self.root/'allowed/nested', target_is_directory=True)
        for path, code in (('nested/a.txt', 'forbidden'), ('dirlink/a.txt', 'invalid_request')):
            with self.subTest(path=path):
                await self.refused(code, 'file_write', path=path, content='two',
                    expected_sha256=(await self.call('file_read', path='nested/a.txt'))['sha256'])
        (self.root/'allowed/nested/hardlink').unlink()
        refusal = await self.refused('invalid_request', 'file_patch', path='nested/a.txt',
            expected_sha256=(await self.call('file_read', path='nested/a.txt'))['sha256'],
            edits=[{'old_text': 'one', 'new_text': 'two'}])
        self.assertIn('exactly once', refusal['message'])
        self.assertEqual((self.root/'allowed/nested/a.txt').read_text(), 'one one')
        self.worker.files.policy = FilePolicy.parse({'roots': {'work': {
            'path': str(self.root/'allowed'), 'read': True, 'write': False}}})
        self.assertEqual((await self.call('file_read', path='nested/a.txt'))['content'], 'one one')
        self.assertIn('disabled', (await self.refused('forbidden', 'file_write', path='nested/new',
                                                      content='x', expected_sha256=None))['message'])
        self.assertFalse((self.root/'allowed/nested/new').exists())

    async def test_bounded_search_incomplete_negative(self):
        (self.root/'allowed/nested/a.txt').write_text('not found')
        policy = FilePolicy.parse({'roots': {'work': {'path': str(self.root/'allowed'),
            'read': True, 'write': False}}, 'limits': {'max_search_entries': 1}})
        self.worker.files.policy = policy
        result = await self.call('file_search', query='needle', path='nested')
        self.assertEqual(result['matches'], [])
        self.assertFalse(result['truncated'])
        (self.root/'allowed/nested/b.txt').write_text('needle')
        result = await self.call('file_search', query='needle', path='nested')
        self.assertTrue(result['truncated'])
        self.assertEqual(result['reason'], 'entry_limit')

    async def test_simultaneous_create_only_one_wins(self):
        outcomes = await asyncio.gather(*(self.call('file_write', path='nested/race',
            content=str(i), expected_sha256=None) for i in range(8)), return_exceptions=True)
        self.assertEqual(sum(isinstance(x, dict) for x in outcomes), 1)
        (winner,) = [x for x in outcomes if isinstance(x, dict)]
        raw = (self.root/'allowed/nested/race').read_bytes()
        self.assertEqual(winner['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertIn(raw.decode(), {str(i) for i in range(8)})
        losers = [x for x in outcomes if isinstance(x, RefusalError)]
        self.assertEqual(len(losers), 7)
        self.assertEqual({x.data['code'] for x in losers}, {'stale'})
        self.assertEqual(len(list((self.root/'allowed/nested').glob('.boring-file-*'))), 0)


class PolicyTest(unittest.TestCase):
    def test_absent_disabled_and_invalid_config(self):
        self.assertFalse(FilePolicy.parse(None).roots)
        with tempfile.TemporaryDirectory() as d:
            base = {'roots': {'work': {'path': d, 'read': True, 'write': True}}}
            for broken in ({'limits': {'max_file_bytes': True}},
                           {'roots': {'work': {'path': d, 'read': 'yes', 'write': False}}},
                           {'roots': {'work': {'path': d, 'read': False, 'write': True}}},
                           {'roots': {'work': {'path': d, 'read': True, 'write': True, 'deny': ['../x']}}}):
                with self.subTest(broken=broken), self.assertRaises(ValueError):
                    FilePolicy.parse(broken)
            self.assertTrue(FilePolicy.parse(base).roots['work'].write)
