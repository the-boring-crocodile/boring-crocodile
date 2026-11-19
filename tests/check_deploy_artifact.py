"""Build gate: the packaged check hook reads an actually converted fixture site."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from zipfile import ZipFile
from unittest.mock import patch

RELEASE = Path(sys.argv.pop(1)).resolve()
sys.path.insert(0, str(RELEASE / "deploy.d"))
import boring_deploy

boring_deploy.verify_files(RELEASE)
# The dry-run reader imports pure code directly from verified wheels. The
# installed admin also loads extension modules. Like normal installation,
# unpack those wheels so the OS loader can open the bundled shared objects.
NATIVE = tempfile.TemporaryDirectory(prefix="deploy-artifact-native-")
pure_wheels = []
for wheel in sorted((RELEASE / "wheels").glob("*.whl")):
    if wheel.name.endswith("-none-any.whl"):
        pure_wheels.append(str(wheel))
    else:
        with ZipFile(wheel) as package:
            package.extractall(NATIVE.name)
sys.path[:0] = [NATIVE.name, *pure_wheels]
sys.path.append(str(Path(__file__).resolve().parent))
from boring_crocodile import site_adopt, site_check
from boring_crocodile.site_model import SiteDescription
import test_site_adopt


def reference(second):
    base = Path(__file__).resolve().parents[1] / "site/default"
    if second:
        base /= "two-ponds"
    base /= "etc/boring-crocodile"
    main = tomllib.loads((base / "config.toml").read_text())
    ponds = {p.parent.name: tomllib.loads(p.read_text()) for p in (base / "ponds").glob("*/config.toml")}
    return SiteDescription.parse(main, ponds)


class ConvertedArtifactTest(unittest.TestCase):
    def test_packaged_check_with_real_context_after_conversion(self):
        self.assertTrue(str(site_adopt.__file__).startswith(str(RELEASE / "wheels")))
        spec = importlib.util.spec_from_file_location("packaged_crocodile_steps", RELEASE / "deploy.d/steps.py")
        steps = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(steps)
        self.assertEqual(Path(steps.__file__).resolve(), RELEASE / "deploy.d/steps.py")
        with tempfile.TemporaryDirectory(prefix="deploy-artifact-") as directory:
            root = Path(directory)
            fixture = test_site_adopt.AdoptionTest()
            # Reuse the adoption fixture, reading its checked-in typed reference
            # instead of the source-only reference generator inside wheel imports.
            with patch.object(test_site_adopt, "load_generator",
                              return_value=SimpleNamespace(description=reference)):
                fixture.prepare(root / "site", second=True)
            self.assertEqual(fixture.apply()["status"], "converted")
            host = fixture.host
            converted = SiteDescription.read(host.paths.config, owner=host.owner)
            self.assertEqual(converted.state, "stopped")
            self.assertEqual(converted.runtime_settings(), fixture.expected_runtime)
            self.assertEqual(set(converted.ponds), {"pond", "second"})
            home = root / "runtime"
            old = home / "current/bin/boring-crocodile-admin"
            old.parent.mkdir(parents=True)
            old.write_text("legacy executable marker; never run")
            release = boring_deploy.Release.load(RELEASE)
            release.details["config"] = str(host.paths.config)
            calls, logged = [], []
            ctx = boring_deploy.Context(release, home, "legacy", logged.append, new=RELEASE)

            run = subprocess.run

            def process(command, *, cwd, text, capture_output, stdin, env):
                calls.append(tuple(command))
                self.assertEqual(cwd, "/")
                self.assertEqual(env["PATH"], boring_deploy.SYSTEM_TOOL_PATH)
                self.assertTrue(Path(command[0]).is_absolute())
                self.assertTrue(text and capture_output)
                self.assertEqual(stdin, subprocess.DEVNULL)
                if steps._SITE_STATE in command:
                    return run(command, cwd=cwd, text=text, capture_output=capture_output, stdin=stdin, env=env)
                if Path(command[0]).name == "systemctl":
                    self.assertIn("list-units", command)
                    output = ""  # All fixture consumers are stopped.
                elif command[0] == str(home / "current/bin/python3"):
                    self.assertEqual(tuple(command[1:4]), ("-I", "-B", "-c"))
                    if "find_spec" in command[-1]:
                        self.assertIn("find_spec('boring_crocodile.site_model')", command[-1])
                        output = "0\n"  # Installed reader predates the typed description.
                    else:
                        self.assertIn("Settings.load", command[-2])
                        self.assertEqual(command[-1], str(host.paths.config))
                        output = json.dumps({"unreadable": "ValueError: config: unknown keys [site]"})
                elif command[0] == str(old):
                    self.assertEqual(tuple(command[1:]), ("--config", str(host.paths.config), "status"))
                    return subprocess.CompletedProcess(command, 2, "", "ValueError: config: unknown keys [site]")
                elif command[0] == str(RELEASE / "bin/boring-crocodile-admin"):
                    self.assertEqual(tuple(command[1:3]), ("--config", str(host.paths.config)))
                    if command[-1] == "list":
                        # The admin subprocess boundary reads the importer's actual
                        # main/per-Pond files through the packaged model. FakeHost
                        # maps root ownership; no installed native runtime is needed.
                        settings = SiteDescription.read(host.paths.config, owner=host.owner).runtime_settings()
                        output = json.dumps([{"name": name} for name in settings.ponds])
                    else:
                        self.assertEqual(tuple(command[-2:]), ("site-check", "--json"))
                        output = json.dumps(site_check.site_check(paths=host.paths, host=host))
                else:
                    self.fail("Unexpected subprocess: " + repr(command))
                return subprocess.CompletedProcess(command, 0, output, "")

            # The process boundary and fixture filesystem root are synthetic;
            # Context.run and the packaged hooks/validators remain real.
            with patch.object(boring_deploy.subprocess, "run", side_effect=process):
                self.assertEqual(steps.preflight(ctx), [])
                for broken in (host.paths.config, host.paths.config.parent / "ponds/second/config.toml"):
                    original = broken.read_bytes()
                    broken.write_bytes(b"bad = @\n")
                    try:
                        problems = steps.preflight(ctx)
                        self.assertEqual(len(problems), 1, problems)
                        self.assertIn(f"{broken}: Invalid value (at line 1, column 7)", problems[0])
                        self.assertNotIn("Traceback", problems[0])
                    finally:
                        broken.write_bytes(original)
                pond = converted.ponds["pond"]
                database = host.path(pond.execution.default_cwd)/".boring-crocodile/jobs.sqlite3"
                database.parent.mkdir()
                database.write_bytes(b"synthetic unsupported Pond-owned store")
                snapshot = {str(p): p.read_bytes() for p in host.root.rglob("*") if p.is_file()}
                # No fixture-root remapping: preflight must not open a Pond's store.
                with patch("sqlite3.connect", side_effect=AssertionError("root opened receipts")):
                    self.assertEqual(steps.preflight(ctx), [])
                self.assertEqual(snapshot, {str(p): p.read_bytes()
                                           for p in host.root.rglob("*") if p.is_file()})
                before = {str(p): p.read_bytes() for p in host.root.rglob("*") if p.is_file()}
                steps.check(ctx)
            self.assertTrue(any("installed before-state is unknown: ValueError" in line for line in logged))
            self.assertIsNone(steps.healthy(ctx))
            self.assertEqual(sum("-c" in call and steps._SITE_STATE not in call for call in calls), 2)
            self.assertTrue(any(call[0] == str(old) and call[-1] == "status" for call in calls))
            self.assertEqual(before, {str(p): p.read_bytes() for p in host.root.rglob("*") if p.is_file()})


if __name__ == "__main__":
    with NATIVE:
        unittest.main()
