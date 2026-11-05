"""Persist atomic starter results and prepare explicit test metering fields."""

VERSION = 20
REQUIRED_COLUMNS = {
    "connection_test_history": {"profile", "actual_request_count"},
    "starter_requests": {
        "id", "owner_id", "idempotency_key", "request_digest", "plan_hash",
        "project_id", "room_id", "proposer_agent_id", "reviewer_agent_id", "created_at",
    },
}


def upgrade(db):
    db.execute("ALTER TABLE connection_test_history ADD COLUMN profile VARCHAR(10) CHECK (profile IS NULL OR profile IN ('text','full'))")
    db.execute("ALTER TABLE connection_test_history ADD COLUMN actual_request_count INTEGER CHECK (actual_request_count IS NULL OR actual_request_count >= 0)")
    db.execute("""CREATE TABLE starter_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        idempotency_key VARCHAR(200) NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 200),
        request_digest VARCHAR(64) NOT NULL CHECK (length(request_digest) = 64),
        plan_hash VARCHAR(64) NOT NULL CHECK (length(plan_hash) = 64),
        project_id INTEGER NOT NULL,
        room_id INTEGER NOT NULL,
        proposer_agent_id INTEGER NOT NULL REFERENCES agents(id),
        reviewer_agent_id INTEGER NOT NULL REFERENCES agents(id),
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(owner_id,idempotency_key),
        FOREIGN KEY (project_id,owner_id) REFERENCES projects(id,user_id) ON DELETE CASCADE,
        FOREIGN KEY (room_id,owner_id) REFERENCES conversations(id,user_id) ON DELETE CASCADE,
        CHECK (proposer_agent_id <> reviewer_agent_id)
    )""")
    db.execute("CREATE INDEX ix_starter_requests_owner_latest ON starter_requests(owner_id,id DESC)")
