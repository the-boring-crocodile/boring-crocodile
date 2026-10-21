"""pirate-add and pirate-remove on the one-Pond reference site: what the site gives
a Pirate (caller entry, pond-pirates, its workers in pirate-clients), planned and
applied by digest, and taken back."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from boring_crocodile import site_extend, site_pirates
from boring_crocodile.site_layout import SiteLocked
from boring_crocodile.pirates import REGISTRATION_GROUP, REGISTRATION_ROOT, registration_path
from boring_crocodile.site_check import _values
from boring_crocodile.site_setup import SiteConflict
from test_site_extend import CEILINGS, SITE, UIDS, ExtendTest, FakeReality, FakePaths, account

GROUPS = {"pond-pirates": 1005, "pirate-clients": 987}


class PirateReality(FakeReality):
    def __init__(self):
        super().__init__()
        self.accounts["pirate-mo"] = dict(name="pirate-mo", uid=950, gid=950, home="/home/pirate-mo",
                                          shell="/usr/sbin/nologin")
        self.membership = {"pirate-mo": [950, GROUPS["pirate-clients"]]}
        self.running = {}  # unit -> the groups its running process started with; absent: not running

    def running_groups(self, unit):
        return self.running.get(unit)

    def group(self, name):
        return {"name": name, "gid": GROUPS[name]} if name in GROUPS else None

    def groups(self, name):
        if name in self.membership:
            return list(self.membership[name])
        return [self.accounts[name]["gid"]] if name != "boring-crocodile-mcp" else list(self.controller_groups)

    def uid_name(self, uid):
        return {a["uid"]: n for n, a in self.accounts.items()}[uid]


def open_to_guests(etc: Path) -> None:
    """The reference site with its Pond accepting guests, as setup renders it."""
    parent = registration_path("pond").parent
    config = etc / "boring-crocodile/config.toml"
    # In [environments.pond], before the file-root table setup renders last.
    text = config.read_text()
    root = "[environments.pond.files.roots.home]\n"
    config.write_text(text.replace(root, "service_registration_enabled = true\n" + root, 1))
    assert "service_registration_enabled = true" in config.read_text()
    worker = etc / "systemd/system/boring-crocodile-worker-pond.service"
    worker.write_text(worker.read_text().replace("ReadWritePaths=", f"ReadWritePaths={parent} ", 1))
    (tmpfiles,) = (etc / "tmpfiles.d").glob("*.conf")
    tmpfiles.write_text(tmpfiles.read_text() + f"d {REGISTRATION_ROOT} 0755 root root - -\n"
                        f"d {parent} 2750 boring-crocodile {REGISTRATION_GROUP} - -\n")


class PirateTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        shutil.copytree(SITE / "etc", root / "etc")
        open_to_guests(root / "etc")
        self.paths, self.reality, self.calls = FakePaths(root), PirateReality(), []
        report = self.paths.check(self.reality).run()
        assert report["status"] == "regular", report

    def tearDown(self):
        self.directory.cleanup()

    def runner(self, command, **_):
        self.calls.append(command)

    def apply(self, kind, digest, *args):
        return site_pirates.apply(kind, digest, *args, paths=self.paths, reality=self.reality,
                                  runner=self.runner, settled=lambda _: "healthy")

    def test_add_plans_the_caller_groups_and_restarts_then_applies_by_digest(self):
        plan, _ = site_pirates.plan_add("mo", "pirate-mo", ["all"], paths=self.paths, reality=self.reality)
        self.assertEqual(plan["ponds"], ["pond"])
        self.assertIn("+mo = 950", plan["changes"][0]["diff"])
        self.assertIn("+[pirate_services.callers]", plan["changes"][0]["diff"])
        self.assertEqual(plan["groups"], [{"group": "pond-pirates", "add": ["pirate-mo"]},
                                          {"group": "pirate-clients", "add": ["boring-crocodile"]}])
        self.assertEqual(plan["restart"], ["boring-crocodile-worker-pond.service", "boring-crocodile-mcp.service"])
        self.assertEqual(plan["for_site_json"]["client_uids"], [UIDS["boring-crocodile"]])
        self.assertEqual(plan["for_site_json"]["service_authority"]["uid"], UIDS["boring-crocodile-mcp"])
        with self.assertRaisesRegex(SiteConflict, "changed since"):
            self.apply("add", "0" * 64, "mo", "pirate-mo", ["all"])
        outcome = self.apply("add", plan["plan_digest"], "mo", "pirate-mo", ["all"])
        self.assertEqual(outcome["status"], "admitted", outcome)
        self.assertIn(["/usr/bin/gpasswd", "-a", "pirate-mo", "pond-pirates"], self.calls)
        self.assertIn(["/usr/bin/gpasswd", "-a", "boring-crocodile", "pirate-clients"], self.calls)
        self.assertIn("mo = 950", self.paths.config.read_text())
        log = json.loads((self.paths.var / "site-log.jsonl").read_text().splitlines()[-1])
        self.assertEqual((log["kind"], log["pirate"], log["site_check_after"]), ("pirate_admission", "mo", "regular"))
        self.assertTrue((Path(outcome["backup"]) / "config.toml").is_file())
        again, _ = site_pirates.plan_add("mo", "pirate-mo", ["all"], paths=self.paths, reality=self.reality)
        self.assertEqual((again["kind"], again["changes"]), ("pirate_extension", []))  # never a second caller

    def test_site_changes_exclude_each_other(self):
        """While pirate-add applies, --site-extend refuses at once (both take the one
        site lock) and changes nothing; pirate-add itself completes."""
        plan, _ = site_pirates.plan_add("mo", "pirate-mo", ["all"], paths=self.paths, reality=self.reality)
        before, refused = self.paths.config.read_bytes(), []

        def runner(command, **_):
            if command[:2] == ["/usr/bin/gpasswd", "-a"] and not refused:  # pirate-add holds the lock
                with self.assertRaises(SiteLocked) as caught:
                    site_extend.apply("0" * 64, paths=self.paths, reality=self.reality,
                                      runner=self.runner, settled=lambda _: "healthy")
                refused.append(str(caught.exception))
                self.assertEqual(self.paths.config.read_bytes(), before)
            self.calls.append(command)

        outcome = site_pirates.apply("add", plan["plan_digest"], "mo", "pirate-mo", ["all"], paths=self.paths,
                                     reality=self.reality, runner=runner, settled=lambda _: "healthy")
        self.assertEqual(outcome["status"], "admitted")
        self.assertEqual(len(refused), 1)
        self.assertIn("site.lock", refused[0])
        self.assertIn("mo = 950", self.paths.config.read_text())
        self.assertFalse([c for c in self.calls if c[0] == "/usr/bin/systemd-analyze"])  # extend ran nothing

    def test_a_worker_already_in_pirate_clients_is_restarted_too(self):
        self.reality.membership["boring-crocodile"] = [UIDS["boring-crocodile"], GROUPS["pirate-clients"]]
        plan, _ = site_pirates.plan_add("mo", "pirate-mo", ["pond"], paths=self.paths, reality=self.reality)
        self.assertEqual(plan["restart"], ["boring-crocodile-worker-pond.service", "boring-crocodile-mcp.service"])
        self.assertEqual([g["group"] for g in plan["groups"]], ["pond-pirates"])

    def test_a_rerun_after_the_group_step_finishes_the_admission(self):
        plan, _ = site_pirates.plan_add("mo", "pirate-mo", ["all"], paths=self.paths, reality=self.reality)
        before = self.paths.config.read_bytes()

        def failing(command, **_):
            self.calls.append(command)
            if command == ["/usr/bin/gpasswd", "-a", "boring-crocodile", "pirate-clients"]:
                raise subprocess.CalledProcessError(1, command)
            self.reality.membership.setdefault(command[2], []).append(GROUPS[command[3]])

        with self.assertRaises(subprocess.CalledProcessError):
            site_pirates.apply("add", plan["plan_digest"], "mo", "pirate-mo", ["all"], paths=self.paths,
                               reality=self.reality, runner=failing, settled=lambda _: "healthy")
        self.assertEqual(self.paths.config.read_bytes(), before)  # stopped before config.toml
        again, _ = site_pirates.plan_add("mo", "pirate-mo", ["all"], paths=self.paths, reality=self.reality)
        self.assertEqual(again["groups"], [{"group": "pirate-clients", "add": ["boring-crocodile"]}])
        self.calls.clear()
        outcome = self.apply("add", again["plan_digest"], "mo", "pirate-mo", ["all"])
        self.assertEqual(outcome["status"], "admitted")
        self.assertIn("mo = 950", self.paths.config.read_text())
        self.assertIn(["/usr/bin/systemctl", "restart", "boring-crocodile-worker-pond.service",
                       "boring-crocodile-mcp.service"], self.calls)

    def test_a_pond_closed_to_guests_is_refused(self):
        closed = Path(self.directory.name) / "closed"
        shutil.copytree(SITE / "etc", closed / "etc")
        for ponds in (["pond"], ["all"]):
            with self.subTest(ponds=ponds), self.assertRaisesRegex(SiteConflict, "accepts guests"):
                site_pirates.plan_add("mo", "pirate-mo", ponds, paths=FakePaths(closed), reality=self.reality)

    def test_refusals(self):
        for args, message in ((("mo", "pirate-mo", ["nowhere"]), "Not a Pond"),
                              (("mo", "pirate-nobody", ["all"]), "No account"),
                              (("Mo!", "pirate-mo", ["all"]), "lowercase")):
            with self.subTest(args=args), self.assertRaisesRegex(SiteConflict, message):
                site_pirates.plan_add(*args, paths=self.paths, reality=self.reality)
        unit = self.paths.units / "boring-crocodile-worker-pond.service"
        unit.write_text(unit.read_text() + "ExecStartPre=/bin/true\n")
        with self.assertRaisesRegex(SiteConflict, "site-check found"):
            site_pirates.plan_add("mo", "pirate-mo", ["all"], paths=self.paths, reality=self.reality)

    def live(self, command, **_):
        """A runner whose group changes and restarts the fake host sees."""
        self.calls.append(command)
        if command[:2] == ["/usr/bin/gpasswd", "-a"]:
            self.reality.membership.setdefault(command[2], [UIDS.get(command[2], 0)]).append(GROUPS[command[3]])
        if command[:2] == ["/usr/bin/systemctl", "restart"]:
            for unit in command[2:]:
                if unit.startswith("boring-crocodile-worker-"):
                    user = _values((self.paths.units / unit).read_text())["User"]
                    self.reality.running[unit] = self.reality.groups(user)

    def add_later(self, *args, runner=None):
        """pirate-add planned and applied with the live runner."""
        plan, _ = site_pirates.plan_add(*args, paths=self.paths, reality=self.reality)
        return plan, site_pirates.apply("add", plan["plan_digest"], *args, paths=self.paths, reality=self.reality,
                                        runner=runner or self.live, settled=lambda _: "healthy")

    def extend_with_second(self):
        """--site-extend brings the Pond second (it accepts guests, like the first)."""
        ExtendTest.add_pond(self)
        extension, _ = site_extend.plan(None, CEILINGS, paths=self.paths, reality=self.reality)
        outcome = site_extend.apply(extension["plan_digest"], None, CEILINGS, paths=self.paths,
                                    reality=self.reality, runner=self.live, settled=lambda _: "healthy")
        self.assertEqual(outcome["status"], "extended", outcome)
        return extension, outcome

    def test_a_pond_added_later_is_offered_to_an_existing_pirate(self):
        self.add_later("mo", "pirate-mo", ["all"])
        extension, extended = self.extend_with_second()
        command = "boring-crocodile-admin pirate-add mo --account pirate-mo --ponds second"
        self.assertEqual(extension["pirates"], [{"pirate": "mo", "account": "pirate-mo", "command": command}])
        self.assertIn("sudo " + command, site_extend.review(extension))
        self.assertIn("sudo " + command, extended["next"])
        self.assertNotIn("boring-crocodile-second", self.reality.membership)  # no automatic admission

        config, self.calls[:] = self.paths.config.read_bytes(), []
        plan, outcome = self.add_later("mo", "pirate-mo", ["all"])
        self.assertEqual((plan["kind"], plan["ponds"], plan["served"]), ("pirate_extension", ["second"], ["pond"]))
        self.assertEqual(plan["groups"], [{"group": "pirate-clients", "add": ["boring-crocodile-second"]}])
        self.assertEqual(plan["restart"], ["boring-crocodile-worker-second.service"])
        self.assertEqual(plan["for_site_json"]["client_uids"], [UIDS["boring-crocodile"], UIDS["boring-crocodile-second"]])
        self.assertIn("client_uids must include 901, 903; then systemctl restart boring-pirate@mo.service",
                      plan["pirate_side"])
        self.assertEqual(outcome["status"], "extended", outcome)
        self.assertEqual(self.calls, [["/usr/bin/gpasswd", "-a", "boring-crocodile-second", "pirate-clients"],
                                      ["/usr/bin/systemctl", "restart", "boring-crocodile-worker-second.service"]])
        self.assertIn(GROUPS["pirate-clients"], self.reality.groups("boring-crocodile-second"))
        self.assertEqual(self.paths.config.read_bytes(), config)  # the caller is there already
        self.assertEqual(list(Path(outcome["backup"]).iterdir()), [])
        log = json.loads((self.paths.var / "site-log.jsonl").read_text().splitlines()[-1])
        self.assertEqual((log["kind"], log["ponds"]), ("pirate_extension", ["second"]))

        # Run again: nothing to add, nothing changes.
        lines, self.calls[:] = (self.paths.var / "site-log.jsonl").read_text(), []
        for ponds in (["all"], ["second"], ["pond", "second"]):
            plan, outcome = self.add_later("mo", "pirate-mo", ponds)
            self.assertEqual((plan["status"], outcome["status"]), ("already_serves", "already_serves"))
            self.assertIn("already serves: ", site_pirates.review(plan))
        self.assertEqual(outcome["ponds"], ["pond", "second"])
        self.assertEqual((self.calls, self.paths.config.read_bytes()), ([], config))
        self.assertEqual((self.paths.var / "site-log.jsonl").read_text(), lines)

    def test_a_rerun_after_the_group_step_finishes_the_extension(self):
        self.add_later("mo", "pirate-mo", ["all"])
        self.extend_with_second()

        def failing(command, **kwargs):
            if command[:2] == ["/usr/bin/systemctl", "restart"]:
                raise subprocess.CalledProcessError(1, command)
            self.live(command, **kwargs)

        with self.assertRaises(subprocess.CalledProcessError):
            self.add_later("mo", "pirate-mo", ["second"], runner=failing)
        # In the group file, but its running worker started before: not served yet.
        self.reality.running["boring-crocodile-worker-second.service"] = [UIDS["boring-crocodile-second"]]
        self.calls.clear()
        plan, outcome = self.add_later("mo", "pirate-mo", ["second"])
        self.assertEqual((plan["ponds"], plan["groups"]), (["second"], []))
        self.assertEqual(self.calls, [["/usr/bin/systemctl", "restart", "boring-crocodile-worker-second.service"]])
        self.assertEqual(outcome["status"], "extended")
        self.assertEqual(site_pirates.plan_add("mo", "pirate-mo", ["all"], paths=self.paths,
                                               reality=self.reality)[0]["status"], "already_serves")

    def test_another_account_under_an_admitted_name_is_refused(self):
        self.add_later("mo", "pirate-mo", ["all"])
        self.reality.accounts["pirate-other"] = dict(name="pirate-other", uid=951, gid=951,
                                                     home="/home/pirate-other", shell="/usr/sbin/nologin")
        config, self.calls[:] = self.paths.config.read_bytes(), []
        with self.assertRaisesRegex(SiteConflict, "is account pirate-mo, uid 950, not pirate-other"):
            site_pirates.plan_add("mo", "pirate-other", ["all"], paths=self.paths, reality=self.reality)
        with self.assertRaisesRegex(SiteConflict, "Not a Pond"):
            site_pirates.plan_add("mo", "pirate-mo", ["nowhere"], paths=self.paths, reality=self.reality)
        self.assertEqual((self.calls, self.paths.config.read_bytes()), ([], config))

    def test_remove_takes_back_caller_and_registration(self):
        self.paths.config.chmod(0o640)  # config.toml keeps its mode whichever command writes it
        plan, _ = site_pirates.plan_add("mo", "pirate-mo", ["all"], paths=self.paths, reality=self.reality)
        self.apply("add", plan["plan_digest"], "mo", "pirate-mo", ["all"])
        self.reality.membership["pirate-mo"].append(GROUPS["pond-pirates"])
        removal, _ = site_pirates.plan_remove("mo", paths=self.paths, reality=self.reality)
        self.assertIn("-mo = 950", removal["changes"][0]["diff"])
        self.assertEqual(removal["groups"], [{"group": "pond-pirates", "remove": ["pirate-mo"]}])
        # Every worker the Pirate may have registered with restarts, so none keeps it.
        self.assertEqual(removal["ponds"], ["pond"])
        self.assertEqual(removal["restart"], ["boring-crocodile-worker-pond.service", "boring-crocodile-mcp.service"])
        self.calls.clear()
        admitted = self.paths.config.read_bytes()
        outcome = self.apply("remove", removal["plan_digest"], "mo")
        self.assertEqual(outcome["status"], "removed", outcome)
        log = json.loads((self.paths.var / "site-log.jsonl").read_text().splitlines()[-1])
        self.assertEqual((log["kind"], log["pirate"], log["site_check_after"], log["backup"]),
                         ("pirate_removal", "mo", "regular", outcome["backup"]))
        self.assertEqual((Path(outcome["backup"]) / "config.toml").read_bytes(), admitted)
        self.assertIn(["/usr/bin/gpasswd", "-d", "pirate-mo", "pond-pirates"], self.calls)
        self.assertEqual(self.calls[-1], ["/usr/bin/systemctl", "restart", "boring-crocodile-worker-pond.service",
                                          "boring-crocodile-mcp.service"])
        self.assertNotIn("mo = 950", self.paths.config.read_text())
        self.assertEqual(self.paths.config.stat().st_mode & 0o777, 0o640)
        self.assertIn("pirate-clients", outcome["kept"])
        with self.assertRaisesRegex(SiteConflict, "no Pirate named mo"):
            site_pirates.plan_remove("mo", paths=self.paths, reality=self.reality)


if __name__ == "__main__":
    unittest.main()
