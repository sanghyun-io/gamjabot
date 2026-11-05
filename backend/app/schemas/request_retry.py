"""Strict safe wire contract for one targeted request retry."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


BlockedReason = Literal[
    "room_read_only", "source_invalid", "source_not_terminal", "failure_superseded",
    "room_busy", "agent_unavailable", "agent_busy", "model_unavailable",
    "action_unsettled", "action_uncertain", "completed_effect", "memory_context_changed",
    "context_unavailable", "teammate_result_incomplete",
]


class RetryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class RetrySourceRequest(RetryModel):
    event_id: str = Field(max_length=36)
    seq: int = Field(gt=0)
    preview: str = Field(max_length=160)
    href: str = Field(max_length=400)


class RetryAgent(RetryModel):
    agent_id: int = Field(gt=0)
    name_snapshot: str = Field(max_length=200)
    current_name: str = Field(max_length=200)


class RetryCurrentModel(RetryModel):
    connection_id: int = Field(gt=0)
    model_id: str = Field(min_length=1, max_length=200)
    provider_kind: str = Field(min_length=1, max_length=40)


class RetryLimits(RetryModel):
    max_rounds: Literal[1]
    max_tool_calls: int = Field(ge=0, le=64)
    max_executor_seconds: int = Field(ge=1, le=3600)


class TeammateRecovery(RetryModel):
    version: Literal[1]
    direct_count: Literal[1]
    nested_count: int = Field(ge=0, le=63)
    total_chars: int = Field(ge=0)
    single_attempt: Literal[True]


class TeammateRecoveryRepeat(RetryModel):
    version: Literal[2]
    direct_count: Literal[1]
    nested_count: int = Field(ge=0, le=63)
    total_chars: int = Field(ge=0)
    single_attempt: Literal[False]
    attempt_number: int = Field(ge=2, le=33)


class RequestRetryPreflight(RetryModel):
    version: Literal[1]
    room_id: int = Field(gt=0)
    failure_event_id: str = Field(max_length=36)
    failure_seq: int = Field(gt=0)
    source_request: RetrySourceRequest | None
    agent: RetryAgent | None
    current_model: RetryCurrentModel | None
    limits: RetryLimits | None
    eligible: bool
    blocked_reason: BlockedReason | None
    plan_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    teammate_recovery: TeammateRecovery | TeammateRecoveryRepeat | None = None


class RequestRetryWrite(RetryModel):
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class RequestRetryResult(RetryModel):
    version: Literal[1]
    room_id: int = Field(gt=0)
    source_failure_event_id: str = Field(max_length=36)
    source_trigger_event_id: str = Field(max_length=36)
    source_request_event_id: str = Field(max_length=36)
    retry_event_id: str = Field(max_length=36)
    retry_seq: int = Field(gt=0)
    agent_id: int = Field(gt=0)
    replayed: bool
    href: str = Field(max_length=400)
