"""Provisioning boundary logic; journeys use the same synthetic host."""
from dataclasses import replace
from functools import partial
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from boring_deploy import root_input
from boring_crocodile.setup import SetupInputs
from boring_crocodile import setup_apply as provision
from boring_crocodile.site_model import SiteDescription
from site_host import FakeHost


class ProvisionCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("boring_crocodile.setup.root_input",
                                side_effect=partial(root_input, owner=os.getuid())))
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.host = FakeHost(Path(self.directory.name), provisioned=False)
        self.inputs = SetupInputs.parse(dict(execution_user="demo-pond", control_user="demo-control",
            home="/home/demo-pond", context_mode="starter", context_source=None, context_files=[]))

    def provision(self):
        proposal = provision.plan(self.inputs, host=self.host)
        return provision.apply(self.inputs, proposal["plan_digest"], host=self.host)


class ProvisionLogicTest(ProvisionCase):
    def test_reception_is_provisioned_without_starting_worker_and_kept_on_rerun(self):
        before = self.host.group("pond-pirates")
        self.provision()
        self.assertIsNone(before)
        created = self.host.group("pond-pirates")
        self.assertGreater(created["gid"], 0)
        definition = "d /run/boring-crocodile/guests 0755 root root - -\n" + (
            "d /run/boring-crocodile/guests/pond 2750 demo-pond pond-pirates - -\n")
        self.assertIn(["/usr/bin/systemd-tmpfiles", "--create", "-"], self.host.calls)
        self.assertIn(["stdin", definition], self.host.calls)
        self.assertFalse(any("systemctl" in item for call in self.host.calls for item in call))
        site = SiteDescription.read(self.host.paths.config)
        self.assertNotIn("service_registration_enabled", site.ponds["pond"].document["execution"])
        self.provision()
        self.assertEqual(self.host.group("pond-pirates"), created)

    def test_preview_and_changed_import_are_read_only(self):
        source = self.host.root / "source"
        source.mkdir()
        (source / "nested").mkdir()
        document = source / "nested/AGREEMENT.md"
        document.write_text("reviewed")
        (source / "unselected").write_text("private")
        self.inputs = replace(self.inputs, context_mode="import", context_source=str(source),
                              context_files=("nested/AGREEMENT.md",))
        proposal = provision.plan(self.inputs, host=self.host)
        self.assertNotIn("reviewed", str(proposal))
        self.assertFalse(self.host.identities)
        document.write_text("changed")
        with self.assertRaisesRegex(ValueError, "changed"):
            provision.apply(self.inputs, proposal["plan_digest"], host=self.host)
        self.assertFalse(self.host.identities)
        self.provision()
        handbook = self.host.path(self.inputs.home) / "handbook"
        self.assertEqual((handbook / "nested/AGREEMENT.md").read_text(), "changed")
        self.assertFalse((handbook / "unselected").exists())

    def test_partial_accounts_and_empty_root_home_need_no_resume(self):
        actual = self.host.create_user
        def interrupted(identity):
            site = SiteDescription.read(self.host.paths.config)
            self.assertEqual(identity, site.controller if identity.name == "demo-control"
                             else site.ponds["pond"].identity)
            if identity.name == "demo-pond":
                raise OSError("account interruption")
            actual(identity)
        with patch.object(self.host, "create_user", interrupted), self.assertRaises(OSError):
            self.provision()
        self.assertEqual(set(self.host.identities), {"demo-control"})
        with patch.object(self.host, "chown", side_effect=OSError("chown interruption")), self.assertRaises(OSError):
            self.provision()
        self.provision()
        self.assertFalse(list(self.host.paths.state.rglob("*.json")))

    def test_identity_home_and_secret_conflicts_fail_before_writes(self):
        home = self.host.path(self.inputs.home)
        home.symlink_to(self.host.root / "etc")
        with self.assertRaises(ValueError):
            provision.plan(self.inputs, host=self.host)
        self.assertFalse(self.host.identities)
        self.assertFalse(self.host.paths.config.exists())
        home.unlink()
        self.provision()
        identity = self.host.identities["demo-pond"]
        self.host.identities[identity.name] = replace(identity, uid=identity.uid+100)
        with self.assertRaisesRegex(ValueError, "identity changed"):
            provision.plan(self.inputs, host=self.host)
        self.host.identities[identity.name] = identity
        with patch.object(self.host, "memberships", return_value=[identity.gid, 27]):
            with self.assertRaisesRegex(ValueError, "authority"):
                provision.plan(self.inputs, host=self.host)
        secret = self.host.path(SiteDescription.read(self.host.paths.config).ponds["pond"].credential_file)
        before = secret.read_bytes()
        secret.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "approval secret"):
            provision.plan(self.inputs, host=self.host)
        self.assertEqual(secret.read_bytes(), before)
        secret.chmod(0o600)
        secret.write_text("tiny\n")
        with self.assertRaisesRegex(ValueError, "at least 24 characters"):
            provision.plan(self.inputs, host=self.host)

    def test_every_publication_and_identity_cut_reuses_the_declaration(self):
        # Two declaration publications, four groups, two accounts; no journal.
        for cut in range(8):
            for after in (False, True):
                with self.subTest(cut=cut, after=after):
                    self.host = FakeHost(Path(self.directory.name) / f"cut-{cut}-{after}", provisioned=False)
                    actual_write, actual_group, actual_user = provision.write_file, self.host.create_group, self.host.create_user
                    count = 0

                    def effect(function, *args, **kwargs):
                        nonlocal count
                        selected = count == cut
                        count += 1
                        if selected and not after:
                            raise OSError("interrupted before effect")
                        result = function(*args, **kwargs)
                        if selected and after:
                            raise OSError("interrupted after effect")
                        return result

                    def publish(path, *args, **kwargs):
                        if path.name == "config.toml":
                            return effect(actual_write, path, *args, **kwargs)
                        return actual_write(path, *args, **kwargs)

                    def group(name, gid=None):
                        site = SiteDescription.read(self.host.paths.config)
                        self.assertFalse(self.host.active)
                        self.assertIn(name, {site.controller.group, site.ponds["pond"].identity.group, "pirate-clients", "pond-pirates"})
                        return effect(actual_group, name, gid)

                    with patch.object(provision, "write_file", publish), patch.object(
                            self.host, "create_group", group), patch.object(
                            self.host, "create_user", lambda identity: effect(actual_user, identity)):
                        with self.assertRaises(OSError):
                            self.provision()
                    declaration = self.host.paths.state / "ponds/pond/config.toml"
                    saved = declaration.read_bytes() if declaration.exists() else None
                    self.provision()
                    if saved:
                        self.assertEqual(declaration.read_bytes(), saved)
                    site = SiteDescription.read(self.host.paths.config)
                    self.assertEqual(self.host.identities, {site.controller.name: site.controller,
                                                           site.ponds["pond"].identity.name: site.ponds["pond"].identity})
                    self.assertFalse(list(self.host.paths.state.rglob("*.json")))

    def test_occupied_ids_and_undeclared_names_are_not_adopted(self):
        from boring_crocodile.site_model import Identity
        for conflict in ("uid", "gid", "user-name", "group-name"):
            with self.subTest(conflict=conflict):
                self.host = FakeHost(Path(self.directory.name) / conflict, provisioned=False)
                proposal = provision.plan(self.inputs, host=self.host)
                worker = Identity.parse(proposal["accounts"]["worker"]["identity"])
                if conflict == "uid":
                    self.host.identities["unrelated"] = replace(worker, name="unrelated", group="unrelated")
                elif conflict == "gid":
                    self.host.extra_groups["unrelated"] = {"name": "unrelated", "gid": worker.gid}
                elif conflict == "user-name":
                    self.host.identities[worker.name] = worker
                else:
                    self.host.extra_groups[worker.group] = {"name": worker.group, "gid": worker.gid}
                with self.assertRaises(ValueError):
                    provision.apply(self.inputs, proposal["plan_digest"], host=self.host)
                self.assertFalse(self.host.paths.config.exists())
                if conflict in {"user-name", "group-name"}:
                    with self.assertRaisesRegex(ValueError, "Undeclared"):
                        provision.plan(self.inputs, host=self.host)

        # Once a binding is published, its IDs cannot be reassigned on retry.
        for conflict in ("uid", "gid"):
            with self.subTest(declared_conflict=conflict):
                self.host = FakeHost(Path(self.directory.name) / ("declared-"+conflict), provisioned=False)
                with patch.object(self.host, "create_group", side_effect=OSError("interrupted")):
                    with self.assertRaises(OSError):
                        self.provision()
                site = SiteDescription.read(self.host.paths.config)
                worker = site.ponds["pond"].identity
                before = self.host.paths.config.read_bytes()
                if conflict == "uid":
                    self.host.identities["unrelated"] = replace(worker, name="unrelated", group="unrelated")
                else:
                    self.host.extra_groups["unrelated"] = {"name": "unrelated", "gid": worker.gid}
                with self.assertRaisesRegex(ValueError, "occupied"):
                    provision.plan(self.inputs, host=self.host)
                self.assertEqual(self.host.paths.config.read_bytes(), before)

    def test_native_id_limits_and_explicit_account_requests(self):
        from boring_crocodile.site_host import Host
        login = self.host.path("/etc/login.defs")
        login.write_text("UID_MIN 2000\nGID_MIN 3000\nSYS_UID_MIN 500\nSYS_UID_MAX 503\n")
        self.assertEqual(list(Host.system_id_range(self.host, "UID")), [503, 502, 501, 500])
        self.assertEqual((Host.system_id_range(self.host, "GID")[0],
                          Host.system_id_range(self.host, "GID")[-1]), (2999, 101))
        with patch.object(self.host, "system_id_range", return_value=range(0)):
            with self.assertRaisesRegex(ValueError, "No free system UID"):
                provision.plan(self.inputs, host=self.host)
        login.write_text("SYS_UID_MIN 600\nSYS_UID_MAX 500\n")
        with self.assertRaisesRegex(ValueError, "Invalid system UID"):
            Host.system_id_range(self.host, "UID")
        proposal = provision.plan(self.inputs, host=self.host)
        from boring_crocodile.site_model import Identity
        identity = Identity.parse(proposal["accounts"]["worker"]["identity"])
        with patch.object(self.host, "command") as command:
            Host.create_group(self.host, identity.group, identity.gid)
            Host.create_user(self.host, identity)
        group_command, user_command = [call.args[0] for call in command.call_args_list]
        self.assertEqual(group_command[group_command.index("--gid")+1], str(identity.gid))
        self.assertEqual(user_command[user_command.index("--gid")+1], str(identity.gid))
        self.assertEqual(user_command[user_command.index("--uid")+1], str(identity.uid))
        self.assertNotIn("--non-unique", user_command)
        self.assertFalse(self.host.identities)
        for role in ("execution_user", "control_user"):
            inputs = replace(self.inputs, **{role: "pirate-clients"},
                             **({"home": "/home/pirate-clients"} if role == "execution_user" else {}))
            with self.assertRaisesRegex(ValueError, "role-group"):
                provision.plan(inputs, host=self.host)
