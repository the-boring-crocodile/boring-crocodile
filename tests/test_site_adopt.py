"""One-time adoption logic and interruption use the existing shared host."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from boring_crocodile import site_adopt
from boring_crocodile.site_model import SiteDescription
from boring_crocodile.site_templates import render_description
from site_host import FakeHost
from test_site_default import load_generator
from pond_config import runtime_settings


class AdoptionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.prepare(Path(self.temp.name)/"single")

    def prepare(self, root, *, second=False):
        self.host = FakeHost(root, provisioned=False)
        self.paths = self.host.paths
        self.site = load_generator().description(second)
        identities = [self.site.controller, *(p.identity for p in self.site.ponds.values())]
        self.host.identities = {i.name: i for i in identities}
        self.host.extra_groups = {i.group: {"name": i.group, "gid": i.gid} for i in identities}
        self.host.extra_groups["pirate-clients"] = {"name": "pirate-clients", "gid": 1302}
        self.host.extra_groups["pond-pirates"] = {"name": "pond-pirates", "gid": 1301}
        for pond in self.site.ponds.values():
            home = self.host.path(pond.identity.home)
            home.mkdir(parents=True)
            self.host.owned[str(home)] = pond.identity.uid
            secret = self.host.path(pond.credential_file)
            secret.parent.mkdir(parents=True, exist_ok=True)
            secret.write_text("synthetic-secret-never-in-plan")
            secret.chmod(0o600)
        fixture = "pre_phase14_two_ponds.toml" if second else "pre_phase14_config.toml"
        self.paths.config.write_bytes((Path(__file__).parent/"fixtures"/fixture).read_bytes())
        self.legacy = runtime_settings(self.paths.config)
        # One canonical Pond display name replaces the old execution fallback.
        # Compare every other runtime field unchanged across adoption/retries.
        self.expected_runtime = replace(self.legacy, environments={
            name: replace(env, display_name=self.legacy.ponds[name].display_name)
            for name, env in self.legacy.environments.items()})
        import sqlite3
        store = self.host.path(self.legacy.oauth_database_file)
        store.parent.mkdir(parents=True)
        with sqlite3.connect(store) as db:
            db.execute("PRAGMA user_version=5")
            db.execute("CREATE TABLE keep_me (value TEXT)")
            db.execute("INSERT INTO keep_me VALUES ('synthetic preserved row')")
        store.chmod(0o600)
        for name, body in render_description(self.site).items():
            if name == "boring-crocodile-mcp.service":
                # Published 0.4.21 did not pass an enabled-Pond startup scope.
                body = body.replace(" --ponds " + " ".join(self.site.ponds), "")
            self.paths.location(name).write_text(body)

    def plan(self):
        return site_adopt.plan(paths=self.paths, host=self.host)[0]

    def apply(self):
        return site_adopt.apply(self.plan()["plan_digest"], paths=self.paths, host=self.host)

    def test_legacy_reception_choice_is_removed_with_explicit_plan(self):
        import tomllib
        from boring_crocodile.site_model import toml_document
        for enabled in (False, True):
            raw = tomllib.loads(self.paths.config.read_text())
            raw["environments"]["pond"]["service_registration_enabled"] = enabled
            self.paths.config.write_text(toml_document(raw))
            proposal, (site, _) = site_adopt.plan(paths=self.paths, host=self.host)
            self.assertEqual(proposal["reception_enabled"], [] if enabled else ["pond"])
            self.assertNotIn("service_registration_enabled", site.ponds["pond"].document["execution"])

    def test_disabled_legacy_reception_provisions_missing_group_only_on_apply(self):
        del self.host.extra_groups["pond-pirates"]
        before = self.paths.config.read_bytes()
        proposal = self.plan()
        self.assertEqual(proposal["provision_groups"], ["pond-pirates"])
        self.assertIsNone(self.host.group("pond-pirates"))
        self.assertEqual(self.paths.config.read_bytes(), before)
        identities = dict(self.host.identities)
        self.apply()
        self.assertIsNotNone(self.host.group("pond-pirates"))
        self.assertEqual(self.host.identities, identities)
        self.assertNotIn("provision_groups", self.plan())

    def test_interrupted_conversion_keeps_provisioned_group_for_retry(self):
        del self.host.extra_groups["pond-pirates"]
        before = self.paths.config.read_bytes()
        with patch.object(site_adopt, "write_file", side_effect=OSError("fixture interruption")):
            with self.assertRaisesRegex(ValueError, "Adoption interrupted"):
                self.apply()
        group = self.host.group("pond-pirates")
        self.assertIsNotNone(group)
        self.assertEqual(self.paths.config.read_bytes(), before)
        self.assertEqual(self.plan()["provision_groups"], [])
        self.apply()
        self.assertEqual(self.host.group("pond-pirates"), group)

    def test_importer_preserves_existing_reception_group(self):
        group = self.host.group("pond-pirates")
        self.assertEqual(self.plan()["provision_groups"], [])
        self.apply()
        self.assertEqual(self.host.group("pond-pirates"), group)

    def test_importer_strips_equal_defaults_and_keeps_custom_policy(self):
        import tomllib
        from boring_crocodile.site_model import toml_document
        raw = tomllib.loads(self.paths.config.read_text())
        raw["ponds"]["pond"]["context_root"] = "/srv/handbook"
        execution = raw["environments"]["pond"]
        execution.update(default_cwd="/srv/work", allowed_roots=["/srv/work"])
        execution["files"]["roots"]["home"].update(write=False, deny=["private"])
        execution["files"]["roots"]["project"] = dict(path="/srv/work", read=True, write=True)
        self.paths.config.write_text(toml_document(raw))
        old = runtime_settings(self.paths.config)
        expected = replace(old, environments={"pond": replace(old.environments["pond"],
                                                            display_name=old.ponds["pond"].display_name)})
        _, (site, _) = site_adopt.plan(paths=self.paths, host=self.host)
        self.assertEqual(site.runtime_settings(), expected)
        document = site.ponds["pond"].document
        self.assertEqual(document["pond"]["context_root"], "/srv/handbook")
        self.assertEqual(document["execution"]["default_cwd"], "/srv/work")
        self.assertEqual(document["execution"]["allowed_roots"], ["/srv/work"])
        self.assertNotIn("path", document["execution"]["files"]["roots"]["home"])

    def test_importer_refuses_conflicting_names_or_nonstandard_home_alias(self):
        import tomllib
        from boring_crocodile.site_model import toml_document
        original = self.paths.config.read_bytes()
        for field in ("display_name", "home_alias"):
            raw = tomllib.loads(original.decode())
            execution = raw["environments"]["pond"]
            if field == "display_name":
                execution[field] = "conflicting execution name"
            else:
                execution["files"]["roots"]["home"]["path"] = "/srv/other"
            self.paths.config.write_text(toml_document(raw))
            before = self.paths.config.read_bytes()
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "Conflicting|home file-root"):
                self.plan()
            self.assertEqual(self.paths.config.read_bytes(), before)

    def test_importer_does_not_accept_a_redundant_rc7_description(self):
        from copy import deepcopy
        from boring_crocodile.site_model import toml_document
        main = deepcopy(self.site.document)
        self.paths.config.write_text(toml_document(main))
        for name, pond in self.site.ponds.items():
            target = self.paths.config.parent / "ponds" / name / "config.toml"
            target.parent.mkdir(parents=True, exist_ok=True)
            document = deepcopy(pond.document)
            document["pond"]["context_root"] = pond.identity.home + "/handbook"
            target.write_text(toml_document(document))
        with self.assertRaisesRegex(ValueError, "ponds.pond.pond.context_root"):
            self.plan()

    def old_policy(self, mode):
        from boring_crocodile.task_policy import task_directives
        path = self.paths.units/"boring-crocodile-task-pond@.service.d/50-pond-task-policy.conf"
        path.parent.mkdir(exist_ok=True)
        path.write_text("[Service]\n"+task_directives(mode, str(self.site.ponds["pond"].identity.home))+"\n")
        return path

    def test_old_generated_policy_is_projected_and_removed_with_retry(self):
        path = self.old_policy(False)
        before = path.read_bytes()
        proposal = self.plan()
        self.assertEqual(proposal["removals"], [str(path)])
        self.assertEqual(path.read_bytes(), before)
        unlink = Path.unlink
        def interrupted(target, *args, **kwargs):
            if target == path:
                raise OSError("drop-in removal interrupted")
            return unlink(target, *args, **kwargs)
        with patch.object(Path, "unlink", interrupted), self.assertRaisesRegex(ValueError, "rerun adoption"):
            self.apply()
        converted = SiteDescription.read(self.paths.config)
        self.assertFalse(converted.ponds["pond"].execution.no_new_privileges)
        self.assertEqual(self.plan()["removals"], [str(path)])
        self.host.active.add("boring-crocodile-worker-pond.service")
        with self.assertRaisesRegex(ValueError, "did not stop"):
            self.apply()
        self.host.active.clear()
        self.apply()
        self.assertFalse(path.exists())
        self.assertEqual(self.apply()["status"], "already_converted")

    def test_foreign_policy_and_changed_review_refuse_unchanged(self):
        path = self.old_policy(True)
        proposal = self.plan()
        path.write_text(path.read_text()+"# foreign content\n")
        with self.assertRaisesRegex(ValueError, "needs review"):
            site_adopt.apply(proposal["plan_digest"], paths=self.paths, host=self.host)
        self.assertFalse((self.paths.state/"ponds").exists())
        path.unlink()
        foreign = self.paths.units/"service.d/50-pond-task-policy.conf"
        foreign.parent.mkdir()
        foreign.write_text("[Service]\nNoNewPrivileges=yes\n")
        with self.assertRaisesRegex(ValueError, "outside its generated location"):
            self.plan()
        self.assertTrue(foreign.exists())

    def test_preview_and_interrupted_conversion_keep_native_bindings(self):
        before = {str(p): p.read_bytes() for p in self.host.root.rglob("*") if p.is_file()}
        proposal = self.plan()
        self.assertNotIn("synthetic-secret-never-in-plan", str(proposal))
        self.assertEqual(proposal["identities"][0]["uid"], 902)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.host.root.rglob("*") if p.is_file()})
        actual_write = site_adopt.write_file

        def interrupted(path, body, **options):
            if path == self.paths.config:
                raise OSError("interrupted before main publication")
            actual_write(path, body, **options)
        with patch.object(site_adopt, "write_file", interrupted), self.assertRaisesRegex(ValueError, "rerun adoption"):
            self.apply()
        self.assertNotIn(b'["site"]', self.paths.config.read_bytes())
        document = self.paths.state/"ponds/pond/config.toml"
        saved = document.read_bytes()
        self.apply()
        converted = SiteDescription.read(self.paths.config)
        self.assertEqual(converted.state, "stopped")
        self.assertEqual(converted.runtime_settings(), self.expected_runtime)
        self.assertEqual(document.read_bytes(), saved)
        self.assertEqual(self.apply()["status"], "already_converted")
        for name in render_description(self.site):
            self.assertEqual(self.paths.location(name).read_bytes(), before[str(self.paths.location(name))])
        self.assertEqual(self.host.path(converted.ponds["pond"].credential_file).read_text(),
                         "synthetic-secret-never-in-plan")

    def test_permission_interruption_repairs_without_republishing_description(self):
        with patch.object(self.host, "apply_description_permissions", side_effect=OSError("permissions cut")), \
             self.assertRaisesRegex(ValueError, "rerun adoption"):
            self.apply()
        converted = SiteDescription.read(self.paths.config)
        self.assertEqual(converted.runtime_settings(), self.expected_runtime)
        before = {str(p): p.read_bytes() for p in self.host.root.rglob("*") if p.is_file()}
        self.assertEqual(self.apply()["status"], "already_converted")
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.host.root.rglob("*") if p.is_file()})

    def test_live_conflicting_and_stale_inputs_refuse_before_publication(self):
        self.host.active.add("boring-crocodile-worker-pond.service")
        with self.assertRaisesRegex(ValueError, "did not stop"):
            self.apply()
        self.assertFalse((self.paths.state/"ponds").exists())
        self.host.active.clear()
        proposal = self.plan()
        worker = self.host.identities["boring-crocodile"]
        self.host.identities[worker.name] = replace(worker, uid=worker.uid+10)
        with self.assertRaisesRegex(ValueError, "Unsafe worker home"):
            site_adopt.apply(proposal["plan_digest"], paths=self.paths, host=self.host)
        self.host.identities[worker.name] = worker
        unit = self.paths.units/"boring-crocodile-mcp.service"
        unit.write_text(unit.read_text()+"# changed since review\n")
        with self.assertRaisesRegex(ValueError, "changed since review"):
            site_adopt.apply(proposal["plan_digest"], paths=self.paths, host=self.host)
        external = self.paths.units/"boring-crocodile-worker-pond.service.d/local.conf"
        external.parent.mkdir()
        external.write_text("[Service]\nTasksMax=200\n")
        self.assertTrue(any(row.get("directive") == "TasksMax" and row.get("warning")
                            for row in self.plan()["adjustments"]))
        self.assertFalse((self.paths.state/"ponds").exists())



    def test_two_ponds_every_document_cut_preserves_policy_and_data(self):
        import sqlite3
        for cut in range(3):
            for after in (False, True):
                with self.subTest(cut=cut, after=after):
                    self.prepare(Path(self.temp.name)/f"cut-{cut}-{after}", second=True)
                    second = self.site.ponds["second"]
                    self.host.role_members["pirate-clients"].add(second.identity.name)
                    budget = self.paths.units/"system-crocodilesecond.slice"
                    budget.write_text(budget.read_text().replace("MemoryMax=2147483648", "MemoryMax=3221225472"))
                    external = self.paths.units/"boring-crocodile-worker-second.service.d/local.conf"
                    external.parent.mkdir()
                    external.write_text("[Service]\nUMask=0077\nReadWritePaths=/srv/shared\n")
                    before = {str(p): p.read_bytes() for p in self.host.root.rglob("*") if p.is_file()}
                    bindings = dict(self.host.identities)
                    actual_write = site_adopt.write_file
                    count = 0
                    def interrupted(path, body, **options):
                        nonlocal count
                        selected = count == cut
                        count += 1
                        if selected and not after:
                            raise OSError("before publication")
                        actual_write(path, body, **options)
                        if selected and after:
                            raise OSError("after publication")
                    with patch.object(site_adopt, "write_file", interrupted), self.assertRaisesRegex(
                            ValueError, "rerun adoption"):
                        self.apply()
                    self.apply()
                    site = SiteDescription.read(self.paths.config)
                    self.assertEqual(site.runtime_settings(), self.expected_runtime)
                    self.assertEqual(site.state, "stopped")
                    self.assertEqual(site.ponds["second"].policy.memory_bytes, 3221225472)
                    self.assertFalse(any("second" in caller.ponds for caller in site.settings.pirate_callers.values()))
                    self.assertEqual(site.ponds["second"].pond.profile_id, self.legacy.ponds["second"].profile_id)
                    self.assertEqual(self.host.identities, bindings)
                    for path, body in before.items():
                        if path != str(self.paths.config):
                            self.assertEqual(Path(path).read_bytes(), body)
                    with sqlite3.connect(self.host.path(self.legacy.oauth_database_file)) as db:
                        self.assertEqual(db.execute("SELECT value FROM keep_me").fetchone()[0], "synthetic preserved row")
                    self.assertFalse(self.host.active)
                    self.assertFalse(any("start" in c or "stop" in c for c in self.host.calls))

    def test_unsafe_inputs_refuse_without_publishing_or_blocking(self):
        import os
        cases = ("secret-link", "secret-hardlink", "secret-mode", "store-fifo",
                 "store-link", "store-mode", "store-generation", "store-header",
                 "unknown-unit", "conflicting-document", "missing-pirate")
        for case in cases:
            with self.subTest(case=case):
                self.prepare(Path(self.temp.name)/case)
                secret = self.host.path(self.site.ponds["pond"].credential_file)
                store = self.host.path(self.legacy.oauth_database_file)
                if case == "secret-link":
                    other = secret.with_name("other")
                    secret.rename(other)
                    secret.symlink_to(other)
                elif case == "secret-hardlink":
                    secret.with_name("other").hardlink_to(secret)
                elif case == "secret-mode":
                    secret.chmod(0o644)
                elif case == "store-fifo":
                    store.unlink()
                    os.mkfifo(store, 0o600)
                elif case == "store-link":
                    other = store.with_name("other")
                    store.rename(other)
                    store.symlink_to(other)
                elif case == "store-mode":
                    store.chmod(0o644)
                elif case == "store-generation":
                    import sqlite3
                    with sqlite3.connect(store) as db:
                        db.execute("PRAGMA user_version=4")
                elif case == "store-header":
                    store.write_bytes(b"not SQLite")
                elif case == "unknown-unit":
                    (self.paths.units/"boring-crocodile-worker-foreign.service").write_text("[Service]\n")
                elif case == "conflicting-document":
                    target = self.paths.state/"ponds/pond/config.toml"
                    target.parent.mkdir(parents=True)
                    target.write_text("conflicting=true\n")
                else:
                    raw = self.paths.config.read_text()
                    self.paths.config.write_text(raw+'\n[pirate_services.callers]\nmissing=1599\n')
                before = {str(p): p.read_bytes() for p in self.host.root.rglob("*")
                          if p.is_file() and not p.is_symlink()}
                actual_open = os.open
                def checked_open(path, flags, *args, **kw):
                    if path == store:
                        self.assertTrue(flags & os.O_NONBLOCK)
                    return actual_open(path, flags, *args, **kw)
                with patch.object(site_adopt.os, "open", checked_open), self.assertRaises((ValueError, OSError)):
                    self.plan()
                self.assertEqual(before, {str(p): p.read_bytes() for p in self.host.root.rglob("*")
                                         if p.is_file() and not p.is_symlink()})
                self.assertNotIn("site", __import__("tomllib").loads(self.paths.config.read_text()))

    def test_offline_launcher_imports_from_verified_pure_python_wheels(self):
        import hashlib
        import importlib
        import json
        import os
        import shutil
        import subprocess
        import sys
        import zipfile
        from dataclasses import asdict
        import boring_deploy
        self.prepare(Path(self.temp.name)/"artifact-site", second=True)
        source = Path(__file__).resolve().parents[1]
        supplied = os.environ.get("CROCODILE_ADOPTION_TEST_ARTIFACT")
        if supplied:
            release = Path(supplied).resolve()
        else:
            release = self.host.root/"release"
            (release/"deploy.d").mkdir(parents=True)
            (release/"wheels").mkdir()
            shutil.copyfile(source/"deploy/adopt-site.py", release/"adopt-site.py")
            shutil.copyfile(boring_deploy.__file__, release/"deploy.d/boring_deploy.py")
            packages = [("boring_crocodile", source/"src/boring_crocodile")]
            for name in ("boring_worker_kit", "boring_file_core", "boring_outcomes", "boring_registry"):
                packages.append((name, Path(importlib.import_module(name).__file__).parent))
            for name, directory in packages:
                distribution = "boring_crocodile_mcp" if name == "boring_crocodile" else name
                with zipfile.ZipFile(release/"wheels"/(distribution+"-0-py3-none-any.whl"), "w") as wheel:
                    for path in directory.rglob("*"):
                        if path.is_file() and "__pycache__" not in path.parts:
                            wheel.write(path, name+"/"+path.relative_to(directory).as_posix())
            manifest = []
            for path in sorted(release.rglob("*")):
                if path.is_file():
                    manifest.append(hashlib.sha256(path.read_bytes()).hexdigest()+"  "+path.relative_to(release).as_posix())
            (release/"MANIFEST.sha256").write_text("\n".join(manifest)+"\n")
        facts = self.host.root/"facts.json"
        facts.write_text(json.dumps({
            "identities": [asdict(i) for i in self.host.identities.values()],
            "groups": self.host.extra_groups, "owned": self.host.owned,
            "members": {key: sorted(value) for key,value in self.host.role_members.items()}}))
        before = {str(p): p.read_bytes() for p in self.host.root.rglob("*") if p.is_file()}
        result = subprocess.run([sys.executable, "-I", "-B",
            str(source/"tests/fixtures/adopt_artifact_probe.py"), str(release), str(self.host.root), str(facts)],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        proposal = json.loads(result.stdout)
        self.assertEqual(proposal["status"], "proposed")
        self.assertEqual(len(proposal["identities"]), 3)
        self.assertNotIn("synthetic-secret-never-in-plan", result.stdout)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.host.root.rglob("*") if p.is_file()})
        self.assertFalse(list(release.rglob("__pycache__")))
