"""Operator-input boundaries through the real Common guard, without root effects."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from boring_deploy import Failure, root_input
from boring_crocodile import setup, setup_apply, site_setup
from site_host import FakeHost


def inputs(source=None):
    return setup.SetupInputs.parse(dict(
        execution_user="demo-pond", control_user="demo-control", home="/home/demo-pond",
        context_mode="import" if source else "starter",
        context_source=str(source) if source else None,
        context_files=["HANDBOOK.md"] if source else []))


def policy():
    return dict(public_base_url="https://example.test", bind_host="127.0.0.1",
                bind_port=8503, runtime_root="/opt/boring-crocodile/current",
                memory_bytes=1024**3, swap_bytes=0, tasks_max=256, task_tasks_max=128)


@pytest.fixture
def admitted(monkeypatch):
    def check(path, **kwargs):
        return root_input(path, owner=os.getuid(), **kwargs)
    monkeypatch.setattr(setup, "root_input", check, raising=False)
    return check


@pytest.mark.parametrize("module,flag", [
    (setup, "--config"), (setup, "--site-config"), (site_setup, "--config")])
@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("unsafe", ["leaf-link", "parent-link", "writer"])
def test_json_refused_before_planning_or_saving(tmp_path, admitted, module, flag, apply, unsafe):
    source = tmp_path / "input.json"
    source.write_text(json.dumps(inputs().as_input() if flag == "--config" and module is setup else policy()))
    if unsafe == "leaf-link":
        link = tmp_path / "link.json"
        link.symlink_to(source)
        source = link
    elif unsafe == "parent-link":
        link = tmp_path / "link"
        link.symlink_to(tmp_path, target_is_directory=True)
        source = link / source.name
    else:
        source.chmod(0o666)
    args = [flag, str(source)] + (["--apply"] if apply else [])
    saved = tmp_path / "saved.json"
    if module is setup and flag == "--config":
        args += ["--save-inputs", str(saved)]
    errors = io.StringIO()
    with patch.object(setup, "inspect", side_effect=AssertionError("reached provisioning")), \
         patch.object(site_setup, "plan", side_effect=AssertionError("reached site plan")), \
         redirect_stderr(errors), redirect_stdout(io.StringIO()):
        assert module.main(args) == 2
    assert "operator input" in errors.getvalue()
    assert not saved.exists()


@pytest.mark.parametrize("module,flag", [
    (setup, "--config"), (setup, "--site-config"), (site_setup, "--config")])
def test_json_preview_uses_open_descriptor_and_reports_metadata(tmp_path, admitted, monkeypatch, module, flag):
    path = tmp_path / "input.json"
    data = inputs().as_input() if module is setup and flag == "--config" else policy()
    path.write_text(json.dumps(data))
    checked = []

    @contextmanager
    def substituted(source, **kwargs):
        with admitted(source, **kwargs) as opened:
            checked.append(str(source))
            path.rename(tmp_path / "original.json")
            path.write_text("{}")
            yield opened

    monkeypatch.setattr(setup, "root_input", substituted, raising=False)
    observed = []
    def provisioning(value):
        observed.append(value)
        return {"review": [], "documents": []}
    def site(value):
        observed.append(value)
        return {"changes": []}, None
    errors, output = io.StringIO(), io.StringIO()
    with patch.object(setup, "inspect", provisioning), patch.object(site_setup, "plan", site), \
         redirect_stderr(errors), redirect_stdout(output):
        assert module.main([flag, str(path)]) == 0
    assert checked == [str(path)]
    assert len(observed) == 1
    assert (observed[0] == inputs() if module is setup and flag == "--config"
            else observed[0].public_base_url == data["public_base_url"])
    assert str(path) in errors.getvalue()
    assert "uid " in errors.getvalue() and "mode " in errors.getvalue()
    json.loads(output.getvalue())


@pytest.mark.parametrize("unsafe", ["source-link", "leaf-link", "parent-link", "writer", "hard-link"])
def test_context_refuses_unsafe_inputs_before_provisioning_writes(tmp_path, admitted, unsafe):
    source = tmp_path / "source"
    source.mkdir()
    document = source / "HANDBOOK.md"
    document.write_text("synthetic handbook")
    value = inputs(source)
    if unsafe == "source-link":
        link = tmp_path / "source-link"
        link.symlink_to(source, target_is_directory=True)
        value = replace(value, context_source=str(link))
    elif unsafe == "leaf-link":
        document.rename(source / "actual.md")
        document.symlink_to(source / "actual.md")
    elif unsafe == "parent-link":
        (source / "nested").symlink_to(source, target_is_directory=True)
        value = replace(value, context_files=("nested/HANDBOOK.md",))
    elif unsafe == "writer":
        document.chmod(0o666)
    else:
        os.link(document, source / "other.md")
    host = FakeHost(tmp_path / "host", provisioned=False)
    with patch.object(setup_apply, "_directory", side_effect=AssertionError("reached first write")), \
         pytest.raises(Failure, match="operator input"):
        setup_apply.apply(value, "not-approved", host=host)
    assert not host.identities


def test_context_reads_admitted_bytes_and_reports_only_metadata(tmp_path, admitted, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    document = source / "HANDBOOK.md"
    document.write_text("synthetic private text")
    checked = []

    @contextmanager
    def substituted(path, **kwargs):
        with admitted(path, **kwargs) as opened:
            checked.append(str(path))
            if Path(path) == document:
                document.rename(source / "original.md")
                document.write_text("replacement")
            yield opened

    monkeypatch.setattr(setup, "root_input", substituted, raising=False)
    host = FakeHost(tmp_path / "host", provisioned=False)
    proposal = setup_apply.plan(inputs(source), host=host)
    assert str(document) in checked
    assert proposal["documents"][0]["bytes"] == len(b"synthetic private text")
    assert any(str(document) in row and "uid " in row and "mode " in row
               for row in proposal["operator_inputs"])
    assert "synthetic private text" not in str(proposal)
    assert "replacement" not in str(proposal)


def test_context_keeps_document_size_limit(tmp_path, admitted):
    source = tmp_path / "source"
    source.mkdir()
    (source / "HANDBOOK.md").write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="exceeds 1 MiB"):
        setup.context_payload(inputs(source))


def test_apply_seeds_the_bytes_admitted_before_first_write(tmp_path, admitted):
    source = tmp_path / "source"
    source.mkdir()
    document = source / "HANDBOOK.md"
    document.write_text("reviewed text")
    value = inputs(source)
    host = FakeHost(tmp_path / "host", provisioned=False)
    proposal = setup_apply.plan(value, host=host)
    directory = setup_apply._directory
    changed = False

    def first_write(*args, **kwargs):
        nonlocal changed
        if not changed:
            document.write_text("later replacement")
            changed = True
        return directory(*args, **kwargs)

    with patch.object(setup_apply, "_directory", first_write):
        result = setup_apply.apply(value, proposal["plan_digest"], host=host)
    assert result["status"] == "provisioned"
    assert (host.path(value.home) / "handbook/HANDBOOK.md").read_text() == "reviewed text"


@pytest.mark.skipif(os.getuid() == 0, reason="ordinary-user ownership boundary")
def test_production_guard_refuses_user_owned_input(tmp_path):
    path = tmp_path / "input.json"
    path.write_text("{}")
    with pytest.raises(Failure, match="expected 0"):
        setup.read_input_json(path)


def test_packaged_context_never_uses_operator_guard(monkeypatch):
    check = Mock(side_effect=AssertionError("release resource treated as operator input"))
    monkeypatch.setattr(setup, "root_input", check, raising=False)
    assert len(setup.context_payload(inputs())) == 3
    check.assert_not_called()
