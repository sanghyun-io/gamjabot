"""Add a cross-room mailbox without rewriting prior rooms, memories, or grants."""
VERSION = 33
REQUIRED_COLUMNS = {"bot_messages": {"message_id", "owner_id", "root_room_id", "root_request_event_id",
    "sender_room_id", "recipient_room_id", "sender_agent_id", "recipient_agent_id", "sender_name", "recipient_name",
    "content", "delivery_key", "parent_job_id", "parent_kind", "depth", "state", "reply_state", "failure_code",
    "request_event_id", "delivery_event_id", "result_event_id", "return_delivery_event_id", "return_event_id",
    "deadline_at", "created_at", "updated_at"}}
REQUIRED_COLUMNS["bot_message_retries"] = {"owner_id", "idempotency_key", "message_id", "request_digest", "response", "created_at"}


def upgrade(db):
    db.execute("""CREATE TABLE bot_messages (
        message_id VARCHAR(36) NOT NULL PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id),
        root_room_id INTEGER NOT NULL REFERENCES conversations(id),
        root_request_event_id VARCHAR(36) NOT NULL REFERENCES room_events(event_id),
        sender_room_id INTEGER NOT NULL REFERENCES conversations(id), recipient_room_id INTEGER NOT NULL REFERENCES conversations(id),
        sender_agent_id INTEGER NOT NULL REFERENCES agents(id), recipient_agent_id INTEGER NOT NULL REFERENCES agents(id),
        sender_name TEXT NOT NULL, recipient_name TEXT NOT NULL, content TEXT NOT NULL,
        delivery_key VARCHAR(64) NOT NULL, parent_job_id VARCHAR(36), parent_kind VARCHAR(16),
        depth INTEGER NOT NULL CHECK(depth BETWEEN 1 AND 3), state VARCHAR(16) NOT NULL, reply_state VARCHAR(16) NOT NULL,
        failure_code VARCHAR(48), request_event_id VARCHAR(36) NOT NULL REFERENCES room_events(event_id),
        delivery_event_id VARCHAR(36) NOT NULL REFERENCES room_events(event_id),
        result_event_id VARCHAR(36) REFERENCES room_events(event_id),
        return_delivery_event_id VARCHAR(36) REFERENCES room_events(event_id), return_event_id VARCHAR(36) REFERENCES room_events(event_id),
        deadline_at DATETIME NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
        UNIQUE(root_request_event_id,delivery_key), CHECK(sender_agent_id != recipient_agent_id),
        CHECK(parent_kind IS NULL OR parent_kind IN ('bot_message','teammate'))
    )""")
    db.execute("CREATE INDEX ix_bot_message_root ON bot_messages(root_request_event_id,state,reply_state)")
    db.execute("CREATE INDEX ix_bot_message_inbox ON bot_messages(owner_id,recipient_room_id,created_at)")
    db.execute("""CREATE TABLE bot_message_retries (
        owner_id INTEGER NOT NULL REFERENCES users(id), idempotency_key VARCHAR(200) NOT NULL,
        message_id VARCHAR(36) NOT NULL REFERENCES bot_messages(message_id), request_digest VARCHAR(64) NOT NULL,
        response JSON NOT NULL, created_at DATETIME NOT NULL, PRIMARY KEY(owner_id,idempotency_key),
        CHECK(length(idempotency_key) BETWEEN 1 AND 200 AND length(request_digest)=64)
    )""")
