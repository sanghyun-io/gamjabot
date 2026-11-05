"""Strict wire shapes for one explicit request finalization turn."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.request_inspector import CompletionSummary, HistoricalModelUse, OutcomeSummary


BlockedReason = Literal[
    "room_read_only", "source_invalid", "source_not_terminal", "source_outcome_unknown",
    "source_no_useful_result", "source_all_passed", "source_all_failed", "source_too_large",
    "action_unsettled", "action_uncertain", "completed_effect", "room_busy",
    "agent_not_selected", "agent_unavailable", "agent_busy", "model_unavailable",
    "context_too_large",
]


class FinalizeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class FinalizePreviewWrite(FinalizeModel):
    agent_id: int = Field(gt=0)


class FinalizeWrite(FinalizePreviewWrite):
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class FinalizeSourceRequest(FinalizeModel):
    event_id: str = Field(max_length=36)
    seq: int = Field(gt=0)
    preview: str = Field(max_length=160)
    href: str = Field(max_length=400)


class FinalizeOutcome(FinalizeModel):
    event_id: str = Field(max_length=36)
    seq: int = Field(gt=0)
    round: int = Field(ge=1, le=4)
    agent_id: int = Field(gt=0)
    agent_name_snapshot: str = Field(max_length=200)
    kind: Literal["message", "failure", "passed"]
    phase: Literal["succeeded", "failed", "passed"]
    summary_code: OutcomeSummary
    model_use: HistoricalModelUse | None
    href: str = Field(max_length=400)


class ExcludedAgent(FinalizeModel):
    agent_id: int = Field(gt=0)
    name_snapshot: str = Field(max_length=200)
    reason: Literal["failed", "passed"]


class FinalizeAgent(FinalizeModel):
    agent_id: int = Field(gt=0)
    name_snapshot: str = Field(max_length=200)
    current_name: str = Field(max_length=200)


class FinalizeCurrentModel(FinalizeModel):
    connection_id: int = Field(gt=0)
    model_id: str = Field(min_length=1, max_length=200)
    provider_kind: str = Field(min_length=1, max_length=40)


class FinalizeLimits(FinalizeModel):
    max_rounds: Literal[1]
    max_tool_calls: Literal[0]
    max_executor_seconds: Literal[60]


class FinalizeTruncation(FinalizeModel):
    event_id: str = Field(max_length=36)
    original_chars: int = Field(ge=0)
    included_chars: int = Field(ge=0)
    omitted_chars: int = Field(ge=0)


class FinalizeCoverage(FinalizeModel):
    source_request_event_id: str = Field(max_length=36)
    source_outcome_event_ids: list[str] = Field(max_length=256)
    included_message_event_ids: list[str] = Field(max_length=256)
    omitted_event_ids: list[str] = Field(max_length=256)
    truncations: list[FinalizeTruncation] = Field(max_length=257)
    used_chars: int = Field(ge=0, le=32000)
    limit_chars: Literal[32000]


class RequestFinalizePreview(FinalizeModel):
    version: Literal[1]
    room_id: int = Field(gt=0)
    source_request: FinalizeSourceRequest | None
    source_completion: CompletionSummary | None
    outcomes: list[FinalizeOutcome] = Field(max_length=256)
    outcome_count: int = Field(ge=0)
    outcomes_truncated: bool
    excluded_agents: list[ExcludedAgent] = Field(max_length=64)
    agent: FinalizeAgent | None
    current_model: FinalizeCurrentModel | None
    limits: FinalizeLimits
    source_coverage: FinalizeCoverage | None
    eligible: bool
    blocked_reason: BlockedReason | None
    plan_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class RequestFinalizeResult(FinalizeModel):
    version: Literal[1]
    room_id: int = Field(gt=0)
    source_request_event_id: str = Field(max_length=36)
    source_outcome_event_ids: list[str] = Field(max_length=256)
    finalize_event_id: str = Field(max_length=36)
    finalize_seq: int = Field(gt=0)
    agent_id: int = Field(gt=0)
    replayed: bool
    href: str = Field(max_length=400)
