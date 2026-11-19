# The admin command

`boring-crocodile-admin` is how a root administrator looks at and changes a
running site. Planned changes show the fresh plan and require `APPLY` in a
terminal when run with `--apply`. If the site changes while you review it,
the apply refuses: run it again and review the new plan. `oauth client-add` creates a client immediately;
`oauth backup` immediately writes a new private backup. `--config PATH` goes
before the command.

All four service mutations show a plan without changing the store. With
`--apply`, typed `APPLY` confirms it; the shared site lock and a fresh DB
comparison guard the write. Service policy stays in SQLite: no render or
Crocodile restart. Only an applied `service-add` prints the one-time secret,
as JSON on stdout; its plan and prompt stay on stderr.

| Command | |
|---|---|
| **Looking** | |
| `list` | Enabled Ponds from the main and per-Pond descriptions, as the existing detailed JSON array |
| `list --json` | Installer inventory: `public_base_url` from the description, enabled Pond names, all admitted Pirate name-to-UID bindings, and `pond_workers` name-to-UID bindings including parked Ponds (from the description, without live sockets) |
| `show --pond NAME` | One Pond's configuration |
| `status [--pond NAME] [--check]` | Unit state and path checks, as JSON; `--check` exits 1 when degraded |
| `site-check [--json]` | Observe the common render plan: desired state, generated-file differences and supported host adjustments; never apply it. |
| `watch --pond NAME` | A Pond's commands and jobs live (below) |
| **Pirates and services** ([SERVICES_AND_PIRATES](SERVICES_AND_PIRATES.md)) | |
| `pirate-add NAME --account ACCOUNT --ponds all\|a,b` | Admit a Pirate, showing existing service grants |
| `pirate-remove NAME [--ponds a,b] [--retire]` | Remove selected active and parked Ponds, keeping the Pirate; without `--ponds`, remove admission. Whole removal with `--retire` also revokes service grants and tokens |
| `service-add NAME --url URL --roles reader,writer --default-role reader --callers a=reader,b=writer` | Register a service's role declaration and caller roles |
| `service-grants NAME --callers a=reader,b=writer` | Change the complete caller-to-role map; empty grants nobody |
| `service-endpoint NAME --url URL` | Change its address |
| `service-remove NAME`, `service-list` | |
| **Sign-in** ([OAUTH](OAUTH.md)) | |
| `oauth status [--pond NAME]` | Counts of active and expired records, no tokens |
| `oauth authorizations --pond NAME` | Current grants of a Pond |
| `oauth revoke --pond NAME --client ID \| --all-clients` | Remove a client's grants in one Pond |
| `oauth clients` | Every client: origin, state, redirect hosts, live grants |
| `oauth client-add --name N --redirect-uri URI [--auth ...]` | Pre-register a client (VS Code, Copilot Studio) |
| `oauth client-remove ID`, `oauth client-prune --idle-days 90` | Remove clients and their grants: the plan, then `--apply` |
| `oauth backup --output FILE` | A consistent copy of the store |
| **Site** | |
| `uninstall --pond NAME \| --site [--delete-data]` | [OPERATIONS](OPERATIONS.md#uninstalling) |

### Installer inventory contract

The current candidate's `boring-crocodile-admin list --json` returns:

```json
{
  "public_base_url": "https://crocodile-url.example.test",
  "ponds": ["pond", "garden"],
  "pirates": {"parrot": 1300},
  "pond_workers": {"pond": 1301, "garden": 1302, "parked": 1303}
}
```

`ponds` contains enabled Pond URL names; parked declarations are excluded.
`pirates` contains admitted names and their positive numeric Unix UIDs.
`pond_workers` contains description-owned worker UIDs, including parked Ponds;
`public_base_url` is the description's normalized public origin. These four
keys are the inventory contract; installers validate that exact key set.
The read has no effects, reads no secret contents and does not require running
workers. A missing or invalid description exits nonzero rather than returning
an empty inventory. Consumers must check that exit before parsing JSON.
Plain `list` keeps its detailed array. Installers ask this command instead
of reading another component's configuration files.

`list`, `show`, `status` and `site-check` read the main and per-Pond
configuration. `status` and `site-check` inspect the host and systemd, read the
OAuth service list when the store exists, and check approval files for length
and UTF-8 encoding. Their output contains no secret value. `status --check` tests
local runtime health at that observation; it does not wait for stability.
An applied render waits for ten seconds of active, unchanged controller/worker
main PIDs. External Pirate/service registration and the public client journey
require their own checks. Workers/task runners read only their own Pond
file plus the main config. The controller reads the enabled Pond files named
by its generated unit; root administration reads the whole description.
Provisioning, adoption and render restore the description's owner/group modes
and remove stale ACLs on managed paths. `site-check` reports permission drift;
old private receipts remain untouched.

Pirate admission and uninstall edit the desired description and call the
same renderer as `setup --site-render`. An approved render always runs its
systemd tail, including when files match. Service mutations remain SQLite
writes with no render. Accounts/secrets are explicit setup actions; retiring
a Pond's admission and access requires explicit `uninstall --delete-data`,
which locks its account and reserves its UID/GID.
Without that flag uninstall parks and keeps its description and data.
Recovery and start/stop semantics: [OPERATIONS](OPERATIONS.md).

## Protected service ports

Run `sudo /opt/boring-crocodile/current/bin/boring-crocodile-admin service-ports`
to see which loopback service ports have confirmed rendered bind protection.
The JSON returns `protected_service_ports` and `reason`. When protection
cannot be confirmed the list is empty and the reason names the stopped site,
missing or mismatched slice rules, a conflicting external drop-in, a pending
daemon reload or a failed systemctl observation. A `null` reason means the
reported ports were verified. Registration and tunnel checks name a requested
port missing from the rendered list.

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
