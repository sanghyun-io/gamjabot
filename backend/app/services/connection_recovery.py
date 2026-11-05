"""Durable connection credential orchestration over the private Core broker.

SQLite stores only safe hashes and lifecycle metadata.  Credential values,
recovery references, owner epochs, commit handles, and binding fingerprints stay
inside this request and the Core credential channel.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from typing import Callable, Literal

from fastapi import HTTPException
from sqlalchemy import delete as sql_delete, text, update as sql_update
from sqlalchemy.orm import Session

from app.models.collaboration import ConnectionModelCache, ProviderConnection
from app.schemas.provider_auth import AuthOperationIntent, OperationBinding, TerminalOutcome
from app.schemas.provider_catalog import PublicConfigEnvelope
from app.services import auth_operations, provider_catalog


LifecycleState = Literal[
    "pending", "finalizing", "succeeded", "failed", "cancelled", "expired",
    "interrupted",
]
RECOVERY_NO_USER_EXPIRY = (1 << 63) - 1
_NATIVE_ENROLLMENT = {
    "azure-openai": ("azure_cli", ("resource_endpoint", "api_version")),
    "amazon-bedrock": ("aws_profile", ("profile_name", "region")),
    "google-vertex": ("vertex_adc", ("project_id", "location")),
}


@dataclass(frozen=True)
class LifecycleResult:
    operation_id: str
    state: LifecycleState
    result: dict | None = None
    error_code: str | None = None

    def safe_dict(self) -> dict:
        value = {"operation_id": self.operation_id, "state": self.state}
        if self.result is not None:
            value["result"] = deepcopy(self.result)
        if self.error_code is not None:
            value["error_code"] = self.error_code
        return value


@dataclass(frozen=True)
class AuthLifecycleResult:
    operation_id: str
    state: str
    expires_at: datetime | None = None
    browser_url: str | None = None
    verification_uri: str | None = None
    user_code: str | None = None
    poll_interval: int | None = None
    result: dict | None = None
    error_code: str | None = None


class CredentialOperationPending(Exception):
    """The connection has durable recovery work and must not be resolved."""

    def __init__(self, operation_id: str):
        self.operation_id = operation_id
        super().__init__("credential_operation_pending")


def _json_value(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _fingerprint(domain: str, value: dict) -> str:
    encoded = json.dumps(
        _json_value(value), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    return sha256(domain.encode("ascii") + b"\0v1\0" + encoded).hexdigest()


def connection_state_projection(db: Session, row: ProviderConnection) -> dict:
    """Return the single canonical DB-before projection used by every R3 path."""
    cached = db.query(ConnectionModelCache).filter_by(
        owner_id=row.user_id, connection_id=row.id).first()
    cache = None if cached is None else {
        "config_revision": cached.config_revision,
        "auth_grant_revision": cached.auth_grant_revision,
        "config_fingerprint": cached.config_fingerprint,
        "observed_at": cached.observed_at,
        "truncated": bool(cached.truncated),
        "models": deepcopy(cached.models or []),
        "updated_at": cached.updated_at,
    }
    return {
        "reference": row.secret_ref,
        "credential_state": row.credential_state,
        "readiness_revision": int(row.readiness_revision or 1),
        "auth_grant_revision": int(row.auth_grant_revision or 0),
        "config": {
            "kind": row.kind,
            "name": row.name,
            "base_url": row.base_url,
            "default_model_id": row.default_model_id,
            "enabled": bool(row.enabled),
            "auth_required": bool(row.auth_required),
            "capabilities": deepcopy(row.capabilities or {}),
            "provider_id": row.provider_id,
            "protocol_id": row.protocol_id,
            "auth_method_id": row.auth_method_id,
            "public_config_version": row.public_config_version,
            "public_config": deepcopy(row.public_config),
        },
        "cache": cache,
    }


def connection_state_fingerprint(db: Session, row: ProviderConnection) -> str:
    return _fingerprint("gamja-connection-before-state", connection_state_projection(db, row))


def _public_config_fingerprint(row: ProviderConnection, auth_method_id: str) -> str:
    return _fingerprint("gamja-provider-public-config", {
        "provider_id": row.provider_id or row.kind,
        "protocol_id": row.protocol_id or row.kind,
        "auth_method_id": auth_method_id,
        "version": row.public_config_version or 1,
        "fields": deepcopy(row.public_config or {}),
    })


def _native_enrollment_spec(row: ProviderConnection, auth_method_id: str) -> dict | None:
    if auth_method_id != "native_chain":
        return None
    selected = _NATIVE_ENROLLMENT.get(row.provider_id or row.kind)
    fields = row.public_config
    if (selected is None or row.public_config_version != 1
            or not isinstance(fields, dict) or set(fields) != set(selected[1])
            or any(not isinstance(fields[key], str) or not fields[key]
                   or fields[key] != fields[key].strip()
                   or len(fields[key].encode("utf-8")) > 2048
                   for key in selected[1])):
        raise auth_operations.AuthOperationError("configuration_changed")
    return {"provider": selected[0], **{key: fields[key] for key in selected[1]}}


def _intent_fingerprint(*, owner_id: int, connection_id: int, kind: str,
                        before_state_fingerprint: str, revoke_requested: bool,
                        generation_fingerprint: str, provider_id: str,
                        auth_method_id: str, config_revision: int,
                        auth_grant_revision: int, public_config_fingerprint: str) -> str:
    return _fingerprint("gamja-connection-operation-intent", {
        "owner_id": owner_id,
        "connection_id": connection_id,
        "kind": kind,
        "before_state_fingerprint": before_state_fingerprint,
        "revoke_requested": revoke_requested,
        "generation_fingerprint": generation_fingerprint,
        "provider_id": provider_id,
        "auth_method_id": auth_method_id,
        "config_revision": config_revision,
        "auth_grant_revision": auth_grant_revision,
        "public_config_fingerprint": public_config_fingerprint,
    })


def _auth_request_fingerprint(*, owner_id: int, connection_id: int,
                              auth_method_id: str, config_revision: int,
                              auth_grant_revision: int) -> str:
    """Stable client intent for keyed replay after a successful revision bump."""
    return _fingerprint("gamja-provider-auth-request", {
        "owner_id": owner_id,
        "connection_id": connection_id,
        "auth_method_id": auth_method_id,
        "config_revision": config_revision,
        "auth_grant_revision": auth_grant_revision,
    })


def assert_connection_resolvable(db: Session, owner_id: int, connection_id: int) -> None:
    operation = auth_operations.blocking_operation(db, owner_id, connection_id)
    if operation is not None:
        raise CredentialOperationPending(operation.operation_id)


def _binding(operation) -> OperationBinding:
    return OperationBinding(
        operation_id=operation.operation_id,
        owner_id=operation.owner_id,
        connection_id=operation.connection_id,
        intent_fingerprint=operation.intent_fingerprint,
        current_generation_fingerprint=operation.current_generation_fingerprint,
    )


def _auth_public(reply):
    """Read only the closed public state from Core's private reply envelope."""
    return reply.safe_projection()


def _public_result(operation, *, credential_state: str | None = None) -> LifecycleResult:
    if operation.terminal_at is None or operation.phase != "terminal":
        state = "finalizing" if operation.phase == "committed_cleanup_pending" else "pending"
        return LifecycleResult(operation.operation_id, state)
    result = None
    if operation.kind == "disconnect":
        result = {
            "credential_state": (
                "missing" if operation.state == "succeeded"
                else credential_state or "unavailable"),
            "revoke_state": operation.revoke_state,
        }
    return LifecycleResult(
        operation.operation_id, operation.state, result=result,
        error_code=operation.error_code,
    )


class ConnectionRecoveryCoordinator:
    """Coordinates one durable row with Core recovery material without secrets."""

    def __init__(self, *, broker, owner_epochs, credential_cache=None,
                 operation_id_factory: Callable[[], str] | None = None,
                 auth_method_availability: Callable[[str, str], bool] | None = None,
                 clock: Callable[[], datetime] | None = None,
                 crash_hook: Callable[[str], None] | None = None):
        self._broker = broker
        self._owner_epochs = owner_epochs
        self._credential_cache = credential_cache
        self._operation_id_factory = operation_id_factory
        self._auth_method_availability = auth_method_availability or (
            lambda _provider_id, _method_id: False)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._crash_hook = crash_hook or (lambda _point: None)

    def available_auth_methods(self, row: ProviderConnection) -> tuple[str, ...]:
        """Return cached launch capability without probing Core/native/provider state."""
        descriptor = provider_catalog.get_descriptor(row.provider_id or row.kind)
        return tuple(
            method.method_id for method in descriptor.methods
            if method.code_state == "implemented"
            and method.external_eligibility == "verified"
            and method.selectable is True
            and self._auth_method_availability(
                descriptor.provider_id, method.method_id) is True
        )

    def start_auth(self, db: Session, row: ProviderConnection, auth_method_id: str,
                   config_revision: int, auth_grant_revision: int, *,
                   idempotency_key: str) -> AuthLifecycleResult:
        """Commit the safe ledger before creating any Core auth workflow."""
        request_fingerprint = _auth_request_fingerprint(
            owner_id=row.user_id, connection_id=row.id,
            auth_method_id=auth_method_id, config_revision=config_revision,
            auth_grant_revision=auth_grant_revision)
        replay = auth_operations.auth_start_preflight(
            db, owner_id=row.user_id, connection_id=row.id,
            intent_fingerprint=request_fingerprint,
            idempotency_key=idempotency_key,
        )
        if replay is not None:
            return self._auth_operation_result(replay)
        try:
            provider_catalog.launchable_auth_method(
                row.provider_id or row.kind, auth_method_id)
        except HTTPException:
            raise auth_operations.AuthOperationError(
                "auth_method_unavailable") from None
        if row.auth_method_id != auth_method_id:
            raise auth_operations.AuthOperationError("configuration_changed")
        if auth_method_id not in self.available_auth_methods(row):
            raise auth_operations.AuthOperationError("auth_method_unavailable")
        if (config_revision != int(row.readiness_revision or 1)
                or auth_grant_revision != int(row.auth_grant_revision or 0)):
            raise auth_operations.AuthOperationError("configuration_changed")
        native_enrollment = _native_enrollment_spec(row, auth_method_id)
        owner_epoch = self._owner_epochs.current(row.user_id)
        if owner_epoch is None:
            raise auth_operations.AuthOperationError("owner_epoch_unavailable")
        generation = self._broker.current_generation_fingerprint
        before = connection_state_fingerprint(db, row)
        operation_id = (self._operation_id_factory() if self._operation_id_factory is not None
                        else self._broker.operation_allocate(row.user_id, owner_epoch))
        expires_at = self._clock() + timedelta(minutes=15)
        operation = self._begin_operation(db, AuthOperationIntent(
            operation_id=operation_id,
            owner_id=row.user_id,
            connection_id=row.id,
            kind="grant_replace" if row.secret_ref else "auth",
            intent_fingerprint=request_fingerprint,
            before_state_fingerprint=before,
            current_generation_fingerprint=generation,
            revoke_requested=False,
            expires_at=expires_at,
        ), idempotency_key)
        self._crash_hook("after_ledger_commit")
        if operation.replayed:
            return self._auth_operation_result(operation)
        binding = self._auth_binding(row, operation, owner_epoch, auth_method_id)
        if row.secret_ref:
            operation = auth_operations.advance_phase(
                db, _binding(operation), expected="none", target="recovery_preparing")
            prepared = self._broker.credential_prepare(
                binding, exact_old_reference=row.secret_ref)
            prepared_public = _auth_public(prepared)
            if prepared_public.state != "prepared":
                self._set_phase(db, operation, "reconcile_pending")
                return self._auth_operation_result(
                    operation, error_code=prepared_public.code)
            operation = auth_operations.advance_phase(
                db, _binding(operation), expected="recovery_preparing",
                target="old_retained")
        public = (self._broker.auth_start(binding, native_enrollment)
                  if native_enrollment is not None
                  else self._broker.auth_start(binding))
        if public.state in {"prepared", "awaiting_user", "polling", "exchanging",
                            "committing", "recovering"}:
            operation = auth_operations.record_auth_state(
                db, _binding(operation), public.state)
            return self._auth_operation_result(operation, public=public)
        terminal_state = {
            "cancelled": "cancelled", "expired": "expired",
            "interrupted": "interrupted", "failed": "failed",
        }.get(public.state, "failed")
        operation = auth_operations.record_auth_terminal_pending(
            db, _binding(operation), state=terminal_state,
            error_code=self._safe_auth_code(public.code) or "internal_error")
        reconciled = self._reconcile_one(db, operation)
        current = auth_operations.operation(
            db, operation.owner_id, operation.operation_id)
        return self._auth_recovery_result(current, reconciled)

    def commit_direct_api_key(self, db: Session, row: ProviderConnection,
                              direct_secret: str) -> LifecycleResult:
        """Durably prepare and commit one Azure API key through Core."""
        if (not isinstance(direct_secret, str) or not direct_secret
                or row.provider_id != "azure-openai"
                or row.protocol_id != "azure_openai"
                or row.auth_method_id != "api_key"
                or row.public_config_version != 1):
            raise auth_operations.AuthOperationError("configuration_changed")
        owner_epoch = self._owner_epochs.current(row.user_id)
        if owner_epoch is None:
            raise auth_operations.AuthOperationError("owner_epoch_unavailable")
        generation = self._broker.current_generation_fingerprint
        before = connection_state_fingerprint(db, row)
        kind = "grant_replace" if row.secret_ref else "auth"
        operation_id = (self._operation_id_factory() if self._operation_id_factory is not None
                        else self._broker.operation_allocate(row.user_id, owner_epoch))
        intent = AuthOperationIntent(
            operation_id=operation_id,
            owner_id=row.user_id,
            connection_id=row.id,
            kind=kind,
            intent_fingerprint=_intent_fingerprint(
                owner_id=row.user_id, connection_id=row.id, kind=kind,
                before_state_fingerprint=before, revoke_requested=False,
                generation_fingerprint=generation, provider_id=row.provider_id,
                auth_method_id=row.auth_method_id,
                config_revision=int(row.readiness_revision or 1),
                auth_grant_revision=int(row.auth_grant_revision or 0),
                public_config_fingerprint=_public_config_fingerprint(row, row.auth_method_id),
            ),
            before_state_fingerprint=before,
            current_generation_fingerprint=generation,
            revoke_requested=False,
            expires_at=self._clock() + timedelta(minutes=15),
        )
        operation = self._begin_operation(db, intent, None)
        self._crash_hook("after_ledger_commit")
        binding = self._auth_binding(row, operation, owner_epoch, row.auth_method_id)
        if operation.replayed:
            return self._reconcile_one(db, operation)
        operation = auth_operations.advance_phase(
            db, _binding(operation), expected="none", target="recovery_preparing")
        prepared = self._broker.credential_prepare(
            binding, exact_old_reference=row.secret_ref, direct_secret=direct_secret)
        public = _auth_public(prepared)
        if public.state != "prepared":
            self._set_phase(db, operation, "reconcile_pending")
            return LifecycleResult(operation.operation_id, "pending", error_code=public.code)
        self._crash_hook("after_direct_prepare")
        if row.secret_ref:
            operation = auth_operations.advance_phase(
                db, _binding(operation), expected="recovery_preparing", target="old_retained")
            return self.commit_prepared_grant(
                db, row, operation=operation, binding=binding,
                target_auth_method_id="api_key", transition="proven_new_grant")
        result = self._commit_initial_auth(db, row, operation, binding)
        return LifecycleResult(
            result.operation_id, result.state, result=result.result,
            error_code=result.error_code)

    @staticmethod
    def _safe_auth_code(code: str | None) -> str | None:
        return {
            None: None,
            "owner_mismatch": "owner_epoch_changed",
            "authorization_expired": "operation_expired",
            "callback_mismatch": "provider_invalid_response",
            "device_slow_down": "provider_temporary_failure",
        }.get(code, code if code in {
            "auth_method_unavailable", "configuration_changed",
            "owner_epoch_unavailable", "owner_epoch_changed",
            "credential_store_unavailable", "credential_recovery_required",
            "credential_cleanup_unconfirmed", "provider_authorization_denied",
            "provider_temporary_failure", "provider_invalid_response",
            "operation_cancelled", "operation_expired", "operation_interrupted",
            "internal_error",
        } else "internal_error")

    def _auth_operation_result(self, operation, *, public=None,
                               error_code: str | None = None) -> AuthLifecycleResult:
        if operation.phase == "terminal" and operation.terminal_at is not None:
            return AuthLifecycleResult(
                operation_id=operation.operation_id,
                state=operation.state,
                expires_at=operation.expires_at,
                result=({"credential_state": "present"}
                        if operation.state == "succeeded" else None),
                error_code=operation.error_code,
            )
        state = operation.state if public is None else public.state
        return AuthLifecycleResult(
            operation_id=operation.operation_id,
            state=state,
            expires_at=operation.expires_at,
            browser_url=(None if public is None else public.authorization_url),
            verification_uri=(None if public is None else public.verification_uri),
            user_code=(None if public is None else public.user_code),
            poll_interval=(None if public is None else public.poll_interval),
            error_code=self._safe_auth_code(
                error_code if public is None else public.code),
        )

    def _auth_recovery_result(self, operation,
                              reconciled: LifecycleResult) -> AuthLifecycleResult:
        """Project recovery truth without reusing a stale Core auth state."""
        if operation.phase == "terminal" and operation.terminal_at is not None:
            return self._auth_operation_result(operation)
        return AuthLifecycleResult(
            operation_id=operation.operation_id,
            state=reconciled.state,
            expires_at=operation.expires_at,
            error_code=self._safe_auth_code(reconciled.error_code),
        )

    def _auth_binding(self, row: ProviderConnection, operation, owner_epoch: str,
                      auth_method_id: str) -> dict:
        return {
            "version": 1,
            "operation_id": operation.operation_id,
            "owner_id": operation.owner_id,
            "connection_id": operation.connection_id,
            "kind": operation.kind,
            "intent_fingerprint": operation.intent_fingerprint,
            "before_state_fingerprint": operation.before_state_fingerprint,
            "revoke_requested": False,
            "expires_at": int(operation.expires_at.timestamp()),
            "owner_epoch": owner_epoch,
            "provider_id": row.provider_id or row.kind,
            "auth_method_id": auth_method_id,
            "config_revision": int(row.readiness_revision or 1),
            "auth_grant_revision": int(row.auth_grant_revision or 0),
            "public_config_fingerprint": _public_config_fingerprint(
                row, auth_method_id),
        }

    def disconnect(self, db: Session, row: ProviderConnection, revoke_remote: bool,
                   *, idempotency_key: str | None = None,
                   remote_revoker=None) -> LifecycleResult:
        """Start and, when possible, finish a durable local disconnect."""
        replay = self._disconnect_replay(
            db, row, revoke_remote=revoke_remote, idempotency_key=idempotency_key)
        if replay is not None:
            return replay
        owner_epoch = self._owner_epochs.current(row.user_id)
        if owner_epoch is None:
            raise auth_operations.AuthOperationError("owner_epoch_unavailable")
        generation = self._broker.current_generation_fingerprint
        before = connection_state_fingerprint(db, row)
        provider_id = row.provider_id or row.kind
        auth_method_id = row.auth_method_id or ("api_key" if row.auth_required else "local_none")
        public_config_fingerprint = _public_config_fingerprint(row, auth_method_id)
        operation_id = (self._operation_id_factory() if self._operation_id_factory is not None
                        else self._broker.operation_allocate(row.user_id, owner_epoch))
        intent = AuthOperationIntent(
            operation_id=operation_id,
            owner_id=row.user_id,
            connection_id=row.id,
            kind="disconnect",
            intent_fingerprint=_intent_fingerprint(
                owner_id=row.user_id, connection_id=row.id, kind="disconnect",
                before_state_fingerprint=before, revoke_requested=revoke_remote,
                generation_fingerprint=generation, provider_id=provider_id,
                auth_method_id=auth_method_id,
                config_revision=int(row.readiness_revision or 1),
                auth_grant_revision=int(row.auth_grant_revision or 0),
                public_config_fingerprint=public_config_fingerprint,
            ),
            before_state_fingerprint=before,
            current_generation_fingerprint=generation,
            revoke_requested=revoke_remote,
        )
        operation = self._begin_operation(db, intent, idempotency_key)
        self._crash_hook("after_ledger_commit")
        if operation.replayed:
            return _public_result(operation)
        binding = {
            "version": 1,
            "operation_id": operation.operation_id,
            "owner_id": operation.owner_id,
            "connection_id": operation.connection_id,
            "kind": operation.kind,
            "intent_fingerprint": operation.intent_fingerprint,
            "before_state_fingerprint": operation.before_state_fingerprint,
            "revoke_requested": operation.revoke_requested,
            "expires_at": RECOVERY_NO_USER_EXPIRY,
            "owner_epoch": owner_epoch,
            "provider_id": provider_id,
            "auth_method_id": auth_method_id,
            "config_revision": int(row.readiness_revision or 1),
            "auth_grant_revision": int(row.auth_grant_revision or 0),
            "public_config_fingerprint": public_config_fingerprint,
        }
        if not row.secret_ref:
            if (row.credential_state == "missing"
                    or (auth_method_id == "local_none"
                        and row.credential_state == "not_required")):
                return self._finish_without_material(
                    db, operation, already_invalid=revoke_remote)
            self._set_phase(db, operation, "reconcile_pending")
            return LifecycleResult(operation.operation_id, "pending",
                                   error_code="configuration_changed")
        operation = auth_operations.advance_phase(
            db, _binding(operation), expected="none", target="recovery_preparing")
        try:
            prepared = self._broker.credential_prepare(
                binding, exact_old_reference=row.secret_ref)
        except Exception:
            return LifecycleResult(operation.operation_id, "pending",
                                   error_code="credential_recovery_required")
        prepared_public = _auth_public(prepared)
        if prepared_public.state != "prepared":
            self._set_phase(db, operation, "reconcile_pending")
            return LifecycleResult(operation.operation_id, "pending",
                                   error_code=prepared_public.code)
        operation = auth_operations.advance_phase(
            db, _binding(operation), expected="recovery_preparing", target="old_retained")
        self._crash_hook("after_recovery_staged")
        try:
            operation = self._commit_disconnect(db, operation)
        except auth_operations.AuthOperationError:
            current = auth_operations.operation(db, operation.owner_id, operation.operation_id)
            self._set_phase(db, current, "reconcile_pending")
            return LifecycleResult(operation.operation_id, "pending",
                                   error_code="configuration_changed")
        self._crash_hook("after_db_commit")
        try:
            cleanup = self._broker.credential_discard_exact(binding, "cleanup_committed")
        except Exception:
            cleanup = None
        cleanup_public = None if cleanup is None else _auth_public(cleanup)
        if cleanup_public is not None and cleanup_public.state != "succeeded":
            return LifecycleResult(operation.operation_id, "finalizing",
                                   error_code=cleanup_public.code)
        self._crash_hook("after_cleanup")
        inspected = self._broker.credential_discard_recovery(
            auth_operations.recovery_selector(operation), "inspect")
        if not self._confirmed(inspected, "cleaned"):
            return LifecycleResult(operation.operation_id, "finalizing",
                                   error_code=inspected.code)
        return self._finish_committed(
            db, operation, inspected, remote_revoker=remote_revoker)

    def reconcile_owner(self, db: Session, owner_id: int, *, limit: int = 25,
                        remote_revoker=None) -> list[LifecycleResult]:
        results = []
        for candidate in auth_operations.nonterminal_candidates(db, owner_id, limit=limit):
            results.append(self._reconcile_one(
                db, candidate, remote_revoker=remote_revoker))
        return results

    def commit_prepared_grant(self, db: Session, row: ProviderConnection, *,
                              operation, binding: dict,
                              target_auth_method_id: str,
                              transition: str) -> LifecycleResult:
        """Commit a Core-prepared winner; Core alone cleans old secure material."""
        owner_epoch = self._owner_epochs.current(row.user_id)
        if owner_epoch is None:
            raise auth_operations.AuthOperationError("owner_epoch_unavailable")
        expected_kind = {
            "proven_new_grant": "grant_replace",
            "same_grant_refresh": "same_grant_refresh",
        }.get(transition)
        if (expected_kind is None or operation.kind != expected_kind
                or operation.owner_id != row.user_id
                or operation.connection_id != row.id
                or binding.get("operation_id") != operation.operation_id
                or binding.get("owner_id") != operation.owner_id
                or binding.get("connection_id") != operation.connection_id
                or binding.get("kind") != operation.kind
                or binding.get("revoke_requested") != operation.revoke_requested
                or binding.get("owner_epoch") != owner_epoch
                or binding.get("intent_fingerprint") != operation.intent_fingerprint
                or binding.get("before_state_fingerprint") != operation.before_state_fingerprint
                or operation.current_generation_fingerprint
                != self._broker.current_generation_fingerprint):
            raise auth_operations.AuthOperationError("configuration_changed")
        selector = auth_operations.recovery_selector(operation)
        inspected = self._broker.credential_discard_recovery(selector, "inspect")
        if (inspected.binding_fingerprint is None
                or inspected.binding_summary is None
                or inspected.material_state != "recovery_and_prepared"):
            self._set_phase(db, operation, "reconcile_pending")
            return LifecycleResult(operation.operation_id, "pending",
                                   error_code=inspected.code or "credential_recovery_required")
        if not self._valid_grant_target(
                db, operation, binding, inspected.binding_summary,
                target_auth_method_id=target_auth_method_id, transition=transition):
            return self._reject_prepared_grant(db, operation, inspected)
        if operation.phase != "old_retained":
            operation = auth_operations.advance_phase(
                db, _binding(operation), expected=operation.phase, target="old_retained")
        committed = self._broker.credential_commit(binding)
        committed_public = _auth_public(committed)
        if (committed_public.state != "committing" or not committed.prepared_ref
                or not committed.commit_handle):
            self._set_phase(db, operation, "reconcile_pending")
            return LifecycleResult(operation.operation_id, "pending",
                                   error_code=committed_public.code)
        try:
            operation = self._commit_grant_row(
                db, operation, prepared_ref=committed.prepared_ref,
                target_auth_method_id=target_auth_method_id)
        except auth_operations.AuthOperationError:
            current = auth_operations.operation(db, operation.owner_id, operation.operation_id)
            self._set_phase(db, current, "reconcile_pending")
            return LifecycleResult(operation.operation_id, "pending",
                                   error_code="configuration_changed")
        self._crash_hook("after_db_commit")
        try:
            cleanup = self._broker.credential_discard_exact(
                binding, "cleanup_committed", commit_handle=committed.commit_handle,
                adopted_prepared_ref=committed.prepared_ref)
        except Exception:
            cleanup = None
        cleanup_public = None if cleanup is None else _auth_public(cleanup)
        if cleanup_public is not None and cleanup_public.state != "succeeded":
            return LifecycleResult(operation.operation_id, "finalizing",
                                   error_code=cleanup_public.code)
        inspected = self._broker.credential_discard_recovery(selector, "inspect")
        if not self._confirmed(inspected, "cleaned"):
            return LifecycleResult(operation.operation_id, "finalizing",
                                   error_code=inspected.code)
        return self._finish_committed(db, operation, inspected)

    def _commit_initial_auth(self, db: Session, row: ProviderConnection,
                             operation, binding: dict) -> AuthLifecycleResult:
        committed = self._broker.credential_commit(binding)
        public = _auth_public(committed)
        if (public.state != "committing" or not committed.prepared_ref
                or not committed.commit_handle):
            self._set_phase(db, operation, "reconcile_pending")
            return self._auth_operation_result(
                operation, error_code=public.code or "credential_recovery_required")
        db.rollback()
        db.execute(text("BEGIN IMMEDIATE"))
        try:
            fresh = db.query(ProviderConnection).filter_by(
                id=operation.connection_id, user_id=operation.owner_id).one()
            if (connection_state_fingerprint(db, fresh)
                    != operation.before_state_fingerprint
                    or fresh.secret_ref is not None):
                raise auth_operations.AuthOperationError("configuration_changed")
            changed = db.execute(sql_update(ProviderConnection).where(
                ProviderConnection.id == fresh.id,
                ProviderConnection.user_id == fresh.user_id,
                ProviderConnection.secret_ref.is_(None),
                ProviderConnection.credential_state == fresh.credential_state,
                ProviderConnection.readiness_revision == fresh.readiness_revision,
                ProviderConnection.auth_grant_revision == fresh.auth_grant_revision,
                ProviderConnection.auth_method_id == fresh.auth_method_id,
            ).values(
                secret_ref=committed.prepared_ref,
                credential_state="present",
                readiness_revision=int(fresh.readiness_revision or 1) + 1,
                auth_grant_revision=int(fresh.auth_grant_revision or 0) + 1,
            ).execution_options(synchronize_session=False)).rowcount
            if changed != 1:
                raise auth_operations.AuthOperationError("configuration_changed")
            db.execute(sql_delete(ConnectionModelCache).where(
                ConnectionModelCache.owner_id == operation.owner_id,
                ConnectionModelCache.connection_id == operation.connection_id,
            ))
            operation = auth_operations.advance_phase_no_commit(
                db, _binding(operation), expected=operation.phase,
                target="committed_cleanup_pending")
            db.commit()
        except Exception:
            db.rollback()
            raise
        self._crash_hook("after_db_commit")
        try:
            cleanup = self._broker.credential_discard_exact(
                binding, "cleanup_committed",
                commit_handle=committed.commit_handle,
                adopted_prepared_ref=committed.prepared_ref)
            cleanup_public = _auth_public(cleanup)
        except Exception:
            cleanup_public = None
        if cleanup_public is None or cleanup_public.state != "succeeded":
            return AuthLifecycleResult(
                operation_id=operation.operation_id,
                state="finalizing",
                expires_at=operation.expires_at,
                error_code=("credential_cleanup_unconfirmed" if cleanup_public is None
                            else self._safe_auth_code(cleanup_public.code)),
            )
        terminal = self._terminalize_exact(db, operation, TerminalOutcome(
            state="succeeded", error_code=None, revoke_state="skipped"))
        self._clear_memory_cache(terminal)
        return self._auth_operation_result(terminal)

    def _valid_grant_target(self, db: Session, operation,
                            binding: dict, summary, *, target_auth_method_id: str,
                            transition: str) -> bool:
        db.expire_all()
        fresh = db.query(ProviderConnection).filter_by(
            id=operation.connection_id, user_id=operation.owner_id).first()
        if (fresh is None or connection_state_fingerprint(db, fresh)
                != operation.before_state_fingerprint):
            return False
        if (binding.get("provider_id") != summary.provider_id
                or binding.get("auth_method_id") != summary.auth_method_id
                or binding.get("config_revision") != summary.config_revision
                or binding.get("auth_grant_revision") != summary.auth_grant_revision
                or binding.get("public_config_fingerprint")
                != summary.public_config_fingerprint
                or target_auth_method_id != summary.auth_method_id
                or (fresh.provider_id or fresh.kind) != summary.provider_id
                or int(fresh.readiness_revision or 1) != summary.config_revision
                or int(fresh.auth_grant_revision or 0) != summary.auth_grant_revision
                or _public_config_fingerprint(fresh, target_auth_method_id)
                != summary.public_config_fingerprint):
            return False
        if (transition != "proven_new_grant"
                and fresh.auth_method_id != target_auth_method_id):
            return False
        try:
            provider_catalog.validate_public_config(PublicConfigEnvelope(
                version=fresh.public_config_version,
                provider_id=fresh.provider_id,
                protocol_id=fresh.protocol_id,
                auth_method_id=target_auth_method_id,
                fields=deepcopy(fresh.public_config or {}),
            ))
        except (HTTPException, ValueError):
            return False
        return True

    def _reject_prepared_grant(self, db: Session, operation, inspected) -> LifecycleResult:
        selector = auth_operations.recovery_selector(operation)
        try:
            acted = self._broker.credential_discard_recovery(
                selector, "restore_old",
                binding_fingerprint=inspected.binding_fingerprint)
            if acted.material_state in {"prepared", "recovery_and_prepared"}:
                self._broker.credential_discard_recovery(
                    selector, "discard_prepared",
                    binding_fingerprint=inspected.binding_fingerprint)
            verified = self._broker.credential_discard_recovery(selector, "inspect")
        except Exception:
            self._set_phase(db, operation, "rollback_pending")
            return LifecycleResult(operation.operation_id, "pending",
                                   error_code="configuration_changed")
        if not self._confirmed(verified, "restored"):
            self._set_phase(db, operation, "rollback_pending")
            return LifecycleResult(operation.operation_id, "pending",
                                   error_code="configuration_changed")
        return self._finish_restored(
            db, operation, verified, error_code="configuration_changed")

    def operation_status(self, db: Session, owner_id: int, connection_id: int,
                         operation_id: str) -> LifecycleResult:
        operation = auth_operations.operation(db, owner_id, operation_id)
        if operation.connection_id != connection_id:
            raise auth_operations.AuthOperationError("configuration_changed")
        if operation.kind != "disconnect":
            return self._auth_status(db, operation)
        if operation.phase == "terminal" and operation.terminal_at is not None:
            return _public_result(
                operation,
                credential_state=self._current_credential_state(db, operation),
            )
        return self._reconcile_one(db, operation)

    def cancel_operation(self, db: Session, owner_id: int, connection_id: int,
                         operation_id: str) -> LifecycleResult:
        operation = auth_operations.operation(db, owner_id, operation_id)
        if operation.connection_id != connection_id:
            raise auth_operations.AuthOperationError("configuration_changed")
        if operation.phase == "terminal" and operation.terminal_at is not None:
            return (self._auth_operation_result(operation)
                    if operation.kind != "disconnect" else _public_result(
                        operation,
                        credential_state=self._current_credential_state(db, operation),
                    ))
        if operation.phase == "committed_cleanup_pending":
            reconciled = self._reconcile_one(db, operation)
            return (self._auth_recovery_result(auth_operations.operation(
                db, owner_id, operation_id), reconciled)
                    if operation.kind != "disconnect" else reconciled)
        if operation.kind != "disconnect":
            row = db.query(ProviderConnection).filter_by(
                id=connection_id, user_id=owner_id).first()
            owner_epoch = self._owner_epochs.current(owner_id)
            if (row is None or owner_epoch is None
                    or connection_state_fingerprint(db, row)
                    != operation.before_state_fingerprint):
                raise auth_operations.AuthOperationError("configuration_changed")
            binding = self._auth_binding(
                row, operation, owner_epoch, row.auth_method_id)
            self._broker.auth_cancel(binding)
        db.rollback()
        db.execute(text("BEGIN IMMEDIATE"))
        try:
            changed = db.execute(text("""UPDATE connection_auth_operations
                SET state='cancelled',error_code='operation_cancelled',
                    updated_at=CURRENT_TIMESTAMP
                WHERE operation_id=:operation_id AND owner_id=:owner_id
                    AND connection_id=:connection_id
                    AND intent_fingerprint=:intent_fingerprint
                    AND current_generation_fingerprint=:current_generation_fingerprint
                    AND terminal_at IS NULL
                    AND phase!='committed_cleanup_pending'"""),
                _binding(operation).model_dump(mode="python")).rowcount
            if changed != 1:
                raise auth_operations.AuthOperationError("configuration_changed")
            db.commit()
        except Exception:
            db.rollback()
            raise
        cancelled = auth_operations.operation(db, owner_id, operation_id)
        reconciled = self._reconcile_one(db, cancelled)
        if cancelled.kind == "disconnect":
            return reconciled
        current = auth_operations.operation(db, owner_id, operation_id)
        return self._auth_recovery_result(current, reconciled)

    def _auth_status(self, db: Session, operation) -> AuthLifecycleResult:
        if operation.phase == "terminal" and operation.terminal_at is not None:
            return self._auth_operation_result(operation)
        row = db.query(ProviderConnection).filter_by(
            id=operation.connection_id, user_id=operation.owner_id).first()
        owner_epoch = self._owner_epochs.current(operation.owner_id)
        if row is None or owner_epoch is None:
            raise auth_operations.AuthOperationError("owner_epoch_unavailable")
        if operation.current_generation_fingerprint != self._broker.current_generation_fingerprint:
            reconciled = self._reconcile_one(db, operation)
            current = auth_operations.operation(
                db, operation.owner_id, operation.operation_id)
            return self._auth_recovery_result(current, reconciled)
        if connection_state_fingerprint(db, row) != operation.before_state_fingerprint:
            if operation.phase == "committed_cleanup_pending":
                reconciled = self._reconcile_one(db, operation)
                current = auth_operations.operation(
                    db, operation.owner_id, operation.operation_id)
                return self._auth_recovery_result(current, reconciled)
            raise auth_operations.AuthOperationError("configuration_changed")
        binding = self._auth_binding(
            row, operation, owner_epoch, row.auth_method_id)
        public = self._broker.auth_status(binding)
        if public.state == "prepared":
            operation = auth_operations.record_auth_state(
                db, _binding(operation), "prepared")
            if operation.kind == "grant_replace":
                result = self.commit_prepared_grant(
                    db, row, operation=operation, binding=binding,
                    target_auth_method_id=row.auth_method_id,
                    transition="proven_new_grant",
                )
                current = auth_operations.operation(
                    db, operation.owner_id, operation.operation_id)
                return self._auth_recovery_result(current, result)
            return self._commit_initial_auth(db, row, operation, binding)
        if public.state in {"awaiting_user", "polling", "exchanging",
                            "committing", "recovering"}:
            operation = auth_operations.record_auth_state(
                db, _binding(operation), public.state)
            return self._auth_operation_result(operation, public=public)
        state = {
            "cancelled": "cancelled", "expired": "expired",
            "interrupted": "interrupted", "failed": "failed",
        }.get(public.state, "failed")
        operation = auth_operations.record_auth_terminal_pending(
            db, _binding(operation), state=state,
            error_code=self._safe_auth_code(public.code) or "internal_error")
        reconciled = self._reconcile_one(db, operation)
        current = auth_operations.operation(
            db, operation.owner_id, operation.operation_id)
        return self._auth_recovery_result(current, reconciled)

    def sweep_terminal_receipts(self, db: Session, owner_id: int, *,
                                terminal_before: datetime, limit: int = 25) -> int:
        acknowledged = 0
        for operation in auth_operations.terminal_receipt_candidates(
                db, owner_id, terminal_before=terminal_before, limit=limit):
            if self._owner_epochs.current(owner_id) is None:
                break
            selector = auth_operations.recovery_selector(operation)
            inspected = self._broker.credential_discard_recovery(selector, "inspect")
            if (inspected.material_state != "none" or inspected.receipt_state != "present"
                    or inspected.state not in {"restored", "cleaned"}
                    or inspected.binding_fingerprint is None):
                continue
            try:
                acknowledged_reply = self._broker.credential_discard_recovery(
                    selector, "acknowledge_terminal",
                    binding_fingerprint=inspected.binding_fingerprint)
            except Exception:
                continue
            if acknowledged_reply.receipt_state == "none":
                acknowledged += 1
        return acknowledged

    def _reconcile_one(self, db: Session, operation, *, remote_revoker=None) -> LifecycleResult:
        if self._owner_epochs.current(operation.owner_id) is None:
            self._set_phase(db, operation, "reconcile_pending")
            return LifecycleResult(operation.operation_id, "pending",
                                   error_code="owner_epoch_unavailable")
        selector = auth_operations.recovery_selector(operation)
        inspected = self._broker.credential_discard_recovery(selector, "inspect")
        fresh = auth_operations.operation(db, operation.owner_id, operation.operation_id)
        generation = self._broker.current_generation_fingerprint
        if fresh.current_generation_fingerprint != generation:
            if inspected.binding_fingerprint is None:
                self._set_phase(db, fresh, "reconcile_pending")
                return LifecycleResult(fresh.operation_id, "pending",
                                       error_code=inspected.code or "credential_recovery_required")
            self._crash_hook("before_generation_adoption")
            adoption = auth_operations.adopt_generation(
                db, selector=selector,
                expected_generation=fresh.current_generation_fingerprint,
                new_generation=generation,
            )
            self._crash_hook("after_generation_adoption")
            fresh = auth_operations.operation(db, operation.owner_id, operation.operation_id)
            if adoption.state == "already_current":
                verified = self._broker.credential_discard_recovery(selector, "inspect")
                newest = auth_operations.operation(db, operation.owner_id, operation.operation_id)
                if (newest.current_generation_fingerprint != generation
                        or self._broker.current_generation_fingerprint != generation
                        or not self._same_inspection(inspected, verified)):
                    self._set_phase(db, newest, "reconcile_pending")
                    return LifecycleResult(newest.operation_id, "pending",
                                           error_code="configuration_changed")
                inspected, fresh = verified, newest
        truth = self._db_truth(db, fresh, inspected)
        if truth == "ambiguous":
            self._set_phase(db, fresh, "reconcile_pending")
            return LifecycleResult(fresh.operation_id, "pending",
                                   error_code="configuration_changed")
        if inspected.state == "absent" and inspected.material_state == "none":
            if truth != "before":
                self._set_phase(db, fresh, "reconcile_pending")
                return LifecycleResult(fresh.operation_id, "pending",
                                       error_code="credential_recovery_required")
            return self._terminal_failure_without_receipt(db, fresh)
        if inspected.binding_fingerprint is None:
            self._set_phase(db, fresh, "reconcile_pending")
            return LifecycleResult(fresh.operation_id, "pending",
                                   error_code=inspected.code or "credential_recovery_required")
        if inspected.material_state == "none" and inspected.receipt_state == "present":
            expected = "restored" if truth == "before" else "cleaned"
            if inspected.state != expected:
                self._set_phase(db, fresh, "reconcile_pending")
                return LifecycleResult(fresh.operation_id, "pending",
                                       error_code="credential_recovery_required")
            if truth == "committed":
                return self._finish_committed(
                    db, fresh, inspected, remote_revoker=remote_revoker)
            return self._finish_restored(db, fresh, inspected)
        action = "restore_old" if truth == "before" else "cleanup_committed"
        self._crash_hook("before_recovery_action")
        acted = self._broker.credential_discard_recovery(
            selector, action, binding_fingerprint=inspected.binding_fingerprint,
            adopted_prepared_ref=(
                self._current_reference(db, fresh) if fresh.kind in {
                    "auth", "grant_replace", "same_grant_refresh"}
                and truth == "committed" else None),
        )
        if truth == "before" and acted.material_state in {"prepared", "recovery_and_prepared"}:
            acted = self._broker.credential_discard_recovery(
                selector, "discard_prepared",
                binding_fingerprint=inspected.binding_fingerprint)
        verified = self._broker.credential_discard_recovery(selector, "inspect")
        if (fresh.kind == "auth" and truth == "committed"
                and inspected.material_state == "prepared"
                and inspected.binding_fingerprint is not None
                and verified.state == "absent"
                and verified.material_state == "none"
                and verified.receipt_state == "none"):
            terminal = self._terminalize_exact(db, fresh, TerminalOutcome(
                state="succeeded", error_code=None, revoke_state="skipped"))
            self._clear_memory_cache(terminal)
            return LifecycleResult(
                terminal.operation_id, "succeeded",
                result={"credential_state": "present"})
        if not self._confirmed(verified, "restored" if truth == "before" else "cleaned"):
            self._set_phase(db, fresh, "rollback_pending" if truth == "before"
                            else "committed_cleanup_pending")
            return LifecycleResult(fresh.operation_id,
                                   "pending" if truth == "before" else "finalizing",
                                   error_code=verified.code)
        if truth == "before":
            return self._finish_restored(db, fresh, verified)
        return self._finish_committed(
            db, fresh, verified, remote_revoker=remote_revoker)

    def _commit_disconnect(self, db: Session, operation):
        db.rollback()
        db.execute(text("BEGIN IMMEDIATE"))
        try:
            row = db.query(ProviderConnection).filter_by(
                id=operation.connection_id, user_id=operation.owner_id).one()
            if connection_state_fingerprint(db, row) != operation.before_state_fingerprint:
                raise auth_operations.AuthOperationError("configuration_changed")
            updated = db.execute(sql_update(ProviderConnection).where(
                ProviderConnection.id == row.id,
                ProviderConnection.user_id == row.user_id,
                ProviderConnection.secret_ref == row.secret_ref,
                ProviderConnection.credential_state == row.credential_state,
                ProviderConnection.readiness_revision == row.readiness_revision,
                ProviderConnection.auth_grant_revision == row.auth_grant_revision,
            ).values(
                secret_ref=None,
                credential_state="missing",
                readiness_revision=int(row.readiness_revision or 1) + 1,
                auth_grant_revision=int(row.auth_grant_revision or 0) + 1,
            ).execution_options(synchronize_session=False)).rowcount
            if updated != 1:
                raise auth_operations.AuthOperationError("configuration_changed")
            db.execute(sql_delete(ConnectionModelCache).where(
                ConnectionModelCache.owner_id == operation.owner_id,
                ConnectionModelCache.connection_id == operation.connection_id,
            ))
            operation = auth_operations.advance_phase_no_commit(
                db, _binding(operation), expected="old_retained",
                target="committed_cleanup_pending")
            db.commit()
            return operation
        except Exception as error:
            db.rollback()
            try:
                current = auth_operations.operation(
                    db, operation.owner_id, operation.operation_id)
                if current.phase == "committed_cleanup_pending":
                    inspected = self._broker.credential_discard_recovery(
                        auth_operations.recovery_selector(current), "inspect")
                    if self._db_truth(db, current, inspected) == "committed":
                        return current
            except Exception:
                pass
            if isinstance(error, auth_operations.AuthOperationError):
                raise
            raise auth_operations.AuthOperationError("configuration_changed") from error

    def _commit_grant_row(self, db: Session, operation, *, prepared_ref: str,
                          target_auth_method_id: str):
        db.rollback()
        db.execute(text("BEGIN IMMEDIATE"))
        try:
            row = db.query(ProviderConnection).filter_by(
                id=operation.connection_id, user_id=operation.owner_id).one()
            if connection_state_fingerprint(db, row) != operation.before_state_fingerprint:
                raise auth_operations.AuthOperationError("configuration_changed")
            increment = int(operation.kind == "grant_replace")
            updated = db.execute(sql_update(ProviderConnection).where(
                ProviderConnection.id == row.id,
                ProviderConnection.user_id == row.user_id,
                ProviderConnection.secret_ref == row.secret_ref,
                ProviderConnection.credential_state == row.credential_state,
                ProviderConnection.readiness_revision == row.readiness_revision,
                ProviderConnection.auth_grant_revision == row.auth_grant_revision,
                ProviderConnection.auth_method_id == row.auth_method_id,
            ).values(
                secret_ref=prepared_ref,
                credential_state="present",
                auth_method_id=target_auth_method_id,
                readiness_revision=int(row.readiness_revision or 1) + increment,
                auth_grant_revision=int(row.auth_grant_revision or 0) + increment,
            ).execution_options(synchronize_session=False)).rowcount
            if updated != 1:
                raise auth_operations.AuthOperationError("configuration_changed")
            db.execute(sql_delete(ConnectionModelCache).where(
                ConnectionModelCache.owner_id == operation.owner_id,
                ConnectionModelCache.connection_id == operation.connection_id,
            ))
            operation = auth_operations.advance_phase_no_commit(
                db, _binding(operation), expected="old_retained",
                target="committed_cleanup_pending")
            db.commit()
            return operation
        except Exception as error:
            db.rollback()
            try:
                current = auth_operations.operation(
                    db, operation.owner_id, operation.operation_id)
                if current.phase == "committed_cleanup_pending":
                    inspected = self._broker.credential_discard_recovery(
                        auth_operations.recovery_selector(current), "inspect")
                    if self._db_truth(db, current, inspected) == "committed":
                        return current
            except Exception:
                pass
            if isinstance(error, auth_operations.AuthOperationError):
                raise
            raise auth_operations.AuthOperationError("configuration_changed") from error

    def _finish_without_material(self, db: Session, operation, *, already_invalid: bool):
        if operation.revoke_requested:
            operation, ambiguous = self._mark_revoke_attempt_exact(db, operation)
            operation = self._record_revoke_outcome_exact(
                db, operation, "remote_revoke_failed" if ambiguous
                else "already_invalid" if already_invalid else "remote_revoke_failed")
        outcome = TerminalOutcome(
                state="succeeded", error_code=None,
                revoke_state=operation.revoke_state,
            )
        terminal = self._terminalize_exact(db, operation, outcome)
        return _public_result(terminal)

    def _finish_committed(self, db: Session, operation, inspected, *, remote_revoker=None):
        fresh = auth_operations.operation(db, operation.owner_id, operation.operation_id)
        revoke_state = self._remote_outcome(
            db, fresh, remote_revoker=remote_revoker)
        fresh = auth_operations.operation(db, operation.owner_id, operation.operation_id)
        outcome = TerminalOutcome(
                state="succeeded", error_code=None, revoke_state=revoke_state,
            )
        terminal = self._terminalize_exact(db, fresh, outcome)
        self._clear_memory_cache(fresh)
        self._crash_hook("after_terminal")
        self._ack_receipt(terminal, inspected.binding_fingerprint)
        self._crash_hook("after_ack")
        return _public_result(terminal)

    def _finish_restored(self, db: Session, operation, inspected, *,
                         error_code: str | None = None):
        fresh = auth_operations.operation(db, operation.owner_id, operation.operation_id)
        state = fresh.state if fresh.state in {
            "cancelled", "expired", "interrupted", "failed"} else "failed"
        code = error_code or fresh.error_code or (
            "operation_cancelled" if state == "cancelled" else
            "operation_expired" if state == "expired" else
            "operation_interrupted")
        outcome = TerminalOutcome(
                state=state, error_code=code, revoke_state="skipped",
            )
        terminal = self._terminalize_exact(db, fresh, outcome)
        self._crash_hook("after_terminal")
        self._ack_receipt(terminal, inspected.binding_fingerprint)
        self._crash_hook("after_ack")
        return _public_result(
            terminal, credential_state=self._current_credential_state(db, terminal))

    def _terminal_failure_without_receipt(self, db: Session, operation):
        state = operation.state if operation.state in {
            "cancelled", "expired", "interrupted", "failed"} else "failed"
        code = operation.error_code or (
            "operation_cancelled" if state == "cancelled" else
            "operation_expired" if state == "expired" else
            "operation_interrupted")
        outcome = TerminalOutcome(
                state=state, error_code=code, revoke_state="skipped",
            )
        terminal = self._terminalize_exact(db, operation, outcome)
        return _public_result(
            terminal, credential_state=self._current_credential_state(db, terminal))

    def _remote_outcome(self, db: Session, operation, *, remote_revoker=None) -> str:
        if not operation.revoke_requested:
            return "skipped"
        if operation.revoke_state != "pending":
            return operation.revoke_state
        if operation.revoke_attempt_started_at is not None:
            closed = self._record_revoke_outcome_exact(
                db, operation, "remote_revoke_failed")
            return closed.revoke_state
        operation, ambiguous_marker = self._mark_revoke_attempt_exact(db, operation)
        self._crash_hook("after_revoke_attempt_marker")
        if ambiguous_marker:
            outcome = "remote_revoke_failed"
        elif remote_revoker is None:
            outcome = "not_supported"
        else:
            try:
                outcome = remote_revoker(
                    selector=auth_operations.recovery_selector(operation))
            except Exception:
                outcome = "remote_revoke_failed"
            if outcome not in {
                    "revoked", "not_supported", "already_invalid", "remote_revoke_failed"}:
                outcome = "remote_revoke_failed"
        self._crash_hook("after_revoke_call")
        closed = self._record_revoke_outcome_exact(db, operation, outcome)
        self._crash_hook("after_revoke_outcome")
        return closed.revoke_state

    def _mark_revoke_attempt_exact(self, db: Session, operation):
        try:
            return (auth_operations.mark_revoke_attempt_started(
                db, _binding(operation), self._clock()), False)
        except Exception as error:
            db.rollback()
            current = auth_operations.operation(
                db, operation.owner_id, operation.operation_id)
            if (current.revoke_state == "pending"
                    and current.revoke_attempt_started_at is not None):
                return current, True
            raise error

    @staticmethod
    def _record_revoke_outcome_exact(db: Session, operation, outcome: str):
        try:
            return auth_operations.record_revoke_outcome(
                db, _binding(operation), outcome)
        except Exception as error:
            db.rollback()
            current = auth_operations.operation(
                db, operation.owner_id, operation.operation_id)
            if current.revoke_state == outcome:
                return current
            raise error

    def _ack_receipt(self, operation, binding_fingerprint: str | None) -> None:
        if binding_fingerprint is None:
            return
        try:
            self._broker.credential_discard_recovery(
                auth_operations.recovery_selector(operation), "acknowledge_terminal",
                binding_fingerprint=binding_fingerprint)
        except Exception:
            pass

    def _begin_operation(self, db: Session, intent: AuthOperationIntent,
                         idempotency_key: str | None):
        try:
            return auth_operations.begin_operation(db, intent, idempotency_key)
        except Exception as error:
            db.rollback()
            try:
                current = auth_operations.operation(db, intent.owner_id, intent.operation_id)
            except Exception:
                raise error
            if (current.connection_id != intent.connection_id
                    or current.kind != intent.kind
                    or current.intent_fingerprint != intent.intent_fingerprint
                    or current.before_state_fingerprint != intent.before_state_fingerprint
                    or current.revoke_requested != intent.revoke_requested):
                raise auth_operations.AuthOperationError("configuration_changed") from error
            return current.model_copy(update={"replayed": True})

    def _disconnect_replay(self, db: Session, row: ProviderConnection, *,
                           revoke_remote: bool,
                           idempotency_key: str | None) -> LifecycleResult | None:
        if idempotency_key is not None:
            if (not isinstance(idempotency_key, str)
                    or not 1 <= len(idempotency_key) <= 200
                    or any(not character.isprintable() for character in idempotency_key)):
                raise auth_operations.AuthOperationError("idempotency_conflict")
            key_hash = sha256(idempotency_key.encode()).hexdigest()
            found = db.execute(text("""SELECT operation_id
                FROM connection_auth_operations
                WHERE owner_id=:owner_id AND idempotency_key_hash=:key_hash"""), {
                    "owner_id": row.user_id, "key_hash": key_hash,
                }).first()
            if found is not None:
                operation = auth_operations.operation(db, row.user_id, found.operation_id)
                if (operation.connection_id != row.id or operation.kind != "disconnect"
                        or operation.revoke_requested != revoke_remote):
                    raise auth_operations.AuthOperationError("idempotency_conflict")
                return _public_result(
                    operation,
                    credential_state=self._current_credential_state(db, operation),
                )
            return None
        blocking = auth_operations.blocking_operation(db, row.user_id, row.id)
        if blocking is not None:
            return _public_result(
                blocking,
                credential_state=self._current_credential_state(db, blocking),
            )
        return None

    def _terminalize_exact(self, db: Session, operation,
                           outcome: TerminalOutcome):
        selector = auth_operations.recovery_selector(operation)
        try:
            return auth_operations.terminalize(
                db, _binding(operation), outcome, selector=selector)
        except Exception as error:
            db.rollback()
            try:
                current = auth_operations.operation(
                    db, operation.owner_id, operation.operation_id)
            except Exception:
                raise error
            if (current.phase == "terminal" and current.terminal_at is not None
                    and current.state == outcome.state
                    and current.error_code == outcome.error_code
                    and current.revoke_state == outcome.revoke_state):
                return current
            raise error

    def _db_truth(self, db: Session, operation, inspected) -> str:
        db.expire_all()
        row = db.query(ProviderConnection).filter_by(
            id=operation.connection_id, user_id=operation.owner_id).first()
        if row is None:
            return "ambiguous"
        if connection_state_fingerprint(db, row) == operation.before_state_fingerprint:
            return "before"
        summary = getattr(inspected, "binding_summary", None)
        if (operation.phase != "committed_cleanup_pending" or summary is None
                or (row.provider_id or row.kind) != summary.provider_id
                or row.auth_method_id != summary.auth_method_id
                or _public_config_fingerprint(row, row.auth_method_id)
                != summary.public_config_fingerprint):
            return "ambiguous"
        cached = db.query(ConnectionModelCache).filter_by(
            owner_id=operation.owner_id, connection_id=operation.connection_id).first()
        if operation.kind == "disconnect":
            return ("committed" if row.secret_ref is None
                    and row.credential_state == "missing"
                    and int(row.readiness_revision or 1) == summary.config_revision + 1
                    and int(row.auth_grant_revision or 0) == summary.auth_grant_revision + 1
                    and cached is None else "ambiguous")
        if operation.kind in {"grant_replace", "same_grant_refresh"}:
            increment = int(operation.kind == "grant_replace")
            revisions = (
                summary.config_revision + increment,
                summary.auth_grant_revision + increment,
            )
            return ("committed" if row.secret_ref is not None
                    and row.credential_state == "present"
                    and (int(row.readiness_revision or 1),
                         int(row.auth_grant_revision or 0)) == revisions
                    and cached is None else "ambiguous")
        if operation.kind == "auth":
            return ("committed" if row.secret_ref is not None
                    and row.credential_state == "present"
                    and int(row.readiness_revision or 1) == summary.config_revision + 1
                    and int(row.auth_grant_revision or 0) == summary.auth_grant_revision + 1
                    and row.auth_method_id == summary.auth_method_id
                    and cached is None else "ambiguous")
        return "ambiguous"

    @staticmethod
    def _current_reference(db: Session, operation) -> str | None:
        row = db.query(ProviderConnection).filter_by(
            id=operation.connection_id, user_id=operation.owner_id).first()
        return None if row is None else row.secret_ref

    @staticmethod
    def _current_credential_state(db: Session, operation) -> str:
        row = db.query(ProviderConnection).filter_by(
            id=operation.connection_id, user_id=operation.owner_id).first()
        return "unavailable" if row is None else row.credential_state

    @staticmethod
    def _confirmed(inspected, expected: str) -> bool:
        return (inspected.state == expected and inspected.material_state == "none"
                and inspected.receipt_state == "present"
                and inspected.binding_fingerprint is not None)

    @staticmethod
    def _same_inspection(first, second) -> bool:
        return all(getattr(first, field) == getattr(second, field) for field in (
            "operation_id", "state", "material_state", "receipt_state",
            "binding_fingerprint", "code",
        ))

    @staticmethod
    def _set_phase(db: Session, operation, target: str):
        if operation.phase == target:
            return operation
        return auth_operations.advance_phase(
            db, _binding(operation), expected=operation.phase, target=target)

    def _clear_memory_cache(self, operation) -> None:
        if self._credential_cache is not None:
            self._credential_cache.clear_owner(operation.owner_id)
