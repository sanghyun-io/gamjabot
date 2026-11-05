"""Atomic bounded batch delivery; no historical action or grant rewrite."""
VERSION=35
REQUIRED_COLUMNS={'bot_messages':{'batch_id','batch_index'},'bot_batches':{'batch_id','owner_id','root_room_id',
    'root_request_event_id','sender_room_id','sender_agent_id','sender_name','parent_job_id','parent_kind',
    'source_scope_id','content','delivery_key','state','reply_state','failure_code','request_event_id',
    'return_delivery_event_id','return_event_id','deadline_at','created_at','updated_at'}}


def upgrade(db):
    db.execute('''CREATE TABLE bot_batches (
        batch_id VARCHAR(36) PRIMARY KEY NOT NULL,owner_id INTEGER NOT NULL REFERENCES users(id),
        root_room_id INTEGER NOT NULL REFERENCES conversations(id),root_request_event_id VARCHAR(36) NOT NULL REFERENCES room_events(event_id),
        sender_room_id INTEGER NOT NULL REFERENCES conversations(id),sender_agent_id INTEGER NOT NULL REFERENCES agents(id),
        sender_name TEXT NOT NULL,parent_job_id VARCHAR(36),parent_kind VARCHAR(16),
        source_scope_id VARCHAR(36) NOT NULL REFERENCES execution_scopes(scope_id),content TEXT NOT NULL,
        delivery_key VARCHAR(64) NOT NULL,state VARCHAR(16) NOT NULL,reply_state VARCHAR(16) NOT NULL,failure_code VARCHAR(64),
        request_event_id VARCHAR(36) NOT NULL REFERENCES room_events(event_id),
        return_delivery_event_id VARCHAR(36) REFERENCES room_events(event_id),return_event_id VARCHAR(36) REFERENCES room_events(event_id),
        deadline_at DATETIME NOT NULL,created_at DATETIME NOT NULL,updated_at DATETIME NOT NULL,
        UNIQUE(root_request_event_id,delivery_key),CHECK(parent_kind IS NULL OR parent_kind IN ('bot_message','teammate'))
    )''')
    db.execute('ALTER TABLE bot_messages ADD COLUMN batch_id VARCHAR(36) REFERENCES bot_batches(batch_id)')
    db.execute('ALTER TABLE bot_messages ADD COLUMN batch_index INTEGER')
    db.execute('CREATE UNIQUE INDEX ux_bot_batch_index ON bot_messages(batch_id,batch_index) WHERE batch_id IS NOT NULL')
    db.execute('CREATE INDEX ix_bot_batch_root ON bot_batches(root_request_event_id,reply_state)')
