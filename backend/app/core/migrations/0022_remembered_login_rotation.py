"""Bind the remembered-login hash to a rotatable credential reference."""

VERSION = 22
REQUIRED_COLUMNS = {"remembered_login": {"secret_ref", "revision"}}


def upgrade(db):
    db.execute("ALTER TABLE remembered_login ADD COLUMN secret_ref VARCHAR(32) NOT NULL DEFAULT 'current'")
    db.execute("ALTER TABLE remembered_login ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0)")
