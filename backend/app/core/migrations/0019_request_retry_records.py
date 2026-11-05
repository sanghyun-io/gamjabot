"""Persist atomic owner-scoped targeted retry results."""

VERSION = 19
REQUIRED_COLUMNS = {
    "request_retry_records": {
        "owner_id", "idempotency_key", "request_digest", "plan_hash", "room_id",
        "source_failure_event_id", "source_trigger_event_id", "source_request_event_id",
        "retry_event_id", "agent_id", "retry_seq", "created_at",
    },
}


def upgrade(db):
    db.execute("""CREATE TABLE request_retry_records (
        owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        idempotency_key VARCHAR(200) NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 200),
        request_digest VARCHAR(64) NOT NULL CHECK (length(request_digest) = 64),
        plan_hash VARCHAR(64) NOT NULL CHECK (length(plan_hash) = 64),
        room_id INTEGER NOT NULL,
        source_failure_event_id VARCHAR(36) NOT NULL REFERENCES room_events(event_id),
        source_trigger_event_id VARCHAR(36) NOT NULL REFERENCES room_events(event_id),
        source_request_event_id VARCHAR(36) NOT NULL REFERENCES room_events(event_id),
        retry_event_id VARCHAR(36) NOT NULL UNIQUE REFERENCES room_events(event_id),
        agent_id INTEGER NOT NULL REFERENCES agents(id),
        retry_seq INTEGER NOT NULL CHECK (retry_seq > 0),
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (owner_id, idempotency_key),
        FOREIGN KEY (room_id, owner_id) REFERENCES conversations(id, user_id) ON DELETE CASCADE
    )""")
    db.execute("CREATE INDEX ix_request_retry_source ON request_retry_records(owner_id, room_id, source_failure_event_id)")
