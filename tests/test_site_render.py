"""Four journeys and readiness logic share one host; no live commands."""
from copy import deepcopy
import tempfile
from unittest.mock import patch
import unittest
from pathlib import Path

from boring_crocodile import site_render
from boring_crocodile.site_render import LISTENER, CONTROLLER, SiteConflict
from site_host import FakeHost
from boring_crocodile.site_model import SiteDescription


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.host = FakeHost(Path(self.directory.name))
        self.paths = self.host.paths

    def plan(self, candidate=None):
        return site_render.plan(paths=self.paths, host=self.host, candidate=candidate)[0]

    def apply(self, digest=None, candidate=None):
        return site_render.apply(digest or self.plan(candidate)["plan_digest"],
                                 paths=self.paths, host=self.host, candidate=candidate)

    def test_pirate_slices_share_the_pond_port_rules(self):
        from boring_crocodile.site_ports import PIRATE_BIND_DROPIN

        base = self.paths.units / "system-pirateparrot.slice"
        base.write_text("[Slice]\nTasksMax=256\n")
        site = SiteDescription.read(self.paths.config, owner=self.paths.owner)
        dropin = base.parent / (base.name + ".d") / PIRATE_BIND_DROPIN
        self.assertIn("not rendered", site_render.rendered_service_ports(site, self.paths, self.host).reason)
        self.apply()
        pond = base.with_name("system-crocodilepond.slice")
        self.assertEqual(site_render._protection(dropin.read_bytes()),
                         site_render._protection(pond.read_bytes()))
        self.assertIn("SocketBindDeny=tcp:18538\n", dropin.read_text())
        self.assertEqual(base.read_text(), "[Slice]\nTasksMax=256\n")
        from types import SimpleNamespace

        installed = sorted(path.name for path in self.paths.units.glob("*.slice"))
        command = ["/usr/bin/systemctl", "show", "--property=NeedDaemonReload", "--value", *installed]
        fresh = "no\n\n" * len(installed)
        with patch.object(self.host, "command", return_value=SimpleNamespace(stdout=fresh)) as call:
            self.assertIn(18538, site_render.rendered_service_ports(site, self.paths, self.host).ports)
            call.assert_called_once_with(command)
        with patch.object(self.host, "command", return_value=SimpleNamespace(stdout=fresh.replace("no", "yes", 1))):
            self.assertEqual(site_render.rendered_service_ports(site, self.paths, self.host).reason,
                             "systemd daemon-reload is pending")
        with patch.object(self.host, "command", side_effect=OSError("fixture systemctl failure")):
            self.assertIn("systemctl", site_render.rendered_service_ports(site, self.paths, self.host).reason)
        foreign = dropin.with_name("90-override.conf")
        foreign.write_text("[Slice]\nSocketBindAllow=tcp:18538\n")
        self.assertIn(f"foreign drop-in can override port protection: {foreign}",
                      site_render.rendered_service_ports(site, self.paths, self.host).reason)
        foreign.unlink()
        dropin.unlink()
        self.assertIn("not rendered", site_render.rendered_service_ports(site, self.paths, self.host).reason)

    def test_site_check_names_existing_unprotected_service(self):
        from types import SimpleNamespace
        from boring_crocodile.site_check import site_check

        self.apply()
        site = SiteDescription.read(self.paths.config, owner=self.paths.owner)
        database = self.host.path(site.settings.oauth_database_file)
        database.parent.mkdir(parents=True, exist_ok=True)
        database.touch()
        store = SimpleNamespace(service_list=lambda: [
            {"name": "retained-box", "url": "http://127.0.0.1:19021/mcp"},
            {"name": "protected-box", "url": "http://[::1]:18538/mcp"},
        ])
        with patch.object(self.host, "oauth_store", return_value=store):
            report = site_check(paths=self.paths, host=self.host)
        diagnostics = [row for row in report["irregular"] if row["what"] == "unprotected service port"]
        self.assertEqual([row["service"] for row in diagnostics], ["retained-box"])
        self.assertIn("19021", diagnostics[0]["detail"])

    def test_render_rejects_short_approval_secret_without_showing_it(self):
        secret = self.host.path(self.host.site.ponds["pond"].credential_file)
        secret.write_text("  tiny-value  \n")
        with self.assertRaisesRegex(ValueError, r"at least 24 characters.*approval-secret") as caught:
            self.plan()
        self.assertNotIn("tiny-value", str(caught.exception))

    def test_task_authority_uses_base_unit_and_preserves_external_dropins(self):
        external = self.paths.units/"boring-crocodile-task-pond@.service.d/local.conf"
        external.parent.mkdir()
        external.write_text("[Service]\nUMask=0027\nReadWritePaths=/srv/shared\n")
        before = external.read_bytes()
        for mode in (False, True):
            ponds = deepcopy(self.host.ponds)
            ponds["pond"]["execution"]["no_new_privileges"] = mode
            candidate = SiteDescription.parse(self.host.main, ponds)
            self.apply(candidate=candidate)
            task = self.paths.units/"boring-crocodile-task-pond@.service"
            self.assertIn("NoNewPrivileges="+("yes" if mode else "no"), task.read_text())
            self.assertEqual(external.read_bytes(), before)
            self.assertFalse((external.parent/"50-pond-task-policy.conf").exists())
        external.write_text("[Service]\nNoNewPrivileges=no\n")
        self.host.calls.clear()
        proposal = self.plan()
        self.assertTrue(any(row.get("directive") == "NoNewPrivileges" and row.get("warning")
                            for row in proposal["adjustments"]))
        self.apply()
        self.assertTrue(any("restart" in call for call in self.host.calls))
        self.assertEqual(external.read_text(), "[Service]\nNoNewPrivileges=no\n")

    def test_network_narrowing_and_unknown_dropins_are_shown_and_preserved(self):
        external = self.paths.units / "boring-crocodile-task-pond@.service.d/network.conf"
        external.parent.mkdir()
        body = ("# operator policy\n[Service]\nIPAddressDeny=any\n"
                "Environment=EXTRA=one\\\n two\n[Unrecognised]\nFutureSetting=example\n")
        external.write_text(body)
        proposal = self.plan()
        self.assertTrue(any(row.get("content") == body for row in proposal["adjustments"]))
        self.assertTrue(any(row.get("directive") == "IPAddressDeny"
                            for row in proposal["adjustments"]))
        self.apply()
        self.assertEqual(external.read_text(), body)

    def test_invalid_serialized_candidate_refuses_before_any_effect(self):
        main = deepcopy(self.host.main)
        main["server"]["public_base_url"] = "https://changed.example.test"
        candidate = SiteDescription.parse(main, self.host.ponds)
        before = {str(p): p.read_bytes() for p in self.host.root.rglob("*") if p.is_file()}
        self.host.calls.clear()
        with patch.object(site_render, "toml_document", return_value="invalid ="):
            with self.assertRaises(ValueError):
                self.plan(candidate=candidate)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.host.root.rglob("*") if p.is_file()})
        self.assertFalse(any(verb in call for call in self.host.calls
                             for verb in ("stop", "restart", "disable", "--no-mask")))

    def test_install_rerun_and_failed_tail_repair(self):
        source = self.paths.config.read_bytes()
        self.assertEqual(self.apply()["status"], "rendered")
        self.assertEqual(self.host.enabled, {"boring-crocodile-mcp.target", "boring-crocodile-task-pond.socket"})
        self.assertTrue(self.plan()["changes"] == [])
        self.host.calls.clear()
        self.apply()
        self.assertIn(["/usr/bin/systemctl", "daemon-reload"], self.host.calls)
        self.assertIn(["/usr/bin/systemctl", "restart", CONTROLLER], self.host.calls)
        self.assertFalse(any("stop" in command and LISTENER in command for command in self.host.calls))
        task = self.paths.units / "boring-crocodile-task-pond@.service"
        loaded_before = self.host.loaded[task.name]
        task.write_bytes(task.read_bytes() + b"# older base unit\n")
        self.host.ponds["pond"]["limits"]["task_tasks_max"] = 129
        self.host.save()
        self.host.fail = "daemon-reload"
        with self.assertRaisesRegex(SiteConflict, "Rerun"):
            self.apply()
        self.assertEqual(self.plan()["changes"], [])
        self.assertNotEqual(self.host.loaded[task.name], task.read_bytes())
        self.apply()
        self.assertEqual(self.host.loaded[task.name], task.read_bytes())
        self.assertNotEqual(self.host.loaded[task.name], loaded_before)
        self.assertEqual(self.paths.config.read_bytes(), source)
        # Local degradation is not a successful render, even with matching files.
        from boring_crocodile.site_host import Host
        self.host.fail = "readiness"
        with patch.object(self.host, "wait_ready", side_effect=lambda site: Host.wait_ready(
                self.host, site, window=0)), self.assertRaisesRegex(SiteConflict, "Rerun"):
            self.apply()
        self.assertEqual(self.plan()["changes"], [])
        self.host.fail = None
        self.host.calls.clear()
        outcome = self.apply()
        self.assertEqual(outcome["local_readiness"]["status"], "healthy")
        self.assertIn("not_checked", outcome["external_registration"])
        self.assertGreater(self.host.calls.index(["readiness"]),
                           self.host.calls.index(["/usr/bin/systemctl", "restart", CONTROLLER]))

    def test_extension_and_listener_transitions_preserve_dropins(self):
        self.apply()
        self.host.ponds["garden"]["enabled"] = True
        self.host.save()
        self.apply()
        dropin = self.paths.units / "boring-crocodile-worker-pond.service.d/local.conf"
        dropin.parent.mkdir()
        dropin.write_text("[Service]\nUMask=0077\n")
        self.host.calls.clear()
        self.host.fail = "--create"
        with self.assertRaisesRegex(SiteConflict, "Rerun"):
            self.apply()
        self.apply()  # Matching files still rerun tmpfiles and the repair tail.
        self.assertFalse(any("stop" in c for c in self.host.calls))
        self.assertEqual(dropin.read_text(), "[Service]\nUMask=0077\n")
        self.host.main["server"]["bind_port"] = 8504
        self.host.save()
        self.assertTrue(self.plan()["listener_transition"])
        self.host.calls.clear()
        self.apply()
        stop = next(i for i,c in enumerate(self.host.calls) if "stop" in c)
        reload = next(i for i,c in enumerate(self.host.calls) if "daemon-reload" in c)
        self.assertLess(stop, reload)
        self.assertIn(LISTENER, self.host.calls[stop])

    def test_park_remove_and_repair_never_delete_data(self):
        self.apply()
        home = self.host.path("/home/demo-pond")
        marker = home / "keep"
        marker.write_text("irreplaceable")
        self.host.main["site"]["state"] = "stopped"
        self.host.save()
        self.apply()
        self.assertFalse(any("restart" in c for c in self.plan()["tail"]))
        self.assertTrue((self.paths.units / LISTENER).exists())
        self.assertEqual(self.host.enabled, set())
        self.assertNotIn(LISTENER, self.host.active)
        # The common edit seam refuses invalid intermediate descriptions and
        # persists stop intent before parking the initial Pond.
        self.host.main["site"]["state"] = "running"
        self.host.save()
        self.apply()
        main, ponds = deepcopy(self.host.main), deepcopy(self.host.ponds)
        main["site"]["state"] = "stopped"
        ponds["pond"]["enabled"] = False
        candidate = SiteDescription.parse(main, ponds)
        original_write = site_render.write_file
        def interrupted(path, body, **options):
            if path.parent.name == "pond" and path.name == "config.toml":
                raise OSError("synthetic intent-write interruption")
            original_write(path, body, **options)
        with patch.object(site_render, "write_file", interrupted), self.assertRaisesRegex(SiteConflict, "Rerun"):
            self.apply(candidate=candidate)
        self.assertEqual(SiteDescription.read(self.paths.config).state, "stopped")
        self.apply(candidate=candidate)
        self.assertFalse(SiteDescription.read(self.paths.config).ponds["pond"].enabled)
        self.host.ponds["pond"]["enabled"] = False
        self.host.main["site"]["state"] = "removed"
        self.host.save()
        self.apply()
        self.assertFalse((self.paths.units / LISTENER).exists())
        self.apply()
        self.assertEqual(marker.read_text(), "irreplaceable")
        self.assertTrue(self.host.path(self.host.site.ponds["pond"].credential_file).exists())

    def test_stale_identity_mask_dropin_and_postcondition_fail_closed(self):
        self.apply()
        digest = self.plan()["plan_digest"]
        unit = self.paths.units / CONTROLLER
        before = unit.read_bytes()
        unit.write_bytes(before + b"# changed\n")
        with self.assertRaisesRegex(SiteConflict, "changed"):
            self.apply(digest=digest)
        unit.write_bytes(before)
        identity = self.host.identities.pop("demo-pond")
        with self.assertRaisesRegex(SiteConflict, "Identity"):
            self.plan()
        self.host.identities[identity.name] = identity
        unit.unlink()
        unit.symlink_to("/dev/null")
        with self.assertRaises(OSError):
            self.plan()
        unit.unlink()
        unit.write_bytes(before)
        dropin = unit.with_name(unit.name + ".d") / "unsafe.conf"
        dropin.parent.mkdir()
        dropin.write_text("[Service]\nExecStart=/bin/true\n")
        self.assertTrue(any(row.get("directive") == "ExecStart" and row.get("warning")
                            for row in self.plan()["adjustments"]))
        dropin.unlink()
        regular = self.host.root / "operator-policy"
        regular.write_text("[Service]\nIPAddressDeny=any\n")
        for target in (regular, Path("/dev/null")):
            dropin.symlink_to(target)
            self.host.calls.clear()
            with self.assertRaises(OSError):
                self.apply()
            self.assertFalse(any("restart" in call or "stop" in call for call in self.host.calls))
            dropin.unlink()
        shared = self.paths.units / "boring-crocodile-.service.d" / "shared.conf"
        shared.parent.mkdir()
        shared.write_text("[Service]\nExecStart=/bin/true\n")
        self.assertTrue(any(row.get("directive") == "ExecStart" and row.get("warning")
                            for row in self.plan()["adjustments"]))
        self.assertEqual(shared.read_text(), "[Service]\nExecStart=/bin/true\n")
        shared.unlink()
        actual_group = self.host.group
        with patch.object(self.host, "group", side_effect=lambda n: None if n == "pond-pirates" else actual_group(n)):
            with self.assertRaisesRegex(SiteConflict, "group provisioning"):
                self.plan()
        hardlink = self.paths.units / "hardlink"
        hardlink.hardlink_to(unit)
        with self.assertRaisesRegex(SiteConflict, "Unsafe site file"):
            self.plan()
        hardlink.unlink()
        unit.chmod(0o666)
        with self.assertRaisesRegex(SiteConflict, "Unsafe site file"):
            self.plan()
        unit.chmod(0o644)
        unknown = self.paths.units / "boring-crocodile-worker-undeclared.service"
        unknown.write_text("[Service]\\nExecStart=/bin/true\\n")
        with self.assertRaisesRegex(SiteConflict, "Undeclared"):
            self.plan()
        unknown.unlink()
        # Unknown LoadState, failed stop and lingering tasks refuse before writes.
        self.host.main["site"]["state"] = "stopped"
        self.host.save()
        self.host.properties[("LoadState", CONTROLLER)] = "unknown"
        with self.assertRaisesRegex(SiteConflict, "loaded unit"):
            self.apply()
        self.assertEqual(unit.read_bytes(), before)
        self.host.properties.clear()
        self.host.properties[("ActiveState", CONTROLLER)] = "activating"
        with self.assertRaisesRegex(SiteConflict, "did not stop"):
            self.apply()
        self.assertEqual(unit.read_bytes(), before)
        self.host.properties.clear()
        task = "boring-crocodile-task-pond@123.service"
        self.host.active.add(task)
        self.host.stubborn.add(task)
        with self.assertRaisesRegex(SiteConflict, "did not stop"):
            self.apply()
        self.assertEqual(unit.read_bytes(), before)
        self.host.stubborn.clear()
        self.apply()
        self.assertNotIn(task, self.host.active)
        self.host.main["site"]["state"] = "running"
        self.host.save()
        self.host.fail = "is-active"
        with self.assertRaisesRegex(SiteConflict, "Rerun"):
            self.apply()
        self.apply()

    def test_process_facts_use_named_properties_and_only_enabled_workers(self):
        from boring_crocodile.site_host import Host
        from types import SimpleNamespace
        response = SimpleNamespace(stdout="MainPID=734\nActiveState=active\n")
        with patch.object(self.host, "command", return_value=response) as command:
            facts = Host.processes(self.host, self.host.site)
        self.assertEqual(facts, {CONTROLLER: ("active", 734),
                                 "boring-crocodile-worker-pond.service": ("active", 734)})
        self.assertEqual(command.call_count, 2)
        for call in command.call_args_list:
            self.assertEqual(call.args[0][2:6], ["-p", "ActiveState", "-p", "MainPID"])

    def test_readiness_requires_ten_seconds_of_unchanged_active_pids(self):
        from boring_crocodile import site_host
        healthy = self.host.runtime_report(self.host.site, None)
        stable = healthy["processes"]
        clock = [0.0]
        observations = []
        def sleep(seconds):
            clock[0] += seconds
        def report(site, deadline):
            observations.append(clock[0])
            return deepcopy(healthy)
        with patch.object(self.host, "runtime_report", side_effect=report), patch.object(
                site_host.time, "monotonic", side_effect=lambda: clock[0]), patch.object(
                site_host.time, "sleep", side_effect=sleep):
            self.assertEqual(site_host.Host.wait_ready(self.host, self.host.site), healthy)
        self.assertEqual(observations[-1], 10.0)
        # Every observation looks healthy, but the worker keeps being restarted.
        clock[0] = 0
        def oscillating(site, deadline):
            value = deepcopy(healthy)
            value["processes"]["boring-crocodile-worker-pond.service"] = (
                "active", 200 + int(clock[0]) // 3)
            return value
        with patch.object(self.host, "runtime_report", side_effect=oscillating), patch.object(
                site_host.time, "monotonic", side_effect=lambda: clock[0]), patch.object(
                site_host.time, "sleep", side_effect=sleep):
            with self.assertRaisesRegex(SiteConflict, "required_stable_seconds"):
                site_host.Host.wait_ready(self.host, self.host.site, window=20)
        self.assertEqual(clock[0], 20)
        # Inactivity resets the interval even if the same PID reappears.
        clock[0] = 0
        def inactive(site, deadline):
            value = deepcopy(healthy)
            if clock[0] == 5:
                value["processes"]["boring-crocodile-mcp.service"] = ("activating", 101)
            return value
        with patch.object(self.host, "runtime_report", side_effect=inactive), patch.object(
                site_host.time, "monotonic", side_effect=lambda: clock[0]), patch.object(
                site_host.time, "sleep", side_effect=sleep):
            site_host.Host.wait_ready(self.host, self.host.site)
        self.assertEqual(clock[0], 16)
        self.assertEqual(healthy["processes"], stable)


    def test_complete_inspection_deadline_kills_the_process_group(self):
        import json
        import signal
        import subprocess
        from boring_crocodile import site_host
        with patch.object(site_host.subprocess, "Popen") as popen, patch.object(
                site_host.time, "monotonic", return_value=2), patch.object(site_host.os, "killpg") as kill:
            process = popen.return_value.__enter__.return_value
            process.returncode = 0
            process.communicate.return_value = (json.dumps({"status": "healthy"}), "")
            self.assertEqual(site_host.Host.runtime_report(self.host, self.host.site, 5), {"status": "healthy"})
            process.communicate.assert_called_once_with(timeout=3)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertEqual(popen.call_args.args[0][:4], [site_host.sys.executable, "-I", "-B", "-c"])
            self.assertEqual(popen.call_args.kwargs["cwd"], "/")
            kill.assert_not_called()
            process.communicate.reset_mock()
            process.communicate.side_effect = [subprocess.TimeoutExpired("inspection", 3), ("", "")]
            with self.assertRaises(subprocess.TimeoutExpired):
                site_host.Host.runtime_report(self.host, self.host.site, 5)
            kill.assert_called_once_with(process.pid, signal.SIGKILL)
            self.assertEqual(process.communicate.call_count, 2)
