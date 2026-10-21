"""Durable job ownership built on the shared receipt store.

One `start()`, one `_run()`, one MAX_UNFINISHED
pool -- for a job started from a single command, from several ordered
steps or from a Pirate assignment alike. The only branch is how the payload
actually runs: a plain command still goes straight through
EnvironmentExecutor (preserving its on_admitted-gated running/queued
transition and streaming on_output), an operations list runs through the same
BatchRunner a direct `batch` call uses, and a Pirate assignment is one held
`run` connection to the Pirate. That is a difference in what is being run, not
a duplicated lifecycle.

The store mints every job_id; callers never choose one. A `job_start` whose
answer was lost has an unknown outcome -- the caller checks `job_list` before
starting again. Nothing here makes repeating a start request safe.
"""
from __future__ import annotations

import asyncio
import math
import os
import re
from typing import TYPE_CHECKING

from boring_task_state import TERMINAL_STATES, UNFINISHED_STATES, ReceiptStore, decode_result

from boring_worker_kit.execution import EnvironmentExecutor, prepare_command
from boring_worker_kit.inputs import MAX_JOB_SECONDS
from .settings import EnvironmentSettings
from boring_worker_kit.observation import Observation
from .outcomes import Refusal

if TYPE_CHECKING:
    from .batch import BatchRunner
    from .pirates import Pirates

RETENTION_SECONDS = 7 * 86400
MAX_UNFINISHED = 32
MAX_STORED = 1000
# A job's own default runtime cap when the caller omits timeout_seconds -- a
# starting point, not a coupling: nothing here reads any Pirate's actual
# configured max_timeout_seconds, so the two can move independently.
DEFAULT_JOB_SECONDS = 600.0
JOB_ID = re.compile(r"[a-f0-9]{32}\Z")
MAX_WAIT_JOBS = 32


class ManagedJobs:
    """Own job scheduling/cancellation; ReceiptStore owns persistence only."""

    def __init__(self, settings: EnvironmentSettings, executor: EnvironmentExecutor,
                batch: "BatchRunner | None" = None, pirates: "Pirates | None" = None):
        if settings.task_socket_path is None:
            raise ValueError("managed jobs require systemd task containment")
        self.settings = settings
        self.executor = executor
        # Only a job started from `operations` needs this; tests and callers
        # that only ever start plain command jobs can omit it.
        self.batch = batch
        self.pirates = pirates
        root = settings.default_cwd / ".boring-crocodile"
        root.mkdir(mode=0o700, exist_ok=True)
        if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
            raise PermissionError("job state directory must be private to the environment")
        self.store = ReceiptStore(root / "jobs.sqlite3", "jobs")
        self.store.mark_unfinished()
        self.running: dict[str, asyncio.Task] = {}
        self.observers: dict[str, Observation] = {}
        self.partial_output: dict[str, dict[str, object]] = {}

    def _row(self, job_id: str) -> tuple:
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise Refusal("invalid_request", "job_id must be the 32-character id job_start returned")
        row = self.store.get(job_id)
        if row is None:
            raise Refusal("not_found", "unknown job")
        return row

    def _public(self, row: tuple, include_output: bool = False) -> dict:
        job_id, state, created, updated = row[:4]
        answer = dict(job_id=job_id, state=state, created_at=created, updated_at=updated)
        result = decode_result(row)
        if include_output and result is not None:
            answer["result"] = result
        return answer

    def _prune(self) -> None:
        self.store.prune(RETENTION_SECONDS, MAX_STORED)

    def start(self, command: str | None, cwd: str | None, timeout: float | None,
              *, operations: list[dict[str, object]] | None = None,
              on_error: str = "stop", pirate: dict[str, object] | None = None) -> dict:
        """Start a job: exactly one of `command`, `operations` or `pirate`.

        `command` is a single command (`cwd` relative to the environment
        root, `timeout` in seconds, default 120, at most MAX_JOB_SECONDS or
        the host task service's lifetime cap). `operations` is the same step
        list a `batch` call takes; `timeout` is then this job's own overall
        budget (default 600, at most MAX_JOB_SECONDS). `pirate` is
        {pirate, flavour, assignment, timeout_seconds}: one assignment for a
        registered, available Pirate flavour; the Pirate enforces its
        timeout (0.1 to MAX_JOB_SECONDS, null: the Pirate's default).

        Every call that is not refused creates a new job; the returned
        status carries the server-minted `job_id`.
        """
        if sum(payload is not None for payload in (command, operations, pirate)) != 1:
            raise Refusal("invalid_request", "exactly one of command, operations or pirate is required")
        if pirate is not None:
            if self.pirates is None:
                raise RuntimeError("this worker cannot run Pirate jobs")
            if cwd is not None or timeout is not None:
                raise Refusal("invalid_request",
                              "a Pirate job takes its timeout inside pirate, and no cwd")
            target = self.pirates.prepare_run(pirate)
            run_request: dict[str, object] = {"pirate": target}
            summary: dict[str, object] = {"kind": "pirate", "pirate": target["pirate"],
                                          "flavour": target["flavour"]}
        elif operations is not None:
            if self.batch is None:
                raise RuntimeError("this worker cannot run operations-based jobs")
            # Refuse a step list the batch runner would refuse, before a job exists.
            self.batch.prepare({"operations": operations, "on_error": on_error})
            effective_timeout = DEFAULT_JOB_SECONDS if timeout is None else float(timeout)
            if not 0.1 <= effective_timeout <= MAX_JOB_SECONDS:
                raise Refusal("invalid_request",
                              f"timeout_seconds must be 0.1 to {MAX_JOB_SECONDS:g}")
            run_request = {
                "operations": operations, "on_error": on_error, "timeout": effective_timeout}
            summary = {"kind": "batch", "step_count": len(operations)}
        else:
            assert command is not None
            effective_cwd, effective_timeout = prepare_command(
                self.settings, command, cwd, timeout, self.executor.limits, is_job=True)
            run_request = {"command": command, "cwd": effective_cwd, "timeout": effective_timeout}
            summary = {"kind": "command", "command": command[:1024],
                       "command_truncated": len(command) > 1024, "cwd": effective_cwd[:1024],
                       "cwd_truncated": len(effective_cwd) > 1024}
        self._prune()
        # One shared pool: command, operations and Pirate jobs are rows in
        # the same table, counted together against the one MAX_UNFINISHED.
        if self.store.count(UNFINISHED_STATES) >= MAX_UNFINISHED:
            raise Refusal("limit", "too many unfinished jobs")
        row = self.store.create("queued")
        job_id = row[0]
        observer = Observation(self.settings, "job:" + job_id, "worker", job_id=job_id)
        self.observers[job_id] = observer
        observer.emit("job_state", state="queued", **summary)
        owner = asyncio.create_task(self._run(job_id, run_request))
        self.running[job_id] = owner
        owner.add_done_callback(lambda task: self._cancel_before_start(job_id, task))
        return self.status(job_id)

    def _cancel_before_start(self, job_id: str, task: asyncio.Task) -> None:
        if task.cancelled() and self.running.get(job_id) is task:
            self.store.update(job_id, state="cancelled")
            self.running.pop(job_id, None)
            observer = self.observers.pop(job_id, None)
            if observer is not None:
                observer.emit("job_state", state="cancelled")
                observer.close()

    async def _run(self, job_id: str, request: dict[str, object]) -> None:
        is_command = "command" in request
        try:
            def mark_admitted() -> None:
                self.store.update(job_id, state="running")
                self.observers[job_id].emit("job_state", state="running")

            def snapshot(output: dict[str, object]) -> None:
                self.partial_output[job_id] = output

            if is_command:
                result = await self.executor.execute(
                    request["command"], request["cwd"], request["timeout"],
                    on_admitted=mark_admitted, job_id=job_id, on_output=snapshot)
            elif "pirate" in request:
                # The Pirate's answer is the job's outcome: a result completes
                # the job; its refusal (busy, ...) makes it refused; a
                # dropped connection or a Pirate failure fails the job.
                assert self.pirates is not None
                mark_admitted()
                result = await self.pirates.run(request["pirate"], job_id)
            else:
                assert self.batch is not None
                mark_admitted()
                result = await self.batch.run(
                    {"operations": request["operations"], "on_error": request["on_error"]},
                    foreground_cap=request["timeout"], job_id=job_id, on_output=snapshot)
            state = "completed"
        except asyncio.CancelledError:
            snapshot_result = self.partial_output.get(job_id)
            result = ({**snapshot_result, "output_incomplete": True, "cancelled": True}
                      if snapshot_result else {"cancelled": True})
            state = "cancelled"
        except Refusal as refusal:
            # The job's work was refused (not admitted, or the Pirate said
            # busy, ...): it did not run. Keep the refusal as data, with its
            # code (a Pirate's passed through), in the job's result.
            snapshot_result = self.partial_output.get(job_id)
            result = {**snapshot_result, "output_incomplete": True} if snapshot_result else {}
            result["refusal"] = refusal.as_data()
            state = "refused"
        except Exception as error:
            snapshot_result = self.partial_output.get(job_id)
            result = {**snapshot_result, "output_incomplete": True} if snapshot_result else {}
            result["error"] = f"{type(error).__name__}: {error}"[:1024]
            state = "failed"
        finally:
            self.store.update(job_id, state=state, result=result)
            self.partial_output.pop(job_id, None)
            self.running.pop(job_id, None)
            observer = self.observers.pop(job_id, None)
            if observer is not None:
                details: dict[str, object] = {}
                if state == "completed" and is_command:
                    details["exit_code"] = result.get("exit_code")
                if state == "failed" and "error" in result:
                    details["error"] = result["error"][:1024]
                if state == "refused":
                    details["refusal"] = result["refusal"]
                observer.emit("job_state", state=state, **details)
                observer.close()

    def list(self, limit: int = 20) -> dict:
        return {"jobs": [self._public(row) for row in self.store.list(limit)]}

    def status(self, job_id: str) -> dict:
        return self._public(self._row(job_id))

    def output(self, job_id: str) -> dict:
        row = self._row(job_id)
        answer = self._public(row, include_output=True)
        answer["partial"] = row[1] in UNFINISHED_STATES
        if answer["partial"] and row[0] in self.partial_output:
            answer["result"] = self.partial_output[row[0]]
        return answer

    async def wait(self, job_ids: object, wait_seconds: float) -> dict:
        """Wait until every job has settled, or until `wait_seconds` passed.

        Reports each job's status, never its output. A job is settled in a
        terminal state; an id that is malformed or unknown is reported with
        its `refusal` and counts as settled. `wait_seconds` 0 looks once.
        Running out of time is `wait_expired: true`, data rather than an
        error: call again, never start the work again.
        """
        if (not isinstance(job_ids, list) or not job_ids or len(job_ids) > MAX_WAIT_JOBS):
            raise Refusal("invalid_request", f"job_ids must list 1 to {MAX_WAIT_JOBS} job ids")
        if (type(wait_seconds) not in (int, float) or not math.isfinite(wait_seconds)
                or wait_seconds < 0):
            raise Refusal("invalid_request", "wait_seconds must be zero or a finite positive number")
        loop = asyncio.get_running_loop()
        started = loop.time()
        owners = {self.running[job_id] for job_id in job_ids
                  if isinstance(job_id, str) and job_id in self.running}
        if owners and wait_seconds > 0:
            await asyncio.wait(owners, timeout=wait_seconds)

        def check(job_id: object) -> dict:
            try:
                return self.status(job_id)
            except Refusal as refusal:
                return {"job_id": job_id, "refusal": refusal.as_data()}

        jobs = [check(job_id) for job_id in job_ids]
        settled = all("refusal" in job or job["state"] in TERMINAL_STATES for job in jobs)
        return {"jobs": jobs, "settled": settled, "wait_expired": not settled,
                "elapsed_seconds": round(loop.time() - started, 3)}

    def stop(self, job_id: str) -> dict:
        row = self._row(job_id)
        owner = self.running.get(row[0])
        if owner and not owner.done() and row[1] != "stopping":
            self.store.update(row[0], state="stopping")
            observer = self.observers.get(row[0])
            if observer is not None:
                observer.emit("job_state", state="stopping")
            owner.cancel()
        return self.status(row[0])
