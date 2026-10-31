"""deploy/steps.py (the Crocodile's deploy steps) where they meet the site: the
deploy holds the site lock, so a deploy and a site change refuse each other."""
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

from boring_crocodile.site_layout import SiteLocked, site_lock

ROOT = Path(__file__).resolve().parents[1]


class Failure(Exception):
    def __init__(self, message, hint=""):
        super().__init__(message)
        self.hint = hint


@pytest.fixture
def steps(monkeypatch, tmp_path):
    try:
        import boring_deploy  # noqa: F401  (the family check has it)
    except ImportError:  # the steps need only its Failure here
        stub = types.ModuleType("boring_deploy")
        stub.Failure = Failure
        monkeypatch.setitem(sys.modules, "boring_deploy", stub)
    spec = importlib.util.spec_from_file_location("crocodile_deploy_steps", ROOT / "deploy" / "steps.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = tmp_path / "etc-boring-crocodile"
    state.mkdir(mode=0o700)
    ctx = types.SimpleNamespace(release=types.SimpleNamespace(details={"config": str(state / "config.toml")}))
    return module, ctx, state


def test_a_deploy_and_a_site_change_refuse_each_other(steps):
    module, ctx, state = steps
    with site_lock(state, owner=os.geteuid()):  # a site command is running
        with pytest.raises(Exception, match="a change to the site is running"):
            with module.lock(ctx):
                pass
    with module.lock(ctx):  # the deploy holds it: a site command refuses
        with pytest.raises(SiteLocked):
            with site_lock(state, owner=os.geteuid()):
                pass
    with site_lock(state, owner=os.geteuid()):  # released again
        pass


def test_no_site_no_lock_and_nothing_created(steps):
    module, ctx, state = steps
    with module.lock(ctx):
        pass
    assert list(state.iterdir()) == []


def test_an_unsafe_site_lock_is_refused(steps):
    module, ctx, state = steps
    (state / "site.lock").write_text("")
    os.chmod(state / "site.lock", 0o644)
    with pytest.raises(Exception, match="unsafe site lock"):
        with module.lock(ctx):
            pass


def test_units_of_a_stopped_site_are_still_listed(steps):
    """`disable --now` unloads the site's units; the deploy must still see them,
    so the engine can say the site is stopped and leave it so."""
    module, ctx, state = steps
    (state / "config.toml").write_text("")
    calls = []

    def run(*command, **kw):
        calls.append(command)
        return types.SimpleNamespace(stdout="boring-crocodile-mcp.service         disabled enabled\n"
                                            "boring-crocodile-worker-pond.service disabled enabled\n")
    ctx.run = run
    assert module.units(ctx) == ["boring-crocodile-mcp.service", "boring-crocodile-worker-pond.service"]
    assert calls[0][:2] == ("systemctl", "list-unit-files")


def _smoke(module, monkeypatch, before, after):
    clock = iter(range(0, 10_000, 30))  # each look 30 s later: the minute runs out quickly
    monkeypatch.setattr(module, "time", types.SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda s: None))
    monkeypatch.setattr(module, "_configured", lambda ctx: True)
    monkeypatch.setattr(module, "_checks", lambda ctx, release: dict(after))
    module._before.clear()
    module._before.update(before)
    logged = []
    ctx = types.SimpleNamespace(home=Path("/nonexistent"), log=logged.append)
    module.smoke(ctx)
    return logged


def test_smoke_tolerates_an_old_failure_but_never_a_dead_controller(steps, monkeypatch):
    """A Pond check failing before and after is reported, not fatal. The
    controller is: a deploy over a site whose controller was already down said
    \"is live\" (after a rollback hit the start limit)."""
    module, _, _ = steps
    logged = _smoke(module, monkeypatch, {"site.controller": True, "tim.worker_active": False},
                    {"site.controller": True, "tim.worker_active": False})
    assert "failing before and after: tim.worker_active" in logged[-1]
    with pytest.raises(Exception, match="fail after the switch: site.controller"):
        _smoke(module, monkeypatch, {"site.controller": False}, {"site.controller": False})
    with pytest.raises(Exception, match="fail after the switch: pond.worker_active"):
        _smoke(module, monkeypatch, {"site.controller": True, "pond.worker_active": True},
               {"site.controller": True, "pond.worker_active": False})



def test_check_says_when_the_new_release_renders_site_files_differently(steps):
    """The deploy changes no site file; it says which ones --site-render would write."""
    import json
    module, ctx, state = steps
    report = {"irregular": [{"what": "differs from the Crocodile's template",
                             "path": "/etc/systemd/system/boring-crocodile-worker-pond.service"},
                            {"what": "drop-in sets a directive the Crocodile does not know", "path": "/x.conf"}]}
    logged = []
    ctx.new, ctx.log = Path("/new"), logged.append
    ctx.run = lambda *command, **kw: types.SimpleNamespace(stdout=json.dumps(report))
    module._render_due(ctx)
    assert logged == ["1 site files differ from what the new release renders (boring-crocodile-worker-pond.service); "
                      "the deploy changes none: afterwards, boring-crocodile-setup --site-render shows the plan"]
    logged.clear()
    ctx.run = lambda *command, **kw: types.SimpleNamespace(stdout=json.dumps({"irregular": []}))
    module._render_due(ctx)
    assert logged == []
