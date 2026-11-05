"""Add explicit delegated authority; never manufacture old grants or reviews."""
VERSION = 34
REQUIRED_COLUMNS = {'tool_actions': {'execution_binding', 'executor_reservation_ms'},
    'execution_scopes': {'scope_id','root_request_event_id','owner_id','root_room_id','parent_scope_id','kind',
    'execution_room_id','agent_id','job_id','job_kind','epoch','state','deadline_at','revocation_state',
    'native_calls','max_native_calls','executor_used_ms','executor_reserved_ms','max_executor_ms'}}
REQUIRED_COLUMNS['scope_stop_receipts'] = {'stop_id','owner_id','room_id','revocations','receipts','state','created_at'}


def upgrade(db):
    db.execute('ALTER TABLE tool_actions ADD COLUMN execution_binding JSON')
    db.execute('ALTER TABLE tool_actions ADD COLUMN executor_reservation_ms INTEGER')
    db.execute('''CREATE TABLE execution_scopes (
        scope_id VARCHAR(36) PRIMARY KEY NOT NULL,
        root_request_event_id VARCHAR(36) NOT NULL REFERENCES room_events(event_id),
        owner_id INTEGER NOT NULL REFERENCES users(id), root_room_id INTEGER NOT NULL REFERENCES conversations(id),
        parent_scope_id VARCHAR(36) REFERENCES execution_scopes(scope_id), kind VARCHAR(16) NOT NULL,
        execution_room_id INTEGER REFERENCES conversations(id), agent_id INTEGER REFERENCES agents(id),
        job_id VARCHAR(36), job_kind VARCHAR(16), epoch INTEGER NOT NULL DEFAULT 0,
        state VARCHAR(16) NOT NULL DEFAULT 'active', deadline_at DATETIME NOT NULL, revocation_state VARCHAR(16),
        native_calls INTEGER NOT NULL DEFAULT 0, max_native_calls INTEGER NOT NULL DEFAULT 0,
        executor_used_ms INTEGER NOT NULL DEFAULT 0, executor_reserved_ms INTEGER NOT NULL DEFAULT 0,
        max_executor_ms INTEGER NOT NULL DEFAULT 0,
        CHECK(epoch>=0 AND native_calls>=0 AND executor_used_ms>=0 AND executor_reserved_ms>=0)
    )''')
    db.execute('CREATE INDEX ix_execution_scope_root ON execution_scopes(root_request_event_id)')
    db.execute('CREATE INDEX ix_execution_scope_parent ON execution_scopes(parent_scope_id)')
    db.execute('CREATE INDEX ix_execution_scope_room ON execution_scopes(execution_room_id,agent_id,state)')
    db.execute('''CREATE TABLE scope_stop_receipts (
        stop_id VARCHAR(36) PRIMARY KEY NOT NULL, owner_id INTEGER NOT NULL REFERENCES users(id),
        room_id INTEGER NOT NULL REFERENCES conversations(id), revocations JSON NOT NULL,
        receipts JSON NOT NULL, state VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)''')
