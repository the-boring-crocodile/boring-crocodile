"""Controller carriers and worker failure presentation; Common owns the vocabulary."""
from __future__ import annotations

import json
import logging
from typing import cast

from mcp.server.mcpserver.exceptions import ToolError

from boring_outcomes import (CODE as CODE, OUR_CODES as OUR_CODES, Failed as Failed,
                             Refused as Refused, is_code as is_code)
from boring_worker_kit.outcomes import failure_data as worker_failure_data


def outcome_data(data: dict[str, object], status: str) -> dict[str, object]:
    """Validate a received outcome through its owner, preserving its flat details."""
    if status not in ("refused", "failed") or data.get("status") != status:
        raise ValueError("outcome has the wrong status")
    cls = Refused if status == "refused" else Failed
    extra = {key: value for key, value in data.items() if key not in ("status", "code", "message")}
    return cls(cast(str, data.get("code")), cast(str, data.get("message")), **extra).as_data()


class RefusalError(ToolError):
    """Controller-side carrier for a validated, flat refused outcome."""

    def __init__(self, data: dict[str, object]) -> None:
        self.data = outcome_data(data, "refused")
        super().__init__(json.dumps(self.data, separators=(",", ":"), ensure_ascii=False))

    @property
    def code(self) -> str:
        return str(self.data["code"])


class FailureReport(RuntimeError, ToolError):
    """An already reported failure, carried through the SDK without guessing from text."""

    def __init__(self, code: str, message: str, **details: object) -> None:
        self.data = Failed(code, message, **details).as_data()
        super().__init__(json.dumps(self.data, separators=(",", ":"), ensure_ascii=False))

    @classmethod
    def from_data(cls, data: dict[str, object]) -> FailureReport:
        checked = outcome_data(data, "failed")
        extra = {key: value for key, value in checked.items() if key not in ("status", "code", "message")}
        return cls(cast(str, checked["code"]), cast(str, checked["message"]), **extra)

    @property
    def code(self) -> str:
        return str(self.data["code"])


def failure_data(error: Exception, *, component: str = "worker") -> dict[str, object]:
    """Present failures at existing catches; never infer a cause by parsing prose."""
    if isinstance(error, FailureReport):
        return error.data
    return worker_failure_data(error, component=component, fallback_code="worker_failed",
                               logger=logging.getLogger("boring_crocodile." + component))


def refuse(code: str, message: str, **details: object) -> RefusalError:
    """A control-plane-side refusal, with the same shape a worker refusal has."""
    return RefusalError(Refused(code, message, **details).as_data())
