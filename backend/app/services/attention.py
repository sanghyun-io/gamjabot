"""Derived, bounded owner attention inbox without a second persisted queue."""

from base64 import b64decode, urlsafe_b64encode
from datetime import datetime, timezone
from hashlib import sha256
import json
from urllib.parse import urlencode

from fastapi import HTTPException
from sqlalchemy import and_, func, literal, or_
from sqlalchemy.orm import Session, aliased

from app.models.collaboration import RoomEvent, ToolAction
from app.models.models import Agent, Conversation, Project
from app.models.shared_text_runtime import SharedTextRoom


CURSOR_VERSION = 1
_PROCESS = frozenset(("start_process", "cancel_process"))
_FILES = frozenset(("read_file", "write_file", "move_file", "delete_file"))
_BROWSER = frozenset(("browser_navigate", "browser_inspect", "browser_interact"))
_INPUT = frozenset(("pointer_click", "pointer_move", "pointer_scroll", "keyboard_text", "keyboard_chord"))


def _owner_fingerprint(owner_id: int) -> str:
    return sha256(f"attention:{owner_id}".encode("ascii")).hexdigest()[:24]


def _canonical_time(value: datetime) -> datetime:
    return (value.astimezone(timezone.utc).replace(tzinfo=None)
            if value.tzinfo is not None else value)


def _valid_item_key(value) -> bool:
    return (isinstance(value, str) and value.startswith(("approval:", "failure:"))
            and 1 <= len(value.partition(":")[2]) <= 200
            and all(char.isprintable() for char in value))


def _invalid_cursor():
    raise HTTPException(422, {"code": "attention_cursor_invalid"})


def _encode_cursor(occurred_at: datetime, item_id: str, owner_id: int) -> str:
    occurred_at = _canonical_time(occurred_at)
    payload = json.dumps({
        "v": CURSOR_VERSION,
        "t": occurred_at.isoformat(timespec="microseconds"),
        "i": item_id,
        "o": _owner_fingerprint(owner_id),
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str, owner_id: int) -> tuple[datetime, str]:
    try:
        raw = b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
        if (not isinstance(payload, dict) or set(payload) != {"v", "t", "i", "o"}
                or type(payload["v"]) is not int or payload["v"] != CURSOR_VERSION
                or not isinstance(payload["t"], str) or not _valid_item_key(payload["i"])
                or payload["o"] != _owner_fingerprint(owner_id)):
            _invalid_cursor()
        occurred_at = datetime.fromisoformat(payload["t"])
        if occurred_at.tzinfo is not None or occurred_at.isoformat(timespec="microseconds") != payload["t"]:
            _invalid_cursor()
    except HTTPException:
        raise
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        _invalid_cursor()
    return occurred_at, payload["i"]


def _after_cursor(query, timestamp, item_key, cursor: tuple[datetime, str] | None):
    if cursor is None:
        return query
    cursor_time, cursor_key = cursor
    return query.filter(or_(timestamp < cursor_time, and_(timestamp == cursor_time, item_key < cursor_key)))


def _approval_summary(family: str) -> str:
    if family in _PROCESS:
        return "approval_process"
    if family in _FILES:
        return "approval_files"
    if family in _BROWSER:
        return "approval_browser"
    if family == "screenshot":
        return "approval_screen"
    if family in _INPUT:
        return "approval_input"
    return "approval_other"


def _failure_summary(payload: dict) -> str:
    code = payload.get("code") if isinstance(payload, dict) else None
    if not isinstance(code, str):
        return "failure_other"
    if code.startswith("context_"):
        return "failure_context"
    if code in {"tool_effect_uncertain", "desktop_release_failed"}:
        return "failure_uncertain"
    if code.endswith("_denied") or "permission" in code:
        return "failure_permission"
    if code.startswith(("provider_", "missing_", "connection_")) or code in {"invalid_configuration", "rate_limited", "quota_exhausted"}:
        return "failure_connection"
    if code.startswith(("tool_", "executor_", "invalid_tool", "invalid_action", "invalid_continuation")):
        return "failure_tool"
    return "failure_other"


def _href(room_id: int, *, seq: int | None, action_id: str | None = None,
          failure_event_id: str | None = None) -> str:
    parameters = {}
    if seq is not None:
        parameters["seq"] = seq
    if action_id is not None:
        parameters["action"] = action_id
    if failure_event_id is not None:
        parameters["failure"] = failure_event_id
    return f"/chat/{room_id}" + ("?" + urlencode(parameters) if parameters else "")


def build_attention(db: Session, owner_id: int, *, limit: int, cursor: str | None) -> dict:
    decoded = _decode_cursor(cursor, owner_id) if cursor else None
    request_event = aliased(RoomEvent)
    approval_time = func.coalesce(ToolAction.requested_at, Conversation.updated_at, Conversation.created_at)
    approval_key = literal("approval:").op("||")(ToolAction.action_id)
    approvals = db.query(
        ToolAction.action_id, ToolAction.family, ToolAction.agent_id,
        approval_time.label("occurred_at"),
        Conversation.id, Conversation.title, Project.id, Project.name,
        Agent.name, request_event.event_id, request_event.seq,
    ).join(
        Conversation, Conversation.id == ToolAction.room_id,
    ).join(
        Project, Project.id == Conversation.project_id,
    ).join(
        Agent, and_(Agent.id == ToolAction.agent_id, Agent.user_id == owner_id),
    ).outerjoin(
        request_event,
        and_(request_event.event_id == ToolAction.trigger_event_id,
             request_event.room_id == Conversation.id,
             request_event.actor_type == "user", request_event.kind == "message"),
    ).filter(
        Conversation.user_id == owner_id,
        ~Conversation.id.in_(db.query(SharedTextRoom.room_id)),
        Project.user_id == owner_id,
        Conversation.is_active.is_(True),
        Project.is_active.is_(True),
        ToolAction.state == "waiting",
    )
    approvals = _after_cursor(approvals, approval_time, approval_key, decoded).order_by(
        approval_time.desc(), approval_key.desc(),
    ).limit(limit + 1).all()

    latest_user = db.query(
        RoomEvent.room_id.label("room_id"), func.max(RoomEvent.seq).label("seq"),
    ).filter(
        RoomEvent.kind == "message", RoomEvent.actor_type == "user",
    ).group_by(RoomEvent.room_id).subquery()
    failure_event = aliased(RoomEvent)
    failure_request = aliased(RoomEvent)
    failure_key = literal("failure:").op("||")(failure_event.event_id)
    failures = db.query(
        failure_event.event_id, failure_event.seq, failure_event.payload,
        failure_event.actor_id, failure_event.actor_name_snapshot,
        failure_event.created_at.label("occurred_at"),
        Conversation.id, Conversation.title, Project.id, Project.name,
        failure_request.event_id, failure_request.seq,
    ).select_from(failure_event).join(
        Conversation, Conversation.id == failure_event.room_id,
    ).join(
        Project, Project.id == Conversation.project_id,
    ).join(
        Agent, and_(Agent.id == failure_event.actor_id, Agent.user_id == owner_id),
    ).outerjoin(
        latest_user, latest_user.c.room_id == failure_event.room_id,
    ).outerjoin(
        failure_request,
        and_(failure_request.event_id == failure_event.payload["trigger_event_id"].as_string(),
             failure_request.room_id == failure_event.room_id,
             failure_request.actor_type == "user", failure_request.kind == "message"),
    ).filter(
        Conversation.user_id == owner_id,
        ~Conversation.id.in_(db.query(SharedTextRoom.room_id)),
        Project.user_id == owner_id,
        Conversation.is_active.is_(True),
        Project.is_active.is_(True),
        failure_event.kind == "failure",
        failure_event.actor_type == "agent",
        failure_event.seq > func.coalesce(latest_user.c.seq, 0),
    )
    failures = _after_cursor(failures, failure_event.created_at, failure_key, decoded).order_by(
        failure_event.created_at.desc(), failure_key.desc(),
    ).limit(limit + 1).all()

    items = []
    for (action_id, family, agent_id, occurred_at, room_id, room_title, project_id,
         project_name, agent_name, request_id, request_seq) in approvals:
        item_id = f"approval:{action_id}"
        items.append({
            "id": item_id, "kind": "approval", "occurred_at": occurred_at,
            "room_id": room_id, "room_title": room_title,
            "project_id": project_id, "project_name": project_name,
            "agent_id": agent_id, "agent_name": agent_name,
            "request_event_id": request_id, "request_seq": request_seq,
            "action_id": action_id, "failure_event_id": None, "failure_seq": None,
            "summary_code": _approval_summary(family),
            "href": _href(room_id, seq=request_seq, action_id=action_id),
        })
    for (failure_id, failure_seq, payload, agent_id, agent_name, occurred_at,
         room_id, room_title, project_id, project_name, request_id, request_seq) in failures:
        item_id = f"failure:{failure_id}"
        items.append({
            "id": item_id, "kind": "failure", "occurred_at": occurred_at,
            "room_id": room_id, "room_title": room_title,
            "project_id": project_id, "project_name": project_name,
            "agent_id": agent_id, "agent_name": agent_name,
            "request_event_id": request_id, "request_seq": request_seq,
            "action_id": None, "failure_event_id": failure_id, "failure_seq": failure_seq,
            "summary_code": _failure_summary(payload),
            "href": _href(room_id, seq=failure_seq, failure_event_id=failure_id),
        })
    for item in items:
        item["occurred_at"] = _canonical_time(item["occurred_at"])
    items.sort(key=lambda item: (item["occurred_at"], item["id"]), reverse=True)
    has_more = len(items) > limit
    items = items[:limit]
    next_cursor = _encode_cursor(items[-1]["occurred_at"], items[-1]["id"], owner_id) if has_more else None
    return {"items": items, "next_cursor": next_cursor}
