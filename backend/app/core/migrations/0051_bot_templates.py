"""Receipts for explicitly imported portable Bot templates."""
VERSION=51
REQUIRED_COLUMNS={'bot_template_import_commands':{'owner_id','idempotency_key','request_digest','response'}}

def upgrade(db):
    db.execute('''CREATE TABLE bot_template_import_commands (
        owner_id INTEGER NOT NULL REFERENCES users(id), idempotency_key VARCHAR(200) NOT NULL,
        request_digest VARCHAR(64) NOT NULL, response TEXT NOT NULL,
        PRIMARY KEY(owner_id,idempotency_key),
        CHECK(length(idempotency_key) BETWEEN 1 AND 200 AND length(request_digest)=64))''')
