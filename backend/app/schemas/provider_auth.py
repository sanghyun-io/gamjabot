"""Closed, non-secret contracts for provider auth operations."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


Hex64 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
OperationKind = Literal["auth", "disconnect", "grant_replace", "same_grant_refresh"]
OperationState = Literal[
    "prepared", "awaiting_user", "polling", "exchanging", "committing", "recovering",
    "succeeded", "cancelled", "expired", "interrupted", "failed",
]
OperationPhase = Literal[
    "none", "recovery_preparing", "old_retained", "old_delete_pending",
    "db_commit_pending", "rollback_pending", "reconcile_pending",
    "committed_cleanup_pending", "terminal",
]
NonterminalPhase = Literal[
    "none", "recovery_preparing", "old_retained", "old_delete_pending",
    "db_commit_pending", "rollback_pending", "reconcile_pending",
    "committed_cleanup_pending",
]
TerminalState = Literal["succeeded", "cancelled", "expired", "interrupted", "failed"]
RevokeState = Literal[
    "pending", "revoked", "skipped", "not_supported", "already_invalid",
    "remote_revoke_failed",
]
ConfirmedRevokeOutcome = Literal[
    "revoked", "not_supported", "already_invalid", "remote_revoke_failed",
]
SafeErrorCode = Literal[
    "auth_method_unavailable", "configuration_changed", "idempotency_conflict",
    "auth_operation_busy", "owner_epoch_unavailable", "owner_epoch_changed",
    "credential_store_unavailable", "credential_recovery_required",
    "credential_cleanup_unconfirmed", "provider_authorization_denied",
    "provider_temporary_failure", "provider_invalid_response", "operation_cancelled",
    "operation_expired", "operation_interrupted", "internal_error",
]
PublicState = Literal[
    "prepared", "awaiting_user", "polling", "exchanging", "committing", "recovering",
    "pending", "finalizing", "succeeded", "cancelled", "expired", "interrupted",
    "failed",
]
RestoredCredentialState = Literal[
    "authorizing", "present", "refresh_required", "rejected", "locked",
    "unavailable",
]
PublicRevokeState = Literal[
    "revoked", "skipped", "not_supported", "already_invalid",
    "remote_revoke_failed",
]


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuthStartRequest(ClosedModel):
    version: Literal[1]
    config_revision: int = Field(gt=0)
    auth_grant_revision: int = Field(ge=0)


class AuthMethodAvailability(ClosedModel):
    method_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    available: bool


class AuthMethodsResponse(ClosedModel):
    version: Literal[1] = 1
    methods: tuple[AuthMethodAvailability, ...]


class AuthOperationIntent(ClosedModel):
    operation_id: Hex64
    owner_id: int = Field(gt=0)
    connection_id: int = Field(gt=0)
    kind: OperationKind
    intent_fingerprint: Hex64
    before_state_fingerprint: Hex64
    current_generation_fingerprint: Hex64
    revoke_requested: bool = False
    expires_at: datetime | None = None


class OperationBinding(ClosedModel):
    operation_id: Hex64
    owner_id: int = Field(gt=0)
    connection_id: int = Field(gt=0)
    intent_fingerprint: Hex64
    current_generation_fingerprint: Hex64


class TerminalOutcome(ClosedModel):
    state: TerminalState
    error_code: SafeErrorCode | None
    revoke_state: RevokeState

    @model_validator(mode="after")
    def validate_terminal_truth(self):
        if self.state == "succeeded" and self.error_code is not None:
            raise ValueError("successful operation cannot have an error code")
        if self.state != "succeeded" and self.error_code is None:
            raise ValueError("non-success terminal operation requires a safe error code")
        if self.state != "succeeded" and self.revoke_state != "skipped":
            raise ValueError("non-success terminal operation cannot claim remote revoke")
        return self


class DurableOperation(ClosedModel):
    operation_id: Hex64
    owner_id: int = Field(gt=0)
    connection_id: int = Field(gt=0)
    kind: OperationKind
    state: OperationState
    phase: OperationPhase
    idempotency_key_hash: Hex64 | None
    server_replay_discriminator_hash: Hex64 | None
    intent_fingerprint: Hex64
    before_state_fingerprint: Hex64
    current_generation_fingerprint: Hex64
    revoke_requested: bool
    revoke_attempt_started_at: datetime | None
    revoke_state: RevokeState
    attempt_count: int = Field(ge=0)
    next_attempt_at: datetime | None
    error_code: SafeErrorCode | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    terminal_at: datetime | None
    replayed: bool = False


class AuthPublicState(ClosedModel):
    version: Literal[1]
    operation_id: Hex64
    state: PublicState
    expires_at: datetime | None = None
    browser_url: HttpUrl | None = Field(default=None, max_length=2048)
    verification_uri: HttpUrl | None = Field(default=None, max_length=2048)
    user_code: str | None = Field(default=None, min_length=1, max_length=64)
    poll_interval: int | None = Field(default=None, ge=1, le=300)
    error_code: SafeErrorCode | None = None

    @model_validator(mode="after")
    def validate_method_instruction(self):
        device = (self.verification_uri, self.user_code, self.poll_interval)
        if any(value is not None for value in device) and not all(
                value is not None for value in device):
            raise ValueError("device instruction fields must be complete")
        if self.browser_url is not None and any(value is not None for value in device):
            raise ValueError("browser and device instructions are mutually exclusive")
        return self


class AuthOperationResult(ClosedModel):
    credential_state: Literal["present"]


class AuthOperationTerminal(ClosedModel):
    version: Literal[1] = 1
    operation_id: Hex64
    state: Literal["succeeded", "cancelled", "expired", "interrupted", "failed"]
    result: AuthOperationResult | None = None
    error_code: SafeErrorCode | None = None

    @model_validator(mode="after")
    def validate_terminal_truth(self):
        if self.state == "succeeded":
            if self.result is None or self.error_code is not None:
                raise ValueError("successful auth requires confirmed present state")
        elif self.result is not None or self.error_code is None:
            raise ValueError("failed auth exposes only a safe error")
        return self


class DisconnectPublicResult(ClosedModel):
    credential_state: Literal["missing"] | RestoredCredentialState
    revoke_state: PublicRevokeState


class DisconnectOperationPending(ClosedModel):
    version: Literal[1] = 1
    operation_id: Hex64
    state: Literal["pending", "finalizing"]
    expires_at: Literal[None] = None
    poll_interval: int = Field(default=1, ge=1, le=300)


class DisconnectOperationTerminal(ClosedModel):
    version: Literal[1] = 1
    operation_id: Hex64
    state: Literal["succeeded", "failed", "cancelled", "expired", "interrupted"]
    result: DisconnectPublicResult
    error_code: SafeErrorCode | None = None

    @model_validator(mode="after")
    def validate_confirmed_terminal_truth(self):
        if self.state == "succeeded":
            if self.error_code is not None or self.result.credential_state != "missing":
                raise ValueError("successful disconnect requires confirmed missing state")
        elif self.error_code is None or self.result.credential_state == "missing":
            raise ValueError("non-success disconnect requires exact restored state")
        if self.state != "succeeded" and self.result.revoke_state != "skipped":
            raise ValueError("restored disconnect cannot claim remote revoke")
        return self
