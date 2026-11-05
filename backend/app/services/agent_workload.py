"""Bounded owner Agent workload derived from canonical room execution state."""

from base64 import b64decode, urlsafe_b64encode
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from urllib.parse import urlencode

from fastapi import HTTPException
from sqlalchemy import and_, func
from sqlalchemy.orm import Session, aliased

from app.models.collaboration import RoomAgentRun, RoomEvent, RoomMembership, RoomTrigger
from app.models.models import Agent, Conversation, Project
from app.models.shared_text_runtime import SharedTextRoom


CURSOR_VERSION = 1
ROOM_LIMIT = 12
STATES = ("running", "waiting", "paused", "failed", "idle", "unknown")
DISPLAY_PRIORITY = {state: index for index, state in enumerate(
    ("running", "waiting", "paused", "failed", "unknown", "idle")
)}


def _fingerprint(owner_id: int, agent_id: int | None) -> str:
    return sha256(f"agent-workload:{owner_id}:{agent_id or ''}".encode("ascii")).hexdigest()[:24]


def _invalid_cursor():
    raise HTTPException(422, {"code": "agent_workload_cursor_invalid"})


def _encode_cursor(agent_id: int, fingerprint: str) -> str:
    payload = json.dumps({"v": CURSOR_VERSION, "i": agent_id, "f": fingerprint},
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    return urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str, fingerprint: str) -> int:
    try:
        raw = b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
        if (not isinstance(payload, dict) or set(payload) != {"v", "i", "f"}
                or type(payload["v"]) is not int or payload["v"] != CURSOR_VERSION
                or type(payload["i"]) is not int or payload["i"] <= 0
                or payload["f"] != fingerprint):
            _invalid_cursor()
    except HTTPException:
        raise
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        _invalid_cursor()
    return payload["i"]


def _canonical_time(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo is not None else value


def _selected(value, agent_id: int) -> bool:
    return isinstance(value, list) and any(type(item) is int and item == agent_id for item in value)


def _href(room_id: int, *, trigger_seq: int | None = None,
          failure_event_id: str | None = None, failure_seq: int | None = None) -> str:
    params = {}
    if failure_event_id is not None and failure_seq is not None:
        params = {"seq": failure_seq, "failure": failure_event_id}
    elif trigger_seq is not None:
        params = {"seq": trigger_seq}
    return f"/chat/{room_id}" + ("?" + urlencode(params) if params else "")


def build_agent_workloads(
    db: Session, owner_id: int, *, agent_id: int | None, limit: int, cursor: str | None,
) -> dict:
    fingerprint = _fingerprint(owner_id, agent_id)
    after_id = _decode_cursor(cursor, fingerprint) if cursor else None
    agents_query = db.query(Agent.id, Agent.name, Agent.is_active).filter(Agent.user_id == owner_id,
        ~Agent.id.in_(db.query(SharedTextRoom.agent_id)))
    if agent_id is not None:
        agents_query = agents_query.filter(Agent.id == agent_id)
    if after_id is not None:
        agents_query = agents_query.filter(Agent.id > after_id)
    agent_rows = agents_query.order_by(Agent.id.asc()).limit(limit + 1).all()
    has_more = len(agent_rows) > limit
    agent_rows = agent_rows[:limit]
    if not agent_rows:
        return {"items": [], "next_cursor": None}

    agent_ids = [row.id for row in agent_rows]
    last_activity = db.query(func.max(RoomEvent.created_at)).filter(
        RoomEvent.room_id == Conversation.id,
    ).correlate(Conversation).scalar_subquery()
    membership_rows = db.query(
        RoomMembership.agent_id,
        Conversation.id.label("room_id"), Conversation.title.label("room_title"),
        Conversation.created_at.label("room_created_at"),
        Project.id.label("project_id"), Project.name.label("project_name"),
        RoomAgentRun.state.label("run_state"), RoomAgentRun.paused,
        RoomAgentRun.trigger_event_id.label("run_trigger_event_id"),
        RoomAgentRun.updated_at.label("run_updated_at"),
        last_activity.label("event_activity_at"),
    ).join(
        Conversation, Conversation.id == RoomMembership.room_id,
    ).join(
        Project, Project.id == Conversation.project_id,
    ).outerjoin(
        RoomAgentRun,
        and_(RoomAgentRun.room_id == RoomMembership.room_id,
             RoomAgentRun.agent_id == RoomMembership.agent_id),
    ).filter(
        RoomMembership.agent_id.in_(agent_ids),
        RoomMembership.is_active.is_(True),
        Conversation.user_id == owner_id,
        Project.user_id == owner_id,
        Conversation.is_active.is_(True),
        Project.is_active.is_(True),
    ).all()

    room_ids = sorted({row.room_id for row in membership_rows})
    member_pairs = {(row.room_id, row.agent_id) for row in membership_rows}
    open_by_pair = defaultdict(list)
    if room_ids:
        request = aliased(RoomEvent)
        trigger_rows = db.query(
            RoomTrigger.event_id, RoomTrigger.room_id, RoomTrigger.state,
            RoomTrigger.selected_agents, request.seq.label("request_seq"),
        ).join(
            Conversation, Conversation.id == RoomTrigger.room_id,
        ).join(
            Project, Project.id == Conversation.project_id,
        ).join(
            request,
            and_(request.event_id == RoomTrigger.event_id,
                 request.room_id == RoomTrigger.room_id,
                 request.actor_type == "user", request.kind == "message"),
        ).filter(
            RoomTrigger.room_id.in_(room_ids),
            RoomTrigger.state.in_(("pending", "running")),
            Conversation.user_id == owner_id,
            Project.user_id == owner_id,
            Conversation.is_active.is_(True),
            Project.is_active.is_(True),
        ).all()
        for trigger_row in trigger_rows:
            selected = trigger_row.selected_agents
            if not isinstance(selected, list):
                continue
            for selected_agent_id in selected:
                if (type(selected_agent_id) is int
                        and (trigger_row.room_id, selected_agent_id) in member_pairs):
                    open_by_pair[(trigger_row.room_id, selected_agent_id)].append(trigger_row)
        for rows in open_by_pair.values():
            rows.sort(key=lambda row: (row.request_seq, row.event_id))

    outcomes = {}
    if room_ids:
        latest_user = db.query(
            RoomEvent.room_id.label("room_id"), func.max(RoomEvent.seq).label("seq"),
        ).filter(
            RoomEvent.room_id.in_(room_ids),
            RoomEvent.actor_type == "user", RoomEvent.kind == "message",
        ).group_by(RoomEvent.room_id).subquery()
        candidate = aliased(RoomEvent)
        latest_outcome = db.query(
            candidate.room_id.label("room_id"), candidate.actor_id.label("agent_id"),
            func.max(candidate.seq).label("seq"),
        ).outerjoin(
            latest_user, latest_user.c.room_id == candidate.room_id,
        ).filter(
            candidate.room_id.in_(room_ids),
            candidate.actor_id.in_(agent_ids),
            candidate.actor_type == "agent", candidate.kind.in_(("message", "failure")),
            candidate.seq > func.coalesce(latest_user.c.seq, 0),
        ).group_by(candidate.room_id, candidate.actor_id).subquery()
        outcome = aliased(RoomEvent)
        outcome_request = aliased(RoomEvent)
        outcome_rows = db.query(
            outcome.room_id, outcome.actor_id, outcome.kind,
            outcome.event_id, outcome.seq, outcome.created_at,
            outcome.payload["trigger_event_id"].as_string().label("trigger_event_id"),
            outcome_request.seq.label("trigger_seq"),
        ).join(
            latest_outcome,
            and_(latest_outcome.c.room_id == outcome.room_id,
                 latest_outcome.c.agent_id == outcome.actor_id,
                 latest_outcome.c.seq == outcome.seq),
        ).join(
            Conversation, Conversation.id == outcome.room_id,
        ).join(
            Project, Project.id == Conversation.project_id,
        ).outerjoin(
            outcome_request,
            and_(outcome_request.event_id == outcome.payload["trigger_event_id"].as_string(),
                 outcome_request.room_id == outcome.room_id,
                 outcome_request.actor_type == "user", outcome_request.kind == "message"),
        ).filter(
            Conversation.user_id == owner_id,
            Project.user_id == owner_id,
            Conversation.is_active.is_(True),
            Project.is_active.is_(True),
        ).all()
        outcomes = {(row.room_id, row.actor_id): row for row in outcome_rows}

    agents = {row.id: {"id": row.id, "name": row.name, "active": bool(row.is_active), "rooms": []}
              for row in agent_rows}
    for row in membership_rows:
        agent = agents[row.agent_id]
        pair = (row.room_id, row.agent_id)
        open_rows = open_by_pair.get(pair, [])
        exact_open = next((item for item in open_rows if item.event_id == row.run_trigger_event_id), None)
        outcome = outcomes.get(pair)
        state = "unknown"
        chosen_trigger = None
        failure_event_id = None
        failure_seq = None
        if not agent["active"] or row.run_state is None:
            state = "unknown"
        elif bool(row.paused) or row.run_state == "paused":
            state = "paused"
        elif row.run_state == "running":
            if exact_open is not None:
                state, chosen_trigger = "running", exact_open
        elif row.run_state == "waiting":
            if exact_open is not None:
                state, chosen_trigger = "waiting", exact_open
        elif row.run_state == "idle":
            if open_rows:
                state, chosen_trigger = "waiting", open_rows[0]
            elif outcome is not None and outcome.kind == "failure":
                state = "failed"
                failure_event_id, failure_seq = outcome.event_id, outcome.seq
            else:
                state = "idle"

        trigger_event_id = chosen_trigger.event_id if chosen_trigger is not None else None
        trigger_seq = chosen_trigger.request_seq if chosen_trigger is not None else None
        if state == "failed" and outcome is not None and outcome.trigger_seq is not None:
            trigger_event_id, trigger_seq = outcome.trigger_event_id, outcome.trigger_seq
        activity = row.event_activity_at or row.run_updated_at or row.room_created_at
        agent["rooms"].append({
            "room_id": row.room_id, "room_title": row.room_title,
            "project_id": row.project_id, "project_name": row.project_name,
            "state": state,
            "trigger_event_id": trigger_event_id, "trigger_seq": trigger_seq,
            "failure_event_id": failure_event_id, "failure_seq": failure_seq,
            "last_activity_at": _canonical_time(activity),
            "href": _href(row.room_id, trigger_seq=trigger_seq,
                          failure_event_id=failure_event_id, failure_seq=failure_seq),
        })

    items = []
    for row in agent_rows:
        agent = agents[row.id]
        room_rows = agent["rooms"]
        counts = {state: sum(room["state"] == state for room in room_rows) for state in STATES}
        counts["total_rooms"] = len(room_rows)
        if not agent["active"]:
            overall = "unknown"
        elif not room_rows:
            overall = "idle"
        else:
            overall = min((room["state"] for room in room_rows), key=DISPLAY_PRIORITY.__getitem__)
        room_rows.sort(key=lambda room: (
            DISPLAY_PRIORITY[room["state"]],
            -room["last_activity_at"].timestamp(),
            -room["room_id"],
        ))
        items.append({
            "agent_id": agent["id"], "agent_name": agent["name"],
            "agent_active": agent["active"], "overall_state": overall,
            "counts": counts, "rooms": room_rows[:ROOM_LIMIT],
            "rooms_truncated": len(room_rows) > ROOM_LIMIT,
        })

    return {
        "items": items,
        "next_cursor": _encode_cursor(agent_rows[-1].id, fingerprint) if has_more else None,
    }
