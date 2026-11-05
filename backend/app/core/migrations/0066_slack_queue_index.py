"""Index Slack queue scans without rebuilding or changing existing records."""
VERSION = 66
REQUIRED_COLUMNS = {'slack_reply_resolutions': {'run_id','owner_id','operation_id','attempt_id','decision','config_revision','run_revision','expected_state','owner_epoch','core_epoch','created_at'}}

def upgrade(db):
    db.execute('CREATE INDEX slack_inbox_queue_order ON slack_inbox(owner_id,state,created_at,workspace_id,event_id)')
    db.execute('CREATE INDEX slack_inbox_thread_order ON slack_inbox(owner_id,workspace_id,channel_id,thread_ts,state,created_at,event_id)')
    db.execute('CREATE TABLE slack_reply_resolutions (run_id VARCHAR(36) NOT NULL REFERENCES slack_runs(run_id),owner_id INTEGER NOT NULL REFERENCES users(id),operation_id VARCHAR(36) NOT NULL,attempt_id VARCHAR(36),decision VARCHAR(32) NOT NULL,config_revision INTEGER NOT NULL,run_revision INTEGER NOT NULL,expected_state VARCHAR(32) NOT NULL,owner_epoch VARCHAR(64) NOT NULL,core_epoch VARCHAR(64) NOT NULL,created_at DATETIME NOT NULL,PRIMARY KEY(owner_id,operation_id))')
    db.execute('CREATE INDEX slack_reply_resolutions_run ON slack_reply_resolutions(run_id,decision)')
