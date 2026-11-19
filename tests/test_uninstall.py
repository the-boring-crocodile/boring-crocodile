import json
import os
import sqlite3
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from boring_crocodile import uninstall, setup_apply, site_setup, site_extend, oauth_records
from boring_crocodile.uninstall import UninstallConflict, _oauth_keys, _secure_home, _delete_path
from boring_crocodile.site_model import SiteDescription
import test_setup_apply as fixtures


class RetirementTest(fixtures.ProvisionCase):
    def setUp(self):
        super().setUp()
        self.provision()
        inputs = replace(self.inputs, execution_user="demo-garden", home="/home/demo-garden")
        p = setup_apply.plan_additional(inputs, "garden", host=self.host)
        setup_apply.apply_additional(inputs, "garden", p["plan_digest"], host=self.host)
        description = SiteDescription.read(self.host.paths.config)
        policy = replace(description.ponds["pond"].policy, public_base_url="https://example.test")
        p, _ = site_setup.plan(policy, paths=self.host.paths, host=self.host)
        site_setup.apply(policy, p["plan_digest"], paths=self.host.paths, host=self.host)
        p, _ = site_extend.plan(paths=self.host.paths, host=self.host, bring_back=["garden"])
        site_extend.apply(p["plan_digest"], paths=self.host.paths, host=self.host, bring_back=["garden"])
        self.description = SiteDescription.read(self.host.paths.config)
        self.database = self.host.path(self.description.settings.oauth_database_file)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database) as db:
            oauth_records.initialize(db)
            oauth_records.add_service(db, "shared", "https://example.test/mcp", {'pond': 'writer', 'garden': 'writer'}, roles=["reader", "writer"], default_role="reader")
            for name,key in (("pond","neighbor"), ("garden","chosen")):
                db.execute("INSERT INTO records VALUES(?,?,?,NULL,NULL)", (
                    "access", key, json.dumps(dict(subject=self.description.ponds[name].pond.profile_id,
                                                  client_id="client", scopes=["mcp"]))))
        self.database.chmod(0o600)

    def apply(self, **selection):
        p, _ = uninstall.plan(paths=self.host.paths, host=self.host, **selection)
        return uninstall.apply(approved_digest=p["plan_digest"], paths=self.host.paths, host=self.host, **selection)

    def test_retirement_reserves_the_pond_account_and_numeric_group(self):
        chosen = self.description.ponds["garden"].identity
        self.apply(pond="garden", delete_data=True)
        self.assertEqual(self.host.account(chosen.name), chosen.as_user())
        self.assertEqual(self.host.group(chosen.group), {"name": chosen.group, "gid": chosen.gid})
        self.assertIn(chosen.uid, {uid for uid, _ in self.host.identity_ids()[0]})
        self.assertIn(chosen.gid, {gid for gid, _ in self.host.identity_ids()[1]})
        self.assertIn(["/usr/sbin/usermod", "--lock", "--expiredate", "1", chosen.name], self.host.calls)
        self.assertFalse(any(command[0].endswith(("userdel", "groupdel")) for command in self.host.calls))
        self.assertNotIn("garden", SiteDescription.read(self.host.paths.config).ponds)

    def test_whole_site_retirement_reserves_all_its_identities(self):
        identities = [self.description.controller, *(p.identity for p in self.description.ponds.values())]
        self.apply(site=True, delete_data=True)
        for identity in identities:
            with self.subTest(name=identity.name):
                self.assertEqual(self.host.account(identity.name), identity.as_user())
                self.assertEqual(self.host.group(identity.group), {"name": identity.group, "gid": identity.gid})
                self.assertIn(["/usr/sbin/usermod", "--lock", "--expiredate", "1", identity.name], self.host.calls)
        self.assertFalse(any(command[0].endswith(("userdel", "groupdel")) for command in self.host.calls))
        self.assertFalse(self.database.exists())

    def test_park_and_retire_retry_keep_neighbors_and_sealed_data(self):
        chosen = self.description.ponds["garden"]
        home = self.host.path(chosen.identity.home)
        marker = home / "work/keep"
        marker.write_text("irreplaceable")
        credential = self.host.path(chosen.credential_file)
        self.apply(pond="garden")
        self.assertTrue(credential.exists())
        self.assertTrue(home.is_dir())
        self.assertFalse(SiteDescription.read(self.host.paths.config).ponds["garden"].enabled)
        self.host.fail = "reserve_user"
        with self.assertRaisesRegex(ValueError, "Rerun"):
            self.apply(pond="garden", delete_data=True)
        self.assertTrue((self.host.paths.state / "ponds/garden/config.toml").exists())
        self.assertEqual(self.host.file_info(home).st_uid, self.host.owner)
        declaration = self.host.paths.state / "ponds/garden/config.toml"
        self.assertEqual(declaration.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(declaration.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.host.file_info(declaration).st_gid, 0)
        self.apply(pond="garden", delete_data=True)
        self.assertNotIn("garden", SiteDescription.read(self.host.paths.config).ponds)
        self.assertIn(chosen.identity.name, self.host.identities)
        self.assertFalse(credential.exists())
        self.assertEqual(marker.read_text(), "irreplaceable")
        self.assertIn("boring-crocodile-worker-pond.service", self.host.active)
        with sqlite3.connect(self.database) as db:
            self.assertEqual({r[1] for r in oauth_records.rows(db)}, {"neighbor"})
            self.assertEqual(oauth_records.service(db, name="shared")["callers"], {"pond": "writer"})
        self.apply(pond="garden", delete_data=True)

    def test_absent_or_retired_pond_is_quiet_but_explicit_render_repairs(self):
        self.apply(pond="garden", delete_data=True)
        before = {str(p): p.read_bytes() for p in self.host.root.rglob("*") if p.is_file()}
        active = set(self.host.active)
        for name in ("missing", "garden"):
            for delete_data in (False, True):
                with self.subTest(name=name, delete_data=delete_data):
                    self.host.calls.clear()
                    with patch.object(uninstall.site_render, "plan") as render:
                        result = self.apply(pond=name, delete_data=delete_data)
                    render.assert_not_called()
                    self.assertEqual(result["status"], "retired" if delete_data else "parked")
                    self.assertEqual(self.host.calls, [])
                    self.assertEqual(self.host.active, active)
                    self.assertEqual(before, {str(p): p.read_bytes() for p in self.host.root.rglob("*") if p.is_file()})
        rendered, _ = uninstall.site_render.plan(paths=self.host.paths, host=self.host)
        uninstall.site_render.apply(rendered["plan_digest"], paths=self.host.paths, host=self.host)
        self.assertIn(["/usr/bin/systemctl", "restart", "boring-crocodile-worker-pond.service"], self.host.calls)

    def test_missing_account_or_group_refuses_without_releasing_bindings(self):
        chosen = self.description.ponds["garden"].identity
        for missing in ("account", "group"):
            with self.subTest(missing=missing), patch.dict(self.host.identities, clear=False), \
                    patch.dict(self.host.extra_groups, clear=False):
                if missing == "account":
                    self.host.identities.pop(chosen.name)
                else:
                    self.host.extra_groups.pop(chosen.group)
                    # FakeHost.group also resolves the account's primary group.
                    original_group = self.host.group
                    self.host.group = lambda name: None if name == chosen.group else original_group(name)
                try:
                    with self.assertRaisesRegex(UninstallConflict, "Worker (identity changed|group is missing)"):
                        self.apply(pond="garden", delete_data=True)
                finally:
                    if missing == "group":
                        self.host.group = original_group
                self.assertTrue((self.host.paths.state / "ponds/garden/config.toml").exists())

    def test_changed_private_gid_is_never_deleted(self):
        chosen = self.description.ponds["garden"].identity
        reserve_user, original_group = self.host.reserve_user, self.host.group
        def replace_group(name):
            reserve_user(name)
            self.host.extra_groups[name] = {"name": name, "gid": chosen.gid + 1}
            self.host.group = lambda group: (self.host.extra_groups[group] if group == chosen.group
                                             else original_group(group))
        self.host.calls.clear()
        with patch.object(self.host, "reserve_user", side_effect=replace_group):
            with self.assertRaisesRegex(UninstallConflict, "Worker group changed"):
                self.apply(pond="garden", delete_data=True)
        self.assertNotIn(["/usr/sbin/groupdel", chosen.group], self.host.calls)
        self.assertTrue((self.host.paths.state / "ponds/garden/config.toml").exists())

    def test_controller_nss_membership_is_foreign_at_retirement_plan(self):
        chosen = self.description.ponds["garden"].identity
        self.host.role_members[chosen.group] = {self.description.controller.name}
        self.host.calls.clear()
        with self.assertRaisesRegex(UninstallConflict, "Worker group contains another identity"):
            self.apply(pond="garden", delete_data=True)
        self.assertEqual(self.host.calls, [])
        self.assertIn(chosen.name, self.host.identities)

    def test_foreign_membership_appearing_before_reservation_refuses(self):
        chosen = self.description.ponds["garden"].identity
        seal = self.host.seal_home
        def seal_then_change(path, uid):
            seal(path, uid)
            self.host.role_members[chosen.group] = {self.description.controller.name}
        with patch.object(self.host, "seal_home", side_effect=seal_then_change), \
             self.assertRaisesRegex(UninstallConflict, "Worker group contains another identity"):
            self.apply(pond="garden", delete_data=True)
        self.assertIn(chosen.name, self.host.identities)
        self.assertNotIn(["/usr/sbin/groupdel", chosen.group], self.host.calls)

    def test_foreign_membership_appearing_after_reservation_refuses(self):
        chosen = self.description.ponds["garden"].identity
        reserve = self.host.reserve_user
        def reserve_then_change(name):
            reserve(name)
            self.host.role_members[chosen.group] = {self.description.controller.name}
        with patch.object(self.host, "reserve_user", side_effect=reserve_then_change), \
             self.assertRaisesRegex(UninstallConflict, "Worker group contains another identity"):
            self.apply(pond="garden", delete_data=True)
        self.assertNotIn(["/usr/sbin/groupdel", chosen.group], self.host.calls)
        self.assertTrue((self.host.paths.state / "ponds/garden/config.toml").exists())

    def test_reused_numeric_group_does_not_reopen_sealed_declaration(self):
        chosen = self.description.ponds["garden"].identity
        self.apply(pond="garden")
        declaration = self.host.paths.state / "ponds/garden/config.toml"
        self.host.apply_description_permissions([
            dict(path=str(declaration.parent), directory=True, uid=self.host.owner, gid=0, mode=0o700),
            dict(path=str(declaration), directory=False, uid=self.host.owner, gid=0, mode=0o600)])
        self.host.delete_user(chosen.name)
        self.host.extra_groups.pop(chosen.group, None)
        self.host.extra_groups["reused"] = {"name":"reused", "gid":chosen.gid}
        with self.assertRaisesRegex(UninstallConflict, "numeric GID was reused"):
            self.apply(pond="garden", delete_data=True)
        self.assertEqual(declaration.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.host.file_info(declaration).st_gid, 0)
        proposal, _ = uninstall.site_render.plan(paths=self.host.paths, host=self.host)
        uninstall.site_render.apply(proposal["plan_digest"], paths=self.host.paths, host=self.host)
        self.assertEqual(declaration.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.host.file_info(declaration).st_gid, 0)

    def test_whole_site_removal_keeps_metadata_and_never_deletes_homes(self):
        self.apply(site=True)
        self.assertEqual(SiteDescription.read(self.host.paths.config).state, "removed")
        self.assertTrue(self.database.exists())
        self.apply(site=True, delete_data=True)
        self.apply(site=True, delete_data=True)
        self.assertFalse(self.database.exists())
        self.assertTrue(self.host.paths.config.exists())
        for p in self.description.ponds.values():
            self.assertTrue(self.host.path(p.identity.home).is_dir())
            self.assertEqual(self.host.file_info(self.host.path(p.identity.home)).st_uid, self.host.owner)
            self.assertIn(p.identity.name, self.host.identities)

    def test_stale_and_mount_conflicts_refuse_before_changes(self):
        with self.assertRaises(ValueError):
            uninstall.plan(pond="pond", paths=self.host.paths, host=self.host)
        p, _ = uninstall.plan(pond="garden", delete_data=True, paths=self.host.paths, host=self.host)
        source = self.host.paths.config.read_bytes()
        with self.assertRaisesRegex(ValueError, "changed"):
            uninstall.apply(approved_digest="stale", pond="garden", delete_data=True,
                            paths=self.host.paths, host=self.host)
        home = self.host.path(self.description.ponds["garden"].identity.home)
        with patch("boring_crocodile.uninstall._mount_points", return_value=[str(home / "mounted")]):
            with self.assertRaises(ValueError):
                uninstall.plan(pond="garden", delete_data=True, paths=self.host.paths, host=self.host)
        self.assertEqual(self.host.paths.config.read_bytes(), source)
        dropin = self.host.paths.units / "boring-crocodile-mcp.service.d/external.conf"
        dropin.parent.mkdir()
        dropin.write_text("\n".join(["[Service]", "SupplementaryGroups=demo-garden", ""]))
        with self.assertRaisesRegex(ValueError, "external drop-in"):
            uninstall.plan(pond="garden", delete_data=True, paths=self.host.paths, host=self.host)
        self.assertTrue(dropin.exists())


class OAuthCleanupTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "var/lib/boring-crocodile").mkdir(parents=True)

    def tearDown(self):
        self.temporary.cleanup()

    def test_oauth_selection_uses_recorded_profile_only(self):
        database = self.root / "var/lib/boring-crocodile/oauth.sqlite3"
        with sqlite3.connect(database) as db:
            from boring_crocodile.oauth_records import initialize
            initialize(db)
            db.executemany("INSERT INTO records VALUES(?,?,?,NULL,NULL)", (
                ("access", "selected", json.dumps({"subject": "profile-a", "client_id": "client", "scopes": ["mcp"]})),
                ("refresh", "neighbor", json.dumps({"subject": "profile-b", "client_id": "client", "scopes": ["mcp"]})),
            ))
        database.chmod(0o600)
        self.assertEqual(
            _oauth_keys(database, "profile-a", self.root.lstat().st_uid),
            [("access", "selected")])

    def test_data_cleanup_removes_caller_grants_and_tokens_only(self):
        from boring_crocodile import oauth_records as records
        from boring_crocodile.uninstall import _revoke
        database = self.root / "var/lib/boring-crocodile/oauth.sqlite3"
        with sqlite3.connect(database) as db:
            records.initialize(db)
            records.add_service(db, "shared", "https://example.test/mcp",
                                {'happy-hippo': 'writer', 'pond': 'writer', 'pirate/parrot': 'writer'}, roles=["reader", "writer"], default_role="reader")
            records.add_service(db, "exclusive", "https://other.test/mcp", {'happy-hippo': 'writer'}, roles=["reader", "writer"], default_role="reader")
            for kind, key, subject in (("access", "chosen", "profile-a"),
                                       ("refresh", "neighbor", "profile-b"),
                                       ("service", "chosen-service", "happy-hippo"),
                                       ("service", "neighbor-service", "pond")):
                db.execute("INSERT INTO records VALUES(?,?,?,NULL,NULL)",
                           (kind, key, json.dumps(dict(subject=subject, client_id="client", scopes=["mcp"]))))
        database.chmod(0o600)
        _revoke(database, "profile-a", self.root.lstat().st_uid, caller="happy-hippo")
        with sqlite3.connect(database) as db:
            self.assertEqual({row[1] for row in records.rows(db)}, {"neighbor", "neighbor-service"})
            self.assertEqual(records.service(db, name="shared")["callers"], {"pond": "writer", "pirate/parrot": "writer"})
            self.assertEqual(records.service(db, name="exclusive")["callers"], {})

    def test_malformed_service_grants_refuse_cleanup_and_roll_back(self):
        from boring_crocodile import oauth_records as records
        from boring_crocodile.uninstall import _revoke
        database = self.root / "var/lib/boring-crocodile/oauth.sqlite3"
        with sqlite3.connect(database) as db:
            records.initialize(db)
            records.add_service(db, "bad", "https://example.test/mcp", {'happy-hippo': 'writer'}, roles=["reader", "writer"], default_role="reader")
            db.execute("UPDATE services SET callers=?", ('"not-a-map"',))
            db.execute("INSERT INTO records VALUES(?,?,?,NULL,NULL)",
                       ("access", "keep-on-failure", json.dumps(dict(
                           subject="profile-a", client_id="client", scopes=["mcp"]))))
        database.chmod(0o600)
        owner = self.root.lstat().st_uid
        for operation in (_oauth_keys, _revoke):
            with self.assertRaisesRegex(UninstallConflict, "malformed service caller"):
                operation(database, "profile-a", owner, caller="happy-hippo")
        with sqlite3.connect(database) as db:
            self.assertEqual([row[1] for row in records.rows(db)], ["keep-on-failure"])


class RemovalBoundaryTest(unittest.TestCase):
    def test_a_mounted_subtree_is_refused(self):
        with tempfile.TemporaryDirectory() as workspace:
            path = Path(workspace) / "test-worker"
            path.mkdir()
            action = {"kind": "remove", "path": str(path)}
            with patch("boring_crocodile.uninstall._mount_points", return_value=[str(path / "mounted")]):
                with self.assertRaisesRegex(UninstallConflict, "mounted subtree"):
                    _delete_path(path)
            self.assertTrue(path.exists())


class SecureHomeTest(unittest.TestCase):
    """On disk; the mode really changes, the owner change is recorded (the test cannot chown to root)."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "boring-crocodile-second"
        (self.home / "work").mkdir(parents=True)
        (self.home / "work/notes.md").write_text("kept\n")
        self.home.chmod(0o755)

    def tearDown(self):
        self.temporary.cleanup()

    def seal_to_me(self):
        # The test cannot give a folder to root: fchown records what it was asked.
        self.owned = {}

        def fchown(fd, uid, gid):
            self.owned[os.readlink(f"/proc/self/fd/{fd}")] = (uid, gid)
        return patch("boring_deploy.install.os.fchown", fchown)

    def test_the_top_folder_changes_owner_and_mode_only(self):
        inside = (self.home / "work").stat().st_mode
        with self.seal_to_me():
            _secure_home(self.home, os.getuid())
        self.assertEqual(self.home.lstat().st_mode & 0o7777, 0o700)
        self.assertEqual(self.owned, {str(self.home): (0, 0)})
        self.assertEqual((self.home / "work/notes.md").read_text(), "kept\n")
        self.assertEqual((self.home / "work").stat().st_mode, inside)

    def test_a_symlink_a_mount_or_another_owner_is_refused_untouched(self):
        link = self.home.with_name("link")
        link.symlink_to(self.home)
        with self.seal_to_me():
            with self.assertRaisesRegex(UninstallConflict, "is a symlink"):
                _secure_home(link, os.getuid())
            with patch("boring_deploy.install._mount_points", return_value={str(self.home)}):
                with self.assertRaisesRegex(UninstallConflict, "is a mount point"):
                    _secure_home(self.home, os.getuid())
            with self.assertRaisesRegex(UninstallConflict, "belongs to uid"):
                _secure_home(self.home, os.getuid() + 1)
        self.assertEqual(self.home.lstat().st_mode & 0o777, 0o755)

    def test_a_home_gone_since_the_plan_is_refused_not_skipped(self):
        gone = self.home.with_name("gone")
        with self.seal_to_me():
            with self.assertRaisesRegex(UninstallConflict, "changed since the plan"):
                _secure_home(gone, os.getuid())
        self.assertEqual(self.owned, {})


if __name__ == "__main__":
    unittest.main()
