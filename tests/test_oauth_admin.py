from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import sys
import pwd
from unittest.mock import patch
from pathlib import Path
import sqlite3
from contextlib import closing
import tempfile
import unittest

from boring_crocodile.oauth_admin import OAuthAdmin
from boring_crocodile.oauth_records import initialize, secret_key, service
from boring_crocodile.site_layout import site_lock
from pond_config import PROFILE_ID, dynamic, one_pond


class OAuthAdminTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "state"
        self.root.mkdir(mode=0o700)
        self.path = self.root / "oauth.sqlite3"
        with closing(sqlite3.connect(self.path)) as db, db:
            initialize(db)
            db.execute("INSERT INTO clients VALUES ('client1', 'dynamic', 'active', ?, 0, 0, NULL, NULL)",
                       (json.dumps({"client_id": "client1", "client_name": "Test client"}),))
            for pond, client, pair in (("alpha", "client1", "a"), ("beta", "client1", "b"),
                                        ("alpha", "client2", "c")):
                for kind in ("code", "access", "refresh"):
                    self.put(db, kind, kind+pair, {"subject": pond, "client_id": client,
                             "scopes": ["mcp"]}, pair)
            self.put(db, "pending", "pending", {"request":"opaque"})
        os.chmod(self.path, 0o600)
        self.controller_uid = os.getuid() or pwd.getpwnam("nobody").pw_uid
        if os.getuid() == 0:
            os.chown(self.root, self.controller_uid, -1)
            os.chown(self.path, self.controller_uid, -1)
        self.store = OAuthAdmin(self.path, self.controller_uid)

    def put(self, db, kind, key, value, pair=None):
        db.execute("INSERT INTO records VALUES (?,?,?,?,?)",
                   (kind,key,json.dumps(value), 9999999999, pair))

    def test_service_add_list_remove(self):
        added = self.store.service_add("canvas", "http://127.0.0.1:18511/mcp", {'alpha': 'writer', 'beta': 'writer'}, roles=["reader", "writer"], default_role="reader")
        self.assertEqual(set(added), {"name", "client_id", "client_secret", "url", "callers", "roles", "default_role", "ping_timeout"})
        self.assertTrue(added["client_id"].startswith("svc_"))
        listed = self.store.service_list()
        self.assertEqual(listed, [{"name": "canvas", "client_id": added["client_id"],
                                   "url": "http://127.0.0.1:18511/mcp", "callers": {"alpha": "writer", "beta": "writer"},
                                   "roles": ["reader", "writer"], "default_role": "reader",
                                   "ping_timeout": 1.0}])
        self.assertNotIn(added["client_secret"], self.path.read_bytes().decode("latin-1"))
        with closing(sqlite3.connect(self.path)) as db, db:
            stored = service(db, client_id=added["client_id"])
            self.assertEqual(stored["secret_sha256"], secret_key(added["client_secret"]))
            # A token minted for it goes with it; other records stay.
            self.put(db, "service", "token-hash", {"subject": "alpha", "client_id": added["client_id"],
                                                   "scopes": []})
        self.assertEqual(self.store.status()["counts"]["service"], 1)
        with self.assertRaisesRegex(ValueError, "exists"):
            self.store.service_add("canvas", "http://127.0.0.1:1/mcp", {'alpha': 'writer'}, roles=["reader", "writer"], default_role="reader")
        for name, url, ponds in (("Canvas", "http://h/mcp", {"alpha": "writer"}), ("-x", "http://h/mcp", {"alpha": "writer"}),
                                 ("x", "ftp://h/mcp", {"alpha": "writer"}), ("x", "/mcp", {"alpha": "writer"}),
                                 ("x", "http://u:p@h/mcp", {"alpha": "writer"}), ("x", "http://h/mcp?q", {"alpha": "writer"}),
                                 ("x", "http://h/mcp", []), ("x", "http://h/mcp", ["alpha", "alpha"])):
            with self.subTest(name=name, url=url, ponds=ponds), self.assertRaises(ValueError):
                self.store.service_add(name, url, ponds, roles=["reader", "writer"], default_role="reader")
        self.assertEqual(len(self.store.service_list()), 1)
        self.assertEqual(self.store.service_remove("canvas"), {"status": "removed", "name": "canvas"})
        self.assertEqual(self.store.service_list(), [])
        self.assertEqual(self.store.status()["counts"]["service"], 0)
        self.assertEqual(self.store.status()["counts"]["access"], 3)
        with self.assertRaisesRegex(ValueError, "no service"):
            self.store.service_remove("canvas")

    def test_guarded_service_removal_preserves_reconfigured_and_recreated_service(self):
        self.store.service_add("devbox", "http://127.0.0.1:18520/mcp", {'alpha': 'writer'}, roles=["reader", "writer"], default_role="reader")
        expected = self.store.service_list()[0]
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE services SET callers=? WHERE name='devbox'", (json.dumps({"beta": "writer"}),))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "definition changed"):
            self.store.service_remove("devbox", expected)
        self.assertEqual(self.path.read_bytes(), before)
        self.store.service_remove("devbox")
        self.store.service_add("devbox", "http://127.0.0.1:18520/mcp", {'alpha': 'writer'}, roles=["reader", "writer"], default_role="reader")
        with self.assertRaisesRegex(ValueError, "definition changed"):
            self.store.service_remove("devbox", expected)
        current = self.store.service_list()[0]
        self.assertEqual(self.store.service_remove("devbox", current)["status"], "removed")
        self.assertEqual(self.store.service_list(), [])

    def test_service_cli_checks_ponds_and_prints_the_secret_once(self):
        from boring_crocodile import admin
        from types import SimpleNamespace
        from boring_crocodile.site_ports import DEFAULT_PROTECTED_PORTS

        settings = SimpleNamespace(ponds={"alpha": object(), "beta": object()},
                                   pirate_service_callers={"parrot": 993},
                                   protected_ports=DEFAULT_PROTECTED_PORTS, bind_port=8503)
        from boring_crocodile.site_render import ServicePortReport

        errors = []
        def run(*argv, confirmation=None, rendered=DEFAULT_PROTECTED_PORTS):
            if argv[0] == "service-add":
                argv += ("--roles", "reader,writer", "--default-role", "reader")
            out = io.StringIO()
            input_stream = io.StringIO(confirmation or "")
            input_stream.isatty = lambda: confirmation is not None
            errors.append(io.StringIO())
            with patch.object(sys, "argv", ["boring-crocodile-admin", *argv]), \
                    patch.object(admin.os, "geteuid", return_value=0), \
                    patch.object(admin.Settings, "load", return_value=settings), \
                    patch.object(admin, "oauth_store", return_value=self.store), \
                    patch.object(admin, "_rendered_service_ports", return_value=ServicePortReport(rendered)), \
                    patch.object(admin, "site_lock", side_effect=lambda path: site_lock(self.root, owner=os.getuid())), \
                    patch.object(sys, "stdin", input_stream), \
                    redirect_stdout(out), redirect_stderr(errors[-1]):
                try:
                    admin.main()
                except SystemExit as exit:
                    return exit.code, out.getvalue()
            return 0, out.getvalue()
        code, _ = run("service-add", "canvas", "--url", "http://[::1]:19012/mcp", "--callers", "alpha=writer")
        self.assertNotEqual(code, 0)
        self.assertIn("canvas: unprotected loopback service port 19012", errors[-1].getvalue())
        code, _ = run("service-add", "canvas", "--url", "http://127.0.0.1:18511/mcp",
                      "--callers", "alpha=writer", rendered=())
        self.assertNotEqual(code, 0)
        self.assertIn("loopback port 18511 has no confirmed rendered bind protection", errors[-1].getvalue())
        code, _ = run("service-add", "canvas", "--url", "http://127.0.0.1:18511/mcp", "--callers", "alpha=writer,gamma=writer")
        self.assertEqual(code, 2)
        self.assertEqual(self.store.service_list(), [])
        code, out = run("service-add", "canvas", "--url", "http://127.0.0.1:18511/mcp", "--callers", "alpha=writer,beta=writer")
        self.assertEqual(code, 0)
        self.assertNotIn("client_secret", json.loads(out))
        self.assertEqual(self.store.service_list(), [])
        self.assertEqual(run("service-add", "canvas", "--url", "http://127.0.0.1:18511/mcp",
                             "--callers", "alpha=writer,beta=writer", "--apply")[0], 2)
        self.assertEqual(self.store.service_list(), [])
        code, out = run("service-add", "canvas", "--url", "http://127.0.0.1:18511/mcp",
                        "--callers", "alpha=writer,beta=writer", "--apply", confirmation="APPLY\n")
        self.assertEqual(code, 0)
        # stdout is the JSON answer alone (pipeable); the human reminder goes to stderr.
        self.assertIn("client_secret", json.loads(out))
        self.assertNotIn("shown only now", out)
        self.assertIn("shown only now", errors[-1].getvalue())
        code, out = run("service-list")
        self.assertEqual([item["name"] for item in json.loads(out)], ["canvas"])
        self.assertNotIn("secret", out)
        self.assertEqual(run("service-endpoint", "canvas", "--url", "https://canvas.test/mcp", "--apply")[0], 2)
        code, out = run("service-endpoint", "canvas", "--url", "https://canvas.test/mcp")
        self.assertEqual(code, 0)
        endpoint_plan = json.loads(out)
        code, out = run("service-endpoint", "canvas", "--url", endpoint_plan['after'],
                        "--apply", confirmation="APPLY\n")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)['applied'])
        current = self.store.service_list()[0]
        before = self.path.read_bytes()
        with site_lock(self.root, owner=os.getuid()):
            for command in (
                    ("service-add", "new", "--url", "https://new.test/mcp", "--callers", "alpha=writer"),
                    ("service-grants", "canvas", "--callers", "alpha=writer"),
                    ("service-endpoint", "canvas", "--url", "https://changed.test/mcp"),
                    ("service-remove", "canvas")):
                self.assertEqual(run(*command, "--apply", confirmation="APPLY\n")[0], 2)
                self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(run("service-remove", "canvas", "--expected-definition", "{}")[0], 2)
        self.assertEqual(len(self.store.service_list()), 1)
        self.assertEqual(run("service-remove", "canvas", "--expected-definition", json.dumps(current))[0], 0)
        self.assertEqual(len(self.store.service_list()), 1)
        self.assertEqual(run("service-remove", "canvas", "--apply")[0], 2)
        self.assertEqual(run("service-remove", "canvas", "--expected-definition", json.dumps(current),
                             "--apply", confirmation="APPLY\n")[0], 0)
        self.assertEqual(run("service-remove", "canvas")[0], 2)

    def test_service_plans_are_readonly_and_bind_the_credential_identity(self):
        before = self.path.read_bytes()
        with patch("boring_crocodile.oauth_records.secrets.token_urlsafe",
                   side_effect=AssertionError("planning generated a credential")):
            plan = self.store.plan_service_add("canvas", "https://canvas.test/mcp", {'alpha': 'writer'}, roles=["reader", "writer"], default_role="reader")
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.store.service_add("canvas", "https://changed.test/mcp", {'alpha': 'writer'},
                                   digest=plan["plan_digest"], roles=["reader", "writer"], default_role="reader")
        self.assertEqual(self.store.service_list(), [])
        added = self.store.service_add("canvas", "https://canvas.test/mcp", {'alpha': 'writer'},
                                       digest=plan["plan_digest"], roles=["reader", "writer"], default_role="reader")
        before = self.path.read_bytes()
        removal = self.store.plan_service_remove("canvas", self.store.service_list()[0])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn(added["client_secret"], json.dumps(removal))
        self.assertNotIn("secret_sha256", removal)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE services SET secret_sha256=? WHERE name='canvas'",
                       (secret_key("synthetic replacement"),))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "stale"):
            self.store.service_remove("canvas", digest=removal["plan_digest"])
        self.assertEqual(self.path.read_bytes(), before)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)

    def test_scoped_revoke_and_stale_refresh(self):
        plan = self.store.plan_revoke("alpha", "client1")
        self.assertEqual(plan["records"], {"code":1, "access":1, "refresh":1})
        self.assertNotIn("accessa", json.dumps(plan))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM records WHERE kind='refresh' AND key='refresha'")
            self.put(db, "refresh", "rotated", {"subject":"alpha", "client_id":"client1",
                     "scopes":["mcp"]}, "next")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.store.revoke("alpha", "client1", plan["plan_digest"])
        newer = self.store.plan_revoke("alpha", "client1")
        self.assertEqual(self.store.revoke("alpha", "client1", newer["plan_digest"])["records"],3)
        self.assertEqual(self.store.status("alpha")["counts"]["refresh"],1)
        self.assertEqual(self.store.status("alpha")["expiry"]["refresh"]["active"],1)
        self.assertEqual(self.store.status("beta")["counts"]["refresh"],1)
        self.assertEqual(self.store.status()["counts"]["client"],1)
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute("SELECT count(*) FROM records WHERE kind='pending'").fetchone()[0],1)

    def test_authorizations_are_summaries(self):
        rows = self.store.authorizations("alpha")
        self.assertEqual([r["client_id"] for r in rows], ["client1", "client2"])
        self.assertEqual(rows[0]["client_name"], "Test client")
        self.assertEqual(rows[0]["active_counts"]["refresh"],1)
        self.assertNotIn("accessa", json.dumps(rows))

    def test_private_source_is_required_and_inspection_does_not_create(self):
        missing = self.root / "missing.sqlite3"
        with self.assertRaises(FileNotFoundError): OAuthAdmin(missing, self.controller_uid)
        self.assertFalse(missing.exists())
        os.chmod(self.path, 0o644)
        with self.assertRaises(ValueError): OAuthAdmin(self.path, self.controller_uid)

    def test_malformed_record_fails_without_exposing_payload(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE records SET payload='SECRET malformed' WHERE key='accessa'")
        with self.assertRaisesRegex(ValueError, '^malformed OAuth record$'):
            self.store.authorizations("alpha")

    def test_approval_digest_ignores_bearer_values_and_record_order(self):
        from boring_crocodile.oauth_records import approval_digest
        records = [("access", "old-bearer", json.dumps({
            "subject":"alpha", "client_id":"client1", "scopes":["mcp"],
            "token":"old-bearer"}), 1000, "issuance-1")]
        changed = [("access", "new-bearer", records[0][2].replace("old-bearer", "new-bearer"),
                    1000, "issuance-1")]
        digest = approval_digest("alpha", "client1", records)
        self.assertEqual(digest, approval_digest("alpha", "client1", changed))
        self.assertNotEqual(digest, approval_digest("alpha", "client1",
            [(*changed[0][:4], "issuance-2")]))
        self.assertEqual(approval_digest("alpha", None, records+changed),
                         approval_digest("alpha", None, changed+records))

    def test_readonly_summary_does_not_mutate_database(self):
        before = self.path.read_bytes()
        self.store.status()
        self.store.authorizations("alpha")
        self.assertEqual(before, self.path.read_bytes())

    def test_backup_mechanics_with_synthetic_root_ownership(self):
        output = Path(self.temp.name) / "copy.sqlite3"
        real_lstat, real_fstat = Path.lstat, os.fstat
        def root_info(info):
            fields = list(info)
            fields[4] = 0
            return os.stat_result(fields)
        def destination_parent(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            return root_info(info) if path == output.parent else info
        before = self.path.read_bytes()
        with patch.object(Path, "lstat", destination_parent), patch(
                "boring_crocodile.oauth_admin.os.fstat",
                side_effect=lambda fd: root_info(real_fstat(fd))):
            self.assertEqual(self.store.backup(output)["integrity"], "ok")
            with self.assertRaises(FileExistsError):
                self.store.backup(output)
            link = output.parent / "link.sqlite3"
            link.symlink_to(output)
            with self.assertRaises(FileExistsError):
                self.store.backup(link)
            self.assertTrue(link.is_symlink())
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(before, self.path.read_bytes())
        with closing(sqlite3.connect(output)) as db, db:
            self.assertEqual(db.execute("SELECT count(*) FROM records").fetchone()[0], 10)

    @unittest.skipUnless(os.getuid() == 0, "root-only backup operation")
    def test_private_backup(self):
        output = Path(self.temp.name) / "copy.sqlite3"
        result = self.store.backup(output)
        self.assertEqual(result["integrity"], "ok")
        self.assertEqual(output.stat().st_mode & 0o777,0o600)
        with self.assertRaises(FileExistsError): self.store.backup(output)



    def test_endpoint_change_preserves_identity_grants_and_records(self):
        created = self.store.service_add('canvas', 'http://127.0.0.1:18511/mcp', {'alpha': 'writer'}, roles=["reader", "writer"], default_role="reader")
        self.store.service_add('knowledge', 'https://kb.test/mcp', {'beta': 'writer'}, roles=["reader", "writer"], default_role="reader")
        with closing(sqlite3.connect(self.path)) as db, db:
            before = service(db, name='canvas')
            self.put(db, 'service', 'old-service-token', {'subject':'alpha',
                'client_id':created['client_id'], 'scopes':[]})
        plan = self.store.service_endpoint('canvas', 'https://canvas.test/mcp')
        self.assertFalse(plan['applied'])
        self.assertEqual(self.store.service_list()[0]['url'], before['url'])
        changed = self.store.service_endpoint('canvas', plan['after'], plan['plan_digest'])
        self.assertTrue(changed['applied'])
        with closing(sqlite3.connect(self.path)) as db, db:
            after = service(db, name='canvas')
            self.assertEqual(service(db, name='knowledge')['url'], 'https://kb.test/mcp')
            self.assertEqual(db.execute('SELECT count(*) FROM records').fetchone()[0], 11)
        for key in ('client_id', 'secret_sha256', 'callers', 'roles', 'default_role', 'ping_timeout'):
            self.assertEqual(before[key], after[key])
        self.assertEqual(after['url'], plan['after'])
        self.assertNotIn(created['client_secret'], json.dumps(changed))
        self.assertNotIn(before['secret_sha256'], json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.store.service_endpoint('canvas', plan['after'], plan['plan_digest'])
        rollback = self.store.service_endpoint('canvas', before['url'])
        self.store.service_endpoint('canvas', before['url'], rollback['plan_digest'])
        self.assertEqual(self.store.service_list()[0]['url'], before['url'])

    def test_endpoint_plan_rejects_changed_callers_and_invalid_url(self):
        self.store.service_add('canvas', 'https://old.test/mcp', {'alpha': 'writer'}, roles=["reader", "writer"], default_role="reader")
        plan = self.store.service_endpoint('canvas', 'https://new.test/mcp')
        grants = self.store.service_grants('canvas', {"alpha":"writer","pirate/parrot":"writer"})
        self.store.service_grants('canvas', grants['after'], grants['plan_digest'])
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.store.service_endpoint('canvas', plan['after'], plan['plan_digest'])
        with self.assertRaises(ValueError):
            self.store.service_endpoint('canvas', 'http://remote.test/mcp')
        with self.assertRaisesRegex(ValueError, 'no such service'):
            self.store.service_endpoint('missing', 'https://new.test/mcp')

    def test_grant_plan_preserves_credentials_and_rejects_stale_plan(self):
        created = self.store.service_add('canvas', 'https://canvas.test/mcp', {'alpha': 'writer'}, roles=["reader", "writer"], default_role="reader")
        with closing(sqlite3.connect(self.path)) as db, db:
            before = service(db, name='canvas')
        plan = self.store.service_grants('canvas', {"alpha":"writer","pirate/parrot":"writer"})
        self.assertFalse(plan['applied'])
        self.assertEqual(self.store.service_list()[0]['callers'], {'alpha': 'writer'})
        changed = self.store.service_grants('canvas', {"alpha":"writer","pirate/parrot":"writer"}, plan['plan_digest'])
        self.assertTrue(changed['applied'])
        with closing(sqlite3.connect(self.path)) as db, db:
            after = service(db, name='canvas')
        for key in ('client_id','secret_sha256','url','ping_timeout'):
            self.assertEqual(before[key], after[key])
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.store.service_grants('canvas', {"alpha":"writer","pirate/parrot":"writer"}, plan['plan_digest'])
        self.assertNotIn('secret', json.dumps(changed))

    def table(self, name):
        with closing(sqlite3.connect(self.path)) as db:
            return sorted(db.execute(f"SELECT * FROM {name}").fetchall(), key=repr)

    def add_client_row(self, client_id, origin):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO clients VALUES (?, ?, 'active', ?, 0, 0, NULL, NULL)",
                       (client_id, origin, json.dumps({"client_id": client_id, "client_name": client_id})))
            self.put(db, "access", "access-" + client_id, {"subject": "alpha", "client_id": client_id,
                                                         "scopes": ["mcp"]}, "p-" + client_id)

    def test_client_remove_reviews_then_removes_the_client_and_its_grants(self):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "no client"):
            self.store.client_remove("ghost", "0" * 64)
        review = self.store.client_remove("client1")
        self.assertEqual((review["applied"], review["client"]["client_id"]), (False, "client1"))
        self.assertEqual(review["client"]["refresh_tokens"], 2)
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.store.client_remove("client1", "0" * 64)
        records = self.table("records")
        self.assertEqual(len(self.table("clients")), 1)
        self.assertTrue(self.store.client_remove("client1", review["plan_digest"])["applied"])
        self.assertEqual(self.table("clients"), [])
        kept = [row for row in records if json.loads(row[2]).get("client_id") != "client1"]
        self.assertEqual(self.table("records"), kept)
        self.assertEqual(len(records) - len(kept), 6)
        self.assertEqual({json.loads(row[2]).get("client_id") for row in kept}, {"client2", None})

    def test_client_prune_reviews_then_removes_only_idle_dynamic_clients(self):
        self.add_client_row("idle", "dynamic")
        self.add_client_row("pre-registered", "admin")
        before = self.path.read_bytes()
        review = self.store.client_prune(30)
        self.assertEqual((review["applied"], [c["client_id"] for c in review["clients"]]), (False, ["idle"]))
        self.assertEqual(self.path.read_bytes(), before)
        records = self.table("records")
        # A client that turns idle after the review is not in the approved plan:
        # the apply refuses and removes nothing.
        self.add_client_row("later", "dynamic")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.store.client_prune(30, review["plan_digest"])
        self.assertEqual({row[0] for row in self.table("clients")}, {"client1", "idle", "pre-registered", "later"})
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM records WHERE key='access-later'")
            db.execute("DELETE FROM clients WHERE client_id='later'")
        self.assertEqual(self.table("records"), records)
        pruned = self.store.client_prune(30, review["plan_digest"])
        self.assertEqual((pruned["applied"], [c["client_id"] for c in pruned["clients"]]), (True, ["idle"]))
        self.assertEqual([row[0] for row in self.table("clients")], ["client1", "pre-registered"])
        self.assertEqual(self.table("records"), [row for row in records if row[1] != "access-idle"])
        self.assertIn("access-pre-registered", [row[1] for row in self.table("records")])


@unittest.skipIf(os.getuid() == 0, "run concurrent provider fixture as a non-root controller")
class OAuthProviderAdminRaceTest(unittest.TestCase):
    def test_scoped_revoke_serializes_with_real_code_exchange_and_refresh(self):
        import asyncio
        import concurrent.futures
        import threading
        import time
        from mcp.server.auth.provider import AuthorizationCode, TokenError
        from mcp.shared.auth import OAuthClientInformationFull
        from pydantic import AnyUrl
        from boring_crocodile.sqlite_oauth_provider import SQLiteOAuthProvider

        for exchange_kind in ("code", "refresh"):
            with self.subTest(exchange=exchange_kind), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "oauth.sqlite3"
                provider = SQLiteOAuthProvider("https://synthetic.test", path, one_pond(Path(directory)), dynamic())
                client = OAuthClientInformationFull(client_id="synthetic-client",
                    redirect_uris=[AnyUrl("https://synthetic.test/callback")])
                asyncio.run(provider.register_client(client))
                code = AuthorizationCode(code="synthetic-code", client_id=client.client_id,
                    subject=PROFILE_ID, scopes=["mcp"], expires_at=time.time()+300,
                    code_challenge="synthetic-challenge",
                    redirect_uri=AnyUrl("https://synthetic.test/callback"),
                    redirect_uri_provided_explicitly=True)
                with provider._transaction() as db:
                    provider._put(db, "code", code.code, code)
                if exchange_kind == "refresh":
                    issued = asyncio.run(provider.exchange_authorization_code(client, code))
                    grant = asyncio.run(provider.load_refresh_token(client, issued.refresh_token))
                admin = OAuthAdmin(path, os.getuid())
                plan = admin.plan_revoke(PROFILE_ID, client.client_id)
                barrier = threading.Barrier(2)
                def revoke():
                    barrier.wait(timeout=5)
                    try:
                        admin.revoke(PROFILE_ID, client.client_id, plan["plan_digest"])
                        return "revoked"
                    except ValueError as error:
                        if "stale" not in str(error):
                            raise
                        return "stale"
                def exchange():
                    barrier.wait(timeout=5)
                    try:
                        if exchange_kind == "code":
                            asyncio.run(provider.exchange_authorization_code(client, code))
                        else:
                            asyncio.run(provider.exchange_refresh_token(client, grant, []))
                        return "issued"
                    except TokenError:
                        return "rejected"
                with concurrent.futures.ThreadPoolExecutor(2) as pool:
                    revocation = pool.submit(revoke)
                    issuance = pool.submit(exchange)
                    outcome = (revocation.result(timeout=10), issuance.result(timeout=10))
                self.assertIn(outcome, (("revoked", "rejected"), ("stale", "issued")))
                if outcome[0] == "stale":
                    fresh = admin.plan_revoke(PROFILE_ID, client.client_id)
                    admin.revoke(PROFILE_ID, client.client_id, fresh["plan_digest"])
                self.assertEqual(admin.status(PROFILE_ID)["counts"]["access"], 0)
                self.assertEqual(admin.status(PROFILE_ID)["counts"]["refresh"], 0)
                self.assertIsNotNone(asyncio.run(provider.get_client(client.client_id)))


if __name__ == "__main__":
    unittest.main()
