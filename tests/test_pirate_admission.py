"""The description owns admission; lifecycle edits never leave numeric worker grants."""
from copy import deepcopy
from contextlib import closing, redirect_stdout, redirect_stderr
import io
import json
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from boring_crocodile import oauth_records, site_extend, site_pirates, site_render, site_setup, uninstall
from boring_crocodile.site_model import SiteDescription
from site_host import FakeHost
from test_site_model import description


class AdmissionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = FakeHost(Path(self.temp.name))
        self.host.main["pirate_services"]["callers"]["parrot"] = {"uid": 1300, "ponds": ["pond"]}
        self.host.ponds["garden"]["enabled"] = True
        self.host.save()
        database = self.host.path(self.host.site.settings.oauth_database_file)
        database.parent.mkdir(parents=True, mode=0o700)
        with closing(sqlite3.connect(database)) as db, db:
            oauth_records.initialize(db)
        database.chmod(0o600)
        self.render()

    def render(self):
        proposal, _ = site_render.plan(paths=self.host.paths, host=self.host)
        site_render.apply(proposal["plan_digest"], paths=self.host.paths, host=self.host)

    def current(self):
        return SiteDescription.read(self.host.paths.config)

    def test_existing_pirate_adds_a_pond_without_repeating_its_account(self):
        before = self.current().ponds["garden"].document
        proposal, _ = site_pirates.plan_add("parrot", None, ["garden"],
                                           paths=self.host.paths, host=self.host)
        result = site_pirates.apply("add", proposal["plan_digest"], "parrot", None, ["garden"],
                                   paths=self.host.paths, host=self.host)
        self.assertEqual(result["ponds"], ["garden", "pond"])
        self.assertNotIn("for_site_json", result)
        self.assertEqual(result["service_authority"]["uid"], 1200)
        self.assertEqual(self.current().document["pirate_services"]["callers"]["parrot"]["ponds"],
                         ["garden", "pond"])
        self.assertEqual(self.current().ponds["garden"].document, before)
        self.assertEqual(self.host.role_members["pirate-clients"], {"demo-pond", "demo-garden"})

    def test_cli_adds_to_existing_pirate_with_one_apply_command(self):
        output = io.StringIO()
        with patch.object(site_pirates.os, "geteuid", return_value=0), \
             patch.object(site_pirates, "Paths", return_value=self.host.paths), \
             patch.object(site_pirates, "Host", return_value=self.host), \
             patch.object(site_pirates, "confirm_apply",
                          side_effect=lambda proposal, prompt: proposal["plan_digest"]), \
             redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = site_pirates.main("add", ["parrot", "--ponds", "garden", "--apply"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["ponds"], ["garden", "pond"])

    def test_partial_remove_clears_active_and_saved_pairs_but_keeps_the_caller(self):
        record = self.host.main["pirate_services"]["callers"]["parrot"]
        record.update(ponds=["pond"], parked_ponds=["garden"])
        self.host.ponds["garden"]["enabled"] = False
        self.host.save()
        self.render()
        proposal, _ = site_pirates.plan_remove("parrot", ponds=["garden"],
                                               paths=self.host.paths, host=self.host)
        self.assertIn("garden", site_pirates.review(proposal))
        outcome = site_pirates.apply("remove", proposal["plan_digest"], "parrot", ponds=["garden"],
                                     paths=self.host.paths, host=self.host)
        self.assertEqual(outcome["status"], "updated")
        self.assertEqual(self.current().settings.pirate_callers["parrot"].ponds, ("pond",))
        self.assertEqual(self.current().settings.pirate_callers["parrot"].parked_ponds, ())
        proposal, _ = site_pirates.plan_remove("parrot", ponds=["pond"],
                                               paths=self.host.paths, host=self.host)
        site_pirates.apply("remove", proposal["plan_digest"], "parrot", ponds=["pond"],
                           paths=self.host.paths, host=self.host)
        self.assertEqual(self.current().document["pirate_services"]["callers"]["parrot"],
                         {"uid": 1300, "ponds": []})
        self.assertNotIn("demo-pond", self.host.role_members["pirate-clients"])
        # Retrying a selection cannot turn an empty caller into whole removal.
        proposal, _ = site_pirates.plan_remove("parrot", ponds=["pond"],
                                               paths=self.host.paths, host=self.host)
        site_pirates.apply("remove", proposal["plan_digest"], "parrot", ponds=["pond"],
                           paths=self.host.paths, host=self.host)
        self.assertIn("parrot", self.current().settings.pirate_callers)

    def test_partial_remove_refuses_bad_scope_and_never_removes_the_whole_caller(self):
        before = self.host.paths.config.read_bytes()
        for ponds, retire in (([], False), (["missing"], False), (["pond"], True)):
            with self.subTest(ponds=ponds, retire=retire), self.assertRaises(ValueError):
                site_pirates.plan_remove("parrot", ponds=ponds, retire=retire,
                                        paths=self.host.paths, host=self.host)
        self.assertEqual(self.host.paths.config.read_bytes(), before)

    def test_cli_partial_remove_uses_plan_confirmation_and_render(self):
        output = io.StringIO()
        with patch.object(site_pirates.os, "geteuid", return_value=0), \
             patch.object(site_pirates, "Paths", return_value=self.host.paths), \
             patch.object(site_pirates, "Host", return_value=self.host), \
             patch.object(site_pirates, "confirm_apply",
                          side_effect=lambda proposal, prompt: proposal["plan_digest"]), \
             redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = site_pirates.main("remove", ["parrot", "--ponds", "pond", "--apply"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "updated")
        self.assertEqual(self.current().document["pirate_services"]["callers"]["parrot"],
                         {"uid": 1300, "ponds": []})

    def test_whole_site_activation_restores_only_the_initial_ponds_saved_pairs(self):
        self.host.main["pirate_services"]["callers"]["parrot"]["ponds"] = ["garden", "pond"]
        self.host.save()
        self.render()
        policy = self.current().ponds["pond"].policy
        proposal, _ = uninstall.plan(site=True, paths=self.host.paths, host=self.host)
        uninstall.apply(site=True, approved_digest=proposal["plan_digest"],
                        paths=self.host.paths, host=self.host)
        record = self.current().document["pirate_services"]["callers"]["parrot"]
        self.assertEqual((record["ponds"], record["parked_ponds"]), ([], ["garden", "pond"]))
        proposal, _ = site_setup.plan(policy, paths=self.host.paths, host=self.host)
        with patch.object(self.host, "apply_groups", side_effect=OSError("membership interruption")), \
             self.assertRaisesRegex(ValueError, "Render did not finish"):
            site_setup.apply(policy, proposal["plan_digest"], paths=self.host.paths, host=self.host)
        record = self.current().document["pirate_services"]["callers"]["parrot"]
        self.assertEqual((record["ponds"], record["parked_ponds"]), (["pond"], ["garden"]))
        # The published candidate is enough for ordinary render repair.
        self.render()
        self.assertEqual(self.host.role_members["pirate-clients"], {"demo-pond"})
        proposal, _ = site_extend.plan(bring_back=["garden"], paths=self.host.paths, host=self.host)
        site_extend.apply(proposal["plan_digest"], bring_back=["garden"],
                          paths=self.host.paths, host=self.host)
        record = self.current().document["pirate_services"]["callers"]["parrot"]
        self.assertEqual(record["ponds"], ["garden", "pond"])
        self.assertNotIn("parked_ponds", record)

    def test_activation_does_not_invent_an_initial_pond_pair(self):
        self.host.main["pirate_services"]["callers"]["parrot"]["ponds"] = ["garden"]
        self.host.save()
        self.render()
        policy = self.current().ponds["pond"].policy
        proposal, _ = uninstall.plan(site=True, paths=self.host.paths, host=self.host)
        uninstall.apply(site=True, approved_digest=proposal["plan_digest"],
                        paths=self.host.paths, host=self.host)
        proposal, _ = site_setup.plan(policy, paths=self.host.paths, host=self.host)
        site_setup.apply(policy, proposal["plan_digest"], paths=self.host.paths, host=self.host)
        record = self.current().document["pirate_services"]["callers"]["parrot"]
        self.assertEqual((record["ponds"], record["parked_ponds"]), ([], ["garden"]))
        self.assertEqual(self.host.role_members["pirate-clients"], set())

    def test_remove_revokes_membership_and_parked_admission(self):
        self.host.main["pirate_services"]["callers"]["parrot"]["parked_ponds"] = ["garden"]
        self.host.ponds["garden"]["enabled"] = False
        self.host.save()
        proposal, _ = site_pirates.plan_remove("parrot", paths=self.host.paths, host=self.host)
        site_pirates.apply("remove", proposal["plan_digest"], "parrot",
                           paths=self.host.paths, host=self.host)
        self.assertNotIn("parrot", self.current().document["pirate_services"]["callers"])
        self.assertNotIn("demo-pond", self.host.role_members["pirate-clients"])
        proposal, _ = site_extend.plan(bring_back=["garden"], paths=self.host.paths, host=self.host)
        site_extend.apply(proposal["plan_digest"], bring_back=["garden"],
                          paths=self.host.paths, host=self.host)
        self.assertNotIn("demo-garden", self.host.role_members["pirate-clients"])

    def test_park_restores_recorded_pairs_and_retire_drops_them_before_uid_reuse(self):
        record = self.host.main["pirate_services"]["callers"]["parrot"]
        record["ponds"] = ["garden", "pond"]
        self.host.save()
        self.render()
        original = self.current().ponds["garden"].identity
        for retiring in (False, True):
            proposal, _ = uninstall.plan(pond="garden", delete_data=retiring,
                                        paths=self.host.paths, host=self.host)
            uninstall.apply(pond="garden", delete_data=retiring,
                            approved_digest=proposal["plan_digest"],
                            paths=self.host.paths, host=self.host)
            record = self.current().document["pirate_services"]["callers"]["parrot"]
            self.assertNotIn("garden", record["ponds"])
            self.assertEqual(record.get("parked_ponds", []), [] if retiring else ["garden"])
            self.assertNotIn("demo-garden", self.host.role_members["pirate-clients"])
            if not retiring:
                proposal, _ = site_extend.plan(bring_back=["garden"], paths=self.host.paths, host=self.host)
                site_extend.apply(proposal["plan_digest"], bring_back=["garden"],
                                  paths=self.host.paths, host=self.host)
                self.assertIn("garden", self.current().document["pirate_services"]["callers"]["parrot"]["ponds"])
        # Simulate the administrator releasing the reserved identity and reusing
        # its number for a newly declared account; no old admission may follow it.
        self.host.delete_user(original.name)
        self.host.main = deepcopy(self.current().document)
        self.host.ponds = {name: deepcopy(p.document) for name, p in self.current().ponds.items()}
        new = deepcopy(self.host.ponds["pond"])
        new["identity"] = dict(name="replacement", uid=original.uid, group="replacement",
                               gid=original.gid, home="/home/replacement", shell=original.shell)
        new["credential_file"] = "/etc/boring-crocodile/credentials/replacement-secret"
        new["pond"]["profile_id"] = "33333333-3333-4333-8333-333333333333"
        self.host.ponds["replacement"] = new
        identity = replace(original, name="replacement", group="replacement", home="/home/replacement")
        self.host.identities[identity.name] = identity
        self.host.extra_groups[identity.group] = {"name": identity.group, "gid": identity.gid}
        self.host.path(identity.home).mkdir()
        self.host.owned[str(self.host.path(identity.home))] = identity.uid
        secret = self.host.path(new["credential_file"])
        secret.write_text("fixture-only-replacement-approval")
        secret.chmod(0o600)
        self.host.save()
        self.render()
        self.assertNotIn("replacement", self.host.role_members["pirate-clients"])
        self.assertNotIn("replacement", self.current().settings.pirate_callers["parrot"].ponds)


class AdmissionModelTest(unittest.TestCase):
    def test_old_uid_only_roster_is_not_an_admission_list(self):
        main, ponds = description()
        main["pirate_services"]["callers"] = {"parrot": 1300}
        with self.assertRaisesRegex(ValueError, "pirate_services.callers.parrot"):
            SiteDescription.parse(main, ponds)

    def test_caller_records_validate_pond_names_and_disjoint_saved_pairs(self):
        for record in ({"uid": 1300, "ponds": ["missing"]},
                       {"uid": 1300, "ponds": ["pond"], "parked_ponds": ["pond"]},
                       {"uid": True, "ponds": ["pond"]},
                       {"uid": 1300, "ponds": ["pond", "pond"]}):
            main, ponds = description()
            main["pirate_services"]["callers"] = {"parrot": record}
            with self.subTest(record=record), self.assertRaises(ValueError):
                SiteDescription.parse(main, ponds)
