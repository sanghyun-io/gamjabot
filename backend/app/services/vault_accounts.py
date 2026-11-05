"""Non-secret durable account IDs; never accepts or returns Vault values."""

import re
from uuid import uuid4

from sqlalchemy import text


class VaultAccountUnavailable(RuntimeError):
    pass


def account_id(db, owner_id: int) -> str | None:
    if type(owner_id) is not int or owner_id <= 0:
        raise VaultAccountUnavailable("vault_account_unavailable")
    value = db.execute(text(
        "SELECT vault_account_id FROM vault_accounts WHERE owner_id=:owner_id"
    ), {"owner_id": owner_id}).scalar_one_or_none()
    if value is not None and (not isinstance(value, str)
                              or re.fullmatch(r"[0-9a-f]{32}", value) is None):
        raise VaultAccountUnavailable("vault_account_unavailable")
    return value


def add_registered_account(db, owner_id: int) -> None:
    """Called inside the new user's existing registration transaction only."""
    if type(owner_id) is not int or owner_id <= 0:
        raise VaultAccountUnavailable("vault_account_unavailable")
    db.execute(text(
        "INSERT INTO vault_accounts(owner_id,vault_account_id) "
        "VALUES (:owner_id,:vault_account_id)"
    ), {"owner_id": owner_id, "vault_account_id": uuid4().hex})


def attest(request, db, owner_id: int) -> bool:
    """Only the signed Engine's private Core channel can attest this identity.

    This binds identity only. It is not proof that a Vault helper, native input
    channel or browser fill transport is available. No secret crosses Engine.
    """
    epochs = getattr(request.app.state, "owner_epoch_provider", None)
    bind = getattr(epochs, "attest_vault_account", None)
    if bind is None or epochs.current(owner_id) is None:
        return False
    try:
        value = account_id(db, owner_id)
    except VaultAccountUnavailable:
        return False
    return value is not None and bind(owner_id, value)
