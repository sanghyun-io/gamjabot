"""Persist replay-safe exact request finalization records."""

VERSION = 21
REQUIRED_COLUMNS = {
    "request_finalize_records": {
        "owner_id", "idempotency_key", "request_digest", "plan_hash", "room_id",
        "source_request_event_id", "source_outcome_event_ids", "finalize_event_id",
        "finalize_seq", "agent_id", "execution_binding_digest", "projection_fingerprint",
        "created_at",
    },
}


def upgrade(db):
    db.execute("""CREATE TABLE request_finalize_records (
        owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        idempotency_key VARCHAR(200) NOT NULL,
        request_digest VARCHAR(64) NOT NULL CHECK (length(request_digest) = 64),
        plan_hash VARCHAR(64) NOT NULL CHECK (length(plan_hash) = 64),
        room_id INTEGER NOT NULL,
        source_request_event_id VARCHAR(36) NOT NULL REFERENCES room_events(event_id),
        source_outcome_event_ids JSON NOT NULL,
        finalize_event_id VARCHAR(36) NOT NULL UNIQUE REFERENCES room_events(event_id),
        finalize_seq INTEGER NOT NULL CHECK (finalize_seq > 0),
        agent_id INTEGER NOT NULL REFERENCES agents(id),
        execution_binding_digest VARCHAR(64) NOT NULL CHECK (length(execution_binding_digest) = 64),
        projection_fingerprint VARCHAR(64) NOT NULL CHECK (length(projection_fingerprint) = 64),
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (owner_id,idempotency_key),
        FOREIGN KEY (room_id,owner_id) REFERENCES conversations(id,user_id) ON DELETE CASCADE,
        CHECK (length(idempotency_key) BETWEEN 1 AND 200)
    )""")
    db.execute("CREATE INDEX ix_request_finalize_source ON request_finalize_records(owner_id,room_id,source_request_event_id)")
