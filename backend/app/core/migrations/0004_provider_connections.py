"""Add exact connection configuration without changing legacy rows."""

VERSION = 4
REQUIRED_COLUMNS = {"provider_connections": {"default_model_id", "enabled", "auth_required", "capabilities"}}


def upgrade(db):
    db.execute("ALTER TABLE provider_connections ADD COLUMN default_model_id VARCHAR NOT NULL DEFAULT ''")
    db.execute("ALTER TABLE provider_connections ADD COLUMN enabled BOOLEAN NOT NULL DEFAULT 0")
    db.execute("ALTER TABLE provider_connections ADD COLUMN auth_required BOOLEAN NOT NULL DEFAULT 1")
    db.execute("ALTER TABLE provider_connections ADD COLUMN capabilities TEXT NOT NULL DEFAULT '{}'")
