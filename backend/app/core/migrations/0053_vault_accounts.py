"""Durable account identity for the isolated Vault, without secret material."""

from uuid import uuid4

VERSION = 53
REQUIRED_COLUMNS = {"vault_accounts": {"owner_id", "vault_account_id"}}
REQUIRED_UNIQUE_INDEXES = {
    "ux_vault_accounts_identity": ("vault_accounts", ("vault_account_id",)),
}


def upgrade(db):
    db.execute("""CREATE TABLE vault_accounts (
        owner_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        vault_account_id TEXT NOT NULL CHECK (
            length(vault_account_id) = 32
            AND vault_account_id NOT GLOB '*[^0-9a-f]*'
        )
    )""")
    db.execute("CREATE UNIQUE INDEX ux_vault_accounts_identity ON vault_accounts(vault_account_id)")
    # Neither a reused numeric user ID nor a different local database may pick
    # up a previous account's Keychain namespace. This identity survives normal
    # updates/recovery with the account, and is not an authentication credential.
    for (owner_id,) in db.execute("SELECT id FROM users ORDER BY id").fetchall():
        db.execute("INSERT INTO vault_accounts(owner_id, vault_account_id) VALUES (?, ?)",
                   (owner_id, uuid4().hex))
    db.execute("""CREATE TRIGGER vault_account_identity_immutable
        BEFORE UPDATE ON vault_accounts
        WHEN NEW.owner_id IS NOT OLD.owner_id
          OR NEW.vault_account_id IS NOT OLD.vault_account_id
        BEGIN SELECT RAISE(ABORT, 'Vault account identity is immutable'); END""")
