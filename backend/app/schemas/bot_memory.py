"""Owner-authored facts, preferences and role context; never execution grants."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator
from app.schemas.room_result import UUID_PATTERN


class BotMemoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class BotMemorySource(BotMemoryModel):
    kind: Literal["manual", "event", "link"] = "manual"
    room_id: StrictInt | None = Field(None, ge=1)
    event_id: StrictStr | None = Field(None, pattern=UUID_PATTERN)
    seq: StrictInt | None = Field(None, ge=1)
    url: StrictStr | None = Field(None, min_length=1, max_length=2048)

    @model_validator(mode="after")
    def exact_source(self):
        event_values = (self.room_id, self.event_id, self.seq)
        if (self.kind == "manual" and any(value is not None for value in (*event_values, self.url))
                or self.kind == "event" and (any(value is None for value in event_values) or self.url is not None)
                or self.kind == "link" and (self.url is None or any(value is not None for value in event_values))):
            raise ValueError("Choose one exact source or explicit manual input")
        return self


class BotMemoryWrite(BotMemoryModel):
    expected_scope_revision: StrictInt = Field(ge=0)
    category: Literal["fact", "preference", "role"]
    content: StrictStr = Field(min_length=1, max_length=2000)
    source: BotMemorySource = Field(default_factory=BotMemorySource)


class BotMemoryEdit(BotMemoryWrite):
    expected_revision: StrictInt = Field(ge=1)


class BotMemoryDelete(BotMemoryModel):
    expected_scope_revision: StrictInt = Field(ge=0)
    expected_revision: StrictInt = Field(ge=1)
