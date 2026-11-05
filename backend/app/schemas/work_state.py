"""Small, owner-scoped work projection shared by GUI and CLI."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

PositiveInt = Annotated[int, Field(strict=True, gt=0)]
WorkPhase = Literal[
    "idle", "queued", "thinking", "peer_waiting", "approval_waiting",
    "tool_waiting", "tool_running", "completed", "passed", "failed",
    "uncertain", "paused", "stopped", "cancelled", "interrupted", "unknown",
]


def canonical_id(value: object) -> str:
    if not isinstance(value, str) or len(value) != 36 or value != value.lower():
        raise ValueError("Expected a canonical event identity")
    if str(UUID(value)) != value:
        raise ValueError("Expected a canonical event identity")
    return value


class WorkEventRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str
    seq: PositiveInt

    _event_id = field_validator("event_id", mode="before")(canonical_id)


class WorkState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    room_id: PositiveInt
    agent_id: PositiveInt
    scope: Literal["current", "previous", "none"] = "none"
    phase: WorkPhase = "idle"
    trigger_event_id: str | None = None
    generation: PositiveInt | None = None
    round: PositiveInt | None = None
    updated_at: datetime | None = None
    request: WorkEventRef | None = None
    event: WorkEventRef | None = None
    result: WorkEventRef | None = None
    action_id: str | None = None
    peer_agent_ids: list[PositiveInt] = Field(default_factory=list, max_length=64)

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported work-state version")
        return value

    @field_validator("trigger_event_id", "action_id", mode="before")
    @classmethod
    def optional_id(cls, value: object) -> object:
        return None if value is None else canonical_id(value)
