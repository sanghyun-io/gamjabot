"""Store only a hash of one bounded device restore credential."""

VERSION = 11
REQUIRED_COLUMNS = {"remembered_login": {"id", "owner_id", "secret_hash", "expires_at"}}


def upgrade(db):
    db.execute("""CREATE TABLE remembered_login (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        secret_hash TEXT NOT NULL,
        expires_at DATETIME NOT NULL
    )""")
