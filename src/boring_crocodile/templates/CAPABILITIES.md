# What can I do here?

This is shared MCP guidance. The connected Pond's permissions and actual service
state determine what is available. Read its maintained welcome, agreement and
project context through `pond_bootstrap`; these remain owned by the inhabitant.
The separate `capabilities` summary reports this Pond's configured access and
loaded command limits. The connection decides the Pond: no tool takes an
`environment` argument.

## Working well

- On this shared host, read before editing and supply the returned file hash.
  A stale refusal protects someone else's intervening change; re-read it.
- Group known independent reads/checks in one batch. Use exec for tree-wide regex,
  Git and tests. Keep decisions that depend on a result sequential.
- Use jobs for long work and job_wait for waiting. Recover a retained receipt
  before retrying after a lost response; an uncertain outcome is not permission
  to repeat an effect.
- Review the diff and run the relevant checks before a coherent commit; gate
  the commit on those checks succeeding.
- Refusals are data: fix the named cause. Changing tools does not grant authority.

## Choose the right owner

- `batch` orders up to 24 file, exec, job, `service_list` and `service_call` steps under one
  foreground deadline. It validates all step shapes and configured authority
  before effects. Steps run in order; `on_error=stop` or `continue` controls
  ordinary settled failures. A job receipt outlives the batch. A stopped batch
  reports prior results and unstarted steps. A dispatched effect can have an
  unknown outcome on timeout or disconnect. A batch ID is only a correlation
  marker, not a replay key. Keep each file expected hash explicit. A
  `job_wait` step's own bound is clamped further by whatever of the batch's
  shared deadline remains when it runs.
- `exec` runs a bounded foreground command when command execution is enabled.
  Bootstrap's `capabilities.command_limits` reports the command task slots and
  host-derived time limits.
  The hosted client's deadline may be shorter. A result carries `cwd`,
  `exit_code`, `stdout`/`stderr` with `stdout_truncated`/`stderr_truncated`,
  `timed_out`, `duration_seconds` and `effective_timeout_seconds`. A returned
  result means the command's processes were cleaned up; unconfirmed cleanup is
  a failure instead. Truncated streams retain their head and tail around an
  omission marker; stdout_bytes/stderr_bytes count all drained bytes.
- `job_start` starts a managed job and returns its server-minted `job_id`:
  exactly one of a `command`, an `operations` step list (the `batch` steps
  without `service_call`), or a `pirate` job (below).
  `timeout_seconds` defaults to 120 s for a command and 600 s for operations
  and is at most 86400 s (one day); bootstrap command limits report a lower host cap for
  command jobs. There is no unlimited job.
  `job_list` finds jobs; `job_output` and `job_stop` take the
  `job_id`. `job_output` returns the job's `result` (for a command: exit code
  and the bounded stdout/stderr output streams). `job_stop` stops a command or
  operations job and ends it `cancelled`; a Pirate stop always ends
  `failed/cleanup_unconfirmed`.
  `job_wait(job_ids, wait_seconds)` waits until every listed job is in a
  terminal state, or `wait_seconds` (0..60, default 45; 0 means look once) runs
  out, and reports each job's state, not its `result`. `wait_expired: true` is
  not an error: call again rather than start the work again. An unknown id is
  reported with its `refusal` and counts as settled.
  For a command job, `completed` means it stopped running: exit 0, non-zero
  exit and timeout termination all end in `completed`. After `job_wait`,
  inspect `job_output.result.exit_code` and `result.timed_out`
  to determine its outcome. While a job is running, `job_output` returns
  `state: running, partial: true` and may include output so far; that snapshot
  is not the final result.
  Every `job_start` call starts a new job; if one fails without an answer, the
  job may or may not exist -- check `job_list` before starting it again.
  Commands require the enabled task runner; operations additionally require
  the worker's batch runner (always present when the task runner is).
- `file_read`, `file_write`, `file_patch`, `file_list`, `file_search`,
  `file_read_bytes` and `file_write_bytes` work
  within explicitly configured file roots under the inhabitant worker identity.
  Read returns a whole-file hash; create requires a null expected hash and
  replacement/patch requires the hash read before editing. List/search are
  bounded and report incomplete coverage. These tools can work when command
  execution is disabled. Configured roots are not a live availability check.
  Text reads normalize to LF and report disk encoding/BOM/newlines. Edits keep
  the format; mixed newlines require an explicit choice. New files use UTF-8/LF.
  Byte reads return ranged base64 plus the whole-file hash; byte writes replace
  or overwrite a range without gaps. Each chunk is at most 64 KiB, still within
  the configured whole-file and output limits. Both byte tools work in batch.
- `file_read_image(root, path, max_px=1568, crop=null)` shows JPEG, PNG or WebP
  under the same file-root policy. Source cap: 25 MiB; JPEG 50 MP, opaque RGB
  PNG 40 MP, other PNG 16 MP, WebP 12 MP. The actual format/mode decides, not
  the extension. Progressive JPEG can have a lower cap for its sampling/mode
  and crop/detail; a refusal names it. Lower max_px or pre-scale the source
  before retrying such a refusal. The worker applies
  EXIF orientation, crops `[x,y,w,h]` in oriented pixels, then scales the longest
  side to max_px (64–2048), never upscaling. Crops must fit fully inside the image.
  Animated PNG/WebP returns frame zero and reports `frames`. Metadata is removed.
  The answer is an image block plus compact metadata text: format, width, height,
  alpha_flattened, colour, frames, source (bytes, sha256, format, oriented width/height),
  and crop. Alpha uses PNG, otherwise JPEG; an oversized PNG is flattened onto
  white and marked alpha_flattened. Answer cap: 1 MiB. Unsupported/corrupt/oversized
  sources are refused as unsupported/unreadable/limit. Source ICC colours are
  converted to sRGB with perceptual intent after scaling and before the profile
  is removed. `colour` is converted, untagged (no profile), or profile_unusable
  (broken/incompatible profile; shown without colour conversion). Alpha is kept.
  One image operation runs
  per worker; an occupied slot can return busy. There is no image batch step.
- Pirates are agents running under their own Unix accounts. Each serves one or
  more flavours and registers them with this Pond itself. `pirate_list` shows
  every registered `pirate`/`flavour` with
  `available` (it answered the last heartbeat ping, every 3 s), `misses` and
  `last_miss`. `pirate_describe(pirate, flavour)` asks that flavour for its own
  description. Give it work as a job:
  `job_start(pirate={"pirate", "flavour", "assignment", "timeout_seconds"})`,
  optionally with `"overrides"` (`system`, `tools`, `services`, `max_turns`,
  `settings`) for a flavour whose description shows `allow_overrides`:
  `system` replaces its system text; `max_turns` may increase up to the Pirate's
  cap; `tools` and `services` only narrow its grants; `settings` are validated
  by the provider and bounded by the output-token cap. Other flavours refuse
  overrides. The Pirate
  enforces the timeout, 0.1 to 86400 s like every job (null: its
  default, waited for at most 86400 s). The job holds one
  connection to the Pirate until it answers: its answer is the job's `result`;
  its refusal (for example `busy`, one job per flavour at a time) ends the job
  `refused`; a Pirate that drops the connection fails the job. `job_stop`
  closes the connection to request cancellation. This protocol has no cleanup
  acknowledgement: the local job always ends `failed/cleanup_unconfirmed`; inspect the Pirate
  before retrying. Pirate jobs need managed jobs (command execution with its task
  runner).

Tools and effective policies depend on the installed release. Distinguish
documented methods, configured permission, current reachability and verified
work. A Pirate flavour may be registered but unavailable. A completed job
means the execution owner finished, not that a claim or edit has been
independently checked.

## Refusals and failures

Both are flat JSON outcomes with `status`, `code`, `message` and extra detail
fields. The code names the cause; status says whether it ran:

- `{"status":"refused","code":"busy","message":"all slots are in use"}`:
  nothing started or changed. Retry after the cause is gone.
- `{"status":"failed","code":"timeout","message":"worker did not answer","component":"worker","effects":"unknown"}`:
  work started and did not complete; it may have had effects. Check before retrying.

Tool errors carry compact JSON text and identical structuredContent. Batch and
job records embed the same object under `refusal` or `failure`. Batch step
statuses and job states remain lifecycle data.
A nonzero command exit is result data, not a transport failure.

The code names the cause independently of status; components document their additional codes.

- `invalid_request`: change the request; rejected `fields` name keys only, never values.
- `not_found`: the named thing is absent in this scope; check the name or list first.
- `forbidden`: policy denies the call, including a service rejecting its call token; check the grant.
  A non-regular file adds `kind`: directory (use file_list), symlink (links are
  not followed), or other (sockets/devices/FIFOs or disallowed multiple hard links).
  The message gives the specific cause and next step.
- `stale`: re-read before editing; `current_sha256` is the current hash, or null when the file is absent.
- `busy`: a shared resource is occupied; retry after it is released.
- `locked`: a lock prevents access; release it or wait for its owner.
- `limit`: reduce size/count or wait for capacity; the stated bound was reached.
- `not_admitted`: nothing was dispatched before the deadline; retry after admission is possible.
- `unavailable`: the component is not listening or missed its ping; check its availability.
- `timeout`: the deadline passed; use status/effects to decide whether to inspect before retrying.
- `unsupported`: choose a supported capability or format.
- `unreadable`: check the source file or encoding before retrying.
- `conflict`: reconcile the conflicting state before submitting again.
- `bad_reply`: the downstream reply violated its contract; inspect that component.
- `cleanup_unconfirmed`: inspect remaining work before retrying; cleanup has not been established.
- `upstream_error`: inspect the downstream error details.

Some clients prefix JSON text with `Error executing tool <name>: `; prefer
structuredContent when present. Worker failures include worker_failed,
worker_interrupted and foreground_interrupted. Service protocol and unstructured
tool errors use upstream_error, with service_code for protocol errors;
save write errors use write_failed.

A service outcome needs `status: refused|failed`, a valid lowercase cause code
and a string message. Its status, code and details pass through unchanged.
Unexpected controller exceptions and SDK argument-validation errors retain
the SDK's reporting; they are not guessed into an outcome by parsing text.

A refused result means the work did not run. A failed result or lost reply
can have effects; inspect files, job receipts or the relevant service.
Worker unreachable before sending is refused/unavailable; a job_wait response
deadline is refused/timeout because that wait changes nothing.
Within a batch, a refused step has status refused and a refusal object;
on_error stop stops on it with step_refused. Unknown IDs in job_wait carry
their refusal and count as settled. Pirate refusals end jobs refused.
Stopping a Pirate always ends failed with cleanup_unconfirmed; inspect the
Pirate before retrying.

Arguments outside a tool's schema are rejected before anything runs, with
validation text naming the argument. Refresh a stale tool list before retrying.
A bare SDK error without our outcome is a crash, not proof that nothing ran.

## Where were we?

After a lost chat, load maintained context and review recent jobs (Pirate jobs
included) if command access is enabled. Retrieve selected status, then the
`result` separately. Preserve job IDs in handoffs. If a `job_start` response was
lost, the outcome is unknown: check `job_list` before starting the job again.

`job_stop` is a request: wait until the job is terminal. A worker restart marks
unfinished jobs interrupted, and a Pirate restart fails its running jobs;
nothing replays them. Retention is bounded. Results can include sensitive
task output, so retrieve only what the work needs. A receipt cannot recover an
unrecorded conversation plan or prove that an external side effect occurred
exactly once. Verify consequential outcomes using the relevant service.

This guide is packaged with MCP and is not part of any inhabitant's handbook.
It grants no operating-system authority. Root-owned configuration and loaded
service policy own the effective boundaries.

File edits serialize with other cooperating file-tool edits. Ordinary exec, external editors and administrators may race; after a lost response, read the file and compare its hash before retrying. A file root must also be writable under systemd/Unix policy.

The admin `watch` command can observe future file operations and batch progress
with bounded metadata. It is best effort: missing events never prove an effect
failed. A lost batch response can require inspecting file hashes and job receipts;
retrying the whole batch may repeat an exec command.

## Services

Services are MCP servers the administrator granted to this Pond (a canvas, for
example). `service_list` shows those registered with this Pond, with
`available`, `misses` and `last_miss` from the last ping. `service_describe`
lists a service's tools with their input schemas. `service_call(service, tool,
arguments)` calls one tool; the service validates the arguments. A service's
valid outcome keeps its status and code. An invalid or unstructured tool error
is `failed/upstream_error`; a protocol error uses the same cause and keeps
`service_code`. A service that rejects the call token (HTTP 401/403) is `forbidden`. A service gets at most
30 s to answer, less when the request has less time left (inside a batch: what
remains of it, minus 5 s). With no time left the call is refused `not_admitted` and
nothing is sent. A timeout or dropped connection is a failure that may have
run: check before repeating a write. A service that is not registered here is
`not_found`; one that missed its last ping is `unavailable` until it answers
again. `service_list` and `service_call` are batch steps (a service step's
token is minted before the batch runs; not in a job's operations), so a batch
can render a file with `exec`, push it with `$file`, save the answer with
`save_to` and check that file, in one request. These tools are separate from
Pirates.

Content you only move between a file and a service need not pass through you.
Any argument value may be `{"$file": {"root": ..., "path": ..., "sha256": ...}}`
(sha256 optional). The worker replaces it with that file's exact text (UTF-8,
line ends kept, a leading BOM dropped), under the same root rules as
`file_read`. At most 16 references and 512 KiB in all per call (`limit`). An
object whose only key is `$file` is always a reference; a malformed one is
refused. `arguments` itself must be an object, not a reference.

`save_to: {"root", "path", "expected_sha256"}` (null for a new file) writes
the service's answer to that file: its text when it is one text item,
otherwise the answer as JSON. You get `saved_to` (root, path, hash, bytes)
instead of the answer. A target that does not match, a root you may not write
or a missing directory is refused before the call. The service's time then
ends 5 s before the request's, to leave time for the write. If the save does
not succeed after the service ran, the answer comes back with one of:

- `save_to_refused`: the write was refused (with its code); nothing was written.
- `save_to_failed` (`effects: unknown`): the write failed or did not finish in
  time; the file may still be written. Compare its hash before writing again.

In a batch such a step is `failed` with `code` (`save_to_refused` or
`save_to_failed`), the answer still in its `result`; `stop` ends the batch
there, so a later step never relies on a file that was not saved.

An answer over 1 MiB then comes back only as its size, with `service_ran`;
inside a batch, an answer larger than the batch result has room for is
dropped, keeping `service_ran` and the save outcome.
Read the saved file only if you need the content.
