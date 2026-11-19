from functools import partial
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from boring_deploy import Failure, root_input

from boring_crocodile.setup import SetupInputs, default_home, context_payload, save_inputs, unique_object, wizard

def example():
    return dict(execution_user="example-worker", control_user="example-control",
                home="/home/example-worker",
                context_mode="starter", context_source=None, context_files=[])

class SetupTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("boring_crocodile.setup.root_input",
                                side_effect=partial(root_input, owner=os.getuid())))

    @patch("boring_crocodile.setup.default_home", side_effect=lambda user: "/home/" + user)
    def test_wizard_and_unattended_share_model(self, _home):
        values = iter(["example-worker", "example-control",
                       "/home/example-worker", "", "starter"])
        self.assertEqual(wizard(lambda _: next(values), lambda _: None), SetupInputs.parse(example()))

    @patch("boring_crocodile.setup.default_home", side_effect=lambda user: "/home/" + user)
    def test_wizard_checks_each_answer_and_asks_again(self, _home):
        values = iter(["sudo boring-crocodile-setup --site-wizard", "example-worker",
                       "example-worker", "example-control",
                       "/home/elsewhere", "/etc/example-worker", "/nowhere-at-all/example-worker",
                       "/home/example-worker",
                       "The Steady Beaver", "maybe", "starter"])
        told = []
        inputs = wizard(lambda _: next(values), told.append)
        self.assertEqual(inputs, SetupInputs.parse(example() | {"display_name": "The Steady Beaver"}))
        self.assertEqual(len(told), 6)
        self.assertIn("Unix account name", told[0])
        self.assertIn("must differ", told[1])
        self.assertIn("ending in /example-worker", told[2])
        self.assertIn("/nowhere-at-all does not exist", told[4])
        self.assertIn("starter, engineer, consultant or import", told[5])

    @patch("boring_crocodile.setup.default_home", side_effect=lambda user: "/home/" + user)
    def test_wizard_for_another_pond_uses_the_sites_controller(self, _home):
        values = iter(["", "/home/garden-user", "", "starter"])
        told = []
        inputs = wizard(lambda _: next(values), told.append, pond="garden", controller="example-control")
        self.assertEqual((inputs.execution_user, inputs.control_user, inputs.display_name),
                         ("garden-user", "example-control", None))
        self.assertIn("example-control", told[0])

    @patch("boring_crocodile.setup.default_home", side_effect=lambda user: "/home/" + user)
    def test_wizard_import_asks_for_files_that_exist(self, _home):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "HANDBOOK.md").write_text("h")
            values = iter(["example-worker", "example-control", "/home/example-worker", "",
                           "import", "relative/path", folder, "", "MISSING.md", "HANDBOOK.md",
                           "HANDBOOK.md", ""])
            told = []
            inputs = wizard(lambda _: next(values), told.append)
        self.assertEqual((inputs.context_source, inputs.context_files), (folder, ("HANDBOOK.md",)))
        self.assertEqual(len(told), 4)

    def test_packaged_handbooks_are_release_resources(self):
        from boring_crocodile.instructions import WELCOME
        from boring_crocodile.shared_guide import load_shared_guide
        guide = load_shared_guide()
        self.assertEqual(guide["filename"], "CAPABILITIES.md")
        self.assertIn("## Working well", guide["content"])
        headings = {"starter": b"# A starting agreement", "engineer": b"# The Steady Beaver",
                    "consultant": b"# The Tidy Kingfisher"}
        for mode, heading in headings.items():
            with self.subTest(mode=mode):
                inputs = SetupInputs.parse(example() | {"context_mode": mode})
                payload = context_payload(inputs)
                self.assertEqual([name for name, _ in payload],
                                 ["HANDBOOK.md", "WELCOME.md", "OPERATING_TOGETHER.md"])
                self.assertTrue(payload[2][1].startswith(b"# Operating this Pond together"))
                self.assertTrue(payload[0][1].startswith(heading))
                self.assertTrue(payload[1][1].startswith(b"Welcome to"))
                if mode == "starter":
                    self.assertEqual(payload[1][1], WELCOME.encode("utf-8"))
                with self.assertRaises(ValueError):
                    SetupInputs.parse(example() | {"context_mode": mode, "context_source": "/tmp"})

    @patch("boring_crocodile.setup.default_home", side_effect=lambda user: "/home/" + user)
    def test_wizard_offers_each_packaged_handbook_without_import_fields(self, _home):
        for mode in ("starter", "engineer", "consultant"):
            with self.subTest(mode=mode):
                answers = iter(["example-worker", "example-control", "/home/example-worker", "", mode])
                inputs = wizard(lambda _: next(answers), lambda _: None)
                self.assertEqual(inputs, SetupInputs.parse(example() | {"context_mode": mode}))
                self.assertIsNone(inputs.context_source)
                self.assertEqual(inputs.context_files, ())

    def test_default_home_uses_the_common_root_child_boundary(self):
        from types import SimpleNamespace
        from boring_deploy import SYSTEM_TOOL_PATH

        calls = []
        def runner(command, **options):
            calls.append((command, options))
            return SimpleNamespace(stdout="HOME=/srv/workspaces\n")

        with patch.dict(os.environ, PATH="/untrusted/bin"):
            self.assertEqual(default_home("example-worker", runner=runner),
                             "/srv/workspaces/example-worker")
        command, options = calls[0]
        self.assertEqual(command, ["/usr/sbin/useradd", "-D"])
        self.assertEqual(options.get("cwd"), "/")
        self.assertEqual(options.get("env", {}).get("PATH"), SYSTEM_TOOL_PATH)
        self.assertTrue(options["check"])
        self.assertTrue(options["capture_output"])
        self.assertTrue(options["text"])

    def test_host_home_base_and_explicit_workspace(self):
        from types import SimpleNamespace
        base = default_home("example-worker",
                            runner=lambda *a, **k: SimpleNamespace(stdout="HOME=/srv/workspaces\n"))
        self.assertEqual(base, "/srv/workspaces/example-worker")
        inputs = SetupInputs.parse(example() | {"home": base})
        self.assertEqual(inputs.home, base)
        for path in ("/etc/example-worker",
                     "/mnt/../example-worker", "/tmp/example-worker",
                     "/srv/another-user", "/srv/space here/example-worker"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                SetupInputs.parse(example() | {"home": path})
        with self.assertRaisesRegex(ValueError, "Unsafe useradd"):
            default_home("example-worker",
                         runner=lambda *a, **k: SimpleNamespace(stdout="HOME=/tmp\n"))

    def test_rejects_unsafe_or_ambiguous_inputs(self):
        changes = [
            {"execution_user": "root"}, {"control_user": "example-worker"},
            {"execution_user": "x; command"}, {"home": "/"},
            {"home": "/home/../etc"}, {"context_mode": "unknown"},
            {"context_source": "/tmp"}, {"context_files": ["../escape"]},
            {"access_group": "old-access"}, {"unexpected": True}, {"context_mode": "import"},
        ]
        changes += [{"display_name": bad} for bad in ("", " padded", "two\nlines", "x"*65, 7)]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                SetupInputs.parse(example() | change)
        with self.assertRaises(ValueError):
            json.loads('{"x": 1, "x": 2}', object_pairs_hook=unique_object)

    def test_missing_import_does_not_seed_starter(self):
        with tempfile.TemporaryDirectory() as d:
            inputs = SetupInputs.parse(example() | dict(context_mode="import", context_source=d, context_files=["missing.md"]))
            with self.assertRaisesRegex(Failure, "operator input"):
                context_payload(inputs)

    def test_import_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)/"source";root.mkdir()
            outside = Path(d)/"private";outside.write_text("secret")
            (root/"escape").symlink_to(outside)
            inputs = SetupInputs.parse(example() | dict(context_mode="import", context_source=str(root), context_files=["escape"]))
            with self.assertRaisesRegex(Failure, "operator input"):
                context_payload(inputs)

    def test_saved_input_is_private_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/"input.json";inputs = SetupInputs.parse(example())
            save_inputs(path, inputs)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(SetupInputs.parse(json.loads(path.read_text())), inputs)
            with self.assertRaises(FileExistsError):
                save_inputs(path, inputs)
            link = Path(d)/"link";link.symlink_to(path)
            with self.assertRaises(FileExistsError):
                save_inputs(link, inputs)

if __name__ == "__main__":
    unittest.main()
