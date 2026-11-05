"""Safe, read-only owner Agent workload responses."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_serializer


WorkloadState = Literal["running", "waiting", "paused", "failed", "idle", "unknown"]


class WorkloadCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    running: int
    waiting: int
    paused: int
    failed: int
    idle: int
    unknown: int
    total_rooms: int


class AgentWorkloadRoom(BaseModel):
    model_config = ConfigDict(extra="forbid")

    room_id: int
    room_title: str
    project_id: int
    project_name: str
    state: WorkloadState
    trigger_event_id: str | None
    trigger_seq: int | None
    failure_event_id: str | None
    failure_seq: int | None
    last_activity_at: datetime
    href: str

    @field_serializer("last_activity_at")
    def utc_last_activity_at(self, value: datetime) -> str:
        value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return value.isoformat().replace("+00:00", "Z")


class AgentWorkload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: int
    agent_name: str
    agent_active: bool
    overall_state: WorkloadState
    counts: WorkloadCounts
    rooms: list[AgentWorkloadRoom]
    rooms_truncated: bool


class AgentWorkloadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AgentWorkload]
    next_cursor: str | None
