"""Ownership, membership, and durable room-local turn gates."""

from hashlib import sha256
import json

from fastapi import HTTPException
from sqlalchemy import and_, exists, select, text, update
from sqlalchemy.orm import Session

from app.models.collaboration import (RoomAgentRun, RoomEvent, RoomGrant, RoomMembership,
                                      RoomLaunchRequest, RoomScheduleSettings, RoomTrigger, ToolAction)
from app.models.models import Agent, Conversation, Message, Project
from app.schemas.room_launch import RoomLaunchWrite
from app.services import resume_boundary
from app.services.llm_service import LLMFailure, llm_service
from app.services.providers import ProviderResult
from app.services.events import ActorRef, append_event
from app.services.tool_broker import record_action_result, stage_action_result


def require_room(db: Session, room_id: int, owner_id: int, *, active: bool = True) -> Conversation:
    query = db.query(Conversation).filter_by(id=room_id, user_id=owner_id)
    if active:
        query = query.join(Project, Project.id == Conversation.project_id).filter(
            Conversation.is_active.is_(True), Project.is_active.is_(True))
    room = query.first()
    if room is None:
        raise HTTPException(404, "Room not found")
    from app.models.shared_text_runtime import SharedTextRoom
    binding=db.query(SharedTextRoom).filter_by(room_id=room_id).first()
    if binding:
        from app.services.shared_text_runtime import require_read
        require_read(db,binding,owner_id)
    return room


def list_members(db: Session, room_id: int, owner_id: int) -> list[dict]:
    require_room(db, room_id, owner_id, active=False)
    members = db.query(RoomMembership).filter_by(room_id=room_id, is_active=True).order_by(RoomMembership.agent_id).all()
    result = []
    for member in members:
        run = db.get(RoomAgentRun, (room_id, member.agent_id))
        result.append({"room_id": member.room_id, "agent_id": member.agent_id, "is_active": member.is_active,
                       "joined_at": member.joined_at, "paused": bool(run and run.paused),
                       "run_state": run.state if run else "idle",
                       "resume_status": resume_boundary.resume_status(db, run) if run else None})
    from app.services.work_state import project_work_states
    work = project_work_states(db, room_id, owner_id, result)
    for member in result:
        member["work_state"] = work[member["agent_id"]]
    return result


def rename_room(db: Session, room_id: int, owner_id: int, title: str) -> Conversation:
    room = db.query(Conversation).join(Project, Project.id == Conversation.project_id).filter(
        Conversation.id == room_id, Conversation.user_id == owner_id,
        Project.is_active.is_(True)).first()
    if room is None:
        raise HTTPException(404, "Room not found")
    room.title = title
    db.commit()
    db.refresh(room)
    return room


def clone_room(db: Session, source_room_id: int, owner_id: int,
               destination_project_id: int, title: str) -> Conversation:
    source = require_room(db, source_room_id, owner_id, active=False)
    project = db.query(Project).filter_by(
        id=destination_project_id, user_id=owner_id, is_active=True).first()
    if project is None:
        raise HTTPException(404, "Project not found")

    member_ids = [row[0] for row in db.query(RoomMembership.agent_id).filter_by(
        room_id=source.id, is_active=True).order_by(RoomMembership.agent_id).all()]
    valid_ids = {row[0] for row in db.query(Agent.id).filter(
        Agent.id.in_(member_ids), Agent.user_id == owner_id, Agent.is_active.is_(True)).all()} if member_ids else set()
    unavailable = sorted(set(member_ids) - valid_ids)
    if unavailable:
        raise HTTPException(409, {
            "code": "room_clone_members_unavailable",
            "agent_ids": unavailable,
        })

    source_schedule = db.get(RoomScheduleSettings, source.id)
    limits = {
        "max_rounds": source_schedule.max_rounds if source_schedule else 2,
        "max_tool_calls": source_schedule.max_tool_calls if source_schedule else 8,
        "max_executor_seconds": source_schedule.max_executor_seconds if source_schedule else 60,
    }
    return create_room_from_setup(db, owner_id, project.id, title, member_ids, limits)


def stage_room_from_setup(db: Session, owner_id: int, project_id: int, title: str,
                          member_ids: list[int], limits: dict[str, int]) -> Conversation:
    """Stage fresh room-local rows after the caller completes owner preflight."""
    target = Conversation(user_id=owner_id, project_id=project_id, title=title, is_active=True)
    db.add(target)
    db.flush()
    db.add_all([RoomMembership(room_id=target.id, agent_id=agent_id) for agent_id in member_ids])
    db.flush()
    db.add_all([RoomAgentRun(room_id=target.id, agent_id=agent_id) for agent_id in member_ids])
    db.add(RoomScheduleSettings(room_id=target.id, **limits))
    return target


def create_room_from_setup(db: Session, owner_id: int, project_id: int, title: str,
                           member_ids: list[int], limits: dict[str, int]) -> Conversation:
    """Create only fresh room-local rows after the caller completes owner preflight."""
    try:
        target = stage_room_from_setup(db, owner_id, project_id, title, member_ids, limits)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    db.refresh(target)
    return target


def launch_room(db: Session, owner_id: int, value: RoomLaunchWrite,
                idempotency_key: str) -> Conversation:
    """Atomically preflight and create one idempotent custom room."""
    if (not idempotency_key or len(idempotency_key) > 200
            or any(not char.isprintable() for char in idempotency_key)):
        raise HTTPException(422, {"code": "room_launch_idempotency_key_invalid"})
    digest = sha256(json.dumps(
        value.model_dump(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    # Serialize key lookup and creation. Auth has already read through this
    # Session, so release that transaction before reserving the SQLite writer.
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        prior = db.get(RoomLaunchRequest, (owner_id, idempotency_key))
        if prior is not None:
            if prior.payload_digest != digest:
                raise HTTPException(409, {"code": "room_launch_idempotency_conflict"})
            room = db.query(Conversation).filter_by(id=prior.room_id, user_id=owner_id).one()
            db.commit()
            return room

        project = db.query(Project).filter_by(
            id=value.project_id, user_id=owner_id, is_active=True).first()
        if project is None:
            raise HTTPException(404, "Project not found")
        valid_ids = {row[0] for row in db.query(Agent.id).filter(
            Agent.id.in_(value.agent_ids), Agent.user_id == owner_id,
            Agent.is_active.is_(True)).all()} if value.agent_ids else set()
        unavailable = sorted(set(value.agent_ids) - valid_ids)
        if unavailable:
            raise HTTPException(409, {
                "code": "room_launch_agents_unavailable", "agent_ids": unavailable,
            })

        room = stage_room_from_setup(db, owner_id, project.id, value.title, value.agent_ids, {
            "max_rounds": value.max_rounds,
            "max_tool_calls": value.max_tool_calls,
            "max_executor_seconds": value.max_executor_seconds,
        })
        db.add(RoomLaunchRequest(
            owner_id=owner_id, idempotency_key=idempotency_key,
            payload_digest=digest, room_id=room.id,
        ))
        db.commit()
        db.refresh(room)
        return room
    except BaseException:
        db.rollback()
        raise


def stage_close_room(db: Session, room_id: int) -> None:
    """Stage durable room close state without committing the caller's transaction."""
    room = db.query(Conversation).filter_by(id=room_id, is_active=True).first()
    if room is None:
        raise HTTPException(404, "Room not found")
    room.is_active = False
    db.query(RoomAgentRun).filter_by(room_id=room_id).update({
        "state": "stopped", "pending_task": {}, "active_tool": {},
        "generation": RoomAgentRun.generation + 1,
    })
    db.query(RoomTrigger).filter(
        RoomTrigger.room_id == room_id,
        RoomTrigger.state.in_(("running", "pending")),
    ).update({"state": "cancelled"}, synchronize_session=False)
    db.query(RoomGrant).filter_by(room_id=room_id).update({"decision": "revoked"})
    for action in db.query(ToolAction).filter(
            ToolAction.room_id == room_id,
            ToolAction.state.in_(("pending", "waiting", "approved", "started"))).all():
        state = "uncertain" if action.state == "started" else "cancelled"
        stage_action_result(db, action.action_id, state,
                            "Room closed; effect unknown." if state == "uncertain" else "Room closed.")


def close_room(db: Session, room_id: int) -> None:
    """Close a room while retaining its safe read-only roster for future cloning."""
    try:
        stage_close_room(db, room_id)
        db.commit()
    except BaseException:
        db.rollback()
        raise


def add_member(db: Session, room_id: int, agent_id: int) -> RoomMembership:
    from app.services.bot_profile import require_membership_change
    require_membership_change(db, room_id, agent_id)
    room = db.query(Conversation).filter_by(id=room_id, is_active=True).first()
    if room is None:
        raise HTTPException(404, "Room not found")
    agent = db.query(Agent).filter_by(id=agent_id, user_id=room.user_id, is_active=True).first()
    if agent is None:
        raise HTTPException(404, "Agent not found")
    member = db.get(RoomMembership, (room_id, agent_id))
    if member is None:
        member = RoomMembership(room_id=room_id, agent_id=agent_id)
        db.add(member)
    elif member.is_active:
        raise HTTPException(409, "Agent already joined")
    else:
        member.is_active = True
    db.flush()
    run = db.get(RoomAgentRun, (room_id, agent_id))
    if run is None:
        db.add(RoomAgentRun(room_id=room_id, agent_id=agent_id))
    else:
        run.state = "idle"
        run.paused = False
        run.active_tool = {}
        run.generation += 1
    db.commit()
    db.refresh(member)
    return member


def get_run(db: Session, room_id: int, agent_id: int) -> RoomAgentRun:
    member = db.get(RoomMembership, (room_id, agent_id))
    if member is None:
        raise HTTPException(404, "Room member not found")
    run = db.get(RoomAgentRun, (room_id, agent_id))
    if run is None:
        run = RoomAgentRun(room_id=room_id, agent_id=agent_id)
        db.add(run)
        db.flush()
    return run


def _require_member(db: Session, room_id: int, agent_id: int) -> RoomMembership:
    member = db.get(RoomMembership, (room_id, agent_id))
    if member is None or not member.is_active:
        raise HTTPException(404, "Room member not found")
    return member


def remove_member(db: Session, room_id: int, agent_id: int) -> None:
    from app.services.bot_profile import require_membership_change
    require_membership_change(db, room_id, agent_id, removing=True)
    _require_member(db, room_id, agent_id).is_active = False
    run = get_run(db, room_id, agent_id)
    run.state = "stopped"
    run.pending_task = {}
    run.active_tool = {}
    run.generation += 1
    db.query(RoomGrant).filter_by(room_id=room_id, agent_id=agent_id).update({"decision": "revoked"})
    db.commit()


def set_paused(db: Session, room_id: int, agent_id: int, paused: bool) -> RoomAgentRun:
    from app.services import resume_boundary
    if paused:return resume_boundary.pause(db, room_id, agent_id)
    return resume_boundary.resume(db, room_id, agent_id)[0]


def stop_room(db: Session, room_id: int) -> None:
    room = db.query(Conversation).join(Project, Project.id == Conversation.project_id).filter(
        Conversation.id == room_id, Conversation.is_active.is_(True),
        Project.is_active.is_(True)).first()
    if room is None:
        raise HTTPException(404, "Room not found")
    room.is_active = False
    db.query(RoomMembership).filter_by(room_id=room_id).update({"is_active": False})
    db.query(RoomAgentRun).filter_by(room_id=room_id).update({"state": "stopped", "pending_task": {},
                                                               "active_tool": {}, "generation": RoomAgentRun.generation + 1})
    db.query(RoomGrant).filter_by(room_id=room_id).update({"decision": "revoked"})
    for action in db.query(ToolAction).filter(ToolAction.room_id == room_id,
                                             ToolAction.state.in_(("pending", "waiting", "approved", "started"))).all():
        state = "uncertain" if action.state == "started" else "cancelled"
        record_action_result(db, action.action_id, state, "Room stopped; effect unknown." if state == "uncertain" else "Room stopped.")
    db.commit()


def cancel_room_work(db: Session, room_id: int) -> None:
    """Cancel active and queued work while keeping the room and membership usable."""
    db.query(RoomAgentRun).filter(RoomAgentRun.room_id == room_id,
                                  RoomAgentRun.state.in_(("running", "waiting", "paused"))).update(
        {"state": "idle", "paused": False, "pending_task": {}, "active_tool": {}, "generation": RoomAgentRun.generation + 1},
        synchronize_session=False)
    db.query(RoomTrigger).filter(RoomTrigger.room_id == room_id,
                                  RoomTrigger.state.in_(("running", "pending", "paused"))).update(
        {"state": "cancelled"}, synchronize_session=False)
    for action in db.query(ToolAction).filter(ToolAction.room_id == room_id,
                                             ToolAction.state.in_(("pending", "waiting", "approved", "started"))).all():
        state = "uncertain" if action.state == "started" else "cancelled"
        record_action_result(db, action.action_id, state, "Work cancelled; effect unknown." if state == "uncertain" else "Work cancelled.")
    db.commit()


def eligible_agents(db: Session, room_id: int) -> list[Agent]:
    return db.query(Agent).join(RoomMembership, RoomMembership.agent_id == Agent.id).outerjoin(
        RoomAgentRun,
        (RoomAgentRun.room_id == RoomMembership.room_id) & (RoomAgentRun.agent_id == Agent.id),
    ).join(Conversation, Conversation.id == RoomMembership.room_id).join(
        Project, Project.id == Conversation.project_id).filter(
        RoomMembership.room_id == room_id,
        RoomMembership.is_active.is_(True),
        Conversation.is_active.is_(True),
        Project.is_active.is_(True),
        Conversation.user_id == Agent.user_id,
        Agent.is_active.is_(True),
        (RoomAgentRun.paused.is_(False)) | (RoomAgentRun.paused.is_(None)),
    ).order_by(Agent.id).all()


def _live_turn(room_id: int, agent_id: int):
    return exists(select(1).select_from(RoomMembership).join(
        Conversation, Conversation.id == RoomMembership.room_id
    ).join(Agent, Agent.id == RoomMembership.agent_id).join(
        Project, Project.id == Conversation.project_id).where(and_(
        RoomMembership.room_id == room_id, RoomMembership.agent_id == agent_id,
        RoomMembership.is_active.is_(True), Conversation.is_active.is_(True),
        Project.is_active.is_(True),
        Conversation.user_id == Agent.user_id, Agent.is_active.is_(True),
    )))


def begin_turn(db: Session, room_id: int, agent_id: int, *, trigger_event_id: str | None = None,
               round_number: int | None = None, watermark: int | None = None, cross_job=None) -> int | None:
    # UPDATE is the dispatch claim. The generation invalidates stale provider results.
    if trigger_event_id is not None:
        from app.schemas.work_state import canonical_id
        try:
            canonical_id(trigger_event_id)
        except (TypeError, ValueError):
            return None
        if type(round_number) is not int or round_number <= 0 or type(watermark) is not int or watermark < 0:
            return None
    member = db.get(RoomMembership, (room_id, agent_id))
    if member is None:
        return None
    get_run(db, room_id, agent_id)
    db.commit()
    eligible = db.query(RoomAgentRun.generation).filter_by(room_id=room_id, agent_id=agent_id).scalar()
    values = {"state": "running", "generation": RoomAgentRun.generation + 1}
    if trigger_event_id is not None:
        run = db.get(RoomAgentRun, (room_id, agent_id))
        pending = dict(run.pending_task) if isinstance(run.pending_task, dict) else {}
        pending.pop('custody_teammate_job', None)
        if cross_job:
            pending['execution_job'] = dict(cross_job)
        else:
            pending.pop('execution_job', None)
            pending.pop('cross_resume_boundary', None)
        pending.pop("work_wave", None)
        if not pending.get('resume_boundary') or pending['resume_boundary'].get('trigger') != trigger_event_id:
            pending['execution_guard'] = ({'cross_job_id': cross_job['message_id'], 'phase': cross_job['phase']}
                if cross_job else resume_boundary.guard(db, room_id, agent_id))
        values.update(trigger_event_id=trigger_event_id, round=round_number,
                      watermark_seq=watermark, pending_task=pending, active_tool={})
    predicates = [
        RoomAgentRun.room_id == room_id, RoomAgentRun.agent_id == agent_id,
        RoomAgentRun.generation == eligible, RoomAgentRun.state == "idle",
        RoomAgentRun.paused.is_(False), _live_turn(room_id, agent_id),
    ]
    if trigger_event_id is not None and cross_job:
        from app.services.bot_messages import resolve_execution_origin
        resolve_execution_origin(db, cross_job["message_id"], cross_job["phase"], room_id, agent_id)
    if trigger_event_id is not None:
        predicates.append(exists(select(1).select_from(RoomTrigger).where(
            RoomTrigger.event_id == trigger_event_id,
            *([] if cross_job else [RoomTrigger.room_id == room_id]),
            RoomTrigger.state == "running", RoomTrigger.current_round == round_number)))
    changed = db.execute(update(RoomAgentRun).where(*predicates).values(**values)).rowcount
    db.commit()
    return eligible + 1 if changed else None


def finish_turn(db: Session, room_id: int, agent_id: int, generation: int | None, content: str,
                model_use: dict | None = None) -> int | None:
    if generation is None:
        return None
    changed = db.execute(update(RoomAgentRun).where(
        RoomAgentRun.room_id == room_id, RoomAgentRun.agent_id == agent_id,
        RoomAgentRun.generation == generation, RoomAgentRun.state == "running",
        RoomAgentRun.paused.is_(False), _live_turn(room_id, agent_id),
    ).values(state="idle")).rowcount
    if not changed:
        db.rollback()
        return None
    payload = {"content": content}
    if model_use:
        payload["model_use"] = model_use
    event = append_event(db, room_id, ActorRef("agent", agent_id), "message", payload)
    return event.payload["legacy_message_id"]


def finish_tool_request(db: Session, room_id: int, agent_id: int, generation: int,
                        result: ProviderResult) -> bool:
    changed = db.execute(update(RoomAgentRun).where(
        RoomAgentRun.room_id == room_id, RoomAgentRun.agent_id == agent_id,
        RoomAgentRun.generation == generation, RoomAgentRun.state == "running",
        RoomAgentRun.paused.is_(False), _live_turn(room_id, agent_id),
    ).values(state="waiting", pending_task={"call_ids": [call.call_id for call in result.tool_calls],
                                           "model_use": result.model_use})).rowcount
    if not changed:
        db.rollback()
        return False
    append_event(db, room_id, ActorRef("agent", agent_id), "activity",
                 {"status": "tool_calls_pending", "call_ids": [call.call_id for call in result.tool_calls],
                  "model_use": result.model_use})
    return True


def finish_failure(db: Session, room_id: int, agent_id: int, generation: int, failure: LLMFailure) -> bool:
    changed = db.execute(update(RoomAgentRun).where(
        RoomAgentRun.room_id == room_id, RoomAgentRun.agent_id == agent_id,
        RoomAgentRun.generation == generation, RoomAgentRun.state == "running",
        RoomAgentRun.paused.is_(False), _live_turn(room_id, agent_id),
    ).values(state="idle")).rowcount
    if not changed:
        db.rollback()
        return False
    append_event(db, room_id, ActorRef("agent", agent_id), "failure",
                 {"code": failure.code, "message": failure.message})
    return True


async def send_legacy_message(db: Session, room_id: int, content: str, on_event=None,
                              idempotency_key: str | None = None, provider_store=None,
                              provider_transport=None, provider_cache=None) -> tuple[Message, list[Message]]:
    """Bridge old chat transport until the event scheduler replaces it."""
    room = db.get(Conversation, room_id)
    user_event = append_event(db, room_id, ActorRef("user", room.user_id), "message", {"content": content}, idempotency_key)
    if on_event and not user_event._duplicate:
        on_event(room_id)
    user_message = db.get(Message, user_event.payload["legacy_message_id"])
    if user_event._duplicate:
        return user_message, []
    history = db.query(RoomEvent).filter_by(room_id=room_id, kind="message").order_by(RoomEvent.seq.desc()).limit(10).all()
    context = [{"role": event.payload.get("role", "assistant" if event.actor_type == "agent" else "user" if event.actor_type == "user" else "system"),
                "content": event.payload["content"]} for event in reversed(history)]
    responses = []
    for agent in eligible_agents(db, room_id):
        generation = begin_turn(db, room_id, agent.id)
        if generation is None:
            continue
        agent._room_id = room_id
        agent._provider_store = provider_store
        agent._provider_cache = provider_cache
        agent._provider_transport = provider_transport
        try:
            output = await llm_service.generate_response(agent=agent, messages=context, user_message=content)
            result = output if isinstance(output, ProviderResult) else ProviderResult(text=output)
            if not result.tool_calls and (not isinstance(result.text, str) or not result.text.strip()):
                raise LLMFailure("provider_invalid_response", "Provider returned no usable text")
        except Exception as error:
            failure = error if isinstance(error, LLMFailure) else LLMFailure("provider_unavailable", "Provider request failed")
            db.rollback()
            if finish_failure(db, room_id, agent.id, generation, failure) and on_event:
                on_event(room_id)
            raise failure from None
        except BaseException:
            # A failed call must release its claim, unless an intervention superseded it.
            db.rollback()
            db.query(RoomAgentRun).filter_by(room_id=room_id, agent_id=agent.id, generation=generation, state="running").update({"state": "idle"})
            db.commit()
            raise
        if result.tool_calls:
            if finish_tool_request(db, room_id, agent.id, generation, result) and on_event:
                on_event(room_id)
            continue
        message_id = (finish_turn(db, room_id, agent.id, generation, result.text, result.model_use)
                      if result.model_use else finish_turn(db, room_id, agent.id, generation, result.text))
        if message_id is not None:
            if on_event:
                on_event(room_id)
            responses.append(db.get(Message, message_id))
    return user_message, responses
