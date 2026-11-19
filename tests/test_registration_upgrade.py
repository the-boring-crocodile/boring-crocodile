"""Upgrade and render leave Pirate compatibility to versioned registration."""
from copy import deepcopy
from pathlib import Path
import json
import sys
from types import SimpleNamespace

import boring_deploy
import pytest

from boring_crocodile import site_render
from boring_crocodile.site_model import SiteDescription
from site_host import FakeHost
from test_deploy_steps import steps


@pytest.mark.parametrize("state", [None, "running", "stopped"])
@pytest.mark.parametrize("peer_version", ["0.3.11", "not-a-version"])
def test_preflight_does_not_stage_or_gate_a_pirate_peer(steps, tmp_path, monkeypatch, state, peer_version):
    module, ctx, directory = steps
    if state:
        (directory / "config.toml").write_text(f'[site]\nstate="{state}"\n')
    peer = tmp_path / "pirates"
    (peer / "current").mkdir(parents=True)
    (peer / "current/VERSION").write_text(peer_version)
    homes = tmp_path / "homes.json"
    homes.write_text(json.dumps({"boring-pirates": str(peer)}))
    monkeypatch.setattr(boring_deploy, "HOMES", homes)
    # Even retained metadata from the replaced mechanism cannot reinstate it.
    ctx.release.details["pirates_requires"] = ">=0.4.0rc8,<0.5"
    calls, logged = [], []
    def run(*command, **kwargs):
        calls.append(command)
        if module._SITE_STATE in command:
            return SimpleNamespace(stdout=state, returncode=0)
        return SimpleNamespace(stdout="", returncode=0)
    ctx.run.side_effect = run
    ctx.log = logged.append
    assert module.preflight(ctx) == []
    if state:
        probe = calls.pop(0)
        assert probe[:4] == (sys.executable, "-I", "-B", "-c")
        assert probe[4] == module._SITE_STATE and probe[-1] == str(directory / "config.toml")
    assert calls == ([("systemctl", "list-units", "--all", "--plain", "--no-legend",
                       "--no-pager", "boring-crocodile-*")] if state == "stopped" else [])
    assert logged == []


def test_stopped_preflight_still_refuses_a_live_crocodile_worker(steps):
    module, ctx, directory = steps
    (directory / "config.toml").write_text('[site]\nstate="stopped"\n')
    def run(*command, **kwargs):
        if module._SITE_STATE in command:
            return SimpleNamespace(returncode=0, stdout="stopped")
        return SimpleNamespace(stdout="boring-crocodile-worker-pond.service loaded active running fixture\n")
    ctx.run.side_effect = run
    with pytest.raises(boring_deploy.Failure, match="all Crocodile units stopped"):
        module.preflight(ctx)


def test_render_and_reactivation_do_not_require_interpreter_release_metadata(tmp_path, monkeypatch):
    host = FakeHost(tmp_path / "host")
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "ordinary-python"))
    proposal, _ = site_render.plan(paths=host.paths, host=host)
    assert site_render.apply(proposal["plan_digest"], paths=host.paths, host=host)["state"] == "running"
    stopped = deepcopy(host.main)
    stopped["site"]["state"] = "stopped"
    candidate = SiteDescription.parse(stopped, host.ponds)
    proposal, _ = site_render.plan(paths=host.paths, host=host, candidate=candidate)
    assert site_render.apply(proposal["plan_digest"], paths=host.paths, host=host,
                             candidate=candidate)["state"] == "stopped"
    running = SiteDescription.parse(host.main, host.ponds)
    proposal, _ = site_render.plan(paths=host.paths, host=host, candidate=running)
    assert site_render.apply(proposal["plan_digest"], paths=host.paths, host=host,
                             candidate=running)["state"] == "running"
