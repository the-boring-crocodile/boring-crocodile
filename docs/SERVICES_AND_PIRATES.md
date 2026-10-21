# Services and Pirates

A Pond's agents reach two kinds of neighbours through their worker:

- **Services** are MCP servers (Canvas, Knowledge, Fishmarket, Sourcecache):
  `service_list`, `service_describe`, `service_call`. The admin registers each;
  the worker is a stateless MCP client of it.
- **Pirates** are agents under their own Unix accounts: `pirate_list`,
  `pirate_describe`, and work as jobs, `job_start(pirate=...)`. They register
  themselves over a Unix socket.

The Crocodile never imports a provider SDK, reads a Pirate's or service's
credentials, opens their workspaces, or accepts an endpoint from a caller.
The service side of the contract is implemented once, in
[boring-service-kit](https://github.com/the-boring-crocodile/boring-pond-common/blob/main/packages/boring-service-kit/README.md);
change the two together. A service can also run without any Pond, as its own
issuer: boring-services `docs/CONFIGURATION.md`.

## Services

### The admin registers a service

In the [example site](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/example-site.md): Canvas on the same host behind
the same proxy, and a Knowledge running elsewhere (a remote service needs HTTPS and a longer ping
timeout).

    sudo boring-crocodile-admin service-add canvas --url https://crocodile-url.example.test/services/canvas/mcp --ponds pond,garden
    sudo boring-crocodile-admin service-add knowledge --url https://knowledge-url.example.test/mcp --ponds pond --ping-timeout 2.5

`service-add` checks the name (`[a-z0-9][a-z0-9-]{0,62}`), the URL (absolute,
no credentials, query or fragment; plain `http` only on loopback, because
call tokens travel with every request), the ping timeout (default 1 s, at
most 2.5 s, below the 3 s interval) and that the callers exist (Ponds, or
`pirate/NAME`). It prints `client_id` and `client_secret` once, as JSON alone
on stdout (the one-time reminder goes to stderr, so `> file` or `| jq` gets
only the JSON); the secret is stored as SHA-256 in the OAuth store. Registration carries no data: workers
contact only admin-approved URLs.

- `service-grants NAME --callers COMPLETE_SET` and `service-endpoint NAME
  --url URL` change callers or address without new credentials (plan, then
  `--apply --approve-digest`). A changed URL invalidates old tokens at the
  next introspection (services may cache a positive answer for up to 60 s).
  Neither pushes the change into workers' registries: make the service
  register again (restart it) and check that each worker sees it.
- `service-remove NAME` also deletes its tokens; `--expected-definition JSON`
  removes only if the record still matches. A new ping timeout needs remove
  and add.

### Registration: `POST /services/register`

On the controller, next to `/mcp/POND`; the edge refuses it and `/introspect` from
outside (a remote service needs the edge to allow its address).

- Request: HTTP Basic `client_id:client_secret`, empty body.
- `200 {"ping_interval_seconds": 3.0, "sources": ["pond", "garden"]}`: the
  granted Ponds; the controller has passed `{name, url}` to their workers.
- `401` (`forbidden`, with `WWW-Authenticate: Basic realm="services"`) or
  `400` (`invalid_request`, a body was sent).

### Heartbeat: MCP `server/discover`

Every 3 s each worker sends `server/discover` with clientInfo name = its Pond
and **no Authorization header**; the service exempts exactly that method (by
its `Mcp-Method` header) from authentication. A miss makes the service
`unavailable`; ten drop it. A service re-registers when a Pond has been silent
for two intervals (the kit's watchdog). Anyone who reaches the service can fake
a ping and delay a re-registration; no access is gained.

### Calls carry a minted token

For `service_describe` and `service_call` the controller mints a token:
subject the Pond, resource the service's URL, 300 s, stored hashed as its own
kind (never valid as an agent token, nor the reverse), reused until 60 s
before expiry. The agent's own token never leaves the controller. The worker
sends one stateless Streamable HTTP request per call. That is why `batch` and
job `operations` allow only `service_list`.

The service checks the token at `POST /introspect` (RFC 7662, HTTP Basic with
its own credentials, body `token=...`): `{"active": true, "sub": POND, "aud":
URL, "exp": ..., "client_id": ...}` only for a live token minted for this
service, otherwise `{"active": false}`. The service also checks `aud` and
never passes the token on.

### What agents get

- `service_list()`: `{"services": [{"name", "available", "misses",
  "last_miss"}]}`, only services currently granted to this Pond (a removed
  grant takes effect at once). One ping that merely ran out of time keeps
  `available`.
- `service_describe(service)`: `{"service", "tools": [...]}`.
- `service_call(service, tool, arguments)`: `{"content": [...],
  "structuredContent": {...}}`.

| Outcome | When |
|---|---|
| `not_found` | not granted to this Pond, or not registered |
| `unavailable` | missed its last ping, or refused the connection (nothing sent) |
| the service's own refusal | a tool error carrying `{code, message, ...}` (structured, or as the error text); code and details pass through |
| `tool_error` | any other tool error, with the service's text |
| `forbidden` | HTTP 401/403: the service rejected the call token |
| `service_error` | another JSON-RPC error, with `service_code` |
| a failure | no answer in 30 s, or the connection dropped after sending: it may have run, and it is never re-sent |

A service's failed outcome stays a failure with its details; it is not turned
into a refusal.

## Pirates

### Admission

A Pirate's own installer runs `boring-crocodile-admin pirate-add NAME
--account ACCOUNT --ponds all|a,b` ([INSTALL](INSTALL.md#a-pirate-parrot)): its
account joins `pond-pirates` (may register), the Ponds' workers join
`pirate-clients` (may call it), and it becomes a service caller
(`[pirate_services.callers]`). Only Ponds with `service_registration_enabled`
accept Pirates. Nothing about a Pirate's flavours is in `config.toml`. A
Pirate serves every Pond it is added for with its own grants:
[boring-pirates](https://github.com/the-boring-crocodile/boring-pirates/blob/main/README.md#installing-a-pirate).

Run again for an admitted Pirate with the same account, `pirate-add` offers
it the Ponds it does not serve yet (a Pond added later): only those workers
join `pirate-clients` and restart; a worker counts as served when it is in the
group and its running process has it (a process gets its groups when it
starts). The commands, the `already serves` answer and the Pirate's side:
[INSTALL](INSTALL.md#a-pirate-parrot).

### Registration and heartbeat

For each Pond that accepts guests, setup creates
`/run/boring-crocodile/guests/POND/` (2750, worker : `pond-pirates`); the
worker binds `register.sock` there (0660; it is not itself in the group) and
checks ownership and modes at start. A Pirate connects and sends, once per
active flavour:

    {"operation": "register", "kind": "pirate", "flavour": F, "socket_path": P}

The worker answers `{"ping_interval_seconds": 3}` after checking: the
Pirate's name is the account of the connecting UID (`SO_PEERCRED`); `F`
matches `[a-z0-9][a-z0-9-]{0,62}`; `P` is an absolute path to a socket (not a
symlink) owned by that UID. Otherwise `invalid_request`, `forbidden`, or
`limit` beyond 64 flavours. Registering again replaces the entry
`PIRATE/FLAVOUR`.

The worker pings each flavour every 3 s (1 s timeout); a miss makes it
unavailable with the reason in `last_miss`, ten drop it. Registrations live in
memory: after a worker restart the Pirate registers again.

### Wire protocol (worker to Pirate)

Newline JSON, one request per connection, each answer one envelope:
`{"ok": true, "result": ...}`, `{"ok": false, "error": {code, message,
...}}` (a refusal, passed through) or `{"ok": false, "failure": "text"}`. The
worker checks the peer UID of every connection; the Pirate checks the worker's
UID against its own `client_uids`. A frame is at most 1 MiB either way: a
larger request is refused (`limit`) before sending, a larger answer fails the
job (the run may have done its work).

- `{"operation": "ping", "flavour": F}` -> `{"flavour": F}`; unknown or
  inactive: `not_found`.
- `{"operation": "describe", "flavour": F}` -> the flavour's summary.
- `{"operation": "run", "flavour": F, "job_id": HEX32, "assignment": TEXT,
  "timeout_seconds": N|null}` -> held open until the single answer.

### A Pirate job

`job_start(pirate={pirate, flavour, assignment, timeout_seconds})` is refused
before a job exists when the flavour is not registered (`not_found`), missed
its last ping (`unavailable`) or the payload is malformed (`invalid_request`,
also for a timeout outside 0.1 to 86400). Then the job is `running` while the
worker holds the connection:

- a result completes it (`job_output` returns it);
- a refusal ends it `refused`, the Pirate's code in `result.refusal` (`busy`:
  one job per flavour at a time);
- a failure, a dropped connection or an invalid answer ends it `failed`; the
  run may have done part of its work, and the Pirate's transcript for that
  `job_id` says what;
- `job_stop` closes the connection; without a cleanup acknowledgement the job
  ends `failed`, "remote cleanup unconfirmed". Look at the Pirate before
  retrying.

The Pirate enforces the timeout (null: its default). The worker gives up
`timeout_seconds + 10 s` after sending (null: 86400 + 10 s) and fails the job.
A Pirate restart fails its running jobs; a worker restart marks them
`interrupted`. Nothing is retried. Pirate jobs need managed jobs (commands
enabled) and share the 32-job pool.

### Reading a Pirate's result

The job contract ends at `state` and `result`; the result's shape is the
Pirate's ([boring-pirates](https://github.com/the-boring-crocodile/boring-pirates/blob/main/README.md#a-run-and-its-result)).
Treat the model's `answer` as a **claim** and the Pirate's observed records
(`write_actions` with path and SHA-256, `tool_errors`) as **evidence**: models
do report checks or fixes they never made. A
recorded write proves the write, not that it is right; read the file when it
matters. A completed job means the run finished, not that its claims were
verified.
