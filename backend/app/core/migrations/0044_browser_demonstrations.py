"""Durable Owner demonstration and editable review; no runtime lease survives restart."""
VERSION = 44
REQUIRED_COLUMNS = {
    'browser_demonstrations': {'demo_id','owner_id','room_id','agent_id','profile_id','profile_revision','state','goal','steps','include_target_labels','review','review_revision','command_key','command_digest','instance_id'},
    'browser_demonstration_review_commands': {'owner_id','command_key','demo_id','digest','response'},
}

def upgrade(db):
    db.execute('''CREATE TABLE browser_demonstrations (
        demo_id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, room_id INTEGER NOT NULL, agent_id INTEGER NOT NULL,
        profile_id TEXT NOT NULL, profile_revision INTEGER NOT NULL, state TEXT NOT NULL, goal TEXT NOT NULL,
        steps JSON NOT NULL, include_target_labels BOOLEAN NOT NULL, review JSON NOT NULL, review_revision INTEGER NOT NULL,
        command_key TEXT NOT NULL, command_digest TEXT NOT NULL, instance_id TEXT NOT NULL, UNIQUE(owner_id,command_key))''')
    db.execute('''CREATE TABLE browser_demonstration_review_commands (
        owner_id INTEGER NOT NULL, command_key TEXT NOT NULL, demo_id TEXT NOT NULL, digest TEXT NOT NULL,
        response JSON NOT NULL, PRIMARY KEY(owner_id,command_key))''')
