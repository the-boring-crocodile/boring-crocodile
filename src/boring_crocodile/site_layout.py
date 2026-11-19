"""Where a site lives on the host, how per-Pond names are formed, the one
lock every change to the site takes and how its files are written.

The one owner of these defaults: setup, rendering, activation, uninstall and
admin import them instead of spelling them out. The initial Pond keeps
the historical unprefixed artifact names.
"""
from __future__ import annotations

from collections.abc import Iterator
import contextlib
import fcntl
import os
from pathlib import Path
import re
import stat

STATE_DIR = Path("/etc/boring-crocodile")
CONFIG_FILE = STATE_DIR / "config.toml"
CREDENTIALS_DIR = STATE_DIR / "credentials"
OAUTH_DATABASE = Path("/var/lib/boring-crocodile/oauth.sqlite3")
CONTROL_CREDENTIALS = Path("/run/credentials/boring-crocodile-mcp.service")
INITIAL_POND = "pond"
REGISTRATION_GROUP = "pond-pirates"
REGISTRATION_ROOT = Path("/run/boring-crocodile/guests")


def registration_path(environment: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", environment):
        raise ValueError("invalid registration environment")
    return REGISTRATION_ROOT / environment / "register.sock"


def approval_credential(pond: str) -> str:
    """Credential (file) name of a Pond's approval secret."""
    return "approval-secret" if pond == INITIAL_POND else f"{pond}-approval-secret"


POND_NAME = r"[a-z](?:[a-z0-9-]{0,22}[a-z0-9])?"
"""Pond names: lowercase letters, digits and inner hyphens (the worker kit's
environment names allow no underscore), at most 24 characters."""


def budget_slice(pond: str) -> str:
    """The Pond's command budget slice. In a slice name a hyphen means one level
    deeper (system-a-b.slice lives inside system-a.slice), so the name's
    hyphens become underscores: rose-garden -> system-crocodilerose_garden.slice,
    still a direct child of system.slice. Unique, as names contain no underscore."""
    return "system-crocodile" + pond.replace("-", "_") + ".slice"


def task_unit_prefix(pond: str) -> str:
    """The Pond's task unit prefix: worker-kit's `<product>-task-<execution name>`."""
    return "boring-crocodile-task-" + pond


def tmpfiles_name(pond: str) -> str:
    """tmpfiles.d file name of a Pond's runtime directories."""
    return "boring-crocodile.conf" if pond == INITIAL_POND else f"boring-crocodile-{pond}.conf"


def write_file(path: Path, content: str | bytes, *, mode: int | None = None, create: bool = False) -> None:
    """Write a site file whole or not at all, with Common's atomic_write: a
    temporary file beside it, fsynced, renamed into place, the folder fsynced.
    An interruption leaves the old file (or none) and at most a hidden
    temporary `.NAME.<hex>`, never a part of the new one.

    create=True writes only a new file: one that exists, or appears meanwhile,
    is refused (boring_file_core.StaleEdit, a ValueError) and kept. Otherwise
    the file is created or replaced (a symlink in its place is replaced, not
    followed). `mode` is the new file's mode; None keeps the existing file's
    mode and group, so config.toml keeps one mode whichever command writes it.
    Keeping needs an existing regular file of the writer (root on a host).
    """
    from boring_file_core import atomic_write
    payload = content.encode() if isinstance(content, str) else content
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        group = None
        if mode is None:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if create or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise ValueError("Cannot keep the mode and owner of " + str(path))
            mode, group = stat.S_IMODE(info.st_mode), info.st_gid
        atomic_write(parent, path.name, payload, mode, create=create, prefix="." + path.name + ".", group=group)
    finally:
        os.close(parent)


SITE_LOCK = "site.lock"


def description_modes(site, config: Path, *, owner: int = 0, credential_path=Path) -> list[dict]:
    """The standard description's only owner/group/mode table."""
    state = config.parent
    rows = []
    def add(path, directory, gid, mode):
        rows.append(dict(path=str(path), directory=directory, uid=owner, gid=gid, mode=mode))
    add(state, True, 0, 0o711)
    add(config, False, 0, 0o644)
    add(state / "ponds", True, site.controller.gid, 0o751)
    for name, pond in site.ponds.items():
        folder = state / "ponds" / name
        retired = site.state == "removed"
        add(folder, True, 0 if retired else pond.identity.gid, 0o700 if retired else 0o750)
        add(folder / "config.toml", False, 0 if retired else pond.identity.gid, 0o600 if retired else 0o640)
    credentials = state / "credentials"
    if credentials.exists():
        add(credentials, True, 0, 0o700)
    for pond in site.ponds.values():
        secret = credential_path(pond.credential_file)
        if os.path.lexists(secret):
            add(secret, False, 0, 0o600)
            rows[-1]["credential"] = True
    add(state / SITE_LOCK, False, 0, 0o600)
    return rows



class SiteLocked(ValueError):
    """Another change to the site holds the site lock."""


@contextlib.contextmanager
def site_lock(state: Path = STATE_DIR, *, owner: int = 0) -> Iterator[None]:
    """One change to the site at a time: render, provisioning, retirement,
    service-policy changes hold this lock (STATE/site.lock).

    It does not wait: a second change refuses (SiteLocked) and changes nothing.
    flock belongs to the open file, so a second site_lock in the same process
    refuses too; each command takes it exactly once, at its top. The initial
    setup creates STATE itself (an exclusive mkdir) before it can lock it.
    The lock file must be a private regular file of `owner` (root on a host).
    """
    fd = os.open(state / SITE_LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "r+") as lock:
        info = os.fstat(lock.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise ValueError("Unsafe site lock: " + str(state / SITE_LOCK))
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SiteLocked("Another change to the site is running (" + str(state / SITE_LOCK)
                             + " is held); retry when it has finished") from None
        yield
