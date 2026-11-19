import asyncio
from importlib.metadata import version
import tempfile
import unittest
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from boring_crocodile.control_plane import create_control_plane
from boring_worker_kit.services import CALL_TIMEOUT_SECONDS
from boring_crocodile.settings import Settings
from pond_config import one_pond_toml, runtime_settings


class ControlPlaneTest(unittest.TestCase):
    def test_registers_worker_backed_exec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "config.toml").write_text(one_pond_toml(root))
            settings = runtime_settings(root / "config.toml")
            tools = {
                tool.name: tool
                for tool in __import__("asyncio").run(
                    create_control_plane(settings).list_tools()
                )
            }
            self.assertEqual(
                set(tools),
                {"pond_bootstrap", "exec",
                 "job_start", "job_list", "job_output", "job_stop",
                 "job_wait", "pirate_list", "pirate_describe", "service_list", "service_describe", "service_call", "file_read", "file_write", "file_patch",
                 "file_list", "file_search", "file_read_bytes", "file_write_bytes", "file_read_image", "batch"},
            )

            # The service call deadline is stated where the caller reads it.
            self.assertIn(f"has at most {CALL_TIMEOUT_SECONDS:g} s",
                          tools["service_call"].description)
            self.assertIn("may have run", tools["service_call"].description)

            bootstrap = tools["pond_bootstrap"]
            self.assertTrue(bootstrap.annotations.read_only_hint)
            self.assertFalse(bootstrap.annotations.destructive_hint)
            self.assertTrue(bootstrap.annotations.idempotent_hint)
            self.assertFalse(bootstrap.annotations.open_world_hint)

            # Every tool refuses arguments it does not take, and says so.
            for tool in tools.values():
                self.assertIs(tool.input_schema.get("additionalProperties"), False, tool.name)
                # The connection's Pond decides; no tool takes an environment.
                self.assertNotIn("environment", tool.input_schema.get("properties", {}), tool.name)
            server = create_control_plane(settings)
            self.assertEqual(server.version, version("boring-crocodile-mcp"))
            with self.assertRaises(ToolError) as caught:
                asyncio.run(server.call_tool("job_start", {
                    "command": "true", "idempotency_key": "retired"}))
            self.assertIn("idempotency_key", str(caught.exception))
            self.assertIn("unknown:", str(caught.exception))
            # The same for the new Pirate payload and job_wait: a stale
            # pirate_submit/pirate_wait habit is refused by field name.
            for name, arguments, field in (
                    ("job_start", {"pirate": {"pirate": "parrot", "flavour": "default",
                                              "assignment": "a", "profile": "x"}}, "profile"),
                    ("job_wait", {"job_ids": ["0" * 32], "mode": "any"}, "mode"),
                    ("exec", {"command": "true", "environment": "pond"}, "environment"),
                    ("job_output", {"job_id": "0" * 32, "environment": "pond"}, "environment"),
                    ("pirate_list", {"environment": "pond"}, "environment"),
                    ("pirate_describe", {"pirate": "parrot", "flavour": "default",
                                         "assignment_id": "a"}, "assignment_id")):
                with self.subTest(tool=name), self.assertRaises(ToolError) as caught:
                    asyncio.run(server.call_tool(name, arguments))
                self.assertIn(field, str(caught.exception))
                self.assertIn("unknown:", str(caught.exception))
            # Known arguments still pass validation and reach the Pond
            # binding, which refuses a call without a token.
            result = asyncio.run(server.call_tool("job_wait", {"job_ids": ["0" * 32], "wait_seconds": 0}))
            self.assertTrue(result.is_error)
            self.assertEqual(result.structured_content["code"], "forbidden")
            self.assertIn("Pond-bound connection", result.structured_content["message"])


    def test_backport_presentation_preserves_structured_results_and_bootstrap(self):
        import json
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from pond_config import PROFILE_ID
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.toml").write_text(one_pond_toml(root))
            settings = runtime_settings(root / "config.toml")
            server = create_control_plane(settings)
            published = {tool.name: tool for tool in asyncio.run(server.list_tools())}
            self.assertNotIn("title", published["file_write"].input_schema)
            self.assertIn("title", published["pond_bootstrap"].input_schema)
            self.assertIn("expected_sha256", published["file_write"].input_schema["required"])
            self.assertEqual(published["file_read_bytes"].input_schema["properties"]["length"]["maximum"], 65536)
            source = {"alpha": [1, {"text": "Grüße\nsecond"}], "none": None}
            token = SimpleNamespace(subject=PROFILE_ID, resource="https://example.test/mcp/pond")
            with patch("boring_crocodile.control_plane.get_access_token", return_value=token):
                with patch("boring_crocodile.control_plane.WorkerClient.job_wait", new=AsyncMock(return_value=source)):
                    result = asyncio.run(server.call_tool("job_wait", {"job_ids": ["0" * 32], "wait_seconds": 0}))
                    self.assertEqual(result.structured_content, source)
                    self.assertEqual(json.loads(result.content[0].text), source)
                    self.assertNotIn("\n", result.content[0].text)
                    compact_size = len(result.content[0].text.encode())
                    self.assertLess(compact_size, len(json.dumps(source, indent=2).encode()))
                documents = {"files": [{"path": "contract.md", "content": "FULL CONTRACT\n"}]}
                with patch("boring_crocodile.control_plane.WorkerClient.request", new=AsyncMock(return_value=documents)):
                    with patch("boring_crocodile.control_plane.load_shared_guide", return_value={"content": "FULL GUIDE"}):
                        bootstrap = asyncio.run(server.call_tool("pond_bootstrap", {}))
                    self.assertEqual(bootstrap.structured_content["files"], documents["files"])
                    self.assertEqual(bootstrap.structured_content["mcp_guide"], {"content": "FULL GUIDE"})
                    self.assertIn("\n", bootstrap.content[0].text)
                from boring_crocodile.outcomes import RefusalError, FailureReport
                refusal = {"status": "refused", "code": "stale", "message": "file changed", "current_sha256": "a" * 64}
                with patch("boring_crocodile.control_plane.WorkerClient.file_call",
                           new=AsyncMock(side_effect=RefusalError(refusal))):
                    outcome = asyncio.run(server.call_tool("file_read", {"root": "home", "path": "x"}))
                self.assertTrue(outcome.is_error)
                self.assertEqual(outcome.structured_content, refusal)
                self.assertEqual(json.loads(outcome.content[0].text), refusal)
                with patch("boring_crocodile.control_plane.WorkerClient.service_call",
                           new=AsyncMock(side_effect=RefusalError({"status": "refused", "code": "not_found", "message": "no canvas"}))):
                    outcome = asyncio.run(server.call_tool("service_call", {
                        "service": "canvas", "tool": "get_elements", "arguments": {"name": "x"}}))
                self.assertEqual(outcome.structured_content["code"], "not_found")
                with patch("boring_crocodile.control_plane.WorkerClient.file_call",
                           new=AsyncMock(side_effect=FailureReport("worker_interrupted", "worker disconnected; effect unknown", effects="unknown"))):
                    outcome = asyncio.run(server.call_tool("file_read", {"root": "home", "path": "x"}))
                self.assertTrue(outcome.is_error)
                self.assertEqual(outcome.structured_content,
                                 {"status": "failed", "code": "worker_interrupted", "message": "worker disconnected; effect unknown", "effects": "unknown"})
            with self.assertRaises(ToolError) as caught:
                asyncio.run(server.call_tool("file_read_bytes", {"root": "work", "path": "x",
                                                                 "offset": -1, "secret": "DO-NOT-ECHO"}))
            self.assertIn("unknown: secret", str(caught.exception))
            self.assertIn("offset", str(caught.exception))
            self.assertNotIn("DO-NOT-ECHO", str(caught.exception))
            self.assertNotIn("errors.pydantic.dev", str(caught.exception))

    def test_production_welcome_invites_reading_and_confirmation(self):
        from boring_crocodile.instructions import SERVER_INSTRUCTIONS, WELCOME
        # Pre-bootstrap rules include shared-host edits and retained-job recovery.
        self.assertLessEqual(len(SERVER_INSTRUCTIONS), 768)
        for term in ("lost context", "file_patch", "read hash", "job_wait", "refusal"):
            self.assertIn(term, SERVER_INSTRUCTIONS)
        self.assertIn("pond_bootstrap", SERVER_INSTRUCTIONS)
        self.assertIn("not orders", SERVER_INSTRUCTIONS)
        # Several Pond connectors in one chat: stay with this one.
        self.assertIn("other Pond connectors", SERVER_INSTRUCTIONS)
        self.assertIn("context is loaded", WELCOME)
        self.assertIn("incomplete", WELCOME)


class ControllerStartupTest(unittest.TestCase):
    def test_real_entrypoint_listens_and_rejects_unauthenticated_request(self):
        """Exercise startup through a real loopback listener, not ASGI injection."""
        import http.client
        import os
        import socket
        import subprocess
        import sys
        import time

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reserve = socket.socket()
            self.addCleanup(reserve.close)
            reserve.bind(("127.0.0.1", 0))
            reserve.listen()
            port = reserve.getsockname()[1]
            config = root / "config.toml"
            from copy import deepcopy
            from test_setup_services import description
            from boring_crocodile.site_model import SiteDescription, toml_document
            site = description()
            main = deepcopy(site.document)
            pond = deepcopy(site.ponds["pond"].document)
            main["server"].update(public_base_url="https://startup.test", bind_port=port,
                                  allowed_hosts=[f"127.0.0.1:{port}"])
            main["auth"].update(oauth_database_file=str(root/"oauth.sqlite3"), dynamic_registration=True)
            main["pirate_services"] = {"socket": str(root/"authority.sock")}
            pond["credential_file"] = str(root/"approval-secret")
            pond["pond"].update(context_root=str(root), files=["contract.md"])
            pond["pond"].pop("welcome_file")
            pond["execution"].update(default_cwd=str(root), allowed_roots=[str(root)],
                                     files={"roots": {"fixture": {"path": str(root), "read": True, "write": True}}})
            SiteDescription.parse(main, {"pond": pond})
            config.write_text(toml_document(main))
            declaration = root / "ponds/pond/config.toml"
            declaration.parent.mkdir(parents=True)
            declaration.write_text(toml_document(pond))
            # The controller must start even while retirement has sealed an
            # unrelated parked declaration. Its generated scope excludes it.
            sealed = root / "ponds/parked"
            sealed.mkdir(mode=0o700)
            (sealed / "config.toml").write_text("sealed; must never be read by this controller")
            (sealed / "config.toml").chmod(0o600)
            (root / "approval-secret").write_text("synthetic-startup-approval-secret")
            (root / "approval-secret").chmod(0o600)
            (root / "contract.md").write_text("Synthetic test context")
            env = dict(os.environ, BORING_CROCODILE_CONFIG=str(config),
                       LISTEN_FDS="1", LISTEN_FDNAMES="controller")
            # Simulate PID 1 passing fd 3 and its child PID to the real entrypoint.
            # Only the systemd credential namespace is replaced for this
            # unprivileged process. The entrypoint loads the real typed files.
            activated = ("import os, runpy; from pathlib import Path; "
                         "import boring_crocodile.site_model as model; "
                         f"model.CONTROL_CREDENTIALS = Path({str(root)!r}); "
                         f"os.dup2({reserve.fileno()}, 3); "
                         "os.environ[\'LISTEN_PID\'] = str(os.getpid()); "
                         "runpy.run_module(\'boring_crocodile.control_plane\', run_name=\'__main__\')")
            with (root / "startup.log").open("w+") as log:
                process = subprocess.Popen(
                    [sys.executable, "-c", activated, "--ponds", "pond"], pass_fds=(reserve.fileno(),),
                    env=env, stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 15
                    status = None
                    while time.monotonic() < deadline and process.poll() is None:
                        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=.5)
                        try:
                            connection.request("POST", "/mcp/pond", body="{}", headers={
                                "Content-Type": "application/json",
                                "Accept": "application/json, text/event-stream"})
                            status = connection.getresponse().status
                            break
                        except (OSError, http.client.HTTPException):
                            time.sleep(.05)
                        finally:
                            connection.close()
                    log.flush()
                    log.seek(0)
                    self.assertEqual(status, 401, log.read()[-4000:])
                    # Exercise the same probe another Pond can use against the installed guest.
                    import importlib.util
                    spec = importlib.util.spec_from_file_location(
                        "guest_auth_probe", "tests/fixtures/guest_auth_probe.py")
                    probe = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(probe)
                    safe_output = []
                    stages = probe.run(
                        f"http://127.0.0.1:{port}", root / "approval-secret",
                        "pond", emit=safe_output.append)
                    self.assertEqual(stages[-1], {"stage": "tools_list", "status": 200})
                    self.assertNotIn("synthetic-startup-approval-secret", str(safe_output))

                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
