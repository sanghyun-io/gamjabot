"""Keep desktop lease identity across restarts without replacing existing data."""

VERSION = 10
REQUIRED_COLUMNS = {"desktop_lease": {"agent_id", "session_id", "lease_id"}}


def upgrade(db):
    db.execute("ALTER TABLE desktop_lease ADD COLUMN agent_id INTEGER")
    db.execute("ALTER TABLE desktop_lease ADD COLUMN session_id VARCHAR(36)")
    db.execute("ALTER TABLE desktop_lease ADD COLUMN lease_id VARCHAR(32)")
