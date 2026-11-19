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
        registration_patch = patch("boring_crocodile.pirates.registration_path", return_value=self.worker_path)
        registration_patch.start()
        self.addCleanup(registration_patch.stop)
        group_patch = patch.object(admin.grp, "getgrnam", return_value=SimpleNamespace(gr_gid=self.worker_path.stat().st_gid))
        group_patch.start()
        self.addCleanup(group_patch.stop)
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

    def test_privileged_status_limits_and_policy_use_the_same_child_boundary(self):
        import boring_deploy

        calls = []
        def runner(command, **options):
            calls.append((command, options))
            if command[:2] == ["/usr/bin/busctl", "call"]:
                stdout = 'o "/org/freedesktop/systemd1/unit/task_2eservice"'
            elif command[:2] == ["/usr/bin/busctl", "get-property"]:
                stdout = "t 90000000"
            elif "MaxConnections" in command:
                stdout = "2"
            else:
                return self.runner(command, **options)
            return subprocess.CompletedProcess(command, 0, stdout, "")

        def policy(environment, run):
            run(["/usr/bin/systemctl", "show", "policy-probe.service"])
            return {"loaded_policy": "matches"}
        self.policy_mock.side_effect = policy
        with patch("boring_worker_kit.host_limits._run", side_effect=AssertionError("root caller bypassed")):
            report = admin.runtime_status(self.settings, runner=runner)
        self.assertEqual(report["ponds"][0]["limits"]["task_slots"], 2)
        self.assertTrue(report["ponds"][0]["checks"]["task_policy_matches"])
        for command, options in calls:
            self.assertEqual(options.get("cwd"), "/")
            self.assertEqual(options.get("env", {}).get("PATH"), boring_deploy.SYSTEM_TOOL_PATH)
            self.assertGreater(options["timeout"], 0)
        self.assertIn("policy-probe.service", [command[-1] for command, _ in calls])

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
        from boring_crocodile import site_check, site_render
        from site_host import FakeHost
        host = FakeHost(self.context / "site")
        proposal, _ = site_render.plan(paths=host.paths, host=host)
        site_render.apply(proposal["plan_digest"], paths=host.paths, host=host)
        host.paths.config.chmod(0o640)
        real_report = site_check.site_check(paths=host.paths, host=host)
        self.assertEqual(real_report["status"], "irregular")
        def irregular():
            return real_report
        with patch.object(admin, "read_host_limits", return_value=HostLimits(2, 290)):
            report = admin.status_report(self.settings, runner=self.runner, site_reader=irregular)
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(report["site_check"]["status"], "irregular")
        self.assertEqual(report["site_check"]["irregular"], [row["what"] for row in real_report["irregular"]])

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


    def test_declared_socket_bindings_and_registration_are_local_readiness(self):
        self.worker_path.chmod(0o660)
        self.task_path.chmod(0o600)
        info = self.worker_path.stat()
        identity = SimpleNamespace(uid=info.st_uid, gid=info.st_gid)
        identities = {"pond": identity}
        with patch.object(admin, "read_host_limits", return_value=HostLimits(2, 290)):
            report = admin.runtime_status(self.settings, runner=self.runner, identities=identities)
            self.assertEqual(report["status"], "healthy")
            self.assertNotIn("site_check", report)
            for socket_path in (self.worker_path, self.task_path):
                original = socket_path.stat().st_mode & 0o777
                socket_path.chmod(0o666)
                report = admin.runtime_status(self.settings, runner=self.runner, identities=identities)
                self.assertEqual(report["status"], "degraded")
                socket_path.chmod(original)
            identity.uid += 1
            self.assertEqual(admin.runtime_status(
                self.settings, runner=self.runner, identities=identities)["status"], "degraded")
            identity.uid -= 1
            with patch("boring_crocodile.pirates.registration_path", return_value=self.context / "missing"), patch.object(
                    admin.grp, "getgrnam", return_value=SimpleNamespace(gr_gid=info.st_gid)):
                report = admin.runtime_status(self.settings, runner=self.runner, identities=identities)
                self.assertFalse(report["ponds"][0]["checks"]["registration_socket_present"])
            # An open local registration socket is sufficient; no Pirate/backend
            # must already be registered to admit and start its first instance.
            with patch("boring_crocodile.pirates.registration_path", return_value=self.worker_path), patch.object(
                    admin.grp, "getgrnam", return_value=SimpleNamespace(gr_gid=info.st_gid)):
                self.assertEqual(admin.runtime_status(
                    self.settings, runner=self.runner, identities=identities)["status"], "healthy")

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


class RootAdminChildTest(unittest.TestCase):
    def test_unit_state_fixes_cwd_and_path_at_the_real_common_boundary(self):
        import boring_deploy

        def runner(command, **options):
            self.assertEqual(options.get("cwd"), "/")
            self.assertEqual(options.get("env", {}).get("PATH"), boring_deploy.SYSTEM_TOOL_PATH)
            self.assertEqual(options["timeout"], 5)
            return subprocess.CompletedProcess(command, 0, "ActiveState=active", "")
        self.assertEqual(admin.unit_state("example.service", runner=runner)["active"], "active")

    def test_oauth_identity_probe_uses_the_real_common_boundary(self):
        import boring_deploy

        def runner(command, **options):
            self.assertEqual(command[0], "/usr/bin/systemctl")
            self.assertEqual(options.get("cwd"), "/")
            self.assertEqual(options.get("env", {}).get("PATH"), boring_deploy.SYSTEM_TOOL_PATH)
            self.assertEqual(options["timeout"], 5)
            return subprocess.CompletedProcess(command, 0, "example-control", "")
        settings = SimpleNamespace(oauth_database_file=Path("/not-opened"))
        with patch.object(admin.subprocess, "run", side_effect=runner), \
                patch.object(admin.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=1234)), \
                patch("boring_crocodile.oauth_admin.OAuthAdmin") as store:
            self.assertIs(admin.oauth_store(settings), store.return_value)
        store.assert_called_once_with(settings.oauth_database_file, 1234)


class StatusTimeoutTest(unittest.TestCase):
    def test_systemctl_status_timeout_is_visible(self):
        def timed_out(command, **kwargs):
            self.assertEqual(kwargs["timeout"], 5)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        result = admin.unit_state("example.service", runner=timed_out)
        self.assertEqual(result["active"], "unknown")
        self.assertIn("timed out", result["error"])
