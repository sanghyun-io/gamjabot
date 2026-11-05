"""Persist owner-scoped named room setups for active projects."""

VERSION = 14
REQUIRED_COLUMNS = {
    "project_room_templates": {
        "id", "owner_id", "project_id", "name", "room_title", "max_rounds",
        "max_tool_calls", "max_executor_seconds", "created_at", "updated_at",
    },
    "project_room_template_members": {"template_id", "agent_id"},
}


def upgrade(db):
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_projects_id_user_id ON projects(id,user_id)")
    db.execute("""CREATE TABLE project_room_templates (
        id INTEGER PRIMARY KEY,
        owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        project_id INTEGER NOT NULL,
        name VARCHAR NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 100),
        room_title VARCHAR NOT NULL CHECK (length(trim(room_title)) BETWEEN 1 AND 200),
        max_rounds INTEGER NOT NULL CHECK (max_rounds BETWEEN 1 AND 4),
        max_tool_calls INTEGER NOT NULL CHECK (max_tool_calls BETWEEN 0 AND 64),
        max_executor_seconds INTEGER NOT NULL CHECK (max_executor_seconds BETWEEN 1 AND 3600),
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (owner_id, project_id, name),
        FOREIGN KEY (project_id, owner_id) REFERENCES projects(id,user_id) ON DELETE CASCADE
    )""")
    db.execute("""CREATE TABLE project_room_template_members (
        template_id INTEGER NOT NULL REFERENCES project_room_templates(id) ON DELETE CASCADE,
        agent_id INTEGER NOT NULL REFERENCES agents(id),
        PRIMARY KEY (template_id, agent_id)
    )""")
