"""Persist an optional structured composer reply without changing existing drafts."""
VERSION=46
REQUIRED_COLUMNS={'room_drafts':{'reply_to_event_id','reply_to_seq'}}
def upgrade(db):
    db.execute('ALTER TABLE room_drafts ADD COLUMN reply_to_event_id VARCHAR(36)')
    db.execute("ALTER TABLE room_drafts ADD COLUMN reply_to_seq INTEGER CHECK ((reply_to_event_id IS NULL AND reply_to_seq IS NULL) OR (reply_to_event_id IS NOT NULL AND reply_to_seq IS NOT NULL AND reply_to_seq > 0))")
