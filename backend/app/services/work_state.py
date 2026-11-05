"""Read-only, bounded work state; history never supplies a current generation."""

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import case
from sqlalchemy.orm import Session, load_only

from app.models.collaboration import RoomAgentRun, RoomEvent, RoomTrigger, ToolAction
from app.models.models import Agent, Conversation, Project
from app.schemas.work_state import WorkEventRef, WorkState, canonical_id

LIMIT = 512
OPEN_ACTIONS = {"pending", "waiting", "approved", "started"}


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


def _uuid(value: object) -> bool:
    try:
        canonical_id(value)
        return True
    except (TypeError, ValueError, AttributeError):
        return False


def _time(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _ref(event: RoomEvent | None, room_id: int) -> WorkEventRef | None:
    if event is None or event.room_id != room_id or not _uuid(event.event_id) or not _positive(event.seq):
        return None
    return WorkEventRef(event_id=event.event_id, seq=event.seq)


def _terminal(events: list[RoomEvent], run: RoomAgentRun, trigger: RoomTrigger) -> RoomEvent | None:
    if not _positive(run.generation) or not _positive(run.round):
        return None
    for event in events:
        payload = event.payload
        if (event.actor_type != "agent" or event.actor_id != run.agent_id
                or not isinstance(payload, dict) or payload.get("trigger_event_id") != trigger.event_id
                or type(payload.get("generation")) is not int or payload["generation"] != run.generation
                or type(payload.get("round")) is not int or payload["round"] != run.round):
            continue
        if event.kind in {"message", "failure"} or (event.kind == "activity" and payload.get("status") == "passed"):
            if _ref(event, trigger.room_id) is None:
                return None
            if event.kind == "message" and (not isinstance(payload.get("content"), str) or not payload["content"].strip()):
                return None
            return event
    return None


def _terminal_phase(event: RoomEvent) -> str:
    if event.kind == "failure":
        return "uncertain" if event.payload.get("code") in {"tool_effect_uncertain", "desktop_release_failed", "action_uncertain"} else "failed"
    return "completed" if event.kind == "message" else "passed"


def _queued(run: RoomAgentRun, trigger: RoomTrigger) -> bool:
    pending = run.pending_task if isinstance(run.pending_task, dict) else {}
    wave = pending.get("work_wave")
    return (isinstance(wave, dict) and wave.get("trigger_event_id") == trigger.event_id
            and type(wave.get("generation")) is int and wave["generation"] == run.generation
            and type(wave.get("round")) is int and wave["round"] == trigger.current_round
            and _positive(wave["round"]) and run.state == "idle")


def _checkin_state(db, value, run, row, room_id, owner_id):
    """Project only a ledger-backed system root matching this exact run generation."""
    if row is None or run is None or row.owner_id!=owner_id or row.room_id!=room_id or row.agent_id!=run.agent_id:
        return False
    if run.trigger_event_id!=row.root_event_id or not _uuid(row.run_id) or not _positive(row.generation):
        return False
    root=db.get(RoomEvent,row.root_event_id)
    ref=_ref(root,room_id)
    data=root.payload.get('checkin') if root and isinstance(root.payload,dict) else None
    if (ref is None or root.actor_type!='system' or root.kind!='activity'
            or root.payload.get('category')!='primary_checkin' or not isinstance(data,dict)
            or type(data.get('version')) is not int or data.get('version')!=1 or data.get('run_id')!=row.run_id
            or type(data.get('owner_id')) is not int or data.get('owner_id')!=owner_id
            or type(data.get('agent_id')) is not int or data.get('agent_id')!=run.agent_id
            or type(data.get('config_revision')) is not int or data.get('config_revision')!=row.config_revision
            or type(data.get('primary_revision')) is not int or data.get('primary_revision')!=row.primary_revision
            or data.get('scheduled_at')!=(_time(row.scheduled_at).isoformat() if _time(row.scheduled_at) else None)):
        value.phase='unknown';return True
    # A newer Owner message takes precedence, including the commit-before-trigger window.
    newer=db.query(RoomEvent.event_id).filter(RoomEvent.room_id==room_id,RoomEvent.seq>root.seq,
        RoomEvent.actor_type=='user',RoomEvent.actor_id==owner_id,RoomEvent.kind=='message').first()
    if newer:return False
    value.scope='current' if row.state=='running' else 'previous'
    value.trigger_event_id=row.root_event_id;value.generation=row.generation;value.request=ref
    value.updated_at=_time(row.completed_at or row.started_at) or value.updated_at
    if row.state=='running':
        valid=(run.generation==row.generation and run.state=='running'
            and (run.pending_task or {}).get('primary_checkin')==row.run_id and row.completed_at is None)
        value.phase='thinking' if valid else 'unknown'
    elif row.state=='completed':
        result=db.get(RoomEvent,row.result_event_id) if row.result_event_id else None
        payload=result.payload if result and isinstance(result.payload,dict) else {}
        valid=(run.generation==row.generation and run.state=='idle' and row.completed_at is not None
            and result is not None and result.actor_type=='agent' and result.actor_id==run.agent_id
            and result.kind=='message' and _ref(result,room_id) is not None and result.seq>root.seq
            and payload.get('checkin')=={'version':1,'run_id':row.run_id}
            and type(payload['checkin'].get('version')) is int
            and isinstance(payload.get('content'),str) and bool(payload['content'].strip()))
        value.phase='completed' if valid else 'unknown'
        if valid:value.result=_ref(result,room_id)
    elif row.state in {'failed','cancelled','interrupted'}:
        expected_generations={row.generation,row.generation+1} if row.state in {'cancelled','interrupted'} else {row.generation}
        valid=(run.generation in expected_generations and run.state=='idle' and row.completed_at is not None and row.result_event_id is None)
        value.phase=row.state if valid else 'unknown'
    else:value.phase='unknown'
    return True


def _routine_state(db,value,run,row,room_id,owner_id):
    if row is None or run is None or row.owner_id!=owner_id or row.room_id!=room_id or row.agent_id!=run.agent_id:
        return False
    if run.trigger_event_id!=row.root_event_id or not _uuid(row.run_id) or not _positive(row.generation):return False
    root=db.get(RoomEvent,row.root_event_id);ref=_ref(root,room_id)
    data=root.payload.get('routine') if root and isinstance(root.payload,dict) else None
    if (ref is None or root.actor_type!='system' or root.kind!='activity' or root.payload.get('category')!='owner_routine'
            or not isinstance(data,dict) or type(data.get('version')) is not int or data.get('version')!=1
            or data.get('run_id')!=row.run_id or data.get('routine_id')!=row.routine_id
            or type(data.get('owner_id')) is not int or data['owner_id']!=owner_id
            or type(data.get('agent_id')) is not int or data['agent_id']!=run.agent_id
            or type(data.get('config_revision')) is not int or data['config_revision']!=row.config_revision
            or data.get('origin')!=row.origin or data.get('scheduled_at')!=(_time(row.scheduled_at).isoformat() if _time(row.scheduled_at) else None)):
        value.phase='unknown';return True
    newer=db.query(RoomEvent.event_id).filter(RoomEvent.room_id==room_id,RoomEvent.seq>root.seq,RoomEvent.actor_type=='user',RoomEvent.actor_id==owner_id,RoomEvent.kind=='message').first()
    if newer:return False
    value.scope='current' if row.state=='running' else 'previous';value.trigger_event_id=row.root_event_id
    value.generation=row.generation;value.round=1;value.request=ref;value.updated_at=_time(row.completed_at or row.started_at) or value.updated_at
    if row.state=='running':
        valid=run.generation==row.generation and run.state=='running' and (run.pending_task or {}).get('owner_routine')==row.run_id and row.completed_at is None
        value.phase='thinking' if valid else 'unknown'
        if valid and isinstance(run.active_tool,dict) and run.active_tool.get('action_id'):
            marker=run.active_tool; action_id=marker['action_id']
            action=db.get(ToolAction,action_id) if _uuid(action_id) else None
            if (type(marker.get('generation')) is int and marker['generation']==row.generation
                    and action and action.room_id==room_id and action.agent_id==run.agent_id
                    and action.trigger_event_id==root.event_id and action.state in {'pending','waiting','approved','started','uncertain'}):
                value.phase={'pending':'tool_waiting','waiting':'approval_waiting','approved':'approval_waiting','started':'tool_running','uncertain':'uncertain'}[action.state]
                value.action_id=action.action_id
            else:value.phase='unknown'
    elif row.state=='completed':
        event=db.get(RoomEvent,row.result_event_id) if row.result_event_id else None;p=event.payload if event and isinstance(event.payload,dict) else {}
        valid=(run.generation==row.generation and run.state=='idle' and row.completed_at is not None and event is not None
            and event.actor_type=='agent' and event.actor_id==run.agent_id and event.kind=='message' and _ref(event,room_id) is not None
            and event.seq>root.seq and p.get('routine')=={'version':1,'run_id':row.run_id} and type(p['routine'].get('version')) is int
            and isinstance(p.get('content'),str) and bool(p['content'].strip()))
        value.phase='completed' if valid else 'unknown'
        if valid:value.result=_ref(event,room_id)
    elif row.state=='failed':
        event=db.get(RoomEvent,row.result_event_id) if row.result_event_id else None
        payload=event.payload if event and isinstance(event.payload,dict) else {}
        valid=(run.generation==row.generation and run.state=='idle' and row.completed_at is not None and event is not None
            and event.actor_type=='agent' and event.actor_id==run.agent_id and event.kind=='failure' and _ref(event,room_id) is not None
            and event.seq>root.seq and payload.get('code')=='routine_failed' and payload.get('trigger_event_id')==root.event_id
            and type(payload.get('generation')) is int and payload['generation']==row.generation and payload.get('round')==1
            and payload.get('routine')=={'version':1,'run_id':row.run_id})
        value.phase='failed' if valid else 'unknown'
        if valid:value.result=_ref(event,room_id)
    elif row.state in {'cancelled','interrupted'}:
        generations={row.generation,row.generation+1}
        value.phase=row.state if run.generation in generations and run.state=='idle' and row.completed_at is not None and row.result_event_id is None else 'unknown'
    else:value.phase='unknown'
    return True


def project_work_states(db: Session, room_id: int, owner_id: int, members: list[dict]) -> dict[int, WorkState]:
    """Called by the existing /members service, also independently owner checked."""
    room = db.query(Conversation).filter_by(id=room_id, user_id=owner_id).first()
    project = db.query(Project).filter_by(id=room.project_id, user_id=owner_id).first() if room else None
    if room is None or project is None:
        raise HTTPException(404, "Room not found")
    result = {m["agent_id"]: WorkState(room_id=room_id, agent_id=m["agent_id"]) for m in members}
    ids = list(result)
    if len(ids) > LIMIT:
        return {key: value.model_copy(update={"phase": "unknown"}) for key, value in result.items()}
    eligible = {row[0] for row in db.query(Agent.id).filter(
        Agent.id.in_(ids), Agent.user_id == owner_id, Agent.is_active.is_(True)).all()}
    runs = {run.agent_id: run for run in db.query(RoomAgentRun).filter(
        RoomAgentRun.room_id == room_id, RoomAgentRun.agent_id.in_(ids)).all()}
    query = db.query(RoomTrigger).join(RoomEvent, RoomEvent.event_id == RoomTrigger.event_id).filter(
        RoomTrigger.room_id == room_id, RoomEvent.room_id == room_id,
        RoomEvent.actor_type == "user", RoomEvent.actor_id == owner_id, RoomEvent.kind == "message")
    trigger = query.filter(RoomTrigger.state.in_(("running", "pending", "paused"))).order_by(
        case((RoomTrigger.state == "running", 0), else_=1), RoomEvent.seq).first()
    scope = "current"
    if trigger is None:
        trigger = query.filter(RoomTrigger.state.in_(("done", "cancelled", "interrupted"))).order_by(RoomEvent.seq.desc()).first()
        scope = "previous" if trigger else "none"
    request_event = db.get(RoomEvent, trigger.event_id) if trigger else None
    request_ref = _ref(request_event, room_id)
    selected = trigger.selected_agents if trigger and isinstance(trigger.selected_agents, list) else []
    frame_valid = bool(trigger and request_ref and all(_positive(value) for value in selected) and len(selected) <= LIMIT)
    selected_ids = set(selected) if frame_valid else set()
    from app.services.teammates import root_rows
    handoffs = root_rows(db, room_id, trigger.event_id) if trigger else []
    selected_ids.update(aid for row in handoffs for aid in (row.sender_agent_id, row.recipient_agent_id))
    events: list[RoomEvent] = []
    actions: list[ToolAction] = []
    if frame_valid:
        events = db.query(RoomEvent).filter(
            RoomEvent.room_id == room_id, RoomEvent.seq > request_ref.seq,
            RoomEvent.payload["trigger_event_id"].as_string() == trigger.event_id,
        ).order_by(RoomEvent.seq.desc()).limit(LIMIT).all()
        actions = db.query(ToolAction).options(load_only(
            ToolAction.action_id, ToolAction.room_id, ToolAction.agent_id, ToolAction.trigger_event_id,
            ToolAction.call_id, ToolAction.state, ToolAction.started_at, ToolAction.completed_at,
        )).filter(ToolAction.room_id == room_id, ToolAction.trigger_event_id == trigger.event_id,
                  ToolAction.agent_id.in_(ids)).order_by(ToolAction.action_id).limit(LIMIT).all()
    from app.models.primary_checkins import PrimaryCheckinRun
    run_roots={run.trigger_event_id for run in runs.values() if _uuid(run.trigger_event_id)}
    checkin_rows=db.query(PrimaryCheckinRun).filter(
        PrimaryCheckinRun.owner_id==owner_id,PrimaryCheckinRun.room_id==room_id,
        PrimaryCheckinRun.agent_id.in_(ids),PrimaryCheckinRun.root_event_id.in_(run_roots)).limit(LIMIT+1).all()
    checkins={}
    for row in checkin_rows:
        checkins[row.root_event_id]=row if row.root_event_id not in checkins else False
    if len(checkin_rows)>LIMIT:
        checkins={root:False for root in run_roots}
    from app.models.owner_routines import OwnerRoutineRun
    routine_rows=db.query(OwnerRoutineRun).filter(OwnerRoutineRun.owner_id==owner_id,OwnerRoutineRun.room_id==room_id,
        OwnerRoutineRun.agent_id.in_(ids),OwnerRoutineRun.root_event_id.in_(run_roots)).limit(LIMIT+1).all()
    routines={}
    for row in routine_rows:routines[row.root_event_id]=row if row.root_event_id not in routines else False
    if len(routine_rows)>LIMIT:routines={root:False for root in run_roots}
    from app.models.bot_message import BotMessage
    cross_rows = db.query(BotMessage).filter(BotMessage.owner_id == owner_id).filter(
        (BotMessage.sender_room_id == room_id) | (BotMessage.recipient_room_id == room_id)).all()
    for agent_id, value in result.items():
        run = runs.get(agent_id)
        value.updated_at = _time(run.updated_at) if run else None
        if not room.is_active or not project.is_active or (run and run.state == "stopped"):
            value.phase = "stopped"
            continue
        if agent_id not in eligible:
            value.phase = "unknown"
            continue
        if run and (run.paused or run.state == "paused"):
            value.phase = "paused"
            continue
        if scope!='current' and run and run.trigger_event_id in checkins:
            if checkins[run.trigger_event_id] is False:
                value.phase='unknown';continue
            if _checkin_state(db,value,run,checkins[run.trigger_event_id],room_id,owner_id):
                continue
        if scope!='current' and run and run.trigger_event_id in routines:
            if routines[run.trigger_event_id] is False:
                value.phase='unknown';continue
            if _routine_state(db,value,run,routines[run.trigger_event_id],room_id,owner_id):continue
        if trigger is None:
            value.phase = "idle" if not run or run.state == "idle" else "unknown"
            continue
        if not frame_valid:
            value.phase = "unknown"
            continue
        if agent_id not in selected_ids:
            continue
        value.scope, value.trigger_event_id, value.request = scope, trigger.event_id, request_ref
        if scope == "previous" and trigger.state in {"cancelled", "interrupted"}:
            value.phase = trigger.state
            continue
        if trigger.state == "paused":
            value.phase = "paused"
            value.round = trigger.current_round if _positive(trigger.current_round) else None
            continue
        waiting_for = [row.recipient_agent_id for row in handoffs if row.sender_agent_id == agent_id
                       and (row.state in {"pending", "running"} or row.reply_state == "pending")]
        receiving = [row for row in handoffs if row.recipient_agent_id == agent_id and row.state == "pending"]
        if scope == "current" and run and run.state == "idle" and waiting_for:
            value.phase, value.peer_agent_ids = "peer_waiting", sorted(set(waiting_for))[:64]
            continue
        if scope == "current" and run and run.state == "idle" and receiving:
            value.phase = "queued"
            continue
        if trigger.state == "pending" or (run and _queued(run, trigger)):
            value.phase = "queued"
            value.round = trigger.current_round if _positive(trigger.current_round) else None
            continue
        if (not run or run.trigger_event_id != trigger.event_id or not _positive(run.generation)
                or not _positive(run.round) or run.round > trigger.current_round):
            value.phase = "queued" if scope == "current" and (not run or run.state == "idle") else "unknown"
            continue
        value.generation, value.round = run.generation, run.round
        terminal = _terminal(events, run, trigger)
        if scope == "current" and run.state in {"running", "waiting"}:
            marker = run.active_tool if isinstance(run.active_tool, dict) else {}
            call_ids = marker.get("call_ids")
            if not marker:
                value.phase = "thinking" if run.state == "running" else "unknown"
                continue
            if (not isinstance(call_ids, list) or not 1 <= len(call_ids) <= 64
                    or any(not isinstance(call, str) or not call or len(call) > 200 for call in call_ids)
                    or type(marker.get("generation")) is not int or marker["generation"] != run.generation
                    or type(marker.get("round")) is not int or marker["round"] != run.round):
                value.phase = "unknown"
                continue
            action_id = marker.get("action_id")
            if action_id is None:
                value.phase = "tool_waiting"
                continue
            if not _uuid(action_id) or len(actions) >= LIMIT:
                value.phase = "unknown"
                continue
            matched = [action for action in actions if action.agent_id == agent_id and action.call_id in call_ids
                       and action.action_id == action_id]
            active = [action for action in matched if action.state in OPEN_ACTIONS | {"uncertain"}]
            if len(active) > 1:
                value.phase = "unknown"
                continue
            if not active:
                value.phase = "thinking" if matched and run.state == "running" else "unknown"
                continue
            action = active[0]
            value.phase = {"waiting": "approval_waiting", "started": "tool_running", "uncertain": "uncertain"}.get(action.state, "tool_waiting")
            value.action_id = action.action_id
            for event in events:
                payload = event.payload
                if (isinstance(payload, dict) and payload.get("action_id") == action.action_id
                        and payload.get("agent_id") == agent_id and event.actor_type in {"tool", "user"}
                        and event.kind in {"tool_result", "activity"}):
                    value.event = _ref(event, room_id)
                    break
            continue
        if run.state != "idle" or terminal is None:
            value.phase = "unknown"
            continue
        if len(actions) >= LIMIT:
            value.phase = "unknown"
            continue
        value.phase = _terminal_phase(terminal)
        value.result = _ref(terminal, room_id)
        value.updated_at = _time(terminal.created_at) or value.updated_at
        unresolved = next((action for action in actions if action.agent_id == agent_id
                           and action.state == "uncertain" and _uuid(action.action_id)), None)
        if unresolved:
            value.phase, value.action_id = "uncertain", unresolved.action_id
            value.event = next((_ref(event, room_id) for event in events if event.actor_type == "tool"
                and event.kind == "tool_result" and isinstance(event.payload, dict)
                and event.payload.get("state") == "uncertain" and event.payload.get("agent_id") == agent_id
                and event.payload.get("action_id") == unresolved.action_id), None)
            continue
        if value.phase in {"failed", "uncertain"}:
            value.event = value.result
            continue
        if (scope == "current" and trigger.state == "running" and run.round == 1
                and trigger.current_round == 1 and trigger.max_rounds > 1):
            peers = [peer_id for peer_id, peer in runs.items() if peer_id != agent_id
                     and peer_id in eligible & selected_ids and not peer.paused
                     and peer.trigger_event_id == trigger.event_id and peer.round == run.round
                     and _positive(peer.generation) and peer.state in {"running", "waiting"}]
            if peers:
                value.phase, value.peer_agent_ids = "peer_waiting", sorted(peers)[:64]
    for agent_id, value in result.items():
        run = runs.get(agent_id)
        relevant = [r for r in cross_rows if (r.sender_room_id == room_id and r.sender_agent_id == agent_id)
                    or (r.recipient_room_id == room_id and r.recipient_agent_id == agent_id)]
        active = [r for r in relevant if r.state in {"pending", "running"} or r.reply_state in {"pending", "running"}]
        if not active or not run or run.paused or run.state == "stopped": continue
        current = active[-1]
        if run.state in {"running", "waiting"} and run.trigger_event_id != current.root_request_event_id:
            continue  # Preserve the genuine active Owner request while a foreign job queues.
        value.scope, value.trigger_event_id = "current", current.root_request_event_id
        # A foreign room's seq must never be dressed as a local WorkEventRef.
        value.request = _ref(db.get(RoomEvent, current.request_event_id), room_id)
        if run.state == "running" and run.trigger_event_id == current.root_request_event_id:
            value.phase, value.generation = "thinking", run.generation
            action_id = (run.active_tool or {}).get('action_id')
            action = db.get(ToolAction, action_id) if action_id else None
            if (action and action.room_id == room_id and action.agent_id == agent_id
                    and action.trigger_event_id == current.root_request_event_id
                    and action.execution_binding and action.execution_binding.get('run_generation') == run.generation):
                value.phase = {'waiting':'approval_waiting','approved':'approval_waiting','pending':'tool_waiting',
                    'started':'tool_running','uncertain':'uncertain'}.get(action.state,'thinking')
                value.action_id = action.action_id
        elif run.state == "idle" and current.sender_room_id == room_id and current.sender_agent_id == agent_id:
            value.phase, value.peer_agent_ids = "peer_waiting", [current.recipient_agent_id]
        elif run.state == "idle": value.phase = "queued"
    return result
