import json
import os
import sqlite3
import subprocess
from contextlib import closing
from functools import partial
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import patch

from boring_crocodile import uninstall
from boring_crocodile.uninstall import (
    UninstallConflict, _delete_user, _oauth_keys, _preflight, _secure_home, plan,
)


import shutil

from boring_crocodile.settings import Settings
from boring_crocodile.site_check import Reality, site_check

SITE = Path(__file__).resolve().parent.parent / "site/default"
UIDS = {"boring-crocodile": 901, "boring-crocodile-mcp": 902, "boring-crocodile-second": 903}
HOMES = {"/home/boring-crocodile": "boring-crocodile", "/home/boring-crocodile-second": "boring-crocodile-second"}


class FakeReality(Reality):
    def owner(self, path):
        return HOMES[str(path)]

    def user(self, name):
        home = "/nonexistent" if name == "boring-crocodile-mcp" else "/home/" + name
        return {"name": name, "uid": UIDS[name], "gid": UIDS[name], "home": home, "shell": "/usr/sbin/nologin"}

    def group(self, name):
        return {"name": name, "gid": UIDS[name]}

    def groups(self, name):
        return [902, 901, 903]

    def uid_name(self, uid):
        return {value: key for key, value in UIDS.items()}[uid]

    def is_root(self):
        return False


class UninstallPlanTest(unittest.TestCase):
    """The two-Pond reference site, read as it is: no setup receipts anywhere."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        shutil.copytree(SITE / "two-ponds/etc", self.root / "etc")
        self.units = self.root / "etc/systemd/system"
        self.config = self.root / "etc/boring-crocodile/config.toml"

    def tearDown(self):
        self.temporary.cleanup()

    def plan(self, **kwargs):
        return plan(root=self.root, reality=FakeReality(), **kwargs)

    def removed(self, proposal):
        return {a["path"] for a in proposal["actions"] if a["kind"] == "remove"}

    def test_removing_a_pond_leaves_a_regular_one_pond_site(self):
        dropins = self.units / "boring-crocodile-task-second.socket.d"
        dropins.mkdir()
        (dropins / "50-max-connections.conf").write_text("[Socket]\nMaxConnections=8\n")
        proposal, replacements = self.plan(pond="second")
        self.assertEqual(proposal["scope"], "pond")
        self.assertEqual(replacements[str(self.units / "boring-crocodile-mcp.service")],
                         (SITE / "etc/systemd/system/boring-crocodile-mcp.service").read_text())
        self.assertNotIn("second", replacements[str(self.config)])
        self.assertIn(str(dropins), self.removed(proposal))  # a later "second" must not inherit it
        self.assertEqual(proposal["actions"][-1]["kind"], "start_site")
        self.assertFalse([a for a in proposal["actions"] if a["kind"].startswith("delete")])
        # Carry the plan out on the copy: what remains is a regular site.
        for path, content in replacements.items():
            Path(path).write_text(content)
        for path in map(Path, self.removed(proposal)):
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        report = site_check(self.config, units=self.units, tmpfiles=self.root / "etc/tmpfiles.d",
                            credentials=self.root / "credentials", reality=FakeReality())
        self.assertEqual(report["status"], "regular", report["irregular"])
        self.assertEqual(set(Settings.load(self.config).ponds), {"pond"})

    def test_homes_are_never_removed_only_reported(self):
        for options in ({"pond": "second"}, {"pond": "second", "delete_data": True},
                        {"site": True, "delete_data": True}):
            proposal, _ = self.plan(**options)
            removed = self.removed(proposal)
            self.assertFalse([p for p in removed if p.startswith(("/home", str(self.root / "home")))], options)
            self.assertIn("/home/boring-crocodile-second", [h["path"] for h in proposal["homes_kept"]])
            self.assertIn("delete them by hand", proposal["note"])

    def test_delete_data_removes_the_accounts_found_on_the_host(self):
        proposal, _ = self.plan(pond="second", delete_data=True)
        kinds = [a["kind"] for a in proposal["actions"]]
        self.assertLess(kinds.index("remove_membership"), kinds.index("delete_group"))
        revoke = next(a for a in proposal["actions"] if a["kind"] == "revoke_profile")
        self.assertEqual((revoke["caller"], revoke["owner"], revoke["profile_id"]),
                         ("second", 902, "00000000-0000-4000-8000-000000000002"))
        users = [a for a in proposal["actions"] if a["kind"] == "delete_user"]
        self.assertEqual([(u["name"], u["expected"]["uid"]) for u in users], [("boring-crocodile-second", 903)])
        self.assertTrue(proposal["homes_kept"][0]["owner_removed"])
        (self.root / "srv/boring-crocodile").mkdir(parents=True)  # nothing of the site's: not touched
        site, _ = self.plan(site=True, delete_data=True)
        self.assertEqual(sorted(a["name"] for a in site["actions"] if a["kind"] == "delete_user"),
                         ["boring-crocodile", "boring-crocodile-mcp", "boring-crocodile-second"])
        # The controller leaves each worker's group before the worker goes, so
        # userdel takes the group with it instead of warning about members.
        kinds = [(a["kind"], a.get("name", a.get("group"))) for a in site["actions"]]
        for worker in ("boring-crocodile", "boring-crocodile-second"):
            self.assertLess(kinds.index(("remove_membership", worker)), kinds.index(("delete_user", worker)))
        self.assertIn(str(self.root / "etc/boring-crocodile"), self.removed(site))
        self.assertFalse([p for p in self.removed(site) if "/srv/" in p])

    def test_delete_data_gives_each_kept_home_to_root_before_its_account_goes(self):
        home = self.root / "home/boring-crocodile-second"
        home.mkdir(parents=True)
        for options in ({"pond": "second", "delete_data": True}, {"site": True, "delete_data": True}):
            proposal, _ = self.plan(**options)
            kinds = [(a["kind"], a.get("path", a.get("name"))) for a in proposal["actions"]]
            secure = kinds.index(("secure_home", str(home)))
            self.assertLess(secure, kinds.index(("delete_user", "boring-crocodile-second")), options)
            action = proposal["actions"][secure]
            self.assertEqual(action["expected_uid"], UIDS["boring-crocodile-second"])
            kept = next(h for h in proposal["homes_kept"] if h["pond"] == "second")
            self.assertEqual((kept["owner"], kept["owner_removed"]), ("root", True))
            self.assertIn("belongs to root", proposal["note"])
        kept, _ = self.plan(pond="second")
        self.assertFalse([a for a in kept["actions"] if a["kind"] == "secure_home"])
        self.assertEqual(kept["homes_kept"][0]["owner"], "boring-crocodile-second")

    def test_apply_removes_a_pond_and_its_data_on_disk(self):
        from boring_crocodile import oauth_records as records
        dropins = self.units / "boring-crocodile-task-second.socket.d"
        dropins.mkdir()
        (dropins / "50-max-connections.conf").write_text("[Socket]\nMaxConnections=8\n")
        credential = self.root / "etc/boring-crocodile/credentials/second-approval-secret"
        credential.parent.mkdir()
        credential.write_text("secret\n")
        database = self.root / "var/lib/boring-crocodile/oauth.sqlite3"
        database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(database)) as db, db:
            records.initialize(db)
            records.add_service(db, "shared", "https://example.test/mcp", ["pond", "second"])
            for kind, key, subject in (("access", "second-token", "00000000-0000-4000-8000-000000000002"),
                                       ("access", "pond-token", "00000000-0000-4000-8000-000000000001")):
                db.execute("INSERT INTO records VALUES(?,?,?,NULL,NULL)",
                           (kind, key, json.dumps(dict(subject=subject, client_id="client", scopes=["mcp"]))))
        database.chmod(0o600)
        mode = self.config.stat().st_mode
        reality = FakeReality()
        safe_file = uninstall._safe_file

        def owned_by_test_user(path, owner=0):
            self.assertEqual(owner, UIDS["boring-crocodile-mcp"])
            safe_file(path, os.getuid())

        calls = []
        def runner(args, check):
            calls.append(args)
        approved, _ = self.plan(pond="second", delete_data=True)
        members = SimpleNamespace(gr_mem=["boring-crocodile-mcp"])
        with patch.object(uninstall, "plan", partial(plan, reality=reality)), \
                patch.object(uninstall, "_actual_user", reality.user), \
                patch.object(uninstall, "_actual_group", reality.group), \
                patch.object(uninstall, "_safe_file", owned_by_test_user), \
                patch.object(uninstall.grp, "getgrnam", return_value=members):
            result = uninstall._apply_locked(approved_digest=approved["plan_digest"], pond="second",
                                             site=False, delete_data=True, root=self.root, runner=runner)
        self.assertEqual(result["status"], "complete")
        self.assertNotIn("second", self.config.read_text())
        self.assertEqual(self.config.stat().st_mode, mode)
        self.assertEqual(set(Settings.load(self.config).ponds), {"pond"})
        self.assertEqual((self.units / "boring-crocodile-mcp.service").read_text(),
                         (SITE / "etc/systemd/system/boring-crocodile-mcp.service").read_text())
        self.assertFalse(dropins.exists())
        self.assertFalse((self.units / "boring-crocodile-worker-second.service").exists())
        self.assertFalse((self.root / "etc/tmpfiles.d/boring-crocodile-second.conf").exists())
        self.assertFalse(credential.exists())
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual({row[1] for row in records.rows(db)}, {"pond-token"})
            self.assertEqual(records.service(db, name="shared")["ponds"], ["pond"])
        tools = [args[0] for args in calls]
        self.assertIn(["/usr/bin/gpasswd", "--delete", "boring-crocodile-mcp", "boring-crocodile-second"], calls)
        self.assertIn(["/usr/sbin/userdel", "boring-crocodile-second"], calls)
        self.assertIn(["/usr/sbin/groupdel", "boring-crocodile-second"], calls)
        self.assertLess(tools.index("/usr/bin/gpasswd"), tools.index("/usr/sbin/groupdel"))
        self.assertLess(tools.index("/usr/bin/gpasswd"), tools.index("/usr/sbin/userdel"))
        # The controller and the workers are named too: systemctl returns once they are down.
        self.assertEqual(calls[0], ["/usr/bin/systemctl", "stop", "boring-crocodile-mcp.target",
                                    "boring-crocodile-mcp*.service", "boring-crocodile-worker-*.service",
                                    "boring-crocodile-task-*"])
        report = site_check(self.config, units=self.units, tmpfiles=self.root / "etc/tmpfiles.d",
                            credentials=self.root / "credentials", reality=FakeReality())
        self.assertEqual(report["status"], "regular", report["irregular"])

    def test_a_rerun_of_site_delete_data_finishes_from_its_record(self):
        """The apply stops at the controller's userdel, after config.toml went
        (as seen on a test VM); a re-run reads the site from uninstall.json
        and finishes, the state folder with the record last."""
        reality, calls, failing = FakeReality(), [], [True]
        state = self.root / "etc/boring-crocodile"

        def runner(args, check):
            calls.append(args)
            if args == ["/usr/sbin/userdel", "boring-crocodile-mcp"] and failing:
                failing.pop()
                raise subprocess.CalledProcessError(1, args)

        def apply(digest):
            with patch.object(uninstall, "plan", partial(plan, reality=reality)), \
                    patch.object(uninstall, "_actual_user", reality.user), \
                    patch.object(uninstall, "_actual_group", reality.group), \
                    patch.object(uninstall.grp, "getgrnam",
                                 return_value=SimpleNamespace(gr_mem=["boring-crocodile-mcp"])):
                return uninstall._apply_locked(approved_digest=digest, pond=None, site=True,
                                               delete_data=True, root=self.root, runner=runner)

        first, _ = self.plan(site=True, delete_data=True)
        steps = [(a["kind"], a.get("path", a.get("name"))) for a in first["actions"]]
        self.assertEqual([kind for kind, _ in steps[:3]], ["stop_site", "disable_site", "record_removal"])
        self.assertEqual(steps[-1], ("remove", str(state)))
        self.assertLess(steps.index(("delete_user", "boring-crocodile-mcp")), len(steps) - 1)
        with self.assertRaises(subprocess.CalledProcessError):
            apply(first["plan_digest"])
        self.assertFalse(self.config.exists())
        self.assertTrue((state / "uninstall.json").exists())
        again, _ = self.plan(site=True, delete_data=True)
        kinds = [a["kind"] for a in again["actions"]]
        self.assertNotIn("record_removal", kinds)
        self.assertEqual(again["actions"][-1], {"kind": "remove", "path": str(state)})
        self.assertIn({"kind": "delete_user", "name": "boring-crocodile-mcp",
                       "expected": reality.user("boring-crocodile-mcp")}, again["actions"])
        self.assertEqual(sorted(h["pond"] for h in again["homes_kept"]), ["pond", "second"])
        calls.clear()
        self.assertEqual(apply(again["plan_digest"])["status"], "complete")
        self.assertIn(["/usr/sbin/userdel", "boring-crocodile-mcp"], calls)
        self.assertFalse(state.exists())
        with self.assertRaisesRegex(UninstallConflict, "No site here"):
            self.plan(site=True, delete_data=True)

    def test_a_rerun_with_delete_data_finishes_what_a_stopped_apply_left(self):
        """The apply stops at userdel, after config.toml was rewritten; a re-run reads
        the Pond from its --add-pond folder and removes what is left of it."""
        from boring_crocodile import oauth_records as records
        database = self.root / "var/lib/boring-crocodile/oauth.sqlite3"
        database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(database)) as db, db:
            records.initialize(db)
            records.add_service(db, "shared", "https://example.test/mcp", ["pond", "second"])
            db.execute("INSERT INTO records VALUES(?,?,?,NULL,NULL)", ("access", "second-token", json.dumps(dict(
                subject="00000000-0000-4000-8000-000000000002", client_id="client", scopes=["mcp"]))))
        database.chmod(0o600)
        credential = self.root / "etc/boring-crocodile/credentials/second-approval-secret"
        credential.parent.mkdir()
        credential.write_text("secret\n")
        folder = self.root / "etc/boring-crocodile/ponds/second"
        folder.mkdir(parents=True)
        receipt = folder / "initial-setup.json"
        receipt.write_text(json.dumps({
            "status": "complete", "pond_name": "second", "profile_id": "00000000-0000-4000-8000-000000000002",
            "inputs": {"execution_user": "boring-crocodile-second", "control_user": "boring-crocodile-mcp",
                       "home": "/home/boring-crocodile-second"}}))
        receipt.chmod(0o600)
        reality, calls, failing = FakeReality(), [], [True]
        safe_file = uninstall._safe_file

        def runner(args, check):
            calls.append(args)
            if args[0] == "/usr/sbin/userdel" and failing:
                failing.pop()
                raise subprocess.CalledProcessError(1, args)  # the first run stops here

        def apply(digest):
            with patch.object(uninstall, "plan", partial(plan, reality=reality)), \
                    patch.object(uninstall, "_actual_user", reality.user), \
                    patch.object(uninstall, "_actual_group", reality.group), \
                    patch.object(uninstall, "_safe_file", lambda path, owner=0: safe_file(path, os.getuid())), \
                    patch.object(uninstall.grp, "getgrnam",
                                 return_value=SimpleNamespace(gr_mem=["boring-crocodile-mcp"])):
                return uninstall._apply_locked(approved_digest=digest, pond="second", site=False,
                                               delete_data=True, root=self.root, runner=runner)

        first, _ = self.plan(pond="second", delete_data=True)
        with self.assertRaises(subprocess.CalledProcessError):
            apply(first["plan_digest"])
        self.assertNotIn("second", self.config.read_text())  # config.toml rewritten, data steps half done
        self.assertFalse(credential.exists())
        self.assertTrue(folder.exists())
        with self.assertRaisesRegex(UninstallConflict, "parked"):
            self.plan(pond="second")
        again, _ = self.plan(pond="second", delete_data=True)
        kinds = [a["kind"] for a in again["actions"]]
        self.assertEqual(kinds, ["revoke_profile", "remove_membership", "delete_user", "delete_group",
                                 "remove", "start_site"])
        self.assertEqual(again["actions"][-2]["path"], str(folder))
        calls.clear()
        result = apply(again["plan_digest"])
        self.assertEqual(result["status"], "complete")
        self.assertFalse(folder.exists())
        self.assertIn(["/usr/sbin/userdel", "boring-crocodile-second"], calls)
        self.assertEqual(calls[-1], ["/usr/bin/systemctl", "start", "boring-crocodile-mcp.target"])
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(list(records.rows(db)), [])
            self.assertEqual(records.service(db, name="shared")["ponds"], ["pond"])
        with self.assertRaisesRegex(UninstallConflict, "Unknown Pond"):
            self.plan(pond="second", delete_data=True)  # nothing of it is left

    def test_site_without_data_marks_the_activation_receipt_as_the_way_back(self):
        state = self.root / "etc/boring-crocodile"
        receipt = state / "site-activation.json"
        receipt.write_text(json.dumps({"schema_version": 1, "status": "complete", "plan_digest": "a" * 64}))
        receipt.chmod(0o600)
        credential = state / "credentials/approval-secret"
        credential.parent.mkdir()
        credential.write_text("kept\n")
        proposal, _ = self.plan(site=True)
        kinds = [a["kind"] for a in proposal["actions"]]
        self.assertEqual(kinds[:3], ["stop_site", "disable_site", "mark_uninstalled"])  # marked first
        self.assertEqual(proposal["actions"][-2], {"kind": "remove", "path": str(self.config)})  # config last
        kept = proposal["actions"][2]["kept"]
        self.assertEqual((kept["live_ponds"], kept["pirate_callers"]), (["pond", "second"], {}))
        self.assertEqual(set(kept["ceilings"]["second"]), {"memory_bytes", "swap_bytes", "tasks_max", "task_tasks_max"})
        with_data, _ = self.plan(site=True, delete_data=True)
        self.assertFalse([a for a in with_data["actions"] if a["kind"] == "mark_uninstalled"])
        with patch.object(uninstall, "plan", partial(plan, reality=FakeReality())):
            uninstall._apply_locked(approved_digest=proposal["plan_digest"], pond=None, site=True,
                                    delete_data=False, root=self.root, runner=lambda *a, **k: None)
        self.assertFalse(self.config.exists())
        self.assertEqual(json.loads(receipt.read_text())["status"], "uninstalled")
        self.assertEqual(json.loads(receipt.read_text())["plan_digest"], "a" * 64)
        self.assertEqual(json.loads(receipt.read_text())["kept"], kept)
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        self.assertEqual(credential.read_text(), "kept\n")

    def test_a_rerun_of_site_finishes_after_a_stop_in_between(self):
        state = self.root / "etc/boring-crocodile"
        receipt = state / "site-activation.json"
        receipt.write_text(json.dumps({"schema_version": 1, "status": "complete", "plan_digest": "a" * 64}))
        receipt.chmod(0o600)
        proposal, _ = self.plan(site=True)
        controller = self.units / "boring-crocodile-mcp.service"
        delete = uninstall._delete_path

        def stopping(path):
            if path == controller:
                raise OSError("stopped here")
            delete(path)

        with patch.object(uninstall, "plan", partial(plan, reality=FakeReality())), \
                patch.object(uninstall, "_delete_path", stopping):
            with self.assertRaisesRegex(OSError, "stopped here"):
                uninstall._apply_locked(approved_digest=proposal["plan_digest"], pond=None, site=True,
                                        delete_data=False, root=self.root, runner=lambda *a, **k: None)
        self.assertEqual(json.loads(receipt.read_text())["status"], "uninstalled")  # the way back is marked
        self.assertTrue(self.config.exists() and controller.exists())
        self.assertFalse((self.units / "boring-crocodile-worker-second.service").exists())
        again, _ = self.plan(site=True)  # site-check could not pass now
        self.assertEqual(again["actions"][-2:], [{"kind": "remove", "path": str(self.config)},
                                                 {"kind": "daemon_reload"}])
        calls = []
        with patch.object(uninstall, "plan", partial(plan, reality=FakeReality())):
            uninstall._apply_locked(approved_digest=again["plan_digest"], pond=None, site=True,
                                    delete_data=False, root=self.root, runner=lambda args, check: calls.append(args))
        self.assertFalse(self.config.exists())
        self.assertEqual([p.name for p in self.units.iterdir() if "crocodile" in p.name], [])
        self.assertEqual(list((self.root / "etc/tmpfiles.d").glob("boring-crocodile*")), [])
        self.assertIn(["/usr/bin/systemctl", "daemon-reload"], calls)
        self.assertEqual(json.loads(receipt.read_text())["status"], "uninstalled")
        with self.assertRaisesRegex(UninstallConflict, "nothing of it is left"):
            self.plan(site=True)

    def test_a_home_root_owns_is_handed_over_only_once(self):
        home = self.root / "home/boring-crocodile-second"
        home.mkdir(parents=True)
        real = Path.lstat

        def as_root(mode):
            def lstat(path):
                info = real(path)
                if path != home:
                    return info
                fields = list(info)
                fields[0], fields[4] = (info.st_mode & ~0o7777) | mode, 0
                return os.stat_result(fields)
            return lstat

        with patch.object(Path, "lstat", as_root(0o755)):  # a root folder setup never made: refused
            with self.assertRaisesRegex(UninstallConflict, "not handed over"):
                self.plan(pond="second", delete_data=True)
        with patch.object(Path, "lstat", as_root(0o700)):  # handed over by an earlier run: no step
            proposal, _ = self.plan(pond="second", delete_data=True)
        kinds = [a["kind"] for a in proposal["actions"]]
        self.assertNotIn("secure_home", kinds)
        self.assertIn("delete_user", kinds)
        with self.assertRaisesRegex(UninstallConflict, "ownership changed"):
            uninstall._check_home(home, 0)  # apply never accepts root's home as the worker's

    def test_apply_refuses_a_stale_digest(self):
        with patch.object(uninstall, "plan", partial(plan, reality=FakeReality())):
            with self.assertRaisesRegex(UninstallConflict, "Host changed"):
                uninstall._apply_locked(approved_digest="0" * 64, pond="second", site=False,
                                        delete_data=False, root=self.root, runner=lambda *a, **k: None)
        self.assertIn("second", self.config.read_text())

    def test_an_irregular_site_is_refused(self):
        unit = self.units / "boring-crocodile-worker-second.service"
        unit.write_text(unit.read_text().replace("Restart=on-failure", "Restart=always"))
        with self.assertRaisesRegex(UninstallConflict, "site-check found"):
            self.plan(pond="second")

    def test_selection_rules(self):
        with self.assertRaisesRegex(UninstallConflict, "owns the site"):
            self.plan(pond="pond")
        with self.assertRaisesRegex(UninstallConflict, "Unknown Pond"):
            self.plan(pond="ghost")
        self.config.unlink()
        with self.assertRaisesRegex(UninstallConflict, "No site here"):
            self.plan(site=True)


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
                                ["happy-hippo", "pond", "pirate/parrot"])
            records.add_service(db, "exclusive", "https://other.test/mcp", ["happy-hippo"])
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
            self.assertEqual(records.service(db, name="shared")["ponds"], ["pond", "pirate/parrot"])
            self.assertEqual(records.service(db, name="exclusive")["ponds"], [])

    def test_malformed_service_grants_refuse_cleanup_and_roll_back(self):
        from boring_crocodile import oauth_records as records
        from boring_crocodile.uninstall import _revoke
        database = self.root / "var/lib/boring-crocodile/oauth.sqlite3"
        with sqlite3.connect(database) as db:
            records.initialize(db)
            records.add_service(db, "bad", "https://example.test/mcp", ["happy-hippo"])
            db.execute("UPDATE services SET ponds=?", ('"not-a-list"',))
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
                    _preflight({"actions": [action]})
            self.assertTrue(path.exists())


class SecureHomeTest(unittest.TestCase):
    """On disk; the test gives the home to its own user, root would give it to root."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "boring-crocodile-second"
        (self.home / "work").mkdir(parents=True)
        (self.home / "work/notes.md").write_text("kept\n")
        self.home.chmod(0o755)
        self.me = (os.getuid(), os.getgid())

    def tearDown(self):
        self.temporary.cleanup()

    def test_the_top_folder_changes_owner_and_mode_only(self):
        inside = (self.home / "work").stat().st_mode
        _secure_home(self.home, os.getuid(), owner=self.me)
        info = self.home.lstat()
        self.assertEqual((info.st_mode & 0o7777, info.st_uid, info.st_gid), (0o700, *self.me))
        self.assertEqual((self.home / "work/notes.md").read_text(), "kept\n")
        self.assertEqual((self.home / "work").stat().st_mode, inside)

    def test_a_symlink_a_mount_or_another_owner_is_refused_untouched(self):
        link = self.home.with_name("link")
        link.symlink_to(self.home)
        with self.assertRaisesRegex(UninstallConflict, "symlinked home"):
            _secure_home(link, os.getuid(), owner=self.me)
        with patch("boring_crocodile.uninstall._mount_points", return_value=[str(self.home)]):
            with self.assertRaisesRegex(UninstallConflict, "mounted home"):
                _secure_home(self.home, os.getuid(), owner=self.me)
        with self.assertRaisesRegex(UninstallConflict, "ownership changed"):
            _secure_home(self.home, os.getuid() + 1, owner=self.me)
        self.assertEqual(self.home.lstat().st_mode & 0o777, 0o755)


class UserDeletionTest(unittest.TestCase):
    def test_transient_userdel_busy_is_retried(self):
        calls = []
        def runner(args, check):
            calls.append((args, check))
            if len(calls) < 3:
                raise subprocess.CalledProcessError(8, args)
        _delete_user("test-worker", runner, sleep=lambda _: None)
        self.assertEqual(len(calls), 3)

    def test_non_busy_userdel_error_is_not_retried(self):
        calls = []
        def runner(args, check):
            calls.append((args, check))
            raise subprocess.CalledProcessError(6, args)
        with self.assertRaises(subprocess.CalledProcessError):
            _delete_user("test-worker", runner, sleep=lambda _: None)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
