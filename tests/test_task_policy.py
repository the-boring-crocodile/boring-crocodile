import tempfile
import os
import stat
from unittest.mock import patch
import unittest
from pathlib import Path
from types import SimpleNamespace
from boring_crocodile.settings import EnvironmentSettings, Settings
from boring_crocodile.task_policy import task_directives, task_dropin
from boring_crocodile import site_layout
from boring_crocodile.task_policy_admin import task_policy_plan, apply_task_policy
from boring_crocodile.task_policy import inspect_task_policy, expected_properties
from boring_crocodile.admin import protected_task_config
from boring_crocodile.setup_services import ServiceInputs, render
try:
    from test_setup_services import inputs, receipt, policy
except ModuleNotFoundError:
    from tests.test_setup_services import inputs, receipt, policy


class TaskPolicyTest(unittest.TestCase):
    def test_restricted_and_admin_capable_are_complete_policies(self):
        restricted=task_directives(True,'/home/alice')
        capable=task_directives(False,'/home/alice')
        for key in ('NoNewPrivileges=', 'CapabilityBoundingSet=', 'ProtectSystem=',
                    'ProtectHome=', 'RestrictSUIDSGID=', 'LockPersonality=',
                    'ProtectControlGroups=', 'PrivateTmp=', 'ReadWritePaths='):
            self.assertIn(key, restricted)
            self.assertIn(key, capable)
        self.assertIn('NoNewPrivileges=yes',restricted)
        self.assertIn('NoNewPrivileges=no',capable)
        self.assertIn('CapabilityBoundingSet=~',capable)
        self.assertIn('RestrictSUIDSGID=no',capable)
        self.assertIn('ProtectSystem=no',capable)
        self.assertNotIn('ReadWritePaths=/home/alice',capable)
        self.assertNotIn('NoNewPrivileges=no',restricted)
        # Ordinary Ponds do not get RestrictSUIDSGID: it makes openat2 fail
        # (ENOSYS), which breaks tar on Ubuntu 26.04.
        self.assertIn('RestrictSUIDSGID=no',restricted)
        self.assertNotIn('RestrictSUIDSGID=yes',restricted)

    def test_fresh_site_policy_is_explicit_and_strictly_typed(self):
        normal=render(inputs(),ServiceInputs.parse(policy()),receipt())
        self.assertIn('no_new_privileges = true',normal['config.toml'])
        self.assertIn('NoNewPrivileges=yes',normal['boring-crocodile-task-pond@.service'])
        enabled=render(inputs(),ServiceInputs.parse(policy()|{'no_new_privileges':False}),receipt())
        self.assertIn('no_new_privileges = false',enabled['config.toml'])
        self.assertIn('NoNewPrivileges=no',enabled['boring-crocodile-task-pond@.service'])
        self.assertIn('CapabilityBoundingSet=~',enabled['boring-crocodile-task-pond@.service'])
        self.assertIn('NoNewPrivileges=yes',enabled['boring-crocodile-worker-pond.service'])
        self.assertIn('NoNewPrivileges=yes',enabled['boring-crocodile-mcp.service'])
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'config.toml';p.write_text(enabled['config.toml'])
            self.assertFalse(Settings.load(p).environments['pond'].no_new_privileges)
        for invalid in (0,'false',None):
            with self.assertRaises(ValueError): ServiceInputs.parse(policy()|{'no_new_privileges':invalid})
            with self.assertRaises(ValueError): EnvironmentSettings.from_mapping('pond',{
                'socket_path':'/tmp/worker.sock','default_cwd':'/tmp',
                'allowed_roots':['/tmp'],'control_plane_uid':42,
                'no_new_privileges':invalid})

    def test_policy_config_must_be_root_owned(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'config.toml';path.write_text('')
            if __import__('os').getuid() != 0:
                with self.assertRaisesRegex(ValueError,'root-owned'):
                    protected_task_config(path)



class TaskPolicyApplyTest(unittest.TestCase):
    """Real temporary files; only root ownership and systemd responses simulated."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.template = self.root / "original.service"
        self.template.write_text("[Service]\nNoNewPrivileges=yes\n")
        self.env = SimpleNamespace(name="pond", task_unit_prefix="boring-crocodile-task-pond",
            command_execution_enabled=True, task_socket_path=Path("/tmp/task.sock"),
            default_cwd=Path("/home/alice"), no_new_privileges=False)
        self.settings = SimpleNamespace(ponds={"pond": SimpleNamespace(environment="pond")},
                                        environments={"pond": self.env})
        self.path = self.root / "boring-crocodile-task-pond@.service.d/50-pond-task-policy.conf"
        self.active = "inactive"
        self.units = ""
        self.extra = []
        self.loaded_mode = True
        self.reloads = 0
        self.force_mismatch = False
        self.fail_reload_once = False
        self.omit = None
        self.compose_paths = False
        self.capmask = (1 << (int(Path("/proc/sys/kernel/cap_last_cap").read_text()) + 1)) - 1
        real_lstat = Path.lstat
        root = self.root
        def fixture_ownership(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            if path == root or root in path.parents:
                fields = list(info)
                fields[4] = 0
                return os.stat_result(fields)
            return info
        patcher = patch.object(Path, "lstat", fixture_ownership)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_systemd(self, args):
        if args[0] == "/usr/bin/busctl":
            return 'o "/org/freedesktop/systemd1/unit/probe"' if args[1] == "call" else (
                "t " + str(0 if self.loaded_mode else self.capmask))
        if args[1] == "daemon-reload":
            self.reloads += 1
            if self.fail_reload_once and self.reloads == 1:
                raise ValueError("synthetic reload failure")
            self.loaded_mode = not self.path.exists() or "NoNewPrivileges=yes" in self.path.read_text()
            if self.force_mismatch and self.reloads == 1:
                self.loaded_mode = True
            return ""
        if args[1] == "list-units":
            return self.units
        if "-p" in args:
            name = args[args.index("-p") + 1]
            paths = self.extra + ([str(self.path)] if self.path.exists() else [])
            return name + "=" + {"ActiveState": self.active,
                "FragmentPath": str(self.template), "DropInPaths": " ".join(paths)}[name]
        values = expected_properties(self.loaded_mode, "/home/alice")
        if self.compose_paths:
            paths = []
            for content in (self.template.read_text(), self.path.read_text() if self.path.exists() else ""):
                for line in content.splitlines():
                    if line.startswith("ReadWritePaths="):
                        value = line.split("=", 1)[1]
                        if not value:
                            paths.clear()
                        else:
                            paths.extend(value.split())
            values["ReadWritePaths"] = " ".join(paths)
        values.update({"PrivateDevices":"no", "ProtectKernelTunables":"no",
            "ProtectKernelModules":"no", "ProtectKernelLogs":"no", "RestrictRealtime":"no",
            "MemoryDenyWriteExecute":"no", "RestrictNamespaces":"no",
            "SystemCallFilter":"~", "RestrictAddressFamilies":"~"})
        return "\n".join(key+"="+value for key,value in values.items() if key != self.omit)

    def plan(self):
        return task_policy_plan(self.settings, "pond", unit_root=self.root, run=self.run_systemd)[0]

    def apply(self, digest=None):
        digest = digest or self.plan()["plan_digest"]
        # The site lock, in the test's folder (the host's is root's /etc/boring-crocodile).
        lock = lambda: site_layout.site_lock(self.root, owner=os.getuid())
        with patch("boring_crocodile.task_policy_admin.os.geteuid", return_value=0), \
                patch("boring_crocodile.task_policy_admin.site_lock", lock):
            return apply_task_policy(self.settings, "pond", digest, unit_root=self.root, run=self.run_systemd)

    def test_restricted_apply_composes_with_existing_template_paths(self):
        # systemd.exec: repeated path assignments append; empty assignment resets.
        # Independent fixture parsing, not expected_properties as the oracle.
        self.compose_paths = True
        self.env.no_new_privileges = True
        self.template.write_text("[Service]\nReadWritePaths=/home/alice\n")
        self.assertEqual(self.apply()["effective_policy"]["loaded_policy"], "matches")
        self.assertEqual(self.apply()["effective_policy"]["properties"]["ReadWritePaths"],
                         "/home/alice")
        self.env.no_new_privileges = False
        self.assertEqual(self.apply()["effective_policy"]["properties"]["ReadWritePaths"], "")
        self.env.no_new_privileges = True
        self.assertEqual(self.apply()["effective_policy"]["properties"]["ReadWritePaths"],
                         "/home/alice")

    def test_conflicting_root_owned_dropin_is_rejected_for_its_directive(self):
        extra = self.root / "foreign.conf"
        extra.write_text("[Service]\nNoNewPrivileges=no\n")
        self.extra = [str(extra)]
        with self.assertRaisesRegex(ValueError, "conflicting task policy drop-in"):
            self.plan()

    def test_unrelated_root_dropin_survives_success_and_reverse(self):
        extra = self.root / "foreign.conf"
        extra.write_text("[Service]\nSupplementaryGroups=engineering\nUMask=0027\n")
        before = extra.read_bytes()
        self.extra = [str(extra)]
        result = self.apply()
        self.assertEqual(result["effective_policy"]["loaded_policy"], "matches")
        self.assertFalse(self.loaded_mode)
        self.assertEqual(extra.read_bytes(), before)
        self.env.no_new_privileges = True
        self.assertEqual(self.apply()["effective_policy"]["loaded_policy"], "matches")
        self.assertTrue(self.loaded_mode)
        self.assertEqual(extra.read_bytes(), before)

    def test_mismatch_restores_previous_bytes_mode_and_loaded_policy(self):
        self.path.parent.mkdir()
        old = task_dropin(True, "/home/alice").encode()
        self.path.write_bytes(old)
        self.path.chmod(0o640)
        self.force_mismatch = True
        with self.assertRaisesRegex(ValueError, "does not match") as raised:
            self.apply()
        self.assertIn("mismatches", str(raised.exception))
        self.assertIn("expected", str(raised.exception))
        self.assertIn("loaded", str(raised.exception))
        self.assertEqual(self.path.read_bytes(), old)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        self.assertTrue(self.loaded_mode)
        self.assertEqual(self.reloads, 2)

    def test_reload_failure_removes_new_dropin_and_restores_loaded_policy(self):
        self.fail_reload_once = True
        with self.assertRaisesRegex(ValueError, "synthetic reload failure"):
            self.apply()
        self.assertFalse(self.path.exists())
        self.assertTrue(self.loaded_mode)
        self.assertEqual(self.reloads, 2)

    def test_active_worker_and_tasks_refuse_before_mutation(self):
        self.active = "active"
        with self.assertRaisesRegex(ValueError, "stop the selected worker"):
            self.apply()
        self.active = "inactive"
        self.units = "boring-crocodile-task-pond@1.service loaded active running"
        with self.assertRaisesRegex(ValueError, "tasks still active"):
            self.apply()
        self.assertFalse(self.path.exists())
        self.assertEqual(self.reloads, 0)

    def test_template_change_invalidates_reviewed_plan(self):
        digest = self.plan()["plan_digest"]
        self.template.write_text("[Service]\nNoNewPrivileges=no\n")
        with self.assertRaisesRegex(ValueError, "plan changed"):
            self.apply(digest)
        self.assertFalse(self.path.exists())

    def test_missing_effective_property_cannot_report_match(self):
        self.loaded_mode = False
        self.omit = "NoNewPrivileges"
        report = inspect_task_policy(self.env, self.run_systemd)
        self.assertEqual(report["loaded_policy"], "unknown")
        self.assertIn("NoNewPrivileges", report["missing_properties"])

    def test_writable_dropin_is_refused(self):
        extra = self.root / "unsafe.conf"
        extra.write_text("[Service]\nSupplementaryGroups=engineering\n")
        extra.chmod(0o666)
        self.extra = [str(extra)]
        with self.assertRaisesRegex(ValueError, "unsafe task policy path"):
            self.plan()


class SiteExtensionTest(unittest.TestCase):
    """A site drop-in that adds a writable path is reported, not a mismatch."""

    def inspect(self, read_write_paths, mode=True):
        env = SimpleNamespace(name="pond", task_unit_prefix="boring-crocodile-task-pond",
                              no_new_privileges=mode, default_cwd=Path("/home/alice"))
        maximum = int(Path("/proc/sys/kernel/cap_last_cap").read_text())

        def run(args):
            if args[0] == "/usr/bin/busctl":
                return ('o "/org/freedesktop/systemd1/unit/probe"' if args[1] == "call"
                        else "t " + str(0 if mode else (1 << (maximum + 1)) - 1))
            values = expected_properties(mode, "/home/alice")
            values["ReadWritePaths"] = read_write_paths
            return "\n".join(key + "=" + value for key, value in values.items())
        return inspect_task_policy(env, run)

    def test_an_added_path_is_an_extension(self):
        result = self.inspect("/home/alice /srv/shared")
        self.assertEqual(result["loaded_policy"], "matches")
        self.assertEqual(result["extended"], {"ReadWritePaths": ["/srv/shared"]})

    def test_a_missing_home_is_still_a_mismatch(self):
        result = self.inspect("/srv/shared")
        self.assertEqual(result["loaded_policy"], "mismatch")
        self.assertEqual(result["extended"], {})


if __name__ == "__main__":
    unittest.main()
