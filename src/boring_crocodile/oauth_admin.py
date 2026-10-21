"""Root-side inspection of the existing controller-owned OAuth store.

This module never creates or repairs the live database. OAuth records remain
owned by the provider; this administrator uses its version-one record contract.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time


from boring_service_kit.issuer.clients import add_client, list_clients, prune_clients, remove_client

from .oauth_records import (
    GRANTS as _GRANTS, VERSION as _VERSION, Transaction, add_service, approval_digest,
    decode as _decode, delete, open_readonly as _open_ro, remove_service, rows, selected,
    services, service_grants, service_endpoint, validate as _validate,
)


def _clients_digest(operation: str, argument: object, clients: list[dict[str, object]]) -> str:
    """What a client removal plan approves: the operation and the clients it
    selects, as listed (state, use, live grant counts)."""
    return hashlib.sha256(json.dumps([operation, argument, clients], sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _safe_source(path: Path, controller_uid: int) -> None:
    if not path.is_absolute() or controller_uid <= 0:
        raise ValueError("invalid OAuth store identity")
    parent, item = path.parent.lstat(), path.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != controller_uid
            or stat.S_IMODE(parent.st_mode) != 0o700):
        raise ValueError("OAuth directory ownership or mode differs from controller")
    if (not stat.S_ISREG(item.st_mode) or item.st_nlink != 1
            or item.st_uid != controller_uid or stat.S_IMODE(item.st_mode) != 0o600):
        raise ValueError("OAuth file ownership or mode differs from controller")


class OAuthAdmin:
    def __init__(self, path: Path, controller_uid: int):
        self.path, self.controller_uid = path, controller_uid
        _safe_source(path, controller_uid)

    def _rows(self, db):
        return rows(db)

    def status(self, profile_id: str | None = None):
        with closing(_open_ro(self.path)) as db:
            rows = self._rows(db)
            clients = list_clients(db)
        counts = {kind: 0 for kind in ("client", "pending", *_GRANTS, "service")}
        expiry = {kind: {"active": 0, "expired": 0, "non_expiring": 0}
                  for kind in counts}
        now = time.time()
        if profile_id is None:
            counts["client"] = len(clients)
            for client in clients:
                expires = client["expires"]
                expiry["client"]["non_expiring" if expires is None else "expired" if expires <= now else "active"] += 1
        for row in rows:
            value = _decode(row)
            # Service tokens name the Pond by name, not profile: counted site-wide only.
            if profile_id is None or (row[0] in _GRANTS and value["subject"] == profile_id):
                kind, expires = row[0], row[3]
                counts[kind] += 1
                bucket = ("non_expiring" if expires is None
                          else "expired" if expires <= now else "active")
                expiry[kind][bucket] += 1
        return {"integrity": "ok", "schema": _VERSION, "counts": counts,
                "expiry": expiry, "scope": "all" if profile_id is None else "pond"}

    def authorizations(self, profile_id: str):
        with closing(_open_ro(self.path)) as db:
            rows = self._rows(db)
            clients = {c["client_id"]: c["name"] for c in list_clients(db)}
        grants = {}
        now = time.time()
        for row in rows:
            if row[0] not in _GRANTS:
                continue
            value = _decode(row)
            if value["subject"] != profile_id or (row[3] is not None and row[3] <= now):
                continue
            client = value["client_id"]
            entry = grants.setdefault(client, {"client_id": client,
                "client_name": clients.get(client), "active_counts": {},
                "scopes": set(), "latest_expiry": None})
            entry["active_counts"][row[0]] = entry["active_counts"].get(row[0], 0) + 1
            entry["scopes"].update(value["scopes"])
            if row[3] is not None:
                entry["latest_expiry"] = max(entry["latest_expiry"] or row[3], row[3])
        return [{**entry, "scopes": sorted(entry["scopes"])}
                for _, entry in sorted(grants.items())]

    _selected = staticmethod(selected)
    _digest = staticmethod(approval_digest)

    def plan_revoke(self, profile_id: str, client_id: str | None):
        with closing(_open_ro(self.path)) as db:
            selected = self._selected(self._rows(db), profile_id, client_id)
        return {"pond_profile": profile_id, "client_id": client_id,
                "records": {kind: sum(row[0] == kind for row in selected) for kind in _GRANTS},
                "plan_digest": self._digest(profile_id, client_id, selected),
                "effect": "delete current grants only; future authorization remains possible"}

    def revoke(self, profile_id: str, client_id: str | None, digest: str):
        _safe_source(self.path, self.controller_uid)
        with Transaction(self.path) as db:
            chosen = self._selected(self._rows(db), profile_id, client_id)
            if digest != self._digest(profile_id, client_id, chosen):
                raise ValueError("OAuth revoke plan is stale; review a fresh plan")
            delete(db, chosen)
        return {"status": "revoked", "records": len(chosen),
                "pond_profile": profile_id, "client_id": client_id}

    def clients(self) -> list[dict[str, object]]:
        """Every client: origin (admin, metadata, dynamic), state, redirect hosts, live grants."""
        with closing(_open_ro(self.path)) as db:
            _validate(db)
            return list_clients(db)

    def client_add(self, name: str, redirect_uris: list[str], auth: str = "none") -> dict[str, object]:
        """Pre-register a client (VS Code, Copilot Studio); a secret, if any, is shown once."""
        _safe_source(self.path, self.controller_uid)
        with Transaction(self.path) as db:
            _validate(db)
            return add_client(db, name, redirect_uris, auth)

    def client_remove(self, client_id: str, digest: str | None = None) -> dict[str, object]:
        """Without a digest: what would go, and the plan digest. With the digest
        of that plan: the client and all its grants go, if it is still as planned."""
        _safe_source(self.path, self.controller_uid)
        with Transaction(self.path) as db:
            _validate(db)
            found = [c for c in list_clients(db) if c["client_id"] == client_id]
            if not found:
                raise ValueError(f"no client {client_id!r}")
            planned = _clients_digest("client-remove", client_id, found)
            if digest is not None:
                if digest != planned:
                    raise ValueError("client-remove plan is stale; review a fresh plan")
                remove_client(db, client_id)
        return {"client": found[0], "applied": digest is not None, "plan_digest": planned}

    def client_prune(self, idle_days: float, digest: str | None = None) -> dict[str, object]:
        """Clients not used for idle_days without a live refresh token (admin clients
        are kept). Without a digest: the plan. With its digest: exactly the planned
        clients go, or nothing does when the selection changed meanwhile."""
        _safe_source(self.path, self.controller_uid)
        with Transaction(self.path) as db:
            _validate(db)
            stale = prune_clients(db, idle_days)
            planned = _clients_digest("client-prune", idle_days, stale)
            if digest is not None:
                if digest != planned:
                    raise ValueError("client-prune plan is stale; review a fresh plan")
                for client in stale:
                    remove_client(db, str(client["client_id"]))
        return {"applied": digest is not None, "idle_days": idle_days, "clients": stale,
                "plan_digest": planned}

    def service_add(self, name: str, url: str, ponds: list[str],
                    ping_timeout: float | None = None) -> dict[str, object]:
        """The answer carries the client secret; it is shown once and stored hashed."""
        _safe_source(self.path, self.controller_uid)
        with Transaction(self.path) as db:
            _validate(db)
            if ping_timeout is None:
                return add_service(db, name, url, ponds)
            return add_service(db, name, url, ponds, ping_timeout)

    def service_grants(self, name: str, callers: list[str], digest: str | None = None) -> dict[str, object]:
        _safe_source(self.path, self.controller_uid)
        if digest is None:
            with closing(_open_ro(self.path)) as db:
                _validate(db)
                return service_grants(db, name, callers)
        with Transaction(self.path) as db:
            _validate(db)
            return service_grants(db, name, callers, digest)

    def service_endpoint(self, name: str, url: str, digest: str | None = None) -> dict[str, object]:
        _safe_source(self.path, self.controller_uid)
        if digest is None:
            with closing(_open_ro(self.path)) as db:
                _validate(db)
                return service_endpoint(db, name, url)
        with Transaction(self.path) as db:
            _validate(db)
            return service_endpoint(db, name, url, digest)

    def service_remove(self, name: str, expected: dict[str, object] | None = None) -> dict[str, object]:
        _safe_source(self.path, self.controller_uid)
        with Transaction(self.path) as db:
            _validate(db)
            if not remove_service(db, name, expected):
                raise ValueError(f"no service named {name!r}")
        return {"status": "removed", "name": name}

    def service_list(self) -> list[dict[str, object]]:
        with closing(_open_ro(self.path)) as db:
            _validate(db)
            return services(db)

    def backup(self, destination: Path):
        _safe_source(self.path, self.controller_uid)
        if not destination.is_absolute() or destination == self.path:
            raise ValueError("backup needs a distinct absolute destination")
        parent = destination.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0
                or stat.S_IMODE(parent.st_mode) & 0o077):
            raise ValueError("backup directory must be root-owned and private")
        fd = os.open(destination, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1:
                raise ValueError("unsafe backup destination")
            os.close(fd)
            fd = -1
            with closing(_open_ro(self.path)) as source:
                _validate(source)
                with closing(sqlite3.connect(destination)) as target:
                    source.backup(target)
                    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("backup integrity check failed")
            return {"status": "backed_up", "path": str(destination),
                    "integrity": "ok", "bytes": destination.stat().st_size}
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        finally:
            if fd != -1:
                os.close(fd)
