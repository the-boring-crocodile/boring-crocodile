import asyncio
import tempfile
import unittest
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from boring_crocodile.control_plane import create_control_plane
from boring_worker_kit.services import CALL_TIMEOUT_SECONDS
from boring_crocodile.settings import Settings
from pond_config import one_pond_toml


class ControlPlaneTest(unittest.TestCase):
    def test_registers_worker_backed_exec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "config.toml").write_text(one_pond_toml(root))
            settings = Settings.load(root / "config.toml")
            tools = {
                tool.name: tool
                for tool in __import__("asyncio").run(
                    create_control_plane(settings).list_tools()
                )
            }
            self.assertEqual(
                set(tools),
                {"pond_profile", "pond_ping", "pond_bootstrap", "pond_limits", "exec",
                 "job_start", "job_list", "job_status", "job_output", "job_stop",
                 "job_wait", "pirate_list", "pirate_describe", "service_list", "service_describe", "service_call", "file_read", "file_write", "file_patch",
                 "file_list", "file_search", "file_read_bytes", "file_write_bytes", "batch"},
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
                    ("pond_limits", {"environment": "pond"}, "environment"),
                    ("pirate_describe", {"pirate": "parrot", "flavour": "default",
                                         "assignment_id": "a"}, "assignment_id")):
                with self.subTest(tool=name), self.assertRaises(ToolError) as caught:
                    asyncio.run(server.call_tool(name, arguments))
                self.assertIn(field, str(caught.exception))
                self.assertIn("unknown:", str(caught.exception))
            # Known arguments still pass validation and reach the Pond
            # binding, which refuses a call without a token.
            result = asyncio.run(server.call_tool("job_status", {"job_id": "0" * 32}))
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
            settings = Settings.load(root / "config.toml")
            server = create_control_plane(settings)
            published = {tool.name: tool for tool in asyncio.run(server.list_tools())}
            self.assertNotIn("title", published["file_write"].input_schema)
            self.assertIn("title", published["pond_bootstrap"].input_schema)
            self.assertIn("expected_sha256", published["file_write"].input_schema["required"])
            self.assertEqual(published["file_read_bytes"].input_schema["properties"]["length"]["maximum"], 65536)
            source = {"alpha": [1, {"text": "Grüße\nsecond"}], "none": None}
            token = SimpleNamespace(subject=PROFILE_ID, resource="https://example.test/mcp/pond")
            with patch("boring_crocodile.control_plane.get_access_token", return_value=token):
                with patch("boring_crocodile.control_plane.WorkerClient.limits", new=AsyncMock(return_value=source)):
                    result = asyncio.run(server.call_tool("pond_limits", {}))
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
                refusal = {"code": "stale", "message": "file changed", "current_sha256": "a" * 64}
                with patch("boring_crocodile.control_plane.WorkerClient.file_call",
                           new=AsyncMock(side_effect=RefusalError(refusal))):
                    outcome = asyncio.run(server.call_tool("file_read", {"root": "home", "path": "x"}))
                self.assertTrue(outcome.is_error)
                self.assertEqual(outcome.structured_content, refusal)
                self.assertEqual(json.loads(outcome.content[0].text), refusal)
                with patch("boring_crocodile.control_plane.WorkerClient.service_call",
                           new=AsyncMock(side_effect=RefusalError({"code": "not_found", "message": "no canvas"}))):
                    outcome = asyncio.run(server.call_tool("service_call", {
                        "service": "canvas", "tool": "get_elements", "arguments": {"name": "x"}}))
                self.assertEqual(outcome.structured_content["code"], "not_found")
                with patch("boring_crocodile.control_plane.WorkerClient.file_call",
                           new=AsyncMock(side_effect=FailureReport("worker disconnected; effect unknown"))):
                    outcome = asyncio.run(server.call_tool("file_read", {"root": "home", "path": "x"}))
                self.assertTrue(outcome.is_error)
                self.assertEqual(outcome.structured_content,
                                 {"failure": "worker disconnected; effect unknown"})
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
            with socket.socket() as reserve:
                reserve.bind(("127.0.0.1", 0))
                port = reserve.getsockname()[1]
            config = root / "config.toml"
            config.write_text(f'''
[server]
public_base_url = "https://startup.test"
bind_host = "127.0.0.1"
bind_port = {port}
allowed_hosts = ["127.0.0.1:{port}"]
[auth]
oauth_database_file = "{root}/oauth.sqlite3"
dynamic_registration = true
[ponds.pond]
profile_id = "11111111-1111-4111-8111-111111111111"
display_name = "Synthetic startup probe"
environment = "pond"
approval_secret_file = "{root}/synthetic-secret"
context_root = "{root}"
files = ["contract.md"]
[environments.pond]
socket_path = "{root}/worker.sock"
default_cwd = "{root}"
allowed_roots = ["{root}"]
control_plane_uid = {os.getuid()}
command_execution_enabled = true
''')
            (root / "synthetic-secret").write_text("synthetic-startup-only")
            (root / "synthetic-secret").chmod(0o600)
            (root / "contract.md").write_text("Synthetic test context")
            env = dict(os.environ, BORING_CROCODILE_CONFIG=str(config))
            with (root / "startup.log").open("w+") as log:
                process = subprocess.Popen(
                    [sys.executable, "-m", "boring_crocodile.control_plane"],
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
                        f"http://127.0.0.1:{port}", root / "synthetic-secret",
                        "pond", emit=safe_output.append)
                    self.assertEqual(stages[-1], {"stage": "pond_profile", "status": 200})
                    self.assertNotIn("synthetic-startup-only", str(safe_output))

                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
