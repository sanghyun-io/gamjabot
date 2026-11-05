"""Provider-free room context preview contract."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from app.schemas.event import ReplyTo, PageTarget


class ContextPreviewWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=65536)
    reply_to: ReplyTo | None = None
    page_target: PageTarget | None = None


class PreviewMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str


class SummaryRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["extractive_checkpoint", "owner_checkpoint"]
    source_start_seq: int
    source_end_seq: int


class SourceCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    committed_high_water: int
    included_event_seqs: list[int]
    summarized_ranges: list[SummaryRange]
    draft_included: bool


class ContextTruncation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str
    seq: int
    original_chars: int
    included_chars: int
    omitted_chars: int


class NonTextSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str
    seq: int
    kind: Literal["screenshot_reference"]
    supplied: Literal[False]
    image_mime: Literal["image/png", "image/jpeg"] | None
    width: int | None
    height: int | None


class CharacterBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unit: Literal["characters"]
    limit_chars: int
    used_chars: int
    remaining_chars: int


PreviewErrorCode = Literal[
    "model_binding_missing", "context_state_missing", "context_input_too_large",
    "context_memory_too_large", "context_too_large", "reply_context_unavailable",
]


class MemoryProjectionEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    revision: int
    scope: Literal['room','agent']
    target_agent_id: int | None


class BotMemoryProjectionEntry(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str
    revision: int
    scope: Literal['bot_private']


class BotMemoryProjection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scope: Literal['bot_private']
    revision: int
    entries: list[BotMemoryProjectionEntry]


class MemoryProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: int
    room_revision: int
    agent_revision: int
    context_after_seq: int
    legacy_disabled: bool
    entries: list[MemoryProjectionEntry]
    serialized_chars: int
    bot_private: BotMemoryProjection | None = None


class AgentContextPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())
    agent_id: int
    agent_name: str
    model_id: str | None
    projection_status: Literal["ready", "unavailable"]
    error_code: PreviewErrorCode | None
    system_text: str
    messages: list[PreviewMessage]
    source_coverage: SourceCoverage
    truncations: list[ContextTruncation]
    non_text: list[NonTextSource]
    budget: CharacterBudget
    memory_projection: MemoryProjection | None = None


class ContextPreviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    room_id: int
    projected_seq: int
    selected_agent_ids: list[int]
    fingerprint: str
    agents: list[AgentContextPreview]
