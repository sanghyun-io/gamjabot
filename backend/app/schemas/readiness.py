from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


CapabilityName = Literal['max_tokens', 'temperature', 'tools', 'vision']
OutcomeCode = Literal[
    'connected', 'missing_secret', 'missing_model', 'secret_store_unavailable',
    'invalid_credential', 'unreachable', 'redirect_refused', 'provider_error',
    'rate_limited', 'quota_exhausted', 'invalid_response', 'connection_disabled',
    'credential_locked', 'configuration_changed',
]
CredentialObservation = Literal['not_required', 'unknown', 'verified', 'rejected', 'missing', 'locked', 'unavailable']


class TestRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: int
    connection_id: int
    tested_at: datetime
    duration_ms: int = Field(ge=0)
    model_id: str | None
    ok: bool
    code: OutcomeCode
    config_fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    config_current: bool
    credential_observation: CredentialObservation
    verified_capabilities: dict[CapabilityName, bool] | None
    profile: Literal['text', 'full'] | None = None
    actual_request_count: int | None = Field(default=None, ge=0)


class TestHistory(BaseModel):
    connection_id: int
    current_config_fingerprint: str
    items: list[TestRecord]
