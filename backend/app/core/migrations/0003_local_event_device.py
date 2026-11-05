"""Separate local installation identity from legacy remote-device IDs."""

VERSION = 3
REQUIRED_COLUMNS = {"room_events": {"device_id"}}


def upgrade(db):
    # Legacy events were imported from Messages and have no known originating
    # installation. A former remote-device ID is not evidence of local authorship.
    if db.execute("SELECT 1 FROM room_events WHERE device_id IS NOT NULL LIMIT 1").fetchone():
        raise RuntimeError("Legacy event has a remote-device ID; manual provenance recovery required")
    db.execute("ALTER TABLE room_events RENAME TO room_events_before_local_device")
    db.execute(
        "CREATE TABLE room_events ("
        "event_id VARCHAR(36) PRIMARY KEY, device_id VARCHAR(36), "
        "room_id INTEGER NOT NULL REFERENCES conversations(id), seq INTEGER NOT NULL, "
        "actor_type VARCHAR NOT NULL CHECK(actor_type IN ('user','agent','tool','system')), "
        "actor_id INTEGER, actor_name_snapshot VARCHAR NOT NULL, "
        "kind VARCHAR NOT NULL CHECK(kind IN ('message','tool_result','failure','summary','activity')), "
        "payload TEXT NOT NULL, created_at DATETIME NOT NULL, idempotency_key VARCHAR, "
        "UNIQUE(room_id,seq), UNIQUE(room_id,idempotency_key))"
    )
    db.execute(
        "INSERT INTO room_events (event_id,device_id,room_id,seq,actor_type,actor_id,"
        "actor_name_snapshot,kind,payload,created_at,idempotency_key) "
        "SELECT event_id,NULL,room_id,seq,actor_type,actor_id,actor_name_snapshot,"
        "kind,payload,created_at,idempotency_key FROM room_events_before_local_device"
    )
    db.execute("DROP TABLE room_events_before_local_device")
    db.execute("CREATE INDEX ix_room_events_room_seq ON room_events(room_id,seq)")
