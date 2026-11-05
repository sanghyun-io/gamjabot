"""Record bounded provider-call source provenance without backfilling legacy calls."""

VERSION = 69
REQUIRED_COLUMNS = {
    "teammate_delivery_receipts": {"receipt_id", "owner_id", "root_room_id",
        "root_request_event_id", "execution_room_id", "agent_id", "generation",
        "job_id", "job_kind", "phase", "invocation_ordinal", "state",
        "sources_json", "created_at", "acknowledged_at"},
}
REQUIRED_UNIQUE_INDEXES = {
    "uq_teammate_delivery_invocation": ("teammate_delivery_receipts", (
        "root_request_event_id", "job_kind", "job_id", "phase", "generation", "invocation_ordinal")),
}


def upgrade(db):
    db.execute("""CREATE TABLE teammate_delivery_receipts (
        receipt_id VARCHAR(36) PRIMARY KEY NOT NULL,
        owner_id INTEGER NOT NULL REFERENCES users(id),
        root_room_id INTEGER NOT NULL REFERENCES conversations(id),
        root_request_event_id VARCHAR(36) NOT NULL REFERENCES room_events(event_id),
        execution_room_id INTEGER NOT NULL REFERENCES conversations(id),
        agent_id INTEGER NOT NULL REFERENCES agents(id),
        generation INTEGER NOT NULL CHECK(generation >= 0),
        job_id VARCHAR(36) NOT NULL,
        job_kind VARCHAR(16) NOT NULL CHECK(job_kind IN ('teammate','bot_message','bot_batch','retry')),
        phase VARCHAR(16) NOT NULL CHECK(phase IN ('response','return')),
        invocation_ordinal INTEGER NOT NULL CHECK(invocation_ordinal >= 1),
        state VARCHAR(16) NOT NULL, sources_json JSON NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, acknowledged_at DATETIME,
        CHECK((state = 'attempted' AND acknowledged_at IS NULL) OR
              (state = 'acknowledged' AND acknowledged_at IS NOT NULL))
    )""")
    db.execute("""CREATE UNIQUE INDEX uq_teammate_delivery_invocation ON teammate_delivery_receipts
        (root_request_event_id, job_kind, job_id, phase, generation, invocation_ordinal)""")
    db.execute("CREATE INDEX ix_teammate_delivery_root ON teammate_delivery_receipts(owner_id, root_room_id, root_request_event_id)")
    db.execute("CREATE INDEX ix_teammate_delivery_job ON teammate_delivery_receipts(owner_id, job_kind, job_id, phase)")
