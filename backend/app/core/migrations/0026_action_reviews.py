"""Add nullable consent-review storage without blessing legacy approvals."""

VERSION = 26
REQUIRED_COLUMNS = {
    "tool_actions": {"review_id", "review_snapshot", "approved_at", "approval_owner_id"},
}
REQUIRED_UNIQUE_INDEXES = {
    "ux_tool_actions_review_id": ("tool_actions", ("review_id",)),
}


def upgrade(db):
    db.execute("ALTER TABLE tool_actions ADD COLUMN review_id VARCHAR(36)")
    db.execute("ALTER TABLE tool_actions ADD COLUMN review_snapshot JSON")
    db.execute("ALTER TABLE tool_actions ADD COLUMN approved_at DATETIME")
    db.execute("ALTER TABLE tool_actions ADD COLUMN approval_owner_id INTEGER REFERENCES users(id)")
    db.execute("CREATE UNIQUE INDEX ux_tool_actions_review_id ON tool_actions(review_id)")
