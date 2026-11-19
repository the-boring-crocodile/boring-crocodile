"""Reactivation is another desired-description edit through the same renderer."""
from dataclasses import replace
from copy import deepcopy
from boring_crocodile import site_setup, site_render
from boring_crocodile.site_model import SiteDescription
import test_setup_apply as fixtures


class ReactivationTest(fixtures.ProvisionCase):
    def test_parked_site_returns_without_rotating_identity_or_secret(self):
        self.provision()
        original = SiteDescription.read(self.host.paths.config)
        pond = original.ponds["pond"]
        secret = self.host.path(pond.credential_file).read_bytes()
        policy = replace(pond.policy, public_base_url="https://example.test")
        proposal, _ = site_setup.plan(policy, paths=self.host.paths, host=self.host)
        site_setup.apply(policy, proposal["plan_digest"], paths=self.host.paths, host=self.host)
        running = SiteDescription.read(self.host.paths.config)
        main = deepcopy(running.document)
        main["site"]["state"] = "stopped"
        stopped = SiteDescription.parse(main, {n:p.document for n,p in running.ponds.items()})
        proposal, _ = site_render.plan(paths=self.host.paths, host=self.host, candidate=stopped)
        site_render.apply(proposal["plan_digest"], paths=self.host.paths, host=self.host, candidate=stopped)
        proposal, _ = site_setup.plan(policy, paths=self.host.paths, host=self.host)
        site_setup.apply(policy, proposal["plan_digest"], paths=self.host.paths, host=self.host)
        self.assertEqual(SiteDescription.read(self.host.paths.config).ponds["pond"].pond.profile_id,
                         pond.pond.profile_id)
        self.assertEqual(self.host.path(pond.credential_file).read_bytes(), secret)
        self.assertFalse(list(self.host.paths.state.rglob("*.json")))
