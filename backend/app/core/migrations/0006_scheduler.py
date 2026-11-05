"""Persist schedule triggers, bounded room defaults, and turn identity."""

VERSION = 6
REQUIRED_COLUMNS = {
    "room_agent_runs": {"trigger_event_id", "active_tool"},
    "room_triggers": {"event_id", "room_id", "state", "selected_agents", "max_rounds", "max_tool_calls", "max_executor_seconds", "current_round"},
    "room_schedule_settings": {"room_id", "max_rounds", "max_tool_calls", "max_executor_seconds"},
    "tool_actions": {"trigger_event_id", "call_id"},
}


def upgrade(db):
    db.execute("ALTER TABLE room_agent_runs ADD COLUMN trigger_event_id VARCHAR(36)")
    db.execute("ALTER TABLE room_agent_runs ADD COLUMN active_tool TEXT NOT NULL DEFAULT '{}'")
    db.execute("ALTER TABLE tool_actions ADD COLUMN trigger_event_id VARCHAR(36)")
    db.execute("ALTER TABLE tool_actions ADD COLUMN call_id VARCHAR")
    db.execute("CREATE UNIQUE INDEX ix_tool_actions_trigger_call ON tool_actions(room_id,trigger_event_id,call_id)")
    db.execute("CREATE TABLE room_triggers (event_id VARCHAR(36) PRIMARY KEY REFERENCES room_events(event_id), "
               "room_id INTEGER NOT NULL REFERENCES conversations(id), state VARCHAR NOT NULL DEFAULT 'pending', "
               "selected_agents TEXT NOT NULL DEFAULT '[]', max_rounds INTEGER NOT NULL DEFAULT 2, "
               "max_tool_calls INTEGER NOT NULL DEFAULT 8, max_executor_seconds INTEGER NOT NULL DEFAULT 60, "
               "current_round INTEGER NOT NULL DEFAULT 0, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    db.execute("CREATE INDEX ix_room_triggers_room_state ON room_triggers(room_id,state)")
    db.execute("CREATE TABLE room_schedule_settings (room_id INTEGER PRIMARY KEY REFERENCES conversations(id), "
               "max_rounds INTEGER NOT NULL DEFAULT 2, max_tool_calls INTEGER NOT NULL DEFAULT 8, "
               "max_executor_seconds INTEGER NOT NULL DEFAULT 60)")
    # Pre-scheduler history is complete history, never a new dispatch request.
    db.execute("INSERT INTO room_triggers (event_id,room_id,state,selected_agents) "
               "SELECT event_id,room_id,'done','[]' FROM room_events WHERE actor_type='user' AND kind='message'")
