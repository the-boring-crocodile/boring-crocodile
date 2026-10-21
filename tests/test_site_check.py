import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from boring_crocodile.site_check import Reality, site_check
from boring_crocodile.task_policy import task_dropin

REFERENCE = Path(__file__).resolve().parent.parent / "site/default/two-ponds"
UIDS = {"boring-crocodile": 901, "boring-crocodile-mcp": 902, "boring-crocodile-second": 903}
HOMES = {"/home/boring-crocodile": "boring-crocodile", "/home/boring-crocodile-second": "boring-crocodile-second"}


class FakeReality(Reality):
    def __init__(self, homes=HOMES):
        self.homes = dict(homes)

    def owner(self, path):
        return self.homes[str(path)]

    def user(self, name):
        return {"uid": UIDS[name]}

    def uid_name(self, uid):
        return {value: key for key, value in UIDS.items()}[uid]

    def is_root(self):
        return False


class SiteCheckTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        shutil.copytree(REFERENCE / "etc", self.root / "etc")
        self.units = self.root / "etc/systemd/system"
        self.tmpfiles = self.root / "etc/tmpfiles.d"

    def tearDown(self):
        self.directory.cleanup()

    def check(self, reality=None):
        return site_check(self.root / "etc/boring-crocodile/config.toml", units=self.units,
                          tmpfiles=self.tmpfiles, credentials=self.root / "credentials",
                          reality=reality or FakeReality())

    def whats(self, report):
        return [item["what"] for item in report["irregular"]]

    def test_the_reference_site_is_regular(self):
        report = self.check()
        self.assertEqual(report["status"], "regular", report["irregular"])
        self.assertEqual(report["counts"]["understood"], 13)  # config + 12 unit/tmpfiles files
        self.assertEqual(report["counts"]["adjustments"], 0)
        self.assertEqual({item["why"] for item in report["unchecked"]}, {"needs root"})

    def test_a_hand_edited_unit_is_irregular_with_its_diff(self):
        path = self.units / "boring-crocodile-worker-second.service"
        path.write_text(path.read_text().replace("Restart=on-failure", "Restart=always"))
        report = self.check()
        self.assertEqual(self.whats(report), ["differs from the Crocodile's template"])
        self.assertIn("+Restart=always", report["irregular"][0]["diff"])

    def test_known_drop_in_lines_are_adjustments_and_unknown_ones_irregular(self):
        dropins = self.units / "boring-crocodile-task-pond@.service.d"
        dropins.mkdir()
        (dropins / "60-shared.conf").write_text("[Service]\nReadWritePaths=/srv/shared\nUMask=0027\n")
        (dropins / "70-odd.conf").write_text("[Service]\nExecStart=\nExecStart=/bin/sh\n")
        report = self.check()
        self.assertEqual([(a["directive"], a["value"]) for a in report["adjustments"]],
                         [("ReadWritePaths", "/srv/shared"), ("UMask", "0027")])
        odd = [item for item in report["irregular"] if item.get("directive") == "ExecStart"]
        self.assertEqual(len(odd), 2)

    # -- the task-policy drop-in: its name selects the check, never passes it --
    POLICY = "boring-crocodile-task-pond@.service.d/50-pond-task-policy.conf"

    def policy(self, text=None, where=POLICY):
        """Write a task-policy drop-in (by default exactly task-policy's for pond) and
        let the fixture's files count as root's, as on a host."""
        path = self.units / where
        path.parent.mkdir(exist_ok=True)
        path.write_text(task_dropin(True, "/home/boring-crocodile") if text is None else text)
        real_lstat, root = Path.lstat, self.root

        def as_root(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            if root in path.parents:
                fields = list(info)
                fields[4] = 0
                return os.stat_result(fields)
            return info
        patcher = patch.object(Path, "lstat", as_root)
        patcher.start()
        self.addCleanup(patcher.stop)
        return path

    def test_the_exact_task_policy_drop_in_is_understood(self):
        path = self.policy()
        report = self.check()
        self.assertEqual(report["status"], "regular", report["irregular"])
        self.assertIn(str(path), [u["path"] for u in report["understood"]])

    def test_a_changed_task_policy_drop_in_is_irregular(self):
        exact = task_dropin(True, "/home/boring-crocodile")
        for changed in (exact + "User=root\n", exact + "ExecStart=\nExecStart=/bin/sh\n",
                        exact.replace("NoNewPrivileges=yes", "NoNewPrivileges=no")):
            with self.subTest(changed=changed[-30:]):
                self.policy(changed)
                report = self.check()
                self.assertEqual(self.whats(report), ["task-policy drop-in differs from what task-policy writes"])
                self.assertNotIn(str(self.units / self.POLICY), [u["path"] for u in report["understood"]])

    def test_a_task_policy_drop_in_on_another_unit_is_irregular(self):
        self.policy(where="boring-crocodile-worker-pond.service.d/50-pond-task-policy.conf")
        self.assertEqual(self.whats(self.check()), ["task-policy drop-in on a unit task-policy does not manage"])
        shutil.rmtree(self.units / "boring-crocodile-worker-pond.service.d")
        self.policy(where="boring-crocodile-task-second@.service.d/50-pond-task-policy.conf")  # pond's, not second's
        self.assertEqual(self.whats(self.check()), ["task-policy drop-in differs from what task-policy writes"])

    def test_a_linked_unreadable_or_foreign_task_policy_drop_in_is_irregular(self):
        path = self.policy()
        target = self.root / "elsewhere.conf"
        path.rename(target)
        path.symlink_to(target)
        self.assertEqual(self.whats(self.check()), ["task-policy drop-in is not a trusted root file"])
        path.unlink()
        target.rename(path)
        path.chmod(0)
        try:
            if os.geteuid() != 0:
                self.assertEqual(self.whats(self.check()), ["task-policy drop-in is not a trusted root file"])
        finally:
            path.chmod(0o644)
        path.chmod(0o664)  # group-writable: not trusted
        self.assertEqual(self.whats(self.check()), ["task-policy drop-in is not a trusted root file"])
        path.chmod(0o644)
        patch.stopall()  # the fixture's real owner again: not root
        self.assertEqual(self.whats(self.check()), ["task-policy drop-in is not a trusted root file"])

    def test_a_file_that_belongs_to_no_pond_is_irregular(self):
        (self.units / "boring-crocodile-worker-ghost.service").write_text("[Service]\n")
        report = self.check()
        self.assertEqual(self.whats(report), ["Crocodile file that belongs to no Pond of config.toml"])

    def test_a_missing_unit(self):
        (self.units / "boring-crocodile-task-second.socket").unlink()
        self.assertEqual(self.whats(self.check()), ["file missing"])

    def test_a_home_owned_by_another_account(self):
        (self.units / "boring-crocodile-task-second.socket").unlink()
        report = self.check(FakeReality(HOMES | {"/home/boring-crocodile-second": "boring-crocodile"}))
        self.assertIn("worker account is not the home's owner", self.whats(report))
        self.assertIn("file missing", self.whats(report))  # noticed although the Pond cannot be rendered

    def test_a_config_the_controller_rejects_stops_the_check(self):
        config = self.root / "etc/boring-crocodile/config.toml"
        config.write_text(config.read_text() + "\n[surprise]\nkey = 1\n")
        report = self.check()
        self.assertEqual(self.whats(report), ["the controller does not accept config.toml"])

    def test_a_task_limit_above_the_pond_budget_is_reported(self):
        path = self.units / "boring-crocodile-task-second@.service"
        path.write_text(path.read_text().replace("TasksMax=128", "TasksMax=512"))
        report = self.check()
        self.assertIn("a command may use more tasks than its Pond's budget", self.whats(report))


if __name__ == "__main__":
    unittest.main()
