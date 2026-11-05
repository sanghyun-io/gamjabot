"""Add nullable versioned provider metadata without rewriting legacy rows."""

VERSION = 23
REQUIRED_COLUMNS = {
    "provider_connections": {
        "provider_id", "protocol_id", "auth_method_id", "public_config_version",
        "public_config", "auth_grant_revision", "credential_state",
    },
}


def upgrade(db):
    db.execute("ALTER TABLE provider_connections ADD COLUMN provider_id VARCHAR(64)")
    db.execute("ALTER TABLE provider_connections ADD COLUMN protocol_id VARCHAR(64)")
    db.execute("ALTER TABLE provider_connections ADD COLUMN auth_method_id VARCHAR(64)")
    db.execute("""ALTER TABLE provider_connections ADD COLUMN public_config_version INTEGER
        CHECK (public_config_version IS NULL OR public_config_version = 1)""")
    db.execute("ALTER TABLE provider_connections ADD COLUMN public_config TEXT")
    db.execute("""ALTER TABLE provider_connections ADD COLUMN auth_grant_revision INTEGER
        CHECK (auth_grant_revision IS NULL OR auth_grant_revision >= 0)""")
    db.execute("""ALTER TABLE provider_connections ADD COLUMN credential_state VARCHAR(32)
        CHECK (credential_state IS NULL OR credential_state IN
        ('missing','authorizing','present','refresh_required','rejected','locked','unavailable','not_required'))""")
