"""Durable room-local run state and cancellation generation."""

VERSION = 2
REQUIRED_COLUMNS = {
    "room_agent_runs": {"checkpoint", "memory", "pending_task", "paused", "generation"},
}


def upgrade(db):
    db.execute("ALTER TABLE room_agent_runs ADD COLUMN checkpoint TEXT NOT NULL DEFAULT '{}'")
    db.execute("ALTER TABLE room_agent_runs ADD COLUMN memory TEXT NOT NULL DEFAULT '{}'")
    db.execute("ALTER TABLE room_agent_runs ADD COLUMN pending_task TEXT NOT NULL DEFAULT '{}'")
    db.execute("ALTER TABLE room_agent_runs ADD COLUMN paused BOOLEAN NOT NULL DEFAULT 0")
    db.execute("ALTER TABLE room_agent_runs ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
