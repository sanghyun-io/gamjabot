"""Persist one bounded, retry-stable composer draft per owner and room."""

VERSION = 12
REQUIRED_COLUMNS = {
    "room_drafts": {"owner_id", "room_id", "content", "version", "idempotency_key", "updated_at"},
}


def upgrade(db):
    db.execute("CREATE UNIQUE INDEX ux_conversations_id_owner ON conversations(id,user_id)")
    db.execute("""CREATE TABLE room_drafts (
        owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        room_id INTEGER NOT NULL,
        content TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        idempotency_key VARCHAR(36),
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (owner_id, room_id),
        FOREIGN KEY (room_id, owner_id) REFERENCES conversations(id, user_id) ON DELETE CASCADE,
        CHECK (length(content) <= 65536),
        CHECK ((content = '' AND idempotency_key IS NULL) OR
               (content <> '' AND idempotency_key IS NOT NULL))
    )""")
