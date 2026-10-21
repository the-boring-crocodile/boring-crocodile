# Architecture

The Crocodile is a private MCP server that gives AI clients a place to work on
a Linux host: each Pond is a real Unix account with a home, a handbook and a
shell. It authenticates a client, binds the connection to one Pond, serves
that Pond's context and runs its operations as that Pond's account.

It is WAN-facing and runs commands, so security comes first: where a security
property and convenience or simplicity conflict, security wins.

## The picture

Two pictures in boring-pond show it: in its README,
[a Pond site at a glance](https://github.com/the-boring-crocodile/boring-pond/blob/main/README.md)
(the Crocodile with its Ponds, Pirates and services); in its architecture page,
[the path of one command](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/architecture.md#the-path-of-one-command)
(proxy, controller, worker, systemd and task runner, each frame an account,
with the checks at every step). The parts:

| Part | Runs as | Owns |
|---|---|---|
| Controller (`boring-crocodile-mcp`) | the control account | The public MCP and OAuth endpoints, the sign-in store, routing a request to its Pond's worker, minting short-lived tokens for services. It holds no worktrees, switches no users, runs no commands. |
| Worker (one per Pond) | the Pond's account | Context and bootstrap, file tools, job admission and the job ledger, the Pirate and service registries. |
| Task runner (one per command) | the Pond's account | One command with bounded output, in its own systemd unit and cgroup. |
| systemd | root | Accounts' processes, resource ceilings, cgroup cleanup. |
| Pirate | its own account | Its assignments, its provider, its workspace ([SERVICES_AND_PIRATES](SERVICES_AND_PIRATES.md)). |
| Service | its own container or account | Its tools ([SERVICES_AND_PIRATES](SERVICES_AND_PIRATES.md)). |

The SDK owns the MCP and OAuth protocol; our provider owns durable grants and
their binding to a Pond.

## Ponds and connections

Each Pond has a permanent `profile_id` (a UUID, created once at setup and kept
through renames, reinstalls and backups), its own approval secret, account,
same-named group, home, worker, task socket and resource budget. One
controller, endpoint and OAuth store serve all of them.

- **One URL per Pond:** `https://DOMAIN/mcp/POND`, for example
  `https://crocodile-url.example.test/mcp/garden`. The path decides the Pond:
  the approval page shows it and accepts only its secret. Each URL is its own
  OAuth protected resource (`/.well-known/oauth-protected-resource/mcp/POND`);
  a token works only on the URL it was issued for, so every token is bound
  to exactly one Pond. There is no shared `https://DOMAIN/mcp`; it
  answers 404.
- **The token's subject is the profile ID.** Every request resolves its Pond
  from its token; no tool takes a Pond or environment argument (one is
  refused before any worker is contacted). Removing a Pond invalidates its
  tokens at the next controller start.
- **ChatGPT's multiple accounts:** `pond_profile` carries
  `_meta["openai/profile"]: true`, so ChatGPT can label connections.
- **Possession of a Pond's approval secret authorizes that Pond.** It is an
  owner-operated model, not a multi-user identity provider. Display names show
  on the approval page.

A conversation keeps what it has loaded; changing connectors erases nothing.
Use one conversation per Pond.

### Chats sharing a Pond

Several chats on one Pond share its account, files, processes and budget. Each
command gets its own unit and cgroup, which separates lifetimes, not
confidentiality, and nothing coordinates edits between chats; the file tools'
hash-checked writes make a stale edit fail instead of overwriting.

## Workers and concurrency

One worker per Pond, as the Pond's account. It reads the task socket's
`MaxConnections` and the task lifetime from systemd at start; foreground calls
and jobs share these slots, and a slot is held until the command's cgroup is
confirmed empty. Changing them is the administrator's (socket and worker
restart). A worker with commands disabled still serves context, files,
Pirate listings and service calls; jobs need the task runner.

## Running a command

1. The worker takes a slot and connects to its Pond's task socket (0600, the
   Pond's account, under root-owned directories).
2. systemd starts a runner as the Pond's account in its own unit
   (`Type=exec`, `KillMode=control-group`, `UMask=0077`, the Pond's slice).
   The runner announces its PID and cgroup.
3. The worker checks the socket's ancestry, the runner's UID and cgroup, and
   only then sends the command.
4. The runner runs it with a deadline and bounded output; closing the
   connection means cancel.
5. The runner exits; systemd kills what is left, including children that
   called `setsid`.
6. The worker waits until the cgroup is empty, then returns the result and
   frees the slot.

Cancelling never skips cleanup. If cleanup cannot be confirmed within six
seconds, that worker refuses new work until someone looks: that is a
`ContainmentError`, never a silent result. `exit_code` and `timed_out`
describe the command; `output_incomplete` says a descendant kept a pipe open.

Before closing, the client reads the task's cgroup counters (CPU, memory,
swap, tasks and their limit events) for `admin watch --json`; agents do not
get them. Missing counters are reported as unavailable, never as zero.

A slice's memory ceiling can hit any task in it; the worker and controller
are outside the command slice.

## State

| What | Where | Owner |
|---|---|---|
| OAuth clients, approvals, codes, tokens (SHA-256 only), registered services | `/var/lib/boring-crocodile/oauth.sqlite3` | controller ([OAUTH](OAUTH.md)) |
| Job records, Pirate jobs included | `POND-HOME/.boring-crocodile/jobs.sqlite3` | worker ([TOOLS](TOOLS.md#jobs-and-time-limits)) |
| Pirate and service registries | memory, refreshed by registration and heartbeat | worker |
| Context | the Pond's handbook | the Pond |

After a worker restart, unfinished jobs become `interrupted`: a record is
evidence, not a supervisor.

## Outcomes

Normal outcomes are data. An owner that decides one (not found, busy, stale,
forbidden, limit, ...) raises one `Refusal(code, message, **details)`,
caught once at the worker boundary. A worker reply is exactly one of
`result`, `refusal` or `failure`; agents get a refusal as `{code, message,
...}` and a failure as `{failure}`, both as tool errors. Codes from a Pirate
or service pass through unchanged. Nothing is silently repaired or retried.

## Trust

A Pond member is trusted like a person with an account on the host. `exec` is
deliberately unrestricted within that account: no command allowlist, approval
step or network filter in the MCP. **The account is the boundary**: a Pond member gets a real account,
not a sandbox; file roots and deny lists
are conveniences where `exec` is on. Narrowing or widening a Pond is done with
standard Linux tools: [PERMISSIONS](PERMISSIONS.md).

The boundaries in order:

1. Every public request is authenticated and authorized.
2. The controller validates tool and parameters; the token selects the Pond.
3. Only configured Ponds resolve.
4. The worker socket authenticates the controller.
5. The worker validates paths and limits again.
6. The worker runs as its final account and never switches users.
7. systemd applies process and resource limits independently.
8. Anything unexpected fails closed.
9. Root stays outside ordinary Ponds.

Ponds do not isolate against a compromised controller: it can address every
worker. Execution accounts never join each other's groups; only the controller
joins every Pond's group.

## Configuration rules

`config.toml` is trusted, root-owned input (`site/default/` shows a generated
one). It refuses: no Pond, unknown environments, duplicate profile IDs,
shared sockets, overlapping context roots, task sockets, unit prefixes or
slices used twice, and the removed single-Pond keys (top-level `[context]`,
`[execution]`, `[auth] approval_secret_file`), naming the key to delete.
Config checks do not prove OS isolation; distinct accounts and permissions do.

## Not goals

Choosing an arbitrary Unix user; root through MCP; public multi-tenancy;
agents inside the Crocodile; gating `exec` per command; distributed jobs;
automatic upgrades.
