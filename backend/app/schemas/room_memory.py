"""Strict Owner commands; provenance, authors and timestamps are server derived."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator
from app.schemas.room_result import UUID_PATTERN

class MemoryModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

class MemorySource(MemoryModel):
    kind: Literal['manual','event','link'] = 'manual'
    event_id: StrictStr | None = Field(None, pattern=UUID_PATTERN)
    seq: StrictInt | None = Field(None, ge=1)
    url: StrictStr | None = Field(None, min_length=1, max_length=2048)
    @model_validator(mode='after')
    def exact_shape(self):
        if ((self.kind=='manual' and any(x is not None for x in (self.event_id,self.seq,self.url)))
            or (self.kind=='event' and (self.event_id is None or self.seq is None or self.url is not None))
            or (self.kind=='link' and (self.url is None or self.event_id is not None or self.seq is not None))):
            raise ValueError('Choose one exact source or explicit manual input')
        return self

class MemoryWrite(MemoryModel):
    expected_room_revision: StrictInt = Field(ge=0)
    category: Literal['fact','decision','requirement']
    content: StrictStr = Field(min_length=1, max_length=2000)
    scope: Literal['room','agent']
    target_agent_id: StrictInt | None = Field(None, ge=1)
    source: MemorySource = Field(default_factory=MemorySource)
    @model_validator(mode='after')
    def scope_target(self):
        if (self.scope=='room') != (self.target_agent_id is None):
            raise ValueError('Room scope has no Agent; Agent scope needs one Agent')
        return self

class MemoryEdit(MemoryWrite):
    expected_revision: StrictInt = Field(ge=1)

class MemoryDelete(MemoryModel):
    expected_room_revision: StrictInt = Field(ge=0)
    expected_revision: StrictInt = Field(ge=1)

class MemoryLegacy(MemoryModel):
    expected_room_revision: StrictInt = Field(ge=0)
    expected_agent_revision: StrictInt = Field(ge=0)
    expected_legacy_digest: StrictStr = Field(pattern=r'^[0-9a-f]{64}$')
    operation: Literal['disable','replace']
    replacement: MemoryWrite | None = None
    @model_validator(mode='after')
    def replacement_shape(self):
        if (self.operation=='replace') != (self.replacement is not None):
            raise ValueError('Replacement required only for replace')
        return self
