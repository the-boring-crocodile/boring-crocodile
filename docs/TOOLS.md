# The MCP tools: what they promise

The contracts behind the Pond's tools, for developers and administrators.
Agents read the shorter guide that `pond_bootstrap` serves
(`src/boring_crocodile/templates/CAPABILITIES.md`). Every tool works on the
connection's Pond only; none takes a Pond or environment argument. Services
and Pirates: [SERVICES_AND_PIRATES](SERVICES_AND_PIRATES.md).

Outcomes use `{status, code, message, ...details}`: refused means nothing
started or changed, failed means work started and may have had effects. Both
are MCP errors with compact JSON text and identical structuredContent.
The code names the cause independently of status; Common owns the vocabulary.
SDK argument validation and unexpected controller crashes keep SDK reporting.

## Context: `pond_bootstrap`

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

Bootstrap's `capabilities.command_limits` reports the task slots, foreground
budget and `job_timeout_seconds` (the host task lifetime less 10 s, at most 86400).

## Files: `file_list`, `file_search`, `file_read`, `file_patch`, `file_write`, `file_read_bytes`, `file_write_bytes`

Off unless the Pond's environment has a `files` section. Each root is an
absolute path with `read` and `write` (write needs read) and optional `deny`
subtrees (invisible); `files.limits` overrides the ceilings in
[`file_policy.py`](https://github.com/the-boring-crocodile/boring-pond-common/blob/main/packages/boring-worker-kit/src/boring_worker_kit/file_policy.py). Unix permissions still apply, and setup never widens them.

**Paths** are relative to a named root; `.` only for list and search. Refused:
`..`, `.`, empty components, `.git`, `.pirate-*`, `.boring-file-*`, absolute
paths, NUL; at most 1024 bytes, 255 per component. Names that are not UTF-8
are counted as skipped. Symlinks are not followed; hard links are followed for
reading (making one already needs read access as the same user), and a write
target must have exactly one. A refused non-regular target keeps `forbidden`
and adds `kind`: `directory` (use `file_list`), `symlink` (links are not
followed), or `other` (sockets, devices, FIFOs; multiple hard links when a
single link is required). The message names the specific cause.

**Reading.** `file_read` returns text in a line range with `next_line`, the
SHA-256 of the whole file on disk, its size, encoding, BOM and newline style
(shown as LF). Supported: UTF-8 with or without BOM, UTF-16 LE/BE with BOM; no
code-page guessing. Defaults: files up to 512 KiB, 96 KiB output, a slice up
to a sixth of that. `file_read_bytes(root, path, offset, length)` returns
base64, up to 64 KiB per call. `file_search` takes one literal, case-sensitive
line of text, decodes the same formats and skips binary, oversized and special
files; `truncated` means a missing match proves nothing. Listings are in
filesystem order, without a cursor.

**Images.** `file_read_image(root, path, max_px=1568, crop=null)` reads JPEG,
PNG and WebP under the same root/path policy. It uses a separate 25 MiB source
cap. Header limits use the actual source format/mode, not its extension:

| Source | Maximum pixels |
|---|---:|
| JPEG | 50 MP |
| RGB PNG without transparency | 40 MP |
| Other PNG modes, including transparency | 16 MP |
| WebP | 12 MP |

Progressive JPEG also has a 210 MiB coefficient/decoded-raster budget, checked
before decoding. Its effective cap depends on source sampling/mode and the
requested crop/detail; a refusal names that cap in MP. For example, at max_px
2048, a 50 MP RGB 4:2:0 JPEG is admitted whole, while a full-detail crop has a
31 MP cap; CMYK is capped at 26 MP whole (1/4 decode) or 18 MP at full detail.

The worker applies EXIF orientation,
then `crop: [x,y,w,h]` in oriented pixels, then scales the longest side to
`max_px` (64–2048), without upscaling. A crop must fit completely inside the
image. Animated PNG/WebP returns frame zero and reports the frame count.

The answer contains an MCP image and compact JSON text, with no structured
content or output schema. Metadata fields: `format, width, height,
alpha_flattened, colour, frames, source: {bytes, sha256, format, width, height}, crop`.
Formats are uppercase names; source sizes use oriented pixels. EXIF, GPS,
ICC profiles and text chunks are removed. After scaling, the worker converts
source ICC colours to sRGB with perceptual intent; alpha is kept separately.
CMYK and gray channels survive scaling; palette colours and bilevel pixels are
expanded before Lanczos because Pillow otherwise uses nearest-neighbour filtering.
16-bit gray is resampled in its source range, then mapped from 0–65535 to
0–255 before colour conversion. Cropping precedes full-size orientation copies;
JPEG uses the largest 1/2, 1/4 or 1/8 decoder reduction that keeps the requested
region's longer side at least max_px. Crop coordinates map into that reduced
scale; small full-detail crops decode at full scale. RGB regions resample
directly without a full-size crop copy. Scaling can differ from a full decoder
reference; oriented crop position, source metadata and colour interpretation stay the same.
`colour` is `converted` (including sRGB-to-sRGB), `untagged` (no profile; existing
mode conversion), or `profile_unusable` (broken/incompatible profile; image shown
without colour conversion). Alpha uses PNG; opaque pixels use
JPEG qualities 85, 75, 65, 50 until the answer fits 1 MiB. An oversized PNG is
flattened onto white and tried as JPEG; `alpha_flattened` records that.
Unsupported formats, corrupt files and exceeded limits return refused
outcomes (`unsupported`, `unreadable`, `limit`). `unreadable` covers every
Pillow read error, a Pillow bug included; other exceptions stay `worker_failed`. One image operation runs per
worker; a cancelled caller retains its slot until processing ends. Worker and
commands share the Pond's memory ceiling. The input bound was measured against
setup's proposed minimum of 512 MiB; concurrent commands still need headroom.
The tool is separate from batch. Pillow's [notices](third-party.md) ship in its wheel.

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
one cut off after admission is failed/foreground_interrupted with unknown effects.
Cleanup wins over the clock, and the client's own deadline is separate.
`capabilities.command_limits.exec_timeout_seconds` is the budget, not the remaining run time.

## Jobs and time limits

`job_start`, `job_list`, `job_output`, `job_wait`, `job_stop`:
for anything longer than a foreground call. `job_start` takes exactly one of:

- `command` (with `cwd`, `timeout_seconds` 0.1 to 86400, default 120, capped at
  the host task lifetime less 10 s);
- `operations`, the `batch` steps without `service_call`, with `on_error` (its own budget,
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
- `job_output(job_id)`: while running, `partial: true` and the latest output
  snapshot (memory only; absence proves nothing); when finished, the stored
  result with the same fields as `exec`.
- `job_wait(job_ids, wait_seconds?)`: 1 to 32 jobs, 0 to 60 s (default 45);
  wakes when they settle; 0 looks once. Each unknown or malformed ID carries
  its refusal. `wait_expired: true` is data, not an error.
- `job_stop(job_id)`: usually answers `stopping`; wait for `cancelled`. A
  Pirate job without a cleanup acknowledgement ends `failed`.

Records live in `.boring-crocodile/jobs.sqlite3` under the Pond's default
working directory, readable by the Pond's account (chats in one Pond see each
other's jobs). They hold results, not commands; finished records go after
seven days or beyond 1000 rows. A worker restart marks unfinished jobs
`interrupted`; it cannot reattach them.

If receipt storage cannot open at startup, bootstrap reports
`managed_jobs_enabled: false` and `managed_jobs_error`. Job calls refuse with
`unavailable`; other worker tools remain available. A batch containing a job
step refuses before any step runs. [Operations](OPERATIONS.md#updating)
describes preserving the store and restarting the worker after repair.

## Several steps at once: `batch`

`batch(operations, on_error="stop", timeout_seconds?)` runs 1 to 24 steps in
order in one request. Steps: the seven file tools, `exec`, `job_start` (a
command job), `job_list`, `job_output`, `job_stop`, `job_wait`,
`service_list`, `service_call` (service, tool, arguments, save_to: the control
plane mints its token before the batch runs; not in a job's operations). Not
steps: `service_describe`, nested batches, branching. A step's result does not
flow into the next by itself; a file does: `save_to` writes a service answer,
`{"$file": ...}` in a later service_call reads one (read a hash first, then
send the edit, for file steps).

The whole request is checked before anything runs. Steps share one budget of
at most 103 s (an `exec` gets what is left; a `job_wait` step at most what is
left). `stop` ends after a failed or refused step (`stop_reason:
"step_refused"` for a refusal); `continue` goes on. A nonzero exit or timeout
is a failed step; unconfirmed cleanup is `unknown` and always stops. A
`service_call` step that ran out of its own time is `unknown` (it may have
run) and follows `on_error`; one the service failed is `failed` with its
`failure` object; one whose answer was not saved (`save_to_refused`,
`save_to_failed`) is `failed` with that `code`, the answer kept in `result`.
Later
steps are `not_started`. Request and result are capped at 768 KiB; a result
over budget keeps every step's `id`, `method`, `status` and receipt (for a
service step: `saved_to`, `save_to_refused`, `save_to_failed`, `service_ran`)
with `result_truncated: true`; job steps retain `job_id` and `state`.
Space for every step's receipts is reserved before execution. Failure and
refusal payloads share the budget: oversized details are omitted, text may
be shortened, and `result_truncated: true` marks the step (`details_omitted`
marks its outcome). Save-problem receipts are bounded the same way. A service
answer larger than what is left is dropped then; save it with `save_to` instead.

There is no transaction: retrying a batch can repeat an `exec` or start a
second job. After a lost reply, check files and `job_list` first.

```json
{"operations": [
  {"id": "check", "method": "exec", "arguments": {"command": "git status --short", "cwd": "/home/crocodile-user/work/example"}},
  {"id": "later", "method": "job_start", "arguments": {"command": "./run-checks", "cwd": "/home/crocodile-user/work/example", "timeout_seconds": 600}}
], "on_error": "stop"}
```

New batch/job outcomes embed `refusal` or `failure` with the flat shape.
A step's `unknown` lifecycle status keeps a failed outcome describing the
uncertainty. Nonzero command exits retain their result fields. Retained records
from older releases, including `error` strings, are returned unchanged.
Worker connection loss is failed/worker_interrupted; invalid replies are
failed/bad_reply. A job_wait response deadline is refused/timeout and changes
nothing; ordinary wait expiry remains wait_expired data.

## Watching

`admin watch` shows file calls (method, path, counts; never contents, queries
or patch text), batch steps, commands and jobs: [ADMIN](ADMIN.md#watching-a-pond).
