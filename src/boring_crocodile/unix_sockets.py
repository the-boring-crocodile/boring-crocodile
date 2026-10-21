"""The one way a listener claims and releases its Unix socket path.

Check before unlink: before binding, only a stale socket of this account (a
socket file nobody listens on) is removed; anything else at the path refuses,
for a person to inspect. On close, the path is removed only while it is still
the socket this listener bound, never one a successor bound meanwhile.
"""
from __future__ import annotations

from contextlib import suppress
import os
from pathlib import Path
import socket
import stat


def clear_stale_socket(path: Path) -> None:
    """Make way for a new listener at path, or refuse (FileExistsError)."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
        raise FileExistsError(f"{path} exists and is not a socket of this account; inspect before removing")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.5)
        probe.connect(str(path))
    except ConnectionRefusedError:
        current = path.lstat()
        if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
            path.unlink()  # stale: its listener is gone
        return
    except FileNotFoundError:
        return
    except TimeoutError:
        pass  # a listener that does not accept in time is still a listener
    finally:
        probe.close()
    raise FileExistsError(f"a listener is active at {path}")


def bound_identity(path: Path) -> tuple[int, int]:
    """The socket file a listener just bound, to release it later."""
    info = path.lstat()
    return info.st_dev, info.st_ino


def release_socket(path: Path, identity: tuple[int, int]) -> None:
    """Remove path only while it is still the socket this listener bound."""
    with suppress(FileNotFoundError):
        current = path.lstat()
        if (current.st_dev, current.st_ino) == identity:
            path.unlink()
