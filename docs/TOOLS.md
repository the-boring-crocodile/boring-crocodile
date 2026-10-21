# The MCP tools: what they promise

The contracts behind the Pond's tools, for developers and administrators.
Agents read the shorter guide that `pond_bootstrap` serves
(`src/boring_crocodile/templates/CAPABILITIES.md`). Every tool works on the
connection's Pond only; none takes a Pond or environment argument. Services
and Pirates: [SERVICES_AND_PIRATES](SERVICES_AND_PIRATES.md).

Ordinary rejections are refusals, `{code, message, ...}`: `not_found`,
`forbidden`, `stale`, `invalid_request`, `limit`, `busy`, `unavailable`,
`not_admitted`. Unexpected errors are failures, `{failure}`. Both arrive as
tool errors ([ARCHITECTURE](ARCHITECTURE.md#outcomes)).

## Context: `pond_bootstrap`, `pond_profile`, `pond_ping`, `pond_limits`

`pond_bootstrap(project?)` returns the Pond's welcome, its handbook documents
in order with their hashes, the shared guide (`mcp_guide`, with version and
hash), the configured capabilities (file roots, limits) and the Pirates and
services the worker's registries know. It probes nothing.

The worker reads the files on every call, as the Pond's account, without a
command slot; the controller never sees the homes. Edits to the handbook need
no restart. Configuration, in `[ponds.NAME]`:

- `context_root` (absolute), `files` (ordered, relative to it), `welcome_file`;
- `[ponds.NAME.projects.PROJECT] files = [...]` for `pond_bootstrap(project=...)`.

Paths stay inside `context_root`; roots differ between Ponds; unknown
projects, missing files, traversal and symlink escapes fail visibly. Each
document is bounded by the worker frame and the whole answer by the 2 MiB
transport limit. One bootstrap is not a snapshot across files, and a chat
does not update what it already loaded. Context is shared memory, not
authority over the person.

`pond_profile` returns the profile ID and display name; `pond_ping` the Pond's
environment; `pond_limits` the task slots, the foreground budget and
`job_timeout_seconds` (the host task lifetime less 10 s, at most 86400).

## Files: `file_list`, `file_search`, `file_read`, `file_patch`, `file_write`, `file_read_bytes`, `file_write_bytes`

Off unless the Pond's environment has a `files` section. Each root is an
absolute path with `read` and `write` (write needs read) and optional `deny`
subtrees (invisible); `files.limits` overrides the ceilings in
`file_policy.py`. Unix permissions still apply, and setup never widens them.

**Paths** are relative to a named root; `.` only for list and search. Refused:
`..`, `.`, empty components, `.git`, `.pirate-*`, `.boring-file-*`, absolute
paths, NUL; at most 1024 bytes, 255 per component. Names that are not UTF-8
are counted as skipped. Symlinks are not followed; hard links are followed for
reading (making one already needs read access as the same user), and a write
target must have exactly one.

**Reading.** `file_read` returns text in a line range with `next_line`, the
SHA-256 of the whole file on disk, its size, encoding, BOM and newline style
(shown as LF). Supported: UTF-8 with or without BOM, UTF-16 LE/BE with BOM; no
code-page guessing. Defaults: files up to 512 KiB, 96 KiB output, a slice up
to a sixth of that. `file_read_bytes(root, path, offset, length)` returns
base64, up to 64 KiB per call. `file_search` takes one literal, case-sensitive
line of text, decodes the same formats and skips binary, oversized and special
files; `truncated` means a missing match proves nothing. Listings are in
filesystem order, without a cursor.

**Writing is hash-checked.** `file_write` needs `expected_sha256`: `null`
creates only if absent, a hash replaces only that version. `file_patch` takes
edits whose old text each occurs exactly once; all apply or none. A file that
changed, or anything that took its place, is refused `stale` with
`current_sha256`. `file_write_bytes` writes up to 64 KiB, whole or at an
offset (no gaps). Existing encoding, BOM and newlines are kept; mixed line
endings need an explicit `newline`. New files: UTF-8, LF, mode 0666 through
the umask or the directory's default ACL; replacements keep mode, group and
POSIX ACLs; privileged bits are refused. Every write goes to a temporary file
in the same directory, is fsynced, renamed atomically (new files with
`RENAME_NOREPLACE`) and the directory fsynced.

File-tool edits of one target are serialized by the worker (256 lock stripes);
`exec`, editors and other programs do not take that lock and can still race
after the final check. After a lost reply, read the file before choosing a new
hash. File calls leave no record.

## Commands: `exec`

`exec(command, cwd?, timeout_seconds?)` runs one shell command as the Pond's
account ([ARCHITECTURE](ARCHITECTURE.md#running-a-command)) and returns `cwd`,
`exit_code`, `stdout`, `stderr` with truncation flags, `timed_out`,
`duration_seconds` and `effective_timeout_seconds`. Commands are at most
64 KiB. Each stream keeps its beginning and its end within its budget
(default 64 KiB, at most 136533 bytes) with a marker between them;
`stdout_bytes` and `stderr_bytes` count everything.

**The foreground budget is 103 seconds** per request, less if the host allows
less, counted from before admission. Ten seconds (or half a shorter budget)
are kept for cleanup, so up to 93 seconds go to waiting, starting and running.
A request that never got a slot is refused `not_admitted` (nothing started);
one cut off after admission is a failure saying the command may have started.
Cleanup wins over the clock, and the client's own deadline is separate.
`pond_limits.exec_timeout_seconds` is the budget, not the remaining run time.

## Jobs and time limits

`job_start`, `job_list`, `job_status`, `job_output`, `job_wait`, `job_stop`:
for anything longer than a foreground call. `job_start` takes exactly one of:

- `command` (with `cwd`, `timeout_seconds` 0.1 to 86400, default 120, capped at
  the host task lifetime less 10 s);
- `operations`, the step list `batch` takes, with `on_error` (its own budget,
  default 600 s, at most 86400; each `exec` step still takes a slot);
- `pirate`, `{pirate, flavour, assignment, timeout_seconds}`
  ([SERVICES_AND_PIRATES](SERVICES_AND_PIRATES.md)).

It answers at once with a server-minted `job_id` (32 hex characters). Every
call starts a new job; if the answer is lost, look in `job_list` before
starting again. At most 32 unfinished jobs per Pond.

| States | |
|---|---|
| `queued`, `running`, `stopping` | not finished; `running` means a slot is held |
| `completed` | the command returned, with any exit code or timeout: read `exit_code` and `timed_out`; its processes are cleaned up |
| `refused` | did not run (`result.refusal`: not admitted, a Pirate's `busy`, ...) |
| `cancelled` | stopped by `job_stop` |
| `failed` | a worker-side error, unconfirmed cleanup, or a command that could not start |
| `interrupted` | the worker restarted before a result |

- `job_list(limit?)`: the latest 20 (up to 100), IDs, states and times only.
- `job_status(job_id)`: the record, small.
- `job_output(job_id)`: while running, `partial: true` and the latest output
  snapshot (memory only; absence proves nothing); when finished, the stored
  result with the same fields as `exec`.
- `job_wait(job_ids, wait_seconds?)`: 1 to 32 jobs, 0 to 60 s (default 45);
  wakes when they settle; `wait_expired: true` is data, not an error.
- `job_stop(job_id)`: usually answers `stopping`; wait for `cancelled`. A
  Pirate job without a cleanup acknowledgement ends `failed`.

Records live in `.boring-crocodile/jobs.sqlite3` under the Pond's default
working directory, readable by the Pond's account (chats in one Pond see each
other's jobs). They hold results, not commands; finished records go after
seven days or beyond 1000 rows. A worker restart marks unfinished jobs
`interrupted`; it cannot reattach them.

## Several steps at once: `batch`

`batch(operations, on_error="stop", timeout_seconds?)` runs 1 to 24 steps in
order in one request. Steps: the seven file tools, `exec`, `job_start` (a
command job), `job_list`, `job_status`, `job_output`, `job_stop`, `job_wait`,
`service_list`. Not steps: `service_describe` and `service_call` (each needs
its own minted token), nested batches, branching, or passing one step's result
into another (read the hash first, then send the edit).

The whole request is checked before anything runs. Steps share one budget of
at most 103 s (an `exec` gets what is left; a `job_wait` step at most what is
left). `stop` ends after a failed or refused step (`stop_reason:
"step_refused"` for a refusal); `continue` goes on. A nonzero exit or timeout
is a failed step; unconfirmed cleanup is `unknown` and always stops. Later
steps are `not_started`. Request and result are capped at 768 KiB; a result
over budget keeps every step's `id`, `method`, `status` and receipt with
`result_truncated: true`.

There is no transaction: retrying a batch can repeat an `exec` or start a
second job. After a lost reply, check files and `job_list` first.

```json
{"operations": [
  {"id": "check", "method": "exec", "arguments": {"command": "git status --short", "cwd": "/home/crocodile-user/work/example"}},
  {"id": "later", "method": "job_start", "arguments": {"command": "./run-checks", "cwd": "/home/crocodile-user/work/example", "timeout_seconds": 600}}
], "on_error": "stop"}
```

## Watching

`admin watch` shows file calls (method, path, counts; never contents, queries
or patch text), batch steps, commands and jobs: [ADMIN](ADMIN.md#watching-a-pond).
