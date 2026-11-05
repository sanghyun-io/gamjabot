from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.schemas.owner_skills import SkillCreate

class StartDemonstration(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    agent_id: int = Field(gt=0)
    profile_id: str = Field(min_length=32, max_length=32)
    profile_revision: int = Field(ge=1)
    goal: str = Field(min_length=1, max_length=500)
    include_target_labels: bool = False

    @field_validator('goal')
    @classmethod
    def nonblank(cls, value):
        if not value.strip(): raise ValueError('Must not be blank')
        return value

class SaveDemonstration(SkillCreate):
    pass

class DraftPreflight(BaseModel):
    review_revision: int = Field(ge=1)
    model_config = ConfigDict(extra='forbid', strict=True)
    agent_id: int = Field(gt=0)
    reviewed_goal: str = Field(min_length=1, max_length=500)
    reviewed_steps: list[str] = Field(min_length=1, max_length=100)
    reviewed_rules: str = Field(max_length=3000)

    @field_validator('reviewed_goal')
    @classmethod
    def goal_nonblank(cls, value):
        if not value.strip(): raise ValueError('Must not be blank')
        return value

    @field_validator('reviewed_steps')
    @classmethod
    def bounded_steps(cls, value):
        if any(not x.strip() or len(x)>500 for x in value) or sum(map(len,value))>12000:
            raise ValueError('Invalid reviewed steps')
        return value


class DraftSubmission(DraftPreflight):
    consent: bool
    preflight_token: str = Field(pattern=r'^[a-f0-9]{64}$')

    @field_validator('consent')
    @classmethod
    def explicit_consent(cls, value):
        if value is not True: raise ValueError('Consent is required')
        return value

class DemonstrationReview(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_revision: int = Field(ge=1)
    goal: str = Field(min_length=1, max_length=500)
    steps: list[str] = Field(max_length=100)
    rules: str = Field(max_length=3000)
    fields: SkillCreate | None = None

    @field_validator('goal')
    @classmethod
    def goal_nonblank(cls, value):
        if not value.strip(): raise ValueError('Must not be blank')
        return value

    @field_validator('steps')
    @classmethod
    def steps_bounded(cls, value):
        if any(len(x)>500 for x in value) or sum(map(len,value))>12000: raise ValueError('Steps too large')
        return value
