"""Add result references without rewriting events, files or existing grants."""

VERSION = 27
REQUIRED_COLUMNS = {
    "room_results": {"result_id", "owner_id", "room_id", "current_version", "created_at", "updated_at"},
    "room_result_versions": {"result_id", "version", "room_id", "kind", "title", "source_event_id", "source_seq", "selector", "author_agent_id", "author_name_snapshot", "execution", "content", "file_ref", "evidence", "state", "state_revision", "link_report", "state_owner_id", "state_changed_at", "link_report_owner_id", "link_reported_at", "created_at"},
    "room_result_reviews": {"review_id", "result_id", "version", "room_id", "source_event_id", "review_event_id", "review_seq", "reviewer_agent_id", "reviewer_name_snapshot", "linked_by_owner_id", "created_at"},
    "room_result_commands": {"owner_id", "idempotency_key", "room_id", "request_digest", "response", "created_at"},
}
REQUIRED_UNIQUE_INDEXES = {
    "ux_room_events_id_room": ("room_events", ("event_id", "room_id")),
    "ux_room_result_source": ("room_result_versions", ("room_id", "source_event_id", "kind", "selector")),
}


def upgrade(db):
    db.execute("CREATE UNIQUE INDEX ux_room_events_id_room ON room_events(event_id,room_id)")
    db.execute("""CREATE TABLE room_results (
        result_id VARCHAR(36) NOT NULL PRIMARY KEY,
        owner_id INTEGER NOT NULL REFERENCES users(id), room_id INTEGER NOT NULL,
        current_version INTEGER NOT NULL CHECK(current_version >= 1),
        created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
        UNIQUE(result_id,room_id),
        FOREIGN KEY(room_id,owner_id) REFERENCES conversations(id,user_id)
    )""")
    db.execute("""CREATE TABLE room_result_versions (
        result_id VARCHAR(36) NOT NULL, version INTEGER NOT NULL CHECK(version >= 1),
        room_id INTEGER NOT NULL, kind VARCHAR(8) NOT NULL CHECK(kind IN ('text','link','file')),
        title VARCHAR(200) NOT NULL, source_event_id VARCHAR(36) NOT NULL,
        source_seq INTEGER NOT NULL CHECK(source_seq >= 1), selector VARCHAR(2048) NOT NULL,
        author_agent_id INTEGER NOT NULL REFERENCES agents(id), author_name_snapshot VARCHAR(200) NOT NULL,
        execution JSON NOT NULL, content TEXT NOT NULL, file_ref JSON, evidence JSON NOT NULL,
        state VARCHAR(12) NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','reviewing','final')),
        state_revision INTEGER NOT NULL DEFAULT 1 CHECK(state_revision >= 1),
        link_report VARCHAR(12) NOT NULL DEFAULT 'unchecked' CHECK(link_report IN ('unchecked','reported_bad')),
        state_owner_id INTEGER REFERENCES users(id), state_changed_at DATETIME,
        link_report_owner_id INTEGER REFERENCES users(id), link_reported_at DATETIME, created_at DATETIME NOT NULL,
        PRIMARY KEY(result_id,version), UNIQUE(result_id,version,room_id),
        UNIQUE(result_id,version,room_id,source_event_id),
        FOREIGN KEY(result_id,room_id) REFERENCES room_results(result_id,room_id),
        FOREIGN KEY(source_event_id,room_id) REFERENCES room_events(event_id,room_id)
    )""")
    db.execute("CREATE UNIQUE INDEX ux_room_result_source ON room_result_versions(room_id,source_event_id,kind,selector)")
    db.execute("CREATE INDEX ix_room_results_list ON room_results(owner_id,room_id,result_id)")
    db.execute("""CREATE TABLE room_result_reviews (
        review_id VARCHAR(36) NOT NULL PRIMARY KEY, result_id VARCHAR(36) NOT NULL,
        version INTEGER NOT NULL CHECK(version >= 1), room_id INTEGER NOT NULL, source_event_id VARCHAR(36) NOT NULL,
        review_event_id VARCHAR(36) NOT NULL, review_seq INTEGER NOT NULL CHECK(review_seq >= 1),
        reviewer_agent_id INTEGER NOT NULL REFERENCES agents(id), reviewer_name_snapshot VARCHAR(200) NOT NULL,
        linked_by_owner_id INTEGER NOT NULL REFERENCES users(id), created_at DATETIME NOT NULL,
        UNIQUE(result_id,version,review_event_id),
        FOREIGN KEY(result_id,version,room_id,source_event_id) REFERENCES room_result_versions(result_id,version,room_id,source_event_id),
        FOREIGN KEY(source_event_id,room_id) REFERENCES room_events(event_id,room_id),
        FOREIGN KEY(review_event_id,room_id) REFERENCES room_events(event_id,room_id)
    )""")
    db.execute("""CREATE TABLE room_result_commands (
        owner_id INTEGER NOT NULL REFERENCES users(id), idempotency_key VARCHAR(200) NOT NULL,
        room_id INTEGER NOT NULL, request_digest VARCHAR(64) NOT NULL CHECK(length(request_digest)=64),
        response JSON NOT NULL, created_at DATETIME NOT NULL,
        PRIMARY KEY(owner_id,idempotency_key), CHECK(length(idempotency_key) BETWEEN 1 AND 200),
        FOREIGN KEY(room_id,owner_id) REFERENCES conversations(id,user_id)
    )""")
