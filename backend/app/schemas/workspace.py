"""Strict versioned wire schema for non-secret workspace blueprints."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from app.schemas.provider_catalog import PublicConfigEnvelope
from app.schemas.provider_models import MODEL_ID_PATTERN, valid_model_id


Ref = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
ProviderKind = Literal["openai", "anthropic", "gemini", "xai", "compatible"]
Capability = Literal["max_tokens", "temperature", "tools", "vision"]


def _trimmed(value: object, minimum: int, maximum: int, label: str) -> object:
    if not isinstance(value, str):
        return value
    value = value.strip()
    if not minimum <= len(value) <= maximum:
        raise ValueError(f"{label} has an invalid length")
    return value


class BlueprintConnectionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())
    ref: Ref
    provider_kind: ProviderKind
    label: str
    default_model_id: str = Field(max_length=200)
    base_url: str | None = Field(default=None, max_length=2048)
    allow_local_http: StrictBool = False
    auth_required: StrictBool = True
    enabled: StrictBool = True
    capabilities: dict[Capability, StrictBool] = Field(default_factory=dict, max_length=4)

    @field_validator("label", mode="before")
    @classmethod
    def valid_label(cls, value: object) -> object:
        return _trimmed(value, 1, 100, "Connection label")

    @field_validator("default_model_id")
    @classmethod
    def valid_default_model(cls, value: str) -> str:
        if value and not valid_model_id(value):
            raise ValueError("Default model ID is invalid")
        return value


class BlueprintConnectionV2(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())
    ref: Ref
    label: str
    default_model_id: str = Field(max_length=200)
    enabled: StrictBool = True
    capabilities: dict[Capability, StrictBool] = Field(default_factory=dict, max_length=4)
    public_config: PublicConfigEnvelope

    @field_validator("label", mode="before")
    @classmethod
    def valid_label(cls, value: object) -> object:
        return _trimmed(value, 1, 100, "Connection label")

    @field_validator("default_model_id")
    @classmethod
    def valid_default_model(cls, value: str) -> str:
        if value and not valid_model_id(value):
            raise ValueError("Default model ID is invalid")
        return value


class BlueprintAgent(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())
    ref: Ref
    name: str
    description: str = Field(min_length=1, max_length=10_000)
    system_prompt: str = Field(min_length=1, max_length=65_536)
    connection_ref: Ref
    model_id: str = Field(min_length=1, max_length=200,
                          pattern=MODEL_ID_PATTERN)
    temperature: str = Field(pattern=r"^(auto|(?:[0-1](?:\.\d+)?|2(?:\.0+)?))$")
    max_tokens: int = Field(ge=0, le=100_000, strict=True)

    @field_validator("name", mode="before")
    @classmethod
    def valid_name(cls, value: object) -> object:
        return _trimmed(value, 1, 100, "Agent name")


class BlueprintRoomTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: Ref
    name: str
    room_title: str
    agent_refs: list[Ref] = Field(max_length=64)
    max_rounds: int = Field(ge=1, le=4, strict=True)
    max_tool_calls: int = Field(ge=0, le=64, strict=True)
    max_executor_seconds: int = Field(ge=1, le=3600, strict=True)

    @field_validator("name", mode="before")
    @classmethod
    def valid_name(cls, value: object) -> object:
        return _trimmed(value, 1, 100, "Template name")

    @field_validator("room_title", mode="before")
    @classmethod
    def valid_title(cls, value: object) -> object:
        return _trimmed(value, 1, 200, "Room title")

    @field_validator("agent_refs")
    @classmethod
    def unique_agents(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("Template Agent references must be unique")
        return value


class BlueprintProject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: Ref
    name: str
    description: str | None = Field(default=None, max_length=1000)
    agent_refs: list[Ref] = Field(max_length=256)
    room_templates: list[BlueprintRoomTemplate] = Field(max_length=512)

    @field_validator("name", mode="before")
    @classmethod
    def valid_name(cls, value: object) -> object:
        return _trimmed(value, 1, 100, "Project name")

    @field_validator("agent_refs")
    @classmethod
    def unique_agents(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("Project Agent references must be unique")
        return value


def _validate_references(blueprint):
    connection_refs = [item.ref for item in blueprint.connections]
    agent_refs = [item.ref for item in blueprint.agents]
    project_refs = [item.ref for item in blueprint.projects]
    template_refs = [item.ref for project in blueprint.projects for item in project.room_templates]
    if any(len(items) != len(set(items)) for items in
           (connection_refs, agent_refs, project_refs, template_refs)):
        raise ValueError("Blueprint references must be unique within their kind")
    connection_set, agent_set = set(connection_refs), set(agent_refs)
    if any(agent.connection_ref not in connection_set for agent in blueprint.agents):
        raise ValueError("Agent connection reference is unavailable")
    for project in blueprint.projects:
        if any(reference not in agent_set for reference in project.agent_refs):
            raise ValueError("Project Agent reference is unavailable")
        if any(reference not in agent_set for template in project.room_templates
               for reference in template.agent_refs):
            raise ValueError("Template Agent reference is unavailable")
    if len(template_refs) > 512:
        raise ValueError("Blueprint contains too many room templates")
    return blueprint


class WorkspaceBlueprintV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    connections: list[BlueprintConnectionV1] = Field(max_length=64)
    agents: list[BlueprintAgent] = Field(max_length=256)
    projects: list[BlueprintProject] = Field(max_length=128)

    @model_validator(mode="after")
    def valid_references(self):
        return _validate_references(self)


class WorkspaceBlueprintV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[2]
    connections: list[BlueprintConnectionV2] = Field(max_length=64)
    agents: list[BlueprintAgent] = Field(max_length=256)
    projects: list[BlueprintProject] = Field(max_length=128)

    @model_validator(mode="after")
    def valid_references(self):
        return _validate_references(self)


WorkspaceBlueprint = Annotated[
    WorkspaceBlueprintV1 | WorkspaceBlueprintV2,
    Field(discriminator="version"),
]


class WorkspaceApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    blueprint: WorkspaceBlueprint
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
