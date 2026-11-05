"""Upgrade a remembered login credential from an old Core to the new immutable V1 broker.

This is an INTERNAL primitive called AFTER the caller has already proven trust
through a separate validation path (e.g., old /auth/restore -> /auth/me chain).
It does NOT provide authentication by itself and must not be called standalone.

The caller must ensure:
- The user has already proven ownership of the old credential
- The trust chain from old Core to new broker has been validated externally

This module only handles the narrow re-enrollment after that trust is established.
"""

import hashlib
import secrets
import hmac
from datetime import datetime
from dataclasses import dataclass
import re
from uuid import uuid4
from typing import NamedTuple

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.secrets import SecretStoreUnavailable
from app.services.remembered_login import _classify_remember_failure


@dataclass(frozen=True)
class ExpectedState:
    """Typed frozen expected state for CAS upgrade.

    All fields are strictly validated before any native Keychain operations.
    """
    owner_id: int  # Positive integer, not boolean, not zero
    old_secret_ref: str  # UUID hex (32 chars) or 'current' for legacy
    revision: int  # Positive integer
    original_expires_at: datetime  # Exact expiry from DB

    def __post_init__(self):
        if (type(self.owner_id) is not int or self.owner_id <= 0
                or type(self.revision) is not int or self.revision <= 0
                or type(self.old_secret_ref) is not str
                or not (self.old_secret_ref == "current"
                        or re.fullmatch(r"[0-9a-f]{32}", self.old_secret_ref))
                or type(self.original_expires_at) is not datetime
                or self.original_expires_at.tzinfo is not None):
            raise UpgradeFailed("state_mismatch", "invalid_expected_state", cleanup="not_applicable")


class UpgradeResult(NamedTuple):
    """Result of successful upgrade."""
    new_secret_ref: str
    new_revision: int
    expires_at: datetime


class UpgradeFailed(HTTPException):
    """Upgrade failed with sanitized diagnostic."""
    def __init__(self, code: str, reason: str, *, cleanup: str = "unknown", native=None):
        correlation_id = secrets.token_hex(16)
        detail = {"code": code, "correlation_id": correlation_id}
        super().__init__(503, detail=detail)
        self._diagnostic = {
            "correlation_id": correlation_id,
            "stage": "auth_upgrade",
            "code": code,
            "reason": reason,
            "cleanup": cleanup,
        }
        if native is not None:
            _, native_reason, error_class, operation, os_status = _classify_remember_failure(native)
            self._diagnostic.update(native_reason=native_reason, error_class=error_class)
            if operation in {"get", "put", "read", "write", "delete", "add", "query",
                             "sec_item_add", "sec_item_update", "sec_item_copy_matching", "sec_item_delete"}:
                self._diagnostic["operation"] = operation
            if type(os_status) is int:
                self._diagnostic["os_status"] = os_status


def _expiry(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise UpgradeFailed("state_mismatch", "invalid_stored_expiry", cleanup="not_applicable") from None


def _validate_expected_state(expected: ExpectedState, row) -> None:
    """Validate expected state matches actual DB row.

    Raises UpgradeFailed with sanitized reason if validation fails.
    Never includes secret values, references, or DB raw content in error messages.
    """
    if not row:
        raise UpgradeFailed("state_mismatch", "no_remembered_login", cleanup="not_applicable")

    # Validate owner_id matches and user is active
    if row.owner_id != expected.owner_id:
        raise UpgradeFailed("state_mismatch", "owner_id_drift", cleanup="not_applicable")

    # Validate revision matches
    if row.revision != expected.revision:
        raise UpgradeFailed("state_mismatch", "revision_drift", cleanup="not_applicable")

    # Validate old_secret_ref matches
    if row.secret_ref != expected.old_secret_ref:
        raise UpgradeFailed("state_mismatch", "secret_ref_drift", cleanup="not_applicable")

    # Validate expiry matches exactly
    stored_expiry = _expiry(row.expires_at)
    if stored_expiry != expected.original_expires_at:
        raise UpgradeFailed("state_mismatch", "expiry_drift", cleanup="not_applicable")

    # Validate expiry is still valid (not expired)
    if stored_expiry <= datetime.utcnow():
        raise UpgradeFailed("state_mismatch", "already_expired", cleanup="not_applicable")


def _delete_new_secret_safely(store, new_ref: str) -> str:
    """Attempt to delete the new uncommitted secret. Returns cleanup status."""
    if store is None:
        return "unknown"
    try:
        store.delete(new_ref)
        return "success"
    except (SecretStoreUnavailable, RuntimeError):
        return "failed"


def upgrade_remembered_login(
    db: Session,
    store,
    expected: ExpectedState,
) -> UpgradeResult:
    """Upgrade remembered login from old Core to new broker.

    This is an internal primitive that MUST only be called after the caller
    has proven the trust path from old Core to new broker.

    Args:
        db: Database session with active transaction
        store: PlatformSecretStore or CoreSecretStore for Keychain access
        expected: Frozen expected state validated by caller before upgrade

    Returns:
        UpgradeResult with new secret_ref, revision, and preserved expires_at

    Raises:
        UpgradeFailed: If validation fails or native operations fail
        HTTPException(409): If CAS race detected

    The function:
    1. Validates expected state matches actual DB row
    2. Creates new UUID hex reference and credential
    3. Stores new credential with put/get_no_ui verification
    4. CAS updates DB with new ref/hash, revision+1, preserving owner/expiry
    5. Never deletes the old secret (keeps for rollback)
    6. On any failure, cleans up only the NEW uncommitted secret

    NEVER deletes old secret, never extends lifetime, never bypasses validation.
    """
    if type(expected) is not ExpectedState:
        raise UpgradeFailed("state_mismatch", "invalid_expected_state", cleanup="not_applicable")
    # Revalidate even a bypassed frozen-object construction before DB/native work.
    expected.__post_init__()
    # Read current DB state
    row = db.execute(text(
        "SELECT owner_id, secret_ref, secret_hash, expires_at, revision "
        "FROM remembered_login WHERE id = 1")).first()

    # Validate user is active
    if row:
        user_check = db.execute(text(
            "SELECT is_active FROM users WHERE id = :id"), {"id": expected.owner_id}).first()
        if not user_check or not user_check.is_active:
            raise UpgradeFailed("state_mismatch", "user_inactive", cleanup="not_applicable")

    # Validate expected state matches DB
    _validate_expected_state(expected, row)

    # Generate new UUID hex reference
    new_reference = uuid4().hex

    # Generate new credential
    new_credential = secrets.token_urlsafe(32)

    # Store new credential with readback verification
    try:
        store.put(new_reference, new_credential)
    except (SecretStoreUnavailable, RuntimeError) as error:
        # Clean up only the NEW uncommitted secret (not stored yet, but clean up just in case)
        cleanup_status = _delete_new_secret_safely(store, new_reference)
        raise UpgradeFailed("native_failure", "store_unavailable", cleanup=cleanup_status, native=error) from None

    try:
        saved = store.get_no_ui(new_reference)
        if not isinstance(saved, str) or not hmac.compare_digest(saved, new_credential):
            raise RuntimeError("readback verification failed")
    except (SecretStoreUnavailable, RuntimeError) as error:
        # Clean up the new secret that was put but failed readback
        cleanup_status = _delete_new_secret_safely(store, new_reference)
        raise UpgradeFailed("native_failure", "readback_failed", cleanup=cleanup_status, native=error) from None

    # Prepare new hash
    new_hash = hashlib.sha256(new_credential.encode()).hexdigest()

    # CAS update: only ref/hash change, revision+1, owner/expiry preserved
    bind = db.get_bind()
    try:
        updated = db.execute(text("""
            UPDATE remembered_login
            SET secret_ref = :new_ref,
                secret_hash = :new_hash,
                revision = revision + 1
            WHERE id = 1
              AND secret_ref = :old_ref
              AND revision = :old_revision
              AND owner_id = :owner_id
              AND expires_at = :expires_at
              AND secret_hash = :old_hash
              AND julianday(expires_at) > julianday(:now)
              AND EXISTS (SELECT 1 FROM users WHERE users.id = :owner_id AND users.is_active = 1)
        """), {
            "new_ref": new_reference,
            "new_hash": new_hash,
            "old_ref": expected.old_secret_ref,
            "old_revision": expected.revision,
            "owner_id": expected.owner_id,
            # SQLite's existing rows may use a space rather than ISO's T.
            # Bind the original representation so the CAS is byte-exact.
            "expires_at": row.expires_at,
            "old_hash": row.secret_hash,
            "now": datetime.utcnow().isoformat(),
        })

        if updated.rowcount != 1:
            db.rollback()
            # Clean up new secret on CAS failure
            _delete_new_secret_safely(store, new_reference)
            raise HTTPException(409, "Concurrent modification; try again")

        db.commit()

        return UpgradeResult(
            new_secret_ref=new_reference,
            new_revision=expected.revision + 1,
            expires_at=expected.original_expires_at,
        )

    except HTTPException:
        raise
    except Exception:
        # Commit or pre-commit failure
        try:
            db.rollback()
        except Exception:
            pass

        # Check if commit actually succeeded despite exception
        try:
            with Session(bind=bind) as check:
                committed = check.execute(text(
                    "SELECT secret_ref, owner_id, expires_at, revision, secret_hash "
                    "FROM remembered_login WHERE id = 1")).first()
        except Exception:
            # Cannot verify commit status
            # Keep new_ref, report uncertain
            raise UpgradeFailed("commit_uncertain", "verification_failed", cleanup="not_applicable") from None

        if committed and committed.secret_ref == new_reference:
            # Commit succeeded despite exception
            if (committed.owner_id == expected.owner_id
                    and committed.revision == expected.revision + 1
                    and committed.expires_at == row.expires_at
                    and hmac.compare_digest(committed.secret_hash, new_hash)):
                # Everything matches, success
                return UpgradeResult(
                    new_secret_ref=new_reference,
                    new_revision=expected.revision + 1,
                    expires_at=expected.original_expires_at,
                )
            # Commit succeeded but state doesn't match - very strange
            # Don't delete new_ref, report uncertain
            raise UpgradeFailed("commit_uncertain", "state_verification_mismatch", cleanup="not_applicable") from None

        # Commit definitely failed, clean up new secret
        cleanup_status = _delete_new_secret_safely(store, new_reference)
        raise UpgradeFailed("native_failure", "commit_failed", cleanup=cleanup_status) from None
