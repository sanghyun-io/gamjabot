"""Closed public DTOs for the built-in provider catalog."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator


ProtocolId = Literal[
    "openai_chat_completions", "openai_responses", "anthropic_messages", "gemini_generate_content",
    "azure_openai", "aws_bedrock_converse", "vertex_generate_content",
]
AuthMethodId = Literal[
    "api_key", "local_none", "oauth_browser", "oauth_device", "native_chain",
]
CredentialState = Literal[
    "missing", "authorizing", "present", "refresh_required", "rejected",
    "locked", "unavailable", "not_required",
]
PublicValue = StrictStr | StrictBool


class PublicFieldDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    field_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    kind: Literal["text", "url", "boolean", "enum", "region", "project", "location", "deployment"]
    required: bool
    max_length: int | None = Field(default=None, ge=1, le=2048)


class AuthMethodDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    method_id: AuthMethodId
    code_state: Literal["implemented", "absent"]
    delivery_phase: Literal["D1", "D2", "D3", "out_of_scope"]
    external_eligibility: Literal["verified", "unknown", "unsupported"]
    selectable: bool
    evidence_ref: str | None = Field(default=None, max_length=500)
    evidence_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class ModelSourceDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    strategy: Literal["configured", "discovery", "native_catalog"]
    manual_id: bool
    static_model_ids: tuple[str, ...] = Field(default=(), max_length=64)
    static_model_protocols: dict[str, ProtocolId] = Field(default_factory=dict, max_length=64)
    static_evidence_ref: str | None = Field(default=None, max_length=500)
    static_evidence_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class ProviderDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())
    provider_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")
    label_key: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_.-]*$")
    icon: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")
    protocols: tuple[ProtocolId, ...] = Field(min_length=1, max_length=8)
    fields: tuple[PublicFieldDescriptor, ...] = Field(max_length=16)
    model_source: ModelSourceDescriptor
    methods: tuple[AuthMethodDescriptor, ...] = Field(min_length=1, max_length=8)
    default_base_url: str | None = Field(default=None, max_length=2048)
    technical_compatibility: Literal["supported", "unknown"] = "supported"
    usage_classification: Literal["verified", "unverified", "unknown"] = "verified"
    usage_scope: Literal["general", "personal_interactive"] = "general"


class ProviderCatalogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1]
    providers: tuple[ProviderDescriptor, ...]


class PublicConfigEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1]
    provider_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")
    protocol_id: ProtocolId
    auth_method_id: AuthMethodId
    fields: dict[str, PublicValue] = Field(max_length=16)

    @field_validator("fields")
    @classmethod
    def bounded_fields(cls, value: dict[str, PublicValue]) -> dict[str, PublicValue]:
        for field_id, item in value.items():
            if not field_id or len(field_id) > 64 or not field_id.replace("_", "a").isalnum():
                raise ValueError("Invalid public field name")
            if isinstance(item, str) and len(item) > 2048:
                raise ValueError("Public field is too long")
        return value


class ValidatedPublicConfig(PublicConfigEnvelope):
    pass


class NormalizedConnection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider_id: str
    protocol_id: ProtocolId
    auth_method_id: AuthMethodId
    public_config_version: Literal[1]
    public_config: dict[str, PublicValue]
    auth_grant_revision: int = Field(ge=0)
    credential_state: CredentialState
