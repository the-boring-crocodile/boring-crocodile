"""Root-reviewed TCP ports Pond and Pirate tasks must never bind."""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

# Standard service listeners, then Ferry (18520–18529), then tunnels (18530–18539).
DEFAULT_PROTECTED_PORTS = tuple(range(18510, 18540))
PIRATE_BIND_DROPIN = "20-boring-crocodile-bind.conf"


def local_tcp_port(url: str) -> int | None:
    """Return the TCP port of registry URLs that directly name this host."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    if host != "localhost" and not host.endswith(".localhost"):
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return None
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not (address.is_loopback or address.is_unspecified):
            return None
    return parsed.port or {"http": 80, "https": 443}.get(parsed.scheme)


def service_port_allowed(url: str, ports: tuple[int, ...], controller_port: int) -> bool:
    port = local_tcp_port(url)
    return port is None or (port in ports and port != controller_port)


def require_protected_service_port(url: str, ports: tuple[int, ...], name: str,
                                   *, controller_port: int) -> None:
    port = local_tcp_port(url)
    if port is not None and port == controller_port:
        raise ValueError(f"{name}: loopback port {port} belongs to the Crocodile listener")
    if port is not None and port not in ports:
        raise ValueError(f"{name}: unprotected loopback service port {port}; "
                         "add it to server.protected_ports and apply a site render first")



def protected_ports(value: object) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or any(
            type(port) is not int or not 1 <= port <= 65535 for port in value):
        raise ValueError("protected_ports must be a list of TCP ports (1..65535)")
    result = tuple(sorted({*DEFAULT_PROTECTED_PORTS, *value}))
    # systemd permits 128 rules; reserve one for a custom controller port.
    if len(result) > 127:
        raise ValueError("protected_ports supports at most 127 distinct TCP ports")
    return result


def socket_bind_denials(controller_port: int, ports: tuple[int, ...]) -> str:
    # Omitting the address family applies each rule to both IPv4 and IPv6.
    return "\n".join(f"SocketBindDeny=tcp:{port}"
                     for port in sorted({controller_port, *ports}))
