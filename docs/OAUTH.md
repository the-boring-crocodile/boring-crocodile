# Sign-in and the OAuth store

The controller signs clients in with OAuth (the Common issuer,
`boring_service_kit.issuer`, with the Ponds as targets) and keeps everything
in one SQLite store, `/var/lib/boring-crocodile/oauth.sqlite3` (schema 5):
clients, agent grants and the admin-added services. Code:
`sqlite_oauth_provider.py`, storage contract `oauth_records.py`. Commands:
[ADMIN](ADMIN.md).

## Who may sign in

Following the MCP specification of 2026-07-28:

| Client | How it gets a client id | Configuration |
|---|---|---|
| Client presenting a metadata-document URL | HTTPS document on an allowed host, fetched (no redirects, 16 KiB, 5 s) and cached a day | `[auth] metadata_document_hosts`, default `claude.ai` and `chatgpt.com` |
| Client configured with an administrator-issued ID | pre-registered: `admin oauth client-add --name ... --redirect-uri ...` | use the returned ID in that client's configuration |
| Client using dynamic registration (RFC 7591) | registers with the issuer | `[auth] dynamic_registration = true`, off by default |

Client support depends on the product and version. The family's
[client guide](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/clients.md)
gives the explicit VS Code client-ID setup and separates our Visual Studio17.14
observations from current-client acceptance.

A client has an origin (`admin`, `metadata`, `dynamic`) and a state:
`registered` until its first completed sign-in (gone after a day; at most 1000
per origin), then `active` (kept until removed). Loopback redirect URIs match
on any port (RFC 8252). The approval page shows the redirect host first and
how the client is known, and warns when it registered itself or redirects
only to loopback.

Then the person enters the Pond's approval secret. Five wrong secrets cancel
that sign-in (counted in the controller's memory).

## Lifetimes and binding

| | Lifetime |
|---|---|
| pending approval, authorization code | 300 s |
| access token | 3600 s |
| refresh token | 14 days |
| service-call token (minted for one Pond and service) | 3600 s, renewed below 900 s left |

Each token is bound to the resource it was issued for: one `/mcp/POND`, or a
service's URL. An authorization request for any other resource, or for none,
is refused with `invalid_target`; bare `/mcp` is not an endpoint (404). Refresh rotates both tokens; revoking one
revokes its partner; a consumed grant is rejected. Code exchange and token
issue run in one `BEGIN IMMEDIATE` transaction; errors roll back. Lock waits
are limited to five seconds: this is a store for a small private site.

## What is stored, and how

- **Bearer values** (approvals, codes, access, refresh and service-call
  tokens) only as SHA-256; nothing needs them back.
- **Service secrets** as SHA-256.
- **Client records** as plain JSON, including a client secret where one
  exists: the MCP library compares it, and a secret alone does not connect
  (PKCE and the approval secret are still needed).

The directory belongs to the control account (0700), the file is 0600. Its
records are protected by permissions, not encryption: keep it, and its
backups, out of Git, logs and Pond-readable places.

## Schema and backup

An empty store starts at schema 5, the only schema this release reads. Any
other schema, an unversioned or a corrupt store refuses to start, unchanged;
there is no in-memory fallback.

A release that changes the schema says so, and its deploy backs the store up
before switching (asked, or `--backup`). By hand, at any time, with the admin
command of the release installed now:

    sudo boring-crocodile-admin oauth backup --output /root/oauth-before-VERSION.sqlite3

It uses SQLite's backup, refuses an existing file and checks integrity.

## Restore

There is no restore command, on purpose. To restore by hand: stop the
controller, keep its current state directory, check the backup
(`PRAGMA integrity_check`, `user_version`), put it into an empty directory with
the right owner and mode 0600, start a release that reads that schema, and
sign in once to check.

An older store can bring back revoked credentials and lose newer clients and
rotations. If that is not acceptable, start with an empty store and sign
everyone in again. Rolling back code is not a reason to restore old state.
