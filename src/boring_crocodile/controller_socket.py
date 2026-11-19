"""Take the controller listener from systemd; never bind a replacement."""
from __future__ import annotations

import ipaddress
import os
import socket


def take_listener(host: str, port: int, *, descriptor: int = 3) -> socket.socket:
    if (os.environ.get("LISTEN_PID") != str(os.getpid())
            or os.environ.get("LISTEN_FDS") != "1"
            or os.environ.get("LISTEN_FDNAMES") != "controller"):
        raise RuntimeError("The controller requires boring-crocodile-mcp.socket activation")
    duplicate = os.dup(descriptor)
    try:
        listener = socket.socket(fileno=duplicate)
    except BaseException:
        os.close(duplicate)
        raise
    try:
        if (listener.family not in (socket.AF_INET, socket.AF_INET6)
                or listener.type != socket.SOCK_STREAM
                or not listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)):
            raise RuntimeError("The controller activation descriptor is not a TCP listener")
        address = listener.getsockname()
        if address[1] != port or ipaddress.ip_address(address[0]) != ipaddress.ip_address(host):
            raise RuntimeError("The controller socket address differs from server.bind_host/bind_port")
        # Do not leak either copy into processes the controller starts.
        os.set_inheritable(descriptor, False)
        listener.set_inheritable(False)
    except BaseException:
        listener.close()
        raise
    for key in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
        os.environ.pop(key, None)
    return listener
