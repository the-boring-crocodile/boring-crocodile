"""Pure typed-description rendering and retained socket/name boundary assertions."""
from copy import deepcopy
import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from boring_crocodile.site_model import SiteDescription
from boring_crocodile.site_policy import ServiceInputs
from boring_crocodile.site_templates import render_description


def policy():
    return dict(public_base_url="https://mcp.example.test", bind_host="127.0.0.1",
        bind_port=8503, runtime_root="/opt/example/runtime",
        memory_bytes=2147483648, swap_bytes=0, tasks_max=256, task_tasks_max=128)


def description(options=None, *, home="/home/demo-worker", display_name="The Pond"):
    p = ServiceInputs.parse(options or policy())
    def identity(name, uid, home):
        return dict(name=name, uid=uid, group=name, gid=uid, home=home, shell="/usr/sbin/nologin")
    public = urlsplit(p.public_base_url)
    bind = "["+p.bind_host+"]" if ":" in p.bind_host else p.bind_host
    main = dict(site=dict(schema=1, state="running", runtime_root=p.runtime_root,
                         controller=identity("demo-control", 1202, "/nonexistent")),
        server=dict(public_base_url=p.public_base_url, bind_host=p.bind_host, bind_port=p.bind_port,
                    protected_ports=list(p.protected_ports), allowed_hosts=[public.netloc, bind+":"+str(p.bind_port)]),
        auth=dict(oauth_database_file="/var/lib/boring-crocodile/oauth.sqlite3"))
    pond = dict(enabled=True, identity=identity("demo-worker", 1201, home),
        credential_file="/etc/boring-crocodile/credentials/approval-secret",
        limits={key: getattr(p,key) for key in ("memory_bytes", "swap_bytes", "tasks_max", "task_tasks_max")},
        pond=dict(profile_id="11111111-1111-4111-8111-111111111111", display_name=display_name,
                  files=["HANDBOOK.md"], welcome_file="WELCOME.md"),
        execution=dict(no_new_privileges=p.no_new_privileges,
                       files={"roots": {"home": {"read": True, "write": True}}}))
    return SiteDescription.parse(main, {"pond": pond})


class ServicesTest(unittest.TestCase):
    def test_units_and_runtime_share_explicit_identities_limits_and_roots(self):
        site = description()
        output = render_description(site)
        config = site.runtime_settings()
        env = config.environments["pond"]
        self.assertEqual(env.control_plane_uid, 1202)
        self.assertEqual(env.default_cwd, Path("/home/demo-worker"))
        self.assertEqual(config.ponds["pond"].welcome_file, "WELCOME.md")
        self.assertEqual(env.files.roots["home"].path, env.default_cwd)
        self.assertTrue(env.files.roots["home"].read and env.files.roots["home"].write)
        required = {
            "boring-crocodile-worker-pond.service": ("User=demo-worker", "Group=demo-worker",
                                                    "Slice=system-crocodilepond.slice"),
            "boring-crocodile-mcp.service": ("User=demo-control", "ProtectHome=yes"),
            "boring-crocodile-task-pond@.service": ("User=demo-worker", "TasksMax=128"),
            "system-crocodilepond.slice": ("MemoryMax=2147483648", "MemorySwapMax=0"),
            "boring-crocodile.conf": ("workers/pond 0750 demo-worker demo-worker",),
        }
        for name, lines in required.items():
            for line in lines:
                self.assertIn(line, output[name])
        self.assertNotIn("SupplementaryGroups=", output["boring-crocodile-worker-pond.service"])
        self.assertNotIn("ReadOnlyPaths=", output["boring-crocodile-mcp.service"])
        self.assertNotIn("MaxConnections=", output["boring-crocodile-task-pond.socket"])
        self.assertIn("/opt/example/runtime/bin/", output["boring-crocodile-mcp.service"])
        self.assertNotIn("/opt/boring-crocodile-deb", str(output))

    def test_credentials_registration_and_custom_home(self):
        site = description(home="/srv/ponds/demo-worker")
        output = render_description(site)
        self.assertEqual(str(site.runtime_settings().ponds["pond"].approval_secret_file),
                         "/run/credentials/boring-crocodile-mcp.service/approval-secret")
        self.assertIn("LoadCredential=approval-secret:/etc/boring-crocodile/credentials/approval-secret",
                      output["boring-crocodile-mcp.service"])
        tmpfiles = output["boring-crocodile.conf"]
        self.assertIn("d /run/boring-crocodile/guests/pond 2750 demo-worker pond-pirates - -\n", tmpfiles)
        self.assertLess(tmpfiles.index("d /run/boring-crocodile 0755"), tmpfiles.index("guests 0755"))
        self.assertIn("ReadWritePaths=/run/boring-crocodile/guests/pond ", output["boring-crocodile-worker-pond.service"])
        self.assertIn("ReadWritePaths=/srv/ponds/demo-worker", output["boring-crocodile-task-pond@.service"])
        self.assertNotIn("mcp-services", str(output))
        self.assertNotIn("approval-token", str(output))

    def test_profile_display_context_and_stop_propagation(self):
        site = description(display_name="The Steady Beaver")
        settings = site.runtime_settings()
        self.assertEqual(settings.ponds["pond"].profile_id, site.ponds["pond"].document["pond"]["profile_id"])
        self.assertEqual(settings.ponds["pond"].display_name, "The Steady Beaver")
        self.assertEqual(settings.ponds["pond"].context_root, Path("/home/demo-worker/handbook"))
        output = render_description(site)
        self.assertEqual(output, render_description(site))
        target = output["boring-crocodile-mcp.target"]
        self.assertIn("Requires=boring-crocodile-mcp.service", target)
        self.assertIn("ConditionPathExists=/etc/boring-crocodile/config.toml", target)
        self.assertNotIn("/opt/", target)
        for name, body in output.items():
            if name.endswith((".service", ".socket", ".slice")):
                self.assertIn("PartOf=boring-crocodile-mcp.target", body, name)

    def test_rejects_unit_injection_and_invalid_policy(self):
        for change in [
            {"runtime_root":"/opt/a\nUser=root"}, {"runtime_root":"/opt/../etc"},
            {"bind_host":"localhost"}, {"bind_port":True},
            {"public_base_url":"http://example.test"},
            {"public_base_url":"https://user:pass@example.test"},
            {"public_base_url":"https://example.test/mcp"},
            {"public_base_url":"https://example.test:99999"},
            {"task_tasks_max":512}, {"memory_bytes":-1}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                ServiceInputs.parse(policy() | change)
        site = description(policy() | dict(public_base_url="https://[::1]:8443", bind_host="::1"))
        self.assertIn("[::1]:8443", site.settings.allowed_hosts)
        self.assertIn("[::1]:8503", site.settings.allowed_hosts)


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
    def test_names_and_the_budget_slice(self):
        import re
        from boring_crocodile.site_layout import POND_NAME, budget_slice
        from boring_crocodile.setup_apply import SetupConflict, pond_name
        self.assertEqual(budget_slice("rose-garden"), "system-crocodilerose_garden.slice")
        self.assertEqual(budget_slice("pond"), "system-crocodilepond.slice")
        self.assertEqual(pond_name("rose-garden"), "rose-garden")
        for bad in ("pond_x", "-rose", "rose-", "Rose", "a"*25, "pond"):
            with self.assertRaises(SetupConflict):
                pond_name(bad)
        self.assertTrue(re.fullmatch(POND_NAME, "a"))

    def test_a_hyphenated_pond_renders_one_level_below_system_slice(self):
        site = description()
        ponds = {name: deepcopy(p.document) for name,p in site.ponds.items()}
        second = deepcopy(ponds["pond"])
        second["identity"].update(name="rose-worker", group="rose-worker", uid=1203, gid=1203, home="/home/rose-worker")
        second["credential_file"] = "/etc/boring-crocodile/credentials/rose-garden-approval-secret"
        second["pond"].update(profile_id="22222222-2222-4222-8222-222222222222")
        ponds["rose-garden"] = second
        site = SiteDescription.parse(site.document, ponds)
        output = render_description(site)
        slice_ = "system-crocodilerose_garden.slice"
        self.assertIn(slice_, output)
        self.assertNotIn("system-crocodilerose-garden.slice", output)
        for unit in ("task-rose-garden@.service", "worker-rose-garden.service"):
            self.assertIn("Slice="+slice_, output["boring-crocodile-"+unit])
        self.assertIn("boring-crocodile-rose-garden.conf", output)
        self.assertEqual(site.runtime_settings().environments["rose-garden"].task_cgroup_parent,
                         Path("/system.slice")/slice_)
