"""Text-only voice call sessions and exact conversation event references."""
VERSION = 57
REQUIRED_COLUMNS = {
    'voice_calls': {'id', 'owner_id', 'room_id', 'agent_id', 'owner_epoch', 'status', 'started_at', 'ended_at', 'lease_expires_at'},
    'voice_call_turns': {'call_id', 'client_turn_id', 'user_event_id'},
}

def upgrade(db):
    db.execute('''CREATE TABLE voice_calls (
        id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id),
        room_id INTEGER NOT NULL REFERENCES conversations(id), agent_id INTEGER NOT NULL REFERENCES agents(id),
        owner_epoch TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','ended','interrupted')),
        started_at DATETIME NOT NULL, ended_at DATETIME, lease_expires_at DATETIME NOT NULL)''')
    db.execute("CREATE UNIQUE INDEX ux_voice_calls_active_owner ON voice_calls(owner_id) WHERE status='active'")
    db.execute('''CREATE TABLE voice_call_turns (
        call_id TEXT NOT NULL REFERENCES voice_calls(id), client_turn_id TEXT NOT NULL,
        user_event_id TEXT NOT NULL UNIQUE REFERENCES room_events(event_id),
        PRIMARY KEY(call_id,client_turn_id))''')
