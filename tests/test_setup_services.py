import tempfile
import os
import socket
import stat
import unittest
from pathlib import Path
from boring_crocodile.setup import SetupInputs
from boring_crocodile.setup_services import ServiceInputs, render
from boring_crocodile.settings import Settings

def inputs():
    return SetupInputs.parse(dict(execution_user="demo-worker",control_user="demo-control",
        home="/home/demo-worker",context_mode="starter",
        context_source=None,context_files=[]))

def policy():
    return dict(public_base_url="https://mcp.example.test",bind_host="127.0.0.1",
        bind_port=8503,runtime_root="/opt/example/runtime",
        memory_bytes=2147483648,swap_bytes=0,tasks_max=256,task_tasks_max=128)

def receipt():
    s=inputs()
    return dict(status="complete",inputs=s.as_input(),
        profile_id="11111111-1111-4111-8111-111111111111",
        users={"demo-worker":{"uid":1201},"demo-control":{"uid":1202}},
        context_root="/home/demo-worker/handbook",
        documents=[{"filename":"HANDBOOK.md"}])

class ServicesTest(unittest.TestCase):
    def test_config_and_units_share_identities_limits_and_context(self):
        output=render(inputs(),ServiceInputs.parse(policy()),receipt())
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"config.toml";path.write_text(output["config.toml"])
            config=Settings.load(path)
        env=config.environments["pond"]
        self.assertEqual(env.control_plane_uid,1202)
        self.assertEqual(env.default_cwd,Path("/home/demo-worker"))
        self.assertNotIn("MaxConnections=",output["boring-crocodile-task-pond.socket"])
        self.assertIn("User=demo-worker",output["boring-crocodile-task-pond@.service"])
        self.assertIn("\nGroup=demo-worker\n",output["boring-crocodile-worker-pond.service"])
        self.assertNotIn("SupplementaryGroups=",output["boring-crocodile-worker-pond.service"])
        self.assertIn("workers/pond 0750 demo-worker demo-worker", output["boring-crocodile.conf"])
        self.assertIn("User=demo-control",output["boring-crocodile-mcp.service"])
        self.assertNotIn("ReadOnlyPaths=",output["boring-crocodile-mcp.service"])
        self.assertIn("ProtectHome=yes",output["boring-crocodile-mcp.service"])
        self.assertEqual(config.ponds["pond"].welcome_file, "WELCOME.md")
        self.assertIn("MemoryMax=2147483648",output["system-crocodilepond.slice"])
        self.assertIn("MemorySwapMax=0",output["system-crocodilepond.slice"])
        self.assertIn("TasksMax=128",output["boring-crocodile-task-pond@.service"])
        self.assertIn("demo-worker demo-worker",output["boring-crocodile.conf"])
        self.assertIn("/opt/example/runtime/bin/",output["boring-crocodile-mcp.service"])
        self.assertNotIn("/opt/boring-crocodile-deb",str(output))
    def test_new_pond_gets_its_home_as_file_root(self):
        # Without a root a fresh Pond had commands
        # but no hash-checked file tools.
        output=render(inputs(),ServiceInputs.parse(policy()),receipt())
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"config.toml";path.write_text(output["config.toml"])
            roots=Settings.load(path).environments["pond"].files.roots
        self.assertEqual(set(roots),{"home"})
        self.assertEqual(roots["home"].path,Path("/home/demo-worker"))
        self.assertTrue(roots["home"].read and roots["home"].write)

    def test_approval_secret_credential_names(self):
        output=render(inputs(),ServiceInputs.parse(policy()),receipt())
        self.assertIn('approval_secret_file = "/run/credentials/boring-crocodile-mcp.service/approval-secret"',
                      output["config.toml"])
        self.assertIn("LoadCredential=approval-secret:/etc/boring-crocodile/credentials/approval-secret",
                      output["boring-crocodile-mcp.service"])
        self.assertNotIn("approval-token", str(output))

    def test_guest_registration_directory_rendering(self):
        output=render(inputs(),ServiceInputs.parse(policy()|{"service_registration_enabled":True}),receipt())
        tmpfiles=output["boring-crocodile.conf"]
        self.assertIn("d /run/boring-crocodile/guests 0755 root root - -\n", tmpfiles)
        self.assertIn("d /run/boring-crocodile/guests/pond 2750 demo-worker pond-pirates - -\n", tmpfiles)
        self.assertLess(tmpfiles.index("d /run/boring-crocodile 0755"), tmpfiles.index("guests 0755"))
        self.assertIn("ReadWritePaths=/run/boring-crocodile/guests/pond ",
                      output["boring-crocodile-worker-pond.service"])
        self.assertNotIn("mcp-services", str(output))
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"config.toml";path.write_text(output["config.toml"])
            self.assertTrue(Settings.load(path).environments["pond"].service_registration_enabled)

    def test_nonstandard_home_is_allowed_in_hardened_units(self):
        selected = SetupInputs.parse(inputs().as_input() | {"home": "/srv/ponds/demo-worker"})
        result = render(selected, ServiceInputs.parse(policy()),
                        receipt() | {"inputs": selected.as_input(), "context_root": selected.home + "/handbook"})
        self.assertIn("ReadWritePaths=/srv/ponds/demo-worker",
                      result["boring-crocodile-task-pond@.service"])

    def test_site_target_owns_stop_propagation_without_runtime_path_dependency(self):
        output=render(inputs(),ServiceInputs.parse(policy()),receipt())
        target=output["boring-crocodile-mcp.target"]
        self.assertIn("Requires=boring-crocodile-mcp.service",target)
        self.assertIn("ConditionPathExists=/etc/boring-crocodile/config.toml",target)
        self.assertNotIn("/opt/",target)
        for name,body in output.items():
            if name.endswith((".service",".socket",".slice")):
                self.assertIn("PartOf=boring-crocodile-mcp.target",body,name)

    def test_receipt_renders_its_persistent_profile(self):
        from uuid import uuid4
        r = receipt() | {"profile_id": str(uuid4())}
        old = receipt()
        del old["profile_id"]
        with self.assertRaisesRegex(ValueError, "persistent profile ID"):
            render(inputs(), ServiceInputs.parse(policy()), old)
        first = render(inputs(), ServiceInputs.parse(policy()), r)
        self.assertEqual(first, render(inputs(), ServiceInputs.parse(policy()), r))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text(first["config.toml"])
            config = Settings.load(path)
        self.assertEqual(config.ponds["pond"].profile_id, r["profile_id"])
        self.assertEqual(config.ponds["pond"].environment, "pond")
        self.assertEqual(config.ponds["pond"].context_root, Path(r["context_root"]))

    def test_a_display_name_from_the_inputs_is_what_people_see(self):
        named = SetupInputs.parse(inputs().as_input() | {"display_name": "The Steady Beaver"})
        r = receipt() | {"inputs": named.as_input()}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text(render(named, ServiceInputs.parse(policy()), r)["config.toml"])
            self.assertEqual(Settings.load(path).ponds["pond"].display_name, "The Steady Beaver")
            path.write_text(render(inputs(), ServiceInputs.parse(policy()), receipt())["config.toml"])
            self.assertEqual(Settings.load(path).ponds["pond"].display_name, "The Pond")
        self.assertNotIn("display_name", inputs().as_input())  # older receipts still match
        for bad in ("", " padded", "two\nlines", "x" * 65, 7):
            with self.assertRaisesRegex(ValueError, "display_name"):
                SetupInputs.parse(inputs().as_input() | {"display_name": bad})

    def test_rejects_unit_injection_and_invalid_policy(self):
        for change in [
            {"runtime_root":"/opt/a\nUser=root"},{"runtime_root":"/opt/../etc"},
            {"bind_host":"localhost"},{"bind_port":True},
            {"public_base_url":"http://example.test"},
            {"public_base_url":"https://user:pass@example.test"},
            {"public_base_url":"https://example.test/mcp"},
            {"public_base_url":"https://example.test:99999"},
            {"task_tasks_max":512},{"memory_bytes":-1}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                ServiceInputs.parse(policy()|change)
    def test_requires_complete_matching_nonroot_setup(self):
        for change in [{"status":"prepared"},{"inputs":{}},
                       {"context_root":"/home/demo-worker"}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                render(inputs(),ServiceInputs.parse(policy()),receipt()|change)
        for uid in (0,1201):
            r=receipt();r["users"]["demo-control"]["uid"]=uid
            with self.assertRaises(ValueError):
                render(inputs(),ServiceInputs.parse(policy()),r)
    def test_rejects_context_escape(self):
        r=receipt();r["documents"]=[{"filename":"../private"}]
        with self.assertRaises(ValueError):
            render(inputs(),ServiceInputs.parse(policy()),r)
    def test_ipv6_and_explicit_port(self):
        p=ServiceInputs.parse(policy()|dict(public_base_url="https://[::1]:8443",bind_host="::1"))
        output=render(inputs(),p,receipt())
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"c";path.write_text(output["config.toml"])
            c=Settings.load(path)
        self.assertIn("[::1]:8443",c.allowed_hosts)
        self.assertIn("[::1]:8503",c.allowed_hosts)

class SocketIdentityTest(unittest.TestCase):
    def test_socket_and_files_use_account_group_after_socket_recreation(self):
        with tempfile.TemporaryDirectory() as tmp:
            regular = Path(tmp) / "ordinary-file"
            regular.write_text("synthetic")
            self.assertEqual(regular.stat().st_gid, os.getegid())
            run = Path(tmp) / "worker"
            run.mkdir(mode=0o750)
            self.assertFalse(run.stat().st_mode & stat.S_ISGID)
            path = run / "control.sock"
            for _ in range(2):
                with socket.socket(socket.AF_UNIX) as server:
                    server.bind(str(path))
                    os.chmod(path, 0o660)
                    server.listen(1)
                    self.assertEqual(path.stat().st_uid, os.geteuid())
                    self.assertEqual(path.stat().st_gid, os.getegid())
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o660)
                    with socket.socket(socket.AF_UNIX) as client:
                        client.connect(str(path))
                        accepted, _ = server.accept()
                        accepted.close()
                path.unlink()


class HyphenatedPondTest(unittest.TestCase):
    """A Pond name may contain hyphens; its slice stays a direct system.slice child."""

    def test_names_and_the_budget_slice(self):
        import re
        from boring_crocodile.site_layout import POND_NAME, budget_slice
        from boring_crocodile.setup_apply import SetupConflict, pond_name
        self.assertEqual(budget_slice("rose-garden"), "system-crocodilerose_garden.slice")
        self.assertEqual(budget_slice("pond"), "system-crocodilepond.slice")
        self.assertEqual(pond_name("rose-garden"), "rose-garden")
        for bad in ("pond_x", "-rose", "rose-", "Rose", "a" * 25, "pond"):
            with self.assertRaises(SetupConflict):
                pond_name(bad)
        self.assertTrue(re.fullmatch(POND_NAME, "a"))

    def test_a_hyphenated_pond_renders_one_level_below_system_slice(self):
        from boring_crocodile.site_layout import budget_slice
        r = receipt() | {"pond_name": "rose-garden"}
        output = render(inputs(), ServiceInputs.parse(policy()), r)
        slice_ = budget_slice("rose-garden")
        self.assertIn(slice_, output)
        self.assertNotIn("system-crocodilerose-garden.slice", output)
        self.assertIn("Slice=" + slice_, output["boring-crocodile-task-rose-garden@.service"])
        self.assertIn("boring-crocodile-worker-rose-garden.service", output)
        self.assertIn("boring-crocodile-rose-garden.conf", output)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text(output["config.toml"])
            env = Settings.load(path).environments["rose-garden"]
        self.assertEqual(env.task_cgroup_parent, Path("/system.slice") / slice_)


if __name__ == "__main__":
    unittest.main()
