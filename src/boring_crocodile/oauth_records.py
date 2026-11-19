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

VERSION = 6
UPGRADES_FROM = 5
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
# MCP URL, service-declared roles/default, caller-to-role grants and heartbeat.
SERVICE_COLUMNS = ("name", "client_id", "secret_sha256", "url", "callers", "roles", "default_role", "ping_timeout")


def initialize(db: sqlite3.Connection) -> None:
    """Create/check6, or upgrade5 inside the issuer's startup transaction."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
            raise ValueError("refusing unversioned nonempty OAuth database")
        create_tables(db)
        _create_services(db)
        db.execute(f"PRAGMA user_version={VERSION}")
    elif version == UPGRADES_FROM:
        _upgrade_5_to_6(db)
    elif version != VERSION:
        raise ValueError(f"OAuth store is schema {version}; this release reads schema {VERSION}")
    validate(db)


def _create_services(db: sqlite3.Connection) -> None:
    db.execute(f"""CREATE TABLE services (
        name TEXT PRIMARY KEY, client_id TEXT NOT NULL UNIQUE,
        secret_sha256 TEXT NOT NULL, url TEXT NOT NULL, callers TEXT NOT NULL,
        roles TEXT NOT NULL, default_role TEXT NOT NULL,
        ping_timeout REAL NOT NULL DEFAULT {PING_TIMEOUT_SECONDS})""")


def _upgrade_5_to_6(db: sqlite3.Connection) -> None:
    """Only services change; issuer rows/credentials/tokens remain byte-for-byte."""
    if not db.in_transaction:
        raise ValueError("OAuth upgrade requires the startup transaction")
    _validate_5(db)
    db.execute("ALTER TABLE services RENAME TO services_5")
    _create_services(db)
    for name, client_id, secret, url, encoded, timeout in db.execute("SELECT * FROM services_5"):
        members = json.loads(encoded)
        if (not isinstance(members, list)
                or any(not isinstance(member, str) or not member for member in members)
                or len(set(members)) != len(members)):
            raise ValueError("malformed schema-5 service membership")
        callers = {member: "reader" for member in members}
        db.execute("INSERT INTO services VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (name, client_id, secret, url, json.dumps(callers),
                    json.dumps(["reader", "writer"]), "reader", timeout))
    db.execute("DROP TABLE services_5")
    db.execute(f"PRAGMA user_version={VERSION}")


def _validate_5(db: sqlite3.Connection) -> None:
    validate_tables(db)
    columns = ("name", "client_id", "secret_sha256", "url", "ponds", "ping_timeout")
    if tuple(row[1] for row in db.execute("PRAGMA table_info(services)")) != columns:
        raise ValueError("unexpected OAuth service schema for upgrade from 5")


def validate(db: sqlite3.Connection, *, upgrade_source: bool = False) -> None:
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if upgrade_source and version == UPGRADES_FROM:
        _validate_5(db)  # Read-only backup before startup, not an admin upgrade.
        return
    if version != VERSION:
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


def declared_roles(roles, default_role):
    """The registered service owns its role vocabulary and default."""
    if (not isinstance(roles, list) or not roles
            or any(not isinstance(role, str) or not SERVICE_NAME.fullmatch(role) for role in roles)
            or len(set(roles)) != len(roles) or default_role not in roles):
        raise ValueError("service roles must be distinct names and include default_role")
    return sorted(roles)


def caller_roles(callers, roles):
    if (not isinstance(callers, dict)
            or any(not isinstance(name, str) or not name or role not in roles
                   for name, role in callers.items())):
        raise ValueError("malformed service caller grants: map each NAME to a declared ROLE; replace the old ponds list")
    return dict(sorted(callers.items()))


def service_add_plan(db, name: str, url: str, callers: dict[str, str],
                     ping_timeout: float | None = None, *, roles: list[str],
                     default_role: str) -> dict[str, object]:
    """Validate creation without generating a credential or writing any row."""
    if not SERVICE_NAME.fullmatch(name):
        raise ValueError("service name must match [a-z0-9][a-z0-9-]{0,62}")
    roles = declared_roles(roles, default_role)
    callers = caller_roles(callers, roles)
    if db.execute("SELECT 1 FROM services WHERE name=?", (name,)).fetchone():
        raise ValueError(f"service {name!r} exists; remove it first to change it")
    plan = {"name": name, "url": service_url(url), "callers": callers,
            "roles": roles, "default_role": default_role,
            "ping_timeout": service_ping_timeout(PING_TIMEOUT_SECONDS if ping_timeout is None else ping_timeout)}
    return {**plan, "plan_digest": _service_plan_digest("add", plan)}


def _service_plan_digest(operation: str, definition: object) -> str:
    return hashlib.sha256(json.dumps([operation, definition], sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def add_service(db, name: str, url: str, callers: dict[str, str],
                ping_timeout: float | None = PING_TIMEOUT_SECONDS, *, roles: list[str],
                default_role: str, digest: str | None = None) -> dict[str, object]:
    """Create a service; a reviewed CLI creation checks its plan in this transaction."""
    plan = service_add_plan(db, name, url, callers, ping_timeout, roles=roles, default_role=default_role)
    if digest is not None and not secrets.compare_digest(digest, str(plan["plan_digest"])):
        raise ValueError("service creation plan is stale; review a fresh plan")
    client_id, secret = "svc_" + secrets.token_urlsafe(16), secrets.token_urlsafe(32)
    db.execute("INSERT INTO services VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               (name, client_id, secret_key(secret), plan["url"], json.dumps(plan["callers"]),
                json.dumps(plan["roles"]), default_role, plan["ping_timeout"]))
    return {**{key: value for key, value in plan.items() if key != "plan_digest"},
            "client_id": client_id, "client_secret": secret}


def _service_definition(found, expected):
    public = {key: value for key, value in found.items() if key != "secret_sha256"}
    if expected is not None and (not isinstance(expected, dict) or
            json.dumps(expected, sort_keys=True, separators=(",", ":")) !=
            json.dumps(public, sort_keys=True, separators=(",", ":"))):
        raise ValueError("service definition changed; removal refused")
    return public


def service_remove_plan(db, name: str, expected: dict[str, object] | None = None) -> dict[str, object]:
    """Review the identity/definition, never raw token values or a stored secret hash."""
    found = service(db, name=name)
    if found is None:
        raise ValueError(f"no service named {name!r}")
    public = _service_definition(found, expected)
    return {**public, "tokens": "all tokens for this service",
            "plan_digest": _service_plan_digest("remove", found)}


def remove_service(db, name: str, expected: dict[str, object] | None = None, *,
                   digest: str | None = None) -> bool:
    """Remove a service and every token minted for it."""
    found = service(db, name=name)
    if found is None:
        return False
    _service_definition(found, expected)
    if digest is not None:
        current = service_remove_plan(db, name, expected)["plan_digest"]
        if not secrets.compare_digest(digest, str(current)):
            raise ValueError("service removal plan is stale; review a fresh plan")
    db.execute("DELETE FROM services WHERE name=?", (name,))
    db.executemany("DELETE FROM records WHERE kind='service' AND key=?", [
        (row[0],) for row in db.execute("SELECT key, payload FROM records WHERE kind='service'")
        if json.loads(row[1]).get("client_id") == found["client_id"]])
    return True


def _service_row(row, *, secret=False):
    roles = declared_roles(json.loads(row[5]), row[6])
    result = {"name": row[0], "client_id": row[1], "url": row[3],
              "callers": caller_roles(json.loads(row[4]), roles), "roles": roles,
              "default_role": row[6], "ping_timeout": row[7]}
    if secret:
        result["secret_sha256"] = row[2]
    return result


def services(db) -> list[dict[str, object]]:
    return [_service_row(row) for row in db.execute("SELECT * FROM services ORDER BY name")]


def caller_services(db, caller):
    """The store's validated role grants, before selective caller removal."""
    return [entry for entry in services(db) if caller in entry["callers"]]


def revoke_caller(db, caller):
    # Keep all other roles and the service's credential/declaration, even when empty.
    for entry in caller_services(db, caller):
        remaining = {name: role for name, role in entry["callers"].items() if name != caller}
        db.execute("UPDATE services SET callers=? WHERE name=?", (json.dumps(remaining), entry["name"]))


def service(db, *, name: str | None = None, client_id: str | None = None) -> dict[str, object] | None:
    """One service by name or by client id, with its secret hash."""
    column, value = ("name", name) if name is not None else ("client_id", client_id)
    row = db.execute(f"SELECT * FROM services WHERE {column}=?", (value,)).fetchone()
    return None if row is None else _service_row(row, secret=True)


def service_grants(db, name: str, callers: dict[str, str], digest: str | None = None) -> dict[str, object]:
    """Review/CAS-update caller roles without rotating service credentials."""
    found = service(db, name=name)
    if found is None:
        raise ValueError("no such service")
    callers = caller_roles(callers, found["roles"])
    public = {key: found[key] for key in ("name", "client_id", "url", "roles", "default_role", "ping_timeout")}
    plan = {**public, "before": found["callers"], "after": callers}
    current = _service_plan_digest("grants", {**plan, "credential_identity": found["secret_sha256"]})
    if digest is not None:
        if not secrets.compare_digest(digest, current):
            raise ValueError("service grant plan is stale; review a fresh plan")
        db.execute("UPDATE services SET callers=? WHERE name=?", (json.dumps(callers), name))
    return {**plan, "plan_digest": current, "applied": digest is not None}


def service_endpoint(db, name: str, url: str, digest: str | None = None) -> dict[str, object]:
    """Review/CAS-update the audience URL without changing credentials or grants."""
    target = service_url(url)
    found = service(db, name=name)
    if found is None:
        raise ValueError("no such service")
    plan = {"name": found["name"], "client_id": found["client_id"],
            "before": found["url"], "after": target,
            "callers": found["callers"], "roles": found["roles"],
            "default_role": found["default_role"], "ping_timeout": found["ping_timeout"]}
    # Bind the approval to credential identity without exposing the secret hash.
    material = {**plan, "credential_identity": found["secret_sha256"]}
    current = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if digest is not None:
        if not secrets.compare_digest(digest, current):
            raise ValueError("service endpoint plan is stale; review a fresh plan")
        db.execute("UPDATE services SET url=? WHERE name=?", (target, name))
    return {**plan, "plan_digest": current, "applied": digest is not None,
            "effect": "preserve credentials and callers; prior-audience tokens become inactive at introspection"}
