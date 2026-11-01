# Installing the Crocodile

A new site in four steps: deploy the runtime, create the first Pond, activate
the site, connect a client. Later you can add Ponds, Pirates and services to
the running site. Separating the runtime from the accounts and site settings
lets you update the code later without replacing a Pond's home or handbook.

Setup commands show a plan before applying it. `deploy` has a separate
read-only `--check`; running it without that flag performs the deployment.

## Our example

This page follows the Pond family's [example site](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/example-site.md):
the Crocodile at `crocodile-url.example.test`, listening on `127.0.0.1:8503`
and running as `crocodile-mcp`; the first Pond `pond` with the account
`crocodile-user`; later the Pond `garden`, the Pirate `parrot` and the
service `canvas`. Put your own values where these appear.

## Before you start

- A Linux host with systemd and cgroup v2, and `python3` 3.11 or newer (only to
  run the deploy, which refuses an older one; the runtime brings its own Python).
- sudo, and a root-owned parent directory for the Pond homes (here `/home`).
- `https://crocodile-url.example.test` served by your reverse proxy, which
  forwards the Crocodile's paths to `127.0.0.1:8503` (the list: [The reverse
  proxy](#the-reverse-proxy) below). DNS, TLS, the proxy
  and the firewall are yours; setup does not touch them.
- `curl`, and the release from the [releases page](https://github.com/the-boring-crocodile/boring-crocodile/releases):
  the archive `boring-crocodile-V-linux-x86_64.tar.gz` and its `.sha256` file
  (tag `vV`); step 1 downloads both. The checksum detects a
  damaged download, not who built it; the releases carry no signatures or
  attestations yet. Versions tested together:
  [releases and versions](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/updating.md#releases-and-versions).

## 1. Deploy the runtime

One block, from the download to the deploy; set `V` to the release you
install (the newest is on the releases page) and run it in one shell:

    V=0.4.20
    f=boring-crocodile-$V-linux-x86_64.tar.gz
    u=https://github.com/the-boring-crocodile/boring-crocodile/releases/download/v$V
    mkdir -p ~/Downloads && cd ~/Downloads
    curl -fLO "$u/$f" && curl -fLO "$u/$f.sha256" &&
    sha256sum -c "$f.sha256" &&            # must print OK; if not, the block stops here
    sudo mkdir -p /root/releases &&
    sudo tar xzf "$f" -C /root/releases &&
    sudo install -d -m 0755 /opt/boring-crocodile &&   # the deploy refuses an install folder that does not exist
    sudo /root/releases/boring-crocodile-$V/deploy

The deploy installs the runtime under `/opt/boring-crocodile/current` and
nothing else: no accounts, no credentials, no running service. How deploy,
update and rollback work is the same for every Pond component:
[boring-deploy](https://github.com/the-boring-crocodile/boring-pond-common/blob/main/packages/boring-deploy/README.md).
The Crocodile's specifics are in [OPERATIONS](OPERATIONS.md).

The commands below live in `/opt/boring-crocodile/current/bin`. Put them on
root's PATH:

    sudo ln -sfn /opt/boring-crocodile/current/bin/boring-crocodile-{setup,admin} /usr/local/bin/

## 2. Create the first Pond

    sudo boring-crocodile-setup --wizard --apply

The wizard asks for the accounts, the home, the name people see and the
starting context, and checks each answer as you give it; we answer
`crocodile-user`, `crocodile-mcp`, `/home/crocodile-user`, the default name
and the packaged starter. It shows the plan in a few plain lines, applies it
when you type `APPLY`, and names the next step. It creates:

- the accounts `crocodile-user` (the Pond) and `crocodile-mcp` (the
  Crocodile), each with a same-named group, password disabled, shell
  `nologin`; `crocodile-mcp` joins the group `crocodile-user`;
- `/home/crocodile-user` with `handbook/`, `work/` and `lab/`;
- the starting context in `handbook/`: the packaged starter handbook
  (`HANDBOOK.md`, `WELCOME.md`), a proposal to edit together; or your own
  files instead (`context_mode: import`, at most 64 files of 1 MiB each), for
  example one of boring-pond's sample handbooks;
- the receipt `/etc/boring-crocodile/initial-setup.json` with the Pond's
  permanent `profile_id`.

No service starts and no credential is created yet.

Unattended, the same answers come from a file. Review the plan, then apply
exactly that plan by its digest:

    sudo boring-crocodile-setup --config setup-inputs.json
    sudo boring-crocodile-setup --config setup-inputs.json --apply --approve-digest DIGEST

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

(`--wizard --save-inputs setup-inputs.json` writes this file from your answers.)
An optional `"display_name"` (up to 64 characters, for example `"The Steady
Beaver"`) is the name people see on the sign-in page and in `pond_ping` and
`pond_profile`; without it the first Pond is "The Pond" and an added one its
name. The same key works in an `--add-pond` file. The file
holds names and paths, never credentials. Existing accounts or paths are not
adopted: they make the plan ask for review.

## 3. Activate the site

Pirates need a local registration socket so they can announce their flavours
to a Pond. Enable that reception only for Ponds that will use them
(`service_registration_enabled`); it does not itself admit a Pirate. The site
wizard leaves it off. You can enable it later with
[`--site-render --guests POND`](OPERATIONS.md#updating).

Our site will have the Pirate `parrot`, so it activates from a file, after
creating the group the Pirates join:

    sudo groupadd --system pond-pirates
    sudo boring-crocodile-setup --site-config site-inputs.json
    sudo boring-crocodile-setup --site-config site-inputs.json --apply --approve-digest DIGEST

```json
{
  "public_base_url": "https://crocodile-url.example.test",
  "bind_host": "127.0.0.1",
  "bind_port": 8503,
  "runtime_root": "/opt/boring-crocodile/current",
  "memory_bytes": 2147483648,
  "swap_bytes": 0,
  "tasks_max": 256,
  "task_tasks_max": 128,
  "service_registration_enabled": true
}
```

A site without Pirates can use the wizard instead. It asks for the public
URL, the local address and port, and the memory ceiling per Pond (it proposes
a quarter of the host's memory, between 512M and 8G); swap and process limits
keep the defaults above. At the end it says where the approval secret is:

    sudo boring-crocodile-setup --site-wizard --apply

Either way, activation writes `/etc/boring-crocodile/config.toml` (each Pond
with its home as the file root its file tools may read and write), the systemd
units, slice and tmpfiles rules, and the Pond's approval credential
`/etc/boring-crocodile/credentials/approval-secret` (mode 0600). It starts the
site and watches local health for up to a minute; a site that stays
unhealthy is reported as `configured_but_degraded`.

### The reverse proxy

The Crocodile answers on loopback; your proxy forwards these paths of
`https://crocodile-url.example.test` to `127.0.0.1:8503`, unchanged:

- `/mcp/*` (each Pond's MCP endpoint, `/mcp/POND`)
- `/.well-known/oauth-protected-resource/mcp/*` and
  `/.well-known/oauth-authorization-server`
- `/authorize`, `/token`, `/revoke`, `/register`, `/approve` (sign-in)
- `/introspect` and `/services/register` (services check tokens and register
  here)

`/services/register` is the Crocodile's even though services may live under
`/services/NAME/...`: give it its own rule, before any `/services/*` rule. In
Caddy, for our example:

    crocodile-url.example.test {
        @crocodile path /mcp/* /.well-known/oauth-protected-resource/mcp/* /.well-known/oauth-authorization-server /authorize /token /revoke /register /approve /introspect /services/register
        handle @crocodile {
            reverse_proxy 127.0.0.1:8503
        }
    }

A service on the same domain adds its own routes (boring-services README).

## 4. Check and connect

    sudo boring-crocodile-admin status --check
    sudo boring-crocodile-admin list

`status --check` exits nonzero on degraded local state. It cannot see your TLS,
your proxy or a client; only the client journey proves those:

1. In your MCP client, add the Pond's URL, `https://DOMAIN/mcp/POND`. In our
   example: `https://crocodile-url.example.test/mcp/pond`. Clients presenting a
   supported metadata document can start with that URL alone. For a registered
   client, create its ID with `sudo boring-crocodile-admin oauth client-add
   --name ... --redirect-uri ...` and configure it in the client; open dynamic
   registration is off ([OAUTH](OAUTH.md)). See the family's
   [client guide](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/clients.md)
   for VS Code and version-specific Visual Studio notes.
2. The sign-in page asks for the Pond's approval secret. Read it in a private
   administrator terminal: `sudo cat /etc/boring-crocodile/credentials/approval-secret`.
   Never put it in a URL, a chat, a command history or a log.
3. Ask the client to call the Pond's tools `pond_bootstrap` and then `exec`
   with the command `pwd`: the answer is `/home/crocodile-user`, the Pond's
   home, run as `crocodile-user`.

Each connection belongs to one Pond for good; there is no switching Ponds
inside a conversation. Use one conversation per Pond.

## Adding to a running site

Each addition reads the site as it is, runs `site-check` first and stops on
anything irregular. Without `--apply` it shows its plan, with a diff per
changed file; `--apply` asks for `APPLY` (unattended: `--apply
--approve-digest DIGEST`), backs up what it replaces under
`/var/lib/boring-crocodile-site/site-backups/`, restarts what needs it and runs
`site-check` again.

### Another Pond: `garden`

Create the account and home first, then review how they will join the running
site. This lets you inspect the new Pond's service grants and resource limits
before it can accept a client connection:

    sudo boring-crocodile-setup --add-pond garden --wizard --apply
    sudo boring-crocodile-setup --site-extend --grant-services all

The second command only previews the extension. If the grants and limits fit,
apply its top-level `plan_digest`:

    sudo boring-crocodile-setup --site-extend --grant-services all --apply --approve-digest DIGEST

`all` grants every currently registered service. Use comma-separated service
names instead when the new Pond needs fewer; keep the same selection in the
preview and apply commands.

Unattended, `--add-pond garden --config garden.json` takes the same answers as
step 2's file, with the Crocodile's existing account as `control_user`:

```json
{
  "execution_user": "garden-user",
  "control_user": "crocodile-mcp",
  "home": "/home/garden-user",
  "context_mode": "starter",
  "context_source": null,
  "context_files": []
}
```

Each plan prints its `plan_digest` at the top level: that is the one to pass
to `--apply --approve-digest` (a plan with service grants also shows the
grants' own digest inside; it is not the one to approve).

`--add-pond` creates the Pond's own account, group and home as in step 2; we
answer `garden-user` and `/home/garden-user`. A Pond's name is 1 to 24
lowercase letters, digits and inner hyphens; it becomes the path in its URL.
`--site-extend` brings it into the running site: its worker, task socket,
slice and tmpfiles file are new, the Crocodile gains its group and its
credential `/etc/boring-crocodile/credentials/garden-approval-secret`, and its
section is appended to `config.toml` (existing lines stay as they are). It
gets setup's default ceilings (memory a quarter of RAM, 512 MiB to 8 GiB;
swap 0; 256 tasks), not the values the site was activated with, unless
`--memory 2G`, `--swap 512M` or `--tasks 256` say otherwise. `--grant-services` takes `all` or names; without it the Pond
gets no service.

Its URL: `https://crocodile-url.example.test/mcp/garden`.

The site's Pirates are not offered `garden` by this: when it accepts guests,
the plan and the answer name each Pirate and the `pirate-add` command that
offers it `garden` ([A Pirate](#a-pirate-parrot)).

A Pond added before activation is simply activated with the others in step 3.
Taking a Pond out and bringing it back: [OPERATIONS](OPERATIONS.md).

### A Pirate: `parrot`

The Pirate's own installer (boring-pirates) does this; by hand it is a plan,
then the apply:

    sudo boring-crocodile-admin pirate-add parrot --account pirate-parrot --ponds all
    sudo boring-crocodile-admin pirate-add parrot --account pirate-parrot --ponds all --apply

`--ponds all` means the Ponds that accept guests at that moment.

It lets `pirate-parrot` register with the Ponds that accept guests (group
`pond-pirates`), lets those Ponds' workers call it (group `pirate-clients`,
the workers restart), and lists it as a caller of the services it is granted.
Its answer names what the Pirate's own configuration needs. `pirate-remove
parrot` takes it back. See [SERVICES_AND_PIRATES](SERVICES_AND_PIRATES.md).

A Pond added later (`garden`) is offered to the existing Pirate by the same
command again, with the same account:

    sudo boring-crocodile-admin pirate-add parrot --account pirate-parrot --ponds all
    sudo boring-crocodile-admin pirate-add parrot --account pirate-parrot --ponds all --apply

It plans only what is missing, as the host shows it: `garden`'s worker joins
`pirate-clients` and restarts; `config.toml` and the controller stay as they
are. With nothing missing it answers `already serves: pond, garden` and
changes nothing. Then the Pirate's side, which the answer names
(`pirate_side`): `client_uids` in `/etc/boring-pirates/parrot/config.json` must
include the uids it lists (add `garden-user`'s), and the Pirate restarts,
after which it registers with `garden` too:

    sudo systemctl restart boring-pirate@parrot.service

Another account under the name `parrot` is refused. `pirate-add` never takes
a Pond away; `pirate-remove` takes out the whole Pirate.

### A service: `canvas`

The site learns about a service with `boring-crocodile-admin service-add`,
which prints the credentials the service's configuration needs; then the
service's own deploy installs it. The order and the commands:
[boring-services](https://github.com/the-boring-crocodile/boring-services/blob/main/README.md#installing-on-linux); what
`service-add` checks and grants: [SERVICES_AND_PIRATES](SERVICES_AND_PIRATES.md).

## If a step is interrupted

Setup keeps root-private receipts (`/etc/boring-crocodile/initial-setup.json`,
`site-activation.json`, `ponds/garden/`) and never deletes users, documents or
partial state on failure. Run the same command with the same inputs, the same
digest and `--resume`: recorded steps are checked again, and anything it did
not record (an account a crash may have created, say) stops it for you to look
at. A completed setup run again reports `already_complete` and does not touch
an edited handbook.

Exit codes of a plan: 0 proposed, 2 invalid or failed, 3 review required. Of
an apply: 0 done or already done, 2 failed.

To take a Pond or the whole site away again: [OPERATIONS](OPERATIONS.md).

## Reference: a default site

`site/default/` holds every file steps 2 and 3 write for the wizard's default
answers (accounts `boring-crocodile` and `boring-crocodile-mcp`, home
`/home/boring-crocodile`), at the paths they are written to (`two-ponds/`: with
a second Pond). It is generated (`site/default/generate.py`), and
`tests/test_site_default.py` fails when it and the renderer drift apart. On a
real host the UIDs, the `profile_id`, the URL and the memory ceiling (a
quarter of RAM, 512 MiB to 8 GiB) differ.
