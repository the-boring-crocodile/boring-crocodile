"""deploy/steps.py (the Crocodile's deploy steps) where they meet the site: the
deploy holds the site lock, so a deploy and a site change refuse each other."""
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

from deploy_context import deploy_context

from boring_crocodile import site_layout
from boring_crocodile.site_layout import SiteLocked, site_lock

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def steps(monkeypatch, tmp_path):
    # build.py copies this exact stdlib-only owner into deploy.d.
    monkeypatch.setitem(sys.modules, "site_layout", site_layout)
    spec = importlib.util.spec_from_file_location("crocodile_deploy_steps", ROOT / "deploy" / "steps.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = tmp_path / "etc-boring-crocodile"
    state.mkdir(mode=0o700)
    ctx = deploy_context(release=types.SimpleNamespace(directory=ROOT, details={"config": str(state / "config.toml")}))
    return module, ctx, state


def test_a_deploy_and_a_site_change_refuse_each_other(steps):
    module, ctx, state = steps
    with site_lock(state, owner=os.geteuid()):  # a site command is running
        with pytest.raises(Exception, match="change to the site is running"):
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


def test_completion_names_runtime_only_until_the_site_is_configured(steps):
    module, ctx, state = steps
    assert module.completion(ctx) == "runtime installed; site not configured yet"
    assert list(state.iterdir()) == []
    (state / "config.toml").write_text("")
    assert module.completion(ctx) is None  # normal live/stopped result belongs to the engine


def test_an_unsafe_site_lock_is_refused(steps):
    module, ctx, state = steps
    (state / "site.lock").write_text("")
    os.chmod(state / "site.lock", 0o644)
    with pytest.raises(Exception, match="Unsafe site lock"):
        with module.lock(ctx):
            pass


def test_generation_reads_the_exact_database_path_with_uri_characters(steps, monkeypatch):
    import json
    import sqlite3
    module, ctx, state = steps
    directory = state / "Notes #2"
    directory.mkdir()
    store = directory / "oauth.sqlite3"
    with sqlite3.connect(store) as db:
        db.execute("PRAGMA user_version=5")
        db.execute("CREATE TABLE keep_me (value TEXT)")
        db.execute("INSERT INTO keep_me VALUES ('known row')")
    (state / "config.toml").write_text("[auth]\noauth_database_file=" + json.dumps(str(store)))
    real_connect = sqlite3.connect
    def checked_connect(database, **options):
        db = real_connect(database, **options)
        assert db.execute("SELECT value FROM keep_me").fetchone() == ("known row",)
        return db
    monkeypatch.setattr(module.sqlite3, "connect", checked_connect)
    assert module.data_generation(ctx) == 5
    assert sorted(p.name for p in directory.iterdir()) == ["oauth.sqlite3"]


@pytest.mark.parametrize("capable", [False, True])
def test_way_back_uses_the_retained_releases_description_capability(steps, capable):
    module, ctx, state = steps
    previous = state / "previous"
    previous.mkdir()
    (previous / "VERSION").write_text("0.5.0rc10\n" if capable else "0.4.21\n")
    ctx.run.side_effect = lambda *command, **kw: types.SimpleNamespace(stdout="1\n" if capable else "0\n")
    hint = module.way_back(ctx, previous)
    assert ctx.run.call_args.args[:5] == (str(previous / "bin/python3"), "-I", "-B", "-c",
                                        "import importlib.util; print(int(importlib.util.find_spec('boring_crocodile.site_model') is not None))")
    if capable:
        assert hint is None  # the engine keeps its normal command
    else:
        assert hint == ("back to 0.4.21: follow OPERATIONS.md, section 'Way back across adoption' "
                        "(restore the matching legacy files and runtime)")
        assert "sudo" not in hint
    assert sorted(path.name for path in previous.iterdir()) == ["VERSION"]


def test_way_back_does_not_guess_when_the_retained_reader_probe_is_invalid(steps):
    import boring_deploy
    module, ctx, state = steps
    ctx.run.side_effect = lambda *command, **kw: types.SimpleNamespace(stdout="unknown\n")
    with pytest.raises(boring_deploy.Failure, match="cannot determine Crocodile description support"):
        module.way_back(ctx, state / "previous")


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
    ctx.run.side_effect = run
    assert module.units(ctx) == ["boring-crocodile-mcp.service", "boring-crocodile-worker-pond.service"]
    assert calls[0][:2] == ("systemctl", "list-unit-files")


def _smoke(module, monkeypatch, before, after, *, unknown=None):
    clock = iter(range(0, 10_000, 30))  # each look 30 s later: the minute runs out quickly
    monkeypatch.setattr(module, "time", types.SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda s: None))
    monkeypatch.setattr(module, "_configured", lambda ctx: True)
    monkeypatch.setattr(module, "_checks", lambda ctx, release: dict(after))
    module._before.clear()
    module._before.update(before)
    module._before_unknown = unknown
    logged = []
    ctx = deploy_context(home=Path("/nonexistent"), log=logged.append)
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



@pytest.mark.parametrize("drift", ["none", "changed", "missing", "permissions"])
def test_render_notice_uses_real_site_check_status(steps, drift):
    """The producer owns status and wording; only the CLI boundary is fake."""
    import json
    from boring_crocodile import site_check, site_render
    from site_host import FakeHost

    module, ctx, state = steps
    host = FakeHost(state / "host")
    proposal, _ = site_render.plan(paths=host.paths, host=host)
    site_render.apply(proposal["plan_digest"], paths=host.paths, host=host)
    unit = host.paths.units / "boring-crocodile-worker-pond.service"
    if drift == "changed":
        unit.write_text(unit.read_text() + "# changed\n")
    elif drift == "missing":
        unit.unlink()
    elif drift == "permissions":
        host.paths.config.chmod(0o640)
    report = site_check.site_check(paths=host.paths, host=host)
    assert report["status"] == ("regular" if drift == "none" else "irregular")
    logged = []
    ctx.new, ctx.log = Path("/new"), logged.append
    ctx.release.details["config"] = str(host.paths.config)
    ctx.run.side_effect = lambda *command, **kw: types.SimpleNamespace(stdout=json.dumps(report))
    module._render_due(ctx)
    assert logged == ([] if drift == "none" else [
        "site-check reports irregular site state with the new release; the deploy changes no site files: "
        "afterwards, boring-crocodile-setup --site-render shows the plan"])
    ctx.run.assert_called_once_with(
        "/new/bin/boring-crocodile-admin", "--config", str(host.paths.config),
        "site-check", "--json", check=False)


def _converted(steps, *, desired="stopped", capable=False):
    """Only the deploy hook's process boundary is fake; no deploy can run."""
    import json
    module, ctx, state = steps
    ctx.home, ctx.new = state / "home", state / "candidate"
    old = ctx.home / "current/bin/boring-crocodile-admin"
    old.parent.mkdir(parents=True)
    old.write_text("synthetic old executable; never executed")
    (state / "config.toml").write_text(
        f'[site]\nschema=1\nstate="{desired}"\n[auth]\noauth_database_file="{state}/oauth.sqlite3"\n')
    ctx.calls, ctx.logged, ctx.active = [], [], ""
    ctx.reader_error = None if capable else "ValueError: config: unknown keys [site]"
    ctx.status_error = ctx.reader_error
    ctx.log = ctx.logged.append

    def run(*command, **kw):
        ctx.calls.append(command)
        if command[0] == "systemctl":
            return types.SimpleNamespace(stdout=ctx.active)
        if "-c" in command:
            if module._SITE_STATE in command:
                return types.SimpleNamespace(returncode=0, stdout=desired)
            if any("Settings.load" in part for part in command):
                return types.SimpleNamespace(returncode=0, stdout=json.dumps(
                    {"unreadable": ctx.reader_error} if ctx.reader_error else {}))
            return types.SimpleNamespace(stdout="1\n" if capable else "0\n")
        if command[-1] == "list":
            return types.SimpleNamespace(stdout='[{"name":"pond"}]')
        if "site-check" in command:
            return types.SimpleNamespace(stdout=json.dumps({"irregular": []}))
        if command[-1] == "status":
            if ctx.status_error:
                import boring_deploy
                raise boring_deploy.Failure(ctx.status_error)
            return types.SimpleNamespace(stdout=json.dumps({
                "site": {"controller": {"active": "active"}},
                "ponds": [{"pond": "pond", "checks": {"worker_active": True}}]}))
        assert "backup" in command
        return types.SimpleNamespace(stdout="synthetic backup completed")
    ctx.run.side_effect = run
    return module, ctx, state



def test_cleaned_rc7_description_has_unknown_before_state_and_strict_smoke(steps, monkeypatch):
    module, ctx, state = _converted(steps, capable=True)
    reason = "ValueError: execution: missing keys ['allowed_roots']; unknown keys []"
    # rc7 has site_model, but requires redundant keys removed by the window cleanup.
    pond = state / "ponds/pond/config.toml"
    pond.parent.mkdir(parents=True)
    pond.write_text('[execution]\n[execution.files.roots.home]\n')
    ctx.reader_error = ctx.status_error = reason
    module.check(ctx)
    assert module.healthy(ctx) is None
    assert module._before == {}
    assert ctx.logged[-1] == "installed before-state is unknown: " + reason
    assert any(call[-1] == "status" for call in ctx.calls)
    assert any("Settings.load" in part for call in ctx.calls for part in call)
    _smoke(module, monkeypatch, {}, {"site.controller": True, "pond.worker_active": True}, unknown=reason)
    with pytest.raises(Exception, match="fail after the switch: pond.worker_active"):
        _smoke(module, monkeypatch, {}, {"site.controller": True, "pond.worker_active": False}, unknown=reason)


@pytest.mark.parametrize("after", [{"site.controller": False}, {}])
def test_unknown_before_state_still_requires_a_running_controller(steps, monkeypatch, after):
    module, _, _ = steps
    with pytest.raises(Exception, match="fail after the switch: site.controller"):
        _smoke(module, monkeypatch, {}, after, unknown="old reader rejected the description")


def test_unrelated_installed_status_failure_is_not_an_unknown_before_state(steps):
    import boring_deploy
    module, ctx, _ = _converted(steps, capable=True)
    ctx.status_error = "systemctl observation failed"
    with pytest.raises(boring_deploy.Failure, match="systemctl observation failed"):
        module.check(ctx)
    assert not any("before-state is unknown" in line for line in ctx.logged)


@pytest.mark.parametrize("probe", [
    types.SimpleNamespace(returncode=1, stdout=""),
    types.SimpleNamespace(returncode=0, stdout="not JSON"),
    types.SimpleNamespace(returncode=0, stdout='{"unreadable":false}'),
])
def test_failed_or_invalid_reader_probe_preserves_the_status_failure(steps, probe):
    import boring_deploy
    module, ctx, _ = _converted(steps, capable=True)
    ctx.status_error = "original installed status failure"
    actual = ctx.run.side_effect
    def run(*command, **kw):
        if any("Settings.load" in part for part in command):
            return probe
        return actual(*command, **kw)
    ctx.run.side_effect = run
    with pytest.raises(boring_deploy.Failure, match="original installed status failure"):
        module.check(ctx)



def test_preflight_refuses_legacy_without_effects_or_preparation(steps):
    module, ctx, state = steps
    assert module.preflight(ctx) == []
    config = state / "config.toml"
    config.write_text("[server]\nbind_port=8503\n")
    before = config.read_bytes()
    ctx.run.return_value = types.SimpleNamespace(returncode=0, stdout="legacy")
    messages = module.preflight(ctx)
    assert len(messages) == 1 and "adopt-site.py --dry-run" in messages[0]
    assert config.read_bytes() == before
    assert sorted(p.name for p in state.iterdir()) == ["config.toml"]


def test_stopped_adoption_uses_candidate_reader_and_backup_without_store_migration(steps):
    import sqlite3
    module, ctx, state = _converted(steps)
    with sqlite3.connect(state / "oauth.sqlite3") as db:
        db.execute("PRAGMA user_version=5")
        db.execute("CREATE TABLE keep_me (value TEXT)")
        db.execute("INSERT INTO keep_me VALUES ('synthetic credential row')")
    assert module.preflight(ctx) == []
    module.check(ctx)
    assert module.healthy(ctx) is None
    assert any("installed before-state is unknown: ValueError" in line for line in ctx.logged)
    assert any("status" in call for call in ctx.calls)
    assert module.backup(ctx, state) == state / "oauth.sqlite3"
    assert ctx.calls[-1][0] == str(ctx.new / "bin/boring-crocodile-admin")
    assert module.data_generation(ctx) == 5
    with sqlite3.connect(state / "oauth.sqlite3") as db:
        assert db.execute("SELECT value FROM keep_me").fetchone()[0] == "synthetic credential row"


def test_normal_typed_update_keeps_installed_status_and_backup(steps):
    module, ctx, state = _converted(steps, desired="running", capable=True)
    module.check(ctx)
    assert module.healthy(ctx) is True
    assert any("status" in call and call[0].startswith(str(ctx.home / "current")) for call in ctx.calls)
    module.backup(ctx, state)
    assert ctx.calls[-1][0] == str(ctx.home / "current/bin/boring-crocodile-admin")
    assert not any("one-time stopped adoption" in line for line in ctx.logged)


@pytest.mark.parametrize("state", ["active", "activating", "deactivating", "unknown"])
def test_stopped_intent_with_live_units_is_not_an_adoption_exception(steps, state):
    module, ctx, _ = _converted(steps)
    ctx.active = f"boring-crocodile-task-pond@7.service loaded {state} running Synthetic\n"
    with pytest.raises(Exception, match="all Crocodile units stopped"):
        module.preflight(ctx)
    with pytest.raises(Exception, match="all Crocodile units stopped"):
        module.check(ctx)


def test_adoption_rechecks_stop_and_requires_stopped_intent(steps):
    module, ctx, state = _converted(steps)
    assert module.preflight(ctx) == []
    ctx.active = "boring-crocodile-mcp.socket loaded active listening Synthetic\n"
    with pytest.raises(Exception, match="all Crocodile units stopped"):
        module.check(ctx)
    ctx.active = ""
    config = state / "config.toml"
    config.write_text(config.read_text().replace('state="stopped"', 'state="running"'))
    with pytest.raises(Exception, match="only while stopped"):
        module.check(ctx)


def test_candidate_validation_and_unknown_capability_never_fall_back(steps):
    module, ctx, _ = _converted(steps)
    actual = ctx.run.side_effect

    def invalid(*command, **kw):
        if command[-1] == "list":
            raise ValueError("candidate configuration invalid")
        return actual(*command, **kw)
    ctx.run.side_effect = invalid
    with pytest.raises(ValueError, match="candidate configuration invalid"):
        module.check(ctx)
    assert not ctx.calls
    ctx.run.side_effect = lambda *command, **kw: types.SimpleNamespace(stdout="unknown") if "-c" in command else actual(*command, **kw)
    with pytest.raises(Exception, match="cannot determine Crocodile description support"):
        module.check(ctx)
