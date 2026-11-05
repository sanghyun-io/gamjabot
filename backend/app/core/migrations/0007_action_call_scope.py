"""Native call IDs belong to a provider response, not a room trigger."""

VERSION = 7
REQUIRED_COLUMNS = {"tool_actions": {"action_id", "trigger_event_id", "call_id"}}


def upgrade(db):
    # Keep the UUID primary key as durable action identity. A response-local
    # call ID may be reused by another Agent or by a later provider response.
    db.execute("DROP INDEX ix_tool_actions_trigger_call")
    db.execute("CREATE INDEX ix_tool_actions_trigger_call_lookup ON tool_actions(room_id,trigger_event_id,call_id)")
