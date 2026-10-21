"""One ordered worker request, with the existing owners executing each step."""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from boring_worker_kit.batch_contracts import BATCH_RESULT_BYTES, BatchRequest, admit_batch
from boring_worker_kit.framing import MAX_FRAME_BYTES
from boring_worker_kit.observation import Observation
from .outcomes import Refusal
from boring_worker_kit.execution import prepare_command
from boring_worker_kit.task_client import ContainmentError

if TYPE_CHECKING:
    from .worker import WorkerServer

FILE_METHODS = frozenset({"file_read", "file_write", "file_patch", "file_list", "file_search", "file_read_bytes", "file_write_bytes"})
JOB_METHODS = frozenset({"job_start", "job_list", "job_status", "job_output", "job_stop",
                         "job_wait"})
# Only the registry view: a service call needs a token the control plane mints
# for that one call, so service_describe and service_call are not batch steps.
SERVICE_METHODS = frozenset({"service_list"})
RESERVED_BYTES = 8192
_RESULT_CEILING = min(BATCH_RESULT_BYTES, MAX_FRAME_BYTES)
_RECEIPT_KEYS = ("job_id", "state")


def _size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8"))


def _trim_result(value: dict[str, object], allowance: int) -> tuple[dict[str, object], bool]:
    if _size(value) <= allowance:
        return value, False
    # Only large text is shortened. Keep hashes, job IDs, exit and lifecycle fields.
    def shrink(obj: object, cap: int) -> object:
        if not isinstance(obj, dict):
            return obj
        result: dict[str, object] = {}
        for key, item in obj.items():
            if key in ("content", "stdout", "stderr") and isinstance(item, str):
                result[key] = item[:cap]
            elif isinstance(item, dict):
                result[key] = shrink(item, cap)
            else:
                result[key] = item
        return result
    cap = 2048
    while cap >= 0:
        candidate = shrink(value, cap)
        if _size(candidate) <= allowance:
            assert isinstance(candidate, dict)
            return candidate, True
        if cap == 0:
            break
        cap //= 2
    # Unknown owner result shape: never discard its receipt or fabricate success.
    essentials = {key: value[key] for key in ("job_id", "sha256", "bytes", "state", "exit_code", "timed_out") if key in value}
    essentials["result_omitted"] = True
    return essentials, True


def _receipt(value: object) -> dict[str, object] | None:
    """The minimal projection of a step result: what recovers its work."""
    if not isinstance(value, dict):
        return None
    kept = {key: value[key] for key in _RECEIPT_KEYS if key in value}
    return kept or None


class BatchRunner:
    def __init__(self, worker: "WorkerServer") -> None:
        self.worker = worker

    def prepare(self, raw: object) -> tuple[BatchRequest, list[tuple[str, str, dict[str, object]]]]:
        batch = admit_batch(raw)
        steps: list[tuple[str, str, dict[str, object]]] = []
        for item in batch.operations:
            method = item.method
            arguments = item.arguments.model_dump(exclude_unset=True)
            if method in FILE_METHODS:
                arguments = self.worker.files.prepare(method, arguments)
            elif method == "exec":
                if self.worker.executor is None:
                    raise Refusal("forbidden", "command execution is disabled")
                prepare_command(self.worker.settings, arguments["command"], arguments.get("cwd"),
                                arguments.get("timeout_seconds"), self.worker.executor.limits)
            elif method in JOB_METHODS:
                if self.worker.jobs is None:
                    raise Refusal("forbidden", "managed jobs are disabled")
                if method == "job_start":
                    assert self.worker.executor is not None
                    prepare_command(self.worker.settings, arguments["command"], arguments.get("cwd"),
                                    arguments.get("timeout_seconds"), self.worker.executor.limits, is_job=True)
            elif method in SERVICE_METHODS:
                pass  # The registry view needs no admission check.
            steps.append((item.id, method, arguments))
        return batch, steps

    async def run(self, raw: object, *, foreground_cap: float = 103.0,
                  job_id: str | None = None,
                  on_output: Callable[[dict[str, object]], None] | None = None) -> dict[str, object]:
        # foreground_cap is 103 (the synchronous batch tool's own hard ceiling)
        # for a direct batch call. A background job (job_id given) passes its
        # own, much longer, job-level cap here instead -- everything else
        # about running the steps is identical.
        batch, steps = self.prepare(raw)
        started = asyncio.get_running_loop().time()
        budget = min(foreground_cap, batch.timeout_seconds or foreground_cap)
        if job_id is None and any(method == "exec" for _, method, _ in steps):
            # A job-aware exec step (job_id given) bypasses this foreground
            # ceiling entirely inside EnvironmentExecutor.execute; only a
            # direct, connection-bound batch call needs clamping to it.
            assert self.worker.executor is not None
            budget = min(budget, self.worker.executor.limits.exec_timeout_seconds)
        deadline = started + budget
        batch_id = secrets.token_hex(8)
        observer = Observation(self.worker.settings, "batch:" + batch_id, "worker")
        def emit(event: str, **fields: object) -> None:
            try:
                observer.emit(event, **fields)
            except Exception:
                pass  # Watch is never an execution dependency.
        emit("batch_state", state="started", batch_id=batch_id, count=len(steps))
        entries: list[dict[str, object]] = []
        stop_reason: str | None = None
        try:
            for index, (identifier, method, args) in enumerate(steps, 1):
                if deadline <= asyncio.get_running_loop().time():
                    stop_reason = "deadline"; break
                if _size(entries) > BATCH_RESULT_BYTES - RESERVED_BYTES:
                    stop_reason = "output_limit"; break
                emit("batch_step", state="started", batch_id=batch_id,
                              step_id=identifier, method=method, index=index, count=len(steps))
                at = time.monotonic()
                entry: dict[str, object] = {"id": identifier, "method": method}
                try:
                    remaining = deadline - asyncio.get_running_loop().time()
                    result = await asyncio.wait_for(self.worker.run_batch_step(method, args, deadline,
                                      batch_id=batch_id, step_id=identifier,
                                      job_id=job_id, on_output=on_output), timeout=remaining)
                    failure = method == "exec" and (result.get("exit_code") != 0 or
                        result.get("timed_out") is True)
                    entry["status"] = "failed" if failure else "succeeded"
                    available = BATCH_RESULT_BYTES - _size(entries) - RESERVED_BYTES - 1024
                    entry["result"], entry["result_truncated"] = _trim_result(result, max(available, 256))
                    if entry["result_truncated"] and stop_reason is None:
                        stop_reason = "output_limit"
                except Refusal as refusal:
                    # A normal outcome of this step, carried as data with its code.
                    entry.update(status="refused", refusal=refusal.as_data())
                except asyncio.TimeoutError:
                    entry.update(status="unknown", error="batch deadline; operation outcome may be unknown")
                    stop_reason = "deadline"
                except ContainmentError:
                    entry.update(status="unknown", error="command cleanup not confirmed")
                    stop_reason = "cleanup_unconfirmed"
                except Exception as error:
                    entry.update(status="failed", error=f"{type(error).__name__}: {str(error)[:256]}")
                entry["duration_ms"] = int((time.monotonic() - at) * 1000)
                entries.append(entry)
                emit("batch_step", state=entry["status"], batch_id=batch_id,
                              step_id=identifier, method=method, index=index, count=len(steps),
                              duration_ms=entry["duration_ms"], result_truncated=entry.get("result_truncated", False),
                              exit_code=result.get("exit_code") if "result" in entry else None)
                if stop_reason:
                    break
                if entry["status"] in ("failed", "refused") and batch.on_error == "stop":
                    stop_reason = "step_refused" if entry["status"] == "refused" else "step_failed"
                    break
            for skipped_index, (identifier, method, _) in enumerate(steps[len(entries):], len(entries) + 1):
                entries.append({"id": identifier, "method": method, "status": "not_started"})
                emit("batch_step", state="not_started", batch_id=batch_id,
                     step_id=identifier, method=method, index=skipped_index, count=len(steps))
            elapsed = round(asyncio.get_running_loop().time() - started, 3)
            result = {"batch_id": batch_id, "results": entries, "stop_reason": stop_reason,
                      "all_attempted": len(entries) == len(steps) and all(e["status"] != "not_started" for e in entries),
                      "elapsed_seconds": elapsed, "effective_timeout_seconds": budget}
            if _size({"ok": True, "result": result}) > _RESULT_CEILING:
                # Steps have already had effects: never discard their receipts.
                # Keep each step's id/status and only the handles needed to
                # recover its work; the owners still hold the full results.
                for entry in entries:
                    if "result" in entry:
                        entry["result"] = _receipt(entry["result"])
                        entry["result_truncated"] = True
                        if entry["result"] is None:
                            del entry["result"]
                if stop_reason is None:
                    stop_reason = result["stop_reason"] = "output_limit"
                if _size({"ok": True, "result": result}) > _RESULT_CEILING:
                    raise RuntimeError("batch result exceeds its budget even after reducing every step to its receipt")
            emit("batch_state", state="completed" if stop_reason is None else "stopped",
                          batch_id=batch_id, reason=stop_reason, attempted=sum(e["status"] != "not_started" for e in entries), count=len(steps))
            return result
        except asyncio.CancelledError:
            emit("batch_state", state="disconnected", batch_id=batch_id,
                          attempted=len(entries), count=len(steps))
            raise
        finally:
            try:
                observer.close()
            except Exception:
                pass
