
import test_setup_apply
from test_setup_apply import FakeHost
from boring_crocodile.setup import SetupInputs, plan_digest
from boring_crocodile.setup_apply import apply, apply_additional, plan_additional, SetupConflict, site_receipts, validate_site
from boring_crocodile.setup_services import ServiceInputs, render_site
from boring_crocodile.settings import Settings
from test_setup_services import policy


class MultiHost(FakeHost):
    def __init__(self, root):
        super().__init__(root)
        self.member_gids = {}
        self.fail_membership_after = False
        self.report = {"status": "regular", "counts": {"understood": 0, "adjustments": 0, "irregular": 0, "unchecked": 0}, "irregular": [],
                       "adjustments": [], "understood": [], "unchecked": []}

    def site_check(self):
        return self.report

    def controller_user(self):
        return "test-control"

    def create_user(self, name, home, access):
        super().create_user(name, home, access)
        self.member_gids[name] = {self.users[name]["gid"]} | ({self.groups[access]["gid"]} if access else set())

    def memberships(self, name, gid):
        return sorted(self.member_gids[name])

    def add_membership(self, name, access):
        self.calls.append(("membership", name, access))
        self.member_gids[name].add(self.groups[access]["gid"])
        if self.fail_membership_after:
            raise RuntimeError("Interrupted after usermod")


# Reuse only the filesystem fixture, not the initial suite's tests.
import unittest
class AdditionalPondTest(unittest.TestCase):
    setUp = test_setup_apply.ApplyTest.setUp
    tearDown = test_setup_apply.ApplyTest.tearDown

    def prepare(self):
        # ApplyTest made the base directories already.
        for child in self.root.iterdir():
            child.rmdir()
        self.host = MultiHost(self.root)
        self.digest = plan_digest(self.host.fresh_plan(self.inputs))
        apply(self.inputs, self.digest, host=self.host)
        self.beta = SetupInputs.parse(self.inputs.as_input() | {
            "execution_user": "beta-worker", "home": "/home/beta-worker"})
        return plan_digest(plan_additional(self.beta, "beta", host=self.host))

    def test_add_repeat_isolation_and_no_activation(self):
        digest = self.prepare()
        original = self.host.path("/etc/boring-crocodile/initial-setup.json").read_bytes()
        config = self.host.path("/etc/boring-crocodile/config.toml")
        config.write_text("live configuration")
        digest = plan_digest(plan_additional(self.beta, "beta", host=self.host))
        outcome = apply_additional(self.beta, "beta", digest, host=self.host)
        self.assertFalse(outcome["service_activated"])
        self.assertEqual(config.read_text(), "live configuration")
        self.assertEqual(self.host.path("/etc/boring-crocodile/initial-setup.json").read_bytes(), original)
        receipts = site_receipts(self.host)
        validate_site(self.host, receipts, None)
        self.assertNotEqual(receipts["pond"]["profile_id"], receipts["beta"]["profile_id"])
        self.assertNotIn(self.host.groups["test-worker"]["gid"],
                         self.host.member_gids["beta-worker"])
        self.assertNotIn(self.host.groups["beta-worker"]["gid"],
                         self.host.member_gids["test-worker"])
        editable = self.host.path("/home/beta-worker/handbook/HANDBOOK.md")
        editable.write_text("evolved beta agreement")
        before = list(self.host.calls)
        self.assertEqual(apply_additional(self.beta, "beta", digest, host=self.host)["status"],
                         "already_complete")
        self.assertEqual(self.host.calls, before)
        self.assertEqual(editable.read_text(), "evolved beta agreement")

    def test_unrecorded_controller_privilege_refused_before_changes(self):
        digest = self.prepare()
        self.host.member_gids["test-control"].add(9999)
        before = list(self.host.calls)
        with self.assertRaises(SetupConflict):
            apply_additional(self.beta, "beta", digest, host=self.host)
        self.assertEqual(self.host.calls, before)

    def test_membership_interruption_resumes_only_recorded_group(self):
        digest = self.prepare()
        self.host.fail_membership_after = True
        with self.assertRaises(RuntimeError):
            apply_additional(self.beta, "beta", digest, host=self.host)
        self.host.fail_membership_after = False
        with self.assertRaises(SetupConflict):
            apply_additional(self.beta, "beta", digest, host=self.host)
        outcome = apply_additional(self.beta, "beta", digest, host=self.host, resume=True)
        self.assertEqual(outcome["status"], "complete")
        validate_site(self.host, site_receipts(self.host), None)
        self.assertEqual(self.host.calls.count(("membership", "test-control", "beta-worker")), 1)

    def test_stale_site_and_existing_identity_fail_before_writes(self):
        digest = self.prepare()
        self.host.path("/etc/boring-crocodile/config.toml").write_text("new operator configuration")
        before = list(self.host.calls)
        with self.assertRaises(SetupConflict):
            apply_additional(self.beta, "beta", digest, host=self.host)
        self.assertEqual(before, self.host.calls)
        self.host.create_group("beta-worker")
        plan = plan_additional(self.beta, "beta", host=self.host)
        self.assertEqual(plan["status"], "review_required")
        before = list(self.host.calls)
        with self.assertRaises(SetupConflict):
            apply_additional(self.beta, "beta", plan_digest(plan), host=self.host)
        self.assertEqual(before, self.host.calls)

    def test_other_incomplete_pond_and_symlink_refused(self):
        digest = self.prepare()
        self.host.fail_group = "beta-worker"
        with self.assertRaises(RuntimeError):
            apply_additional(self.beta, "beta", digest, host=self.host)
        gamma = SetupInputs.parse(self.beta.as_input() | {
            "execution_user": "gamma-worker", "home": "/home/gamma-worker"})
        with self.assertRaises(SetupConflict):
            plan_additional(gamma, "gamma", host=self.host)
        receipt = self.host.path("/etc/boring-crocodile/ponds/beta/initial-setup.json")
        receipt.unlink()
        receipt.symlink_to(self.host.path("/etc/boring-crocodile/initial-setup.json"))
        with self.assertRaises(SetupConflict):
            plan_additional(self.beta, "beta", host=self.host)

    def test_combined_rendering_distinct_workers_and_shared_controller(self):
        digest = self.prepare()
        apply_additional(self.beta, "beta", digest, host=self.host)
        receipts = site_receipts(self.host)
        entries = [(self.inputs, ServiceInputs.parse(policy()), receipts["pond"]),
                   (self.beta, ServiceInputs.parse(policy()), receipts["beta"])]
        output = render_site(entries)
        path = self.root / "combined.toml"
        path.write_text(output["config.toml"])
        config = Settings.load(path)
        self.assertEqual(set(config.ponds), {"pond", "beta"})
        control = output["boring-crocodile-mcp.service"]
        self.assertIn("SupplementaryGroups=test-worker beta-worker", control)
        self.assertIn("LoadCredential=beta-approval-secret:", control)
        self.assertIn("boring-crocodile-worker-beta.service", control)
        self.assertIn("User=beta-worker", output["boring-crocodile-task-beta@.service"])
        self.assertIn("Slice=system-crocodilebeta.slice", output["boring-crocodile-task-beta@.service"])
        self.assertIn("SocketGroup=beta-worker", output["boring-crocodile-task-beta.socket"])
        self.assertIn("beta-worker beta-worker", output["boring-crocodile-beta.conf"])
        self.assertIn("--environment beta", output["boring-crocodile-worker-beta.service"])
        self.assertNotIn("User=test-worker", output["boring-crocodile-worker-beta.service"])
        with self.assertRaises(ValueError):
            render_site(entries + [entries[1]])

    def test_a_running_site_is_read_as_it_is_without_receipts(self):
        # Like an adopted site: config.toml and units, no setup receipt of the first Pond.
        self.prepare()
        self.host.path("/etc/boring-crocodile/initial-setup.json").unlink()
        self.host.path("/etc/boring-crocodile/config.toml").write_text("running site\n")
        plan = plan_additional(self.beta, "beta", host=self.host)
        self.assertTrue(plan["site"]["running"])
        self.assertEqual(plan["site"]["controller"]["name"], "test-control")
        outcome = apply_additional(self.beta, "beta", plan_digest(plan), host=self.host)
        self.assertEqual(outcome["status"], "complete")
        self.assertIn(self.host.groups["beta-worker"]["gid"], self.host.member_gids["test-control"])
        self.assertEqual(self.host.calls.count(("membership", "test-control", "beta-worker")), 1)

    def test_an_irregular_running_site_is_refused_before_changes(self):
        self.prepare()
        self.host.path("/etc/boring-crocodile/config.toml").write_text("running site\n")
        self.host.report = dict(self.host.report, status="irregular",
                                irregular=[{"what": "differs from the Crocodile's template", "path": "/x"}])
        before = list(self.host.calls)
        with self.assertRaisesRegex(SetupConflict, "site-check found"):
            plan_additional(self.beta, "beta", host=self.host)
        self.assertEqual(self.host.calls, before)
