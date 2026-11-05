"""Strict atomic custom-room launch input."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator


StrictId = Annotated[int, Field(gt=0, strict=True)]


class RoomLaunchWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: StrictId
    title: str
    agent_ids: list[StrictId] = Field(max_length=64)
    max_rounds: int = Field(ge=1, le=4, strict=True)
    max_tool_calls: int = Field(ge=0, le=64, strict=True)
    max_executor_seconds: int = Field(ge=1, le=3600, strict=True)

    @field_validator("title", mode="before")
    @classmethod
    def valid_title(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        value = value.strip()
        if not 1 <= len(value) <= 200:
            raise ValueError("Room title must contain 1 to 200 characters")
        return value

    @field_validator("agent_ids")
    @classmethod
    def unique_agents(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("Agent IDs must be unique")
        return sorted(value)
