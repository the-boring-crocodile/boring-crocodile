"""Native proof orchestration; account execution remains an explicit OS boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import boring_deploy
from boring_crocodile import site_adopt, site_adopt_proof
from boring_crocodile.site_host import Host
from boring_crocodile.site_model import SiteDescription
from test_site_model import description
from site_host import FakeHost


class AdoptionProofTest(unittest.TestCase):
    def test_unprivileged_refusal_reports_every_account_without_staging(self):
        site = SiteDescription.parse(*description())
        with patch.object(site_adopt_proof.os, "geteuid", return_value=1000), patch.object(
                site_adopt_proof.tempfile, "TemporaryDirectory") as temporary:
            report = site_adopt_proof.prove(site, None)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["cleanup"], "done")
        self.assertEqual(len(report["accounts"]), 3)
        for result in [*report["accounts"], report["deploy_check"]]:
            self.assertEqual(result["status"], "failed")
            self.assertIn("requires root", result["error"])
        temporary.assert_not_called()

    def test_private_copy_account_errors_real_context_hook_and_cleanup(self):
        main, ponds = description()
        ponds["garden"]["enabled"] = True
        site = SiteDescription.parse(main, ponds)
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary) / "artifact"
            release.mkdir()
            (release / "RELEASE.json").write_text(json.dumps(dict(
                component="boring-crocodile", version="0.5.0rc6",
                runtime="venv", default_home="/opt/boring-crocodile")))
            staged, calls, contexts = [], [], []
            # Real modes/xattrs on temporary files; only account switching and
            # heavy offline runtime preparation are faked in this logic test.
            private = tempfile.TemporaryDirectory(prefix="proof-fixture-")
            host = FakeHost(Path(private.name))
            def prepare(context):
                self.assertIsInstance(context, boring_deploy.Context)
                contexts.append(context)
                context.new = context.home / "releases/candidate"
                context.new.mkdir(parents=True)
                self.assertFalse((context.home / "current").exists())
            def probe(command):
                calls.append(command)
                if command[0].endswith("setpriv"):
                    config = Path(command[-3])
                    staged.append(config.parent.parent)
                    self.assertTrue(config.is_file())
                    role = command[-2]
                    loaded = SiteDescription.read(config, owner=os.getuid(), pond=role or None,
                        ponds=tuple(command[-1].split(",")) if not role else None)
                    self.assertEqual(loaded.state, site.state)
                    if role == "garden":
                        return dict(status="failed", error="PermissionError: exact garden fixture error", exit_code=1)
                else:
                    self.assertEqual(command[4], site_adopt_proof.CHECK)
                    self.assertEqual(Path(command[-1]), staged[0] / "description/config.toml")
                    self.assertIn("steps.check(ctx)", command[4])
                    self.assertEqual(Path(command[-2]), contexts[0].home)
                return dict(status="ok")
            with patch.object(site_adopt_proof.tempfile, "TemporaryDirectory", return_value=private), patch.object(
                    site_adopt_proof.os, "geteuid", return_value=0), patch.object(
                    site_adopt_proof, "Host", return_value=host), patch.object(
                    boring_deploy.Venv, "prepare", side_effect=prepare), patch.object(
                    site_adopt_proof, "_probe", side_effect=probe):
                report = site_adopt_proof.prove(site, release)
            self.assertEqual(report["status"], "failed")
            self.assertEqual([row["status"] for row in report["accounts"]], ["ok", "ok", "failed"], report)
            self.assertEqual(report["accounts"][-1]["error"], "PermissionError: exact garden fixture error")
            self.assertEqual(report["deploy_check"]["status"], "ok")
            self.assertEqual(len(calls), 4)
            self.assertTrue(all(not root.exists() for root in staged))
            for call, account in zip(calls, report["accounts"]):
                self.assertEqual(call[1:5], ["--reuid", str(account["uid"]), "--regid", str(account["gid"])])
                if account["role"] == "controller":
                    self.assertEqual(call[5:7], ["--groups", "1201,1202"])
                else:
                    self.assertEqual(call[5:7], ["--clear-groups", "--no-new-privs"])

    def test_umask_077_normalizes_only_the_runtime_and_proof_passes(self):
        site = SiteDescription.parse(*description())
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "artifact"
            artifact.mkdir(mode=0o700)
            (artifact / "RELEASE.json").write_text(json.dumps(dict(
                component="boring-crocodile", version="0.5.0rc7",
                runtime="venv", default_home="/opt/boring-crocodile")))
            private_input = artifact / "private-input"
            private_input.write_text("private fixture")
            private_input.chmod(0o600)
            private = tempfile.TemporaryDirectory(prefix="proof-umask-")
            root = Path(private.name)
            host = FakeHost(root)
            contexts = []
            def prepare(context):
                contexts.append(context)
                context.new = context.home / "releases/candidate"
                (context.new / "bin").mkdir(parents=True)
                python = context.new / "bin/python3"
                python.write_text("executable fixture")
                python.chmod(0o700)
                self.assertEqual(context.home.stat().st_mode & 0o777, 0o700)
            def probe(command):
                context = contexts[0]
                for folder in (context.home, context.home / "releases", context.new, context.new / "bin"):
                    self.assertEqual(folder.stat().st_mode & 0o777, 0o755)
                self.assertEqual((context.new / "bin/python3").stat().st_mode & 0o777, 0o755)
                for config in (root / "description").rglob("config.toml"):
                    expected = 0o644 if config.parent.name == "description" else 0o640
                    self.assertEqual(config.stat().st_mode & 0o777, expected)
                    self.assertFalse(host.description_metadata(config)["acl"])
                self.assertEqual(root.stat().st_mode & 0o777, 0o711)
                self.assertEqual(private_input.stat().st_mode & 0o777, 0o600)
                return dict(status="ok")
            previous = os.umask(0o077)
            try:
                with patch.object(site_adopt_proof.tempfile, "TemporaryDirectory", return_value=private), patch.object(
                        site_adopt_proof.os, "geteuid", return_value=0), patch.object(
                        site_adopt_proof, "Host", return_value=host), patch.object(
                        boring_deploy.Venv, "prepare", side_effect=prepare), patch.object(
                        site_adopt_proof, "_probe", side_effect=probe):
                    report = site_adopt_proof.prove(site, artifact)
            finally:
                os.umask(previous)
            self.assertEqual(report["status"], "ok", report)
            self.assertEqual(report["cleanup"], "done")
            self.assertFalse(root.exists())
            self.assertEqual(private_input.read_text(), "private fixture")
            self.assertEqual(private_input.stat().st_mode & 0o777, 0o600)

    def test_process_error_and_timeout_are_reported_verbatim(self):
        with patch.object(site_adopt_proof.subprocess, "Popen") as popen, patch.object(
                site_adopt_proof.os, "killpg") as kill:
            process = popen.return_value.__enter__.return_value
            process.returncode = 1
            process.communicate.return_value = ("", "PermissionError: /private/ponds\n")
            command = [sys.executable, "-c", "pass"]
            self.assertEqual(site_adopt_proof._probe(command)["error"], "PermissionError: /private/ponds")
            self.assertEqual(popen.call_args.args[0], [sys.executable, "-I", "-B", "-c", "pass"])
            self.assertEqual(popen.call_args.kwargs["cwd"], "/")
            self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            kill.assert_not_called()
            process.communicate.side_effect = [subprocess.TimeoutExpired("loader", 120), ("", "")]
            self.assertIn("120", site_adopt_proof._probe(command)["error"])
            kill.assert_called_once_with(process.pid, site_adopt_proof.signal.SIGKILL)
            self.assertEqual(process.communicate.call_count, 3)

    def test_cli_failed_proof_is_json_and_nonzero(self):
        from contextlib import redirect_stdout
        import io
        site = SiteDescription.parse(*description())
        output = io.StringIO()
        with patch.object(site_adopt, "plan", return_value=({"status": "proposed"}, (site, {}))), patch.object(
                site_adopt_proof, "prove", return_value={"status": "failed"}), redirect_stdout(output):
            self.assertEqual(site_adopt.main(["--dry-run"], artifact_root=Path("/candidate")), 2)
        self.assertEqual(json.loads(output.getvalue())["native_proof"]["status"], "failed")
