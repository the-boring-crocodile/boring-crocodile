"""A Pond's private receipt storage cannot veto the site's update or other tools."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from boring_crocodile import jobs
from boring_crocodile.handbook_context import ContextBootstrap
from boring_crocodile.outcomes import RefusalError
from boring_crocodile.settings import EnvironmentSettings
from boring_crocodile.task_state import ReceiptStore
from boring_crocodile.worker import WorkerServer
from boring_crocodile.worker_client import WorkerClient
from boring_worker_kit.file_policy import FilePolicy
from boring_worker_kit.host_limits import HostLimits
from test_deploy_steps import steps
from test_site_model import description
from boring_crocodile.site_model import toml_document


def database(home):
    root = home / ".boring-crocodile"
    root.mkdir(mode=0o700)
    path = root / "jobs.sqlite3"
    store = ReceiptStore(path, "jobs")
    store.create("completed")
    store.close()
    return path


@pytest.mark.parametrize("broken", ["schema", "corrupt", "journal", "state-link", "home-link"])
def test_root_preflight_never_opens_pond_receipts(steps, broken, monkeypatch):
    module, ctx, state = steps
    main, ponds = description()
    (state / "config.toml").write_text(toml_document(main))
    for name in ponds:
        home = state / name
        home.mkdir()
        path = database(home)
        if broken == "schema":
            with sqlite3.connect(path) as db:
                db.execute("PRAGMA user_version=0")
        elif broken == "corrupt":
            path.write_bytes(b"synthetic invalid database")
        elif broken == "journal":
            Path(str(path) + "-journal").write_bytes(b"synthetic pending journal")
        elif broken == "state-link":
            root = path.parent
            moved = home / "other-state"
            root.rename(moved)
            root.symlink_to(moved, target_is_directory=True)
        else:
            actual = state / (name + "-actual")
            home.rename(actual)
            home.symlink_to(actual, target_is_directory=True)
        declaration = state / "ponds" / name / "config.toml"
        declaration.parent.mkdir(parents=True)
        declaration.write_text(toml_document(ponds[name]))
    snapshot = {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in state.rglob("*") if p.is_file()}
    # A root-side parser must not touch even a synthetically valid store.
    monkeypatch.setattr(module.sqlite3, "connect",
                        Mock(side_effect=AssertionError("root parsed a Pond database")))
    def probe(*command, **kwargs):
        assert command[4] == module._SITE_STATE
        with patch.object(sys, "argv", ["-c", *command[5:]]), patch.object(sys, "path", list(sys.path)), \
             redirect_stdout(StringIO()) as output:
            exec(command[4], {})
        return SimpleNamespace(stdout=output.getvalue(), returncode=0)
    ctx.run.side_effect = probe
    assert module.preflight(ctx) == []
    assert snapshot == {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                        for p in state.rglob("*") if p.is_file()}
    ctx.run.assert_called_once()


@pytest.mark.parametrize("broken", ["schema", "corrupt", "directory", "state-link", "state-writer"])
def test_bad_receipts_leave_worker_tools_and_protocol_alive(tmp_path, broken):
    async def exercise():
        home = tmp_path / "pond"
        home.mkdir()
        (home / "HANDBOOK.md").write_text("synthetic agreement")
        path = database(home)
        if broken == "schema":
            with sqlite3.connect(path) as db:
                db.execute("PRAGMA user_version=0")
        elif broken == "corrupt":
            path.write_bytes(b"synthetic invalid database")
        elif broken == "directory":
            path.unlink()
            path.mkdir()
        elif broken == "state-link":
            root = path.parent
            moved = home / "other-state"
            root.rename(moved)
            root.symlink_to(moved, target_is_directory=True)
        else:
            path.parent.chmod(0o777)
        snapshot = {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()}
        settings = EnvironmentSettings(
            "pond", "Pond", home / "worker.sock", home, (home,), os.getuid(), 128,
            home / "task.sock", files=FilePolicy.parse({
                "roots": {"home": {"path": str(home), "read": True, "write": True}}}))
        context = ContextBootstrap(home, ("HANDBOOK.md",))
        with patch("boring_crocodile.worker.read_host_limits", return_value=HostLimits(2, 290)):
            worker = WorkerServer(settings, context)
        server = None
        try:
            server = await asyncio.start_unix_server(worker.handle, settings.socket_path)
            client = WorkerClient({"pond": settings})
            report = await client.request("pond", "bootstrap")
            assert report["handbook"]["documents"][0]["content"] == "synthetic agreement"
            caps = report["capabilities"]
            assert caps["command_execution_enabled"] is True
            assert caps["managed_jobs_enabled"] is False
            assert caps["managed_jobs_error"]
            assert "job_start" not in caps["batch"]["methods"]
            read = await client.request("pond", "file_read", root="home", path="HANDBOOK.md")
            assert read["content"] == "synthetic agreement"
            assert (await client.service_list("pond"))["services"] == []
            execute = AsyncMock(return_value={"exit_code": 0, "stdout": "synthetic", "stderr": ""})
            with patch.object(worker.executor, "execute", execute):
                assert (await client.execute("pond", "true", None, 1))["exit_code"] == 0
                execute.reset_mock()
                for operation, arguments in (
                    ("job_start", {"command": "true"}),
                    ("list_jobs", {}),
                    ("job_output", {"job_id": "a" * 32}),
                    ("job_stop", {"job_id": "a" * 32}),
                    ("job_wait", {"job_ids": ["a" * 32], "wait_seconds": 0}),
                ):
                    with pytest.raises(RefusalError) as caught:
                        await client.request("pond", operation, **arguments)
                    assert caught.value.code == "unavailable"
                    assert caught.value.data["component"] == "job_store"
                    assert "restart" in caught.value.data["message"]
                with pytest.raises(RefusalError) as caught:
                    await client.batch("pond", {"operations": [
                        {"id": "write", "method": "file_write", "arguments": {
                            "root": "home", "path": "must-not-exist",
                            "content": "no effect", "expected_sha256": None}},
                        {"id": "jobs", "method": "job_list", "arguments": {}},
                    ]})
                assert caught.value.code == "unavailable"
                assert not (home / "must-not-exist").exists()
                execute.assert_not_called()
            for name, content in snapshot.items():
                assert Path(name).read_bytes() == content
        finally:
            if server is not None:
                server.close()
                await server.wait_closed()
            worker.files.pool.shutdown(wait=True)
            if worker.jobs is not None:
                worker.jobs.store.close()

    asyncio.run(exercise())


def test_release_assembly_keeps_receipt_parser_out_of_root_deploy(tmp_path, monkeypatch):
    """Collect the builder's file map with every build/tool action mocked."""
    import importlib.util
    import sys
    from types import SimpleNamespace

    artifact, develop = Mock(), Mock()
    args = SimpleNamespace(allow_dirty=False, out=tmp_path, common_wheels=tmp_path,
                           python_version="3.14")
    develop.arguments.return_value.parse_args.return_value = args
    develop.new_directory.return_value = tmp_path
    develop.PYTHONS = {"3.14": "synthetic"}
    develop.git.return_value = "synthetic-commit"

    def wheels(root, folder, **kwargs):
        application = folder / "boring_crocodile_mcp-0.5.0rc17-py3-none-any.whl"
        application.write_text("synthetic file-map fixture, not a wheel")
        return [application]
    develop.wheelhouse.side_effect = wheels
    artifact.standalone_python.return_value = tmp_path / "synthetic-python"

    class Collected(Exception):
        pass

    def collect(out, release, steps, files):
        assert "deploy.d/task_state.py" not in files
        assert "deploy.d/site_layout.py" in files
        assert "deploy.d/release_compatibility.py" not in files
        assert "pirates_requires" not in release
        raise Collected
    artifact.assemble.side_effect = collect
    monkeypatch.setitem(sys.modules, "_develop", SimpleNamespace(load=lambda: (artifact, develop)))
    source = Path(__file__).resolve().parents[1] / "scripts/build.py"
    spec = importlib.util.spec_from_file_location("receipt_isolation_build", source)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    with pytest.raises(Collected):
        builder.main()
    artifact.assemble.assert_called_once()


def test_failed_receipt_recovery_closes_the_open_store(tmp_path):
    settings = EnvironmentSettings("pond", "Pond", tmp_path / "worker.sock",
        tmp_path, (tmp_path,), os.getuid(), 128, tmp_path / "task.sock")
    store = Mock()
    store.mark_unfinished.side_effect = sqlite3.OperationalError("synthetic locked store")
    with patch.object(jobs, "ReceiptStore", return_value=store):
        with pytest.raises(sqlite3.OperationalError, match="synthetic locked"):
            jobs.ManagedJobs(settings, Mock())
    store.close.assert_called_once()
