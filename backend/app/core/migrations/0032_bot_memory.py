"""Add explicit Bot-private memory without converting any room-local memory."""
VERSION = 32
REQUIRED_COLUMNS = {
    "bot_memory_entries": {"memory_id", "owner_id", "agent_id", "category", "content", "source_kind",
        "source_room_id", "source_event_id", "source_seq", "source_url", "source_snapshot", "revision",
        "created_by", "updated_by", "created_at", "updated_at", "deleted_at"},
    "bot_memory_state": {"owner_id", "agent_id", "revision"},
    "bot_memory_commands": {"owner_id", "idempotency_key", "agent_id", "request_digest", "response", "created_at"},
}


def upgrade(db):
    db.execute("""CREATE TABLE bot_memory_entries (
        memory_id VARCHAR(36) PRIMARY KEY NOT NULL,
        owner_id INTEGER NOT NULL REFERENCES users(id), agent_id INTEGER NOT NULL REFERENCES agents(id),
        category VARCHAR(16) NOT NULL CHECK(category IN ('fact','preference','role')),
        content TEXT NOT NULL CHECK(length(content) BETWEEN 1 AND 2000), source_kind VARCHAR(8) NOT NULL,
        source_room_id INTEGER, source_event_id VARCHAR(36), source_seq INTEGER, source_url VARCHAR(2048),
        source_snapshot JSON NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
        created_by INTEGER NOT NULL REFERENCES users(id), updated_by INTEGER NOT NULL REFERENCES users(id),
        created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, deleted_at DATETIME,
        FOREIGN KEY(source_room_id,owner_id) REFERENCES conversations(id,user_id),
        FOREIGN KEY(source_event_id,source_room_id) REFERENCES room_events(event_id,room_id),
        CHECK(created_by=owner_id AND updated_by=owner_id),
        CHECK((source_kind='manual' AND source_room_id IS NULL AND source_event_id IS NULL AND source_seq IS NULL AND source_url IS NULL)
            OR (source_kind='event' AND source_room_id IS NOT NULL AND source_event_id IS NOT NULL AND source_seq IS NOT NULL AND source_seq >= 1 AND source_url IS NULL)
            OR (source_kind='link' AND source_room_id IS NULL AND source_event_id IS NULL AND source_seq IS NULL AND source_url IS NOT NULL))
    )""")
    db.execute("CREATE INDEX ix_bot_memory_active ON bot_memory_entries(owner_id,agent_id,deleted_at,memory_id)")
    db.execute("""CREATE TABLE bot_memory_state (
        owner_id INTEGER NOT NULL REFERENCES users(id), agent_id INTEGER NOT NULL REFERENCES agents(id),
        revision INTEGER NOT NULL CHECK(revision >= 0), PRIMARY KEY(owner_id,agent_id)
    )""")
    db.execute("""CREATE TABLE bot_memory_commands (
        owner_id INTEGER NOT NULL REFERENCES users(id), idempotency_key VARCHAR(200) NOT NULL,
        agent_id INTEGER NOT NULL REFERENCES agents(id), request_digest VARCHAR(64) NOT NULL,
        response JSON NOT NULL, created_at DATETIME NOT NULL, PRIMARY KEY(owner_id,idempotency_key),
        CHECK(length(idempotency_key) BETWEEN 1 AND 200 AND length(request_digest)=64)
    )""")
