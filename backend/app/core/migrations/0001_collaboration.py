"""Initial legacy schema and room-scoped collaboration foundation."""

import json
import uuid


VERSION = 1
MESSAGE_NAMESPACE = uuid.UUID("e53da351-c510-47b1-91e9-5edf3da94f3a")
LEGACY_TABLE_NAMES = frozenset({
    "users", "agents", "projects", "project_agents", "conversations", "messages", "remote_devices"
})

# Immutable revision-1 readiness contract, independent of later ORM changes.
REQUIRED_COLUMNS = {
    "users": {"id", "email", "username", "hashed_password", "is_active", "created_at", "updated_at"},
    "agents": {"id", "user_id", "name", "description", "system_prompt", "model", "temperature", "max_tokens", "is_active", "created_at", "updated_at"},
    "projects": {"id", "user_id", "name", "description", "is_active", "created_at", "updated_at"},
    "project_agents": {"project_id", "agent_id", "assigned_at"},
    "conversations": {"id", "user_id", "project_id", "title", "is_active", "created_at", "updated_at"},
    "messages": {"id", "conversation_id", "agent_id", "role", "content", "created_at"},
    "remote_devices": {"id", "user_id", "name", "access_token", "is_active", "last_connected_at", "created_at", "updated_at"},
    "room_events": {"event_id", "device_id", "room_id", "seq", "actor_type", "actor_id", "actor_name_snapshot", "kind", "payload", "created_at", "idempotency_key"},
    "room_memberships": {"room_id", "agent_id", "is_active", "joined_at"},
    "room_agent_runs": {"room_id", "agent_id", "state", "watermark_seq", "round", "updated_at"},
    "provider_connections": {"id", "user_id", "kind", "name", "base_url", "secret_ref", "created_at", "updated_at"},
    "room_grants": {"id", "room_id", "agent_id", "scope", "decision", "expires_at", "created_at"},
    "tool_actions": {"action_id", "room_id", "agent_id", "grant_id", "family", "arguments", "state", "workflow_id", "started_at", "completed_at", "observation"},
    "desktop_lease": {"id", "room_id", "workflow_id", "acquired_at", "expires_at"},
    "schema_version": {"version", "applied_at"},
}

LEGACY_SCHEMA = (
    "CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR NOT NULL UNIQUE, username VARCHAR NOT NULL UNIQUE, hashed_password VARCHAR NOT NULL, is_active BOOLEAN, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME)",
    "CREATE TABLE agents (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), name VARCHAR NOT NULL, description TEXT NOT NULL, system_prompt TEXT NOT NULL, model VARCHAR NOT NULL, temperature VARCHAR NOT NULL, max_tokens INTEGER NOT NULL, is_active BOOLEAN, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME)",
    "CREATE TABLE projects (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), name VARCHAR NOT NULL, description TEXT, is_active BOOLEAN, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME)",
    "CREATE TABLE project_agents (project_id INTEGER NOT NULL REFERENCES projects(id), agent_id INTEGER NOT NULL REFERENCES agents(id), assigned_at DATETIME DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (project_id,agent_id))",
    "CREATE TABLE conversations (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), project_id INTEGER NOT NULL REFERENCES projects(id), title VARCHAR NOT NULL, is_active BOOLEAN, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME)",
    "CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL REFERENCES conversations(id), agent_id INTEGER REFERENCES agents(id), role VARCHAR NOT NULL, content TEXT NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE remote_devices (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), name VARCHAR NOT NULL, access_token VARCHAR NOT NULL UNIQUE, is_active BOOLEAN, last_connected_at DATETIME, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME)",
    "CREATE INDEX ix_users_id ON users(id)",
    "CREATE INDEX ix_users_email ON users(email)",
    "CREATE INDEX ix_users_username ON users(username)",
    "CREATE INDEX ix_agents_id ON agents(id)",
    "CREATE INDEX ix_projects_id ON projects(id)",
    "CREATE INDEX ix_conversations_id ON conversations(id)",
    "CREATE INDEX ix_messages_id ON messages(id)",
)

COLLABORATION_SCHEMA = (
    "CREATE TABLE room_events (event_id VARCHAR(36) PRIMARY KEY, device_id INTEGER REFERENCES remote_devices(id), room_id INTEGER NOT NULL REFERENCES conversations(id), seq INTEGER NOT NULL, actor_type VARCHAR NOT NULL CHECK(actor_type IN ('user','agent','tool','system')), actor_id INTEGER, actor_name_snapshot VARCHAR NOT NULL, kind VARCHAR NOT NULL CHECK(kind IN ('message','tool_result','failure','summary','activity')), payload TEXT NOT NULL, created_at DATETIME NOT NULL, idempotency_key VARCHAR, UNIQUE(room_id,seq), UNIQUE(room_id,idempotency_key))",
    "CREATE INDEX ix_room_events_room_seq ON room_events(room_id,seq)",
    "CREATE TABLE room_memberships (room_id INTEGER NOT NULL REFERENCES conversations(id), agent_id INTEGER NOT NULL REFERENCES agents(id), is_active BOOLEAN NOT NULL DEFAULT 1, joined_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(room_id,agent_id))",
    "CREATE TABLE room_agent_runs (room_id INTEGER NOT NULL, agent_id INTEGER NOT NULL, state VARCHAR NOT NULL DEFAULT 'idle', watermark_seq INTEGER, round INTEGER, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(room_id,agent_id), FOREIGN KEY(room_id,agent_id) REFERENCES room_memberships(room_id,agent_id))",
    "CREATE TABLE provider_connections (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), kind VARCHAR NOT NULL, name VARCHAR NOT NULL, base_url VARCHAR, secret_ref VARCHAR, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME)",
    "CREATE TABLE room_grants (id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES conversations(id), agent_id INTEGER REFERENCES agents(id), scope TEXT NOT NULL, decision VARCHAR NOT NULL, expires_at DATETIME, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE tool_actions (action_id VARCHAR(36) PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES conversations(id), agent_id INTEGER REFERENCES agents(id), grant_id INTEGER REFERENCES room_grants(id), family VARCHAR NOT NULL, arguments TEXT NOT NULL, state VARCHAR NOT NULL, workflow_id VARCHAR, started_at DATETIME, completed_at DATETIME, observation TEXT)",
    "CREATE INDEX ix_tool_actions_room_state ON tool_actions(room_id,state)",
    "CREATE TABLE desktop_lease (id INTEGER PRIMARY KEY CHECK(id=1), room_id INTEGER NOT NULL REFERENCES conversations(id), workflow_id VARCHAR NOT NULL, acquired_at DATETIME NOT NULL, expires_at DATETIME NOT NULL)",
)


def upgrade(db):
    legacy = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not (legacy & LEGACY_TABLE_NAMES):
        if legacy - {"schema_version"}:
            raise RuntimeError("Unrecognized SQLite schema; refusing to initialize over existing data")
        for statement in LEGACY_SCHEMA:
            db.execute(statement)
    elif not LEGACY_TABLE_NAMES <= legacy:
        raise RuntimeError("Incomplete legacy schema; manual recovery required")

    for statement in COLLABORATION_SCHEMA:
        db.execute(statement)

    db.execute(
        "INSERT INTO room_memberships (room_id,agent_id) "
        "SELECT c.id,pa.agent_id FROM conversations c "
        "JOIN project_agents pa ON pa.project_id=c.project_id"
    )
    rows = db.execute(
        "SELECT m.id,m.conversation_id,m.agent_id,m.role,m.content,m.created_at,"
        "c.user_id,u.username,a.name FROM messages m "
        "JOIN conversations c ON c.id=m.conversation_id "
        "LEFT JOIN users u ON u.id=c.user_id LEFT JOIN agents a ON a.id=m.agent_id "
        "ORDER BY m.conversation_id,m.created_at,m.id"
    )
    last_room = None
    seq = 0
    for message_id, room_id, agent_id, role, content, created_at, user_id, username, agent_name in rows:
        seq = seq + 1 if room_id == last_room else 1
        last_room = room_id
        actor_type = "agent" if agent_id is not None else "user" if role == "user" else "system"
        actor_id = agent_id if actor_type == "agent" else user_id if actor_type == "user" else None
        actor_name = agent_name if actor_type == "agent" else username if actor_type == "user" else "System"
        db.execute(
            "INSERT INTO room_events (event_id,room_id,seq,actor_type,actor_id,actor_name_snapshot,kind,payload,created_at) "
            "VALUES (?,?,?,?,?,?,'message',?,?)",
            (
                str(uuid.uuid5(MESSAGE_NAMESPACE, str(message_id))), room_id, seq,
                actor_type, actor_id, actor_name or "Unknown",
                json.dumps({"content": content, "role": role, "legacy_message_id": message_id}),
                created_at or "1970-01-01 00:00:00",
            ),
        )
