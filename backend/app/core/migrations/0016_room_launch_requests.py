"""Persist owner-scoped idempotency for atomic custom room launch."""

VERSION = 16
REQUIRED_COLUMNS = {
    "room_launch_requests": {
        "owner_id", "idempotency_key", "payload_digest", "room_id", "created_at",
    },
}


def upgrade(db):
    db.execute("""CREATE TABLE room_launch_requests (
        owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        idempotency_key VARCHAR(200) NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 200),
        payload_digest VARCHAR(64) NOT NULL CHECK (length(payload_digest) = 64),
        room_id INTEGER NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (owner_id, idempotency_key),
        FOREIGN KEY (room_id, owner_id) REFERENCES conversations(id, user_id) ON DELETE CASCADE
    )""")
