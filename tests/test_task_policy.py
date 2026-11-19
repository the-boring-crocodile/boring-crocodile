import unittest
from pathlib import Path
from types import SimpleNamespace
from boring_crocodile.settings import EnvironmentSettings
from boring_crocodile.task_policy import task_directives
from boring_crocodile.task_policy import inspect_task_policy, expected_properties
from boring_crocodile.site_policy import ServiceInputs
from boring_crocodile.site_templates import render_description
try:
    from test_setup_services import description, policy
except ModuleNotFoundError:
    from tests.test_setup_services import description, policy


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
        normal_site = description()
        normal=render_description(normal_site)
        self.assertTrue(normal_site.runtime_settings().environments["pond"].no_new_privileges)
        self.assertIn('NoNewPrivileges=yes',normal['boring-crocodile-task-pond@.service'])
        enabled_site = description(policy()|{'no_new_privileges':False})
        enabled=render_description(enabled_site)
        self.assertFalse(enabled_site.runtime_settings().environments["pond"].no_new_privileges)
        self.assertIn('NoNewPrivileges=no',enabled['boring-crocodile-task-pond@.service'])
        self.assertIn('CapabilityBoundingSet=~',enabled['boring-crocodile-task-pond@.service'])
        self.assertIn('NoNewPrivileges=yes',enabled['boring-crocodile-worker-pond.service'])
        self.assertIn('NoNewPrivileges=yes',enabled['boring-crocodile-mcp.service'])
        for invalid in (0,'false',None):
            with self.assertRaises(ValueError): ServiceInputs.parse(policy()|{'no_new_privileges':invalid})
            with self.assertRaises(ValueError): EnvironmentSettings.from_mapping('pond',{
                'socket_path':'/tmp/worker.sock','default_cwd':'/tmp',
                'allowed_roots':['/tmp'],'control_plane_uid':42,
                'no_new_privileges':invalid})

class SiteExtensionTest(unittest.TestCase):
    """A site drop-in that adds a writable path is reported, not a mismatch."""

    def inspect(self, read_write_paths, mode=True, omit=None):
        env = SimpleNamespace(name="pond", task_unit_prefix="boring-crocodile-task-pond",
                              no_new_privileges=mode, default_cwd=Path("/home/alice"))
        maximum = int(Path("/proc/sys/kernel/cap_last_cap").read_text())

        def run(args):
            if args[0] == "/usr/bin/busctl":
                return ('o "/org/freedesktop/systemd1/unit/probe"' if args[1] == "call"
                        else "t " + str(0 if mode else (1 << (maximum + 1)) - 1))
            values = expected_properties(mode, "/home/alice")
            values["ReadWritePaths"] = read_write_paths
            return "\n".join(key + "=" + value for key, value in values.items() if key != omit)
        return inspect_task_policy(env, run)

    def test_missing_effective_property_cannot_report_match(self):
        result = self.inspect("/home/alice", omit="NoNewPrivileges")
        self.assertEqual(result["loaded_policy"], "unknown")
        self.assertIn("NoNewPrivileges", result["missing_properties"])

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
