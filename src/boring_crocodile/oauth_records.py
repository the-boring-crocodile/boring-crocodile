"""The Crocodile's OAuth store: the Common issuer's tables plus its services.

The issuer's part (grants in `records`, clients in `clients`) is defined by
boring_service_kit.issuer.store; this module adds the `services` table and the
schema line of the Crocodile's own store file. No service initialization.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3

from boring_registry import PING_TIMEOUT_SECONDS
from boring_service_kit.issuer.store import (Transaction as Transaction, create_tables,
                                             secret_key as secret_key, validate_tables)

VERSION = 5
# Records whose key is a bearer value. The key is stored as its SHA-256; the
# payload field that repeats the value (if any) is stored blank. Nothing ever
# needs a bearer value back: clients hold them, the store only verifies them.
# "service" is an access token the control plane mints for a worker's call to
# a service: subject the Pond, resource the service URL. It is its own kind so
# it can never be presented as an agent's access token, nor the reverse.
SECRET_KINDS = {"pending": None, "code": "code", "access": "token", "refresh": "token",
                "service": "token"}
GRANTS = ("code", "access", "refresh")
# Services the admin added: their credentials (the secret as SHA-256), their
# MCP URL, the Ponds they are granted to (a JSON list of Pond names) and how
# long a worker waits for their heartbeat.
SERVICE_COLUMNS = ("name", "client_id", "secret_sha256", "url", "ponds", "ping_timeout")


def initialize(db: sqlite3.Connection) -> None:
    """Create a new store, or check an existing one: schema 5 only."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
            raise ValueError("refusing unversioned nonempty OAuth database")
        create_tables(db)
        _create_services(db)
        db.execute(f"PRAGMA user_version={VERSION}")
    elif version != VERSION:
        raise ValueError(f"OAuth store is schema {version}; this release reads schema {VERSION}")
    validate(db)


def _create_services(db: sqlite3.Connection) -> None:
    db.execute(f"""CREATE TABLE services (
        name TEXT PRIMARY KEY, client_id TEXT NOT NULL UNIQUE,
        secret_sha256 TEXT NOT NULL, url TEXT NOT NULL, ponds TEXT NOT NULL,
        ping_timeout REAL NOT NULL DEFAULT {PING_TIMEOUT_SECONDS})""")


def validate(db: sqlite3.Connection) -> None:
    if db.execute("PRAGMA user_version").fetchone()[0] != VERSION:
        raise ValueError("unsupported OAuth schema")
    validate_tables(db)
    if tuple(row[1] for row in db.execute("PRAGMA table_info(services)")) != SERVICE_COLUMNS:
        raise ValueError("unexpected OAuth service schema")


def open_readonly(path):
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)


def rows(db):
    validate(db)
    return list(db.execute("SELECT kind,key,payload,expires,pair FROM records"))


def decode(row):
    try:
        value = json.loads(row[2])
        if not isinstance(value, dict) or row[0] not in (*GRANTS, "service", "pending"):
            raise ValueError()
        if row[0] in (*GRANTS, "service") and (
            not isinstance(value.get("subject"), str)
            or not isinstance(value.get("client_id"), str)
            or not isinstance(value.get("scopes"), list)
            or any(not isinstance(scope, str) for scope in value["scopes"])
        ):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise ValueError("malformed OAuth record") from None


def selected(records, profile_id, client_id=None, *, caller=None):
    answer = []
    for row in records:
        value = decode(row)
        if ((row[0] in GRANTS and value["subject"] == profile_id
                and (client_id is None or value["client_id"] == client_id))
                or (caller is not None and row[0] == "service" and value["subject"] == caller)):
            answer.append(row)
    return answer


def delete(db, records):
    db.executemany("DELETE FROM records WHERE kind=? AND key=?",
                   [(row[0], row[1]) for row in records])


def approval_digest(profile_id, client_id, records):
    # Exclude raw bearer values, record keys, and pending/client payloads.
    # A pair is a non-bearer generation identifier created on every issuance.
    # Code expiry/challenge/binding distinguish approved code generations.
    metadata = []
    for row in records:
        value = decode(row)
        metadata.append(json.dumps({
            "kind": row[0], "expires": row[3], "pair": row[4],
            "subject": value["subject"], "client_id": value["client_id"],
            "scopes": sorted(value["scopes"]),
            "resource": value.get("resource"),
            "code_challenge": value.get("code_challenge"),
            "redirect_uri": value.get("redirect_uri"),
            "redirect_uri_provided_explicitly": value.get("redirect_uri_provided_explicitly"),
        }, sort_keys=True, separators=(",", ":")))
    data = json.dumps([profile_id, client_id, sorted(metadata)], separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


from boring_worker_kit.service_contract import (SERVICE_NAME as SERVICE_NAME, service_url as service_url,
    service_ping_timeout as service_ping_timeout, MAX_PING_TIMEOUT_SECONDS as MAX_PING_TIMEOUT_SECONDS)


def add_service(db, name: str, url: str, ponds: list[str],
                ping_timeout: float = PING_TIMEOUT_SECONDS) -> dict[str, object]:
    """Create a service; the secret is returned here once and stored only as its hash."""
    if not SERVICE_NAME.fullmatch(name):
        raise ValueError("service name must match [a-z0-9][a-z0-9-]{0,62}")
    if not ponds or len(set(ponds)) != len(ponds):
        raise ValueError("a service needs one or more distinct Ponds")
    if db.execute("SELECT 1 FROM services WHERE name=?", (name,)).fetchone():
        raise ValueError(f"service {name!r} exists; remove it first to change it")
    client_id, secret = "svc_" + secrets.token_urlsafe(16), secrets.token_urlsafe(32)
    timeout = service_ping_timeout(ping_timeout)
    db.execute("INSERT INTO services VALUES (?, ?, ?, ?, ?, ?)",
               (name, client_id, secret_key(secret), service_url(url), json.dumps(ponds), timeout))
    return {"name": name, "client_id": client_id, "client_secret": secret,
            "url": url, "ponds": ponds, "ping_timeout": timeout}


def remove_service(db, name: str, expected: dict[str, object] | None = None) -> bool:
    """Remove a service and every token minted for it."""
    found = service(db, name=name)
    if found is None:
        return False
    if expected is not None:
        public = {key: value for key, value in found.items() if key != "secret_sha256"}
        if (not isinstance(expected, dict) or
                json.dumps(expected, sort_keys=True, separators=(",", ":")) !=
                json.dumps(public, sort_keys=True, separators=(",", ":"))):
            raise ValueError("service definition changed; removal refused")
    db.execute("DELETE FROM services WHERE name=?", (name,))
    db.executemany("DELETE FROM records WHERE kind='service' AND key=?", [
        (row[0],) for row in db.execute("SELECT key, payload FROM records WHERE kind='service'")
        if json.loads(row[1]).get("client_id") == found["client_id"]])
    return True


def services(db) -> list[dict[str, object]]:
    return [{"name": row[0], "client_id": row[1], "url": row[3], "ponds": json.loads(row[4]),
             "ping_timeout": row[5]}
            for row in db.execute("SELECT * FROM services ORDER BY name")]


def caller_services(db, caller):
    """Validate service grants before selective caller removal."""
    affected = []
    for entry in services(db):
        callers = entry["ponds"]
        if not isinstance(callers, list) or any(not isinstance(x, str) or not x for x in callers):
            raise ValueError("malformed service caller grants")
        if caller in callers:
            affected.append(entry)
    return affected


def revoke_caller(db, caller):
    # Keep the service identity and other callers. An empty list grants nobody;
    # deleting the service would also destroy credentials held by its operator.
    for entry in caller_services(db, caller):
        remaining = [name for name in entry["ponds"] if name != caller]
        db.execute("UPDATE services SET ponds=? WHERE name=?",
                   (json.dumps(remaining), entry["name"]))


def service(db, *, name: str | None = None, client_id: str | None = None) -> dict[str, object] | None:
    """One service by name or by client id, with its secret hash."""
    column, value = ("name", name) if name is not None else ("client_id", client_id)
    row = db.execute(f"SELECT * FROM services WHERE {column}=?", (value,)).fetchone()
    if row is None:
        return None
    return {"name": row[0], "client_id": row[1], "secret_sha256": row[2], "url": row[3],
            "ponds": json.loads(row[4]), "ping_timeout": row[5]}


def service_grants(db, name: str, callers: list[str], digest: str | None = None) -> dict[str, object]:
    """Review/CAS-update caller membership without rotating service credentials."""
    if not callers or len(set(callers)) != len(callers) or any(not isinstance(x, str) or not x for x in callers):
        raise ValueError("service grants need distinct nonempty caller names")
    found = service(db, name=name)
    if found is None:
        raise ValueError("no such service")
    public = {key: found[key] for key in ("name", "client_id", "url", "ping_timeout")}
    plan = {**public, "before": found["ponds"], "after": sorted(callers)}
    current = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if digest is not None:
        if not secrets.compare_digest(digest, current):
            raise ValueError("service grant plan is stale; review a fresh plan")
        db.execute("UPDATE services SET ponds=? WHERE name=?", (json.dumps(sorted(callers)), name))
    return {**plan, "plan_digest": current, "applied": digest is not None}


def service_endpoint(db, name: str, url: str, digest: str | None = None) -> dict[str, object]:
    """Review/CAS-update the audience URL without changing credentials or grants."""
    target = service_url(url)
    found = service(db, name=name)
    if found is None:
        raise ValueError("no such service")
    plan = {"name": found["name"], "client_id": found["client_id"],
            "before": found["url"], "after": target,
            "callers": sorted(found["ponds"]), "ping_timeout": found["ping_timeout"]}
    # Bind the approval to credential identity without exposing the secret hash.
    material = {**plan, "credential_identity": found["secret_sha256"]}
    current = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if digest is not None:
        if not secrets.compare_digest(digest, current):
            raise ValueError("service endpoint plan is stale; review a fresh plan")
        db.execute("UPDATE services SET url=? WHERE name=?", (target, name))
    return {**plan, "plan_digest": current, "applied": digest is not None,
            "effect": "preserve credentials and callers; prior-audience tokens become inactive at introspection"}
