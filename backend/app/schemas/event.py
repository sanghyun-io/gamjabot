"""Canonical room event wire shapes."""

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class ReplyTo(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    event_id:str=Field(min_length=36,max_length=36)
    seq:int=Field(gt=0)

class PageTarget(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    result_id:str=Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
    version:int=Field(gt=0)

class PageSelection(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    index:int=Field(ge=0,le=255)
    type:Literal['heading','markdown']
    text:str=Field(min_length=1,max_length=1500)
    sha256:str=Field(pattern=r'^[0-9a-f]{64}$')

class RoutineReference(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    agent_id:int=Field(gt=0)
    routine_id:str=Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
    revision:int=Field(gt=0)

class MessageEventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1)
    output_kind: Literal["auto","text","image_generation","speech_generation"] = "auto"
    reply_to: ReplyTo | None = None
    page_target: PageTarget | None = None
    page_selection: PageSelection | None = None
    routine_ref: RoutineReference | None = None


class RoomEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    event_id: str
    device_id: str | None
    room_id: int
    seq: int
    actor_type: Literal["user", "agent", "tool", "system"]
    actor_id: int | None
    actor_name_snapshot: str
    kind: Literal["message", "tool_result", "failure", "summary", "activity"]
    payload: dict[str, Any]
    created_at: datetime
    idempotency_key: str | None

    @field_serializer("created_at")
    def utc_created_at(self, value: datetime) -> str:
        return value.replace(tzinfo=timezone.utc).isoformat() if value.tzinfo is None else value.astimezone(timezone.utc).isoformat()
