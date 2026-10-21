from __future__ import annotations

import asyncio
import secrets

from boring_worker_kit.framing import read_frame, write_frame
from .outcomes import FailureReport, RefusalError, is_code
from boring_worker_kit.services import CALL_TIMEOUT_SECONDS
from .settings import EnvironmentSettings


# Beyond the worker's 103 s foreground budget, including settlement margin.
WORKER_REQUEST_TIMEOUT = 110.0
SOCKET_CLOSE_TIMEOUT = 2.0
PIRATE_REQUEST_TIMEOUT = 7.0
# The worker gives a service CALL_TIMEOUT_SECONDS; the control plane waits for
# the worker's answer that long plus the socket round trip, so the worker's
# own timeout (which says the call may have run) is what the caller sees.
SERVICE_WORKER_MARGIN_SECONDS = 2.0
SERVICE_REQUEST_TIMEOUT = CALL_TIMEOUT_SECONDS + SERVICE_WORKER_MARGIN_SECONDS


def _response_result(response: dict[str, object]) -> dict[str, object]:
    """Exactly one of: a result, a refusal (data, with its code), or a failure."""
    keys = set(response) - {"request_id"}
    if response.get("ok") is True and keys == {"ok", "result"}:
        result = response["result"]
        if isinstance(result, dict):
            return result
    elif response.get("ok") is False and keys == {"ok", "refusal"}:
        refusal = response["refusal"]
        if (isinstance(refusal, dict) and is_code(refusal.get("code"))
                and isinstance(refusal.get("message"), str)):
            raise RefusalError(refusal)
    elif response.get("ok") is False and keys == {"ok", "failure"}:
        failure = response["failure"]
        if isinstance(failure, str):
            raise FailureReport(failure)
    raise FailureReport("invalid worker response; the operation may have run")


class WorkerClient:
    def __init__(self, environments: dict[str, EnvironmentSettings]) -> None:
        self.environments = environments

    async def request(self, environment: str, operation: str, **arguments) -> dict[str, object]:
        try:
            return await asyncio.wait_for(
                self._request(environment, operation, **arguments), WORKER_REQUEST_TIMEOUT)
        except asyncio.TimeoutError as error:
            raise FailureReport("worker request deadline exceeded; the operation may have run") from error

    async def _request(self, environment: str, operation: str, **arguments) -> dict[str, object]:
        try:
            settings = self.environments[environment]
        except KeyError as error:
            raise ValueError(f"unknown environment {environment!r}") from error
        try:
            reader, writer = await asyncio.open_unix_connection(settings.socket_path)
        except (FileNotFoundError, ConnectionRefusedError, PermissionError) as error:
            raise FailureReport(f"the {environment} worker is not reachable "
                                f"({type(error).__name__}); nothing was sent") from error
        try:
            await write_frame(writer, {"request_id": secrets.token_urlsafe(18),
                                       "operation": operation, **arguments})
            response = await read_frame(reader)
        except (ConnectionError, asyncio.IncompleteReadError) as error:
            raise FailureReport(f"the {environment} worker closed the connection before answering "
                                f"({type(error).__name__}); the operation may have run") from error
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), SOCKET_CLOSE_TIMEOUT)
            except (ConnectionError, asyncio.TimeoutError):
                # Closing a failed socket cannot override the operation outcome.
                pass
        return _response_result(response)

    async def _pirate_request(self, environment: str, operation: str,
                              **arguments) -> dict[str, object]:
        try:
            return await asyncio.wait_for(
                self.request(environment, operation, **arguments),
                PIRATE_REQUEST_TIMEOUT,
            )
        except asyncio.TimeoutError as error:
            raise FailureReport("Pirate worker request deadline exceeded; the operation may have run") from error

    async def service_list(self, environment: str) -> dict[str, object]:
        return await self._service_request(environment, "service_list")

    async def service_register(self, environment: str, name: str, url: str,
                               ping_timeout: float) -> dict[str, object]:
        return await self._service_request(environment, "service_register", name=name, url=url,
                                           ping_timeout_seconds=ping_timeout)

    async def service_forget(self, environment: str, name: str) -> dict[str, object]:
        return await self._service_request(environment, "service_forget", name=name)

    async def service_describe(self, environment: str, service: str, token: str) -> dict[str, object]:
        return await self._service_request(environment, "service_describe", service=service,
                                           token=token)

    async def service_call(self, environment: str, service: str, tool: str,
                           arguments: dict[str, object], token: str) -> dict[str, object]:
        return await self._service_request(environment, "service_call", service=service,
                                           tool=tool, arguments=arguments, token=token)

    async def _service_request(self, environment: str, operation: str,
                               **arguments: object) -> dict[str, object]:
        try:
            return await asyncio.wait_for(self.request(environment, operation, **arguments),
                                          SERVICE_REQUEST_TIMEOUT)
        except asyncio.TimeoutError as error:
            raise FailureReport("service worker deadline exceeded; the call may have run") from error

    async def pirate_list(self, environment: str) -> dict[str, object]:
        return await self._pirate_request(environment, "list_pirates")

    async def pirate_describe(self, environment: str, pirate: str,
                              flavour: str) -> dict[str, object]:
        return await self._pirate_request(environment, "pirate_describe",
                                          pirate=pirate, flavour=flavour)

    async def batch(self, environment: str, batch: dict[str, object]) -> dict[str, object]:
        return await self.request(environment, "batch", batch=batch)

    async def file_call(self, environment: str, operation: str, **arguments) -> dict[str, object]:
        return await self.request(environment, operation, **arguments)

    async def limits(self, environment: str) -> dict[str, object]:
        return await self.request(environment, "limits")

    async def execute(self, environment: str, command: str, cwd: str | None = None,
                      timeout_seconds: float | None = None) -> dict[str, object]:
        return await self.request(environment, "exec", command=command, cwd=cwd,
                                  timeout_seconds=timeout_seconds)

    async def job_start(self, environment: str, command: str | None, cwd: str | None,
                        timeout_seconds: float | None, *,
                        operations: list[dict[str, object]] | None = None,
                        on_error: str = "stop",
                        pirate: dict[str, object] | None = None) -> dict[str, object]:
        return await self.request(environment, "job_start", command=command, cwd=cwd,
                                  timeout_seconds=timeout_seconds,
                                  operations=operations, on_error=on_error, pirate=pirate)

    async def list_jobs(self, environment: str, limit: int = 20) -> dict[str, object]:
        return await self.request(environment, "list_jobs", limit=limit)

    async def job_action(self, environment: str, action: str, job_id: str) -> dict[str, object]:
        return await self.request(environment, action, job_id=job_id)

    async def job_wait(self, environment: str, job_ids: list[str],
                       wait_seconds: float) -> dict[str, object]:
        # The worker itself waits up to wait_seconds; give the socket round
        # trip room beyond that instead of racing it with a fixed budget.
        try:
            return await asyncio.wait_for(
                self.request(environment, "job_wait", job_ids=job_ids,
                             wait_seconds=wait_seconds),
                wait_seconds + PIRATE_REQUEST_TIMEOUT,
            )
        except asyncio.TimeoutError as error:
            raise FailureReport("job_wait worker deadline exceeded; nothing was changed") from error
