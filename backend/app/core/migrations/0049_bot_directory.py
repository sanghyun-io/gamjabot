"""Owner-scoped Bot directory layout, independent of Bot execution state."""
VERSION = 49
REQUIRED_COLUMNS = {
    'bot_directory_layouts': {'owner_id', 'revision', 'snapshot'},
    'bot_directory_commands': {'owner_id', 'idempotency_key', 'request_digest', 'response'},
}


def upgrade(db):
    db.execute('''CREATE TABLE bot_directory_layouts (
        owner_id INTEGER PRIMARY KEY REFERENCES users(id),
        revision INTEGER NOT NULL CHECK(revision >= 1), snapshot TEXT NOT NULL)''')
    db.execute('''CREATE TABLE bot_directory_commands (
        owner_id INTEGER NOT NULL REFERENCES users(id), idempotency_key VARCHAR(200) NOT NULL,
        request_digest VARCHAR(64) NOT NULL, response TEXT NOT NULL,
        PRIMARY KEY(owner_id,idempotency_key),
        CHECK(length(idempotency_key) BETWEEN 1 AND 200 AND length(request_digest)=64))''')
