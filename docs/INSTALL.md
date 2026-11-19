# Crocodile setup reference

For a first installation, follow the family's
[Getting started](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/getting-started.md).
That page owns download, provisioning, activation, proxy setup and the first
client call. This reference describes the commands, inputs and files.

For later work, use the [task map](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/installation.md).
An existing legacy description requires the
[adoption procedure](OPERATIONS.md#adopting-a-pre-description-site), before
normal deployment.

## Host and runtime

Linux releases target x86_64, glibc 2.39 or newer, systemd and cgroup v2.
Python 3.12 or newer runs deploy; the runtime brings its own Python.
The home parent and installed runtime must be root-owned and protected
from unprivileged writes. No ACL executable is required.

Memory defaults to a quarter of host RAM, bounded to 512 MiB–8 GiB for
each Pond. The worker and its commands share that ceiling. It reserves no
memory. Docker is needed for the Linux service deploys, not this runtime.

<a id="1-deploy-the-runtime"></a>
The runtime lives at `/opt/boring-crocodile/current`; its setup and admin
commands are in `bin/`. A release's deploy installs runtime code without
creating a site. `deploy --check` is read-only. Without that flag it
prepares, checks, switches and starts according to the installed state.
[Operations](OPERATIONS.md#updating) owns update behavior and recovery.

## Setup commands

All planned mutations preview without `--apply`. With it, they recompute
the plan and require typed APPLY at a terminal. Plan digests are internal
stale-state guards, not approval tokens.

| Input or operation | Meaning |
|---|---|
| `--wizard` | Ask for a Pond's accounts, home, display name and handbook |
| `--config FILE` | Read those provisioning answers from JSON |
| `--add-pond NAME` | Provision an additional, parked Pond; reuse the controller |
| `--save-inputs FILE` | Save wizard answers, without credentials |
| `--site-wizard` | Ask for public origin, listener and memory policy |
| `--site-config FILE` | Read site policy from JSON |
| `--site-extend` | Enable parked declarations and render |
| `--grant-services all\|a,b` | Grant selected registered services to added Ponds |
| `--site-render` | Reconcile the saved description and generated integration |
| `--apply` | Review and confirm the fresh plan, then perform it |

<a id="2-create-the-first-pond"></a>
## Provisioning inputs

Setup JSON and imported handbook sources must be root-owned, protected from
other writers and free of symlinks throughout their paths. Stage these inputs
under a protected root-owned directory before running setup. Preview shows
their paths, owners and modes. Apply reads the admitted handbook bytes before
provisioning changes the host. Packaged handbooks need no input staging.

A JSON input has these required fields:

```json
{
  "execution_user": "crocodile-user",
  "control_user": "crocodile-mcp",
  "home": "/home/crocodile-user",
  "context_mode": "starter",
  "context_source": null,
  "context_files": []
}
```

An optional `display_name` is the sign-in name, up to 64 characters.
Packaged context choices are `starter`, `engineer` and `consultant`.
Each seeds HANDBOOK.md, WELCOME.md and the shared OPERATING_TOGETHER.md.
Import mode instead uses the selected `context_source` and `context_files`;
it accepts at most 64 files of 1 MiB each.

Provisioning publishes numeric identity bindings and a permanent profile UUID
before creating accounts. It creates missing secrets and seeds missing files;
it retains existing secret and file bytes on a rerun. It starts no worker.
Unexpected existing accounts or paths require review.

The initial Pond is named `pond`. Additional names are 1–24 lowercase
letters, digits and inner hyphens. Each Pond has a dedicated account,
same-named private group, home and declaration.

## Site-policy inputs

These fields are required in `--site-config` JSON:

```json
{
  "public_base_url": "https://crocodile-url.example.test",
  "bind_host": "127.0.0.1",
  "bind_port": 8503,
  "runtime_root": "/opt/boring-crocodile/current",
  "memory_bytes": 2147483648,
  "swap_bytes": 0,
  "tasks_max": 256,
  "task_tasks_max": 128
}
```

Optional fields are `no_new_privileges` (default true) and
`protected_ports` (additional TCP ports). The public URL is an HTTPS
origin. Bind host is an IP literal; port is 1024–65535. The per-task process
limit may not exceed the shared process limit.

Activation enables the initial Pond and declares the site running.
The common renderer writes units, slices and tmpfiles rules, starts the
desired components and checks local readiness. It creates no account or
secret. Every enabled Pond has reception; admission is separate.

## Description and files

| Path | Owner of the contents |
|---|---|
| `/etc/boring-crocodile/config.toml` | Site state, controller binding, listener, OAuth-store path |
| `/etc/boring-crocodile/ponds/NAME/config.toml` | Pond identity, context, limits, execution and credential path |
| `/etc/boring-crocodile/credentials/` | Root-only approval secrets |
| `/var/lib/boring-crocodile/oauth.sqlite3` | Sign-ins, service credentials and grants |
| `HOME/handbook/`, `work/`, `lab/` | The Pond's editable files |
| `/etc/systemd/system/boring-crocodile-*` | Generated host integration |

The main description contains no secrets. Permission details live in
[PERMISSIONS](PERMISSIONS.md); schema and derived defaults live in
[OPERATIONS](OPERATIONS.md#the-description-and-the-render).

## Controller listener and protected ports

A root-owned systemd socket holds the controller listener. Restarting the
controller keeps that socket open; a full site stop closes it.
Pond and Pirate command slices deny TCP binds to the controller port and ports
18510–18539 on IPv4 and IPv6. Service registrations with loopback URLs must
use a protected port. Add custom loopback ports to `server.protected_ports`
and render before using them. Up to 127 additional ports are supported.

These rules need cgroup/BPF socket-bind support and govern the Pond and Pirate command slices,
not unrelated accounts. Verify the host enforces them before admitting
untrusted work. An account with root authority can change that policy.

## The reverse proxy

Forward these paths unchanged to the configured listener:

- `/mcp/*`, `/.well-known/oauth-protected-resource/mcp/*`
- `/.well-known/oauth-authorization-server`
- `/authorize`, `/token`, `/revoke`, `/register`, `/approve`
- `/introspect`, `/services/register`

Keep `/services/register` on the Crocodile when services add routes.
The [install walkthrough](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/getting-started.md#4-connect-the-reverse-proxy)
has the Caddy example. DNS, TLS and firewall policy remain operator-owned.

## Adding to a running site

[Add a Pond](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/add-pond.md)
owns provisioning and extension. Service policy is stored separately in
SQLite: service mutations do not render or restart the Crocodile.

<a id="a-pirate-parrot"></a>
### Pirate admission

The Pirate installer handles first admission. Manual `pirate-add NAME
--account ACCOUNT --ponds all|a,b` previews admission, client membership and
the render tail; `--apply` confirms it. `all` selects currently enabled
Ponds. For an existing Pirate, `pirate-add NAME --ponds a,b --apply` adds
Ponds to its recorded list without repeating the account or editing Pirate
configuration. The description's `[pirate_services.callers.NAME]` owns the
`uid` and admitted `ponds`; the response supplies `service_authority` directly.
The running Pirate discovers the new sockets and verifies admission.
`pirate-remove NAME --ponds a,b` previews withdrawal of those active or parked
Ponds; `--apply` confirms the same render. Other pairs stay recorded, and
removing the last Pond leaves the Pirate admitted for none. Without `--ponds`,
`pirate-remove NAME` removes the whole admission; `--retire` is only for whole
removal. A withdrawn Pond loses access even while its old socket remains.
Another account under an existing Pirate name is refused. Registration names
the Pirate from this record, rather than from the account's login name. Admission restarts
the controller and every enabled Pond worker. It grants no service access:
[Let a Pirate call a service](SERVICES_AND_PIRATES.md#let-a-pirate-call-a-service)
owns the three grants and the restarts needed for them.
[Services and Pirates](SERVICES_AND_PIRATES.md) owns these policies;
[Pirates](https://github.com/the-boring-crocodile/boring-pirates/blob/main/README.md#installing-a-pirate)
owns installation.

## Interrupted changes

For provisioning, rerun the same inputs after inspecting the failure.
For a saved desired description, the common render repairs integration even
when file contents already match. Explicit retirement is separate from
render. [Operations](OPERATIONS.md#when-a-change-stops-part-way) gives the
recovery path.

Exit codes are 0 for success or valid preview, and 2 for invalid/failed work.
Provisioning also returns 3 when the plan requires review.

## Reference: a default site

`site/default/` is generated from default inputs; `two-ponds/` adds
a second Pond. `site/default/generate.py` owns those files and
`tests/test_site_default.py` checks them. Real numeric identities, UUIDs,
URL and memory ceiling differ. Start an installation with
[Getting started](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/getting-started.md).
