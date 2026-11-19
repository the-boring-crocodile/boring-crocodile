"""Create-once provisioning and interrupted seeding on the shared fake host."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from boring_crocodile.setup import SetupInputs
from boring_crocodile import setup_apply as provision, site_render, site_setup
from boring_crocodile.site_model import SiteDescription
from site_host import FakeHost


class ProvisionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.host = FakeHost(Path(self.directory.name), provisioned=False)
        self.inputs = SetupInputs.parse(dict(execution_user="demo-pond", control_user="demo-control",
            home="/home/demo-pond", context_mode="starter", context_source=None, context_files=[]))

    def provision(self, inputs=None, name="pond"):
        inputs = inputs or self.inputs
        proposal = provision.plan(inputs, name, host=self.host)
        return provision.apply(inputs, proposal["plan_digest"], name=name, host=self.host)

    def test_provision_repeat_add_and_render_preserve_identity_and_data(self):
        self.provision()
        site = SiteDescription.read(self.host.paths.config)
        self.assertEqual((site.state, site.ponds["pond"].enabled), ("stopped", False))
        self.assertFalse(self.host.active)
        self.assertFalse(list(self.host.paths.state.rglob("*.json")))
        pond = site.ponds["pond"]
        secret = self.host.path(pond.credential_file).read_bytes()
        handbook = self.host.path(pond.identity.home) / "handbook/HANDBOOK.md"
        handbook.write_text("Our evolved agreement")
        self.provision()
        repeated = SiteDescription.read(self.host.paths.config)
        self.assertEqual(repeated.ponds["pond"].pond.profile_id, pond.pond.profile_id)
        self.assertEqual(self.host.path(pond.credential_file).read_bytes(), secret)
        self.assertEqual(handbook.read_text(), "Our evolved agreement")
        garden = replace(self.inputs, execution_user="demo-garden", home="/home/demo-garden")
        self.provision(garden, "garden")
        both = SiteDescription.read(self.host.paths.config)
        self.assertNotEqual(both.ponds["garden"].pond.profile_id, pond.pond.profile_id)
        self.assertFalse(both.ponds["garden"].enabled)
        policy = replace(both.ponds["pond"].policy, public_base_url="https://example.test")
        proposal, _ = site_setup.plan(policy, paths=self.host.paths, host=self.host)
        site_setup.apply(policy, proposal["plan_digest"], paths=self.host.paths, host=self.host)
        self.assertNotIn("boring-crocodile-worker-garden.service", self.host.active)
        both = SiteDescription.read(self.host.paths.config)
        main = dict(both.document)
        main["site"] = {**main["site"], "state": "running"}
        documents = {name: p.document | {"enabled": True} for name,p in both.ponds.items()}
        candidate = SiteDescription.parse(main, documents)
        proposal, _ = site_render.plan(paths=self.host.paths, host=self.host, candidate=candidate)
        site_render.apply(proposal["plan_digest"], paths=self.host.paths, host=self.host, candidate=candidate)
        self.assertIn("boring-crocodile-worker-garden.service", self.host.active)
        self.assertEqual(handbook.read_text(), "Our evolved agreement")
        self.assertEqual(self.host.path(pond.credential_file).read_bytes(), secret)

    def test_each_packaged_handbook_seeds_once_without_retaining_choice(self):
        for mode in ("starter", "engineer", "consultant"):
            with self.subTest(mode=mode):
                self.host = FakeHost(Path(self.directory.name)/mode, provisioned=False)
                inputs = replace(self.inputs, context_mode=mode, display_name="Our Pond")
                self.provision(inputs)
                site = SiteDescription.read(self.host.paths.config)
                pond = site.ponds["pond"]
                self.assertEqual(pond.pond.files, ("HANDBOOK.md", "WELCOME.md", "OPERATING_TOGETHER.md"))
                self.assertEqual(pond.pond.welcome_file, "WELCOME.md")
                self.assertEqual(pond.pond.display_name, "Our Pond")
                self.assertNotIn("context_mode", pond.document)
                self.assertNotIn("display_name", pond.document["execution"])
                folder = self.host.path(pond.identity.home)/"handbook"
                (folder/"HANDBOOK.md").write_text("Our evolved agreement")
                before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.iterdir()}
                self.provision(inputs)
                self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.iterdir()})

    def test_seed_interruption_and_link_conflict_keep_saved_identity(self):
        self.host.fail = "seed"
        with self.assertRaisesRegex(OSError, "seed interruption"):
            self.provision()
        site = SiteDescription.read(self.host.paths.config)
        pond = site.ponds["pond"]
        secret = self.host.path(pond.credential_file).read_bytes()
        self.provision()
        self.assertEqual(SiteDescription.read(self.host.paths.config).ponds["pond"].pond.profile_id,
                         pond.pond.profile_id)
        self.assertEqual(self.host.path(pond.credential_file).read_bytes(), secret)
        handbook = self.host.path(pond.identity.home) / "handbook/HANDBOOK.md"
        other = handbook.parent / "keep"
        other.write_text("must survive")
        handbook.unlink()
        handbook.symlink_to(other)
        import subprocess
        with self.assertRaises(subprocess.CalledProcessError):
            self.provision()
        self.assertTrue(handbook.is_symlink())
        self.assertEqual(other.read_text(), "must survive")
