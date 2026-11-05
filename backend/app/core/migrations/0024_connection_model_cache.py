"""Add an owner-scoped, non-secret provider model cache."""

VERSION = 24
REQUIRED_COLUMNS = {
    "connection_model_cache": {
        "owner_id", "connection_id", "config_revision", "auth_grant_revision",
        "config_fingerprint", "observed_at", "truncated", "models", "updated_at",
    },
}


def upgrade(db):
    db.execute("""CREATE TABLE connection_model_cache (
        owner_id INTEGER NOT NULL,
        connection_id INTEGER NOT NULL,
        config_revision INTEGER NOT NULL CHECK (config_revision >= 1),
        auth_grant_revision INTEGER NOT NULL CHECK (auth_grant_revision >= 0),
        config_fingerprint VARCHAR(64) NOT NULL CHECK (length(config_fingerprint) = 64),
        observed_at DATETIME,
        truncated BOOLEAN NOT NULL DEFAULT 0 CHECK (truncated IN (0,1)),
        models TEXT NOT NULL,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (owner_id, connection_id),
        FOREIGN KEY (connection_id, owner_id)
            REFERENCES provider_connections(id, user_id) ON DELETE CASCADE
    )""")

