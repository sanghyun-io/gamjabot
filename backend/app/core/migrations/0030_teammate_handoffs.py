"""Persist bounded teammate request/return mailboxes rooted in real user requests."""
VERSION = 30
REQUIRED_COLUMNS = {
    "teammate_handoffs": {"handoff_id", "owner_id", "room_id", "root_request_event_id",
        "parent_handoff_id", "sender_agent_id", "recipient_agent_id", "sender_name", "recipient_name",
        "content", "delivery_key", "state", "reply_state", "depth", "request_event_id",
        "result_event_id", "return_event_id", "created_at", "updated_at"},
    "teammate_budgets": {"root_request_event_id", "provider_calls"},
}


def upgrade(db):
    db.execute("""CREATE TABLE teammate_handoffs (
        handoff_id VARCHAR(36) PRIMARY KEY NOT NULL,
        owner_id INTEGER NOT NULL REFERENCES users(id), room_id INTEGER NOT NULL REFERENCES conversations(id),
        root_request_event_id VARCHAR(36) NOT NULL REFERENCES room_events(event_id),
        parent_handoff_id VARCHAR(36) REFERENCES teammate_handoffs(handoff_id),
        sender_agent_id INTEGER NOT NULL REFERENCES agents(id), recipient_agent_id INTEGER NOT NULL REFERENCES agents(id),
        sender_name TEXT NOT NULL, recipient_name TEXT NOT NULL, content TEXT NOT NULL,
        delivery_key VARCHAR(64) NOT NULL, state VARCHAR(16) NOT NULL, reply_state VARCHAR(16) NOT NULL,
        depth INTEGER NOT NULL CHECK(depth BETWEEN 1 AND 3),
        request_event_id VARCHAR(36) NOT NULL REFERENCES room_events(event_id),
        result_event_id VARCHAR(36) REFERENCES room_events(event_id), return_event_id VARCHAR(36) REFERENCES room_events(event_id),
        created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
        UNIQUE(root_request_event_id,delivery_key), CHECK(sender_agent_id != recipient_agent_id)
    )""")
    db.execute("CREATE INDEX ix_teammate_root ON teammate_handoffs(root_request_event_id,state,reply_state)")
    db.execute("CREATE INDEX ix_teammate_room ON teammate_handoffs(owner_id,room_id,created_at)")
    db.execute("""CREATE TABLE teammate_budgets (
        root_request_event_id VARCHAR(36) PRIMARY KEY NOT NULL REFERENCES room_events(event_id),
        provider_calls INTEGER NOT NULL CHECK(provider_calls >= 0)
    )""")
