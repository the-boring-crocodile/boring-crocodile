# Operating a site

Installing a new site: [INSTALL](INSTALL.md). Administrative commands:
[ADMIN](ADMIN.md). Runtime deployment:
[boring-deploy](https://github.com/the-boring-crocodile/boring-pond-common/blob/main/packages/boring-deploy/README.md).

## The description and the render

Two kinds of files describe the site:

| Path | What it owns |
|---|---|
| `/etc/boring-crocodile/config.toml` | Site state, runtime path, controller identity, listener and Pirate admission |
| `/etc/boring-crocodile/ponds/NAME/config.toml` | Pond enablement, Unix identity binding, UUID, context, file roots, execution settings, explicit ceilings and credential path |
| `/etc/boring-crocodile/credentials/` | Approval secrets, private root files; contents are not configuration |
| `/var/lib/boring-crocodile/oauth.sqlite3` | Sign-ins, grants and service policy ([OAUTH](OAUTH.md)) |
| `DEFAULT-CWD/.boring-crocodile/jobs.sqlite3` | The Pond's job records |

The main file's `[site].state` is `running`, `stopped` or `removed`.
A per-Pond `enabled = false` parks that Pond. Numeric UID/GID bindings,
UUIDs, ceilings and paths are explicit; render refuses changed or missing
identities rather than adopting another account with the same name.

Render derives the root-owned base units, task policy and tmpfiles from this
description. It reconciles the declared Unix access groups and runs the
systemd tail. It restores ordinary owner/group modes on the description:

| Path below `/etc/boring-crocodile` | Owner:group | Mode |
|---|---|---|
| the directory | root:root | 0711 |
| `config.toml` | root:root | 0644 |
| `ponds/` | root:controller-group | 0751 |
| `ponds/NAME/` | root:Pond-group | 0750 |
| `ponds/NAME/config.toml` | root:Pond-group | 0640 |
| `credentials/`, secret files | root:root | 0700, 0600 |
| `site.lock` | root:root | 0600 |

Workers/task runners read the main file and their own declaration. The controller
unit names its enabled Ponds and supplies their supplementary groups; it opens
exactly those files. Root administration reads parked/sealed declarations.
Render removes stale access/default ACLs from managed description paths without
an ACL-tool dependency. Old private receipts and unrelated entries are untouched;
unsafe siblings require review. `site-check` reports owner/group/mode or ACL drift.
It never creates accounts, replaces secrets or deletes data.
Explicit setup provisions accounts, UUIDs and missing secrets; explicit
`uninstall --delete-data` retires them.

All planned changes preview without effects. With `--apply`, review the fresh
plan and type `APPLY` at the terminal. The shared
`/etc/boring-crocodile/site.lock` excludes concurrent site changes; a changed
description, host fact or policy makes the reviewed plan stale. No digest
needs to be copied and there is no `--resume`, receipt or activation record.

## Rendering and repairing

Finish or stop active work before an apply: restarting workers interrupts
their running jobs and client connections. Interrupted work is never replayed.

    sudo boring-crocodile-setup --site-render
    sudo boring-crocodile-setup --site-render --apply

The plan includes configuration and generated-file diffs, group changes,
units to stop and the systemd tail. Even with no file differences, every
render reloads systemd. For a running site it starts the wanted slices and
task sockets, restarts enabled workers, starts the listener/target and
restarts the controller. It then waits for local readiness for up to a minute.
The controller and enabled workers must remain active with unchanged positive
main PIDs for at least ten seconds; inactivity or a restarted process resets
the interval. A passing observation between restarts is insufficient.
Rerunning therefore repairs a failed tail even when the saved files match.

The root TCP listener stays open for ordinary renders and controller
restarts. It stops when its unit/listen address or an existing slice's
protected ports change, and when the desired site is stopped or removed.
Those transitions stop the affected work before the change. Review the stop
list and allow an interruption. Render does not perform a complete site
stop/start for every change.

Pirate reception folders/sockets are prepared before workers start.
Admission and worker client groups are reconciled before startup; service
grants requested by `--site-extend` are saved before enabling the Pond.
Workers start before the controller's final restart. Local readiness does
not prove external registration: Pirates and services reconnect
asynchronously. Check them from the intended Pond and test the client path.

A stopped site keeps its generated files and starts nothing. A removed site
has no generated integration. Neither state deletes its configuration,
accounts, credentials, homes or database.

### External drop-ins

Render accepts every root-owned external drop-in and shows its complete
contents verbatim in the reviewed plan. Put secrets in files referenced by
`LoadCredential=` and keep their contents out of drop-ins. Settings also
present in the generated unit
carry warnings; systemd owns precedence and syntax. Render never rewrites or
deletes these files. Symlinks, masks and unsafe ownership or modes are refused;
there is no directive allow-list.

Task authority is declared by the Pond file's `[execution].no_new_privileges`
and generated base task unit; see [PERMISSIONS](PERMISSIONS.md).
Site-check and status use the same effective task-policy comparison. A loaded
mismatch is irregular/degraded, including a drop-in's `NoNewPrivileges=no`
over a restricted declaration. A warning in the render plan does not itself
prove what systemd loaded.

Base units are generated: an approved render replaces edits to them.
Keep a separate backup before deliberate edits; render creates no backup
journal. Inspect the fresh plan rather than inferring success from file
equality.

## Updating

The runtime lives under `/opt/boring-crocodile`: `releases/`, `current`,
`previous` and `DEPLOYED`. Site files and homes are independent of it.
From a trusted unpacked release (download:
[INSTALL](INSTALL.md#1-deploy-the-runtime)):

    sudo boring-crocodile-VERSION/deploy --check
    sudo boring-crocodile-VERSION/deploy

`--check` verifies without writing. Deploy never opens Pond-owned receipt
databases as root. Each worker opens its own store and accepts only schema 1
with the current receipt columns; it creates an absent store itself.
An unsupported or unreadable store disables only that Pond's managed jobs.
Bootstrap reports `managed_jobs_error`, job calls refuse with `unavailable`,
and the worker logs the cause. Files, foreground commands and services stay
available; the bad store cannot veto the site's runtime update.

To restore jobs, stop that worker, preserve a consistent private archive,
then explicitly repair or move the store and restart the worker. Include any
WAL or rollback journal in the recovery; do not discard pending writes.
The worker never converts an old receipt schema or silently retries storage
initialization.

Deploy validates the candidate against
the configuration, holds the site lock, switches the runtime and restarts
running components. It can wait up to 90 seconds for controller connections
to close. Clients reconnect; interrupted commands do not replay. A stopped
site remains stopped. Before a schema change, deploy backs up the OAuth
store; normal smoke compares local status before and after. If the installed
reader cannot parse the live description, deploy logs its reason and records
the before-state as unknown. Smoke then requires every new-release status
check to pass, including a running controller. Other status failures still
stop the deploy. A stopped installation is checked when it next starts.

Deploy does not render host policy. Review and apply `--site-render` after
the runtime update when generated files change. A render is also the repair
operation if startup failed after the switch.

### Adopting a pre-description site

The importer handles the standard published 0.4.21 single-file description.
Normal deploy refuses that format. The family's
[update guide](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/updating.md#choose-the-configuration-path-first)
owns the component order and the decision between normal update and adoption.
Use one maintenance window; keep Pirate work stopped until the compatible pair
is checked. The description importer leaves the OAuth store to its owner:
normal controller startup [upgrades schema 5 to 6](OAUTH.md#schema-and-backup)
transactionally. Existing callers become readers until the reviewed service
grant commands restore any intended writers.

Keep the old artifact, its hash and a private matching snapshot before
conversion. The importer preserves user identities, UUIDs, credentials,
explicit limits and OAuth state for the normal startup upgrade. It rejects unsupported
layouts. It plans a missing `pond-pirates` group and creates it only on
stopped APPLY; existing groups are retained.

1. **Prove the candidate.** From the verified, root-protected artifact, run
   `adopt-site.py --dry-run` with the installed runtime's Python 3.12+.
   Require every account and `deploy_check` to report ok, and cleanup done.
   This stages a private temporary description and offline runtime under
   `/opt`, loads it as the actual controller and worker identities, and
   runs the packaged check hook. It changes no live description, group,
   store, runtime link or unit activity.
2. **Finish work and stop consumers.** Record the actual loaded units and
   which are active, including task instances, workers, controller, target
   and sockets, plus Pirate services, task sockets and task instances. Stop and
   verify them. Published 0.4.21 has no
   `boring-crocodile-mcp.socket`: do not put a confirmed absent unit into
   an all-or-nothing stop command.
3. **Take the stopped snapshot.** Preserve the complete description and
   credentials, generated units/drop-ins, slices, tmpfiles and enablement
   links, OAuth state and affected private receipt directories. Preserve
   numeric ownership, modes, ACLs and xattrs. Record each account's UID/GID
   and supplementary groups, the active-unit list, paths and existing
   shared role groups. Keep this root-only. Copying a live SQLite file is
   not a consistent backup.
4. **Convert while stopped.** Run the artifact's `adopt-site.py --apply`
   with that Python, review the plan and type APPLY. It publishes the typed
   description with state stopped. The old runtime cannot read those files;
   do not start it. The controller's normal startup transaction performs
   the store upgrade; no separate converter or schema-number edit is needed.
5. **Preflight and deploy the candidate.** Run its `deploy --check`.
   Deploy after refusals are resolved. A stopped site remains stopped. Receipt
   stores belong to the workers; deploy never opens them as root. An unreadable
   or unsupported store disables that Pond's managed jobs and reports the cause
   after startup, while its other tools remain available. Preserve the old store
   before an explicit repair or replacement; do not edit its schema number.
6. **Render and verify.** Set `site.state` to running in the root-owned
   main description, preview and APPLY the common render. Require regular
   site-check, healthy status and a command through each enabled Pond.
   After adoption, run the pirate-add commands printed by the Pirates check,
   confirm their plans with --apply, then remove legacy client_uids and retry.
   After adoption, run each service-grants preview printed by the Services
   check. Review the complete caller map, run the printed `--apply` command
   and type `APPLY`. Decide any custom author aliases before resuming
   services. Verify an intended reader's read and an intended writer's
   harmless write.
   Complete the matching Pirate update before releasing Pirate work. The
   Crocodile checks Pirate versions at registration. Check a fresh client and
   Pirate registration before releasing the window.

A failed step stops the operation. Inspect saved state before retrying.
For a declared site, the common render is the repair operation; it does not
reverse adoption.

#### Way back across adoption

Restore the matching legacy files as well as the old runtime. The following
sequence was exercised with a two-Pond 0.4.21 site:

1. Stop all actual candidate consumers and tasks. Preserve the candidate
   description, state and receipt directories privately before replacing
   them. Keep both snapshots.
2. Identify generated files and enablement links absent from the original
   snapshot. Move only those candidate additions aside; in particular, the
   new root-held MCP socket must not remain in the old deployment. Preserve
   unrelated administrator files.
3. With all consumers stopped, move the converted description out of its
   live path. Run the retained old artifact's deploy with no site config
   present; this installs its runtime without asking the old reader to
   parse converted files. Verify the old runtime is selected before restore.
4. Restore the consistent legacy description, credentials, state, generated
   integration and private receipts, including numeric owners, modes, ACLs
   and xattrs. Restore the recorded supplementary memberships: the old
   controller relied on NSS membership, while the new unit uses explicit
   SupplementaryGroups. Keep the same UID/GID bindings.
5. If adoption created a previously absent shared group, remove it only
   after confirming it has no supplementary or primary users. Never delete
   a pre-existing shared group. Compare restored files with the snapshot
   before starting services.
6. Daemon-reload and start the saved old active-unit set, without a socket
   that did not exist there. If systemd explicitly reports a start-limit
   failure during this recovery, reset those failed units before retrying.
   Check the old version, site-check and a command through each restored
   Pond, then its clients and compatible Pirates.

Restoring OAuth state can restore old grants too; inspect the restored
authority before opening the site to work. This is a maintenance operation,
not an automatic response to every deployment refusal.

### Rolling back a runtime

Use the previous release's deploy:

    sudo /opt/boring-crocodile/previous/deploy

Check data and configuration compatibility first. A code rollback does not
undo work or restore the OAuth database. In particular, rollback across
adoption requires the saved legacy description while all consumers are
stopped. If systemd's start limit blocks a reviewed recovery, reset failed
Crocodile units before retrying.

## Changing configuration

For a persistent stop, set `state = "stopped"` in the main file's `[site]`
table, then preview/apply render. Set it to `"running"` and render to start
again. Enabled Ponds retain their limits and settings. An emergency
`systemctl stop boring-crocodile-mcp.target` changes activity only: the next
render follows the saved desired state.

Edit the appropriate description, then preview/apply render. Runtime
processes read configuration at startup; handbook files are read on each
bootstrap. The sign-in name is `display_name` under `[pond]` in that
Pond's file. It also names the worker in runtime results. Preserve its
`profile_id` and identity binding; there is no second execution display name.

The identity's `home` supplies these defaults. Omit a key when using its default:

| Key | Derived value |
|---|---|
| `pond.context_root` | `<home>/handbook` |
| `execution.default_cwd` | `<home>` |
| `execution.allowed_roots` | `[<home>]` |
| `execution.files.roots.home.path` | `<home>` (fixed; never written) |

Custom context, working-directory and allowed-root overrides remain explicit.
The home file root still declares its read/write rights and optional deny list;
other named file roots declare their own paths. File limits stay explicit.
An absent file policy still disables file tools.

The reader rejects repeated default keys and `execution.display_name`, naming
the exact key to remove. For a published 0.4.21 single-file site, use the
[one-time adoption procedure](#adopting-a-pre-description-site); it preserves custom
paths and policy, removes equal defaults and rejects conflicting display names
or a home file-root alias that points elsewhere.

`--site-config FILE` and `--site-wizard` configure the public listener and
initial Pond policy and select running state. Use direct description edits
and render when preserving an existing site's individual Pond policies.
Every enabled Pond has Pirate reception. Provisioning creates the shared
registration group and each private registration directory; workers bind their
sockets on start. Admission remains explicit through `pirate-add`.

`--site-extend --bring-back NAME` enables only the named Ponds, optionally
changing their limits or granting existing SQLite services first. Without
`--bring-back`, no Pond is selected for activation, limits or grants. The same
explicit selection can repair an interrupted extension after its declarations
were saved. Saved ceilings stay as declared unless an explicit flag changes them.
Pirate admission uses the same render; service policy commands use the same
plan/APPLY and lock, but write SQLite without rendering
or restarting the site ([ADMIN](ADMIN.md)).

## Backup and restore

Back up each Pond's home, the complete `/etc/boring-crocodile` description
and secrets, the OAuth store, and each service/Pirate's own state.
Use `boring-crocodile-admin oauth backup --output FILE` for a consistent
live database copy in a private root-owned directory. It contains bearer
tokens. Preserve numeric account/group bindings with the host backup.

To restore on the same host, stop every Crocodile consumer, retain the
current files for recovery, restore the matching description/secrets/homes
and database, verify ownership and identities, then render the desired
state. The store belongs to the controller, mode 0600. Run
`admin site-check`, `admin status --check` and a client journey.
A render cannot reconstruct missing accounts or data. Moving to a different
host is outside this standard restore path.

Restoring an older OAuth store can revive revoked grants or lose newer
ones. A code rollback alone is no reason to restore it.

## Replacing an approval secret

Replacement is explicit. The first Pond's default file is
`/etc/boring-crocodile/credentials/approval-secret`; others use
`NAME-approval-secret`. The declared credential path is authoritative.
Replacing it prevents new approvals with the old value, but does not
invalidate issued tokens. For the first Pond:

    sudo sh -c 'umask 077; cd /etc/boring-crocodile/credentials && python3 -c "import secrets; print(secrets.token_urlsafe(32))" > approval-secret.new && mv approval-secret.new approval-secret'
    sudo boring-crocodile-setup --site-render --apply
    sudo boring-crocodile-admin oauth revoke --pond pond --all-clients
    sudo boring-crocodile-admin oauth revoke --pond pond --all-clients --apply

Keep the value in a private administrator terminal. Never put it in a URL,
chat, command history or log.

## Uninstalling

    sudo boring-crocodile-admin uninstall --pond NAME
The family task page [Park or retire a Pond](https://github.com/the-boring-crocodile/boring-pond/blob/main/docs/retire-pond.md)
owns the command sequence, checks and return path.

`uninstall --pond NAME` selects an added Pond; `--site` selects the site
and its initial Pond. Without `--delete-data`, the scope is parked:
accounts, secrets, UUIDs, grants, descriptions and homes remain. With it,
the separate destructive plan retires admission and access after deactivation.
Retained paths are sealed; accounts are locked and expired, with their UID/GID
reserved because a Pirate may still trust those numbers. A selected added
declaration is removed last.

Home contents are never recursively deleted. Whole-site retirement leaves
removed-state descriptions for explicit archival, removes the OAuth store
and reserves the controller's identity too. The family task page above gives
the separate operator steps for releasing identities once no Pirate trusts them.
Identity changes, unsafe paths/mounts, unsupported stores and external drop-ins
referring to retiring groups refuse. Inspect
the error and use the same approved scope on recovery; runtime files and
PATH links are removed separately by the human if wanted.

## Crocodile and Pirates compatibility

Install the matching Crocodile and Pirates set together; keep Pirate work
stopped until both updates are complete. Wire-format changes bump the minor
version. The Pirate release declares the Crocodile minor line it requires.
At registration the Crocodile checks the Pirate's installed version against
its declared range and names any refusal. The Crocodile admits only the
Pirate/Pond pairs in its description; the Pirate trusts only accepted live
worker sockets backed by the controller's proof. Crocodile-only installs
remain supported. Render and Crocodile deploy do not inspect a Pirates release
or the rendering interpreter's release metadata.

## When a change stops part-way

Keep the error, inspect what was saved and fix its stated cause. There is no
resume flag, step ledger or automatic rollback.

At startup the controller serves all enabled Ponds or none. A broken main
description or enabled Pond file stops startup. The controller journal,
`status` and `site-check` name the broken file and include the line and
column for TOML syntax errors. Correct the file by hand or restore it from a
backup, then restart `boring-crocodile-mcp.service`. Render validates the
description before deriving units and refuses a broken file.

| Operation | Recovery |
|---|---|
| Provisioning | Rerun the same inputs. Published declarations retain UUIDs and numeric bindings; existing secrets and handbook files stay. An unexpected identity/path requires review. |
| Render, activation, extension or Pirate admission | Inspect the saved desired files, then rerun `--site-render --apply`. Its tail runs even when files match. If the command stopped before saving its requested candidate, rerun that command to propose it again. |
| Extension with service grants | Grants already saved remain; inspect `service-list`. Rerun extension if the declaration is still parked; otherwise render repairs startup. |
| Service policy | Read `service-list`, then replan the intended mutation. Never repeat `service-add` just to recover a lost one-time secret; inspect whether registration completed. |
| Explicit retirement | Rerun the same `uninstall --delete-data` selection. Kept declarations bind cleanup to the reviewed identities until it finishes. |

Render reports local readiness separately from external registration.
An error after publication can leave the desired configuration saved with
incomplete runtime effects; a matching file is not proof that a unit started.

## Logs and host administration

Use journald:
`journalctl -u boring-crocodile-mcp -u 'boring-crocodile-worker-*'`.
Secrets, environments and command output are not logged. Root's
`admin watch` is a best-effort live debugging view, not an audit log.
Optional host administration by a Pond: [PERMISSIONS](PERMISSIONS.md).
