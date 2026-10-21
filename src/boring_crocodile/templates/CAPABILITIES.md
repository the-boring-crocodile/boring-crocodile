# What can I do here?

This is shared MCP guidance. The connected Pond's permissions and actual service
state determine what is available. Read its maintained welcome, agreement and
project context through `pond_bootstrap`; these remain owned by the inhabitant.
The separate `capabilities` summary reports this Pond's configured access and
loaded command limits. `pond_profile` identifies the bound Pond where enabled;
`pond_ping` confirms connectivity, not full health. The connection decides the
Pond: no tool takes an `environment` argument.

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

- `batch` orders up to 24 file, exec, job and `service_list` steps under one
  foreground deadline. It validates all step shapes and configured authority
  before effects. Steps run in order; `on_error=stop` or `continue` controls
  ordinary settled failures. A job receipt outlives the batch. A stopped batch
  reports prior results and unstarted steps. A dispatched effect can have an
  unknown outcome on timeout or disconnect. A batch ID is only a correlation
  marker, not a replay key. Keep each file expected hash explicit. A
  `job_wait` step's own bound is clamped further by whatever of the batch's
  shared deadline remains when it runs.
- `exec` runs a bounded foreground command when command execution is enabled.
  `pond_limits` reports the command task slots and host-derived time limits.
  The hosted client's deadline may be shorter. A result carries `cwd`,
  `exit_code`, `stdout`/`stderr` with `stdout_truncated`/`stderr_truncated`,
  `timed_out`, `duration_seconds` and `effective_timeout_seconds`. A returned
  result means the command's processes were cleaned up; unconfirmed cleanup is
  a failure instead. Truncated streams retain their head and tail around an
  omission marker; stdout_bytes/stderr_bytes count all drained bytes.
- `job_start` starts a managed job and returns its server-minted `job_id`:
  exactly one of a `command`, the same `operations` step list `batch` takes,
  or a `pirate` job (below).
  `timeout_seconds` defaults to 120 s for a command and 600 s for operations
  and is at most 86400 s (one day); `pond_limits` reports a lower host cap for
  command jobs. There is no unlimited job.
  `job_list` finds jobs; `job_status`, `job_output` and `job_stop` take the
  `job_id`. `job_output` returns the job's `result` (for a command: exit code
  and the bounded stdout/stderr output streams). `job_stop` stops the job; it
  ends `cancelled`.
  `job_wait(job_ids, wait_seconds)` waits until every listed job is in a
  terminal state, or `wait_seconds` (0..60, default 45; 0 means look once) runs
  out, and reports each job's state, not its `result`. `wait_expired: true` is
  not an error: call again rather than start the work again. An unknown id is
  reported with its `refusal` and counts as settled.
  For a command job, `completed` means it stopped running: exit 0, non-zero
  exit and timeout termination all end in `completed`. After `job_wait` or
  `job_status`, inspect `job_output.result.exit_code` and `result.timed_out`
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
- Pirates are agents running under their own Unix accounts. Each serves one or
  more flavours and registers them with this Pond itself. `pirate_list` shows
  every registered `pirate`/`flavour` with
  `available` (it answered the last heartbeat ping, every 3 s), `misses` and
  `last_miss`. `pirate_describe(pirate, flavour)` asks that flavour for its own
  description. Give it work as a job:
  `job_start(pirate={"pirate", "flavour", "assignment", "timeout_seconds"})`;
  the Pirate enforces the timeout, 0.1 to 86400 s like every job (null: its
  default, waited for at most 86400 s). The job holds one
  connection to the Pirate until it answers: its answer is the job's `result`;
  its refusal (for example `busy`, one job per flavour at a time) ends the job
  `refused`; a Pirate that drops the connection fails the job. `job_stop`
  closes the connection to request cancellation. With no cleanup acknowledgement,
  the local job ends `failed` with remote cleanup unconfirmed; inspect the Pirate
  before retrying. Pirate jobs need managed jobs (command execution with its task
  runner).

Tools and effective policies depend on the installed release. Distinguish
documented methods, configured permission, current reachability and verified
work. A Pirate flavour may be registered but unavailable. A completed job
means the execution owner finished, not that a claim or edit has been
independently checked.

## Refusals and failures

A refusal is a normal answer you can act on, not a breakdown. It arrives as
an error result with compact JSON text and a matching structured object:
`{"code": ..., "message": ..., ...details}`. Some clients prepend
`Error executing tool <name>: ` to the text; use the structured object when
available and the text otherwise. Our codes:

- `invalid_request`: the request itself is malformed or not acceptable; change
  it. Rejected arguments may be named in `fields` (names only, never values).
- `not_found`: the named file, directory, job, Pirate or service is not here.
- `forbidden`: this Pond's policy or connection does not allow it, or a
  service rejected the call token.
- `stale`: the file changed since you read it; `current_sha256` is its hash
  now (`null` when it is absent). Re-read before editing again.
- `busy`: a shared resource is held right now; try again shortly.
- `limit`: a size, count or capacity limit was reached.
- `not_admitted`: the work was not started; retrying is safe.
- `unavailable`: the Pirate or service is not listening or missed its last ping.

Arguments that do not match a tool's schema -- unknown, missing or of the
wrong type -- are rejected before anything runs, with a plain-text validation
message naming the argument (for example `idempotency_key: Extra inputs are
not permitted`). For an argument you expected to work, your tool list is
probably stale; refresh it.

A Pirate's or service's own refusal is passed through with its own code
unchanged (for example `busy`). A failure -- a bug, crash or
broken dependency, not an answer about your request -- arrives as JSON
`{"failure": "..."}` saying what failed and whether the call may have run. A
foreground `exec` (or batch `exec` step) that passed its request deadline
after it started is such a failure (`ForegroundInterrupted: ... command may
have started`): inspect before retrying. A refusal always means the work did
not run.
Only a bare `Error executing tool <name>` with no text is a crash inside the MCP
server itself. Inside a `batch`, a refused step has `status: "refused"` and a
`refusal` object of the same shape; `on_error=stop` stops on it as on a failed
step, with `stop_reason: "step_refused"`. In `job_wait`, an id that is refused
(for example an unknown job) is reported with its `refusal` and counts as
settled; the other jobs are still reported. A Pirate job its Pirate refused
ends `refused` with that `refusal` in its `result`.

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
refusal arrives with its own code (`tool_error` when it gave none,
`service_error` with `service_code` for a protocol error); a service that
rejects the call token (HTTP 401/403) is `forbidden`. Calls have a
30-second deadline (an earlier caller cancellation still applies). A timeout or dropped connection is a failure that may
have run: check before repeating a write. A service that is not registered
here is `not_found`; one that missed its last ping is `unavailable` until it
answers again. Only `service_list` is a batch step. These tools are separate
from Pirates.
