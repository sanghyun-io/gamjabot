"""Local notification settings and at-most-once attempts; no message content."""
VERSION=65
REQUIRED_COLUMNS={
 'local_notification_settings':{'owner_id','agent_id','revision','completion','failure','approval','updated_at'},
 'local_notification_commands':{'owner_id','operation_id','request_digest','agent_id','revision','created_at'},
 'local_notification_sessions':{'session_id','owner_id','owner_epoch','started_at'},
 'local_notification_deliveries':{'delivery_id','owner_id','owner_epoch','session_id','agent_id','kind','source_id','room_id','state','created_at','updated_at'},
}
def upgrade(db):
 db.execute('CREATE TABLE local_notification_settings (owner_id INTEGER NOT NULL REFERENCES users(id),agent_id INTEGER NOT NULL REFERENCES agents(id),revision INTEGER NOT NULL,completion BOOLEAN NOT NULL,failure BOOLEAN NOT NULL,approval BOOLEAN NOT NULL,updated_at DATETIME NOT NULL,PRIMARY KEY(owner_id,agent_id))')
 db.execute('CREATE TABLE local_notification_commands (owner_id INTEGER NOT NULL REFERENCES users(id),operation_id VARCHAR(36) NOT NULL,request_digest VARCHAR(64) NOT NULL,agent_id INTEGER NOT NULL REFERENCES agents(id),revision INTEGER NOT NULL,created_at DATETIME NOT NULL,PRIMARY KEY(owner_id,operation_id))')
 db.execute('CREATE TABLE local_notification_sessions (session_id VARCHAR(36) PRIMARY KEY,owner_id INTEGER NOT NULL UNIQUE REFERENCES users(id),owner_epoch VARCHAR(64) NOT NULL,started_at DATETIME NOT NULL)')
 db.execute('CREATE TABLE local_notification_deliveries (delivery_id VARCHAR(36) PRIMARY KEY,owner_id INTEGER NOT NULL REFERENCES users(id),owner_epoch VARCHAR(64) NOT NULL,session_id VARCHAR(36) NOT NULL,agent_id INTEGER NOT NULL REFERENCES agents(id),kind VARCHAR(16) NOT NULL,source_id VARCHAR(36) NOT NULL,room_id INTEGER NOT NULL REFERENCES conversations(id),state VARCHAR(16) NOT NULL,created_at DATETIME NOT NULL,updated_at DATETIME NOT NULL,UNIQUE(owner_id,kind,source_id))')
