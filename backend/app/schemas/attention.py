"""Safe owner-wide intervention inbox responses."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_serializer


SummaryCode = Literal[
    "approval_process", "approval_files", "approval_browser", "approval_screen",
    "approval_input", "approval_other", "failure_connection", "failure_context",
    "failure_tool", "failure_permission", "failure_uncertain", "failure_other",
]


class AttentionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["approval", "failure"]
    occurred_at: datetime
    room_id: int
    room_title: str
    project_id: int
    project_name: str
    agent_id: int | None
    agent_name: str
    request_event_id: str | None
    request_seq: int | None
    action_id: str | None
    failure_event_id: str | None
    failure_seq: int | None
    summary_code: SummaryCode
    href: str

    @field_serializer("occurred_at")
    def utc_occurred_at(self, value: datetime) -> str:
        value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return value.isoformat().replace("+00:00", "Z")


class AttentionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AttentionItem]
    next_cursor: str | None
