# The admin command

`boring-crocodile-admin` is how a root administrator looks at and changes a
running site. Site changes and grant/client removals show a plan first and
apply only with `--apply` and the plan's `--approve-digest` (a changed site makes
the digest stale: plan again). `oauth client-add` creates a client immediately;
`oauth backup` immediately writes a new private backup. `--config PATH` goes
before the command.

| Command | |
|---|---|
| **Looking** | |
| `list` | The Ponds in `config.toml` |
| `show --pond NAME` | One Pond's configuration |
| `status [--pond NAME] [--check]` | Unit state and path checks, as JSON; `--check` exits 1 when degraded |
| `site-check [--json]` | Is the site regular: every file what setup would render, every account as expected. Every site change runs it first. |
| `watch --pond NAME` | A Pond's commands and jobs live (below) |
| **Pirates and services** ([SERVICES_AND_PIRATES](SERVICES_AND_PIRATES.md)) | |
| `pirate-add NAME --account ACCOUNT --ponds all\|a,b` | Admit a Pirate; `pirate-remove NAME` |
| `service-add NAME --url URL --ponds a,b` | Register a service and its callers |
| `service-grants NAME --callers a,b` | Change who may call it (the complete set) |
| `service-endpoint NAME --url URL` | Change its address |
| `service-remove NAME`, `service-list` | |
| **Sign-in** ([OAUTH](OAUTH.md)) | |
| `oauth status [--pond NAME]` | Counts of active and expired records, no tokens |
| `oauth authorizations --pond NAME` | Current grants of a Pond |
| `oauth revoke --pond NAME --client ID \| --all-clients` | Remove a client's grants in one Pond |
| `oauth clients` | Every client: origin, state, redirect hosts, live grants |
| `oauth client-add --name N --redirect-uri URI [--auth ...]` | Pre-register a client (VS Code, Copilot Studio) |
| `oauth client-remove ID`, `oauth client-prune --idle-days 90` | Remove clients and their grants: the plan, then `--apply --approve-digest DIGEST` |
| `oauth backup --output FILE` | A consistent copy of the store |
| **Site** | |
| `uninstall --pond NAME \| --site [--delete-data]` | [OPERATIONS](OPERATIONS.md#uninstalling) |
| `task-policy --pond NAME` | Host administration by a Pond: [PERMISSIONS](PERMISSIONS.md) |

`list`, `show`, `status` and `site-check` read the configuration and ask
systemd; they never read secrets or the OAuth store.

## OAuth commands

They read the controller's SQLite store directly, as root, without creating it
or printing token values; the controller keeps issuing, refreshing and
revoking. A revoke plan names scope and counts; a token refresh in between
makes it stale. Revoking removes the client's current grants in that Pond, not
the client itself, and cannot undo work already dispatched.

A backup must be a new file under a private root-owned directory; it holds
bearer tokens, so treat it like a credential. There is no restore command:
restoring an older store can revive revoked grants and break current sessions.
To restore by hand, stop the controller, keep the current store, and check
that the release reads the snapshot.

## Watching a Pond

    sudo boring-crocodile-admin watch --pond pond [--view compact|timeline|cards] [--no-color] [--json]

Shows each command with its output, job states (`queued`, `running`,
`stopping`, final; a nonzero exit is a *completed* job) and the foreground
calls other processes answer (`service_call`, `service_describe`,
`pirate_describe`, with duration and outcome; never arguments, tokens or
results). Pirate jobs name Pirate and flavour. `--json` is the raw event
stream with exact output bytes and the tasks' cgroup resources.

Open it before starting the work: there is no replay. It is best effort:
events are small datagrams, a full queue drops them (never delaying the work),
and sequence numbers show gaps. A missing final event means unknown, never
success. One viewer per Pond; closing it never stops work.

Only root views. The viewer owns a socket under `/run/boring-crocodile-watch`
and accepts events only from the Pond's account. This is a debugging view for
the owner, not an audit log and not a boundary against root or the Pond's own
account. Nothing is stored.
