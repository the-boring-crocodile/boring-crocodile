"""Pirate admission edits config and uses the one renderer on the shared host."""
from dataclasses import replace
from contextlib import closing
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest

from boring_crocodile import site_pirates, site_render, oauth_records
from boring_crocodile.site_model import SiteDescription
from boring_crocodile.site_layout import site_lock, SiteLocked
from site_host import FakeHost


class PirateTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.host = FakeHost(Path(self.directory.name))
        database = self.host.path(self.host.site.settings.oauth_database_file)
        database.parent.mkdir(parents=True, mode=0o700)
        with closing(sqlite3.connect(database)) as db, db:
            oauth_records.initialize(db)
        database.chmod(0o600)
        self.store = self.host.oauth_store(self.host.site)

    def plan(self, kind="add", *args, **options):
        planner = site_pirates.plan_add if kind == "add" else site_pirates.plan_remove
        args = args or (("parrot", "pirate-parrot", ["pond"]) if kind == "add" else ("parrot",))
        return planner(*args, paths=self.host.paths, host=self.host, **options)[0]

    def apply(self, kind="add", *args, **options):
        args = args or (("parrot", "pirate-parrot", ["pond"]) if kind == "add" else ("parrot",))
        proposal = self.plan(kind, *args, **options)
        return site_pirates.apply(kind, proposal["plan_digest"], *args, paths=self.host.paths, host=self.host, **options)

    def test_retirement_after_temporary_removal_revokes_only_this_caller(self):
        shared = self.store.service_add("shared", "https://example.test/shared", {'pond': 'writer', 'pirate/parrot': 'writer'}, roles=["reader", "writer"], default_role="reader")
        sole = self.store.service_add("sole", "https://example.test/sole", {'pirate/parrot': 'writer'}, roles=["reader", "writer"], default_role="reader")
        with closing(sqlite3.connect(self.store.path)) as db, db:
            for caller in ("pond", "pirate/parrot"):
                db.execute("INSERT INTO records VALUES (?,?,?,?,?)", ("service", caller,
                           json.dumps({"subject": caller, "client_id": shared["client_id"], "scopes": []}),
                           9999999999, None))
        self.apply("remove")
        self.assertEqual(sum("pirate/parrot" in s["callers"] for s in self.store.service_list()), 2)
        self.apply("remove", retire=True)
        self.assertFalse(any("pirate/parrot" in s["callers"] for s in self.store.service_list()))
        listed = {s["name"]: s for s in self.store.service_list()}
        self.assertEqual(listed["shared"]["callers"], {"pond": "writer"})
        self.assertEqual(listed["sole"]["callers"], {})
        self.assertEqual(listed["shared"]["client_id"], shared["client_id"])
        self.assertEqual(listed["sole"]["client_id"], sole["client_id"])
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute("SELECT key FROM records").fetchall(), [("pond",)])
        self.apply("remove", retire=True)  # idempotent after admission and grants have gone

    def test_readmission_shows_existing_grants_and_refuses_a_stale_plan(self):
        self.store.service_add("knowledge", "https://example.test/mcp", {'pond': 'writer', 'pirate/parrot': 'writer'}, roles=["reader", "writer"], default_role="reader")
        self.apply("remove")
        proposal = self.plan()
        self.assertEqual([s["name"] for s in proposal["existing_service_grants"]], ["knowledge"])
        self.assertIn("Existing service grants (kept)", site_pirates.review(proposal))
        self.assertIn("knowledge", site_pirates.review(proposal))
        changed = self.store.service_grants("knowledge", {"pond":"writer"})
        self.store.service_grants("knowledge", changed["after"], changed["plan_digest"])
        calls = list(self.host.calls)
        with self.assertRaisesRegex(ValueError, "changed"):
            site_pirates.apply("add", proposal["plan_digest"], "parrot", "pirate-parrot", ["pond"],
                               paths=self.host.paths, host=self.host)
        self.assertEqual(self.host.calls, calls)
        self.assertNotIn("parrot", SiteDescription.read(self.host.paths.config).settings.pirate_service_callers)
        with self.assertRaisesRegex(ValueError, "grants changed"):
            self.store.retire_caller("pirate/parrot", proposal["existing_service_grants"])
        self.assertEqual(self.store.service_list()[0]["callers"], {"pond": "writer"})

    def test_admit_rerun_repairs_group_failure_and_keeps_installer_output(self):
        self.host.fail = "-a"
        with self.assertRaisesRegex(ValueError, "Rerun"):
            self.apply()
        self.assertEqual(SiteDescription.read(self.host.paths.config).settings.pirate_callers["parrot"].ponds, ("pond",))
        self.assertNotIn("demo-pond", self.host.role_members["pirate-clients"])
        self.apply()
        described = self.plan()
        self.assertEqual(described["status"], "already_serves")
        self.assertEqual(described["ponds"], ["pond"])
        self.assertEqual(described["service_authority"]["uid"], 1200)
        self.assertIn("demo-pond", self.host.role_members["pirate-clients"])
        self.host.calls.clear()
        self.apply()  # APPLY never returns early even when the installer readback is already_serves.
        self.assertIn(["/usr/bin/systemctl", "restart", site_render.CONTROLLER], self.host.calls)
        self.assertFalse(any("stop" in c and site_render.LISTENER in c for c in self.host.calls))

    def test_remove_rerun_uses_roster_after_saved_config_and_failed_group_change(self):
        self.apply()
        self.host.role_members["pirate-clients"].add("pirate-parrot")  # The Pirate owns its socket role.
        self.host.fail = "-d"
        with self.assertRaisesRegex(ValueError, "Rerun"):
            self.apply("remove")
        self.assertNotIn("parrot", SiteDescription.read(self.host.paths.config).settings.pirate_service_callers)
        self.assertIn("pirate-parrot", self.host.role_members["pond-pirates"])
        self.apply("remove")
        self.assertNotIn("pirate-parrot", self.host.role_members["pond-pirates"])
        self.assertEqual(self.host.role_members["pirate-clients"], {"pirate-parrot"})
        self.assertNotIn("parrot", SiteDescription.read(self.host.paths.config).settings.pirate_callers)
        self.apply("remove")  # Idempotent removal also repairs.

    def test_identity_closed_pond_and_group_binding_refuse_before_changes(self):
        identity = self.host.identities["pirate-parrot"]
        self.host.identities["other-pirate"] = replace(identity, name="other-pirate", uid=1500, group="other-pirate", gid=1500)
        for account, ponds in (("other-pirate", ["pond"]), ("pirate-parrot", ["garden"]),
                                ("missing", ["pond"]), ("demo-pond", ["pond"])):
            with self.subTest(account=account, ponds=ponds), self.assertRaises(ValueError):
                self.plan("add", "parrot", account, ponds)
        self.host.main["pirate_services"]["callers"] = {}
        self.host.save()
        # Native membership outside the admission roster is exposed as a removal in the plan.
        proposal, _ = site_render.plan(paths=self.host.paths, host=self.host)
        row = next(g for g in proposal["groups"] if g["group"] == "pond-pirates")
        self.assertEqual(row["remove"], ["pirate-parrot"])
        calls = list(self.host.calls)
        with site_lock(self.host.paths.state, owner=self.host.owner):
            with self.assertRaises(SiteLocked):
                self.apply()
        self.assertEqual(self.host.calls, calls)
