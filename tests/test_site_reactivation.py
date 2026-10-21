"""Activation after `uninstall --site` (data kept), on the real activation path:
setup receipts made by setup and --add-pond, the kept data uninstall recorded,
files rendered and installed on disk. A parked Pond stays parked."""
import json
import os
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_setup_apply
import test_setup_additional
from boring_crocodile import site_setup
from boring_crocodile.setup import SetupInputs, plan_digest
from boring_crocodile.setup_apply import apply_additional, plan_additional
from boring_crocodile.setup_services import ServiceInputs
from boring_crocodile.settings import Settings
from boring_crocodile.site_check import _size, _values
from boring_crocodile.site_layout import STATE_DIR, budget_slice, site_lock
from test_setup_services import policy


class ReactivationTest(unittest.TestCase):
    setUp = test_setup_apply.ApplyTest.setUp
    tearDown = test_setup_apply.ApplyTest.tearDown
    prepare = test_setup_additional.AdditionalPondTest.prepare

    def add(self, name):
        inputs = SetupInputs.parse(self.inputs.as_input() | {"execution_user": name + "-worker",
                                                              "home": f"/home/{name}-worker"})
        apply_additional(inputs, name, plan_digest(plan_additional(inputs, name, host=self.host)), host=self.host)

    def test_live_ponds_come_back_with_their_ceilings_and_callers_a_parked_one_stays_parked(self):
        self.prepare()
        self.add("beta")
        self.add("gamma")
        state = self.host.path(STATE_DIR)
        units, tmpfiles = self.host.path("/etc/systemd/system"), self.host.path("/etc/tmpfiles.d")
        units.mkdir(parents=True)
        tmpfiles.mkdir()
        credentials = state / "credentials"
        credentials.mkdir(mode=0o700)
        for name in ("approval-secret", "beta-approval-secret", "gamma-approval-secret"):
            (credentials / name).write_text(name + "-kept\n")
            (credentials / name).chmod(0o600)
        # What `uninstall --site` without --delete-data leaves: gamma was parked then.
        receipt = state / "site-activation.json"
        receipt.write_text(json.dumps({
            "schema_version": 1, "status": "uninstalled", "plan_digest": "old",
            "kept": {"live_ponds": ["beta", "pond"],
                     "ceilings": {"beta": {"memory_bytes": 1024**3, "swap_bytes": 0, "tasks_max": 64,
                                           "task_tasks_max": 32}},
                     "pirate_callers": {"parrot": 950}}}))
        receipt.chmod(0o600)
        chosen = ServiceInputs.parse(policy())
        calls = []
        temporary = tempfile.TemporaryDirectory
        with ExitStack() as stack:
            for target, value in (("STATE", state), ("UNITS", units), ("TMPFILES", tmpfiles),
                                  ("SITE_RECEIPT", receipt)):
                stack.enter_context(patch.object(site_setup, target, value))
            stack.enter_context(patch.object(site_setup, "Host", return_value=self.host))
            stack.enter_context(patch.object(site_setup, "_check_runtime"))  # no /opt runtime in a test
            stack.enter_context(patch.object(site_setup, "_state",  # root's receipt check aside
                                             side_effect=lambda: json.loads(receipt.read_text())))
            proposal, _ = site_setup.plan(chosen)
            self.assertEqual((proposal["status"], sorted(proposal["ponds"])), ("proposed", ["beta", "pond"]))
            self.assertEqual(proposal["credential_names"], ["approval-secret", "beta-approval-secret"])
            stack.enter_context(patch.object(site_setup.os, "geteuid", return_value=0))
            stack.enter_context(patch.object(site_setup, "site_lock",
                                             lambda path: site_lock(path, owner=os.getuid())))
            stack.enter_context(patch.object(site_setup, "_check_bind"))
            stack.enter_context(patch.object(site_setup, "_local_state", return_value="healthy"))
            stack.enter_context(patch.object(site_setup.tempfile, "TemporaryDirectory",
                                             side_effect=lambda **kwargs: temporary(dir=self.root)))
            result = site_setup.apply(chosen, proposal["plan_digest"],
                                      runner=lambda command, **kwargs: calls.append(command))
        self.assertEqual(result["status"], "configured")
        settings = Settings.load(state / "config.toml")
        self.assertEqual(sorted(settings.ponds), ["beta", "pond"])
        self.assertEqual(settings.pirate_service_callers, {"parrot": 950})
        installed = sorted(p.name for p in [*units.iterdir(), *tmpfiles.iterdir()])
        self.assertTrue(any("beta" in name for name in installed))
        self.assertFalse([name for name in installed if "gamma" in name])  # still parked
        self.assertEqual((credentials / "gamma-approval-secret").read_text(), "gamma-approval-secret-kept\n")
        self.assertEqual((credentials / "beta-approval-secret").read_text(), "beta-approval-secret-kept\n")
        beta = _values((units / budget_slice("beta")).read_text())
        pond = _values((units / budget_slice("pond")).read_text())
        self.assertEqual((_size(beta["MemoryMax"]), int(beta["TasksMax"])), (1024**3, 64))
        self.assertEqual(_size(pond["MemoryMax"]), chosen.memory_bytes)  # no kept ceilings: the policy's
        recorded = json.loads(receipt.read_text())
        self.assertEqual((recorded["status"], recorded["plan_digest"]), ("complete", proposal["plan_digest"]))
        self.assertTrue((state / "ponds/gamma/initial-setup.json").exists())


if __name__ == "__main__":
    unittest.main()
