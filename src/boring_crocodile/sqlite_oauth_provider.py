"""The Crocodile's OAuth: the Common issuer, with Ponds as targets and services on top.

The sign-in itself (clients, approval, tokens) is boring_service_kit.issuer;
this adds what only the Crocodile has: one target per Pond (`/mcp/<pond>`
locks it; there is no untargeted resource), the services the admin added,
the short-lived tokens it mints for a Pond's calls to them, and introspection
for those services.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import re
import time
from pathlib import Path

from boring_service_kit.issuer import ClientPolicy, ConsentDetails, Denied, SQLiteIssuer, Target, canonical_resource
from boring_service_kit.issuer.provider import (MAX_APPROVAL_ATTEMPTS, MAX_PENDING, MAX_PENDING_PER_CLIENT,
                                                MAX_UNUSED_CLIENTS, PENDING_SECONDS, UNUSED_CLIENT_SECONDS, Pending)
from mcp.server.auth.provider import AccessToken

from .oauth_records import SECRET_KINDS, initialize, secret_key
from .oauth_records import service as find_service, services as list_services
from .settings import PondSettings
from .site_layout import POND_NAME

__all__ = ["ConsentDetails", "Denied", "MAX_APPROVAL_ATTEMPTS", "MAX_PENDING", "MAX_PENDING_PER_CLIENT",
           "MAX_UNUSED_CLIENTS", "PENDING_SECONDS", "Pending", "SQLiteOAuthProvider", "UNUSED_CLIENT_SECONDS",
           "canonical_resource", "pond_resources"]


def pond_resources(public_base_url: str, ponds: dict[str, PondSettings]) -> dict[str, str | None]:
    """Every protected resource this server is, mapped to the Pond its path names.

    Each resource is `/mcp/<pond>`, so every token is bound to exactly one
    Pond. Bare `/mcp` is not a resource: the issuer
    refuses to authorize it, or a request without a resource, with
    `invalid_target`. Pond names become path segments, so they must be
    boring names.
    """
    base = f"{public_base_url.rstrip('/')}/mcp"
    resources: dict[str, str | None] = {}
    for name in ponds:
        if not re.fullmatch(POND_NAME, name):
            raise ValueError(f"Pond name {name!r} cannot be a URL path segment")
        resources[str(canonical_resource(f"{base}/{name}"))] = name
    return resources


class SQLiteOAuthProvider(SQLiteIssuer):
    secret_kinds = SECRET_KINDS

    def __init__(self, public_base_url: str, database_file: Path, ponds: dict[str, PondSettings],
                 policy: ClientPolicy = ClientPolicy()) -> None:
        self.pirate_service_principals: frozenset[str] = frozenset()
        self.ponds = ponds
        self.public_base_url = public_base_url.rstrip("/")
        targets = {name: Target(pond.profile_id, pond.display_name, pond.approval_secret_file)
                   for name, pond in ponds.items()}
        super().__init__(self.public_base_url, database_file, pond_resources(self.public_base_url, ponds),
                         targets, policy, initialize=initialize)

    @property
    def profile_ids(self) -> set[str]:
        return self.subjects

    def pending_pond(self, request_id: str) -> str | None:
        """The Pond a pending request is locked to by its resource path, if any."""
        return self.pending_target(request_id)

    def target_noun(self) -> str:
        return "a Pond"

    # Services: the admin adds them (oauth_records.add_service); the control
    # plane authenticates them, mints their call tokens and answers their
    # introspection requests.

    def service(self, name: str) -> dict[str, object] | None:
        with self._read_database() as db:
            return find_service(db, name=name)

    def service_inventory(self) -> list[dict[str, object]]:
        with self._read_database() as db:
            return list_services(db)

    def authenticate_service(self, authorization: str | None) -> dict[str, object] | None:
        """The service whose HTTP Basic client id and secret these are, if any."""
        scheme, _, encoded = (authorization or "").partition(" ")
        if scheme.lower() != "basic":
            return None
        try:
            client_id, separator, secret = base64.b64decode(encoded, validate=True).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return None
        if not separator:
            return None
        with self._read_database() as db:
            found = find_service(db, client_id=client_id)
        # The stored value is a hash; compare hashes in constant time.
        stored = str(found["secret_sha256"]) if found is not None else secret_key("")
        if found is None or not hmac.compare_digest(secret_key(secret), stored):
            return None
        return found

    def mint_service_token(self, pond: str, granted: dict[str, object], seconds: int) -> AccessToken:
        """A short-lived token for one Pond's calls to one service."""
        if pond not in granted["ponds"]:
            raise ValueError("caller has no service grant")
        value = self._new_token("service_")
        token = AccessToken(token=value, client_id=str(granted["client_id"]), scopes=[],
                            expires_at=int(time.time()) + seconds,
                            resource=str(granted["url"]), subject=pond)
        with self._transaction() as db:
            self._put(db, "service", value, token)
        return token

    def introspect(self, granted: dict[str, object], token: str) -> dict[str, object]:
        """RFC 7662 for the service asking: active only for its own live tokens."""
        found = self._load("service", token, AccessToken)
        if found is not None and isinstance(found.subject, str) and found.subject.startswith("pirate/"):
            if found.subject not in self.pirate_service_principals:
                return {"active": False}
        if (found is None or found.resource != granted["url"]
                or found.client_id != granted["client_id"] or found.subject not in granted["ponds"]):
            return {"active": False}
        return {"active": True, "sub": found.subject, "aud": found.resource,
                "exp": found.expires_at, "client_id": found.client_id}
