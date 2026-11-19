"""The observer reuses declared rendering and never infers configuration from units."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from boring_crocodile import site_check, site_render
from site_host import FakeHost


class SiteCheckTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.host = FakeHost(Path(self.directory.name))
        p, _ = site_render.plan(paths=self.host.paths, host=self.host)
        site_render.apply(p["plan_digest"], paths=self.host.paths, host=self.host)

    def check(self):
        return site_check.site_check(paths=self.host.paths, host=self.host)

    def test_matching_changed_and_missing_units_are_observed_without_writing(self):
        self.assertEqual(self.check()["status"], "regular")
        unit = self.host.paths.units / "boring-crocodile-worker-pond.service"
        unit.write_text(unit.read_text()+"# changed\n")
        self.assertEqual(self.check()["status"], "irregular")
        self.assertTrue(unit.read_text().endswith("# changed\n"))
        unit.unlink()
        self.assertEqual(self.check()["status"], "irregular")

    def test_description_mode_and_group_drift_are_reported_and_repaired(self):
        path = self.host.paths.config
        path.chmod(0o640)
        self.host.group_owned[str(path)] = 43210
        report = self.check()
        rows = [row for row in report["irregular"] if row["what"] == "description permissions differ"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], str(path))
        self.assertEqual(rows[0]["actual"]["gid"], 43210)
        self.assertEqual(rows[0]["actual"]["mode"], 0o640)
        self.assertEqual(rows[0]["expected"]["gid"], 0)
        self.assertEqual(rows[0]["expected"]["mode"], 0o644)
        proposal, _ = site_render.plan(paths=self.host.paths, host=self.host)
        site_render.apply(proposal["plan_digest"], paths=self.host.paths, host=self.host)
        self.assertEqual(self.check()["status"], "regular")


    def test_approval_secret_permission_drift_is_named_and_repaired_without_rotation(self):
        import os
        secret = self.host.path(self.host.site.ponds["pond"].credential_file)
        before = secret.read_bytes()
        real_lstat = Path.lstat
        def lstat(path):
            info = real_lstat(path)
            if path == secret:
                fields = list(info)
                fields[4] = self.host.owned.get(str(path), info.st_uid)
                return os.stat_result(fields)
            return info
        with patch.object(Path, "lstat", lstat):
            for mode, uid in ((0o644, self.host.owner), (0o640, self.host.owner),
                              (0o600, self.host.site.ponds["pond"].identity.uid)):
                with self.subTest(mode=mode, uid=uid):
                    secret.chmod(mode)
                    self.host.owned[str(secret)] = uid
                    report = self.check()
                    rows = [r for r in report["irregular"] if r.get("path") == str(secret)]
                    self.assertEqual(len(rows), 1, report)
                    self.assertEqual(rows[0]["actual"]["mode"], mode)
                    self.assertEqual(rows[0]["actual"]["uid"], uid)
                    self.assertEqual(rows[0]["expected"]["mode"], 0o600)
                    self.assertEqual(rows[0]["expected"]["uid"], self.host.owner)
                    proposal, _ = site_render.plan(paths=self.host.paths, host=self.host)
                    site_render.apply(proposal["plan_digest"], paths=self.host.paths, host=self.host)
                    self.assertEqual(self.check()["status"], "regular")
                    self.assertEqual(secret.read_bytes(), before)
                    self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(self.host.description_metadata(secret)["uid"], self.host.owner)


    def test_declared_external_approval_secret_uses_its_actual_path(self):
        old = self.host.path(self.host.site.ponds["pond"].credential_file)
        secret = self.host.path("/srv/approval/pond-secret")
        secret.parent.mkdir(parents=True)
        old.rename(secret)
        before = secret.read_bytes()
        self.host.ponds["pond"]["credential_file"] = "/srv/approval/pond-secret"
        self.host.save()
        secret.chmod(0o644)
        rows = [r for r in self.check()["irregular"] if r.get("path") == str(secret)]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actual"]["mode"], 0o644)
        self.assertEqual(rows[0]["expected"]["mode"], 0o600)
        proposal, _ = site_render.plan(paths=self.host.paths, host=self.host)
        site_render.apply(proposal["plan_digest"], paths=self.host.paths, host=self.host)
        self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
        self.assertEqual(secret.read_bytes(), before)
        self.assertEqual(self.check()["status"], "regular")

    def test_only_a_missing_approval_secret_needs_explicit_provisioning(self):
        secret = self.host.path(self.host.site.ponds["pond"].credential_file)
        secret.unlink()
        report = self.check()
        self.assertEqual(report["status"], "irregular")
        self.assertIn("Approval secret needs explicit provisioning: " + str(secret),
                      report["irregular"][0]["detail"])
        self.assertFalse(secret.exists())

    def test_task_override_is_warned_and_effective_mismatch_is_irregular(self):
        dropin = self.host.paths.units / "boring-crocodile-task-pond@.service.d/override.conf"
        dropin.parent.mkdir()
        dropin.write_text("[Service]\nNoNewPrivileges=no\n")
        self.host.task_policy_values = {"NoNewPrivileges": "no"}
        report = self.check()
        self.assertTrue(any(row.get("directive") == "NoNewPrivileges" and row.get("warning")
                            for row in report["adjustments"]))
        rows = [row for row in report["irregular"] if row["what"] == "task policy differs"]
        self.assertEqual(len(rows), 1, report)
        self.assertEqual(rows[0]["pond"], "pond")
        self.assertEqual(rows[0]["policy"]["mismatches"]["NoNewPrivileges"],
                         {"expected": "yes", "loaded": "no"})
        self.assertEqual(report["status"], "irregular")

    def test_dropins_groups_and_unreadable_metadata_are_explicit(self):
        dropin = self.host.paths.units / "boring-crocodile-worker-pond.service.d/local.conf"
        dropin.parent.mkdir()
        dropin.write_text("[Service]\nUMask=0077\n")
        self.assertTrue(self.check()["adjustments"])
        dropin.write_text("[Service]\nExecStart=/bin/false\n")
        report = self.check()
        self.assertTrue(any(row.get("directive") == "ExecStart" and row.get("warning")
                            for row in report["adjustments"]))
        dropin.unlink()
        self.host.role_members["pond-pirates"].clear()
        self.assertEqual(self.check()["status"], "irregular")
        with patch.object(self.host, "prerequisites", side_effect=PermissionError("private")):
            self.assertEqual(self.check()["status"], "incomplete")
