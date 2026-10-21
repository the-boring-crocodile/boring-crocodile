"""--site-extend reads the site as it is: the one-Pond reference plus a completed
--add-pond for "second" must become exactly the two-Pond reference."""
import json
import os
import shutil
import sqlite3
import subprocess
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from boring_crocodile import oauth_records, site_extend, site_setup
from boring_crocodile.oauth_admin import OAuthAdmin
from boring_crocodile.setup import SetupInputs, context_payload
from boring_crocodile.site_check import Reality
from boring_crocodile.site_setup import SiteConflict

SITE = Path(__file__).resolve().parent.parent / "site/default"
UIDS = {"boring-crocodile": 901, "boring-crocodile-mcp": 902, "boring-crocodile-second": 903,
        "boring-crocodile-beta": 904}
HOMES = {"/home/boring-crocodile": "boring-crocodile", "/home/boring-crocodile-second": "boring-crocodile-second",
         "/home/boring-crocodile-beta": "boring-crocodile-beta"}
SECOND = dict(execution_user="boring-crocodile-second", control_user="boring-crocodile-mcp",
              home="/home/boring-crocodile-second", context_mode="starter", context_source=None, context_files=[])
CEILINGS = dict(memory_bytes=2 * 1024**3, swap_bytes=0, tasks_max=256, task_tasks_max=128)


def account(name):
    home = "/nonexistent" if name == "boring-crocodile-mcp" else "/home/" + name
    return dict(name=name, uid=UIDS[name], gid=UIDS[name], home=home, shell="/usr/sbin/nologin")


class FakeReality(Reality):
    def __init__(self):
        self.homes = dict(HOMES)
        self.accounts = {name: account(name) for name in UIDS}
        self.controller_groups = [902, 901, 903, 904]

    def owner(self, path):
        try:
            return self.homes[str(path)]
        except KeyError:
            raise FileNotFoundError(path) from None

    def user(self, name):
        return dict(self.accounts[name])

    def groups(self, name):
        return list(self.controller_groups)

    def uid_name(self, uid):
        return {value: key for key, value in UIDS.items()}[uid]

    def is_root(self):
        return False


class FakePaths(site_extend.Paths):
    def __init__(self, root: Path):
        super().__init__(root / "etc/boring-crocodile", root / "etc/systemd/system",
                         root / "etc/tmpfiles.d", root / "var", owner=os.getuid())
        self.provisioned = []

    def provision(self, names):
        self.provisioned.extend(names)


class ExtendTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        shutil.copytree(SITE / "etc", self.root / "etc")
        self.paths = FakePaths(self.root)
        self.reality = FakeReality()
        self.calls = []

    def tearDown(self):
        self.directory.cleanup()

    def add_pond(self, status="complete", name="second", worker="boring-crocodile-second"):
        """What --add-pond leaves behind: its folder with the progress journal."""
        setup = SetupInputs.parse(SECOND | dict(execution_user=worker, home="/home/" + worker))
        folder = self.paths.ponds / name
        folder.mkdir(parents=True)
        journal = dict(schema_version=2, status=status, pond_name=name, inputs=setup.as_input(),
                       profile_id=f"00000000-0000-4000-8000-{UIDS[worker] - 901:012d}",
                       context_root=setup.home + "/handbook",
                       documents=[{"filename": f} for f, _ in context_payload(setup)],
                       users={n: account(n) for n in (setup.execution_user, setup.control_user)})
        path = folder / "initial-setup.json"
        path.write_text(json.dumps(journal))
        path.chmod(0o600)

    def plan(self, grant=None):
        return site_extend.plan(grant, CEILINGS, paths=self.paths, reality=self.reality)[0]

    def apply(self, digest, grant=None):
        return site_extend.apply(digest, grant, CEILINGS, paths=self.paths, reality=self.reality,
                                 runner=lambda command, **kwargs: self.calls.append(command),
                                 settled=lambda policy: "healthy")

    def tree(self, base):
        return {str(p.relative_to(base)): p.read_text() for p in sorted(base.rglob("*"))
                if p.is_file() and "ponds" not in p.parts and p.name != "site.lock"}

    def test_nothing_to_add(self):
        extension = self.plan()
        self.assertEqual((extension["status"], extension["changes"], extension["restart"]),
                         ("nothing_to_add", [], []))

    def test_a_completed_pond_makes_the_two_pond_reference(self):
        self.add_pond()
        before = self.paths.config.read_text()
        extension = self.plan()
        self.assertEqual(extension["added_ponds"], ["second"])
        actions = {Path(c["path"]).name: c["action"] for c in extension["changes"]}
        self.assertEqual(actions, {
            "boring-crocodile-mcp.service": "replace", "config.toml": "append",
            "boring-crocodile-worker-second.service": "new", "boring-crocodile-task-second.socket": "new",
            "boring-crocodile-task-second@.service": "new", "system-crocodilesecond.slice": "new",
            "boring-crocodile-second.conf": "new"})
        config_diff = next(c["diff"] for c in extension["changes"] if c["action"] == "append")
        self.assertFalse([line for line in config_diff if line.startswith("-")])  # nothing removed
        result = self.apply(extension["plan_digest"])
        self.assertEqual((result["status"], result["site_check"]), ("extended", "regular"))
        self.assertEqual(self.tree(self.root / "etc"), self.tree(SITE / "two-ponds/etc"))
        self.assertTrue(self.paths.config.read_text().startswith(before))
        self.assertEqual(self.paths.provisioned, ["second-approval-secret"])
        enable = ["/usr/bin/systemctl", "enable", "--now", "boring-crocodile-task-second.socket"]
        order = [self.calls.index(["/usr/bin/systemctl", "daemon-reload"]), self.calls.index(enable),
                 self.calls.index(["/usr/bin/systemctl", "restart", "boring-crocodile-mcp.service"])]
        self.assertEqual(order, sorted(order))
        backup = next((self.root / "var/site-backups").iterdir())
        self.assertEqual(sorted(p.name for p in backup.iterdir()), ["boring-crocodile-mcp.service", "config.toml"])
        self.assertEqual((backup / "config.toml").read_text(), before)
        log = [json.loads(line) for line in (self.root / "var/site-log.jsonl").read_text().splitlines()]
        self.assertEqual(log[0]["added_ponds"], ["second"])
        self.assertEqual(self.plan()["status"], "nothing_to_add")  # the log is never read back

    def test_apply_provisions_the_credential_and_installs_new_units_on_disk(self):
        self.add_pond()
        paths = site_extend.Paths(self.paths.state, self.paths.units, self.paths.tmpfiles, self.paths.var,
                                  owner=os.getuid())
        self.paths = paths
        extension = self.plan()
        replaced = [Path(c["path"]) for c in extension["changes"] if c["action"] != "new"]
        for path in replaced:  # a replaced file keeps its mode, whichever command writes it
            path.chmod(0o640)
        with patch.object(site_setup, "STATE", paths.state), \
                patch.object(site_setup, "Host", return_value=SimpleNamespace(protected=lambda path: None)):
            self.apply(extension["plan_digest"])
        secret = paths.credentials / "second-approval-secret"
        self.assertTrue(secret.read_text().strip())
        self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
        self.assertEqual(paths.credentials.stat().st_mode & 0o777, 0o700)
        for change in extension["changes"]:
            path = Path(change["path"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o640 if path in replaced else 0o644, path)
            reference = SITE / "two-ponds/etc" / path.relative_to(self.root / "etc")
            self.assertEqual(path.read_text(), reference.read_text(), path)

    def test_a_name_sorting_before_existing_ponds_keeps_the_site_regular(self):
        self.add_pond()
        self.apply(self.plan()["plan_digest"])
        self.add_pond(name="beta", worker="boring-crocodile-beta")
        result = self.apply(self.plan()["plan_digest"])
        self.assertEqual(result["site_check"], "regular")
        control = (self.paths.units / "boring-crocodile-mcp.service").read_text()
        self.assertIn("worker-pond.service boring-crocodile-worker-beta.service "
                      "boring-crocodile-worker-second.service", control)

    def test_an_irregular_site_is_refused_before_anything_else(self):
        self.add_pond()
        unit = self.paths.units / "boring-crocodile-worker-pond.service"
        unit.write_text(unit.read_text().replace("Restart=on-failure", "Restart=always"))
        with self.assertRaisesRegex(SiteConflict, "site-check found"):
            self.plan()

    def test_a_stale_digest_changes_nothing(self):
        self.add_pond()
        before = self.tree(self.root / "etc")
        with self.assertRaisesRegex(SiteConflict, "changed since the reviewed plan"):
            self.apply("stale")
        self.assertEqual(self.tree(self.root / "etc"), before)
        self.assertEqual(self.calls, [])

    def test_accounts_must_still_be_what_add_pond_created(self):
        self.add_pond()
        self.reality.accounts["boring-crocodile-second"]["shell"] = "/bin/bash"
        with self.assertRaisesRegex(SiteConflict, "differs from what --add-pond created"):
            self.plan()

    def test_the_home_and_the_controller_membership_are_checked(self):
        self.add_pond()
        self.reality.homes["/home/boring-crocodile-second"] = "boring-crocodile"
        with self.assertRaisesRegex(SiteConflict, "belongs to boring-crocodile"):
            self.plan()
        self.reality.homes = dict(HOMES)
        self.reality.controller_groups = [902, 901]
        with self.assertRaisesRegex(SiteConflict, "not a member"):
            self.plan()

    def test_a_parked_pond_comes_back_only_when_named(self):
        self.add_pond()
        self.paths.credentials.mkdir(exist_ok=True)
        (self.paths.credentials / "second-approval-secret").write_text("kept by uninstall\n")
        extension = self.plan()
        self.assertEqual((extension["status"], extension["parked_ponds"]), ("nothing_to_add", ["second"]))
        with self.assertRaisesRegex(SiteConflict, "Not a parked Pond"):
            site_extend.plan(None, CEILINGS, paths=self.paths, reality=self.reality, bring_back=["ghost"])
        back = site_extend.plan(None, CEILINGS, paths=self.paths, reality=self.reality, bring_back=["second"])[0]
        self.assertEqual((back["added_ponds"], back["brought_back"], back["new_credentials"]),
                         (["second"], ["second"], []))

    def test_an_incomplete_add_pond_is_listed_not_added(self):
        self.add_pond(status="prepared")
        extension = self.plan()
        self.assertEqual((extension["status"], extension["incomplete_ponds"]), ("nothing_to_add", ["second"]))

    def test_services_are_granted_only_when_asked(self):
        self.add_pond()
        store = SimpleNamespace(
            service_list=lambda: [{"name": "canvas", "ponds": ["pond"]}, {"name": "knowledge", "ponds": ["pond"]}],
            service_grants=lambda name, after, digest=None: {"name": name, "after": after, "plan_digest": "g-" + name})
        with patch.object(site_extend, "_store", return_value=store):
            self.assertEqual(self.plan()["service_grants"], [])
            chosen = self.plan(["canvas"])["service_grants"]
            every = self.plan(["all"])["service_grants"]
            with self.assertRaisesRegex(SiteConflict, "Unknown service"):
                self.plan(["fishmarket"])
        self.assertEqual(chosen, [{"name": "canvas", "after": ["pond", "second"], "plan_digest": "g-canvas"}])
        self.assertEqual([g["name"] for g in every], ["canvas", "knowledge"])

    def test_granted_services_gain_the_new_pond_in_the_store(self):
        self.add_pond()
        folder = self.root / "oauth"
        folder.mkdir(mode=0o700)
        database = folder / "oauth.sqlite3"
        with closing(sqlite3.connect(database)) as db, db:
            oauth_records.initialize(db)
            oauth_records.add_service(db, "canvas", "https://canvas.test/mcp", ["pond"])
            oauth_records.add_service(db, "knowledge", "https://knowledge.test/mcp", ["pond"])
        database.chmod(0o600)
        store = OAuthAdmin(database, os.getuid())
        with patch.object(site_extend, "_store", return_value=store):
            result = self.apply(self.plan(["canvas"])["plan_digest"], ["canvas"])
        self.assertEqual(result["service_grants"], ["canvas"])
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(oauth_records.service(db, name="canvas")["ponds"], ["pond", "second"])
            self.assertEqual(oauth_records.service(db, name="knowledge")["ponds"], ["pond"])

    def test_grants_are_in_the_store_before_a_failed_restart(self):
        """A re-run after the restart failed finds nothing to add: the grants must
        already be there."""
        self.add_pond()
        folder = self.root / "oauth"
        folder.mkdir(mode=0o700)
        database = folder / "oauth.sqlite3"
        with closing(sqlite3.connect(database)) as db, db:
            oauth_records.initialize(db)
            oauth_records.add_service(db, "canvas", "https://canvas.test/mcp", ["pond"])
        database.chmod(0o600)
        store = OAuthAdmin(database, os.getuid())

        def runner(command, **kwargs):
            if command[1:2] == ["restart"]:
                raise subprocess.CalledProcessError(1, command)

        with patch.object(site_extend, "_store", return_value=store):
            digest = self.plan(["canvas"])["plan_digest"]
            with self.assertRaises(subprocess.CalledProcessError):
                site_extend.apply(digest, ["canvas"], CEILINGS, paths=self.paths, reality=self.reality,
                                  runner=runner, settled=lambda policy: "healthy")
            again = self.plan(["canvas"])
        self.assertEqual((again["status"], again["service_grants"]), ("nothing_to_add", []))
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(oauth_records.service(db, name="canvas")["ponds"], ["pond", "second"])


class ToldTest(unittest.TestCase):
    def test_after_apply_the_operator_reads_plain_lines(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder, "config.toml")
            config.write_text('[server]\npublic_base_url = "https://mcp.example.org"\n')
            text = site_extend.told({
                "status": "extended", "added_ponds": ["garden"], "backup": "/var/backup",
                "local_health": "healthy", "site_check": "regular",
                "pirates": [{"pirate": "parrot", "command": "boring-pirates-setup --offer garden"}],
                "next": "ignored"}, config)
        self.assertIn("connect a client to https://mcp.example.org/mcp/garden", text)
        self.assertIn("credentials/garden-approval-secret", text)
        self.assertIn("To offer it to the Pirate parrot: sudo boring-pirates-setup --offer garden", text)
        self.assertNotIn("{", text)

    def test_nothing_to_add_is_said_plainly(self):
        self.assertEqual(site_extend.told({"status": "nothing_to_add", "added_ponds": [], "applied": False}),
                         "Nothing to add; the site is unchanged.")


class RecordsTest(unittest.TestCase):
    """Root's log and backups never go through a link or into a folder others can write."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.me = os.getuid()

    def test_a_planted_log_link_is_refused_and_its_target_untouched(self):
        from boring_crocodile.site_layout import append_record
        records = self.root / "records"
        records.mkdir(mode=0o700)
        target = self.root / "victim"
        target.write_text("untouched\n")
        (records / "site-log.jsonl").symlink_to(target)
        with self.assertRaises(OSError):
            append_record("{}", records, owner=self.me)
        self.assertEqual(target.read_text(), "untouched\n")

    def test_a_records_folder_that_is_a_link_or_writable_by_others_is_refused(self):
        from boring_crocodile.site_layout import records_dir
        real = self.root / "real"
        real.mkdir(mode=0o700)
        (self.root / "link").symlink_to(real)
        with self.assertRaisesRegex(ValueError, "Unsafe records folder"):
            records_dir(self.root / "link", owner=self.me)
        open_folder = self.root / "open"
        open_folder.mkdir()
        open_folder.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "Unsafe records folder"):
            records_dir(open_folder, owner=self.me)

    def test_the_log_is_appended_in_a_private_folder(self):
        from boring_crocodile.site_layout import append_record
        records = self.root / "new"
        append_record('{"a": 1}', records, owner=self.me)
        append_record('{"a": 2}', records, owner=self.me)
        self.assertEqual((records / "site-log.jsonl").read_text().splitlines(), ['{"a": 1}', '{"a": 2}'])
        self.assertEqual(records.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
