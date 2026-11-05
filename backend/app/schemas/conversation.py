"""
Conversation schemas
"""
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Optional, List
from datetime import datetime
from app.schemas.work_state import WorkState
from app.schemas.event import ReplyTo


class ConversationCreate(BaseModel):
    """Schema for creating a conversation"""
    project_id: int
    title: str = Field(..., min_length=1, max_length=200)


class ConversationResponse(BaseModel):
    """Schema for conversation response"""
    id: int
    user_id: int
    project_id: int
    title: str
    is_active: bool
    created_at: datetime
    updated_at: Optional[datetime]
    
    class Config:
        from_attributes = True


class ConversationTitleWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str

    @field_validator("title", mode="before")
    @classmethod
    def trim_title(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        value = value.strip()
        if not 1 <= len(value) <= 200:
            raise ValueError("Title must contain 1 to 200 characters")
        return value


class ConversationCloneWrite(ConversationTitleWrite):
    project_id: int = Field(gt=0, strict=True)


class MessageCreate(BaseModel):
    """Schema for creating a message"""
    content: str = Field(..., min_length=1)


class MessageResponse(BaseModel):
    """Schema for message response"""
    id: int
    conversation_id: int
    agent_id: Optional[int]
    role: str
    content: str
    created_at: datetime
    
    class Config:
        from_attributes = True


class RoomMemberResponse(BaseModel):
    room_id: int
    agent_id: int
    is_active: bool
    joined_at: datetime
    paused: bool = False
    run_state: str = "idle"
    work_state: WorkState | None = None

    resume_status: str | None = None

    class Config:
        from_attributes = True


class RoomRunResponse(BaseModel):
    room_id: int
    agent_id: int
    state: str
    paused: bool
    generation: int

    class Config:
        from_attributes = True


class RoomDraftWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(max_length=65_536)
    expected_version: int = Field(ge=0)
    reply_to: Optional[ReplyTo] = None


class RoomDraftResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    content: str
    version: int
    idempotency_key: Optional[str]
    updated_at: Optional[datetime]
    reply_to: Optional[ReplyTo] = None


class RoomReadStateWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    last_read_seq: int = Field(ge=0, strict=True)


class RoomReadStateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    last_read_seq: int
    updated_at: Optional[datetime]
