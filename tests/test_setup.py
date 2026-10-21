import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from boring_crocodile.setup import SetupInputs, default_home, inspect, save_inputs, unique_object, wizard

def example():
    return dict(execution_user="example-worker", control_user="example-control",
                home="/home/example-worker",
                context_mode="starter", context_source=None, context_files=[])

class SetupTest(unittest.TestCase):
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
        self.assertIn("starter or import", told[5])

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

    def test_host_home_base_and_explicit_workspace(self):
        from types import SimpleNamespace
        base = default_home("example-worker",
                            runner=lambda *a, **k: SimpleNamespace(stdout="HOME=/srv/workspaces\n"))
        self.assertEqual(base, "/srv/workspaces/example-worker")
        inputs = SetupInputs.parse(example() | {"home": base})
        self.assertEqual(inputs.home, base)
        for path in ("/etc/example-worker", "/srv/boring-crocodile/example-worker",
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
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                SetupInputs.parse(example() | change)
        with self.assertRaises(ValueError):
            json.loads('{"x": 1, "x": 2}', object_pairs_hook=unique_object)

    def test_existing_accounts_and_config_are_review_only(self):
        with tempfile.TemporaryDirectory() as d:
            config = Path(d) / "config"
            config.write_text("preserve")
            with patch("boring_crocodile.setup.account", return_value={"uid": 1003}), patch("boring_crocodile.setup.group", return_value=None):
                plan = inspect(SetupInputs.parse(example()), config)
            self.assertEqual(plan["status"], "review_required")
            self.assertEqual(config.read_text(), "preserve")
            self.assertEqual(plan["accounts"]["execution"]["action"], "review_existing")
            self.assertFalse(plan["context"]["overwrite_existing"])

    def test_import_hashes_order_without_copying_content(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.md").write_text("private agreement")
            (root / "b.md").write_text("history")
            inputs = SetupInputs.parse(example() | dict(context_mode="import", context_source=d, context_files=["b.md", "a.md"]))
            plan = inspect(inputs, root / "absent")
            self.assertEqual([x["filename"] for x in plan["context"]["documents"]], ["b.md", "a.md", "WELCOME.md"])
            self.assertNotIn("private agreement", json.dumps(plan))

    def test_missing_import_does_not_seed_starter(self):
        with tempfile.TemporaryDirectory() as d:
            inputs = SetupInputs.parse(example() | dict(context_mode="import", context_source=d, context_files=["missing.md"]))
            with self.assertRaises(FileNotFoundError):
                inspect(inputs, Path(d)/"absent")

    def test_import_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)/"source";root.mkdir()
            outside = Path(d)/"private";outside.write_text("secret")
            (root/"escape").symlink_to(outside)
            inputs = SetupInputs.parse(example() | dict(context_mode="import", context_source=str(root), context_files=["escape"]))
            with self.assertRaises(ValueError):
                inspect(inputs, Path(d)/"absent")

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
