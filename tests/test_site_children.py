"""Root provisioning uses Common's child boundary before changing identity."""
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from boring_crocodile import site_host


def test_seed_python_is_isolated_after_the_user_switch():
    script = "import json; print('seed')"
    host = site_host.Host()
    with patch.object(site_host.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
        host.seed("garden-user", Path("/home/garden-user"), [("WELCOME.md", b"hello")], script)
    assert run.call_args.args[0] == [
        "/usr/sbin/runuser", "-u", "garden-user", "--",
        "/usr/bin/python3", "-I", "-B", "-c", script]
    assert run.call_args.kwargs["cwd"] == "/"
    home, files = json.loads(run.call_args.kwargs["input"])
    assert home == "/home/garden-user"
    assert files == [["WELCOME.md", base64.b64encode(b"hello").decode()]]
