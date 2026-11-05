"""Add durable, redacted consent metadata without rewriting old actions."""

import json
import re

VERSION = 8
REQUIRED_COLUMNS = {
    "tool_actions": {"target", "expected_effect", "session_id", "requested_at", "approval_scope"},
    "broad_modes": {"owner_id", "enabled", "session_id", "expires_at", "updated_at"},
}


def _safe_text(value):
    text = str(value or "")[:16000]
    text = re.sub(r"-----BEGIN [^-]+PRIVATE KEY-----.*?-----END [^-]+PRIVATE KEY-----",
                  "[private key redacted]", text, flags=re.S)
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text)
    text = re.sub(r"(?i)(['\"]?\b(?:api[_-]?key|access[_-]?token|token|password|secret|client[_-]?secret)\b['\"]?\s*[:=]\s*['\"]?)[^'\"\s,;}]+",
                  r"\1[redacted]", text)
    text = re.sub(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@", r"\1[redacted]@", text)
    return text


def upgrade(db):
    for column, kind in (("target", "VARCHAR"), ("expected_effect", "VARCHAR"),
                         ("session_id", "VARCHAR(36)"), ("requested_at", "DATETIME"),
                         ("approval_scope", "VARCHAR")):
        db.execute(f"ALTER TABLE tool_actions ADD COLUMN {column} {kind}")
    db.execute("CREATE TABLE broad_modes (owner_id INTEGER PRIMARY KEY REFERENCES users(id), "
               "enabled BOOLEAN NOT NULL DEFAULT 0, session_id VARCHAR(36), expires_at DATETIME, "
               "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    # Task 8 stored raw arguments before consent. Historical actions cannot
    # replay, so drop those inputs while retaining useful sanitized outcomes.
    for action_id, raw in db.execute("SELECT action_id,observation FROM tool_actions WHERE observation IS NOT NULL"):
        try:
            old = json.loads(raw)
        except (TypeError, ValueError):
            old = {}
        safe = {key: old[key] for key in ("call_id", "state") if isinstance(old, dict) and key in old}
        safe["text"] = _safe_text(old.get("text")) if isinstance(old, dict) else ""
        db.execute("UPDATE tool_actions SET observation=? WHERE action_id=?", (json.dumps(safe), action_id))
    db.execute("UPDATE tool_actions SET arguments='{}' WHERE arguments != '{}'")
    for event_id, raw in db.execute("SELECT event_id,payload FROM room_events WHERE kind='tool_result'"):
        try:
            old = json.loads(raw)
        except (TypeError, ValueError):
            old = {}
        safe = {key: old[key] for key in ("action_id", "call_id", "state", "agent_id", "trigger_event_id")
                if isinstance(old, dict) and key in old}
        safe["text"] = _safe_text(old.get("text")) if isinstance(old, dict) else ""
        db.execute("UPDATE room_events SET payload=? WHERE event_id=?", (json.dumps(safe), event_id))
