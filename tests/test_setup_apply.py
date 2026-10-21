import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from boring_crocodile.setup import SetupInputs, inspect, plan_digest
from boring_crocodile.setup_apply import Host, SetupConflict, apply

class FakeHost(Host):
    def __init__(self, root):
        super().__init__(root)
        self.owner = os.getuid()
        self.users = {}
        self.groups = {}
        self.calls = []
        self.fail_group = None
        for name in ("etc", "srv", "home"):
            (root/name).mkdir(mode=0o755)
    def require_authority(self):
        pass
    def account(self, name):
        return self.users.get(name)
    def group(self, name):
        return self.groups.get(name)
    def create_group(self, name):
        if name == self.fail_group:
            raise RuntimeError("Injected failure")
        self.calls.append(("group", name))
        self.groups[name] = {"name": name, "gid": 2000+len(self.groups)}
    def create_user(self, name, home, access):
        self.calls.append(("user", name))
        self.users[name] = dict(name=name, uid=3000+len(self.users),
                               gid=self.groups[name]["gid"], home=home,
                               shell="/usr/sbin/nologin")
    def chown(self, path, uid, gid):
        self.calls.append(("chown", str(path), uid, gid))
    def memberships(self, name, gid):
        return sorted({gid, self.groups["test-worker"]["gid"]}) if name == "test-control" else [gid]
    def fresh_plan(self, inputs):
        with patch("boring_crocodile.setup.account", side_effect=self.account), patch("boring_crocodile.setup.group", side_effect=self.group):
            return inspect(inputs, self.path("/etc/boring-crocodile/config.toml"))

class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.host = FakeHost(self.root)
        self.inputs = SetupInputs.parse(dict(execution_user="test-worker",
            control_user="test-control", home="/home/test-worker", context_mode="starter",
            context_source=None, context_files=[]))
        self.digest = plan_digest(self.host.fresh_plan(self.inputs))
    def tearDown(self):
        self.temp.cleanup()
    def test_initial_apply_and_repeat_preserve_editable_agreement(self):
        result = apply(self.inputs, self.digest, host=self.host)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(set(self.host.groups), {"test-worker", "test-control"})
        published = self.host.path("/srv/boring-crocodile/context-initial/HANDBOOK.md")
        editable = self.host.path("/home/test-worker/handbook/HANDBOOK.md")
        self.assertFalse(published.exists())
        self.assertTrue(editable.is_file())
        self.assertTrue((editable.parent / "WELCOME.md").is_file())
        self.assertEqual(result["context_root"], "/home/test-worker/handbook")
        receipt_path = self.host.path("/etc/boring-crocodile/initial-setup.json")
        profile_id = json.loads(receipt_path.read_text())["profile_id"]
        from uuid import UUID
        self.assertEqual(str(UUID(profile_id)), profile_id)
        self.assert_seeded_on_disk(result)
        editable.write_text("our evolved agreement")
        before = len(self.host.calls)
        repeat_digest = plan_digest(self.host.fresh_plan(self.inputs))
        self.assertNotEqual(repeat_digest, self.digest)
        result = apply(self.inputs, repeat_digest, host=self.host)
        self.assertEqual(result["status"], "already_complete")
        self.assertEqual(editable.read_text(), "our evolved agreement")
        self.assertEqual(len(self.host.calls), before)
        self.assertEqual(json.loads(receipt_path.read_text())["profile_id"], profile_id)
    def assert_seeded_on_disk(self, result):
        from boring_crocodile.instructions import WELCOME
        mode = lambda path: path.lstat().st_mode & 0o777
        starter = Path(__file__).resolve().parent.parent / "src/boring_crocodile/templates/HANDBOOK.md"
        home = self.host.path("/home/test-worker")
        handbook = home / "handbook"
        self.assertEqual((handbook / "HANDBOOK.md").read_bytes(), starter.read_bytes())
        self.assertEqual((handbook / "WELCOME.md").read_bytes(), WELCOME.encode())
        for directory in (home, handbook, home / "work", home / "lab"):
            self.assertEqual(mode(directory), 0o700, directory)
        for name in ("HANDBOOK.md", "WELCOME.md"):
            self.assertEqual(mode(handbook / name), 0o600, name)
        receipt_path = self.host.path("/etc/boring-crocodile/initial-setup.json")
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(mode(receipt_path), 0o600)
        worker = self.host.users["test-worker"]
        self.assertEqual((result["execution_uid"], receipt["users"]["test-worker"]), (worker["uid"], worker))
        chowned = {path: (uid, gid) for kind, path, *ids in self.host.calls if kind == "chown" for uid, gid in [ids]}
        expected = {str(p) for p in (home, handbook, home / "work", home / "lab",
                                     handbook / "HANDBOOK.md", handbook / "WELCOME.md")}
        self.assertEqual(set(chowned), expected)
        self.assertEqual(set(chowned.values()), {(worker["uid"], worker["gid"])})
        self.assertEqual(self.host.calls[-1][:2], ("chown", str(home)))

    def test_changed_plan_refused_before_writes(self):
        with self.assertRaises(SetupConflict):
            apply(self.inputs, "wrong", host=self.host)
        self.assertEqual(self.host.calls, [])
        self.assertFalse(self.host.path("/etc/boring-crocodile").exists())
    def test_existing_installation_refused(self):
        state = self.host.path("/etc/boring-crocodile");state.mkdir()
        config = state/"config.toml";config.write_text("live")
        with self.assertRaises(SetupConflict):
            apply(self.inputs, self.digest, host=self.host)
        self.assertEqual(config.read_text(), "live")
        self.assertEqual(self.host.calls, [])
    def test_partial_account_setup_requires_explicit_resume(self):
        self.host.fail_group = "test-control"
        with self.assertRaises(RuntimeError):
            apply(self.inputs, self.digest, host=self.host)
        self.host.fail_group = None
        with self.assertRaises(SetupConflict):
            apply(self.inputs, self.digest, host=self.host)
        result = apply(self.inputs, self.digest, resume=True, host=self.host)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.host.calls.count(("group", "test-worker")), 1)
    def test_unrecorded_account_operation_is_not_silently_adopted(self):
        self.host.fail_group = "test-control"
        with self.assertRaises(RuntimeError):
            apply(self.inputs, self.digest, host=self.host)
        self.host.fail_group = None
        self.host.create_group("test-control")
        with self.assertRaisesRegex(SetupConflict, "Unrecorded existing group"):
            apply(self.inputs, self.digest, resume=True, host=self.host)
    def test_home_symlink_refused(self):
        self.host.path("/home/test-worker").symlink_to(self.root/"etc")
        with self.assertRaises(SetupConflict):
            apply(self.inputs, self.digest, host=self.host)
        self.assertEqual(self.host.calls, [])
    def test_import_change_after_plan_is_refused(self):
        source = self.root/"source";source.mkdir();(source/"contract.md").write_text("first")
        inputs = SetupInputs.parse(self.inputs.as_input() | dict(context_mode="import",
                                  context_source=str(source), context_files=["contract.md"]))
        digest = plan_digest(self.host.fresh_plan(inputs))
        (source/"contract.md").write_text("second")
        with self.assertRaises(SetupConflict):
            apply(inputs, digest, host=self.host)
        self.assertEqual(self.host.calls, [])
    def test_nested_import_seeds_selected_files_only(self):
        source = self.root/"source";source.mkdir()
        (source/"contract.md").write_text("agreement")
        (source/"excluded.md").write_text("private unrelated data")
        (source/"nested").mkdir();(source/"nested/history.md").write_text("history")
        inputs = SetupInputs.parse(self.inputs.as_input() | dict(context_mode="import",
            context_source=str(source), context_files=["contract.md", "nested/history.md"]))
        apply(inputs, plan_digest(self.host.fresh_plan(inputs)), host=self.host)
        snapshot = self.host.path("/home/test-worker/handbook")
        self.assertEqual((snapshot/"nested/history.md").read_text(), "history")
        self.assertFalse((snapshot/"excluded.md").exists())
        self.assertTrue((source/"excluded.md").exists())

    def test_resume_refuses_interrupted_ownership_transfer(self):
        apply(self.inputs, self.digest, host=self.host)
        path = self.host.path("/etc/boring-crocodile/initial-setup.json")
        receipt = json.loads(path.read_text());receipt["status"] = "transferring"
        path.write_text(json.dumps(receipt))
        before = list(self.host.calls)
        with self.assertRaisesRegex(SetupConflict, "manual inspection"):
            apply(self.inputs, self.digest, resume=True, host=self.host)
        self.assertEqual(before, self.host.calls)

    def test_staged_context_tampering_blocks_resume(self):
        self.host.fail_group = "test-control"
        with self.assertRaises(RuntimeError):
            apply(self.inputs, self.digest, host=self.host)
        self.host.path("/etc/boring-crocodile/initial-context/0").write_text("changed")
        before = list(self.host.calls)
        with self.assertRaises(SetupConflict):
            apply(self.inputs, self.digest, resume=True, host=self.host)
        self.assertEqual(self.host.calls, before)

if __name__ == "__main__":
    unittest.main()

class AccountCommandTest(unittest.TestCase):
    def test_worker_has_only_primary_group_controller_joins_pond(self):
        host = Host()
        with patch("boring_crocodile.setup_apply.subprocess.run") as run:
            host.create_user("worker", "/home/worker", None)
            worker_command = run.call_args.args[0]
            self.assertNotIn("--groups", worker_command)
            self.assertEqual(worker_command[worker_command.index("--gid") + 1], "worker")
            host.create_user("controller", "/nonexistent", "worker")
            controller_command = run.call_args.args[0]
            self.assertEqual(controller_command[controller_command.index("--groups") + 1], "worker")
