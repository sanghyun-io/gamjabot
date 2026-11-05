from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.provider_models import MODEL_ID_PATTERN


class StarterAgentIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())
    role: Literal["proposer", "reviewer"]
    name: str = Field(min_length=1, max_length=100)
    connection_id: int = Field(gt=0)
    model_id: str = Field(min_length=1, max_length=200, pattern=MODEL_ID_PATTERN)

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name is empty")
        return value


class StarterIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_title: str = Field(min_length=1, max_length=100)
    room_title: str = Field(min_length=1, max_length=200)
    agents: list[StarterAgentIntent] = Field(min_length=2, max_length=2)

    @field_validator("project_title", "room_title")
    @classmethod
    def trim_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title is empty")
        return value

    @model_validator(mode="after")
    def exact_roles(self):
        if {agent.role for agent in self.agents} != {"proposer", "reviewer"}:
            raise ValueError("agents must contain proposer and reviewer exactly once")
        return self


class StarterApply(StarterIntent):
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class StarterPlanAgent(BaseModel):
    role: Literal["proposer", "reviewer"]
    requested_name: str
    name: str
    connection_id: int
    model_id: str
    test_history_id: int
    config_revision: int
    config_fingerprint: str


class StarterPreview(BaseModel):
    plan_hash: str
    project_title: str
    room_title: str
    agents: list[StarterPlanAgent]
    policy: dict[Literal["max_rounds", "max_tool_calls", "max_executor_seconds"], int]


class StarterAgentIds(BaseModel):
    proposer: int
    reviewer: int


class StarterCommitted(BaseModel):
    project_id: int
    room_id: int
    agents: StarterAgentIds


class StarterResult(StarterCommitted):
    replayed: bool
