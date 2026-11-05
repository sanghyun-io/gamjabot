"""Safe, bounded wire shapes for one canonical room request execution."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class InspectorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class RequestIdentity(InspectorModel):
    event_id: str = Field(max_length=36)
    seq: int = Field(gt=0)
    created_at: datetime
    actor_name_snapshot: str = Field(max_length=200)
    preview: str = Field(max_length=160)
    href: str = Field(max_length=400)

    @field_serializer("created_at")
    def utc_created_at(self, value: datetime) -> str:
        value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return value.isoformat().replace("+00:00", "Z")


class SelectedAgent(InspectorModel):
    agent_id: int = Field(gt=0)
    name_snapshot: str | None = Field(default=None, max_length=200)
    live_state: Literal["running", "waiting", "paused", "idle", "stopped", "unknown"] | None


class PolicySnapshot(InspectorModel):
    max_rounds: int = Field(ge=1, le=4)
    max_tool_calls: int = Field(ge=0, le=64)
    max_executor_seconds: int = Field(ge=1, le=3600)


class ExecutionSummary(InspectorModel):
    phase: Literal["queued", "running", "waiting_approval", "completed", "cancelled", "interrupted", "unknown"]
    selected_agents: list[SelectedAgent] = Field(max_length=64)
    selected_agent_count: int = Field(ge=0, le=64)
    policy: PolicySnapshot | None
    current_round: int | None = Field(default=None, ge=0, le=4)


class CompletionSummary(InspectorModel):
    state: Literal["queued", "running", "waiting_approval", "interrupted", "completed", "cancelled", "unknown"]
    outcome_status: Literal["pending", "succeeded", "partial", "failed", "unknown"]
    result_available: bool
    completed_rounds: int = Field(ge=0, le=4)
    pending_agent_count: int = Field(ge=0, le=64)


class HistoricalModelUse(InspectorModel):
    connection_id: int = Field(gt=0)
    model_id: str = Field(min_length=1, max_length=200)
    provider_kind: str = Field(min_length=1, max_length=40)
    watermark_seq: int = Field(gt=0)


OutcomeSummary = Literal[
    "response", "passed", "failure_connection", "failure_context", "failure_tool",
    "failure_permission", "failure_uncertain", "failure_other", "failure_teammate_result_incomplete",
]


class RequestOutcome(InspectorModel):
    event_id: str = Field(max_length=36)
    seq: int = Field(gt=0)
    round: int = Field(ge=1, le=4)
    agent_id: int = Field(gt=0)
    actor_type: Literal['agent','system'] = 'agent'
    agent_name_snapshot: str = Field(max_length=200)
    kind: Literal["message", "failure", "passed"]
    phase: Literal["succeeded", "failed", "passed"]
    summary_code: OutcomeSummary
    model_use: HistoricalModelUse | None
    href: str = Field(max_length=400)


ActionState = Literal[
    "pending", "waiting", "approved", "started", "completed", "failed", "denied",
    "cancelled", "uncertain", "unknown",
]
ActionPhase = Literal[
    "queued", "approval", "running", "completed", "failed", "denied", "cancelled",
    "uncertain", "unknown",
]
ActionSummary = Literal[
    "action_process", "action_files", "action_browser", "action_screen", "action_input",
    "action_other",
]


class RequestAction(InspectorModel):
    action_id: str = Field(max_length=36)
    agent_id: int = Field(gt=0)
    state: ActionState
    phase: ActionPhase
    summary_code: ActionSummary
    requested_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    result_event_id: str | None = Field(default=None, max_length=36)
    result_seq: int | None = Field(default=None, gt=0)
    href: str = Field(max_length=400)

    @field_serializer("requested_at", "started_at", "completed_at")
    def utc_time(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return value.isoformat().replace("+00:00", "Z")


class CollaborationCheck(InspectorModel):
    event_id: str = Field(max_length=36)
    seq: int = Field(gt=0)
    agent_id: int = Field(gt=0)
    kind: Literal['teammates', 'bots']
    state: Literal['completed', 'failed']
    shown_count: int | None = Field(ge=0, le=10000)
    has_more: bool | None


class RequestInspectorResponse(InspectorModel):
    version: Literal[1]
    room_id: int = Field(gt=0)
    room_title: str = Field(max_length=200)
    read_only: bool
    request: RequestIdentity
    execution: ExecutionSummary
    completion: CompletionSummary
    outcomes: list[RequestOutcome] = Field(max_length=512)
    outcome_count: int = Field(ge=0)
    outcomes_truncated: bool
    actions: list[RequestAction] = Field(max_length=512)
    action_count: int = Field(ge=0)
    actions_truncated: bool
    collaboration_checks: list[CollaborationCheck] = Field(default_factory=list, max_length=128)
    collaboration_check_count: int = Field(default=0, ge=0)
    collaboration_checks_truncated: bool = False
