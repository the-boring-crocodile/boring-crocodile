"""Extension is a candidate edit; grants use the real generation-6 store."""
from contextlib import closing
from copy import deepcopy
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from boring_crocodile import site_extend, site_render, oauth_records
from boring_crocodile.oauth_admin import OAuthAdmin
from boring_crocodile.site_model import SiteDescription
from site_host import FakeHost


class ExtendTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.host = FakeHost(Path(self.directory.name))
        path = self.host.root / "store"
        path.mkdir(mode=0o700)
        database = path / "oauth.sqlite3"
        with closing(sqlite3.connect(database)) as db, db:
            oauth_records.initialize(db)
            for name in ("canvas", "knowledge"):
                oauth_records.add_service(db, name, "https://"+name+".test/mcp", {'pond': 'writer'}, roles=["reader", "writer"], default_role="reader")
        database.chmod(0o600)
        self.host.store = OAuthAdmin(database, os.getuid())

    def plan(self, grant=None, **options):
        return site_extend.plan(grant, paths=self.host.paths, host=self.host, **options)[0]

    def apply(self, grant=None, **options):
        proposal = self.plan(grant, **options)
        return site_extend.apply(proposal["plan_digest"], grant, paths=self.host.paths, host=self.host, **options)

    def test_omitted_selection_never_activates_or_grants_a_parked_pond(self):
        before = deepcopy(self.host.ponds["garden"])
        self.apply(["canvas"], ceilings=site_extend.budget(memory="3G"))
        result = SiteDescription.read(self.host.paths.config).ponds["garden"]
        self.assertFalse(result.enabled)
        self.assertEqual(result.document, before)
        self.assertEqual({s["name"]: s["callers"] for s in self.host.store.service_list()},
                         {"canvas": {"pond": "writer"}, "knowledge": {"pond": "writer"}})

    def test_explicit_retry_keeps_pond_and_service_selection(self):
        self.host.fail = "restart"
        with self.assertRaises(ValueError) as failure:
            self.apply(["canvas"], bring_back=["garden"])
        self.assertIn("boring-crocodile-setup --site-extend --bring-back garden --grant-services canvas --apply",
                      str(failure.exception))
        self.apply(["canvas"], bring_back=["garden"])
        result = SiteDescription.read(self.host.paths.config)
        self.assertTrue(result.ponds["garden"].enabled)
        self.assertTrue(result.ponds["pond"].enabled)
        self.assertEqual({s["name"]: s["callers"] for s in self.host.store.service_list()},
                         {"canvas": {"garden": "reader", "pond": "writer"}, "knowledge": {"pond": "writer"}})
        self.assertIn("boring-crocodile-worker-garden.service", self.host.active)

    def test_enable_preserves_ceilings_and_repairs_after_saved_grant_and_failed_restart(self):
        before = self.host.ponds["garden"]["limits"]
        secret = self.host.path(self.host.site.ponds["garden"].credential_file).read_bytes()
        self.host.fail = "restart"
        with self.assertRaisesRegex(ValueError, "Rerun"):
            self.apply(["canvas"], bring_back=["garden"])
        self.assertTrue(SiteDescription.read(self.host.paths.config).ponds["garden"].enabled)
        self.assertEqual({s["name"]:s["callers"] for s in self.host.store.service_list()},
                         {"canvas": {"garden": "reader", "pond": "writer"}, "knowledge": {"pond": "writer"}})
        self.apply(["canvas"], bring_back=["garden"])  # Saved enabled state still allows explicit repair.
        self.assertIn("boring-crocodile-worker-garden.service", self.host.active)
        self.assertEqual(SiteDescription.read(self.host.paths.config).ponds["garden"].document["limits"], before)
        self.assertEqual(self.host.path(self.host.site.ponds["garden"].credential_file).read_bytes(), secret)

    def test_explicit_selection_ceiling_and_stale_store_are_checked(self):
        with self.assertRaisesRegex(ValueError, "Unknown Pond"):
            self.plan(bring_back=["ghost"])
        with self.assertRaisesRegex(ValueError, "Unknown service"):
            self.plan(["ghost"], bring_back=["garden"])
        proposal = self.plan(["canvas"], bring_back=["garden"])
        changed = self.host.store.service_grants("canvas", {"pond":"writer","pirate:parrot":"writer"})
        self.host.store.service_grants("canvas", changed["after"], changed["plan_digest"])
        with self.assertRaisesRegex(ValueError, "changed"):
            site_extend.apply(proposal["plan_digest"], ["canvas"], paths=self.host.paths, host=self.host,
                              bring_back=["garden"])
        self.assertFalse(SiteDescription.read(self.host.paths.config).ponds["garden"].enabled)
        ceilings = site_extend.budget(memory="3G", tasks=64)
        self.apply(ceilings=ceilings, bring_back=["garden"])
        result = SiteDescription.read(self.host.paths.config).ponds["garden"]
        self.assertEqual((result.policy.memory_bytes, result.policy.tasks_max, result.policy.task_tasks_max),
                         (3*1024**3, 64, 64))
        self.assertEqual(site_extend.budget(), {})


    def test_park_and_bring_back_preserve_pirate_client_admission(self):
        from boring_crocodile import uninstall
        self.host.ponds["garden"]["enabled"] = True
        self.host.main["pirate_services"]["callers"]["parrot"]["ponds"] = ["garden"]
        self.host.save()
        proposal, _ = site_render.plan(paths=self.host.paths, host=self.host)
        site_render.apply(proposal["plan_digest"], paths=self.host.paths, host=self.host)
        garden = SiteDescription.read(self.host.paths.config).ponds["garden"]
        self.assertIn(garden.identity.name, self.host.role_members["pirate-clients"])
        proposal, _ = uninstall.plan(pond="garden", paths=self.host.paths, host=self.host)
        uninstall.apply(pond="garden", approved_digest=proposal["plan_digest"],
                        paths=self.host.paths, host=self.host)
        parked = SiteDescription.read(self.host.paths.config).ponds["garden"]
        self.assertNotIn("garden", SiteDescription.read(self.host.paths.config).settings.pirate_callers["parrot"].ponds)
        self.assertEqual(SiteDescription.read(self.host.paths.config).settings.pirate_callers["parrot"].parked_ponds, ("garden",))
        self.assertFalse(parked.enabled)
        self.assertNotIn(garden.identity.name, self.host.role_members["pirate-clients"])
        self.apply(bring_back=["garden"])
        restored = SiteDescription.read(self.host.paths.config).ponds["garden"]
        self.assertIn("garden", SiteDescription.read(self.host.paths.config).settings.pirate_callers["parrot"].ponds)
        self.assertTrue(restored.enabled)
        self.assertEqual(restored.identity, garden.identity)
        self.assertIn(garden.identity.name, self.host.role_members["pirate-clients"])

    def test_invalid_candidate_keeps_sources_and_native_state(self):
        source = self.host.paths.config.read_bytes()
        with self.assertRaises(ValueError):
            self.apply(ceilings={"memory_bytes": True}, bring_back=["garden"])
        self.assertEqual(self.host.paths.config.read_bytes(), source)
        self.assertFalse(self.host.active)
