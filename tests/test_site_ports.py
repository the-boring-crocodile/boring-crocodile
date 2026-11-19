import pytest

from boring_crocodile.site_ports import local_tcp_port


@pytest.mark.parametrize(("url", "port"), [
    ("http://localhost./mcp", 80),
    ("https://server.LOCALHOST/mcp", 443),
    ("http://127.255.2.3:19000/mcp", 19000),
    ("http://0.0.0.0:18530/mcp", 18530),
    ("http://[::1]:18530/mcp", 18530),
    ("http://[::]:18530/mcp", 18530),
    ("http://[::ffff:127.0.0.2]:18530/mcp", 18530),
    ("http://[::ffff:0.0.0.0]:18530/mcp", 18530),
    ("http://[::ffff:192.0.2.1]:18530/mcp", None),
    ("https://service.example.test:18530/mcp", None),
])
def test_registry_loopback_addresses(url, port):
    assert local_tcp_port(url) == port
