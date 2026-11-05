"""Persist bounded, normalized outcomes from explicit owner connection tests."""

VERSION = 18
REQUIRED_COLUMNS = {
    "provider_connections": {"readiness_revision"},
    "connection_test_history": {
        "id", "owner_id", "connection_id", "config_revision", "config_fingerprint",
        "model_id", "ok", "outcome_code", "credential_observation",
        "verified_capabilities", "duration_ms", "tested_at",
    },
}


def upgrade(db):
    db.execute("ALTER TABLE provider_connections ADD COLUMN readiness_revision INTEGER NOT NULL DEFAULT 1")
    db.execute("CREATE UNIQUE INDEX ux_provider_connections_id_user_id ON provider_connections(id,user_id)")
    db.execute("""CREATE TABLE connection_test_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        connection_id INTEGER NOT NULL,
        config_revision INTEGER NOT NULL,
        config_fingerprint VARCHAR(64) NOT NULL,
        model_id VARCHAR(200),
        ok BOOLEAN NOT NULL,
        outcome_code VARCHAR(40) NOT NULL,
        credential_observation VARCHAR(20) NOT NULL,
        verified_capabilities TEXT,
        duration_ms INTEGER NOT NULL,
        tested_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (connection_id,owner_id) REFERENCES provider_connections(id,user_id) ON DELETE CASCADE,
        CHECK (duration_ms >= 0),
        CHECK (ok IN (0,1))
    )""")
    db.execute("CREATE INDEX ix_connection_test_history_owner_connection ON connection_test_history(owner_id,connection_id,tested_at DESC,id DESC)")
