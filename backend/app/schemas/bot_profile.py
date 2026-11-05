"""Explicit primary selection and creation-free reads; no model dispatch inputs."""
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, field_validator

PositiveId = Annotated[int, Field(strict=True, gt=0)]


class BotPrimaryWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: PositiveId | None
    expected_revision: int = Field(strict=True, ge=0)


class BotConversationResolve(BaseModel):
    model_config = ConfigDict(extra="forbid")
    replace_closed_room_id: PositiveId | None = None


class BotMemoryImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_primary_revision: int = Field(strict=True, ge=0)
    expected_agent_id: PositiveId
    source_label: str = Field(strict=True, min_length=1, max_length=120)
    content: str = Field(strict=True, min_length=1, max_length=12000)

    @field_validator("source_label", "content", mode="before")
    @classmethod
    def trim_text(cls, value):
        return value.strip() if isinstance(value, str) else value
