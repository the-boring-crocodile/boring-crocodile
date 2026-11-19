-- Published boring-crocodile v0.4.21 oauth_records._create_services and
-- Common v0.7.13 boring_service_kit.issuer.store.create_tables/create_clients.
-- Exact column/constraint/index layout; no schema6 database relabeling.
CREATE TABLE records (
    kind TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
    expires REAL, pair TEXT, PRIMARY KEY(kind, key));
CREATE INDEX record_expiry ON records(expires);
CREATE INDEX record_pair ON records(pair);
CREATE TABLE clients (
    client_id TEXT PRIMARY KEY,
    origin TEXT NOT NULL CHECK(origin IN ('admin','metadata','dynamic')),
    state TEXT NOT NULL CHECK(state IN ('registered','active')),
    payload TEXT NOT NULL, created REAL NOT NULL, refreshed REAL NOT NULL,
    last_used REAL, expires REAL,
    CHECK((state = 'active') = (expires IS NULL)));
CREATE INDEX client_expiry ON clients(expires);
CREATE TABLE services (
    name TEXT PRIMARY KEY, client_id TEXT NOT NULL UNIQUE,
    secret_sha256 TEXT NOT NULL, url TEXT NOT NULL, ponds TEXT NOT NULL,
    ping_timeout REAL NOT NULL DEFAULT 1.0);
PRAGMA user_version=5;
