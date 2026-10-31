"""--site-render writes the site's files as this release renders them, and with
--guests lets running Ponds accept guests: on the one-Pond reference site."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from boring_crocodile import site_render
from boring_crocodile.settings import Settings
from boring_crocodile.site_setup import SiteConflict

from test_site_extend import SITE, FakePaths, FakeReality

WORKER = "boring-crocodile-worker-pond.service"


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        shutil.copytree(SITE / "etc", self.root / "etc")
        self.paths = FakePaths(self.root)
        self.reality = FakeReality()
        self.calls = []
        group = patch.object(site_render.grp, "getgrnam", return_value=SimpleNamespace(gr_gid=990))
        group.start()
        self.addCleanup(group.stop)

    def tearDown(self):
        self.directory.cleanup()

    def plan(self, guests=None):
        return site_render.plan(guests, paths=self.paths, reality=self.reality)[0]

    def apply(self, digest, guests=None):
        return site_render.apply(digest, guests, paths=self.paths, reality=self.reality,
                                 runner=lambda command, **kwargs: (self.calls.append(command) or
                                                                   SimpleNamespace(returncode=0, stdout="active")),
                                 settled=lambda policy: "healthy")

    def tree(self):
        base = self.root / "etc"
        return {str(p.relative_to(base)): p.read_text() for p in sorted(base.rglob("*"))
                if p.is_file() and p.name != "site.lock"}

    def test_a_site_as_this_release_renders_it_has_nothing_to_render(self):
        rendering = self.plan()
        self.assertEqual((rendering["status"], rendering["changes"], rendering["restart"]),
                         ("nothing_to_render", [], []))

    def test_a_unit_an_older_release_rendered_is_written_and_the_site_restarted(self):
        unit = self.paths.units / WORKER
        old = unit.read_text()
        unit.write_text(old.replace("Restart=on-failure", "Restart=always"))  # as an older release wrote it
        before = unit.read_text()
        rendering = self.plan()
        self.assertEqual([(Path(c["path"]).name, c["action"]) for c in rendering["changes"]], [(WORKER, "replace")])
        self.assertIn("-Restart=always", rendering["changes"][0]["diff"])
        self.assertEqual(rendering["restart"], ["boring-crocodile-mcp.service", WORKER])
        result = self.apply(rendering["plan_digest"])
        self.assertEqual((result["status"], result["site_check"]), ("rendered", "regular"))
        self.assertEqual(unit.read_text(), old)
        self.assertEqual(self.calls[-3:], [["/usr/bin/systemctl", "daemon-reload"],
                                           ["/usr/bin/systemctl", "try-restart", "boring-crocodile-mcp.service", WORKER],
                                           ["/usr/bin/systemctl", "is-active", "boring-crocodile-mcp.service"]])
        backup = next((self.root / "var/site-backups").iterdir())
        self.assertEqual((backup / WORKER).read_text(), before)
        log = [json.loads(line) for line in (self.root / "var/site-log.jsonl").read_text().splitlines()]
        self.assertEqual((log[0]["kind"], log[0]["files"]), ("site_render", [str(unit)]))
        self.assertEqual(self.plan()["status"], "nothing_to_render")

    def test_guests_change_three_files_and_the_pond_accepts_them(self):
        before = self.paths.config.read_text()
        rendering = self.plan(["pond"])
        names = sorted(Path(c["path"]).name for c in rendering["changes"])
        self.assertEqual(names, sorted(["boring-crocodile.conf", "config.toml", WORKER]))
        config_diff = next(c["diff"] for c in rendering["changes"] if c["path"].endswith("config.toml"))
        self.assertEqual(config_diff, ["+service_registration_enabled = true"])  # nothing else changes
        result = self.apply(rendering["plan_digest"], ["pond"])
        self.assertEqual((result["status"], result["site_check"], result["guests"]), ("rendered", "regular", ["pond"]))
        settings = Settings.load(self.paths.config)
        self.assertTrue(settings.environments[settings.ponds["pond"].environment].service_registration_enabled)
        self.assertIn("/run/boring-crocodile/guests/pond", (self.paths.units / WORKER).read_text())
        self.assertIn(["/usr/bin/systemd-tmpfiles", "--create", str(self.paths.tmpfiles / "boring-crocodile.conf")],
                      self.calls)
        self.assertEqual(self.paths.config.read_text().replace("\nservice_registration_enabled = true", ""), before)
        with self.assertRaisesRegex(SiteConflict, "Already accepts guests: pond"):
            self.plan(["pond"])

    def test_guests_need_a_known_pond_and_the_registration_group(self):
        with self.assertRaisesRegex(SiteConflict, "Not a Pond of this site: nope"):
            self.plan(["nope"])
        with patch.object(site_render.grp, "getgrnam", side_effect=KeyError("pond-pirates")), \
                self.assertRaisesRegex(SiteConflict, "Create pond-pirates"):
            self.plan(["pond"])

    def test_other_irregular_findings_stop_it(self):
        dropin = self.paths.units / (WORKER + ".d")
        dropin.mkdir()
        (dropin / "local.conf").write_text("[Service]\nExecStartPre=/bin/true\n")
        with self.assertRaisesRegex(SiteConflict, "drop-in sets a directive"):
            self.plan()

    def test_drop_ins_are_kept(self):
        dropin = self.paths.units / (WORKER + ".d")
        dropin.mkdir()
        (dropin / "local.conf").write_text("[Service]\nMemoryMax=1G\n")
        unit = self.paths.units / WORKER
        unit.write_text(unit.read_text().replace("Restart=on-failure", "Restart=always"))
        self.apply(self.plan()["plan_digest"])
        self.assertEqual((dropin / "local.conf").read_text(), "[Service]\nMemoryMax=1G\n")

    def test_a_failure_after_writing_says_how_to_finish(self):
        """A rerun plans nothing once the files match, so the error names the backup
        and the remaining steps; the log line is written before them."""
        rendering = self.plan(["pond"])

        def runner(command, **kwargs):
            if command[0].endswith("systemd-tmpfiles"):
                raise subprocess.CalledProcessError(1, command)
            return SimpleNamespace(returncode=0, stdout="active")
        with self.assertRaisesRegex(SiteConflict, r"files are written \(old ones in .*site-backups.*\).*"
                                                  r"Finish by hand: /usr/bin/systemd-tmpfiles --create .*; "
                                                  r"/usr/bin/systemctl daemon-reload; /usr/bin/systemctl try-restart"):
            site_render.apply(rendering["plan_digest"], ["pond"], paths=self.paths, reality=self.reality,
                              runner=runner, settled=lambda policy: "healthy")
        log = [json.loads(line) for line in (self.root / "var/site-log.jsonl").read_text().splitlines()]
        self.assertEqual(log[0]["kind"], "site_render")

    def test_a_masked_or_linked_file_is_refused_not_unmasked(self):
        socket = self.paths.units / "boring-crocodile-task-pond.socket"
        socket.unlink()
        socket.symlink_to("/dev/null")
        with self.assertRaisesRegex(SiteConflict, "not a regular file owned by root"):
            self.plan()

    def test_an_existing_false_guests_line_is_replaced(self):
        config = self.paths.config
        text = config.read_text()
        config.write_text(text.replace("[environments.pond]\n", "[environments.pond]\nservice_registration_enabled = false\n", 1))
        rendering = self.plan(["pond"])
        diff = next(c["diff"] for c in rendering["changes"] if c["path"].endswith("config.toml"))
        self.assertEqual(diff, ["-service_registration_enabled = false", "+service_registration_enabled = true"])

    def test_a_stopped_site_is_rendered_without_waiting_for_health(self):
        unit = self.paths.units / WORKER
        unit.write_text(unit.read_text().replace("Restart=on-failure", "Restart=always"))
        rendering = self.plan()

        def runner(command, **kwargs):
            return SimpleNamespace(returncode=3 if "is-active" in command else 0,
                                   stdout="inactive" if "is-active" in command else "")
        result = site_render.apply(rendering["plan_digest"], paths=self.paths, reality=self.reality, runner=runner,
                                   settled=lambda policy: self.fail("waited for health on a stopped site"))
        self.assertEqual((result["status"], result["local_health"]), ("rendered", "stopped"))

    def test_a_running_controller_that_dies_on_restart_is_degraded(self):
        unit = self.paths.units / WORKER
        unit.write_text(unit.read_text().replace("Restart=on-failure", "Restart=always"))
        rendering = self.plan()
        active = True

        def runner(command, **kwargs):
            nonlocal active
            if "try-restart" in command:
                active = False
            return SimpleNamespace(returncode=0 if active else 3, stdout="active" if active else "inactive")
        result = site_render.apply(rendering["plan_digest"], paths=self.paths, reality=self.reality,
                                   runner=runner, settled=lambda _: "degraded")
        self.assertEqual(result["status"], "rendered_but_degraded")
        self.assertEqual(result["local_health"], "stopped")
        self.assertIn("site-check", result["next"])

    def test_unknown_activity_after_writing_is_degraded_with_cause(self):
        unit = self.paths.units / WORKER
        unit.write_text(unit.read_text().replace("Restart=on-failure", "Restart=always"))
        rendering = self.plan()
        changed = False

        def runner(command, **kwargs):
            nonlocal changed
            if "try-restart" in command:
                changed = True
            if "is-active" in command and changed:
                raise OSError("synthetic service manager failure")
            return SimpleNamespace(returncode=0, stdout="active")
        result = site_render.apply(rendering["plan_digest"], paths=self.paths, reality=self.reality,
                                   runner=runner, settled=lambda _: self.fail("unknown state passed to health"))
        self.assertEqual((result["status"], result["local_health"]), ("rendered_but_degraded", "unknown"))
        self.assertIn("synthetic service manager failure", result["activity_error"])

    def test_unknown_prechange_activity_stops_before_writing(self):
        unit = self.paths.units / WORKER
        unit.write_text(unit.read_text().replace("Restart=on-failure", "Restart=always"))
        rendering = self.plan()
        before = self.tree()
        with self.assertRaisesRegex(SiteConflict, "activity"):
            site_render.apply(rendering["plan_digest"], paths=self.paths, reality=self.reality,
                runner=lambda *a, **k: SimpleNamespace(returncode=1, stdout="unknown"),
                settled=lambda _: self.fail("health checked before admission"))
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.root / "var/site-backups").exists())

    def test_a_stale_digest_changes_nothing(self):
        unit = self.paths.units / WORKER
        unit.write_text(unit.read_text().replace("Restart=on-failure", "Restart=always"))
        before = self.tree()
        with self.assertRaisesRegex(SiteConflict, "changed since the reviewed plan"):
            self.apply("stale")
        self.assertEqual((self.tree(), self.calls), (before, []))


if __name__ == "__main__":
    unittest.main()
