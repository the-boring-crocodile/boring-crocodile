"""Description logic. Complete apply journeys will share the new host fixture."""
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from boring_crocodile.settings import Settings
from boring_crocodile.site_model import DescriptionError, SiteDescription, toml_document
from boring_crocodile.site_templates import render_description


def description():
    def identity(name, uid, home):
        return dict(name=name, uid=uid, group=name, gid=uid, home=home, shell="/usr/sbin/nologin")
    main = dict(site=dict(schema=1, state="running", runtime_root="/opt/boring-crocodile/current",
                          controller=identity("demo-control", 1200, "/nonexistent")),
                server=dict(public_base_url="https://example.test", bind_host="127.0.0.1",
                            bind_port=8503, allowed_hosts=["example.test"], protected_ports=[19999]),
                auth=dict(oauth_database_file="/var/lib/boring-crocodile/oauth.sqlite3"),
                pirate_services=dict(callers={"parrot": {"uid": 1300, "ponds": []}}))
    ponds = {}
    for i, name in enumerate(("pond", "garden"), 1):
        home = "/home/demo-" + name
        ponds[name] = dict(
            enabled=name == "pond", identity=identity("demo-" + name, 1200+i, home),
            credential_file="/etc/boring-crocodile/credentials/" + name + "-approval-secret",
            limits=dict(memory_bytes=i*1024**3, swap_bytes=0, tasks_max=256, task_tasks_max=128),
            pond=dict(profile_id=f"11111111-1111-4111-8111-{i:012}", display_name=name,
                      files=["HANDBOOK.md"], welcome_file="WELCOME.md",
                      projects={"work": {"files": ["PROJECT.md"]}}),
            execution=dict(files={"roots": {"home": {"read": True, "write": True}}}))
    return main, ponds


class SiteModelTest(unittest.TestCase):
    def test_home_defaults_and_one_display_name_are_derived_once(self):
        main, ponds = description()
        ponds["pond"]["pond"]["display_name"] = "The Steady Beaver"
        site = SiteDescription.parse(main, ponds)
        pond = site.ponds["pond"]
        env = pond.execution
        self.assertEqual(pond.pond.context_root, Path(pond.identity.home) / "handbook")
        self.assertEqual((env.default_cwd, env.allowed_roots, env.files.roots["home"].path),
                         (Path(pond.identity.home), (Path(pond.identity.home),), Path(pond.identity.home)))
        self.assertEqual(env.display_name, pond.pond.display_name)
        self.assertIs(site.settings.environments["pond"], env)
        self.assertIs(site.settings.ponds["pond"], pond.pond)
        self.assertNotIn("context_root", pond.document["pond"])
        self.assertNotIn("allowed_roots", pond.document["execution"])
        self.assertNotIn("path", pond.document["execution"]["files"]["roots"]["home"])
        self.assertFalse(hasattr(Settings, "from_mapping"))

    def test_custom_paths_rights_limits_and_empty_file_policy_are_preserved(self):
        main, ponds = description()
        document = ponds["pond"]
        document["pond"]["context_root"] = "/srv/project-context"
        document["execution"].update(default_cwd="/srv/project-work",
            allowed_roots=["/srv/project-work"], output_limit_bytes=2048,
            files={"roots": {"home": {"read": True, "write": False, "deny": ["private"]},
                             "project": {"path": "/srv/project-work", "read": True, "write": True}},
                   "limits": {"max_file_bytes": 1024}})
        pond = SiteDescription.parse(main, ponds).ponds["pond"]
        self.assertEqual(pond.pond.context_root, Path("/srv/project-context"))
        self.assertEqual(pond.execution.default_cwd, Path("/srv/project-work"))
        self.assertEqual(pond.execution.allowed_roots, (Path("/srv/project-work"),))
        self.assertEqual(pond.execution.output_limit_bytes, 2048)
        self.assertFalse(pond.execution.files.roots["home"].write)
        self.assertEqual(pond.execution.files.roots["project"].path, Path("/srv/project-work"))
        self.assertEqual(pond.execution.files.limits["max_file_bytes"], 1024)
        self.assertEqual(pond.document, document)  # no derived keys inserted into the source
        del document["execution"]["files"]
        self.assertEqual(SiteDescription.parse(main, ponds).ponds["pond"].execution.files.roots, {})


    def test_unknown_keys_name_the_pond_and_owning_table(self):
        for section in (None, "identity", "limits", "pond", "execution"):
            main, ponds = description()
            target = ponds["garden"] if section is None else ponds["garden"][section]
            target["display_nmae"] = "typo"
            expected = "ponds.garden" + ("." + section if section else "")
            with self.subTest(section=section):
                with self.assertRaises(ValueError) as caught:
                    SiteDescription.parse(main, ponds)
                self.assertTrue(str(caught.exception).startswith(expected + ":"), str(caught.exception))
                self.assertIn("display_nmae", str(caught.exception))

    def test_redundant_keys_are_rejected_with_precise_paths(self):
        for section, key, value, path in (
                ("pond", "context_root", "/home/demo-pond/handbook", "pond.context_root"),
                ("execution", "default_cwd", "/home/demo-pond", "execution.default_cwd"),
                ("execution", "allowed_roots", ["/home/demo-pond"], "execution.allowed_roots"),
                ("execution", "display_name", "pond", "execution.display_name"),
                ("execution", "display_name", "conflicting name", "execution.display_name")):
            main, ponds = description()
            ponds["pond"][section][key] = value
            with self.subTest(path=path, value=value), self.assertRaises(ValueError) as caught:
                SiteDescription.parse(main, ponds)
            self.assertIn("ponds.pond." + path, str(caught.exception))
            self.assertIn("adopt-site.py", str(caught.exception))
        for home in ("/home/demo-pond", "/srv/other"):
            main, ponds = description()
            ponds["pond"]["execution"]["files"]["roots"]["home"]["path"] = home
            with self.subTest(home=home), self.assertRaises(ValueError) as caught:
                SiteDescription.parse(main, ponds)
            self.assertIn("ponds.pond.execution.files.roots.home.path", str(caught.exception))

    def test_stable_explicit_values_and_parked_runtime_projection(self):
        main, ponds = description()
        first = SiteDescription.parse(main, ponds)
        second = SiteDescription.parse(main, ponds)
        self.assertEqual(first, second)
        self.assertEqual(first.ponds["garden"].policy.memory_bytes, 2*1024**3)
        self.assertEqual(first.runtime_settings().ponds.keys(), {"pond"})
        self.assertEqual(first.settings.ponds.keys(), {"pond", "garden"})
        self.assertEqual(first.runtime_settings().pirate_service_callers, {"parrot": 1300})
        self.assertIn(19999, first.runtime_settings().protected_ports)
        self.assertIn(18529, first.runtime_settings().protected_ports)
        self.assertTrue(first.runtime_settings().environments["pond"].files.roots["home"].write)
        self.assertEqual(first.ponds["pond"].pond.projects["work"], ("PROJECT.md",))
        main["site"]["runtime_root"] = "/opt/changed"
        ponds["pond"]["limits"]["memory_bytes"] = 7*1024**3
        self.assertEqual(first.runtime_root, "/opt/boring-crocodile/current")
        self.assertEqual(first.ponds["pond"].policy.memory_bytes, 1024**3)

    def test_all_states_and_parked_descriptions_are_validated(self):
        for state in ("running", "stopped", "removed"):
            main, ponds = description()
            main["site"]["state"] = state
            site = SiteDescription.parse(main, ponds)
            self.assertEqual(site.state, state)
            if state == "removed":
                with self.assertRaisesRegex(ValueError, "removed"):
                    site.runtime_settings()
            else:
                self.assertIn("pond", site.runtime_settings().ponds)
        for change in (
                lambda m,p: m["site"].update(state="failed"),
                lambda m,p: m["site"].update(schema=True),
                lambda m,p: m["site"].update(runtime_root="/tmp/untrusted"),
                lambda m,p: p["pond"].update(enabled="yes"),
                lambda m,p: p["pond"].update(pirate_client="yes"),
                lambda m,p: p["pond"]["identity"].update(uid=0),
                lambda m,p: p["pond"]["identity"].update(group="another-group"),
                lambda m,p: p["garden"]["identity"].update(uid=1201),
                lambda m,p: p["garden"]["limits"].update(memory_bytes=True),
                lambda m,p: p["garden"]["pond"].update(profile_id=p["pond"]["pond"]["profile_id"]),
                lambda m,p: p["garden"].update(credential_file=p["pond"]["credential_file"]),
                lambda m,p: p["pond"]["execution"].update(control_plane_uid=0),
                lambda m,p: m.update(services={"canvas": {}})):
            main, ponds = description()
            change(main, ponds)
            with self.subTest(change=change), self.assertRaises(ValueError):
                SiteDescription.parse(main, ponds)

    def test_toml_files_feed_the_real_runtime_loader_without_receipts(self):
        main, ponds = description()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text(toml_document(main))
            for name, document in ponds.items():
                target = root / "ponds" / name / "config.toml"
                target.parent.mkdir(parents=True)
                target.write_text(toml_document(document))
            site = SiteDescription.read(config, owner=os.getuid())
            self.assertEqual(site.runtime_settings(), Settings.load(config))
            self.assertEqual(len(site.fingerprints()), 3)
            (root / "ponds/garden/config.toml").chmod(0o666)
            with self.assertRaisesRegex(ValueError, "unsafe"):
                SiteDescription.read(config, owner=os.getuid())
            (root / "ponds/garden/config.toml").chmod(0o644)
            target = root / "ponds/garden/config.toml"
            content = target.read_text()
            target.unlink()
            outside = root / "outside"
            outside.write_text(content)
            target.symlink_to(outside)
            with self.assertRaises(OSError):
                SiteDescription.read(config, owner=os.getuid())
            target.unlink()
            os.mkfifo(target)
            with self.assertRaisesRegex(ValueError, "unsafe"):
                SiteDescription.read(config, owner=os.getuid())

    def test_worker_reads_only_main_and_own_declaration(self):
        main, ponds = description()
        ponds["garden"]["enabled"] = True
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text(toml_document(main))
            for name, document in ponds.items():
                target = root / "ponds" / name / "config.toml"
                target.parent.mkdir(parents=True)
                target.write_text(toml_document(document))
            # A worker must neither enumerate the namespace nor open a neighbour.
            (root / "ponds/pond/config.toml").write_text("not valid TOML")
            with patch.object(Path, "iterdir", side_effect=AssertionError("namespace listing")):
                scoped = Settings.load(config, environment="garden")
            self.assertEqual(set(scoped.environments), {"garden"})
            self.assertEqual(set(scoped.ponds), {"garden"})
            with self.assertRaises(DescriptionError) as error:
                Settings.load(config)  # Controller retains whole-site validation.
            self.assertIn(str(config.parent / "ponds/pond/config.toml"), str(error.exception))
            self.assertIn("line 1, column 5", str(error.exception))
            with self.assertRaisesRegex(ValueError, "invalid Pond"):
                Settings.load(config, environment="../garden")
            ponds["garden"]["enabled"] = False
            (root / "ponds/garden/config.toml").write_text(toml_document(ponds["garden"]))
            with self.assertRaisesRegex(ValueError, "parked"):
                Settings.load(config, environment="garden")

    def test_units_are_reconstructible_from_descriptions_and_keep_host_boundaries(self):
        main, ponds = description()
        site = SiteDescription.parse(main, ponds)
        units = render_description(site)
        self.assertNotIn("config.toml", units)  # desired input is never regenerated from units
        self.assertNotIn("boring-crocodile-worker-garden.service", units)
        self.assertIn("MemoryMax=1073741824", units["system-crocodilepond.slice"])
        self.assertIn("SocketBindDeny=tcp:19999", units["system-crocodilepond.slice"])
        self.assertIn("ListenStream=127.0.0.1:8503", units["boring-crocodile-mcp.socket"])
        self.assertIn("demo-pond pond-pirates", units["boring-crocodile.conf"])
        self.assertIn("LoadCredential=approval-secret:" + str(site.ponds["pond"].credential_file),
                      units["boring-crocodile-mcp.service"])
        ponds["garden"]["enabled"] = True
        units = render_description(SiteDescription.parse(main, ponds))
        self.assertIn("MemoryMax=2147483648", units["system-crocodilegarden.slice"])
        self.assertIn("SupplementaryGroups=demo-pond demo-garden", units["boring-crocodile-mcp.service"])
        self.assertIn("garden-approval-secret:" + ponds["garden"]["credential_file"],
                      units["boring-crocodile-mcp.service"])
        main["site"]["state"] = "removed"
        self.assertEqual(render_description(SiteDescription.parse(main, ponds)), {})

    def test_writer_uses_bare_keys_and_preserves_quoted_documents(self):
        quoted = '''["site"]
"state" = "stopped"
"A-Z_a-z09" = true
"123" = 42

["table.with.dot"]
"has space" = "kept"
"" = "empty"
"é" = "unicode"
"quote\\"" = "quoted"

["table.with.dot"."nested table"]
"key.with.dot" = "one key"
'''
        before = tomllib.loads(quoted)
        written = toml_document(before)
        self.assertEqual(tomllib.loads(written), before)
        for line in ('[site]', 'state = "stopped"', 'A-Z_a-z09 = true', '123 = 42',
                     '["table.with.dot"]', '"has space" = "kept"', '"" = "empty"',
                     '"é" = "unicode"', '["table.with.dot"."nested table"]',
                     '"key.with.dot" = "one key"'):
            self.assertIn(line, written.splitlines())
        self.assertNotIn('["site"]', written)
        self.assertNotIn('"state" =', written)

    def test_writer_roundtrips_supported_toml_and_refuses_unsupported_values(self):
        main, ponds = description()
        for document in (main, *ponds.values()):
            self.assertEqual(tomllib.loads(toml_document(document)), document)
        controls = "".join(chr(i) for i in range(32)) + "\x7f😀é"
        value = {"display_name": controls, "table😀\x7f": {"array": [controls, True, 42]}}
        self.assertEqual(tomllib.loads(toml_document(value)), value)
        with self.assertRaises(ValueError):
            toml_document({"invalid": [None]})
        with self.assertRaises(ValueError):
            toml_document({"invalid": "\ud800"})
