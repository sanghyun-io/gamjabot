"""Unwired recipient-only memories for canonical shared text rooms."""
VERSION = 60
REQUIRED_COLUMNS = {
    'shared_private_memory_state': {'room_id','revision','context_after_seq'},
    'shared_private_memory_entries': {'memory_id','room_id','category','content','revision','source_event_id','source_seq','deleted_at'},
    'shared_private_memory_commands': {'room_id','root_event_id','agent_id','call_id','request_digest','response','created_at'},
}

def upgrade(db):
    db.execute('''CREATE TABLE shared_private_memory_state (
        room_id INTEGER PRIMARY KEY REFERENCES shared_text_rooms(room_id),
        revision INTEGER NOT NULL CHECK(revision>=0),
        context_after_seq INTEGER NOT NULL CHECK(context_after_seq>=0))''')
    db.execute('''CREATE TABLE shared_private_memory_entries (
        memory_id TEXT PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES shared_text_rooms(room_id),
        category TEXT NOT NULL CHECK(category IN ('fact','preference')),
        content TEXT NOT NULL CHECK(length(content)<=2000), revision INTEGER NOT NULL CHECK(revision>0),
        source_event_id TEXT NOT NULL REFERENCES room_events(event_id), source_seq INTEGER NOT NULL CHECK(source_seq>0),
        created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, deleted_at DATETIME,
        CHECK((deleted_at IS NULL AND length(content)>0) OR (deleted_at IS NOT NULL AND content='')))''')
    db.execute('CREATE INDEX ix_shared_private_memory_active ON shared_private_memory_entries(room_id,deleted_at,memory_id)')
    db.execute('''CREATE TABLE shared_private_memory_commands (
        room_id INTEGER NOT NULL REFERENCES shared_text_rooms(room_id),
        root_event_id TEXT NOT NULL REFERENCES room_events(event_id), agent_id INTEGER NOT NULL REFERENCES agents(id),
        call_id TEXT NOT NULL CHECK(length(call_id) BETWEEN 1 AND 200),
        request_digest TEXT NOT NULL CHECK(length(request_digest)=64), response TEXT NOT NULL, created_at DATETIME NOT NULL,
        PRIMARY KEY(room_id,root_event_id,agent_id,call_id))''')
