"""Owner-bound proactive check-in configuration and at-most-once run ledger."""
VERSION=37
REQUIRED_COLUMNS={'primary_checkins':{'owner_id','revision','primary_revision','agent_id','room_id','enabled','prompt','interval_minutes','next_run_at','last_run_id','profile_fingerprint'},
 'primary_checkin_runs':{'run_id','owner_id','config_revision','primary_revision','agent_id','room_id','generation','scheduled_at','started_at','completed_at','state','root_event_id','result_event_id','failure_code','connection_id','model_id'}}

def upgrade(db):
    db.execute('''CREATE TABLE primary_checkins (owner_id INTEGER PRIMARY KEY REFERENCES users(id), revision INTEGER NOT NULL,
        primary_revision INTEGER NOT NULL, agent_id INTEGER REFERENCES agents(id), room_id INTEGER REFERENCES conversations(id),
        enabled BOOLEAN NOT NULL, profile_fingerprint VARCHAR(64), prompt TEXT NOT NULL, interval_minutes INTEGER NOT NULL, next_run_at DATETIME, last_run_id VARCHAR(36),
        CHECK(revision>=0 AND interval_minutes BETWEEN 1 AND 10080))''')
    db.execute('''CREATE TABLE primary_checkin_runs (run_id VARCHAR(36) PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id),
        config_revision INTEGER NOT NULL, primary_revision INTEGER NOT NULL, agent_id INTEGER NOT NULL REFERENCES agents(id),
        room_id INTEGER NOT NULL REFERENCES conversations(id), generation INTEGER NOT NULL, scheduled_at DATETIME NOT NULL,
        started_at DATETIME NOT NULL, completed_at DATETIME, state VARCHAR(16) NOT NULL, root_event_id VARCHAR(36) NOT NULL REFERENCES room_events(event_id),
        result_event_id VARCHAR(36) REFERENCES room_events(event_id), failure_code VARCHAR(64), connection_id INTEGER, model_id VARCHAR(200),
        UNIQUE(owner_id,config_revision,scheduled_at))''')
    db.execute('CREATE INDEX ix_primary_checkin_runs_owner ON primary_checkin_runs(owner_id,started_at)')
