"""The Crocodile's batch steps: the worker kit's, plus service_call.

A service call needs a token the control plane mints for that call. The control
plane mints one for every service_call step after validating the batch and
sends them to the worker beside the steps (never inside them); the worker runs
a service step only with its token. So service_call is a step of a direct
batch, not of a job's operations (a job can outlive a token). With file
references and save_to, files carry results from one step to the next.
"""
from __future__ import annotations

import json
from typing import Annotated, Literal, Union, get_args

from pydantic import Field, ValidationError

from boring_worker_kit.batch_contracts import (BATCH_INPUT_BYTES, BATCH_MAX_STEPS, Arguments, BatchRequest,
                                               BatchStep, StepBase, invalid_request)
from boring_worker_kit.framing import MAX_FRAME_BYTES
from boring_worker_kit.outcomes import Refusal


class SaveTo(Arguments):
    root: str = Field(min_length=1)
    path: str = Field(min_length=1)
    expected_sha256: str | None  # required: null for a new file, or the sha256 of the file to replace


class ServiceCallArgs(Arguments):
    service: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    arguments: dict[str, object] = Field(default_factory=dict)
    save_to: SaveTo | None = None


class ServiceCallStep(StepBase):
    method: Literal["service_call"]
    arguments: ServiceCallArgs


_KIT_STEPS = get_args(get_args(BatchStep)[0])  # the kit's step classes, in its order
PondBatchStep = Annotated[Union[(*_KIT_STEPS, ServiceCallStep)], Field(discriminator="method")]  # type: ignore[valid-type]


class PondBatchRequest(BatchRequest):
    operations: list[PondBatchStep] = Field(min_length=1, max_length=BATCH_MAX_STEPS)  # type: ignore[assignment]


def admit(raw: object) -> PondBatchRequest:
    """The kit's admission (input size, step shapes, unique IDs) with service_call added."""
    if len(json.dumps(raw, ensure_ascii=True, allow_nan=False).encode()) > min(BATCH_INPUT_BYTES, MAX_FRAME_BYTES):
        raise Refusal("limit", "batch input exceeds limit")
    try:
        return PondBatchRequest.model_validate(raw)
    except ValidationError as error:
        raise invalid_request(error, "invalid batch request") from None


def service_steps(batch: PondBatchRequest) -> list[ServiceCallStep]:
    return [step for step in batch.operations if isinstance(step, ServiceCallStep)]
