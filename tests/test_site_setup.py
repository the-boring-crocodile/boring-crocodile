import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import ExitStack
import os
import json
import subprocess

from boring_crocodile import site_setup
from boring_crocodile.site_layout import site_lock
from boring_crocodile.setup_services import ServiceInputs


class SitePlanTest(unittest.TestCase):
    def test_resume_reuses_approved_bytes_without_adopting_foreign_files(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state, units, tmp = (base / name for name in ("state", "units", "tmpfiles"))
            for path in (state, units, tmp):
                path.mkdir()
            policy = ServiceInputs.parse(dict(
                public_base_url="https://example.test", bind_host="127.0.0.1",
                bind_port=8503, runtime_root="/opt/test/runtime",
                memory_bytes=1024**3, swap_bytes=0,
                tasks_max=256, task_tasks_max=128))
            fake_host = SimpleNamespace(protected=lambda path: None)
            replacements = [
                patch.object(site_setup, "STATE", state),
                patch.object(site_setup, "UNITS", units),
                patch.object(site_setup, "TMPFILES", tmp),
                patch.object(site_setup, "SITE_RECEIPT", state / "site-activation.json"),
                patch.object(site_setup, "Host", return_value=fake_host),
                patch.object(site_setup, "_candidate", return_value=(
                    {"pond": {"profile_id": "id", "name": "pond"}},
                    {"config.toml": "generated configuration\n"})),
                patch.object(site_setup, "_safe_existing", side_effect=lambda *args: None),
            ]
            with replacements[0], replacements[1], replacements[2], replacements[3], replacements[4], replacements[5], replacements[6]:
                proposal, _ = site_setup.plan(policy)
                self.assertEqual(proposal["status"], "proposed")
                target = state / "config.toml"
                target.write_text("generated configuration\n")
                with self.assertRaisesRegex(site_setup.SiteConflict, "A site already runs here"):
                    site_setup.plan(policy)
                (state / "site-activation.json").write_text("placeholder")
                with patch.object(site_setup, "_state", return_value={
                        "status": "prepared", "plan_digest": proposal["plan_digest"]}):
                    resumed, _ = site_setup.plan(policy)
                self.assertEqual(resumed["status"], "resume_required")
                self.assertEqual(resumed["plan_digest"], proposal["plan_digest"])


class ReactivationTest(unittest.TestCase):
    """After `uninstall --site` without --delete-data: the site files are gone, the
    activation receipt says uninstalled, credentials and setup receipts stay."""

    def test_activation_installs_the_site_again_from_the_kept_data(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state, units, tmp = (base / name for name in ("state", "units", "tmpfiles"))
            for path in (state, units, tmp):
                path.mkdir()
            receipt = state / "site-activation.json"
            receipt.write_text(json.dumps({"schema_version": 1, "status": "complete", "plan_digest": "old",
                                           "credential_names": ["approval-secret"]}))
            receipt.chmod(0o600)
            (state / "credentials").mkdir(mode=0o700)
            secret = state / "credentials/approval-secret"
            secret.write_text("kept-secret\n")
            secret.chmod(0o600)
            policy = ServiceInputs.parse(dict(
                public_base_url="https://example.test", bind_host="127.0.0.1", bind_port=8503,
                runtime_root="/opt/test/runtime", memory_bytes=1024**3, swap_bytes=0,
                tasks_max=256, task_tasks_max=128))
            rendered = {"config.toml": "generated configuration\n",
                        "boring-crocodile-worker-pond.service": "[Service]\n"}
            calls = []
            temporary = tempfile.TemporaryDirectory
            with ExitStack() as stack:
                for target, value in (("STATE", state), ("UNITS", units), ("TMPFILES", tmp),
                                      ("SITE_RECEIPT", receipt)):
                    stack.enter_context(patch.object(site_setup, target, value))
                stack.enter_context(patch.object(site_setup, "Host", return_value=SimpleNamespace(
                    protected=lambda path: None, owner=os.getuid())))
                stack.enter_context(patch.object(site_setup, "_candidate", return_value=(
                    {"pond": {"profile_id": "id", "name": "pond"}}, rendered)))
                stack.enter_context(patch.object(site_setup, "_state",  # root's receipt check aside
                                                 side_effect=lambda: json.loads(receipt.read_text())))
                with self.assertRaisesRegex(site_setup.SiteConflict, "disappeared"):
                    site_setup.plan(policy)  # what activation said before the way back
                receipt.write_text(json.dumps(json.loads(receipt.read_text()) | {"status": "uninstalled"}))
                proposal, _ = site_setup.plan(policy)
                self.assertEqual(proposal["status"], "proposed")
                stack.enter_context(patch.object(site_setup.os, "geteuid", return_value=0))
                stack.enter_context(patch.object(site_setup, "site_lock",
                                                 lambda state: site_lock(state, owner=os.getuid())))
                stack.enter_context(patch.object(site_setup, "_check_bind"))
                stack.enter_context(patch.object(site_setup.Settings, "load"))
                stack.enter_context(patch.object(site_setup, "_local_state", return_value="healthy"))
                stack.enter_context(patch.object(site_setup.tempfile, "TemporaryDirectory",
                                                 side_effect=lambda **kwargs: temporary(dir=base)))
                result = site_setup.apply(policy, proposal["plan_digest"],
                                          runner=lambda command, **kwargs: calls.append(command))
            self.assertEqual(result["status"], "configured")
            self.assertEqual((state / "config.toml").read_text(), "generated configuration\n")
            self.assertEqual((units / "boring-crocodile-worker-pond.service").read_text(), "[Service]\n")
            self.assertEqual(secret.read_text(), "kept-secret\n")  # the kept credential, not a new one
            recorded = json.loads(receipt.read_text())
            self.assertEqual((recorded["status"], recorded["plan_digest"]), ("complete", proposal["plan_digest"]))
            self.assertIn(["/usr/bin/systemctl", "enable", "--now", "boring-crocodile-mcp.target"], calls)


class ReadinessTest(unittest.TestCase):
    def test_startup_can_become_healthy_after_initial_probe(self):
        with patch.object(site_setup, "_local_state", side_effect=["degraded", "healthy"]) as probe, \
                patch.object(site_setup.time, "sleep"):
            self.assertEqual(site_setup._settled_local_state(None), "healthy")
        self.assertEqual(probe.call_count, 2)

    def test_persistent_degradation_is_not_reported_as_success(self):
        with patch.object(site_setup, "_local_state", return_value="degraded"), \
                patch.object(site_setup.time, "monotonic", side_effect=[0, 0, 60]), \
                patch.object(site_setup.time, "sleep") as sleep:
            self.assertEqual(site_setup._settled_local_state(None), "degraded")
        sleep.assert_called_once_with(1.0)

    def test_healthy_start_does_not_wait(self):
        with patch.object(site_setup, "_local_state", return_value="healthy"), \
                patch.object(site_setup.time, "sleep") as sleep:
            self.assertEqual(site_setup._settled_local_state(None), "healthy")
        sleep.assert_not_called()


class SiteActivationTest(unittest.TestCase):
    def exercise(self, fail_socket=False, credential_names=(), prepare=None, inspect=None):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            receipt = base / "site-activation.json"
            proposal = {"plan_digest": "approved", "status": "proposed",
                        "generated_files": {}, "credential_names": list(credential_names)}
            if prepare:
                prepare(base)
            rendered = {"config.toml": "synthetic",
                        "boring-crocodile-task-pond.socket": "synthetic",
                        "boring-crocodile-task-garden.socket": "synthetic"}
            calls = []
            def runner(command, **kwargs):
                calls.append(command)
                if fail_socket and command[1:2] == ["enable"] and any(name.endswith(".socket") for name in command):
                    raise subprocess.CalledProcessError(1, command)
            temporary = tempfile.TemporaryDirectory
            real_fstat = os.fstat
            def root_stat(fd):
                fields = list(real_fstat(fd))
                fields[4] = 0
                return os.stat_result(fields)
            with ExitStack() as stack:
                for target, value in [
                    ("STATE", base), ("SITE_RECEIPT", receipt),
                    ("UNITS", base), ("TMPFILES", base),
                ]:
                    stack.enter_context(patch.object(site_setup, target, value))
                stack.enter_context(patch.object(site_setup.os, "geteuid", return_value=0))
                stack.enter_context(patch.object(site_setup.os, "fstat",
                    side_effect=root_stat))
                stack.enter_context(patch.object(site_setup, "plan", return_value=(proposal, rendered)))
                stack.enter_context(patch.object(site_setup, "_check_bind"))
                stack.enter_context(patch.object(site_setup, "Host",
                    return_value=SimpleNamespace(protected=lambda path: None)))
                stack.enter_context(patch.object(site_setup.Settings, "load"))
                stack.enter_context(patch.object(site_setup, "_state",
                    side_effect=lambda: json.loads(receipt.read_text())))
                stack.enter_context(patch.object(site_setup, "_local_state", return_value="healthy"))
                stack.enter_context(patch.object(site_setup.tempfile, "TemporaryDirectory",
                    side_effect=lambda **kwargs: temporary(dir=base)))
                if fail_socket:
                    with self.assertRaises(subprocess.CalledProcessError):
                        site_setup.apply(SimpleNamespace(), "approved", runner=runner)
                else:
                    result = site_setup.apply(SimpleNamespace(), "approved", runner=runner)
                    self.assertEqual(result["status"], "configured")
            recorded = json.loads(receipt.read_text())
            if inspect:
                inspect(base, rendered)
            return calls, recorded

    def test_all_socket_dependencies_enabled_before_target_start(self):
        calls, receipt = self.exercise()
        enable = ["/usr/bin/systemctl", "enable", "--now",
                  "boring-crocodile-task-garden.socket", "boring-crocodile-task-pond.socket"]
        start = ["/usr/bin/systemctl", "enable", "--now", "boring-crocodile-mcp.target"]
        self.assertLess(calls.index(enable), calls.index(start))
        self.assertEqual(receipt["status"], "complete")

    def test_apply_writes_site_files_receipt_and_credentials(self):
        def mode(path):
            return oct(path.stat().st_mode & 0o777)

        def prepare(base):
            (base / "credentials").mkdir(mode=0o700)
            (base / "credentials/kept-secret").write_text("existing-secret\n")
            (base / "credentials/kept-secret").chmod(0o600)

        def inspect(base, rendered):
            for name, body in rendered.items():
                self.assertEqual((base / name).read_text(), body, name)
                self.assertEqual(mode(base / name), oct(0o644), name)
            self.assertEqual(mode(base / "site-activation.json"), oct(0o600))
            self.assertEqual(mode(base / "credentials"), oct(0o700))
            secret = base / "credentials/approval-secret"
            self.assertTrue(secret.read_text().strip())
            self.assertEqual(mode(secret), oct(0o600))
            self.assertEqual((base / "credentials/kept-secret").read_text(), "existing-secret\n")
            self.assertFalse([p for p in (base / "credentials").iterdir() if p.name.startswith(".")])

        _, receipt = self.exercise(credential_names=["approval-secret", "kept-secret"],
                                   prepare=prepare, inspect=inspect)
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["credential_names"], ["approval-secret", "kept-secret"])

    def test_socket_enable_failure_keeps_activation_resumable(self):
        calls, receipt = self.exercise(fail_socket=True)
        self.assertFalse(any("boring-crocodile-mcp.target" in command for command in calls))
        self.assertEqual(receipt["status"], "prepared")


class RuntimeRootTest(unittest.TestCase):
    def test_a_deployed_release_names_current(self):
        from boring_crocodile.site_setup import runtime_root
        self.assertEqual(runtime_root("/opt/boring-crocodile/releases/0.4.7/app/bin/python3"),
                         "/opt/boring-crocodile/current")
        self.assertEqual(runtime_root("/srv/croc/releases/1.0.0/app/bin/python3.14"), "/srv/croc/current")
        self.assertEqual(runtime_root("/opt/venv/bin/python3"), "/opt/venv")



class SiteWizardTest(unittest.TestCase):
    @patch("boring_crocodile.site_setup.defaults", return_value=dict(
        bind_host="127.0.0.1", bind_port=8503, memory_bytes=960 * 1024**2, swap_bytes=0,
        tasks_max=256, task_tasks_max=128, runtime_root="/opt/boring-crocodile/current"))
    def test_each_answer_is_checked_and_asked_again(self, _defaults):
        values = iter(["", "http://mcp.example.org", "https://mcp.example.org/path",
                       "https://mcp.example.org/", "localhost", "192.0.2.1", "", "80", "port",
                       "8080", "", "lots", "1024G", "2g"])
        told = []

        def bind(host, port):
            if host != "127.0.0.1" or port == 8080:
                raise OSError(99, "Cannot assign requested address")

        with tempfile.TemporaryDirectory() as state, \
                patch.object(site_setup, "STATE", Path(state)), \
                patch.object(site_setup, "_bind", bind), \
                patch.object(site_setup.os, "sysconf", side_effect=lambda name: {
                    "SC_PHYS_PAGES": 1024**2, "SC_PAGE_SIZE": 4096}[name]):
            policy = site_setup.wizard(lambda _: next(values), told.append)
        self.assertEqual((policy.public_base_url, policy.bind_host, policy.bind_port, policy.memory_bytes),
                         ("https://mcp.example.org", "127.0.0.1", 8503, 2 * 1024**3))
        again = [line for line in told if "Again" in line]
        self.assertEqual(len(again), 10)
        self.assertTrue(any("no address 192.0.2.1" in line for line in again))
        self.assertTrue(any("Port 8080 is taken" in line for line in again))
        self.assertTrue(any("This host has 4G of memory" in line for line in again))
        self.assertTrue(any("a quarter of this host's memory" in line for line in told))

    @patch("boring_crocodile.site_setup.defaults", return_value=dict(
        bind_host="127.0.0.1", bind_port=8503, memory_bytes=960 * 1024**2, swap_bytes=0,
        tasks_max=256, task_tasks_max=128, runtime_root="/home/someone/venv"))
    def test_a_runtime_outside_opt_stops_before_the_first_question(self, _defaults):
        asked = []
        with self.assertRaisesRegex(ValueError, "below /opt"):
            site_setup.wizard(lambda prompt: asked.append(prompt) or "", lambda _: None)
        self.assertEqual(asked, [])

    def test_summary_speaks_plainly(self):
        text = site_setup.review_summary({
            "policy": {"memory_bytes": 2 * 1024**3, "bind_host": "127.0.0.1", "bind_port": 8503,
                       "public_base_url": "https://mcp.example.org"},
            "ponds": {"pond": "id"}, "generated_files": {"a": "", "b": ""},
            "credential_names": ["approval-secret"]})
        self.assertIn("1 Pond (pond), each with at most 2G of memory", text)
        self.assertIn("2 files", text)
        self.assertIn("1 approval secret,", text)
        self.assertNotIn("(s)", text)


if __name__ == "__main__":
    unittest.main()
