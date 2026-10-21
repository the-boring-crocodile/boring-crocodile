"""Task-tool registration; worker lifecycle and authority stay with their owners."""
from __future__ import annotations
from typing import Annotated, Callable, Literal
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from boring_worker_kit.batch_contracts import BatchRequest, BatchStep, validation_fields
from boring_worker_kit.inputs import ExecTimeoutSeconds, JobTimeoutSeconds, JobWaitSeconds, ReceiptLimit
from .outcomes import refuse
from .worker_client import WorkerClient

class PirateJob(BaseModel):
    """Work for one registered Pirate flavour."""
    model_config = ConfigDict(extra="forbid", strict=True)
    pirate: str = Field(min_length=1)
    flavour: str = Field(min_length=1)
    assignment: str = Field(min_length=1)
    # null: the Pirate's own default; the worker still waits at most
    # MAX_JOB_SECONDS (plus its margin) for the answer.
    timeout_seconds: JobTimeoutSeconds | None = None


def register_task_tools(mcp: MCPServer, workers: WorkerClient,
                        selected_environment: Callable[[], str],
                        command_environment: Callable[[], str],
                        visible_batch: Callable[[dict[str, object]], dict[str, object]]) -> None:
    @mcp.tool()
    async def batch(operations: list[BatchStep], on_error: Literal["stop", "continue"] = "stop",
                    timeout_seconds: Annotated[float, Field(strict=True, allow_inf_nan=False,
                                                           ge=0.1, le=103)] | None = None) -> dict[str, object]:
        """Run prepared file, exec and job steps in order; return each settled outcome.

        Preflight checks the entire batch before effects. Jobs retain their own
        lifecycle. Earlier effects are not rolled back when a later step fails.
        """
        try:
            admitted = BatchRequest.model_validate({"operations": operations,
                "on_error": on_error, "timeout_seconds": timeout_seconds})
        except ValidationError as error:
            raise refuse("invalid_request", "invalid batch request",
                         fields=validation_fields(error)) from None
        return visible_batch(await workers.batch(selected_environment(), admitted.model_dump()))

    @mcp.tool()
    async def exec(
        command: str,
        cwd: str | None = None,
        timeout_seconds: ExecTimeoutSeconds | None = None,
    ) -> dict[str, object]:
        """Run a command and wait for the result. For work that fits in one breath.

        The worker request budget is at most 103 seconds, including queueing,
        startup and cleanup. The command gets the remaining budget. A shorter
        host lifetime can reduce it; the external client deadline is separate.
        Results report the effective command timeout. An admission timeout means
        the command was not started; a post-admission timeout can have effects.
        """
        return await workers.execute(command_environment(), command, cwd,
                                     timeout_seconds)

    @mcp.tool()
    async def job_start(command: str | None = None,
                        operations: list[BatchStep] | None = None,
                        pirate: PirateJob | None = None,
                        on_error: Literal["stop", "continue"] = "stop",
                        cwd: str | None = None,
                        timeout_seconds: JobTimeoutSeconds | None = None) -> dict[str, object]:
        """Start a job and return its job_id at once: a command, ordered steps, or work for a Pirate.

        Exactly one of command, operations (the step list `batch` takes) or
        pirate ({pirate, flavour, assignment, timeout_seconds}; null timeout:
        the Pirate's default). `cwd` applies only to a command.
        timeout_seconds: default 120 for a command, 600 for operations, at
        most 86400; pond_limits shows a lower host cap for commands. Then
        job_wait, job_status, job_output (the job's `result`), job_stop. Every
        call starts a new job; if one fails without an answer, check job_list
        before starting it again.
        """
        if sum(payload is not None for payload in (command, operations, pirate)) != 1:
            raise refuse("invalid_request", "exactly one of command, operations or pirate is required")
        if pirate is not None:
            if cwd is not None or timeout_seconds is not None:
                raise refuse("invalid_request",
                             "a Pirate job takes its timeout inside pirate, and no cwd")
            return await workers.job_start(command_environment(), None, None, None,
                                           pirate=pirate.model_dump())
        wire_operations = None
        if operations is not None:
            if cwd is not None:
                raise refuse("invalid_request", "cwd only applies to a single command job")
            try:
                admitted = BatchRequest.model_validate({"operations": operations, "on_error": on_error})
            except ValidationError as error:
                raise refuse("invalid_request", "invalid job operations",
                             fields=validation_fields(error)) from None
            wire_operations = admitted.model_dump()["operations"]
        return await workers.job_start(command_environment(), command, cwd,
                                       timeout_seconds,
                                       operations=wire_operations, on_error=on_error)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                           idempotent_hint=True, open_world_hint=False))
    async def job_list(limit: ReceiptLimit = 20) -> dict[str, object]:
        """See what's running, what's waiting, and what has already washed ashore.

        Lists recent Pond job IDs and states, without results.
        """
        return await workers.list_jobs(command_environment(), limit)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                           idempotent_hint=True, open_world_hint=False))
    async def job_status(job_id: str) -> dict[str, object]:
        """Check on a job. Still swimming, or done? Use the job_id job_start returned."""
        return await workers.job_action(command_environment(), "job_status", job_id)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                           idempotent_hint=True, open_world_hint=False))
    async def job_output(job_id: str) -> dict[str, object]:
        """Return the job's `result`: a partial snapshot while it runs, final once terminal.

        Judge a command by state, exit_code and timed_out; stdout/stderr are bounded.
        """
        output = await workers.job_action(command_environment(), "job_output", job_id)
        result = output.get("result")
        if isinstance(result, dict) and isinstance(result.get("results"), list):
            output["result"] = visible_batch(result)
        return output

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                           idempotent_hint=True, open_world_hint=False))
    async def job_wait(job_ids: list[str], wait_seconds: JobWaitSeconds = 45) -> dict[str, object]:
        """Wait for jobs to settle; return once all are done or time is up.

        Returns each job's status, not its `result` (job_output returns that).
        An unknown id is reported with its refusal. `wait_seconds`
        0 looks once. Running out of time sets `wait_expired: true`, which is
        not an error -- call again, never start the work again.
        """
        return await workers.job_wait(command_environment(), job_ids, wait_seconds)

    @mcp.tool()
    async def job_stop(job_id: str) -> dict[str, object]:
        """Stop a job and clean up its processes. No loose tails.

        Returns at once (usually `stopping`). The job ends `cancelled` once its
        processes are cleaned up, `failed` if cleanup could not be confirmed.
        """
        return await workers.job_action(command_environment(), "job_stop", job_id)
