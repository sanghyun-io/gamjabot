"""The model supplies role text, never credentials, execution origins or model bindings."""
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator


class BotCreateArguments(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    name: StrictStr=Field(min_length=1,max_length=100)
    description: StrictStr=Field(min_length=1,max_length=2000)
    role: StrictStr=Field(min_length=1,max_length=8000)

    @field_validator('name','description','role')
    @classmethod
    def nonblank(cls,value):
        if not value.strip():raise ValueError('Supply nonblank text')
        return value
