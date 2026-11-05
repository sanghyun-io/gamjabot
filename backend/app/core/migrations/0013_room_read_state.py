"""Persist one monotonic read cursor per owner and room."""

VERSION = 13
REQUIRED_COLUMNS = {
    "room_read_state": {"owner_id", "room_id", "last_read_seq", "updated_at"},
}


def upgrade(db):
    db.execute("""CREATE TABLE room_read_state (
        owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        room_id INTEGER NOT NULL,
        last_read_seq INTEGER NOT NULL CHECK (last_read_seq >= 0),
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (owner_id, room_id),
        FOREIGN KEY (room_id, owner_id) REFERENCES conversations(id, user_id) ON DELETE CASCADE
    )""")
