"""Owner-bound project archive planning and atomic durable application."""

from datetime import datetime, timezone
from hashlib import sha256
import json

from fastapi import HTTPException
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.models.collaboration import RoomAgentRun, RoomEvent, RoomGrant, RoomTrigger, ToolAction
from app.models.models import Conversation, Project
from app.services.room_service import stage_close_room


OPEN_ACTIONS = ("pending", "waiting", "approved", "started")
OPEN_TRIGGERS = ("pending", "running")
ACTIVE_RUNS = ("running", "waiting")


def _project(db: Session, project_id: int, owner_id: int) -> Project:
    row = db.query(Project).filter_by(id=project_id, user_id=owner_id).first()
    if row is None:
        raise HTTPException(404, "Project not found")
    return row


def _state(db: Session, project: Project) -> tuple[dict, list[dict], dict]:
    rooms = db.query(Conversation).filter_by(
        project_id=project.id, user_id=project.user_id).order_by(Conversation.id).all()
    room_ids = [row.id for row in rooms]
    active_room_ids = [row.id for row in rooms if row.is_active]
    event_highwater = dict(db.query(RoomEvent.room_id, func.max(RoomEvent.seq)).filter(
        RoomEvent.room_id.in_(room_ids)).group_by(RoomEvent.room_id).all()) if room_ids else {}
    triggers = db.query(RoomTrigger.event_id, RoomTrigger.room_id, RoomTrigger.state).filter(
        RoomTrigger.room_id.in_(active_room_ids), RoomTrigger.state.in_(OPEN_TRIGGERS)).order_by(
        RoomTrigger.room_id, RoomTrigger.event_id).all() if active_room_ids else []
    runs = db.query(RoomAgentRun.room_id, RoomAgentRun.agent_id, RoomAgentRun.state,
                    RoomAgentRun.generation).filter(
        RoomAgentRun.room_id.in_(active_room_ids)).order_by(
        RoomAgentRun.room_id, RoomAgentRun.agent_id).all() if active_room_ids else []
    actions = db.query(ToolAction.action_id, ToolAction.room_id, ToolAction.state).filter(
        ToolAction.room_id.in_(active_room_ids), ToolAction.state.in_(OPEN_ACTIONS)).order_by(
        ToolAction.room_id, ToolAction.action_id).all() if active_room_ids else []
    grants = db.query(RoomGrant.id, RoomGrant.room_id, RoomGrant.decision).filter(
        RoomGrant.room_id.in_(active_room_ids), RoomGrant.decision != "revoked").order_by(
        RoomGrant.room_id, RoomGrant.id).all() if active_room_ids else []

    counts = {
        "active_rooms": sum(bool(room.is_active) for room in rooms),
        "pending_triggers": sum(row.state == "pending" for row in triggers),
        "running_triggers": sum(row.state == "running" for row in triggers),
        "running_runs": sum(row.state == "running" for row in runs),
        "waiting_runs": sum(row.state == "waiting" for row in runs),
        "waiting_approvals": sum(row.state == "waiting" for row in actions),
        "started_actions_uncertain": sum(row.state == "started" for row in actions),
        "open_actions_cancelled": sum(row.state in ("pending", "waiting", "approved") for row in actions),
        "grants_revoked": len(grants),
    }
    work_by_room: dict[int, int] = {}
    for row in (*triggers, *actions):
        work_by_room[row.room_id] = work_by_room.get(row.room_id, 0) + 1
    for row in runs:
        if row.state in ACTIVE_RUNS:
            work_by_room[row.room_id] = work_by_room.get(row.room_id, 0) + 1
    summaries = [{
        "id": room.id, "title": room.title,
        "last_event_seq": int(event_highwater.get(room.id) or 0),
        "active_work_count": work_by_room.get(room.id, 0),
    } for room in rooms if room.is_active]
    digest_state = {
        "owner_id": project.user_id,
        "project": {"id": project.id, "is_active": bool(project.is_active)},
        "rooms": [[room.id, bool(room.is_active), int(event_highwater.get(room.id) or 0)] for room in rooms],
        "triggers": [[row.event_id, row.room_id, row.state] for row in triggers],
        "runs": [[row.room_id, row.agent_id, row.state, row.generation] for row in runs],
        "actions": [[row.action_id, row.room_id, row.state] for row in actions],
        "grants": [[row.id, row.room_id, row.decision] for row in grants],
    }
    return counts, summaries, digest_state


def preview(db: Session, project_id: int, owner_id: int) -> dict:
    project = _project(db, project_id, owner_id)
    counts, rooms, digest_state = _state(db, project)
    plan_hash = sha256(json.dumps(digest_state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "project_id": project.id, "plan_hash": plan_hash, "counts": counts,
        "rooms": rooms[:20], "rooms_truncated": len(rooms) > 20,
    }


def apply(db: Session, project_id: int, owner_id: int, plan_hash: str) -> dict:
    # Authentication has read through this Session; restart with a reserved write
    # transaction so digest recheck and every child close are one atomic boundary.
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        project = _project(db, project_id, owner_id)
        if project.archive_plan_hash == plan_hash and project.archive_result:
            result = dict(project.archive_result)
            db.commit()
            return result
        plan = preview(db, project_id, owner_id)
        if plan["plan_hash"] != plan_hash:
            raise HTTPException(409, {"code": "project_archive_plan_stale"})
        room_ids = [row[0] for row in db.query(Conversation.id).filter_by(
            project_id=project.id, user_id=owner_id, is_active=True).order_by(Conversation.id).all()]
        for room_id in room_ids:
            stage_close_room(db, room_id)
        archived_at = datetime.now(timezone.utc)
        project.is_active = False
        project.archived_at = archived_at
        project.archive_plan_hash = plan_hash
        result = {
            "project_id": project.id, "is_active": False,
            "archived_at": archived_at.isoformat(), "plan_hash": plan_hash,
            "counts": plan["counts"], "room_ids": room_ids,
        }
        project.archive_result = result
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise


def restore(db: Session, project_id: int, owner_id: int) -> dict:
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        project = _project(db, project_id, owner_id)
        if project.is_active:
            replay = dict(project.archive_result or {})
            if replay.get("restored") is True and type(replay.get("closed_rooms")) is int:
                result = {"project_id": project.id, "is_active": True,
                          "closed_rooms": replay["closed_rooms"]}
                db.commit()
                return result
            raise HTTPException(409, {"code": "project_not_archived"})
        active_rooms = db.query(func.count(Conversation.id)).filter_by(
            project_id=project.id, user_id=owner_id, is_active=True).scalar() or 0
        if active_rooms:
            raise HTTPException(409, {
                "code": "project_restore_active_children", "active_rooms": active_rooms})
        closed_rooms = db.query(func.count(Conversation.id)).filter_by(
            project_id=project.id, user_id=owner_id, is_active=False).scalar() or 0
        project.is_active = True
        project.archived_at = None
        project.archive_plan_hash = None
        project.archive_result = {"restored": True, "closed_rooms": closed_rooms}
        db.commit()
        return {"project_id": project.id, "is_active": True, "closed_rooms": closed_rooms}
    except BaseException:
        db.rollback()
        raise
