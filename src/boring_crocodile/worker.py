from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
import time
from typing import Awaitable, Callable

from boring_worker_kit.settlement import settled

from boring_worker_kit.execution import EnvironmentExecutor
from .handbook_context import ContextBootstrap
from boring_worker_kit.framing import peer_uid, read_frame, write_frame
from boring_worker_kit.host_limits import HostLimits, read_host_limits
from .jobs import ManagedJobs
from boring_worker_kit.observation import Observation
from .pirates import Pirates, registration_path
from boring_worker_kit.services import Services, ServiceFailure
from boring_worker_kit.files import FileTools
from .batch import BatchRunner, FILE_METHODS, JOB_METHODS, SERVICE_METHODS
from boring_worker_kit.inputs import MAX_JOB_SECONDS
from .jobs import DEFAULT_JOB_SECONDS
from boring_worker_kit.batch_contracts import BATCH_MAX_STEPS, BATCH_INPUT_BYTES, BATCH_RESULT_BYTES
from .outcomes import Refusal
from .settings import EnvironmentSettings, Settings
from .unix_sockets import bound_identity, clear_stale_socket, release_socket

BATCH_STEP_MARGIN = 0.1
logger = logging.getLogger(__name__)


class WorkerServer:
    def __init__(self, settings: EnvironmentSettings, context: ContextBootstrap | None = None,
                 pond: str | None = None) -> None:
        self.settings = settings
        self.context = context
        self.limits = (read_host_limits(settings) if settings.task_socket_path else HostLimits(1, None)) if settings.command_execution_enabled else None
        self.executor = EnvironmentExecutor(settings, self.limits) if self.limits is not None else None
        self.pirates = Pirates()
        # Services know this worker by its Pond's name (the ping's clientInfo).
        self.services = Services(pond or settings.name)
        self.files = FileTools(settings.files, settings.socket_path.parent / "file-locks", settings)
        self.batch = BatchRunner(self)
        # BatchRunner only stores this reference at construction time and reads
        # self.jobs later, when a request actually runs -- so building batch
        # before jobs (which needs batch, to run an operations-based job) is safe.
        self.jobs = (ManagedJobs(settings, self.executor, self.batch, self.pirates)
                     if self.executor is not None and settings.task_socket_path else None)

    def _command_limits(self) -> dict[str, object]:
        return (self.limits.public() if self.limits is not None
                else {"command_execution_enabled": False,
                      "task_slots": 0, "exec_timeout_seconds": None,
                      "job_timeout_seconds": None})

    def _capabilities(self) -> dict[str, object]:
        return {
            "command_execution_enabled": self.executor is not None,
            "command_limits": self._command_limits(),
            "managed_jobs_enabled": self.jobs is not None,
            "pirates": self.pirates.list()["pirates"],
            "services": self.services.list()["services"],
            "files": self.settings.files.public(),
            "batch": {"enabled": True, "max_steps": BATCH_MAX_STEPS,
                      "max_input_bytes": BATCH_INPUT_BYTES, "max_result_bytes": BATCH_RESULT_BYTES,
                      "max_foreground_seconds": min(103.0, self.limits.exec_timeout_seconds) if self.limits else 103.0,
                      "methods": sorted(FILE_METHODS | (JOB_METHODS if self.jobs else frozenset()) |
                                        ({"exec"} if self.executor else set()) |
                                        SERVICE_METHODS),
                      "job_default_timeout_seconds": DEFAULT_JOB_SECONDS if self.jobs else None,
                      "job_max_timeout_seconds": MAX_JOB_SECONDS if self.jobs else None},
        }

    async def run_batch_step(self, method: str, args: dict[str, object], deadline: float | None = None,
                             *, batch_id: str | None = None, step_id: str | None = None,
                             job_id: str | None = None,
                             on_output: Callable[[dict[str, object]], None] | None = None) -> dict[str, object]:
        if method in FILE_METHODS:
            operation = self.files.call(method, {"operation": method, "request_id": "batch", **args},
                                        batch_id=batch_id, step_id=step_id)
            # A managed job owns settlement beyond its request deadline.
            # Foreground callers retain the documented unknown-outcome path.
            return await settled(operation) if job_id is not None else await operation
        if method == "exec":
            if self.executor is None:
                raise Refusal("forbidden", "command execution is disabled")
            # job_id/on_output are only set when this exec step is running
            # under a job (see ManagedJobs._run via BatchRunner.run): that
            # gives it the same streaming/job-aware admission a plain
            # command job already had, instead of the foreground-only path
            # an ad-hoc `batch` call gets.
            return await self.executor.execute(args.get("command"), args.get("cwd"),
                                               args.get("timeout_seconds"),
                                               foreground_deadline=deadline,
                                               job_id=job_id, on_output=on_output)
        if method in JOB_METHODS:
            if self.jobs is None:
                raise Refusal("forbidden", "command execution is disabled or lacks a task runner")
            if method == "job_start":
                return self.jobs.start(args.get("command"), args.get("cwd"),
                                       args.get("timeout_seconds"),
                                       operations=args.get("operations"),
                                       on_error=args.get("on_error", "stop"),
                                       pirate=args.get("pirate"))
            if method == "job_list":
                return self.jobs.list(args.get("limit", 20))
            if method == "job_wait":
                wait_seconds = args.get("wait_seconds", 45.0)
                # "Inside a batch: 0 up to the remaining batch time" -- the step's
                # own bound (0..103) still applies; the shared deadline can only
                # shrink it further, never extend it past what the step asked for.
                # BATCH_STEP_MARGIN leaves room for this step's own round trip
                # and return so it settles before batch.run's own per-step
                # asyncio.wait_for(..., timeout=remaining), computed a moment
                # earlier against the same deadline, elapses first.
                if deadline is not None:
                    remaining = deadline - asyncio.get_running_loop().time()
                    wait_seconds = max(0.0, min(wait_seconds, remaining - BATCH_STEP_MARGIN))
                return await self.jobs.wait(args.get("job_ids"), wait_seconds)
            return getattr(self.jobs, method.removeprefix("job_"))(args.get("job_id"))
        if method in SERVICE_METHODS:
            return self.services.list()
        raise Refusal("invalid_request", "unsupported batch operation")

    async def _observed(self, kind: str, method: str, target: object, detail: object,
                        work: Awaitable[dict]) -> dict:
        """Best-effort watch events around a foreground call; never its outcome.

        Names only: the service or Pirate, the tool or flavour, timing and a
        refusal code. Arguments, tokens and results stay out of observation.
        """
        name = target if isinstance(target, str) else "?"
        fields: dict[str, object] = {"method": method, "target": name[:64]}
        if isinstance(detail, str) and detail:
            # A service's tool, or a Pirate's flavour.
            fields["tool" if kind == "service" else "flavour"] = detail[:64]
        observer = Observation(self.settings, kind + ":" + name[:64], "worker")
        observer.emit("call_state", state="started", **fields)
        started = time.monotonic()
        state, extra = "failed", {}
        try:
            result = await work
            state = "completed"
            return result
        except Refusal as refusal:
            state, extra = "refused", {"code": refusal.code}
            raise
        except asyncio.CancelledError:
            state = "cancelled"
            raise
        finally:
            observer.emit("call_state", state=state,
                          duration_ms=int((time.monotonic() - started) * 1000), **fields, **extra)
            observer.close()

    async def handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        execution: asyncio.Task | None = None
        disconnected: asyncio.Task | None = None
        try:
            if peer_uid(writer) != self.settings.control_plane_uid:
                # Decided before reading the request: a normal outcome, as data.
                raise Refusal("forbidden", "only this Pond's control plane may call its worker")
            try:
                request = await read_frame(reader)
            except (ConnectionError, asyncio.IncompleteReadError):
                # Only an incomplete *incoming* request is a disconnected client.
                return
            operation = request.get("operation")
            if not isinstance(operation, str):
                raise Refusal("invalid_request", "operation is required")
            if operation == "bootstrap":
                if self.context is None:
                    raise RuntimeError("No context configured for this worker")
                project = request.get("project")
                if project is not None and not isinstance(project, str):
                    raise Refusal("invalid_request", "project must be a name or null")
                if project is not None and project not in self.context.projects:
                    raise Refusal("not_found", "unknown project", project=project)
                result = await asyncio.to_thread(self.context.load, project)
                result["capabilities"] = self._capabilities()
            elif operation == "batch":
                execution = asyncio.create_task(self.batch.run(request.get("batch")))
                disconnected = asyncio.create_task(reader.read(1))
                done, _ = await asyncio.wait((execution, disconnected), return_when=asyncio.FIRST_COMPLETED)
                if disconnected in done:
                    return
                result = await execution
            elif operation == "limits":
                result = self._command_limits()
            elif operation in ("service_describe", "service_call"):
                result = await self._observed(
                    "service", operation, request.get("service"),
                    request.get("tool") if operation == "service_call" else None,
                    self.services.dispatch(request))
            elif operation in ("service_register", "service_list", "service_forget"):
                result = await self.services.dispatch(request)
            elif operation == "list_pirates":
                result = self.pirates.list()
            elif operation == "pirate_describe":
                execution = asyncio.create_task(self._observed(
                    "pirate", operation, request.get("pirate"), request.get("flavour"),
                    self.pirates.describe(request.get("pirate"), request.get("flavour"))))
                # The control plane gives up after its own deadline; do not keep
                # waiting on the Pirate for a caller that has gone.
                disconnected = asyncio.create_task(reader.read(1))
                done, _ = await asyncio.wait((execution, disconnected), return_when=asyncio.FIRST_COMPLETED)
                if disconnected in done:
                    return
                result = await execution
            elif operation == "job_wait":
                if self.jobs is None:
                    raise Refusal("forbidden", "command execution is disabled or lacks a task runner")
                wait_seconds = request.get("wait_seconds")
                execution = asyncio.create_task(self.jobs.wait(
                    request.get("job_ids"), 45.0 if wait_seconds is None else wait_seconds))
                # A wait can run up to wait_seconds; a client that hangs up
                # mid-wait should not keep this connection's coroutine alive.
                disconnected = asyncio.create_task(reader.read(1))
                done, _ = await asyncio.wait(
                    (execution, disconnected), return_when=asyncio.FIRST_COMPLETED,
                )
                if disconnected in done:
                    return
                result = await execution
            elif operation in FILE_METHODS or operation in ("job_start", "list_jobs", "job_status", "job_output", "job_stop"):
                method = "job_list" if operation == "list_jobs" else operation
                result = await self.run_batch_step(method, {k: v for k, v in request.items() if k not in ("operation", "request_id")})
            elif operation == "exec":
                if self.executor is None:
                    raise Refusal("forbidden", "command execution is disabled")
                execution = asyncio.create_task(self.executor.execute(
                    request.get("command"), request.get("cwd"),
                    request.get("timeout_seconds"),
                ))
                # Foreground EOF cancels only ordinary exec, never a managed job.
                disconnected = asyncio.create_task(reader.read(1))
                done, _ = await asyncio.wait(
                    (execution, disconnected), return_when=asyncio.FIRST_COMPLETED,
                )
                if disconnected in done:
                    return
                result = await execution
            else:
                raise Refusal("invalid_request", "unknown worker operation")
            await write_frame(writer, {"ok": True, "result": result})
        except Refusal as refusal:
            # A normal outcome: carried to the caller as data, with its code.
            try:
                await write_frame(writer, {"ok": False, "refusal": refusal.as_data()})
            except ConnectionError:
                pass
        except Exception as error:
            # Everything else is a failure -- never disguised as a refusal.
            logger.exception("worker request failed")
            try:
                failure = (str(error) if isinstance(error, ServiceFailure)
                           else f"{type(error).__name__}: {error}"[:512])
                await write_frame(writer, {"ok": False, "failure": failure})
            except ConnectionError:
                pass
        finally:
            tasks = [task for task in (execution, disconnected) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            # The executor keeps its slot until process cleanup has completed.
            cleanup = asyncio.gather(*tasks, return_exceptions=True)
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def serve(self) -> None:
        path = self.settings.socket_path
        path.parent.mkdir(parents=True, exist_ok=True)
        clear_stale_socket(path)
        server = await asyncio.start_unix_server(self.handle, path)
        os.chmod(path, 0o660)
        bound = bound_identity(path)  # after chmod: it changes the change time
        registration = None
        # The heartbeats live as long as the worker: they ping every registered
        # Pirate flavour and service and drop the ones that stopped answering.
        heartbeats = [asyncio.create_task(self.pirates.registry.run()),
                      asyncio.create_task(self.services.registry.run())]
        try:
            if self.settings.service_registration_enabled:
                registration = await self.pirates.listen(registration_path(self.settings.name))
            async with server:
                await server.serve_forever()
        finally:
            for heartbeat in heartbeats:
                heartbeat.cancel()
            server.close()
            await server.wait_closed()
            release_socket(path, bound)
            if registration is not None:
                registration.close()
                await registration.wait_closed()
            await self.pirates.close()
            await self.services.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--environment", required=True)
    arguments = parser.parse_args()
    settings = Settings.load(arguments.config)
    try:
        environment = settings.environments[arguments.environment]
    except KeyError as error:
        raise SystemExit(f"unknown environment: {arguments.environment}") from error
    if environment.command_execution_enabled and environment.task_socket_path is None:
        raise SystemExit("Command-enabled workers require task_socket_path; no local-execution fallback")
    ponds = [name for name, pond in settings.ponds.items()
             if pond.environment == arguments.environment]
    asyncio.run(WorkerServer(environment, ContextBootstrap.for_environment(
        settings, arguments.environment), pond=ponds[0] if ponds else None).serve())


if __name__ == "__main__":
    main()
