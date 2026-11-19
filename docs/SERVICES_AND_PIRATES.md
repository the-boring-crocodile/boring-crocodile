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

    sudo boring-crocodile-admin service-add canvas --url https://crocodile-url.example.test/services/canvas/mcp --roles reader,writer --default-role reader --callers pond=writer,garden=reader --apply
    sudo boring-crocodile-admin service-add knowledge --url https://knowledge-url.example.test/mcp --roles reader,writer --default-role reader --callers pond=reader --ping-timeout 2.5 --apply

`service-add` checks the name (`[a-z0-9][a-z0-9-]{0,62}`), the URL (absolute,
no credentials, query or fragment; plain `http` only on loopback, because
call tokens travel with every request), the ping timeout (default 1 s, at
most 2.5 s, below the 3 s interval) and that the callers exist (Ponds, or
`pirate/NAME`). Without `--apply` it shows a plan and creates nothing. With
`--apply`, review the plan on stderr and type `APPLY` at the terminal; it holds
the shared site lock while creating the credential. It prints `client_id`
and `client_secret` once, as JSON alone
on stdout (the one-time reminder goes to stderr, so `> file` or `| jq` gets
only the JSON); the secret is stored as SHA-256 in the OAuth store. Registration carries no data: workers
contact only admin-approved URLs.

- `service-grants NAME --callers NAME=ROLE,... --apply` shows the plan and
  requires typed APPLY to replace the complete caller/role map. Preserve other
  callers. The service reads its role from authenticated introspection, with
  a positive cache bounded by 60 seconds. No local access-list edit, render or
  service restart is needed for a grant/role change.
- `service-endpoint NAME --url URL --apply` changes the address without
  rotating credentials. Old-audience tokens become inactive at introspection.
  Update the service's own URL and proxy routes, restart it to register that
  address, and verify the worker sees it.
- `service-remove NAME` plans removal of the service and its tokens; `--apply`
  asks for typed `APPLY` and takes the site lock. `--expected-definition JSON`
  removes only if the record still matches. A new ping timeout needs remove
  and add.

### Let a Pirate call a service

The service grant carries the Pirate's role. Once the flavour has the
service tools enabled, parrot can gain Knowledge with one command:

    sudo boring-crocodile-admin service-grants knowledge --callers pond=writer,pirate/parrot=reader --apply

Review the plan and type APPLY. This replaces the whole map; keep every
existing caller that should retain access. Knowledge has no second access
list. Parrot's next service listing reads its current grants, and a cached
token reflects role changes within at most 60 seconds, without a restart.

In `/etc/boring-pirates/NAME/config.json`, edit the flavour that will use
the service. Add `service_list`, `service_describe` and `service_call` to
its existing `allowed_tools`. For example, these flavour fields allow one
service and up to three service calls per assignment.

```json
{
  "allowed_tools": ["file_list", "file_read", "service_list", "service_describe", "service_call"],
  "allowed_services": ["knowledge"],
  "max_service_calls": 3
}
```

Keep any other tools the flavour needs. Omitting `allowed_services` permits
all services granted to the Pirate. An empty list permits none. The call cap
is optional. Restart the edited Pirate with
`sudo systemctl restart boring-pirate@parrot.service` (replace `parrot`
with its name). This ends its active assignments. Grant changes take effect
through introspection without editing the flavour or restarting the Pirate.
[Flavour configuration](https://github.com/the-boring-crocodile/boring-pirates/blob/main/docs/CONFIGURATION.md#flavours)
defines the limits. Admission creates no service grant, though re-admitting
a name can make retained grants usable again; the plan shows them first.

`pirate-add` admits the account to the selected Ponds and records its UID
for the local service authority. Its reply supplies the service authority
settings for the Pirate installer. It does not add the Pirate
to any service's caller set or enable the flavour's tools. Its own
configuration may narrow the granted service set. The Pirate calls as `pirate/parrot`, not as
the Pond that gave it work. A custom Unix account does not change that
service subject.

`pirate-remove NAME --ponds a,b` withdraws those active and parked Ponds from
one Pirate through the admission plan and render; add `--apply` to confirm.
It keeps the caller and its other pairs, including when no Ponds remain.
Without `--ponds`, temporary `pirate-remove NAME` removes the whole admission
and keeps service grants. Permanent name retirement
uses `pirate-remove NAME --retire` (plan, then `--apply`): the Crocodile removes
`pirate/NAME` from every service's caller set and revokes its issued service
tokens before rendering admission removal. The Pirate uninstaller requests
this for `--delete-data`, including after an earlier temporary uninstall.
Other callers and the services' credentials stay. Pirate flavour files stay
the operator's; review them before reusing a name.
Existing service-side positive-token caches may take up to 60 seconds to expire.

Check `pirate_describe` for the flavour, then give it a small assignment
that lists the service and makes a call appropriate to its role. Check the
result rather than relying on the model's claim. To withdraw access,
withdraw the relevant service grant. A flavour-file change additionally
requires restarting the Pirate; changing the service grant does not.

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
subject the Pond, resource the service's URL, 3600 s, stored hashed as its own
kind (never valid as an agent token, nor the reverse), reused while more than
900 s are left, so a token handed out always outlives the longest foreground
request. The agent's own token never leaves the controller. The worker sends
one stateless Streamable HTTP request per call. A `batch` may hold
`service_call` steps because the controller checks the grants and mints their
tokens before the batch starts; job `operations` allow only `service_list`,
since a job can outlive any token. (Pirates' own service calls use separate 60 s
tokens from the local authority; this rule is for the Pond's calls.)

The service checks the token at `POST /introspect` (RFC 7662, HTTP Basic with
its own credentials, body `token=...`): `{"active": true, "sub": POND, "aud":
URL, "exp": ..., "client_id": ..., "role": ROLE}` only for a live token minted for this
service, otherwise `{"active": false}`. The role comes from the current
caller grant. The registered service declares its roles and default; the
family service kit supports `reader` and `writer`. A role
change appears at the next introspection (a positive cache lasts at most 60 s).
The service also checks `aud` and
never passes the token on.

### What agents get

- `service_list()`: `{"services": [{"name", "available", "misses",
  "last_miss"}]}`, only services currently granted to this Pond (a removed
  grant takes effect at once). One ping that merely ran out of time keeps
  `available`.
- `service_describe(service)`: `{"service", "tools": [...]}`.
- `service_call(service, tool, arguments, save_to?)`: `{"content": [...],
  "structuredContent": {...}}`, or with `save_to` the `saved_to` record (see
  below).

| Outcome | When |
|---|---|
| `not_found` | not granted to this Pond, or not registered |
| `unavailable` | missed its last ping, or refused the connection (nothing sent) |
| the service's own outcome | a tool error carrying `{status, code, message, ...}` (structured, or as JSON error text); status, code and details pass through |
| `failed/upstream_error` | any invalid or unstructured tool error, with the service's text and unknown effects |
| `forbidden` | HTTP 401/403: the service rejected the call token |
| `failed/upstream_error` | another JSON-RPC error, with `service_code` |
| `not_admitted` | no time left for the call; nothing sent |
| `limit` | more than 16 file references or 512 KiB of them |
| `stale`, `not_found`, `forbidden`, `invalid_request` | a file reference or the save target was refused before the call (nothing sent) |
| a failure | no answer in time (at most 30 s), or the connection dropped after sending: it may have run, and it is never re-sent |

A service outcome requires `status: refused|failed`, a valid lowercase code
and a string message. Details are flat and must be JSON values. A failed outcome stays
failed with its original code and details.

### File references and save_to

`service_call` resolves `{"$file": {"root", "path", "sha256"?}}` in its
arguments to the file's text and, with `save_to`, writes the answer to a file
and returns `saved_to` instead of the answer. Both happen in the worker
through its file tools (the worker kit's `file_refs`, which Pirates use too),
so the file policy decides what may be read and written; the service sees
plain arguments and never a path. After the service ran, a save that does not
succeed returns the answer with `save_to_refused` (nothing written) or
`save_to_failed` (failed/write_failed or failed/timeout, with unknown effects:
the write failed or did not finish in time and may still land). Both embeds
are flat outcome objects. Inside a batch that step is `failed` with `code`
(the save outcome's name) and keeps the answer in its `result`, so `on_error:
stop` stops before a step that would rely on the file.
The agent-facing rules are in the guide.

### Time

Every request from the controller to a worker carries its budget
(`budget_seconds`); the worker caps it per operation and the controller waits
the budget plus a 5 s reply margin, so the worker's own deadline, which says
what may have run, is what the caller sees. A single `service_call` has 45 s:
the service's 30 s plus reading references and saving. Inside a batch a
service step ends 5 s before the batch does, so its save is reported by the
step and never cut off. The controller and its workers must run the same
release; deploy restarts them together.

## Pirates

### Admission

A Pirate's own installer runs `boring-crocodile-admin pirate-add NAME
--account ACCOUNT --ponds all|a,b` ([INSTALL](INSTALL.md#a-pirate-parrot)): its
account joins `pond-pirates` (may register), the Ponds' workers join
`pirate-clients` (may connect), and its caller record stores `uid` and
admitted `ponds` in the Crocodile description. The worker admits only Pirates
in that Pond's list; the shared group alone grants no admission. This grants no service membership:
[Let a Pirate call a service](#let-a-pirate-call-a-service).
Every enabled Pond has a registration socket;
provisioning creates its directory and the shared registration group. Reception
alone grants no admission. Nothing about a Pirate's flavours is in `config.toml`. A
Pirate serves every Pond it is added for with its own grants:
[boring-pirates](https://github.com/the-boring-crocodile/boring-pirates/blob/main/README.md#installing-a-pirate).

For a later Pond, use `pirate-add NAME --ponds a,b --apply`; the existing
account is retained and the named Ponds join the recorded list. Admission and
worker client membership are reconciled by the common renderer. An
`already_serves` preview means no file/group differences; an approved apply
still runs the repair tail and restarts enabled workers and the controller.
The response supplies `service_authority` directly; Pirate configuration has
no worker UID list. Park removes and saves a Pond's pairs, bring-back restores
those pairs, and retire drops both active and saved pairs. Commands and response:
[INSTALL](INSTALL.md#a-pirate-parrot).

### Registration and heartbeat

For each Pond, setup creates
`/run/boring-crocodile/guests/POND/` (2750, worker : `pond-pirates`); the
worker binds `register.sock` there (0660; it is not itself in the group) and
checks ownership and modes at start. A Pirate connects and sends, once per
active flavour:

    {"operation": "register", "kind": "pirate", "flavour": F, "socket_path": P, "version": V}

The worker requires the peer UID's admitted pair for this Pond and a version
in its declared Pirates range. `F` matches `[a-z0-9][a-z0-9-]{0,62}`; `P`
is an absolute path to a socket owned by that UID; the socket itself must
not be a symlink.
The result carries `ping_interval_seconds`, `accepted: true`, the Pond and
configured Pirate name. Refusals name malformed
input (`invalid_request`), absent admission (`forbidden`), incompatible
versions (`unsupported`) or registry capacity (`limit`, 64 flavours).
Registering again replaces `PIRATE/FLAVOUR`.

The controller's existing local authority also offers
`{"operation": "registration", "pond": NAME}`. It uses the Pirate's kernel
UID, requires an admitted pair and an enabled Pond in the loaded description,
then vouches for the root-provisioned registration directory's owner UID,
and canonical socket path. It reads directory metadata,
so the controller needs no access inside the worker's registration directory.
The client transport verifies the controller's socket and peer UID. A worker's
reply alone cannot prove that the root description admitted the pair; the
Pirate must also check the live registration socket and its peer against
the controller's answer.

The worker pings each flavour every 3 s (1 s timeout); a miss makes it
unavailable with the reason in `last_miss`, ten drop it. Registrations live in
memory: after a worker restart the Pirate registers again.

### Wire protocol (worker to Pirate)

Newline JSON, one request per connection. Success is
`{"ok":true,"result":{...}}`; an error is
`{"ok":false,"outcome":{"status":"refused|failed","code":"...","message":"...",...}}`.
The worker preserves its typed status, code and details. Registration and
authority sockets use the same outcome envelope. HTTP service registration
returns the flat outcome body with its HTTP status and authentication header.

Install the matching Crocodile and Pirates set in one maintenance window;
keep Pirate work stopped until both updates are complete. Pirates require
the Crocodile line declared in their ordinary release `requires`; Pirate
deploy checks that floor before preparing or switching. The Crocodile checks
the Pirate's version at registration, the one runtime version check, with a
named refusal for an incompatible Pirate. Crocodile deploy and render have no
installed-Pirates or interpreter-release gate. Wire-format changes bump
the minor version. Roll back both
components together. Unexpected replies become failed/bad_reply with unknown effects.

A Pirate socket served by another account is refused/forbidden before sending
and logged as a warning; investigate the socket's owner.

The worker checks every peer UID and the description's Pirate/Pond admission.
The Pirate accepts work only from a worker whose live registration socket
accepted it, with the socket and peer UID vouched by the configured controller.
It drops trust on disappearance or replacement and checks current controller
admission before work. There is no configured worker UID grant.
A frame is at most 1 MiB either way: an oversized request is
refused/limit before sending, an oversized answer fails with bad_reply and
unknown effects. An expired deadline before dispatch is refused/not_admitted.

- `{"operation": "ping", "flavour": F}` -> `{"flavour": F}`; unknown or
  inactive: `not_found`.
- `{"operation": "describe", "flavour": F}` -> the flavour's summary.
- `{"operation": "run", "flavour": F, "job_id": HEX32, "assignment": TEXT,
  "timeout_seconds": N|null}`, and optionally `"overrides": {...}` -> held open
  until the single answer. The worker sends `overrides` only when the job has
  them; the Pirate checks them against the flavour (`allow_overrides`, its
  tools and services, its caps) and refuses what the flavour cannot take. A
  Pirate older than 0.3.6rc9 refuses a run with the field.

### A Pirate job

`job_start(pirate={pirate, flavour, assignment, timeout_seconds, overrides?})`
is refused before a job exists when the flavour is not registered
(`not_found`), missed its last ping (`unavailable`) or the payload is malformed
(`invalid_request`, also for a timeout outside 0.1 to 86400, or overrides that
are not an object; overrides above 64 KiB are `limit`). `overrides` passes
through unchanged; the Pirate's refusal of them ends the job `refused`. Then the job is `running` while the
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
`timeout_seconds + 15 s` after sending (null: 86400 + 15 s): the executor's
10 s cleanup reserve for a running command plus the 5 s reply margin; then it
fails the job.
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
