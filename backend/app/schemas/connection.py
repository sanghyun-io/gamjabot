from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.provider_catalog import AuthMethodId, ProtocolId, PublicValue
from app.schemas.provider_models import MODEL_ID_PATTERN

ProviderKind = Literal['openai', 'anthropic', 'gemini', 'xai', 'compatible']
ConnectionTestProfile = Literal['text', 'full']


class ConnectionTestPreview(BaseModel):
    profile: ConnectionTestProfile
    model_id: str = Field(min_length=1, max_length=200, pattern=MODEL_ID_PATTERN)
    max_request_count: int = Field(ge=1, le=7)


class ConnectionCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider_kind: ProviderKind
    label: str = Field(min_length=1, max_length=100)
    default_model_id: str = Field(min_length=1, max_length=200, pattern=MODEL_ID_PATTERN)
    base_url: str | None = None
    allow_local_http: bool = False
    auth_required: bool = True
    api_key: str | None = Field(default=None, repr=False)
    enabled: bool = True
    capabilities: dict[Literal['max_tokens', 'temperature', 'tools', 'vision'], bool] = Field(default_factory=dict)


class ConnectionUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    label: str | None = Field(default=None, min_length=1, max_length=100)
    default_model_id: str | None = Field(default=None, min_length=1, max_length=200, pattern=MODEL_ID_PATTERN)
    base_url: str | None = None
    allow_local_http: bool | None = None
    auth_required: bool | None = None
    api_key: str | None = Field(default=None, repr=False)
    enabled: bool | None = None
    capabilities: dict[Literal['max_tokens', 'temperature', 'tools', 'vision'], bool] | None = None


class VersionedConnectionCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: Literal[1]
    provider_id: str = Field(min_length=1, max_length=64, pattern=r'^[a-z][a-z0-9-]*$')
    protocol_id: ProtocolId
    auth_method_id: AuthMethodId
    fields: dict[str, PublicValue] = Field(max_length=16)
    label: str = Field(min_length=1, max_length=100)
    default_model_id: str = Field(min_length=1, max_length=200, pattern=MODEL_ID_PATTERN)
    api_key: str | None = Field(default=None, min_length=1, max_length=32_768, repr=False)
    enabled: bool = True
    capabilities: dict[Literal['max_tokens', 'temperature', 'tools', 'vision'], bool] = Field(default_factory=dict)


class VersionedConnectionUpdate(VersionedConnectionCreate):
    label: str | None = Field(default=None, min_length=1, max_length=100)
    default_model_id: str | None = Field(default=None, min_length=1, max_length=200, pattern=MODEL_ID_PATTERN)
    enabled: bool | None = None
    capabilities: dict[Literal['max_tokens', 'temperature', 'tools', 'vision'], bool] | None = None


class DisconnectRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: Literal[1]
    revoke_remote: bool


class DisconnectResult(BaseModel):
    model_config = ConfigDict(extra='forbid')
    credential_state: Literal['missing']
    revoke_state: Literal[
        'revoked', 'skipped', 'not_supported', 'already_invalid',
        'remote_revoke_failed',
    ]


class ConnectionRevisions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: Literal[1]
    config_revision: int = Field(ge=1)
    auth_grant_revision: int = Field(ge=0)
