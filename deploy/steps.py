"""Deploy steps for the Crocodile (runtime venv; see boring_deploy).

The site's units (boring-crocodile-mcp.service, the workers, the task units)
run HOME/current/bin/...; the configuration is /etc/boring-crocodile/config.toml
(RELEASE.json "config"). The OAuth store's schema is the data generation.
An update restarts the site's services that run; a stopped site stays stopped
(the engine's switch decides by `systemctl is-active`, units() only lists).
"""
import fcntl
import json
import os
import sqlite3
import stat
from contextlib import closing, contextmanager
import time
import tomllib
from pathlib import Path

import boring_deploy

_before = {}


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


@contextmanager
def lock(ctx):
    """The site lock (site.lock beside config.toml, as site_layout.site_lock
    takes it) for the whole deploy, taken right after the deploy's own lock: a
    site change (setup, --site-extend, pirate-add, uninstall, ...) and a deploy
    of the Crocodile refuse each other instead of overlapping. Like the site
    commands it never waits. A host without a site has no lock file: nothing to
    hold, and nothing is created."""
    path = _config(ctx).parent / "site.lock"
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        fd = None
    try:
        if fd is not None:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                    or info.st_mode & 0o077):
                raise boring_deploy.Failure(f"unsafe site lock: {path}")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise boring_deploy.Failure(f"a change to the site is running ({path} is held)",
                                            "retry when it has finished") from None
        yield
    finally:
        if fd is not None:
            os.close(fd)


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
    with closing(sqlite3.connect(f"file:{store}?mode=ro", uri=True)) as db:
        return db.execute("PRAGMA user_version").fetchone()[0]


def backup(ctx, directory):
    """With the installed release, as its own command does it (integrity-checked)."""
    target = Path(directory) / "oauth.sqlite3"
    _admin(ctx, ctx.home / "current", "oauth", "backup", "--output", str(target))
    return target


def check(ctx):
    """The new code reads the live configuration; the installed release's own
    status is recorded, so smoke can tell a new failure from an old one."""
    if not _configured(ctx):
        ctx.log(f"no {_config(ctx)} yet: installing only; then run {ctx.home}/current/bin/boring-crocodile-setup --wizard")
        return
    ponds = _admin(ctx, ctx.new, "list").stdout
    ctx.log(f"configuration parses with the new release: {len(json.loads(ponds))} Ponds")
    if (ctx.home / "current" / "bin" / "boring-crocodile-admin").exists():
        _before.update(_checks(ctx, ctx.home / "current"))


def healthy(ctx):
    """The running release passed all its status checks right before the switch
    (recorded by check); it then becomes previous. None when there was none."""
    return all(_before.values()) if _before else None


def smoke(ctx):
    """Every status check that passed before the switch passes after it
    (workers need a moment to open their sockets: up to a minute), and the
    controller runs, whatever it did before: a deploy over a broken site said
    "is live" with the controller dead."""
    if not _configured(ctx):
        return
    deadline = time.monotonic() + 60
    while True:
        after = _checks(ctx, ctx.home / "current")
        # A check the new release no longer has is not a failure (it is reported below).
        lost = sorted(k for k, v in _before.items() if v and k in after and not after[k])
        if not after.get("site.controller", True) and "site.controller" not in lost:
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
