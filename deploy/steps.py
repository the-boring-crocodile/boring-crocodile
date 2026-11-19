"""Deploy steps for the Crocodile (runtime venv; see boring_deploy).

The site's units (boring-crocodile-mcp.service, the workers, the task units)
run HOME/current/bin/...; the configuration is /etc/boring-crocodile/config.toml
(RELEASE.json "config"). The OAuth store's schema is the data generation.
An update restarts the site's services that run; a stopped site stays stopped
(the engine's switch decides by `systemctl is-active`, units() only lists).
"""
import json
import os
import sqlite3
import sys
from contextlib import closing, contextmanager, ExitStack
import time
import tomllib
from pathlib import Path

import boring_deploy
from site_layout import SITE_LOCK, site_lock

_before = {}
_before_unknown = None


def _config(ctx):
    return Path(ctx.release.details.get("config", "/etc/boring-crocodile/config.toml"))


def _admin(ctx, release_dir, *args):
    admin = str(Path(release_dir) / "bin" / "boring-crocodile-admin")
    return ctx.run(admin, "--config", str(_config(ctx)), *args)


def _checks(ctx, release_dir):
    report = json.loads(_admin(ctx, release_dir, "status").stdout)
    passed = {f"site.{k}": v.get("active") in ("active",) for k, v in report["site"].items()}
    for pond in report["ponds"]:
        passed.update({f"{pond['pond']}.{k}": bool(v) for k, v in pond["checks"].items()})
    return passed


def _configured(ctx):
    """A fresh host has no configuration until boring-crocodile-setup has run:
    then the deploy only installs the release (no units, no checks)."""
    return _config(ctx).is_file()


def completion(ctx):
    """The runtime can be installed before the operator configures a site."""
    if not _configured(ctx):
        return "runtime installed; site not configured yet"
    return None


@contextmanager
def lock(ctx):
    """Take the site's canonical lock; a fresh host creates nothing."""
    state = _config(ctx).parent
    if not os.path.lexists(state / SITE_LOCK):
        yield
        return
    with ExitStack() as stack:
        try:
            stack.enter_context(site_lock(state, owner=os.geteuid()))
        except ValueError as error:
            raise boring_deploy.Failure(str(error)) from None
        yield


def units(ctx):
    """The site's installed services, running or not: the engine restarts those
    that run and leaves a stopped one stopped. Unit files, not loaded units: a
    site stopped with `disable --now` has its units unloaded, and listing only
    loaded ones would make a stopped site look like no site ("is live")."""
    if not _configured(ctx):
        return []
    listed = ctx.run("systemctl", "list-unit-files", "--plain", "--no-legend", "--type=service",
                     "boring-crocodile-mcp.service", "boring-crocodile-worker-*.service").stdout.split("\n")
    return sorted(line.split()[0] for line in listed if line.strip())


def data_generation(ctx):
    if not _configured(ctx):
        return None
    store = Path(tomllib.loads(_config(ctx).read_text())["auth"]["oauth_database_file"])
    if not store.is_file():
        return None
    with closing(sqlite3.connect(store.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute("PRAGMA user_version").fetchone()[0]



def _stopped(ctx):
    """Read-only proof covering the listener, workers, sockets and task instances."""
    result = ctx.run("systemctl", "list-units", "--all", "--plain", "--no-legend",
                     "--no-pager", "boring-crocodile-*")
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[2] not in {"inactive", "failed"}:
            raise boring_deploy.Failure(
                "offline adoption/stopped state requires all Crocodile units stopped",
                "finish jobs; stop the listener, target, workers and task units; verify again")


_SITE_STATE = """import os, sys
from pathlib import Path

release = Path(sys.argv[1])
sys.path[:0] = [str(wheel) for wheel in sorted((release / "wheels").glob("*.whl"))]
from boring_crocodile.site_model import LegacyDescription, SiteDescription
try:
    site = SiteDescription.read(Path(sys.argv[2]), owner=os.geteuid())
except LegacyDescription:
    print("legacy")
except (OSError, ValueError, KeyError) as error:
    sys.exit(str(error))
else:
    print(site.state)
"""


def preflight(ctx):
    """Read the complete description with the verified candidate before prepare."""
    if _configured(ctx):
        result = ctx.run(sys.executable, "-I", "-B", "-c", _SITE_STATE,
                         str(ctx.release.directory), str(_config(ctx)), check=False)
        if result.returncode:
            return [(result.stderr or result.stdout).strip() or f"cannot validate {_config(ctx)}"]
        state = result.stdout.strip()
        if state == "legacy":
            return [f"{_config(ctx)}: legacy site configuration: use this release's adopt-site.py --dry-run, "
                    "take the reviewed snapshot and convert while stopped before deploying"]
        if state in {"stopped", "removed"}:
            _stopped(ctx)
        elif state != "running":
            return [f"{_config(ctx)}: candidate description check returned an unexpected result"]
    # Receipt storage belongs to each worker; root never opens Pond-owned databases.
    return []


def _description_reader(ctx, release_dir):
    """Probe this release without asking it to parse the live description."""
    python = str(Path(release_dir) / "bin" / "python3")
    result = ctx.run(python, "-I", "-B", "-c",
                     "import importlib.util; print(int(importlib.util.find_spec('boring_crocodile.site_model') is not None))")
    answer = result.stdout.strip()
    if answer not in {"0", "1"}:
        raise boring_deploy.Failure(f"cannot determine Crocodile description support in {release_dir}")
    return answer == "1"


def way_back(ctx, previous):
    """Description adoption needs the matching legacy files as well as code."""
    if _description_reader(ctx, previous):
        return None
    version = (Path(previous) / "VERSION").read_text().strip()
    return (f"back to {version}: follow OPERATIONS.md, section 'Way back across adoption' "
            "(restore the matching legacy files and runtime)")


def _legacy_reader(ctx):
    """Keep the stopped-adoption proof separate from the release capability."""
    if _description_reader(ctx, ctx.home / "current"):
        return False
    raw = tomllib.loads(_config(ctx).read_text())
    if raw.get("site", {}).get("schema") != 1 or raw["site"].get("state") != "stopped":
        raise boring_deploy.Failure("the legacy runtime can cross to a typed description only while stopped",
                                    "use the one-time importer and the reviewed adoption runbook")
    _stopped(ctx)
    return True

def backup(ctx, directory):
    """Consistent generation-5 backup; the old reader cannot read converted config."""
    target = Path(directory) / "oauth.sqlite3"
    installed = ctx.home / "current"
    source = ctx.new if not (installed / "bin/boring-crocodile-admin").exists() or _legacy_reader(ctx) else installed
    _admin(ctx, source, "oauth", "backup", "--output", str(target))
    return target


def _unreadable_description(ctx, installed):
    """Confirm a failed status came from the installed reader, not host observation."""
    probe = """import json, sys
from pathlib import Path
from boring_crocodile.settings import Settings
try:
    Settings.load(Path(sys.argv[1]))
except (ValueError, KeyError) as error:
    print(json.dumps({"unreadable": f"{type(error).__name__}: {error}"}))
else:
    print("{}")
"""
    try:
        result = ctx.run(str(installed / "bin/python3"), "-I", "-B", "-c", probe,
                         str(_config(ctx)), check=False)
        if result.returncode != 0:
            return None
        report = json.loads(result.stdout)
    except (OSError, ValueError):
        return None
    reason = report.get("unreadable") if isinstance(report, dict) else None
    return reason if isinstance(reason, str) and reason else None


def check(ctx):
    """The new code reads the live configuration; the installed release's own
    status is informational when its reader cannot parse the live description.
    Unknown before-state requires every new-release smoke check to pass."""
    global _before_unknown
    _before.clear()
    _before_unknown = None
    if not _configured(ctx):
        ctx.log(f"no {_config(ctx)} yet: installing only; then run {ctx.home}/current/bin/boring-crocodile-setup --wizard")
        return
    ponds = _admin(ctx, ctx.new, "list").stdout
    ctx.log(f"configuration parses with the new release: {len(json.loads(ponds))} Ponds")
    _render_due(ctx)
    if (ctx.home / "current" / "bin" / "boring-crocodile-admin").exists():
        _legacy_reader(ctx)  # retain the stopped-adoption safety proof
        installed = ctx.home / "current"
        try:
            _before.update(_checks(ctx, installed))
        except boring_deploy.Failure:
            reason = _unreadable_description(ctx, installed)
            if reason is None:
                raise
            _before_unknown = reason
            ctx.log("installed before-state is unknown: " + reason)


def _render_due(ctx):
    """Say when the new release renders site files differently (the deploy writes
    none of them): site-check of the new release on the live files. Informational."""
    admin = str(Path(ctx.new) / "bin" / "boring-crocodile-admin")
    result = ctx.run(admin, "--config", str(_config(ctx)), "site-check", "--json", check=False)
    try:
        report = json.loads(result.stdout)
    except ValueError:
        return
    if report.get("status") == "irregular":
        ctx.log("site-check reports irregular site state with the new release; "
                "the deploy changes no site files: afterwards, "
                "boring-crocodile-setup --site-render shows the plan")


def healthy(ctx):
    """The running release passed all its status checks right before the switch
    (recorded by check); it then becomes previous. None when absent or unreadable."""
    return all(_before.values()) if _before and _before_unknown is None else None


def smoke(ctx):
    """Every status check that passed before the switch passes after it
    (workers need a moment to open their sockets: up to a minute), and the
    controller runs. An unknown before-state requires every new check to pass.
    A deploy over a broken site once said
    "is live" with the controller dead."""
    if not _configured(ctx):
        return
    deadline = time.monotonic() + 60
    while True:
        after = _checks(ctx, ctx.home / "current")
        # A check the new release no longer has is not a failure (it is reported below).
        if _before_unknown is not None:
            lost = sorted(k for k, passed in after.items() if not passed)
        else:
            lost = sorted(k for k, v in _before.items() if v and k in after and not after[k])
        if not after.get("site.controller", _before_unknown is None) and "site.controller" not in lost:
            lost.append("site.controller")
        if not lost:
            break
        if time.monotonic() > deadline:
            raise boring_deploy.Failure("checks that fail after the switch: " + ", ".join(lost),
                                        "boring-crocodile-admin status; journalctl -u boring-crocodile-mcp -n 50")
        time.sleep(2)
    failing = sorted(k for k, v in after.items() if not v)
    gone = sorted(set(_before) - set(after))
    if gone:
        ctx.log("checks this release no longer has: " + ", ".join(gone))
    ctx.log(f"{sum(after.values())} status checks pass" + (f"; failing before and after: {', '.join(failing)}" if failing else ""))
