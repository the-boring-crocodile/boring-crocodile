"""Outcomes of normal operation, carried as data -- distinct from failures.

An exception marks a failure: a bug, a crash, a restart, a broken dependency.
A refusal (busy, limit reached, not found, stale, ...) is a normal outcome the
caller can act on. It travels as data with its code across every boundary;
nothing downstream parses it back out of a message.
"""
from __future__ import annotations

import json

from mcp.server.mcpserver.exceptions import ToolError

from boring_worker_kit.outcomes import CODE as CODE, OUR_CODES as OUR_CODES, is_code as is_code, Refusal as Refusal


class RefusalError(ToolError):
    """Controller-side carrier: the MCP SDK reports it as an anticipated error.

    Its text is the compact JSON of the refusal data, so the caller reads the
    code as data rather than parsing prose.
    """

    def __init__(self, data: dict[str, object]) -> None:
        if not is_code(data.get("code")) or not isinstance(data.get("message"), str):
            raise ValueError("refusal data needs a code and a message")
        self.data = dict(data)
        super().__init__(json.dumps(self.data, separators=(",", ":"), ensure_ascii=False))

    @property
    def code(self) -> str:
        return str(self.data["code"])


class FailureReport(RuntimeError, ToolError):
    """A failure reported to the caller, with its explanation.

    The component that failed has already logged it. Carrying the report as an
    anticipated error keeps the MCP SDK from reducing it to a bare "Error
    executing tool": a failure is reported with what failed and whether the
    operation may have run, never hidden. A bug in the controller itself is
    still the SDK's to report as an unexpected crash.
    """

    def __init__(self, failure: str) -> None:
        self.failure = failure
        super().__init__(json.dumps({"failure": failure}, separators=(",", ":"), ensure_ascii=False))


def refuse(code: str, message: str, **details: object) -> RefusalError:
    """A control-plane-side refusal, with the same shape a worker refusal has."""
    return RefusalError(Refusal(code, message, **details).as_data())
