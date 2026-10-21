"""The smallest config Settings accepts: one Pond named pond, rooted at root."""
from pathlib import Path

PROFILE_ID = "11111111-1111-4111-8111-111111111111"


def dynamic():
    """A client policy with open registration: the test clients register themselves."""
    from boring_service_kit.issuer import ClientPolicy
    return ClientPolicy(dynamic_registration=True)


def one_pond_toml(root: Path, environment: str = "") -> str:
    """`environment` lines are appended to [environments.pond]."""
    return f'''
[server]
public_base_url = "https://example.test"
bind_host = "127.0.0.1"
bind_port = 8503
allowed_hosts = ["example.test"]
[auth]
oauth_database_file = "{root}/oauth.sqlite3"
[ponds.pond]
profile_id = "{PROFILE_ID}"
display_name = "The Pond"
environment = "pond"
approval_secret_file = "{root}/secret"
context_root = "{root}"
files = ["contract.md"]
[environments.pond]
socket_path = "{root}/pond.sock"
default_cwd = "{root}"
allowed_roots = ["{root}"]
control_plane_uid = 1000
{environment}'''


def one_pond(root: Path, secret: Path | None = None) -> dict:
    """The Pond catalog of one_pond_toml, for code that takes Settings.ponds."""
    from boring_crocodile.settings import PondSettings
    return {"pond": PondSettings(PROFILE_ID, "The Pond", "pond", secret or root / "secret",
                                 root, ("contract.md",))}
