"""Canonical, owner-scoped dashboard aggregates."""

from collections.abc import Iterable

from sqlalchemy import and_, case, func
from sqlalchemy.orm import Session

from app.models.collaboration import (
    ProviderConnection,
    RoomAgentRun,
    RoomEvent,
    RoomMembership,
    RoomReadState,
    RoomTrigger,
    ToolAction,
)
from app.models.models import Agent, Conversation, Project, ProjectAgent
from app.models.shared_text_runtime import SharedTextRoom
from app.models.bot_profile import BotConversation


AUTHORED_ACTORS = ("user", "agent")


def _counts_by_room(rows: Iterable[tuple[int, int]]) -> dict[int, int]:
    return {room_id: count for room_id, count in rows}


def _connection_ready_filters():
    return (
        ProviderConnection.enabled.is_(True),
        ((ProviderConnection.auth_required.is_(False))
         | ((ProviderConnection.secret_ref.is_not(None)) & (ProviderConnection.secret_ref != ""))),
    )


def _setup_counts(db: Session, owner_id: int) -> tuple[int, int, int, int, int]:
    enabled_connections = db.query(func.count(ProviderConnection.id)).filter(
        ProviderConnection.user_id == owner_id,
        ProviderConnection.enabled.is_(True),
    ).scalar() or 0
    ready_connections = db.query(func.count(ProviderConnection.id)).filter(
        ProviderConnection.user_id == owner_id,
        *_connection_ready_filters(),
    ).scalar() or 0
    ready_agents = db.query(func.count(func.distinct(Agent.id))).join(
        ProviderConnection, ProviderConnection.id == Agent.connection_id
    ).filter(
        Agent.user_id == owner_id,
        Agent.is_active.is_(True),
        Agent.model_id.is_not(None),
        Agent.model_id != "",
        ProviderConnection.user_id == owner_id,
        *_connection_ready_filters(),
    ).scalar() or 0
    ready_projects = db.query(func.count(func.distinct(Project.id))).join(
        ProjectAgent, ProjectAgent.project_id == Project.id
    ).join(Agent, Agent.id == ProjectAgent.agent_id).join(
        ProviderConnection, ProviderConnection.id == Agent.connection_id
    ).filter(
        Project.user_id == owner_id,
        Project.is_active.is_(True),
        Agent.user_id == owner_id,
        Agent.is_active.is_(True),
        Agent.model_id.is_not(None),
        Agent.model_id != "",
        ProviderConnection.user_id == owner_id,
        *_connection_ready_filters(),
    ).scalar() or 0
    active_rooms = db.query(func.count(Conversation.id)).filter(
        Conversation.user_id == owner_id,
        Conversation.is_active.is_(True),
    ).scalar() or 0
    return enabled_connections, ready_connections, ready_agents, ready_projects, active_rooms


def _recent_rooms(db: Session, owner_id: int, limit: int):
    latest_seq = db.query(
        RoomEvent.room_id.label("room_id"),
        func.max(RoomEvent.seq).label("seq"),
    ).group_by(RoomEvent.room_id).subquery()
    return db.query(
        Conversation,
        Project.name.label("project_name"),
        RoomEvent.seq.label("last_event_seq"),
        RoomEvent.created_at.label("last_event_at"),
    ).join(Project, Project.id == Conversation.project_id).outerjoin(
        latest_seq, latest_seq.c.room_id == Conversation.id
    ).outerjoin(
        RoomEvent,
        and_(RoomEvent.room_id == latest_seq.c.room_id, RoomEvent.seq == latest_seq.c.seq),
    ).filter(
        Conversation.user_id == owner_id,
        Conversation.is_active.is_(True),
        Project.user_id == owner_id,
        ~Conversation.id.in_(db.query(SharedTextRoom.room_id)),
    ).order_by(
        func.coalesce(RoomEvent.created_at, Conversation.created_at).desc(),
        Conversation.id.desc(),
    ).limit(limit).all()


def _authored_unread(db: Session, owner_id: int) -> dict[int, int]:
    return _counts_by_room(db.query(
        RoomEvent.room_id,
        func.count(RoomEvent.event_id),
    ).join(Conversation, Conversation.id == RoomEvent.room_id).outerjoin(
        RoomReadState,
        and_(RoomReadState.room_id == Conversation.id, RoomReadState.owner_id == owner_id),
    ).filter(
        Conversation.user_id == owner_id,
        Conversation.is_active.is_(True),
        RoomEvent.kind == "message",
        RoomEvent.actor_type.in_(AUTHORED_ACTORS),
        RoomEvent.seq > func.coalesce(RoomReadState.last_read_seq, 0),
    ).group_by(RoomEvent.room_id).all())


def _waiting_approvals(db: Session, owner_id: int) -> dict[int, int]:
    return _counts_by_room(db.query(
        ToolAction.room_id,
        func.count(ToolAction.action_id),
    ).join(Conversation, Conversation.id == ToolAction.room_id).filter(
        Conversation.user_id == owner_id,
        Conversation.is_active.is_(True),
        ToolAction.state == "waiting",
    ).group_by(ToolAction.room_id).all())


def _active_failures(db: Session, owner_id: int) -> dict[int, int]:
    latest_user = db.query(
        RoomEvent.room_id.label("room_id"),
        func.max(RoomEvent.seq).label("seq"),
    ).filter(
        RoomEvent.kind == "message",
        RoomEvent.actor_type == "user",
    ).group_by(RoomEvent.room_id).subquery()
    return _counts_by_room(db.query(
        RoomEvent.room_id,
        func.count(RoomEvent.event_id),
    ).join(Conversation, Conversation.id == RoomEvent.room_id).outerjoin(
        latest_user, latest_user.c.room_id == RoomEvent.room_id
    ).filter(
        Conversation.user_id == owner_id,
        Conversation.is_active.is_(True),
        RoomEvent.kind == "failure",
        RoomEvent.seq > func.coalesce(latest_user.c.seq, 0),
    ).group_by(RoomEvent.room_id).all())


def _work_priorities(db: Session, owner_id: int) -> dict[int, int]:
    run_priority = case(
        (RoomAgentRun.state == "waiting", 3),
        (RoomAgentRun.state == "running", 2),
        ((RoomAgentRun.paused.is_(True)) | (RoomAgentRun.state == "paused"), 1),
        else_=0,
    )
    priorities = dict(db.query(
        RoomAgentRun.room_id,
        func.max(run_priority),
    ).join(
        RoomMembership,
        and_(RoomMembership.room_id == RoomAgentRun.room_id,
             RoomMembership.agent_id == RoomAgentRun.agent_id),
    ).join(Conversation, Conversation.id == RoomAgentRun.room_id).filter(
        Conversation.user_id == owner_id,
        Conversation.is_active.is_(True),
        RoomMembership.is_active.is_(True),
    ).group_by(RoomAgentRun.room_id).all())
    trigger_rooms = db.query(RoomTrigger.room_id).join(
        Conversation, Conversation.id == RoomTrigger.room_id
    ).filter(
        Conversation.user_id == owner_id,
        Conversation.is_active.is_(True),
        RoomTrigger.state.in_(("pending", "running")),
    ).distinct().all()
    for (room_id,) in trigger_rooms:
        priorities[room_id] = max(priorities.get(room_id, 0), 2)
    return priorities


def _last_authored(db: Session, room_ids: list[int]):
    if not room_ids:
        return {}
    latest_seq = db.query(
        RoomEvent.room_id.label("room_id"),
        func.max(RoomEvent.seq).label("seq"),
    ).filter(
        RoomEvent.room_id.in_(room_ids),
        RoomEvent.kind == "message",
        RoomEvent.actor_type.in_(AUTHORED_ACTORS),
    ).group_by(RoomEvent.room_id).subquery()
    rows = db.query(RoomEvent).join(
        latest_seq,
        and_(latest_seq.c.room_id == RoomEvent.room_id, latest_seq.c.seq == RoomEvent.seq),
    ).all()
    return {row.room_id: row for row in rows}


def build_dashboard(db: Session, owner_id: int, limit: int) -> dict:
    enabled_connections, ready_connections, ready_agents, ready_projects, active_rooms = _setup_counts(db, owner_id)
    recent = _recent_rooms(db, owner_id, limit)
    unread = _authored_unread(db, owner_id)
    waiting = _waiting_approvals(db, owner_id)
    failures = _active_failures(db, owner_id)
    priorities = _work_priorities(db, owner_id)
    shared_ids={row[0] for row in db.query(SharedTextRoom.room_id).filter_by(owner_id=owner_id).all()}
    unread={key:value for key,value in unread.items() if key not in shared_ids}
    waiting={key:value for key,value in waiting.items() if key not in shared_ids}
    failures={key:value for key,value in failures.items() if key not in shared_ids}
    authored = _last_authored(db, [room.id for room, _, _, _ in recent])
    status_by_priority = {0: "idle", 1: "paused", 2: "running", 3: "waiting"}

    if not ready_connections:
        next_action = "connection"
    elif not ready_agents:
        next_action = "agent"
    elif not ready_projects:
        next_action = "project"
    elif not active_rooms:
        next_action = "room"
    else:
        next_action = None

    room_ids=[room.id for room, _, _, _ in recent]
    personal = dict(db.query(BotConversation.room_id, BotConversation.agent_id).join(
        Agent, Agent.id == BotConversation.agent_id).filter(
        BotConversation.owner_id == owner_id, Agent.user_id == owner_id,
        BotConversation.room_id.in_(room_ids)).all()) if room_ids else {}
    rooms = []
    for room, project_name, last_event_seq, last_event_at in recent:
        message = authored.get(room.id)
        content = message.payload.get("content") if message and isinstance(message.payload, dict) else None
        preview = content[:160] if isinstance(content, str) else None
        priority = 3 if waiting.get(room.id, 0) else priorities.get(room.id, 0)
        rooms.append({
            "room_id": room.id,
            "room_title": room.title,
            "personal_agent_id": personal.get(room.id),
            "project_id": room.project_id,
            "project_name": project_name,
            "last_event_seq": last_event_seq or 0,
            "last_activity_at": last_event_at or room.created_at,
            "last_authored_seq": message.seq if message else None,
            "last_authored_at": message.created_at if message else None,
            "last_authored_preview": preview,
            "unread_authored": unread.get(room.id, 0),
            "waiting_approvals": waiting.get(room.id, 0),
            "active_failures": failures.get(room.id, 0),
            "work_status": status_by_priority[priority],
        })

    return {
        "setup": {"next_action": next_action},
        "counts": {
            "enabled_connections": enabled_connections,
            "ready_agents": ready_agents,
            "ready_projects": ready_projects,
            "active_rooms": active_rooms,
            "unread_authored": sum(unread.values()),
            "waiting_approvals": sum(waiting.values()),
            "active_failures": sum(failures.values()),
        },
        "rooms": rooms,
    }
