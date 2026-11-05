"""Bounded owner room navigation and durable pins."""

from base64 import b64decode, urlsafe_b64encode
from datetime import datetime
from hashlib import sha256
import json
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import and_, case, delete, func, not_, or_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models.collaboration import (
    RoomAgentRun,
    RoomEvent,
    RoomMembership,
    RoomPin,
    RoomReadState,
    RoomTrigger,
    ToolAction,
)
from app.models.models import Conversation, Project
from app.models.shared_text_runtime import SharedTextRoom


AUTHORED_ACTORS = ("user", "agent")
CURSOR_VERSION = 1


def _filter_fingerprint(owner_id: int, status: str, project_id: int | None, query: str | None) -> str:
    canonical = json.dumps(
        [owner_id, status, project_id, query], ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()[:24]


def _encode_cursor(pinned: int, activity: datetime, room_id: int, fingerprint: str) -> str:
    payload = json.dumps({
        "v": CURSOR_VERSION,
        "p": pinned,
        "t": activity.isoformat(timespec="microseconds"),
        "i": room_id,
        "f": fingerprint,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _invalid_cursor():
    raise HTTPException(422, {"code": "room_index_cursor_invalid"})


def _decode_cursor(value: str, fingerprint: str) -> tuple[int, datetime, int]:
    try:
        padding = "=" * (-len(value) % 4)
        raw = b64decode(value + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
        if (not isinstance(payload, dict)
                or set(payload) != {"v", "p", "t", "i", "f"}
                or type(payload["v"]) is not int or payload["v"] != CURSOR_VERSION
                or type(payload["p"]) is not int or payload["p"] not in (0, 1)
                or not isinstance(payload["t"], str)
                or type(payload["i"]) is not int or payload["i"] <= 0
                or payload["f"] != fingerprint):
            _invalid_cursor()
        activity = datetime.fromisoformat(payload["t"])
        if activity.tzinfo is not None or activity.isoformat(timespec="microseconds") != payload["t"]:
            _invalid_cursor()
    except HTTPException:
        raise
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        _invalid_cursor()
    return payload["p"], activity, payload["i"]


def _counts(rows) -> dict[int, int]:
    return {room_id: int(count) for room_id, count in rows}


def _aggregates(db: Session, owner_id: int, room_ids: list[int]):
    if not room_ids:
        return {}, {}, {}, {}
    owned = (
        Conversation.id.in_(room_ids),
        Conversation.user_id == owner_id,
        Project.user_id == owner_id,
    )
    effective = (*owned, Conversation.is_active.is_(True), Project.is_active.is_(True))

    unread = _counts(db.query(
        RoomEvent.room_id, func.count(RoomEvent.event_id),
    ).join(Conversation, Conversation.id == RoomEvent.room_id).join(
        Project, Project.id == Conversation.project_id,
    ).outerjoin(
        RoomReadState,
        and_(RoomReadState.room_id == Conversation.id, RoomReadState.owner_id == owner_id),
    ).filter(
        *owned,
        RoomEvent.kind == "message",
        RoomEvent.actor_type.in_(AUTHORED_ACTORS),
        RoomEvent.seq > func.coalesce(RoomReadState.last_read_seq, 0),
    ).group_by(RoomEvent.room_id).all())

    waiting = _counts(db.query(
        ToolAction.room_id, func.count(ToolAction.action_id),
    ).join(Conversation, Conversation.id == ToolAction.room_id).join(
        Project, Project.id == Conversation.project_id,
    ).filter(*effective, ToolAction.state == "waiting").group_by(ToolAction.room_id).all())

    latest_user = db.query(
        RoomEvent.room_id.label("room_id"), func.max(RoomEvent.seq).label("seq"),
    ).filter(
        RoomEvent.room_id.in_(room_ids), RoomEvent.kind == "message", RoomEvent.actor_type == "user",
    ).group_by(RoomEvent.room_id).subquery()
    failures = _counts(db.query(
        RoomEvent.room_id, func.count(RoomEvent.event_id),
    ).join(Conversation, Conversation.id == RoomEvent.room_id).join(
        Project, Project.id == Conversation.project_id,
    ).outerjoin(latest_user, latest_user.c.room_id == RoomEvent.room_id).filter(
        *effective,
        RoomEvent.kind == "failure",
        RoomEvent.seq > func.coalesce(latest_user.c.seq, 0),
    ).group_by(RoomEvent.room_id).all())

    run_priority = case(
        (RoomAgentRun.state == "waiting", 3),
        (RoomAgentRun.state == "running", 2),
        ((RoomAgentRun.paused.is_(True)) | (RoomAgentRun.state == "paused"), 1),
        else_=0,
    )
    priorities = {room_id: int(priority) for room_id, priority in db.query(
        RoomAgentRun.room_id, func.max(run_priority),
    ).join(
        RoomMembership,
        and_(RoomMembership.room_id == RoomAgentRun.room_id,
             RoomMembership.agent_id == RoomAgentRun.agent_id),
    ).join(Conversation, Conversation.id == RoomAgentRun.room_id).join(
        Project, Project.id == Conversation.project_id,
    ).filter(*effective, RoomMembership.is_active.is_(True)).group_by(RoomAgentRun.room_id).all()}
    trigger_rooms = db.query(RoomTrigger.room_id).join(
        Conversation, Conversation.id == RoomTrigger.room_id,
    ).join(Project, Project.id == Conversation.project_id).filter(
        *effective, RoomTrigger.state.in_(("pending", "running")),
    ).distinct().all()
    for (room_id,) in trigger_rooms:
        priorities[room_id] = max(priorities.get(room_id, 0), 2)
    return unread, waiting, failures, priorities


def build_room_index(
    db: Session,
    owner_id: int,
    *,
    status: Literal["active", "closed", "all"],
    project_id: int | None,
    query: str | None,
    limit: int,
    cursor: str | None,
) -> dict:
    query = query.strip() if query else None
    query = query or None
    fingerprint = _filter_fingerprint(owner_id, status, project_id, query)

    latest_seq = db.query(
        RoomEvent.room_id.label("room_id"), func.max(RoomEvent.seq).label("seq"),
    ).group_by(RoomEvent.room_id).subquery()
    activity = func.coalesce(RoomEvent.created_at, Conversation.created_at)
    pinned = case((RoomPin.room_id.is_not(None), 1), else_=0)
    effective = and_(Conversation.is_active.is_(True), Project.is_active.is_(True))

    rooms = db.query(
        Conversation.id,
        Conversation.title,
        Conversation.is_active,
        Project.id,
        Project.name,
        Project.is_active,
        RoomEvent.seq,
        activity.label("last_activity_at"),
        pinned.label("pinned"),
        RoomPin.pinned_at,
    ).join(
        Project, Project.id == Conversation.project_id,
    ).outerjoin(
        latest_seq, latest_seq.c.room_id == Conversation.id,
    ).outerjoin(
        RoomEvent,
        and_(RoomEvent.room_id == latest_seq.c.room_id, RoomEvent.seq == latest_seq.c.seq),
    ).outerjoin(
        RoomPin,
        and_(RoomPin.room_id == Conversation.id, RoomPin.owner_id == owner_id),
    ).filter(
        Conversation.user_id == owner_id,
        Project.user_id == owner_id,
        ~Conversation.id.in_(db.query(SharedTextRoom.room_id)),
    )
    if status == "active":
        rooms = rooms.filter(effective)
    elif status == "closed":
        rooms = rooms.filter(not_(effective))
    if project_id is not None:
        rooms = rooms.filter(Conversation.project_id == project_id)
    if query is not None:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rooms = rooms.filter(Conversation.title.ilike(f"%{escaped}%", escape="\\"))
    if cursor is not None:
        cursor_pinned, cursor_activity, cursor_id = _decode_cursor(cursor, fingerprint)
        rooms = rooms.filter(or_(
            pinned < cursor_pinned,
            and_(pinned == cursor_pinned, activity < cursor_activity),
            and_(pinned == cursor_pinned, activity == cursor_activity, Conversation.id < cursor_id),
        ))
    rows = rooms.order_by(pinned.desc(), activity.desc(), Conversation.id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    room_ids = [row[0] for row in rows]
    unread, waiting, failures, priorities = _aggregates(db, owner_id, room_ids)
    status_by_priority = {0: "idle", 1: "paused", 2: "running", 3: "waiting"}
    items = []
    for (room_id, title, room_active, returned_project_id, project_name, project_active,
         last_event_seq, last_activity_at, pin_flag, pinned_at) in rows:
        effective_active = bool(room_active and project_active)
        waiting_count = waiting.get(room_id, 0)
        failure_count = failures.get(room_id, 0)
        priority = 3 if waiting_count else priorities.get(room_id, 0)
        items.append({
            "room_id": room_id,
            "room_title": title,
            "project_id": returned_project_id,
            "project_name": project_name,
            "room_active": bool(room_active),
            "project_active": bool(project_active),
            "effective_active": effective_active,
            "read_only": not effective_active,
            "pinned": bool(pin_flag),
            "pinned_at": pinned_at,
            "last_event_seq": last_event_seq or 0,
            "last_activity_at": last_activity_at,
            "unread_authored": unread.get(room_id, 0),
            "waiting_approvals": waiting_count,
            "active_failures": failure_count,
            "attention_count": waiting_count + failure_count,
            "work_status": status_by_priority[priority],
        })
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = _encode_cursor(int(last[8]), last[7], last[0], fingerprint)
    return {"items": items, "next_cursor": next_cursor}


def _owned_room(db: Session, owner_id: int, room_id: int) -> Conversation:
    room = db.query(Conversation).join(Project, Project.id == Conversation.project_id).filter(
        Conversation.id == room_id,
        Conversation.user_id == owner_id,
        Project.user_id == owner_id,
    ).first()
    if room is None:
        raise HTTPException(404, "Room not found")
    from app.services.shared_text_runtime import require_read
    binding=db.query(SharedTextRoom).filter_by(room_id=room_id).first()
    if binding:require_read(db,binding,owner_id)
    return room


def pin_room(db: Session, owner_id: int, room_id: int) -> RoomPin:
    _owned_room(db, owner_id, room_id)
    db.execute(sqlite_insert(RoomPin).values(
        owner_id=owner_id, room_id=room_id,
    ).on_conflict_do_nothing(index_elements=[RoomPin.owner_id, RoomPin.room_id]))
    db.commit()
    return db.get(RoomPin, (owner_id, room_id))


def unpin_room(db: Session, owner_id: int, room_id: int) -> None:
    _owned_room(db, owner_id, room_id)
    db.execute(delete(RoomPin).where(RoomPin.owner_id == owner_id, RoomPin.room_id == room_id))
    db.commit()


def build_stored_draft_index(
    db: Session,
    owner_id: int,
    *,
    status: Literal["active", "closed", "all"],
    query: str | None,
    limit: int,
    cursor: str | None,
) -> dict:
    """Owner-only metadata/short previews. Never return draft body or send key."""
    from app.models.collaboration import RoomDraft

    query = (query or "").strip() or None
    fingerprint = sha256(json.dumps(
        ["stored-drafts-v1", owner_id, status, query],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()[:24]
    effective = and_(Conversation.is_active.is_(True), Project.is_active.is_(True))
    rows = db.query(
        RoomDraft.room_id, Conversation.title, Project.id, Project.name,
        Conversation.is_active, Project.is_active, RoomDraft.version,
        func.length(RoomDraft.content).label("chars"),
        func.substr(RoomDraft.content, 1, 400).label("prefix"),
        RoomDraft.updated_at,
    ).join(Conversation, and_(Conversation.id == RoomDraft.room_id, Conversation.user_id == RoomDraft.owner_id)).join(
        Project, Project.id == Conversation.project_id,
    ).filter(RoomDraft.owner_id == owner_id, Conversation.user_id == owner_id,
             Project.user_id == owner_id, RoomDraft.content != "",
             ~Conversation.id.in_(db.query(SharedTextRoom.room_id)))
    if status == "active":
        rows = rows.filter(effective)
    elif status == "closed":
        rows = rows.filter(not_(effective))
    if query:
        rows = rows.filter(or_(
            Conversation.title.contains(query, autoescape=True),
            Project.name.contains(query, autoescape=True),
            RoomDraft.content.contains(query, autoescape=True),
        ))
    if cursor:
        pin, updated, room = _decode_cursor(cursor, fingerprint)
        if pin != 0:
            _invalid_cursor()
        rows = rows.filter(or_(RoomDraft.updated_at < updated,
            and_(RoomDraft.updated_at == updated, RoomDraft.room_id < room)))
    fetched = rows.order_by(RoomDraft.updated_at.desc(), RoomDraft.room_id.desc()).limit(limit + 1).all()
    page = fetched[:limit]
    return {
        "items": [{
            "room_id": row[0], "room_title": row[1], "project_id": row[2], "project_name": row[3],
            "read_only": not (row[4] and row[5]), "version": row[6], "character_count": row[7],
            "preview": " ".join(row[8].split())[:100], "updated_at": row[9],
        } for row in page],
        "next_cursor": _encode_cursor(0, page[-1][9], page[-1][0], fingerprint)
            if len(fetched) > limit and page else None,
    }
