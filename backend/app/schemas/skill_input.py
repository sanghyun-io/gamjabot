from pydantic import BaseModel, ConfigDict, Field, field_validator
from uuid import UUID
import re

class SkillInputPreview(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    bot_id: int = Field(gt=0)
    skill_id: str
    skill_revision: int = Field(gt=0)
    binding_revision: int = Field(gt=0)
    question: str = Field(min_length=1, max_length=4000)
    values: dict[str, str]

    @field_validator('skill_id')
    @classmethod
    def canonical(cls, value):
        try:
            if str(UUID(value)) != value: raise ValueError()
        except (ValueError, TypeError, AttributeError): raise ValueError('Invalid skill ID') from None
        return value

    @field_validator('question')
    @classmethod
    def nonblank(cls, value):
        if not value.strip(): raise ValueError('Question required')
        return value

    @field_validator('values')
    @classmethod
    def bounded_values(cls, value):
        if len(value)>16 or any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,31}', k) or len(v.encode('utf-8'))>500 for k,v in value.items()) or sum(len(v.encode('utf-8')) for v in value.values())>4000:
            raise ValueError('Invalid input values')
        return value

class SkillInputSend(SkillInputPreview):
    basis: str = Field(pattern=r'^[a-f0-9]{64}$')
    consent: bool

    @field_validator('consent')
    @classmethod
    def consent_required(cls, value):
        if value is not True: raise ValueError('Consent required')
        return value
