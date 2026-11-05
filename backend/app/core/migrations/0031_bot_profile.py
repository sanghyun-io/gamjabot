"""Add primary Bot preferences and canonical DM bindings without changing old rooms."""
VERSION = 31
REQUIRED_COLUMNS = {
    "bot_owner_profiles": {"owner_id", "primary_agent_id", "revision", "personal_project_id"},
    "bot_conversations": {"owner_id", "agent_id", "room_id", "replaced_room_id", "created_at", "updated_at"},
}


def upgrade(db):
    db.execute("""CREATE TABLE bot_owner_profiles (
        owner_id INTEGER PRIMARY KEY NOT NULL REFERENCES users(id),
        primary_agent_id INTEGER REFERENCES agents(id),
        revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
        personal_project_id INTEGER REFERENCES projects(id)
    )""")
    db.execute("""CREATE TABLE bot_conversations (
        owner_id INTEGER NOT NULL REFERENCES users(id), agent_id INTEGER NOT NULL REFERENCES agents(id),
        room_id INTEGER NOT NULL UNIQUE REFERENCES conversations(id),
        replaced_room_id INTEGER,
        created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
        PRIMARY KEY(owner_id,agent_id)
    )""")
