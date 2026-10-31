# Changelog

Notable changes per release, newest first ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
[SemVer](https://semver.org/); tags `vX.Y.Z`, candidates `vX.Y.Z-rc.N`).

## 0.4.20 — 2026-10-31

Everything since 0.4.15. Built on Pond Common 0.7.11.

New and changed:

- `service_call` reads files and writes answers for you: an argument value
  `{"$file": {"root", "path", "sha256"?}}` becomes that file's text (at most 16 references and
  512 KiB per call); `save_to` writes the answer to a file and returns `saved_to`. A stale or
  unwritable target is refused before the call; a save that fails afterwards comes back with
  `save_to_refused` (nothing written) or `save_to_failed` (the file may be written).
- `service_call` is a `batch` step. Grants are checked and tokens minted before the batch runs.
  A step whose service answered but whose save failed is `failed` with its `code`, so
  `on_error: stop` stops there. Batch admission checks file-reference shapes and configured
  read/write scopes before any earlier step acts.
- `job_start(pirate=...)` takes optional `overrides` (`system`, `tools`, `services`,
  `max_turns`, `settings`), passed to a Pirate flavour that allows them.
- Time limits are one budget per request: a worker answers within the time the request has left;
  a service gets at most 30 s. A Pirate job waits 15 s past its timeout for the Pirate's answer.
- `boring-crocodile-setup --site-render` writes the site's units and tmpfiles as the release
  renders them (plan with a diff and a digest, then apply with backup, reload, restart and
  site-check); `--guests POND` lets a running Pond accept Pirates. A running controller that fails
  during the restart is reported as degraded.
- OAuth: public and Basic-auth clients can revoke tokens without a `client_secret` field.
- Execution settings follow worker-kit's rules: `default_cwd` must lie inside an allowed root,
  the task unit prefix must be `<product>-task-<environment>`, an `output_limit_bytes` above the
  frame limit is refused instead of clamped. Without explicit values, the unit prefix and budget
  slice follow the environment's name.
- The receipt store is part of the Crocodile (`boring_crocodile.task_state`); same schema, no data
  migration.

Removed:

- The standalone `boring-crocodile-render-services` command (setup, extend and render keep the
  renderer).
- Named messages for keys of earlier config layouts (`[context]`, `[execution]`,
  `[auth] approval_secret_file`, `[environments.*.pirates.*]`, the old limit fields): the generic
  unknown-key rule refuses them.

Upgrade: the controller and its workers must run the same release; a deploy restarts them
together. Check `output_limit_bytes` and any explicit `task_unit_prefix` against the rules above
before deploying.

Fixed:

- Python 3.10: a `service_call` step that runs out of time in a `batch` is `unknown`
  (it may have run), as on newer Pythons, not `failed`.

## 0.4.15 — 2026-10-21

First public release of the Crocodile, an MCP server that gives AI clients a Pond, a Unix account with files, commands, jobs and services, behind their own sign-in.
