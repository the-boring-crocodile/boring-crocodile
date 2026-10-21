import copy
import tempfile
import unittest
from pathlib import Path

from boring_crocodile.settings import Settings, EnvironmentSettings, tomllib
from unittest.mock import patch

from pond_config import one_pond_toml


class SettingsTest(unittest.TestCase):
    def config(self, directory: str, environment: str = "") -> Path:
        config = Path(directory) / "config.toml"
        config.write_text(one_pond_toml(Path(directory), environment))
        return config

    def test_loads_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings.load(self.config(directory))
            self.assertEqual(settings.environments["pond"].default_cwd, Path(directory))
            self.assertEqual(settings.ponds["pond"].context_root, Path(directory))

    def test_refuses_the_single_pond_layout_by_key(self) -> None:
        # Each stale key names itself; a config without Ponds says so.
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            base = config.read_text()
            for stale, message in (
                    ('[execution]\ndefault_environment = "pond"\n', r"delete the \[execution\] section \(default_environment\)"),
                    ('[context]\nroot = "/srv"\n', r"delete the \[context\] section"),
                    ('[auth]\n', r"delete approval_secret_file from \[auth\]")):
                text = (base.replace("[auth]\n", '[auth]\napproval_secret_file = "/srv/secret"\n')
                        if stale == "[auth]\n" else stale + base)
                config.write_text(text)
                with self.subTest(stale=stale), self.assertRaisesRegex(ValueError, message):
                    Settings.load(config)
            config.write_text(base[:base.index("[ponds.pond]")] + base[base.index("[environments.pond]"):])
            with self.assertRaisesRegex(ValueError, r"at least one \[ponds.<name>\]"):
                Settings.load(config)

    def test_refuses_a_pirates_section_and_loads_the_command_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory, """command_execution_enabled = false
[environments.pond.pirates.parrot]
display_name = "Parrot"
socket_path = "/srv/parrot.sock"
""")
            # Pirates register themselves now; a leftover section is refused
            # at startup and tells the admin what to delete.
            with self.assertRaisesRegex(ValueError,
                                        r"delete the \[environments\.pond\.pirates\.\*\] sections"):
                Settings.load(config)
            config.write_text(config.read_text().split("[environments.pond.pirates.parrot]")[0])
            environment = Settings.load(config).environments["pond"]
            self.assertFalse(environment.command_execution_enabled)
            self.assertFalse(hasattr(environment, "pirates"))

    def test_files_toml_admission_with_commands_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings.load(self.config(directory, f"""command_execution_enabled = false
[environments.pond.files.roots.project]
path = "{root}/project"
read = true
write = true
deny = ["private"]
[environments.pond.files.limits]
max_file_bytes = 1024
"""))
            env = settings.environments["pond"]
            self.assertFalse(env.command_execution_enabled)
            self.assertEqual(env.files.roots["project"].path, root / "project")
            self.assertEqual(env.files.limits["max_file_bytes"], 1024)
            from boring_crocodile.worker import WorkerServer
            worker = WorkerServer(env)
            self.assertIsNone(worker.executor)
            self.assertTrue(worker._capabilities()["files"]["configured"])
            worker.files.pool.shutdown(wait=True)

    def test_rejects_obsolete_limit_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            base = config.read_text()
            for field in ("max_parallel_tasks", "default_timeout_seconds", "max_timeout_seconds"):
                config.write_text(base + f"{field} = 33\n")
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                    Settings.load(config)

    def test_explicit_context_configuration(self):
        # The fresh-install reference, with hand-chosen files and a project.
        reference = Path("site/default/etc/boring-crocodile/config.toml").read_text()
        source = reference.replace('files = ["HANDBOOK.md", "WELCOME.md"]',
                                   'files = ["CONTRACT.md", "BOUNDARIES.md"]', 1)
        source += ('[ponds.pond.projects.example-project]\n'
                   'files = ["example-project/README.md", "example-project/STATUS.md"]\n')
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text(source)
            pond = Settings.load(config).ponds["pond"]
            self.assertEqual(pond.files[0], "CONTRACT.md")
            self.assertEqual(pond.projects["example-project"],
                             ("example-project/README.md", "example-project/STATUS.md"))
            for invalid in ('files = []', 'files = ["../escape"]', 'files = ["a", "a"]', 'files = "a"'):
                start = source.index("files = [")
                end = source.index("]", start) + 1
                config.write_text(source[:start] + invalid + source[end:])
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    Settings.load(config)

    def test_rejects_wrong_container_scalar_and_policy_key_types(self):
        base = dict(allowed_roots=["/intended"], socket_path="/tmp/worker.sock",
                    default_cwd="/intended", control_plane_uid=1000)
        for key, values in {
            "allowed_roots": ("/intended", [], {}, [1], [""]),
            "control_plane_uid": (False, "1000", 1000.9, -1, 2**32 - 1),
            "output_limit_bytes": (True, "1", 1.5, 0),
            "command_execution_enable": (False,),
            "pirates": ([], "parrot"),
            "socket_path": (False, 2),
            "default_cwd": (False, 2, ""),
            "task_unit_prefix": (123,),
        }.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    EnvironmentSettings.from_mapping("test", dict(base, **{key: value}))
        # Relative cwd/root normalization is existing, intentional compatibility.
        valid = EnvironmentSettings.from_mapping("test", dict(base, allowed_roots=["."], default_cwd="."))
        self.assertEqual(valid.allowed_roots, (Path.cwd(),))
        self.assertNotIn(Path("/"), valid.allowed_roots)

    def test_config_sections_reject_unknown_keys_and_coercion(self):
        path = Path("site/default/two-ponds/etc/boring-crocodile/config.toml")
        base = tomllib.loads(path.read_text())
        changes = (
            ("server", "bind_port", True), ("server", "bind_port", 8503.5),
            ("server", "bind_port", 65536), ("server", "bind_port", 0),
            ("server", "allowed_hosts", "example.test"),
            ("server", "allowed_hosts", [True]), ("server", "bind_hots", "bad"),
            ("auth", "oauth_database_file", False),
        )
        for section, key, value in changes:
            data = copy.deepcopy(base)
            data[section][key] = value
            with self.subTest(section=section, key=key, value=value):
                with patch("boring_crocodile.settings.tomllib.load", return_value=data):
                    with self.assertRaises(ValueError):
                        Settings.load(path)
        for section in ("environments", "ponds"):
            with self.subTest(section=section):
                with patch("boring_crocodile.settings.tomllib.load", return_value=dict(base, **{section: []})):
                    with self.assertRaises(ValueError):
                        Settings.load(path)


class TaskStartTest(unittest.TestCase):
    def test_metadata_hosts_match_the_issuer(self):
        from boring_service_kit.issuer.clients import DEFAULT_METADATA_HOSTS
        from boring_crocodile import settings
        self.assertEqual(settings.DEFAULT_METADATA_HOSTS, DEFAULT_METADATA_HOSTS)

    def test_a_task_runner_does_not_load_the_mcp_sdk(self):
        # Every command starts a task runner; the SDK cost 0.8 s of its start.
        import subprocess, sys
        code = "import sys, boring_crocodile.task_runner; sys.exit('mcp' in sys.modules)"
        self.assertEqual(subprocess.run([sys.executable, "-c", code]).returncode, 0)
