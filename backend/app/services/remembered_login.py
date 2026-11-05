"""A Keychain-held device credential backed by a revocable local DB hash."""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.models import User
from app.services.secrets import (PlatformSecretStore, SecretInteractionRequired,
                                  SecretReentryRequired, SecretStoreUnavailable)


# Sanitized diagnostic field allowlists
REMEMBER_REASONS = frozenset({
    "credential_channel_unavailable", "credential_response_invalid", "credential_request_invalid",
    "keychain_operation_in_progress", "keychain_interaction_required",
    "keychain_interaction_state_read_failed", "keychain_interaction_disable_failed",
    "keychain_interaction_restore_failed", "keychain_no_ui_guard_failed",
    "keychain_interaction_state_uncertain", "keychain_query_failed",
    "keychain_query_binding_failed", "keychain_store_unavailable",
    "keychain_backend_unsupported", "keychain_backend_unavailable",
    "credential_reentry_required", "automatic_login_readback_failed",
    "automatic_login_save_uncertain", "automatic_login_save_failed",
})
REMEMBER_ERROR_CLASSES = frozenset({
    "native_os_status", "interaction_required", "bridge_transport",
    "bridge_protocol", "request_validation", "store_unavailable",
    "credential_reentry_required",
})


class LoginSecretStore(PlatformSecretStore):
    SERVICE = "cloud.gamja.desktop.remembered-login"


KEYCHAIN_ACCOUNT = "current"
LIFETIME = timedelta(days=30)


def secret_store(request: Request):
    if not hasattr(request.app.state, "login_secret_store"):
        request.app.state.login_secret_store = LoginSecretStore()
    return request.app.state.login_secret_store


def current_reference(db) -> str:
    row = db.execute(text("SELECT secret_ref FROM remembered_login WHERE id = 1")).first()
    return row.secret_ref if row else KEYCHAIN_ACCOUNT


def _delete_secret(store, reference: str) -> str:
    """Attempt to delete a secret reference. Returns cleanup status.
    
    Returns "success", "failed", or "unknown" (when store is None).
    Never raises; cleanup failures are tracked but do not override the primary error.
    """
    if store is None:
        return "unknown"
    try:
        store.delete(reference)
        return "success"
    except (SecretStoreUnavailable, RuntimeError):
        return "failed"


def revoke(db, store):
    row = db.execute(text(
        "SELECT secret_ref,revision FROM remembered_login WHERE id = 1")).first()
    if not row:
        return
    deleted = db.execute(text("""DELETE FROM remembered_login
        WHERE id = 1 AND secret_ref = :reference AND revision = :revision"""),
                         {"reference": row.secret_ref, "revision": row.revision})
    if deleted.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "Automatic login changed; try again")
    db.commit()
    _delete_secret(store, row.secret_ref)  # Return value ignored; revoke has no diagnostic


def _classify_remember_failure(error):
    """Classify a remember failure into sanitized diagnostic fields.
    
    Returns (code, reason, error_class, operation, os_status).
    Never includes secret values, references, or raw exception text.
    """
    if isinstance(error, SecretReentryRequired):
        return ("credential_locked", "credential_reentry_required",
                "credential_reentry_required", None, None)
    if isinstance(error, SecretInteractionRequired):
        reason = error.reason if error.reason in REMEMBER_REASONS else "keychain_store_unavailable"
        return ("credential_locked", reason, "interaction_required",
                error.operation, error.os_status)
    if isinstance(error, SecretStoreUnavailable):
        reason = error.reason if error.reason in REMEMBER_REASONS else "keychain_store_unavailable"
        error_class = error.error_class if error.error_class in REMEMBER_ERROR_CLASSES else "store_unavailable"
        return ("secret_store_unavailable", reason, error_class,
                error.operation, error.os_status)
    # RuntimeError (e.g., readback failure) or other exceptions
    return ("secret_store_unavailable", "automatic_login_readback_failed",
            "store_unavailable", None, None)


def remember(db, store, owner_id: int):
    prior = db.execute(text(
        "SELECT secret_ref,revision FROM remembered_login WHERE id = 1")).first()
    new_reference = uuid4().hex
    credential = secrets.token_urlsafe(32)
    try:
        store.put(new_reference, credential)
        saved = store.get_no_ui(new_reference)
        if not isinstance(saved, str) or not hmac.compare_digest(saved, credential):
            raise RuntimeError("automatic login readback failed")
    except (SecretStoreUnavailable, RuntimeError) as error:
        cleanup_status = _delete_secret(store, new_reference)
        code, reason, error_class, operation, os_status = _classify_remember_failure(error)
        correlation_id = secrets.token_hex(16)
        diagnostic = {
            "correlation_id": correlation_id,
            "stage": "auth_remember",
            "code": code,
            "reason": reason,
            "error_class": error_class,
            "cleanup": cleanup_status,
        }
        if operation is not None:
            diagnostic["operation"] = operation
        if os_status is not None:
            diagnostic["os_status"] = os_status
        exc = HTTPException(503, detail={"code": code, "correlation_id": correlation_id})
        exc._remember_diagnostic = diagnostic
        raise exc from None
    bind = db.get_bind()
    try:
        values = {"owner": owner_id, "hash": hashlib.sha256(credential.encode()).hexdigest(),
                  "expiry": datetime.utcnow() + LIFETIME, "reference": new_reference}
        if prior:
            updated = db.execute(text("""UPDATE remembered_login
                SET owner_id=:owner,secret_hash=:hash,expires_at=:expiry,
                    secret_ref=:reference,revision=revision+1
                WHERE id=1 AND secret_ref=:old_reference AND revision=:old_revision"""),
                                 {**values, "old_reference": prior.secret_ref,
                                  "old_revision": prior.revision})
            if updated.rowcount != 1:
                db.rollback()
                _delete_secret(store, new_reference)
                raise HTTPException(409, "Automatic login changed; try again")
        else:
            db.execute(text("""INSERT INTO remembered_login
                (id,owner_id,secret_hash,expires_at,secret_ref,revision)
                VALUES (1,:owner,:hash,:expiry,:reference,1)"""), values)
        db.commit()
        return
    except HTTPException:
        raise
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        try:
            with Session(bind=bind) as check:
                committed = check.execute(text(
                    "SELECT secret_ref FROM remembered_login WHERE id = 1")).scalar_one_or_none()
        except Exception:
            correlation_id = secrets.token_hex(16)
            diagnostic = {
                "correlation_id": correlation_id,
                "stage": "auth_remember",
                "code": "secret_store_unavailable",
                "reason": "automatic_login_save_uncertain",
                "error_class": "store_unavailable",
                "cleanup": "unknown",
            }
            exc = HTTPException(503, detail={"code": "secret_store_unavailable",
                                             "correlation_id": correlation_id})
            exc._remember_diagnostic = diagnostic
            raise exc from None
        if committed == new_reference:
            return
        cleanup_status = _delete_secret(store, new_reference)
        correlation_id = secrets.token_hex(16)
        diagnostic = {
            "correlation_id": correlation_id,
            "stage": "auth_remember",
            "code": "secret_store_unavailable",
            "reason": "automatic_login_save_failed",
            "error_class": "store_unavailable",
            "cleanup": cleanup_status,
        }
        exc = HTTPException(503, detail={"code": "secret_store_unavailable",
                                         "correlation_id": correlation_id})
        exc._remember_diagnostic = diagnostic
        raise exc from None


def restore_credential(db, credential: str | None, *, expected_owner: int | None = None) -> User:
    row = db.execute(text("SELECT owner_id, secret_hash, expires_at FROM remembered_login WHERE id = 1")).first()
    valid = bool(credential and row and
                 hmac.compare_digest(hashlib.sha256(credential.encode()).hexdigest(), row.secret_hash) and
                 datetime.fromisoformat(row.expires_at) > datetime.utcnow())
    if not valid:
        raise HTTPException(401, "No valid automatic login on this device")
    if expected_owner is not None and row.owner_id != expected_owner:
        raise HTTPException(401, "No valid automatic login on this device")
    owner = db.get(User, row.owner_id)
    if not owner or not owner.is_active:
        raise HTTPException(401, "No valid automatic login on this device")
    return owner


def restore(db, store, *, interactive: bool = False, expected_owner: int | None = None) -> User:
    reference = current_reference(db)
    credential = store.get(reference) if interactive else store.get_no_ui(reference)
    return restore_credential(db, credential, expected_owner=expected_owner)
