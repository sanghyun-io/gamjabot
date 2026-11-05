"""Persist replay-safe owner project archive metadata."""

VERSION = 15
REQUIRED_COLUMNS = {
    "projects": {"archived_at", "archive_plan_hash", "archive_result"},
}


def upgrade(db):
    present = {row[1] for row in db.execute('PRAGMA table_info("projects")')}
    if "archived_at" not in present:
        db.execute("ALTER TABLE projects ADD COLUMN archived_at DATETIME")
    if "archive_plan_hash" not in present:
        db.execute("ALTER TABLE projects ADD COLUMN archive_plan_hash VARCHAR(64)")
    if "archive_result" not in present:
        db.execute("ALTER TABLE projects ADD COLUMN archive_result JSON")
