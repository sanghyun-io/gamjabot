"""Separate reusable grant scope from an exact action payload."""

VERSION = 9
REQUIRED_COLUMNS = {"tool_actions": {"scope_target", "payload_digest"}}


def upgrade(db):
    db.execute("ALTER TABLE tool_actions ADD COLUMN scope_target VARCHAR")
    db.execute("ALTER TABLE tool_actions ADD COLUMN payload_digest VARCHAR(64)")
