"""Startup upgrades exact published schema5, including real issuer payloads."""
import asyncio
import base64
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from mcp.server.auth.provider import AccessToken, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from boring_crocodile import oauth_records as records
from boring_crocodile.sqlite_oauth_provider import SQLiteOAuthProvider
from pond_config import PROFILE_ID, dynamic, one_pond

FIXTURE = Path(__file__).parent / "fixtures/oauth-schema5.sql"
EXPIRY = 4102444800


def legacy_store(root):
    root.chmod(0o700)
    (root / "secret").write_text("synthetic-approval")
    path = root / "oauth.sqlite3"
    client = OAuthClientInformationFull(client_id="old-client", scope="mcp",
        redirect_uris=[AnyUrl("https://client.test/callback")])
    access = AccessToken(token="", client_id=client.client_id, scopes=["mcp"],
        expires_at=EXPIRY, resource="https://server.test/mcp/pond", subject=PROFILE_ID)
    refresh = RefreshToken(token="", client_id=client.client_id, scopes=["mcp"],
        expires_at=EXPIRY, resource="https://server.test/mcp/pond", subject=PROFILE_ID)
    service = AccessToken(token="", client_id="svc-old", scopes=[],
        expires_at=EXPIRY, resource="https://knowledge.test/mcp", subject="pond")
    with sqlite3.connect(path) as db:
        db.executescript(FIXTURE.read_text())
        db.execute("INSERT INTO clients VALUES (?, 'dynamic', 'active', ?, 100, 100, 100, NULL)",
                   (client.client_id, client.model_dump_json(exclude_none=True)))
        for kind, raw, value in (("access", "old-access", access),
                                 ("refresh", "old-refresh", refresh),
                                 ("service", "old-service", service)):
            db.execute("INSERT INTO records VALUES (?, ?, ?, ?, ?)",
                       (kind, records.secret_key(raw), value.model_dump_json(), EXPIRY,
                        None if kind == "service" else "old-pair"))
        for name, identity, members, timeout in (
            ("knowledge", "svc-old", ["pond", "pirate/parrot"], 2.5),
            ("canvas", "svc-empty", [], 1.0)):
            db.execute("INSERT INTO services VALUES (?, ?, ?, ?, ?, ?)",
                (name, identity, records.secret_key("synthetic-service-secret"),
                 f"https://{name}.test/mcp", json.dumps(members), timeout))
    path.chmod(0o600)
    return path


def snapshot(path):
    with sqlite3.connect(path) as db:
        return (db.execute("PRAGMA user_version").fetchone()[0],
                list(db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name")),
                {name: list(db.execute(f"SELECT * FROM {name} ORDER BY 1,2"))
                 for name in ("records", "clients", "services")})


def start(root):
    return SQLiteOAuthProvider("https://server.test", root / "oauth.sqlite3", one_pond(root), dynamic())


def test_pre_start_adoption_backup_accepts5_without_upgrading(tmp_path, monkeypatch):
    from boring_crocodile.oauth_admin import OAuthAdmin
    path = legacy_store(tmp_path)
    before = snapshot(path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    output = backup_dir / "backup.sqlite3"
    real_lstat, real_fstat = Path.lstat, os.fstat
    def root_info(info):
        fields = list(info)
        fields[4] = 0
        return os.stat_result(fields)
    def destination_parent(item, *args, **kwargs):
        info = real_lstat(item, *args, **kwargs)
        return root_info(info) if item == output.parent else info
    # Model only root-owned backup destination checks; actual SQLite backup.
    monkeypatch.setattr(Path, "lstat", destination_parent)
    monkeypatch.setattr("boring_crocodile.oauth_admin.os.fstat",
                        lambda fd: root_info(real_fstat(fd)))
    assert OAuthAdmin(path, os.getuid()).backup(output)["integrity"] == "ok"
    assert snapshot(path) == before
    assert snapshot(output) == before


def test_startup_upgrades_published5_preserving_credentials_clients_tokens(tmp_path):
    path = legacy_store(tmp_path)
    before = snapshot(path)
    provider = start(tmp_path)
    after = snapshot(path)
    assert after[0] == 6
    assert after[2]["records"] == before[2]["records"]
    assert after[2]["clients"] == before[2]["clients"]
    for old in before[2]["services"]:
        new = provider.service(old[0])
        assert (new["client_id"], new["secret_sha256"], new["url"], new["ping_timeout"]) == (old[1], old[2], old[3], old[5])
        assert new["roles"] == ["reader", "writer"] and new["default_role"] == "reader"
        assert new["callers"] == {name: "reader" for name in json.loads(old[4])}
    auth = "Basic " + base64.b64encode(b"svc-old:synthetic-service-secret").decode()
    grant = provider.authenticate_service(auth)
    assert grant is not None
    assert provider.introspect(grant, "old-service")["role"] == "reader"
    async def tokens():
        client = await provider.get_client("old-client")
        assert client is not None
        assert (await provider.load_access_token("old-access")).subject == PROFILE_ID
        assert (await provider.load_refresh_token(client, "old-refresh")).subject == PROFILE_ID
    asyncio.run(tokens())
    assert snapshot(path)[2]["records"] == before[2]["records"]
    assert snapshot(path)[2]["clients"] == before[2]["clients"]
    start(tmp_path)  # reopening6 does not repeat the upgrade
    assert snapshot(path)[2] == after[2]


@pytest.mark.parametrize("version", [4, 7])
def test_other_schema_refuses_unchanged(tmp_path, version):
    path = legacy_store(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute(f"PRAGMA user_version={version}")
    before = snapshot(path)
    with pytest.raises(RuntimeError, match=f"schema {version}"):
        start(tmp_path)
    assert snapshot(path) == before


def test_startup_failure_rolls_back_ddl_rows_and_version(tmp_path, monkeypatch):
    path = legacy_store(tmp_path)
    before = snapshot(path)
    def interrupted(db):
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6
        raise KeyboardInterrupt("injected before commit")
    monkeypatch.setattr(records, "validate", interrupted)
    with pytest.raises(KeyboardInterrupt):
        start(tmp_path)
    assert snapshot(path) == before


def test_abrupt_process_exit_before_commit_leaves_schema5(tmp_path):
    path = legacy_store(tmp_path)
    before = snapshot(path)
    program = """
import os, sys
from pathlib import Path
from boring_crocodile import oauth_records as records
from boring_crocodile.sqlite_oauth_provider import SQLiteOAuthProvider
from pond_config import one_pond, dynamic
def die(db):
    assert db.execute("PRAGMA user_version").fetchone()[0] == 6
    os._exit(71)
records.validate = die
root = Path(sys.argv[1])
SQLiteOAuthProvider("https://server.test", root / "oauth.sqlite3", one_pond(root), dynamic())
"""
    child = subprocess.run([sys.executable, "-c", program, str(tmp_path)], capture_output=True)
    assert child.returncode == 71, child.stderr.decode()
    assert snapshot(path) == before  # SQLite recovers the uncommitted journal
    assert start(tmp_path).service("knowledge")["callers"]["pond"] == "reader"
