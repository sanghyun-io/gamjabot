"""Owner-scoped memory visibility and explicitly shared Owner facts."""
VERSION=38
REQUIRED_COLUMNS={'bot_memory_entries':{'group_allowed'},'bot_memory_policies':{'owner_id','agent_id','revision','mode'},
 'owner_common_memory_state':{'owner_id','revision'},'owner_common_memory_entries':{'fact_id','owner_id','kind','content','origin_agent_id','origin_kind','origin_room_id','origin_event_id','origin_seq','revision','created_at','updated_at','deleted_at'},
 'owner_common_memory_commands':{'owner_id','idempotency_key','request_digest','response','created_at'}}

def upgrade(db):
    db.execute('ALTER TABLE bot_memory_entries ADD COLUMN group_allowed BOOLEAN NOT NULL DEFAULT 1 CHECK(group_allowed IN (0,1))')
    db.execute('''CREATE TABLE bot_memory_policies(owner_id INTEGER NOT NULL REFERENCES users(id),agent_id INTEGER NOT NULL REFERENCES agents(id),
        revision INTEGER NOT NULL CHECK(revision>=0),mode VARCHAR(20) NOT NULL CHECK(mode IN ('canonical_only','owner_rooms')),
        PRIMARY KEY(owner_id,agent_id))''')
    db.execute('''CREATE TABLE owner_common_memory_state(owner_id INTEGER PRIMARY KEY REFERENCES users(id),revision INTEGER NOT NULL CHECK(revision>=0))''')
    db.execute('''CREATE TABLE owner_common_memory_entries(fact_id VARCHAR(36) PRIMARY KEY,owner_id INTEGER NOT NULL REFERENCES users(id),
        kind VARCHAR(12) NOT NULL CHECK(kind IN ('name','timezone')),content TEXT NOT NULL CHECK(length(content) BETWEEN 1 AND 2000),
        origin_agent_id INTEGER NOT NULL REFERENCES agents(id),origin_kind VARCHAR(8) NOT NULL CHECK(origin_kind IN ('manual','event')),origin_room_id INTEGER REFERENCES conversations(id),
        origin_event_id VARCHAR(36) REFERENCES room_events(event_id),origin_seq INTEGER CHECK(origin_seq IS NULL OR origin_seq>0),
        revision INTEGER NOT NULL CHECK(revision>0),created_at DATETIME NOT NULL,updated_at DATETIME NOT NULL,deleted_at DATETIME,
        FOREIGN KEY(origin_room_id,owner_id) REFERENCES conversations(id,user_id),
        FOREIGN KEY(origin_event_id,origin_room_id) REFERENCES room_events(event_id,room_id),
        CHECK((origin_kind='manual' AND origin_room_id IS NULL AND origin_event_id IS NULL AND origin_seq IS NULL) OR
              (origin_kind='event' AND origin_room_id IS NOT NULL AND origin_event_id IS NOT NULL AND origin_seq IS NOT NULL)))''')
    db.execute('CREATE UNIQUE INDEX ux_owner_common_active_kind ON owner_common_memory_entries(owner_id,kind) WHERE deleted_at IS NULL')
    db.execute('''CREATE TABLE owner_common_memory_commands(owner_id INTEGER NOT NULL REFERENCES users(id),idempotency_key VARCHAR(200) NOT NULL,
        request_digest VARCHAR(64) NOT NULL,response JSON NOT NULL,created_at DATETIME NOT NULL,PRIMARY KEY(owner_id,idempotency_key))''')
