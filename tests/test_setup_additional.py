"""Additional provisioning uses the initial description, never setup receipts."""
from dataclasses import replace
from boring_crocodile import setup_apply as provision
import test_setup_apply as fixtures


class AdditionalTest(fixtures.ProvisionCase):
    def test_wrong_controller_and_overlapping_worker_are_refused(self):
        self.provision()
        for inputs in (replace(self.inputs, control_user="other-control"),
                       replace(self.inputs, home="/home/demo-pond")):
            with self.subTest(inputs=inputs), self.assertRaises(ValueError):
                provision.plan_additional(inputs, "garden", host=self.host)

    def test_plan_keeps_running_neighbors_and_declared_data(self):
        self.provision()
        before = self.host.paths.config.read_bytes()
        inputs = replace(self.inputs, execution_user="demo-garden", home="/home/demo-garden")
        proposal = provision.plan_additional(inputs, "garden", host=self.host)
        provision.apply_additional(inputs, "garden", proposal["plan_digest"], host=self.host)
        self.assertEqual(self.host.paths.config.read_bytes(), before)
        self.assertFalse(self.host.active)
