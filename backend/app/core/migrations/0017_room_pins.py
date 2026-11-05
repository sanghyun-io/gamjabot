"""Persist the owner's durable room navigation pins."""

VERSION = 17
REQUIRED_COLUMNS = {
    "room_pins": {"owner_id", "room_id", "pinned_at"},
}


def upgrade(db):
    db.execute("""CREATE TABLE room_pins (
        owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        room_id INTEGER NOT NULL,
        pinned_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (owner_id, room_id),
        FOREIGN KEY (room_id, owner_id) REFERENCES conversations(id, user_id) ON DELETE CASCADE
    )""")
    db.execute("CREATE INDEX ix_room_pins_owner_time ON room_pins(owner_id, pinned_at DESC, room_id DESC)")
