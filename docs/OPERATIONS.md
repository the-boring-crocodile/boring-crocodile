# Operating a site

Updating, configuring, backing up and removing a running Crocodile site.
Installing one: [INSTALL](INSTALL.md). How deploy, update and rollback work in
general: [boring-deploy](https://github.com/the-boring-crocodile/boring-pond-common/blob/main/packages/boring-deploy/README.md).
The admin command: [ADMIN](ADMIN.md).

Go straight to [updating](#updating), [starting and stopping](#starting-and-stopping),
[configuration](#changing-configuration), [backup and restore](#backup-and-restore),
[secret replacement](#replacing-an-approval-secret), [removal](#uninstalling),
or [an interrupted site change](#when-a-site-change-stops-part-way).

## Where things are

| Path | What |
|---|---|
| `/opt/boring-crocodile/current`, `previous`, `releases/` | The runtime (a standalone Python with the locked wheels), switched by the deploy |
| `/opt/boring-crocodile/DEPLOYED` | One line per deploy |
| `/etc/boring-crocodile/config.toml` | The site configuration |
| `/etc/boring-crocodile/credentials/` | Approval secrets, 0600, root |
| `/etc/boring-crocodile/ponds/NAME/`, `*.json` receipts | What setup created |
| `/var/lib/boring-crocodile/oauth.sqlite3` | Sign-ins, grants, registered services ([OAUTH](OAUTH.md)) |
| `/var/lib/boring-crocodile-site/` | Root's own records: `site-backups/` (files replaced by `--site-extend`, `--site-render`, `pirate-add`) and `site-log.jsonl`; kept by uninstall |
| `/run/boring-crocodile/` | Worker and task sockets; `guests/POND/register.sock` for Pirates |
| `POND-HOME/.boring-crocodile/jobs.sqlite3` | The Pond's job records |

The deploy writes only under `/opt/boring-crocodile`; everything in `/etc`,
`/var/lib` and the homes belongs to the site.

## Updating

Finish or stop active work first: an update restarts running components,
interrupting commands and client connections. It does not replay interrupted
jobs. A code update and a change to the site's systemd units are separate
operations, so you can review host-policy changes instead of inheriting them
silently with a new runtime.

### Replace the runtime

From a trusted, unpacked release (see [INSTALL](INSTALL.md#1-deploy-the-runtime)):

    sudo boring-crocodile-VERSION/deploy --check
    sudo boring-crocodile-VERSION/deploy

`--check` is read-only: it verifies the release and the host and writes
nothing. The deploy itself then runs the Crocodile's check, `admin list` from
the new release against the site, before the switch; its smoke is `admin
status`: every check that passed before the switch must pass after it, and
the controller must run whatever it did before (a check that already failed
is reported, not fatal). The deploy backs up the OAuth store when its schema
changes and restarts the site's controller and worker services that are
running: a short outage. The controller first waits up to 90 seconds for open
connections to close, so the switch can sit quiet that long; clients
reconnect afterwards. Running commands end (systemd cleans up their cgroups);
their jobs are listed as `interrupted`; nothing replays them. A stopped site stays stopped: the deploy says `site is stopped: not
started` and skips start and smoke. On a host without `config.toml` the
restart, check and smoke are skipped. A deploy holds the site lock, so it
and a site change (setup, `--site-extend`, `pirate-add`, ...) refuse each
other rather than overlap.

### Review site-file changes

A deploy does not change rendered site files (units, tmpfiles, `config.toml`
sections). When a release renders them differently, its check says so, and
`site-check` reports each file as differing from the template. Preview those
changes as root:

    sudo boring-crocodile-setup --site-render

The preview shows a diff per file and a digest. After reviewing it, apply that
plan:

    sudo boring-crocodile-setup --site-render --apply --approve-digest DIGEST

Apply backs up the files it replaces under
`/var/lib/boring-crocodile-site/site-backups/`, writes them, reloads systemd,
restarts what runs (the controller and workers, changed task sockets; running
jobs are interrupted) and runs site-check again. Any other irregular finding
stops it. Drop-ins are never touched: keep local changes there, since a hand
edit of a base unit shows up as a diff and is replaced once the plan is
approved. Drop-ins may set only the directives site-check knows (write paths,
limits, groups, runtime caps, umask, ...); anything else, such as extra
hardening (`IPAddressDeny=`, `Protect...=`) or `ExecStartPre=`, makes
site-check irregular and stops this command and `--site-extend`. A masked or
linked unit is refused, not unmasked. On a stopped site the files are written
and nothing starts. Apply records controller activity before writing; a controller that was active
must remain active and pass settled health afterward. A failed restart reports
`rendered_but_degraded`, rather than treating the site as already stopped. An unreadable or
transitional pre-change state stops before writing; an unreadable post-change state reports
`local_health: unknown` with `activity_error` and recovery guidance.
`--guests POND[,POND]` lets running Ponds accept guests (Pirates) in
the same step: their config.toml line, the worker's write path and the
registration folder.

### Roll back a runtime

Use the previous release's deploy, `sudo /opt/boring-crocodile/previous/deploy`.
This switches the code back; it does not undo work the AI did or restore the
OAuth database. Check the release's data compatibility before going back
([backup and restore](#backup-and-restore)).

A rollback out of a restart loop can find the units and task sockets at
systemd's start limit: `sudo systemctl reset-failed 'boring-crocodile*'`,
then the rollback again.

## Starting and stopping

The site is one systemd target; it starts the controller, the workers, the
task sockets and the slices, and stopping it stops running work:

    sudo systemctl enable --now boring-crocodile-mcp.target
    sudo systemctl disable --now boring-crocodile-mcp.target   # stays off across updates

## Changing configuration

The controller and workers read `config.toml` once at start. After an edit,
restart the affected worker and the controller together, so both see the same
values (`daemon-reload` only for unit changes; it does not reload the
config). Task runners read the config when they start. Handbook edits are read
on every bootstrap, no restart.

A Pond's name as people see it (the sign-in page, `pond_ping`,
`pond_profile`) is `display_name` in its `[ponds.NAME]` table; change it
there and restart the controller (`systemctl restart
boring-crocodile-mcp.service`). site-check judges `config.toml` by its
values, so the edit keeps the site regular.

Removed fields (`default_timeout_seconds`, `max_timeout_seconds`,
`max_parallel_tasks` in an environment) are refused, not ignored. Time limits:
[TOOLS](TOOLS.md#jobs-and-time-limits).

## Backup and restore

The release can replace the software; it cannot recreate a Pond's work or
remember who was allowed in. Back up those separately. In particular, use the
OAuth backup command below for a consistent copy of the live database.

What to back up, most important first:

1. **Each Pond's home**: its work, its handbook and its job records
   (`.boring-crocodile/jobs.sqlite3`). This is the Pond's work and memory;
   everything else can be set up again.
2. `/etc/boring-crocodile`: configuration, approval secrets, receipts.
3. The OAuth store: `boring-crocodile-admin oauth backup --output FILE`
   writes a consistent SQLite copy while the site runs, into a folder only
   root can read (it refuses any other).
4. Services and Pirates: their own state folders (each one's README).

The runtime, the units and the sockets are not data: a deploy and setup
bring them back.

To restore on the same host after a loss, with the same release deployed:

    sudo systemctl stop boring-crocodile-mcp.target
    # put back /etc/boring-crocodile (root, as it was) and the homes, each
    # owned by its Pond's account; copy the OAuth backup to
    # /var/lib/boring-crocodile/oauth.sqlite3, owned by the controller, 0600
    sudo systemctl start boring-crocodile-mcp.target
    sudo boring-crocodile-admin site-check
    sudo boring-crocodile-admin status --check

The accounts must still exist with the same names and IDs; site-check
reports anything that does not fit. Moving a site to another host is not a
path we have built: set it up fresh there and copy each home's contents in.

Restoring an older OAuth store can bring back revoked grants or lose newer
ones; a code rollback alone is not a reason to restore it. An update that
changes a data format is the exception: the older release refuses the newer
data, so going back means the backup taken before the update
([updating](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/updating.md)).

## Replacing an approval secret

Each Pond's secret is one root file in `/etc/boring-crocodile/credentials/`:
`approval-secret` for the first Pond, `NAME-approval-secret` for the others.
Changing the secret prevents new approvals with the old value; it does not
invalidate tokens clients already hold. To make those clients sign in again,
replace the secret, restart the controller (which reads it at start), and
revoke the Pond's existing grants. This example is for the first Pond, `pond`:

    sudo sh -c 'umask 077; cd /etc/boring-crocodile/credentials && python3 -c "import secrets; print(secrets.token_urlsafe(32))" > approval-secret.new && mv approval-secret.new approval-secret'
    sudo systemctl restart boring-crocodile-mcp.service
    sudo boring-crocodile-admin oauth revoke --pond pond --all-clients      # shows the plan
    sudo boring-crocodile-admin oauth revoke --pond pond --all-clients --apply --approve-digest DIGEST
    sudo boring-crocodile-admin site-check

Clients then sign in again with the new secret.

## Logs

journald first: `journalctl -u boring-crocodile-mcp -u 'boring-crocodile-worker-*'`.
Secrets, environments and command output are not logged. `admin watch` shows
a Pond live, best effort, root only; it is not an audit log.

## Uninstalling

Choose whether you want to park a Pond for later or retire its identity.
Without `--delete-data`, its accounts and grants remain so it can be brought
back. With that flag, they are removed and retained homes are made private to
root. Neither choice deletes the home contents for you.

    sudo boring-crocodile-admin uninstall --pond NAME
    sudo boring-crocodile-admin uninstall --pond NAME --apply --approve-digest DIGEST
    sudo boring-crocodile-admin uninstall --site [--delete-data]

Like every site change it reads the site as it is, runs `site-check` first
and stops on anything irregular, shows its plan, and applies only that plan
by digest. It holds the site lock.

- `--pond NAME` takes one added Pond out: its sections leave `config.toml`,
  its units (with drop-ins), tmpfiles file and runtime folders go. It stops
  and starts the whole site target, so every Pond's connections drop briefly.
  Stopping waits until the controller and every worker are down (each within
  its stop timeout) before anything is removed.
  The first Pond, `pond`, goes only with `--site`.
- `--site` removes the whole integration: `config.toml`, the controller,
  target and every Pond's units. Without `--delete-data` it first marks the
  activation receipt `uninstalled` and records in it the Ponds that are live,
  their ceilings and the Pirate callers. `boring-crocodile-setup --site-config`
  (or `--site-wizard`) then installs the site again from the kept accounts,
  homes, credentials and OAuth grants: the Ponds that were live, with their
  ceilings, and the Pirate callers. A parked Pond stays parked
  (`--site-extend --bring-back NAME` still takes it back). Everything else
  (address, public origin, guests and `no_new_privileges` per Pond) comes
  from the activation's own settings.
- Without `--delete-data` a Pond is **parked**: accounts, groups, credential
  and OAuth grants stay, and `--site-extend --bring-back NAME` takes it back.
- `--delete-data` also removes the credentials, the Pond's OAuth grants and
  service tokens (in one transaction; other callers stay), its `--add-pond`
  folder and the accounts and groups (the site: also the controller account,
  state folder and OAuth store). Apply refuses if an account changed since the
  plan. Remove accounts in the same run as the site: afterwards uninstall has
  nothing left to read them from.
- `--pond NAME --delete-data` also works on a Pond that `config.toml` no
  longer names (a parked Pond, or one an earlier apply left part-way): it
  reads the Pond from its `--add-pond` folder and removes what is still
  there. The folder goes last.

**Homes are never removed.** The result lists them as no longer live; delete
them by hand once nothing in them is needed. With `--delete-data` the home's
top folder then belongs to root (mode 0700): uninstall changes it before it
removes the account, so an account created later with the same uid cannot
read it. What is inside keeps its owner.

It refuses before any change when: site-check finds anything irregular,
`config.toml` or the site target is missing, the Pond is unknown (or is
`pond` with `--pond`), an account changed, the OAuth schema is unexpected, a
removal would cross a mount or follow a symlink, a home to give to root is a
symlink, a mount point or someone else's, the digest is stale, or
another operation holds the lock.

To remove the runtime afterwards: `sudo rm -rf /opt/boring-crocodile` and the
command links you put on PATH. Root's own records of site changes stay in
`/var/lib/boring-crocodile-site` until you delete them. A deploy never
uninstalls a site.

## When a site change stops part-way

Every command that changes the site (setup and `--add-pond` apply, site
activation, `--site-extend`, `--site-render`, `uninstall`, `pirate-add`, `pirate-remove`,
`task-policy --apply`) holds the one site lock,
`/etc/boring-crocodile/site.lock`; a second one refuses until the first has
finished. Recovery records differ by command: setup has receipts, while other
operations may leave changed files or completed steps. There is no single
resume command for all of them. Keep the error and any backup path it names;
inspect the state and fix the cause before following the matching row:

| Command | What to do |
|---|---|
| setup, `--add-pond` | Run it again with `--apply --resume`: its receipt records each account and folder it made. A stop while the home changes owner says so; inspect the home, then resume. |
| site activation | Run it again with `--apply --resume`. |
| `--site-extend` | Run it again. Service grants come first, so they are in place. If it stopped after `config.toml` names the new Pond (for example the controller restart failed), a re-run finds nothing to add: `systemctl restart boring-crocodile-mcp.service`, then `admin site-check`. |
| `--site-render` | Before it writes (a refused unit, a changed site): nothing changed, run it again. After: a re-run plans nothing, since the files already match; the error names the backup and the steps left (`systemd-tmpfiles --create ...`, `systemctl daemon-reload`, `systemctl try-restart ...`): run those, then `admin site-check`. The site log has the line already. |
| `pirate-add` | If `config.toml` does not name the Pirate yet, run it again: it adds the missing groups and restarts every worker it serves. If it does, run it again too: it restarts each worker that does not run with `pirate-clients` yet; after a first admission also `systemctl restart boring-crocodile-mcp.service`. Offering more Ponds changes no `config.toml`, so a re-run finishes it. |
| `pirate-remove` | If `config.toml` still names the Pirate, run it again. If not, restart the units the plan listed under `restart`. |
| `uninstall --pond` | With `--delete-data`, run it again: it reads the Pond from its `--add-pond` folder and removes what is left. Without, the Pond is parked once `config.toml` no longer names it; `systemctl start boring-crocodile-mcp.target` if the site is down. |
| `uninstall --site` | Without `--delete-data`, run it again: the receipt is marked already, and it removes the site files that are left (`config.toml` last), without site-check. With `--delete-data`, run it again: the first run wrote what it removes to `/etc/boring-crocodile/uninstall.json` before anything went, and a re-run finishes from it (the state folder with that file last). |
| `task-policy --apply` | It restores the previous drop-in itself; if that fails it says so and the worker stays stopped: inspect the drop-in, `systemctl daemon-reload`. |

When `site-check` reports a file that belongs to no Pond of `config.toml`
after such a stop, it is one the command had not removed yet: remove it by
hand, then run the command again.

## Administration of the host by a Pond

Optional and deliberate: [PERMISSIONS](PERMISSIONS.md).
