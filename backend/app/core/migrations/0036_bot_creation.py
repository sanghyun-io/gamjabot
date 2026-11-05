"""Atomic conversational Bot creation receipts, without rewriting legacy Agents/grants."""
VERSION=36
REQUIRED_COLUMNS={'bot_creations':{'creation_id','owner_id','root_room_id','root_request_event_id',
    'source_agent_id','source_generation','call_id','payload_digest','source_profile_fingerprint',
    'created_agent_id','created_room_id','request_event_id','result_event_id','response','created_at'}}


def upgrade(db):
    db.execute('''CREATE TABLE bot_creations (
        creation_id VARCHAR(36) PRIMARY KEY NOT NULL,owner_id INTEGER NOT NULL REFERENCES users(id),
        root_room_id INTEGER NOT NULL REFERENCES conversations(id),root_request_event_id VARCHAR(36) NOT NULL REFERENCES room_events(event_id),
        source_agent_id INTEGER NOT NULL REFERENCES agents(id),source_generation INTEGER NOT NULL,
        call_id VARCHAR(200) NOT NULL,payload_digest VARCHAR(64) NOT NULL,source_profile_fingerprint VARCHAR(64) NOT NULL,
        created_agent_id INTEGER NOT NULL UNIQUE REFERENCES agents(id),created_room_id INTEGER NOT NULL UNIQUE REFERENCES conversations(id),
        request_event_id VARCHAR(36) NOT NULL REFERENCES room_events(event_id),result_event_id VARCHAR(36) NOT NULL REFERENCES room_events(event_id),
        response JSON NOT NULL,created_at DATETIME NOT NULL,
        UNIQUE(root_request_event_id,source_agent_id,call_id),CHECK(source_generation>=0)
    )''')
    db.execute('CREATE INDEX ix_bot_creation_root ON bot_creations(root_request_event_id)')
