"""
Agent schemas
"""
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Optional, Literal
from datetime import datetime

from app.schemas.provider_models import MODEL_ID_PATTERN


class AvatarConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: Literal[1] = 1
    head: int = Field(strict=True, ge=0, lt=10)
    hair: int = Field(strict=True, ge=0, lt=12)
    eyes: int = Field(strict=True, ge=0, lt=12)
    nose: int = Field(strict=True, ge=0, lt=8)
    mouth: int = Field(strict=True, ge=0, lt=12)
    skin: int = Field(strict=True, ge=0, lt=12)
    accent: int = Field(strict=True, ge=0, lt=10)


class AgentCreate(BaseModel):
    """Schema for creating an agent"""
    model_config = ConfigDict(protected_namespaces=())
    avatar: Optional[AvatarConfig] = None
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(..., min_length=1)
    system_prompt: str = Field(..., min_length=1)
    connection_id: int = Field(gt=0)
    model_id: str = Field(min_length=1, max_length=200, pattern=MODEL_ID_PATTERN)
    temperature: str = Field(default="auto", pattern=r'^(auto|(?:[0-1](?:\.\d+)?|2(?:\.0+)?))$')
    max_tokens: int = Field(default=0, ge=0, le=100000)


class AgentUpdate(BaseModel):
    """Schema for updating an agent"""
    model_config = ConfigDict(protected_namespaces=())
    avatar: Optional[AvatarConfig] = None
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, min_length=1)
    system_prompt: Optional[str] = Field(None, min_length=1)
    connection_id: Optional[int] = Field(None, gt=0)
    model_id: Optional[str] = Field(None, min_length=1, max_length=200, pattern=MODEL_ID_PATTERN)
    temperature: Optional[str] = Field(None, pattern=r'^(auto|(?:[0-1](?:\.\d+)?|2(?:\.0+)?))$')
    max_tokens: Optional[int] = Field(None, ge=0, le=100000)


    @field_validator('avatar')
    @classmethod
    def explicit_avatar_must_not_be_null(cls, value):
        if value is None:
            raise ValueError('Supply an avatar recipe')
        return value


class AgentResponse(BaseModel):
    """Schema for agent response"""
    model_config = ConfigDict(protected_namespaces=(), from_attributes=True)
    id: int
    avatar: AvatarConfig
    user_id: int
    name: str
    description: str
    system_prompt: str
    model: str
    connection_id: Optional[int]
    model_id: Optional[str]
    temperature: str
    max_tokens: int
    is_active: bool
    created_at: datetime
    updated_at: Optional[datetime]


class InactiveAgentConnectionResponse(BaseModel):
    """Minimal connection reference; prompts and credentials are excluded."""
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    connection_id: Optional[int]
    model_id: Optional[str]
    model: str
    is_active: bool
    updated_at: Optional[datetime]


class InactiveAgentConnectionPage(BaseModel):
    items: list[InactiveAgentConnectionResponse]
    next_after_id: Optional[int]


class InactiveAgentDetach(BaseModel):
    expected_connection_id: int = Field(gt=0)
    expected_name: str = Field(max_length=1000)
    expected_model_id: Optional[str] = Field(max_length=2000)
    expected_model: str = Field(max_length=2000)
    expected_updated_at: Optional[datetime]
