"""Add genuine webhook origin and preserve existing routine run references."""
VERSION=45
REQUIRED_COLUMNS={
 'owner_routine_webhook_keys':{'routine_id','owner_id','generation','routine_revision','key_hash','updated_at'},
 'owner_routine_webhook_events':{'routine_id','event_key','body_digest','key_generation','routine_revision','run_id','created_at'}}
def upgrade(db):
    db.execute('CREATE TEMP TABLE routine_run_refs_45 AS SELECT request_id,run_id FROM owner_routine_pending_tests WHERE run_id IS NOT NULL')
    db.execute('UPDATE owner_routine_pending_tests SET run_id=NULL WHERE run_id IS NOT NULL')
    db.execute("""CREATE TABLE owner_routine_runs_v45 (run_id VARCHAR(36) PRIMARY KEY,routine_id VARCHAR(36) NOT NULL REFERENCES owner_routines(routine_id),owner_id INTEGER NOT NULL REFERENCES users(id),agent_id INTEGER NOT NULL REFERENCES agents(id),room_id INTEGER NOT NULL REFERENCES conversations(id),config_revision INTEGER NOT NULL,generation INTEGER NOT NULL,origin VARCHAR(12) NOT NULL CHECK(origin IN ('scheduled','test','webhook')),scheduled_at DATETIME NOT NULL,started_at DATETIME NOT NULL,completed_at DATETIME,state VARCHAR(16) NOT NULL,root_event_id VARCHAR(36) NOT NULL REFERENCES room_events(event_id),result_event_id VARCHAR(36) REFERENCES room_events(event_id),failure_code VARCHAR(64),connection_id INTEGER,model_id VARCHAR(200),max_tool_calls INTEGER NOT NULL,max_executor_seconds INTEGER NOT NULL,UNIQUE(routine_id,origin,scheduled_at))""")
    db.execute('INSERT INTO owner_routine_runs_v45 SELECT * FROM owner_routine_runs')
    if db.execute('SELECT COUNT(*) FROM owner_routine_runs').fetchone()!=db.execute('SELECT COUNT(*) FROM owner_routine_runs_v45').fetchone():raise RuntimeError('routine run count changed')
    if db.execute('SELECT * FROM owner_routine_runs EXCEPT SELECT * FROM owner_routine_runs_v45').fetchone() or db.execute('SELECT * FROM owner_routine_runs_v45 EXCEPT SELECT * FROM owner_routine_runs').fetchone():raise RuntimeError('routine run rows changed')
    db.execute('DROP TABLE owner_routine_runs')
    db.execute('ALTER TABLE owner_routine_runs_v45 RENAME TO owner_routine_runs')
    db.execute('CREATE INDEX ix_owner_routine_runs ON owner_routine_runs(routine_id,started_at)')
    db.execute('UPDATE owner_routine_pending_tests SET run_id=(SELECT run_id FROM routine_run_refs_45 WHERE routine_run_refs_45.request_id=owner_routine_pending_tests.request_id) WHERE request_id IN (SELECT request_id FROM routine_run_refs_45)')
    if db.execute('SELECT request_id,run_id FROM routine_run_refs_45 EXCEPT SELECT request_id,run_id FROM owner_routine_pending_tests').fetchone():raise RuntimeError('routine test references changed')
    db.execute('DROP TABLE routine_run_refs_45')
    db.execute("""CREATE TABLE owner_routine_webhook_keys (routine_id VARCHAR(36) PRIMARY KEY REFERENCES owner_routines(routine_id),owner_id INTEGER NOT NULL REFERENCES users(id),generation INTEGER NOT NULL CHECK(generation>=1),routine_revision INTEGER NOT NULL,key_hash VARCHAR(64),updated_at DATETIME NOT NULL)""")
    db.execute("""CREATE TABLE owner_routine_webhook_events (routine_id VARCHAR(36) NOT NULL REFERENCES owner_routines(routine_id),event_key VARCHAR(100) NOT NULL,body_digest VARCHAR(64) NOT NULL,key_generation INTEGER NOT NULL,routine_revision INTEGER NOT NULL,run_id VARCHAR(36) NOT NULL UNIQUE REFERENCES owner_routine_runs(run_id),created_at DATETIME NOT NULL,PRIMARY KEY(routine_id,event_key))""")
    if db.execute('PRAGMA foreign_key_check').fetchone():raise RuntimeError('routine webhook migration foreign key check failed')
