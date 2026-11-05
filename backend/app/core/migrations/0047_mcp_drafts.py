"""Persist only Owner-submitted MCP draft edits and immutable proposal summary."""
VERSION=47
REQUIRED_COLUMNS={'mcp_owner_drafts':{'action_id','owner_id','room_id','revision','base_review_id','edits','pending','original_review_id','original_summary','applied_review_id','updated_at'}}
def upgrade(db):
    db.execute('''CREATE TABLE mcp_owner_drafts (action_id VARCHAR(36) PRIMARY KEY REFERENCES tool_actions(action_id),owner_id INTEGER NOT NULL REFERENCES users(id),room_id INTEGER NOT NULL REFERENCES conversations(id),revision INTEGER NOT NULL,base_review_id VARCHAR(36) NOT NULL,edits JSON NOT NULL,pending BOOLEAN NOT NULL,original_review_id VARCHAR(36) NOT NULL,original_summary JSON NOT NULL,applied_review_id VARCHAR(36),updated_at DATETIME NOT NULL)''')
