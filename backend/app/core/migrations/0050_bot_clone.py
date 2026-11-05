"""Owner-reviewed Bot copy command receipts; no copied history or secrets."""
VERSION=50
REQUIRED_COLUMNS={'bot_clone_commands':{'owner_id','idempotency_key','source_agent_id','request_digest','response'}}


def upgrade(db):
    db.execute('''CREATE TABLE bot_clone_commands (
        owner_id INTEGER NOT NULL REFERENCES users(id), idempotency_key VARCHAR(200) NOT NULL,
        source_agent_id INTEGER NOT NULL REFERENCES agents(id), request_digest VARCHAR(64) NOT NULL,
        response TEXT NOT NULL, PRIMARY KEY(owner_id,idempotency_key),
        CHECK(length(idempotency_key) BETWEEN 1 AND 200 AND length(request_digest)=64))''')
