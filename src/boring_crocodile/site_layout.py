"""Where a site lives on the host, how per-Pond names are formed, the one
lock every change to the site takes and how its files are written.

The one owner of these defaults: setup, rendering, activation, uninstall and
admin import them instead of spelling them out. The first Pond is the one
created by the initial setup; it owns the site, and its artifacts keep the
unprefixed names.
"""
from __future__ import annotations

from collections.abc import Iterator
import contextlib
import fcntl
import os
from pathlib import Path
import stat

from boring_file_core import atomic_write

STATE_DIR = Path("/etc/boring-crocodile")
CONFIG_FILE = STATE_DIR / "config.toml"
CREDENTIALS_DIR = STATE_DIR / "credentials"
OAUTH_DATABASE = Path("/var/lib/boring-crocodile/oauth.sqlite3")
SITE_RECORDS = Path("/var/lib/boring-crocodile-site")
"""Root's own records of changes to a running site: site-log.jsonl and
site-backups/. Not /var/lib/boring-crocodile, which belongs to the controller:
root never writes where an unprivileged account could plant a link."""
CONTROL_CREDENTIALS = Path("/run/credentials/boring-crocodile-mcp.service")
INITIAL_POND = "pond"


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


class SiteLocked(ValueError):
    """Another change to the site holds the site lock."""


def records_dir(path: Path = SITE_RECORDS, *, owner: int = 0) -> Path:
    """`path`, created if missing, after checking it is a real folder of
    `owner` (root on a host) that nobody else can write. Refuses otherwise."""
    with contextlib.suppress(FileExistsError):
        os.mkdir(path, 0o700)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
        raise ValueError(f"Unsafe records folder (must be a folder of uid {owner}, "
                         f"writable by it only): {path}")
    return path


def append_record(line: str, path: Path = SITE_RECORDS, *, owner: int = 0) -> None:
    """One line appended to site-log.jsonl in the records folder, never through a link."""
    folder = records_dir(path, owner=owner)
    fd = os.open(folder / "site-log.jsonl",
                 os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "a") as log:
        info = os.fstat(log.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_nlink != 1:
            raise ValueError("Unsafe site log: " + str(folder / "site-log.jsonl"))
        log.write(line + "\n")


@contextlib.contextmanager
def site_lock(state: Path = STATE_DIR, *, owner: int = 0) -> Iterator[None]:
    """One change to the site at a time: setup and --add-pond apply, site
    activation, --site-extend, uninstall, pirate-add/remove and task-policy
    apply all hold this lock (STATE/site.lock) while they change the site.

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
