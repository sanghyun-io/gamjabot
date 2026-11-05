"""Bounded read model for one exact owner room request."""

from collections import defaultdict
import re
from urllib.parse import urlencode
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, case, func, not_, or_
from sqlalchemy.orm import Session, aliased

from app.models.collaboration import RoomAgentRun, RoomEvent, RoomTrigger, ToolAction
from app.models.models import Agent, Conversation, Project


ITEM_LIMIT = 512
TRIGGER_PHASES = {
    "pending": "queued", "running": "running", "done": "completed",
    "cancelled": "cancelled", "interrupted": "interrupted",
}
ACTION_STATES = {
    "pending", "waiting", "approved", "started", "completed", "failed", "denied",
    "cancelled", "uncertain",
}
ACTION_PHASES = {
    "pending": "queued", "waiting": "approval", "approved": "queued", "started": "running",
    "completed": "completed", "failed": "failed", "denied": "denied",
    "cancelled": "cancelled", "uncertain": "uncertain",
}
RUN_STATES = {"running", "waiting", "idle", "stopped"}
OPEN_ACTION_STATES = {"pending", "waiting", "approved", "started", "uncertain"}
FAILED_ACTION_STATES = {"failed", "denied", "cancelled"}


def canonical_uuid(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 36 or value != value.lower():
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _canonical_uuid_column(column):
    compact = func.replace(column, "-", "")
    return and_(
        func.length(column) == 36,
        func.lower(column) == column,
        func.substr(column, 9, 1) == "-",
        func.substr(column, 14, 1) == "-",
        func.substr(column, 19, 1) == "-",
        func.substr(column, 24, 1) == "-",
        not_(compact.op("GLOB")("*[^0-9a-f]*")),
    )


def _not_found() -> None:
    raise HTTPException(404, {"code": "request_execution_not_found"})


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value[:limit]


def _preview(payload: object) -> str:
    content = payload.get("content") if isinstance(payload, dict) else None
    return re.sub(r"\s+", " ", content).strip()[:160] if isinstance(content, str) else ""


def _href(room_id: int, **params: object) -> str:
    clean = {key: value for key, value in params.items() if value is not None}
    return f"/chat/{room_id}" + ("?" + urlencode(clean) if clean else "")


def _failure_summary(payload: dict) -> str:
    code = payload.get("code")
    if not isinstance(code, str):
        return "failure_other"
    if code in {"context_too_large", "memory_too_large", "input_too_large"}:
        return "failure_context"
    if code in {"tool_effect_uncertain", "action_uncertain", "desktop_release_failed"}:
        return "failure_uncertain"
    if code in {"screen_recording_denied", "accessibility_denied", "permission_denied"}:
        return "failure_permission"
    if code.startswith(("provider_", "connection_", "credential_", "model_")) or code == "quota_exhausted":
        return "failure_connection"
    if code == "review_changed" or code.startswith(("tool_", "executor_", "invalid_tool")):
        return "failure_tool"
    return "failure_other"


def _action_summary(family: object) -> str:
    if not isinstance(family, str):
        return "action_other"
    if family == "start_process":
        return "action_process"
    if family in {"read_file", "write_file"}:
        return "action_files"
    if family.startswith("browser_"):
        return "action_browser"
    if family == "screenshot":
        return "action_screen"
    if family.startswith(("pointer_", "keyboard_")):
        return "action_input"
    return "action_other"


def _model_use(payload: dict) -> dict | None:
    value = payload.get("model_use")
    if not isinstance(value, dict):
        return None
    expected = {"connection_id", "model_id", "provider_kind", "watermark_seq"}
    if not expected <= value.keys():
        return None
    connection_id, watermark = value.get("connection_id"), value.get("watermark_seq")
    model, provider = value.get("model_id"), value.get("provider_kind")
    if (type(connection_id) is not int or connection_id <= 0
            or type(watermark) is not int or watermark <= 0
            or not isinstance(model, str) or not 1 <= len(model) <= 200 or not model.isprintable()
            or not isinstance(provider, str) or not 1 <= len(provider) <= 40
            or not provider.isprintable()):
        return None
    return {
        "connection_id": connection_id, "model_id": model,
        "provider_kind": provider, "watermark_seq": watermark,
    }


def _canonical_selected(value: object) -> list[int] | None:
    if (not isinstance(value, list) or len(value) > 64
            or any(type(item) is not int or item <= 0 for item in value)
            or len(set(value)) != len(value)):
        return None
    return value


def _canonical_policy(trigger: RoomTrigger | None) -> dict | None:
    if trigger is None:
        return None
    values = (trigger.max_rounds, trigger.max_tool_calls, trigger.max_executor_seconds)
    if (type(values[0]) is not int or not 1 <= values[0] <= 4
            or type(values[1]) is not int or not 0 <= values[1] <= 64
            or type(values[2]) is not int or not 1 <= values[2] <= 3600):
        return None
    return {
        "max_rounds": values[0], "max_tool_calls": values[1],
        "max_executor_seconds": values[2],
    }


def _completion(trigger: RoomTrigger | None, policy: dict | None, selected: list[int] | None,
                owned_ids: list[int], raw_outcome_count: int, outcome_total: int, rows: list,
                result_available: bool, raw_action_count: int, action_total: int,
                action_counts: dict) -> dict:
    base_state = TRIGGER_PHASES.get(trigger.state, "unknown") if trigger is not None else "unknown"
    if base_state in {"queued", "running"} and action_counts["waiting"]:
        state = "waiting_approval"
    else:
        state = base_state
    metadata_valid = bool(
        trigger is not None and policy is not None and selected is not None
        and len(owned_ids) == len(selected)
        and raw_outcome_count == outcome_total
        and raw_action_count == action_total
        and not action_counts["unknown"]
    )
    current_round = trigger.current_round if trigger is not None else None
    if (not metadata_valid or type(current_round) is not int or current_round < 0
            or policy is None or current_round > policy["max_rounds"]):
        return {"state": state, "outcome_status": "unknown", "result_available": result_available,
                "completed_rounds": 0, "pending_agent_count": len(selected or [])}

    # At most 64 Agents x 4 rounds are canonical. More rows necessarily mean
    # duplicate/ambiguous outcomes, even though the display remains capped at 512.
    if outcome_total > len(selected or []) * max(current_round, 1):
        return {"state": state, "outcome_status": "unknown", "result_available": result_available,
                "completed_rounds": 0, "pending_agent_count": len(selected or [])}

    pairs: dict[tuple[int, int], list[str]] = defaultdict(list)
    speakers = set()
    for event, *_rest in rows:
        payload = event.payload if isinstance(event.payload, dict) else {}
        round_number = payload.get("round")
        pairs[(event.actor_id, round_number)].append(event.kind)
        if round_number == 1 and event.kind == "message":
            speakers.add(event.actor_id)

    expected: dict[int, set[int]] = {}
    if current_round >= 1:
        expected[1] = set(selected or [])
    followups = {agent_id for agent_id in (selected or []) if speakers - {agent_id}}
    for round_number in range(2, current_round + 1):
        expected[round_number] = followups
    expected_pairs = {(agent_id, round_number) for round_number, ids in expected.items() for agent_id in ids}
    if any(pair not in expected_pairs or len(kinds) != 1 for pair, kinds in pairs.items()):
        metadata_valid = False

    completed_rounds = 0
    for round_number in range(1, current_round + 1):
        ids = expected.get(round_number, set())
        if not ids or any(len(pairs.get((agent_id, round_number), [])) != 1 for agent_id in ids):
            break
        completed_rounds = round_number
    current_ids = expected.get(current_round, set(selected or []) if current_round == 0 else set())
    pending = sum(len(pairs.get((agent_id, current_round), [])) != 1 for agent_id in current_ids)
    if state == "completed" and completed_rounds != current_round:
        metadata_valid = False

    if not metadata_valid:
        outcome_status = "unknown"
    elif state in {"queued", "running", "waiting_approval"}:
        outcome_status = "pending"
    elif state == "unknown":
        outcome_status = "unknown"
    elif state in {"cancelled", "interrupted"}:
        outcome_status = "partial" if result_available else "failed"
    elif pending:
        outcome_status = "unknown"
    else:
        kinds = [kind for values in pairs.values() for kind in values]
        has_failure = "failure" in kinds or bool(action_counts["failed"] or action_counts["open"])
        if has_failure:
            outcome_status = "partial" if result_available else "failed"
        else:
            outcome_status = "succeeded" if kinds else "failed"
    return {"state": state, "outcome_status": outcome_status,
            "result_available": result_available, "completed_rounds": completed_rounds,
            "pending_agent_count": pending}


def build_request_inspector(db: Session, owner_id: int, room_id: int, request_event_id: str) -> dict:
    from app.services import teammates
    team_rows = teammates.root_rows(db, room_id, request_event_id)
    team_ids = {aid for row in team_rows for aid in (row.sender_agent_id, row.recipient_agent_id)}
    from app.services.bot_messages import root_rows as bot_root_rows
    team_ids |= {aid for r in bot_root_rows(db, request_event_id) if r.owner_id == owner_id and r.root_room_id == room_id
        for aid in (r.sender_agent_id, r.recipient_agent_id)}
    raw_outcome = aliased(RoomEvent)
    raw_action = aliased(ToolAction)
    # Creation requests are provenance, not round outcomes. Exempt only the
    # exact committed receipt-backed event: an unbacked or malformed Agent
    # activity must still invalidate the raw/canonical outcome count audit.
    from app.models.bot_creation import BotCreation
    creation_event = aliased(RoomEvent)
    creation_tag = creation_event.payload['bot_creation']
    creation_requests = db.query(BotCreation.request_event_id).join(
        creation_event, creation_event.event_id == BotCreation.request_event_id,
    ).filter(
        BotCreation.owner_id == owner_id, BotCreation.root_room_id == room_id,
        BotCreation.root_request_event_id == request_event_id,
        creation_event.room_id == room_id, creation_event.actor_type == 'agent',
        creation_event.actor_id == BotCreation.source_agent_id, creation_event.kind == 'activity',
        creation_event.payload['trigger_event_id'].as_string() == request_event_id,
        creation_event.payload['category'].as_string() == 'bot_creation',
        creation_event.payload['status'].as_string() == 'requested',
        func.json_type(creation_event.payload, '$.bot_creation.version') == 'integer',
        func.json_type(creation_event.payload, '$.bot_creation.root_room_id') == 'integer',
        func.json_type(creation_event.payload, '$.bot_creation.source_agent_id') == 'integer',
        creation_tag['version'].as_integer() == 1,
        creation_tag['creation_id'].as_string() == BotCreation.creation_id,
        creation_tag['phase'].as_string() == 'request',
        creation_tag['root_room_id'].as_integer() == room_id,
        creation_tag['root_request_event_id'].as_string() == request_event_id,
        creation_tag['source_agent_id'].as_integer() == BotCreation.source_agent_id,
    )
    raw_outcome_count = db.query(func.count(raw_outcome.event_id)).filter(
        raw_outcome.room_id == room_id,
        raw_outcome.actor_type == "agent",
        raw_outcome.event_id.notin_(creation_requests),
        func.json_type(raw_outcome.payload, "$.teammate_handoff").is_(None),
        func.json_type(raw_outcome.payload, "$.bot_message").is_(None),
        func.json_type(raw_outcome.payload, "$.bot_batch").is_(None),
        raw_outcome.kind.in_(("message", "failure", "activity")),
        or_(raw_outcome.kind != 'activity',
            func.coalesce(raw_outcome.payload['category'].as_string(),'') != 'bot_message_reaction',
            func.coalesce(func.json_type(raw_outcome.payload,'$.version'),'') != 'integer',
            func.coalesce(raw_outcome.payload['version'].as_integer(),0) != 1),
        raw_outcome.payload["trigger_event_id"].as_string() == request_event_id,
    ).scalar_subquery()
    raw_action_count = db.query(func.count(raw_action.action_id)).filter(
        or_(raw_action.room_id == room_id,
            raw_action.execution_binding['root_request_event_id'].as_string() == request_event_id),
        raw_action.trigger_event_id == request_event_id,
    ).scalar_subquery()
    row = db.query(RoomEvent, Conversation, Project, RoomTrigger,
                   raw_outcome_count.label("raw_outcome_count"),
                   raw_action_count.label("raw_action_count")).join(
        Conversation, Conversation.id == RoomEvent.room_id,
    ).join(
        Project, Project.id == Conversation.project_id,
    ).outerjoin(
        RoomTrigger,
        and_(RoomTrigger.event_id == RoomEvent.event_id, RoomTrigger.room_id == RoomEvent.room_id),
    ).filter(
        RoomEvent.event_id == request_event_id,
        RoomEvent.room_id == room_id,
        RoomEvent.actor_type == "user",
        RoomEvent.actor_id == owner_id,
        RoomEvent.kind == "message",
        Conversation.user_id == owner_id,
        Project.user_id == owner_id,
    ).first()
    if row is None:
        _not_found()
    request, room, project, trigger, raw_outcome_count, raw_action_count = row
    policy = _canonical_policy(trigger)
    selected = _canonical_selected(trigger.selected_agents) if trigger is not None else None
    if trigger is not None and (policy is None or selected is None):
        policy = None
    safe_selected = selected or []

    owned_runs = {}
    if safe_selected:
        owned_runs = {
            agent_id: run
            for agent_id, run in db.query(Agent.id, RoomAgentRun).outerjoin(
                RoomAgentRun,
                and_(RoomAgentRun.room_id == room_id, RoomAgentRun.agent_id == Agent.id),
            ).filter(Agent.user_id == owner_id, Agent.id.in_(safe_selected)).all()
        }
    owned_ids = [agent_id for agent_id in safe_selected if agent_id in owned_runs]

    outcomes = []
    outcome_total = 0
    rows=[]
    if owned_ids:
        action_room = aliased(Conversation)
        total = func.count().over().label("total")
        message_total = func.sum(case((RoomEvent.kind == "message", 1), else_=0)).over().label("message_total")
        rows = db.query(RoomEvent, total, message_total).join(
            Agent, and_(Agent.id == RoomEvent.actor_id, Agent.user_id == owner_id),
        ).filter(
            RoomEvent.room_id == room_id,
            RoomEvent.actor_type == "agent",
            RoomEvent.actor_id.in_(owned_ids),
            func.json_type(RoomEvent.payload, "$.teammate_handoff").is_(None),
            func.json_type(RoomEvent.payload, "$.bot_message").is_(None),
            func.json_type(RoomEvent.payload, "$.bot_batch").is_(None),
            RoomEvent.kind.in_(("message", "failure", "activity")),
            or_(RoomEvent.kind != 'activity',
                func.coalesce(RoomEvent.payload['category'].as_string(),'') != 'bot_message_reaction',
                func.coalesce(func.json_type(RoomEvent.payload,'$.version'),'') != 'integer',
                func.coalesce(RoomEvent.payload['version'].as_integer(),0) != 1),
            RoomEvent.payload["trigger_event_id"].as_string() == request_event_id,
            func.json_type(RoomEvent.payload, "$.round") == "integer",
            RoomEvent.payload["round"].as_integer() >= 1,
            RoomEvent.payload["round"].as_integer() <= 4,
            or_(RoomEvent.kind != "activity", RoomEvent.payload["status"].as_string() == "passed"),
            _canonical_uuid_column(RoomEvent.event_id),
        ).order_by(RoomEvent.seq.asc(), RoomEvent.event_id.asc()).limit(ITEM_LIMIT + 1).all()
        outcome_total = rows[0].total if rows else 0
        for event, _total, _message_total in rows[:ITEM_LIMIT]:
            payload = event.payload if isinstance(event.payload, dict) else {}
            kind = "passed" if event.kind == "activity" else event.kind
            phase = {"message": "succeeded", "failure": "failed", "passed": "passed"}[kind]
            summary = "response" if kind == "message" else "passed" if kind == "passed" else _failure_summary(payload)
            outcomes.append({
                "event_id": event.event_id, "seq": event.seq, "round": payload["round"],
                "agent_id": event.actor_id,
                "agent_name_snapshot": _text(event.actor_name_snapshot, 200),
                "kind": kind, "phase": phase, "summary_code": summary,
                "model_use": _model_use(payload) if kind == "message" else None,
                # Inspector links address immutable history. The owner-wide attention
                # inbox alone uses failure= to recheck current attention eligibility.
                "href": _href(room_id, seq=event.seq),
            })

    # Only a DB-validated system admission failure for this exact structured
    # reply can join the terminal outcomes; never impersonate its target Bot.
    from app.services.events import canonical_reply_scope_failure
    from collections import namedtuple
    from types import SimpleNamespace
    system_events=db.query(RoomEvent).filter(RoomEvent.room_id==room_id,RoomEvent.actor_type=='system',
        RoomEvent.kind=='failure',RoomEvent.payload['category'].as_string()=='reply_target_unavailable',
        RoomEvent.payload['trigger_event_id'].as_string()==request_event_id).order_by(RoomEvent.seq).all()
    raw_outcome_count=int(raw_outcome_count or 0)+len(system_events)
    extra_rows=[]
    for event in system_events:
        proof=canonical_reply_scope_failure(db,event,owner_id)
        if not proof or proof[1] not in owned_ids:continue
        _,agent_id,_,source=proof
        proxy=SimpleNamespace(actor_id=agent_id,payload=event.payload,kind='failure')
        extra_rows.append(proxy)
        outcomes.append({'event_id':event.event_id,'seq':event.seq,'round':1,'agent_id':agent_id,
            'actor_type':'system','agent_name_snapshot':_text(source.actor_name_snapshot,200),
            'kind':'failure','phase':'failed','summary_code':'failure_other','model_use':None,
            'href':_href(room_id,seq=event.seq)})
    if extra_rows:
        synthetic=namedtuple('ReplyFailureRow','event total message_total')
        total=outcome_total+len(extra_rows);message_count=rows[0].message_total if rows else 0
        rows=[*rows,*(synthetic(row,total,message_count) for row in extra_rows)]
        outcome_total=total
        outcomes=sorted(outcomes,key=lambda item:(item['seq'],item['event_id']))[:ITEM_LIMIT]

    snapshot_names = {}
    for outcome in outcomes:
        snapshot_names.setdefault(outcome["agent_id"], outcome["agent_name_snapshot"])

    # Delegated colleagues can be outside the original @selected set. Their
    # local actions still belong to this exact user request and its safety view.
    execution_owned_ids = sorted(set(owned_ids) | {aid for aid, in db.query(Agent.id).filter(
        Agent.id.in_(team_ids), Agent.user_id == owner_id).all()}) if team_ids else owned_ids
    actions = []
    action_total = 0
    action_rows = []
    if owned_ids:
        occurred = func.coalesce(
            ToolAction.requested_at, ToolAction.started_at, ToolAction.completed_at, request.created_at,
        )
        total = func.count().over().label("total")
        open_count = func.sum(case((ToolAction.state.in_(OPEN_ACTION_STATES), 1), else_=0)).over().label("open_count")
        failed_count = func.sum(case((ToolAction.state.in_(FAILED_ACTION_STATES), 1), else_=0)).over().label("failed_count")
        waiting_count = func.sum(case((ToolAction.state == "waiting", 1), else_=0)).over().label("waiting_count")
        unknown_count = func.sum(case((not_(ToolAction.state.in_(ACTION_STATES)), 1), else_=0)).over().label("unknown_count")
        action_rows = db.query(ToolAction, total, occurred.label("occurred_at"), open_count,
                               failed_count, waiting_count, unknown_count).join(
            Agent, and_(Agent.id == ToolAction.agent_id, Agent.user_id == owner_id),
        ).join(action_room, action_room.id == ToolAction.room_id).filter(
            action_room.user_id == owner_id,
            or_(ToolAction.room_id == room_id,
                ToolAction.execution_binding['root_request_event_id'].as_string() == request_event_id),
            ToolAction.trigger_event_id == request_event_id,
            ToolAction.agent_id.in_(execution_owned_ids),
            _canonical_uuid_column(ToolAction.action_id),
        ).order_by(occurred.asc(), ToolAction.action_id.asc()).limit(ITEM_LIMIT + 1).all()
        action_total = action_rows[0].total if action_rows else 0

    action_counts = {
        "open": int(action_rows[0].open_count or 0) if action_rows else 0,
        "failed": int(action_rows[0].failed_count or 0) if action_rows else 0,
        "waiting": int(action_rows[0].waiting_count or 0) if action_rows else 0,
        "unknown": int(action_rows[0].unknown_count or 0) if action_rows else 0,
    }

    visible_actions = [row[0] for row in action_rows[:ITEM_LIMIT]]
    result_rows = []
    if visible_actions:
        action_ids = [action.action_id for action in visible_actions]
        result_action = RoomEvent.payload["action_id"].as_string()
        result_rows = db.query(
            result_action.label("action_id"), func.count(RoomEvent.event_id).label("total"),
            func.min(RoomEvent.event_id).label("event_id"), func.min(RoomEvent.seq).label("seq"),
        ).join(ToolAction, and_(ToolAction.action_id == result_action, ToolAction.room_id == RoomEvent.room_id)).filter(
            RoomEvent.room_id.in_({a.room_id for a in visible_actions}),
            RoomEvent.kind == "tool_result",
            RoomEvent.actor_type == "tool",
            RoomEvent.payload["trigger_event_id"].as_string() == request_event_id,
            result_action.in_(action_ids),
            _canonical_uuid_column(RoomEvent.event_id),
        ).group_by(result_action).limit(ITEM_LIMIT).all()
    result_by_action = {row.action_id: row for row in result_rows if row.total == 1}

    for action in visible_actions:
        state = action.state if action.state in ACTION_STATES else "unknown"
        result = result_by_action.get(action.action_id)
        if state == "waiting":
            href = _href(action.room_id, action=action.action_id)
        elif result is not None:
            href = _href(action.room_id, seq=result.seq)
        else:
            href = _href(action.room_id, seq=request.seq) if action.room_id == room_id else _href(action.room_id)
        actions.append({
            "action_id": action.action_id, "agent_id": action.agent_id,
            "state": state, "phase": ACTION_PHASES.get(state, "unknown"),
            "summary_code": _action_summary(action.family),
            "requested_at": action.requested_at, "started_at": action.started_at,
            "completed_at": action.completed_at,
            "result_event_id": result.event_id if result is not None else None,
            "result_seq": result.seq if result is not None else None,
            "href": href,
        })

    selected_agents = []
    for agent_id in owned_ids:
        run = owned_runs.get(agent_id)
        live_state = None
        if run is not None and run.trigger_event_id == request_event_id:
            live_state = "paused" if run.paused else run.state if run.state in RUN_STATES else "unknown"
        selected_agents.append({
            "agent_id": agent_id, "name_snapshot": snapshot_names.get(agent_id),
            "live_state": live_state,
        })

    result_available = bool(rows and rows[0].message_total) if owned_ids else False
    completion = _completion(
        trigger, policy, selected, owned_ids, int(raw_outcome_count or 0), outcome_total,
        rows if owned_ids else [], result_available, int(raw_action_count or 0), action_total,
        action_counts,
    )
    team_event_ids = {event_id for delivery in team_rows for event_id in
                      (delivery.result_event_id, delivery.return_event_id) if event_id}
    team_events = db.query(RoomEvent).join(Agent, Agent.id == RoomEvent.actor_id).filter(
        RoomEvent.room_id == room_id, RoomEvent.event_id.in_(team_event_ids),
        RoomEvent.actor_type == "agent", Agent.user_id == owner_id).order_by(RoomEvent.seq).all() if team_event_ids else []
    valid_team_events = []
    for event in team_events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        tag = payload.get("teammate_handoff")
        if (payload.get("trigger_event_id") != request_event_id or not isinstance(tag, dict)
                or tag.get("phase") not in {"response", "return"}
                or event.kind not in {"message", "failure", "activity"}):
            continue
        valid_team_events.append(event)
        kind = "passed" if event.kind == "activity" else event.kind
        from app.services.teammates import retry_result_incomplete
        team_incomplete = kind == "failure" and retry_result_incomplete(db, event, db.get(RoomEvent, request_event_id))
        outcomes.append({"event_id": event.event_id, "seq": event.seq,
            "round": payload.get("round", 1), "agent_id": event.actor_id,
            "agent_name_snapshot": _text(event.actor_name_snapshot, 200), "kind": kind,
            "phase": {"message": "succeeded", "failure": "failed", "passed": "passed"}[kind],
            "summary_code": "response" if kind == "message" else "passed" if kind == "passed" else "failure_teammate_result_incomplete" if team_incomplete else _failure_summary(payload),
            "model_use": _model_use(payload) if kind == "message" else None,
            "href": _href(room_id, seq=event.seq)})
    if team_rows:
        pending_team = sum(row.state in {"pending", "running"} or row.reply_state in {"pending", "running"} for row in team_rows)
        incomplete_team = any(row.state != "completed" or row.reply_state != "delivered" for row in team_rows)
        team_results = any(event.kind == "message" for event in valid_team_events)
        completion["result_available"] = completion["result_available"] or team_results
        completion["pending_agent_count"] = min(64, completion["pending_agent_count"] + pending_team)
        if len(valid_team_events) != len(team_event_ids):
            completion["outcome_status"] = "unknown"
        elif pending_team:
            completion["outcome_status"] = "pending" if completion["state"] in {"running", "queued", "waiting_approval"} else "unknown"
        elif incomplete_team and completion["outcome_status"] != "unknown":
            completion["outcome_status"] = "partial" if completion["result_available"] else "failed"
        outcome_total += len(valid_team_events)
        outcomes.sort(key=lambda item: item["seq"])
    from app.services import bot_messages
    cross_rows = [r for r in bot_messages.root_rows(db, request_event_id) if r.owner_id == owner_id and r.root_room_id == room_id]
    cross_event_ids = {eid for row in cross_rows for eid in (row.result_event_id, row.return_event_id) if eid}
    cross_events = db.query(RoomEvent).join(Conversation, Conversation.id == RoomEvent.room_id).join(
        Agent, Agent.id == RoomEvent.actor_id).filter(RoomEvent.event_id.in_(cross_event_ids),
        Conversation.user_id == owner_id, Agent.user_id == owner_id, RoomEvent.actor_type == "agent").order_by(RoomEvent.seq).all() if cross_event_ids else []
    for event in cross_events:
        payload = event.payload
        tag = payload.get("bot_message", {})
        row = next((r for r in cross_rows if r.message_id == tag.get("message_id")), None)
        phase = tag.get("phase")
        if (row is None or payload.get("trigger_event_id") != request_event_id
                or phase not in {"response", "return"}
                or event.room_id != (row.recipient_room_id if phase == "response" else row.sender_room_id)
                or event.actor_id != (row.recipient_agent_id if phase == "response" else row.sender_agent_id)):
            completion["outcome_status"] = "unknown"
            continue
        kind = "passed" if event.kind == "activity" else event.kind
        if kind not in {"message", "failure", "passed"}:
            completion["outcome_status"] = "unknown"
            continue
        outcomes.append({"event_id": event.event_id, "seq": event.seq, "round": payload.get("round", 1),
            "agent_id": event.actor_id, "agent_name_snapshot": _text(event.actor_name_snapshot, 200),
            "kind": kind, "phase": {"message":"succeeded", "failure":"failed", "passed":"passed"}[kind],
            "summary_code": "response" if kind == "message" else "passed" if kind == "passed" else _failure_summary(payload),
            "model_use": _model_use(payload) if kind == "message" else None, "href": _href(event.room_id, seq=event.seq)})
        completion["result_available"] |= kind == "message"
    if cross_rows:
        pending_cross = sum(r.state in {"pending", "running"} or r.reply_state in {"pending", "running"} for r in cross_rows)
        completion["pending_agent_count"] = min(64, completion["pending_agent_count"] + pending_cross)
        if len(cross_events) != len(cross_event_ids): completion["outcome_status"] = "unknown"
        elif pending_cross: completion["outcome_status"] = "pending" if completion["state"] in {"queued","running","waiting_approval"} else "unknown"
        elif any(r.state != "completed" or r.reply_state != "delivered" for r in cross_rows) and completion["outcome_status"] != "unknown":
            completion["outcome_status"] = "partial" if completion["result_available"] else "failed"
        outcome_total += len(cross_events)
    from app.services.bot_batches import root_rows as batch_root_rows
    batches=[b for b in batch_root_rows(db,request_event_id) if b.owner_id==owner_id and b.root_room_id==room_id]
    batch_ids={b.return_event_id for b in batches if b.return_event_id}
    batch_events=db.query(RoomEvent).join(Conversation,Conversation.id==RoomEvent.room_id).join(
        Agent,Agent.id==RoomEvent.actor_id).filter(RoomEvent.event_id.in_(batch_ids),Conversation.user_id==owner_id,
        Agent.user_id==owner_id,RoomEvent.actor_type=='agent').all() if batch_ids else []
    for event in batch_events:
        payload=event.payload;tag=payload.get('bot_batch',{})
        batch=next((b for b in batches if b.batch_id==tag.get('batch_id')),None)
        kind='passed' if event.kind=='activity' and payload.get('status')=='passed' else event.kind
        if (not batch or tag.get('phase')!='return' or payload.get('trigger_event_id')!=request_event_id
                or event.room_id!=batch.sender_room_id or event.actor_id!=batch.sender_agent_id
                or kind not in {'message','failure','passed'}):
            completion['outcome_status']='unknown';continue
        if event.event_id not in {o['event_id'] for o in outcomes}:
            outcomes.append({'event_id':event.event_id,'seq':event.seq,'round':payload.get('round',1),
                'agent_id':event.actor_id,'agent_name_snapshot':_text(event.actor_name_snapshot,200),'kind':kind,
                'phase':{'message':'succeeded','failure':'failed','passed':'passed'}[kind],
                'summary_code':'response' if kind=='message' else 'passed' if kind=='passed' else _failure_summary(payload),
                'model_use':_model_use(payload) if kind=='message' else None,'href':_href(event.room_id,seq=event.seq)})
            outcome_total+=1
        completion['result_available']|=kind=='message'
    if batches:
        remaining=sum(b.reply_state in {'none','pending','running'} for b in batches)
        completion['pending_agent_count']=min(64,completion['pending_agent_count']+remaining)
        if len(batch_events)!=len(batch_ids):completion['outcome_status']='unknown'
        elif remaining:completion['outcome_status']='pending' if completion['state'] in {'queued','running','waiting_approval'} else 'unknown'
        elif any(b.state!='completed' or b.reply_state!='delivered' for b in batches) and completion['outcome_status']!='unknown':
            completion['outcome_status']='partial' if completion['result_available'] else 'failed'
    outcomes.sort(key=lambda item:(item['seq'],item['event_id']))
    trigger_phase = completion["state"]
    current_round = trigger.current_round if (trigger is not None and policy is not None
                                               and type(trigger.current_round) is int
                                               and 0 <= trigger.current_round <= policy["max_rounds"]) else None
    if policy is None:
        trigger_phase = "unknown"
    # System progress is separate from canonical Agent outcomes and tool actions.
    checks = []
    check_rows = db.query(RoomEvent).filter(
        RoomEvent.room_id == room_id, RoomEvent.actor_type == 'system',
        RoomEvent.actor_id.is_(None), RoomEvent.kind == 'activity',
        RoomEvent.payload['category'].as_string() == 'collaboration_check',
        RoomEvent.payload['trigger_event_id'].as_string() == request_event_id,
    ).order_by(RoomEvent.seq.asc()).limit(1024).all()
    for event in check_rows:
        value = event.payload if isinstance(event.payload, dict) else {}
        state, kind = value.get('state'), value.get('kind')
        agent = value.get('agent_id')
        count, more = value.get('shown_count'), value.get('has_more')
        if (value.get('version') != 1 or type(agent) is not int or agent not in execution_owned_ids
                or kind not in ('teammates', 'bots') or state not in ('completed', 'failed')
                or not canonical_uuid(event.event_id) or event.seq < 1
                or (state == 'completed' and (type(count) is not int or not 0 <= count <= 10000 or type(more) is not bool))
                or (state == 'failed' and (count is not None or more is not None))):
            continue
        checks.append({'event_id':event.event_id, 'seq':event.seq, 'agent_id':agent,
                       'kind':kind, 'state':state, 'shown_count':count, 'has_more':more})

    return {
        "version": 1,
        "room_id": room.id,
        "room_title": _text(room.title, 200),
        "read_only": not (bool(room.is_active) and bool(project.is_active)),
        "request": {
            "event_id": request.event_id, "seq": request.seq,
            "created_at": request.created_at,
            "actor_name_snapshot": _text(request.actor_name_snapshot, 200),
            "preview": _preview(request.payload),
            "href": _href(room.id, seq=request.seq),
        },
        "execution": {
            "phase": trigger_phase, "selected_agents": selected_agents,
            "selected_agent_count": len(selected_agents), "policy": policy,
            "current_round": current_round,
        },
        "completion": completion,
        "outcomes": outcomes[:ITEM_LIMIT],
        "outcome_count": outcome_total,
        "outcomes_truncated": outcome_total > ITEM_LIMIT,
        "actions": actions,
        "action_count": action_total,
        "actions_truncated": action_total > ITEM_LIMIT,
        "collaboration_checks": checks[:128],
        "collaboration_check_count": len(checks),
        "collaboration_checks_truncated": len(checks) > 128 or len(check_rows) == 1024,
    }
