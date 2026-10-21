import io
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from boring_crocodile import admin
from boring_worker_kit.host_limits import HostLimits


class AdminStatusTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.context = root / "context"
        self.context.mkdir()
        self.credential = root / "approval-secret"
        self.credential.write_text("not-read-by-status")
        self.worker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.task = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.worker.close)
        self.addCleanup(self.task.close)
        self.worker_path = root / "worker.sock"
        self.task_path = root / "task.sock"
        self.worker.bind(str(self.worker_path))
        self.task.bind(str(self.task_path))
        self.environment = SimpleNamespace(
            name="pond",
            display_name="The Pond",
            socket_path=self.worker_path,
            task_socket_path=self.task_path,
            default_cwd=root,
            task_unit_prefix="boring-crocodile-task-pond",
            task_cgroup_parent=Path(
                "/system.slice/system-crocodilepond.slice"),
            output_limit_bytes=65536,
        )
        self.profile = SimpleNamespace(
            profile_id="11111111-1111-4111-8111-111111111111",
            display_name="The Pond",
            environment="pond",
            approval_secret_file=self.credential,
            context_root=self.context,
        )
        self.settings = SimpleNamespace(
            ponds={"pond": self.profile},
            environments={"pond": self.environment},
        )
        self.inactive = set()
        self.policy_patch = patch.object(admin, "inspect_task_policy", return_value={
            "configured_no_new_privileges": True, "loaded_policy": "matches",
            "sudo_behavior": "not_tested"})
        self.policy_mock = self.policy_patch.start()
        self.addCleanup(self.policy_patch.stop)

    def runner(self, command, **kwargs):
        unit = command[-1]
        active = unit not in self.inactive
        return subprocess.CompletedProcess(
            command, 0,
            "LoadState=loaded\n"
            f"ActiveState={'active' if active else 'inactive'}\n"
            f"SubState={'running' if active else 'dead'}\n"
            "MaxConnections=2\n",
            "",
        )

    def site(self):
        return {"status": "regular", "irregular": [],
                "counts": {"understood": 6, "adjustments": 0, "irregular": 0, "unchecked": 0}}

    def test_healthy_report_reuses_systemd_state_and_shows_site_check(self):
        with patch.object(admin, "read_host_limits", return_value=HostLimits(2, 290)):
            report = admin.status_report(
                self.settings, runner=self.runner, site_reader=self.site)
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(report["site_check"]["status"], "regular")
        self.assertTrue(all(report["ponds"][0]["checks"].values()))
        self.assertEqual(
            report["ponds"][0]["paths"]["worker_socket"]["kind"], "socket")

    def test_degraded_report_identifies_failed_check(self):
        self.inactive.add("boring-crocodile-worker-pond.service")
        report = admin.status_report(
            self.settings, "pond", runner=self.runner,
            site_reader=self.site)
        self.assertEqual(report["status"], "degraded")
        self.assertFalse(report["ponds"][0]["checks"]["worker_active"])
        with self.assertRaisesRegex(ValueError, "Unknown Pond"):
            admin.status_report(
                self.settings, "missing", runner=self.runner,
                site_reader=self.site)

    def test_an_irregular_site_is_reported_not_counted_as_unhealthy(self):
        def irregular():
            return {"status": "irregular", "counts": {"irregular": 1},
                    "irregular": [{"what": "differs from the Crocodile's template"}]}
        with patch.object(admin, "read_host_limits", return_value=HostLimits(2, 290)):
            report = admin.status_report(self.settings, runner=self.runner, site_reader=irregular)
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(report["site_check"]["status"], "irregular")
        self.assertEqual(report["site_check"]["irregular"], ["differs from the Crocodile's template"])

    def test_an_unreadable_site_is_unknown(self):
        def broken():
            raise PermissionError(13, "Permission denied", "/etc/boring-crocodile/config.toml")
        with patch.object(admin, "read_host_limits", return_value=HostLimits(2, 290)):
            report = admin.status_report(self.settings, runner=self.runner, site_reader=broken)
        self.assertEqual(report["site_check"]["status"], "unknown")
        self.assertNotIn("setup_receipt_valid", report["ponds"][0]["checks"])

    def test_loaded_policy_mismatch_degrades_health(self):
        self.policy_mock.return_value = {"loaded_policy": "mismatch"}
        with patch.object(admin, "read_host_limits", return_value=HostLimits(2, 290)):
            report = admin.status_report(self.settings, runner=self.runner, site_reader=self.site)
        self.assertEqual(report["status"], "degraded")
        self.assertFalse(report["ponds"][0]["checks"]["task_policy_matches"])

    def test_command_disabled_policy_is_not_applicable(self):
        self.environment.command_execution_enabled = False
        self.environment.task_socket_path = None
        report = admin.status_report(self.settings, runner=self.runner, site_reader=self.site)
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(report["ponds"][0]["task_authority"]["loaded_policy"], "not_applicable")
        self.policy_mock.assert_not_called()

    def test_check_switch_returns_one_for_degraded_state(self):
        with patch.object(admin.Settings, "load", return_value=self.settings):
            with patch.object(
                    admin, "status_report",
                    return_value={"status": "degraded", "ponds": []}):
                with patch.object(
                        sys, "argv", ["admin", "status", "--check"]):
                    with patch("sys.stdout", new=io.StringIO()):
                        with self.assertRaises(SystemExit) as caught:
                            admin.main()
        self.assertEqual(caught.exception.code, 1)


if __name__ == "__main__":
    unittest.main()


class StatusTimeoutTest(unittest.TestCase):
    def test_systemctl_status_timeout_is_visible(self):
        def timed_out(command, **kwargs):
            self.assertEqual(kwargs["timeout"], 5)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        result = admin.unit_state("example.service", runner=timed_out)
        self.assertEqual(result["active"], "unknown")
        self.assertIn("timed out", result["error"])
