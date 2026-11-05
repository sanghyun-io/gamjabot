"""Owner-scoped, audited, one-Agent retry of one canonical failed turn."""

from hashlib import sha256
import json
import re
from urllib.parse import urlencode

from fastapi import HTTPException
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.models.collaboration import (
    ProviderConnection, RequestRetryRecord, RoomAgentRun, RoomEvent, RoomMembership,
    RoomTrigger, ToolAction,
)
from app.models.models import Agent, Conversation, Project
from app.desktop_diagnostics import validated_detail
from app.services.connections import ConnectionConfig, validated_connection_contract
from app.services.events import ActorRef, stage_event
from app.services.diagnostic_detail import SAFE_FAILURE_CODES
from app.services.request_inspector import canonical_uuid
from app.services.memory_context import snapshot as memory_snapshot


TERMINAL_TRIGGERS = {"done", "cancelled", "interrupted"}
UNSETTLED_ACTIONS = {"pending", "waiting", "approved"}
UNCERTAIN_ACTIONS = {"started", "uncertain"}
TERMINAL_ACTIONS = {"completed", "failed", "denied", "cancelled"}
# Completed observations from these families do not mutate external state.
READ_ONLY_FAMILIES = {"read_file", "screenshot", "browser_inspect"}


def _preview(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()[:160]


def _hash(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode()).hexdigest()


def retry_admission_digest(db, owner_id, room_id, agent_id):
    """Bind the reviewed stable settings without persisting their contents."""
    agent = db.get(Agent, agent_id, populate_existing=True)
    if not agent or agent.user_id != owner_id or not agent.connection_id:
        raise ValueError('retry recipient unavailable')
    connection = db.get(ProviderConnection, agent.connection_id, populate_existing=True)
    if not connection or connection.user_id != owner_id:
        raise ValueError('retry connection unavailable')
    return _hash({'memory_binding': memory_snapshot(db, room_id, agent_id)['binding'],
        'agent': [agent.id, agent.name, agent.is_active, agent.connection_id, agent.model_id,
                  _hash(agent.system_prompt or ''), agent.temperature, agent.max_tokens],
        'connection': [connection.id, connection.kind, connection.base_url, connection.default_model_id,
                       connection.enabled, connection.auth_required, bool(connection.secret_ref),
                       connection.readiness_revision, _hash(connection.capabilities or {}),
                       connection.provider_id, connection.protocol_id, connection.auth_method_id,
                       connection.public_config_version, _hash(connection.public_config or {}),
                       connection.auth_grant_revision, connection.credential_state]})


def _blocked(base: dict, reason: str) -> dict:
    return {**base, "eligible": False, "blocked_reason": reason, "plan_hash": None}


def teammate_retry_effects_safe(db: Session, owner_id: int, room_id: int, root_id: str, *, delivery_activity_ids=frozenset()) -> bool:
    """Do not replay an old handoff if any effect may already have happened."""
    from app.services.execution_scopes import root_actions
    from app.models.bot_creation import BotCreation
    from app.models.owner_routines import OwnerRoutinePendingTest
    # These durable workflows can have effects without a ToolAction row.
    # A retry cannot silently repeat either an already-created Bot or a routine test.
    if (db.query(BotCreation.creation_id).filter_by(root_request_event_id=root_id).first()
            or db.query(OwnerRoutinePendingTest.request_id).filter_by(root_request_event_id=root_id).first()):
        return False
    actions = root_actions(db, owner_id, room_id, root_id)
    all_actions = db.query(ToolAction.action_id).filter_by(trigger_event_id=root_id).all()
    if {row[0] for row in all_actions} != {action.action_id for action in actions}:
        return False
    for action in actions:
        if (action.state in UNSETTLED_ACTIONS | UNCERTAIN_ACTIONS
                or action.state not in TERMINAL_ACTIONS
                or action.state == 'completed' and action.family not in READ_ONLY_FAMILIES
                or action.started_at is not None and action.state in {'failed', 'denied', 'cancelled'}):
            return False
    prior = db.query(RoomEvent).filter(
        RoomEvent.payload['trigger_event_id'].as_string() == root_id,
        RoomEvent.kind.in_(['tool_result', 'activity'])).all()
    read_actions = {action.action_id: action for action in actions
                    if action.state == 'completed' and action.family in READ_ONLY_FAMILIES}
    seen_read_receipts = set()
    for event in prior:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.kind == 'activity':
            if payload.get('category') not in {'teammate_handoff', 'collaboration_check'} and event.event_id not in delivery_activity_ids:
                return False
            continue
        action = read_actions.get(payload.get('action_id'))
        if (not action or event.actor_type != 'tool' or event.room_id != action.room_id
                or payload.get('agent_id') != action.agent_id or payload.get('state') != 'completed'
                or event.idempotency_key != 'tool-result:' + action.action_id):
            return False
        seen_read_receipts.add(action.action_id)
    return seen_read_receipts == set(read_actions)


def _not_found() -> None:
    raise HTTPException(404, {"code": "request_retry_not_found"})


def _legacy_profile_guard_diagnostic(event: RoomEvent, code: str, diagnostic: dict) -> bool:
    """Read old fixed-profile failures without rewriting their canonical events.

    Old diagnostics mapped this exact locally emitted code to unexpected_error.
    Admit only the full, content-free pre-transport ProviderFailure shape; other
    mismatches and every action/ownership/revision gate remain unchanged.
    """
    if (code != "unsupported_model_protocol"
            or event.payload.get("message") not in {
                "The exact model is not supported by this Coding Plan profile.",
                "This OpenCode Go model is not supported by the selected protocol.",
            }
            or diagnostic.get("failure_code") != "unexpected_error"
            or diagnostic.get("reason") != "unexpected_error"
            or diagnostic.get("stage") != "provider_request"
            or diagnostic.get("error_class") != "provider_failure"
            or diagnostic.get("exception_chain") != [{
                "module": "app.services.providers.base", "class": "ProviderFailure"}]
            or diagnostic.get("frames") != [] or diagnostic.get("action_id") is not None
            or "http_status" in diagnostic or "vendor_request_id" in diagnostic):
        return False
    return validated_detail(json.dumps(diagnostic), event.room_id, event.actor_id) is not None


def _canonical_failure(event: RoomEvent, room_id: int) -> tuple[str, int] | None:
    payload = event.payload if isinstance(event.payload, dict) else {}
    trigger_id, round_number = payload.get("trigger_event_id"), payload.get("round")
    code = payload.get("code")
    diagnostic = payload.get("diagnostic")
    if (event.room_id != room_id or event.actor_type != "agent" or event.kind != "failure"
            or type(event.actor_id) is not int or event.actor_id <= 0
            or not canonical_uuid(event.event_id) or not canonical_uuid(trigger_id)
            or type(round_number) is not int or not 1 <= round_number <= 4
            or not isinstance(diagnostic, dict) or diagnostic.get("version") != 1
            or not isinstance(code, str) or code not in SAFE_FAILURE_CODES
            or (diagnostic.get("failure_code") != code
                and not _legacy_profile_guard_diagnostic(event, code, diagnostic))
            or diagnostic.get("room_id") != room_id or diagnostic.get("agent_id") != event.actor_id
            or diagnostic.get("trigger_event_id") != trigger_id):
        return None
    return trigger_id, round_number


def _canonical_tool_review_change(db: Session, event: RoomEvent, room_id: int, owner_id: int):
    """Accept only the exact durable, unstarted denied action and its stored review."""
    payload = event.payload if isinstance(event.payload, dict) else {}
    action_id, trigger_id, review_id = (payload.get(k) for k in ('action_id', 'trigger_event_id', 'review_id'))
    agent_id, call_id = payload.get('agent_id'), payload.get('call_id')
    if (event.room_id != room_id or event.kind != 'tool_result' or event.actor_type != 'tool'
            or event.actor_id is not None or not canonical_uuid(event.event_id)
            or not all(canonical_uuid(v) for v in (action_id, trigger_id, review_id))
            or type(agent_id) is not int or agent_id <= 0 or not isinstance(call_id, str) or not call_id
            or payload.get('state') != 'denied' or payload.get('error_code') != 'review_changed'
            or event.idempotency_key != f'tool-result:{action_id}'):
        return None
    action = db.get(ToolAction, action_id)
    if (action is None or action.room_id != room_id or action.agent_id != agent_id
            or action.trigger_event_id != trigger_id or action.call_id != call_id
            or action.review_id != review_id or action.state != 'denied' or action.started_at is not None
            or action.completed_at is None or action.approved_at is None or action.approval_owner_id != owner_id
            or not isinstance(action.observation, dict)
            or payload.get('screenshot_ref') is not None or payload.get('generated_file') is not None
            or any(payload.get(k) != value for k, value in action.observation.items())):
        return None
    try:
        from app.services.resource_binding import freeze_binding
        from app.services.review_snapshot import ReviewSnapshot
        binding = freeze_binding(action.execution_binding)
        if binding is not None:
            proof = binding.as_dict()
            if (proof['context_mode'] != 'user_room' or proof['root_request_event_id'] != trigger_id
                    or proof['root_room_id'] != room_id or proof['execution_room_id'] != room_id
                    or proof['agent_id'] != agent_id or proof['owner_id'] != owner_id
                    or proof['job_id'] is not None):
                return None
        review = ReviewSnapshot.model_validate_json(json.dumps(action.review_snapshot))
        saved = review.action
        for key in ('action_id', 'room_id', 'agent_id', 'trigger_event_id', 'call_id', 'workflow_id',
                    'session_id', 'family', 'target', 'scope_target', 'expected_effect', 'payload_digest'):
            if getattr(saved, key) != getattr(action, key):
                return None
        if review.authority.owner_id != owner_id or freeze_binding(saved.execution_binding) != binding:
            return None
    except (ValueError, TypeError, AttributeError, KeyError):
        return None
    return trigger_id, agent_id


def tool_retry_requires_once(db: Session, action: ToolAction) -> bool:
    """Revalidate server ledger provenance at preview, decision and action claim."""
    from app.services.action_review import ReviewChanged
    record = db.query(RequestRetryRecord).filter_by(retry_event_id=action.trigger_event_id).one_or_none()
    if record is None:
        candidate = db.get(RoomEvent, action.trigger_event_id) if action.trigger_event_id else None
        candidate_payload = candidate.payload if candidate and isinstance(candidate.payload, dict) else {}
        claimed_source = db.get(RoomEvent, candidate_payload.get('retry_of_failure_event_id')) if canonical_uuid(candidate_payload.get('retry_of_failure_event_id')) else None
        if claimed_source is not None and claimed_source.kind == 'tool_result':
            raise ReviewChanged('Tool retry ledger unavailable')
        return False
    source = db.get(RoomEvent, record.source_failure_event_id)
    if source is not None and source.kind == 'failure':
        return False  # Existing Agent-failure retries retain their previous contract.
    root = db.get(RoomEvent, record.retry_event_id)
    trigger = db.get(RoomTrigger, record.retry_event_id)
    original = db.get(RoomEvent, record.source_request_event_id)
    canonical = _canonical_tool_review_change(db, source, record.room_id, record.owner_id) if source else None
    payload = root.payload if root and isinstance(root.payload, dict) else {}
    original_payload = original.payload if original and isinstance(original.payload, dict) else {}
    if (canonical != (record.source_trigger_event_id, record.agent_id)
            or record.source_request_event_id != record.source_trigger_event_id
            or root is None or root.room_id != record.room_id or root.seq != record.retry_seq
            or root.kind != 'message' or root.actor_type != 'user' or root.actor_id != record.owner_id
            or root.idempotency_key != 'request-retry:' + _hash([record.owner_id, record.idempotency_key])
            or trigger is None or trigger.room_id != record.room_id or trigger.selected_agents != [record.agent_id]
            or trigger.max_rounds != 1
            or set(payload) - {'content', 'legacy_message_id', 'output_kind', 'reply_to',
                               'retry_of_failure_event_id', 'retry_of_trigger_event_id', 'retry_agent_id'}
            or original is None or original.room_id != record.room_id or original.kind != 'message'
            or original.actor_type != 'user' or original.actor_id != record.owner_id
            or set(original_payload) - {'content', 'legacy_message_id', 'output_kind', 'reply_to'}
            or original_payload.get('output_kind', 'auto') not in ('auto', 'text')
            or original.idempotency_key and original.idempotency_key.startswith('file-question:')
            or payload.get('content') != original_payload.get('content')
            or payload.get('output_kind', 'auto') != original_payload.get('output_kind', 'auto')
            or payload.get('reply_to') != original_payload.get('reply_to')
            or payload.get('retry_of_failure_event_id') != source.event_id
            or payload.get('retry_of_trigger_event_id') != original.event_id
            or payload.get('retry_agent_id') != record.agent_id
            or action.room_id != record.room_id or action.agent_id != record.agent_id
            or action.action_id == source.payload.get('action_id')
            or action.review_id == source.payload.get('review_id')
            or record.request_digest != _hash({'room_id': record.room_id,
                'failure_event_id': record.source_failure_event_id, 'plan_hash': record.plan_hash})):
        raise ReviewChanged('Tool retry provenance changed')
    from app.models.teammate import TeammateHandoff
    from app.models.bot_message import BotMessage
    from app.models.bot_batch import BotBatch
    if any(db.query(model).filter_by(root_request_event_id=original.event_id).first()
           for model in (TeammateHandoff, BotMessage, BotBatch)):
        raise ReviewChanged('Tool retry source scope changed')
    return True


def _valid_connection_contract(connection: ProviderConnection, model_id: str) -> bool:
    try:
        validated_connection_contract(ConnectionConfig(
            connection.id, connection.kind, model_id, connection.base_url, None,
            dict(connection.capabilities or {}), provider_id=connection.provider_id,
            protocol_id=connection.protocol_id, auth_method_id=connection.auth_method_id,
            public_config_version=connection.public_config_version,
            public_fields=dict(connection.public_config or {}) if connection.public_config is not None else None,
            auth_grant_revision=connection.auth_grant_revision,
            credential_state=connection.credential_state,
        ), auth_required=bool(connection.auth_required),
            has_secret=bool(connection.secret_ref))
        return True
    except Exception:
        return False


def build_retry_preflight(db: Session, owner_id: int, room_id: int,
                          failure_event_id: str) -> dict:
    row = db.query(RoomEvent, Conversation, Project).join(
        Conversation, Conversation.id == RoomEvent.room_id,
    ).join(Project, Project.id == Conversation.project_id).filter(
        RoomEvent.event_id == failure_event_id,
        RoomEvent.room_id == room_id,
        Conversation.user_id == owner_id,
        Project.user_id == owner_id,
    ).first()
    if row is None:
        _not_found()
    failure, room, project = row
    base = {
        "version": 1, "room_id": room_id, "failure_event_id": failure_event_id,
        "failure_seq": failure.seq, "source_request": None, "agent": None,
        "current_model": None, "limits": None,
    }
    tool_source = failure.kind == 'tool_result'
    # Only an exact snapshotted return can be rehydrated in a new scope.
    from app.services.teammates import retry_result_incomplete, retry_snapshot
    from app.services.llm_service import LLMFailure
    source_id = failure.payload.get("trigger_event_id") if isinstance(failure.payload, dict) else None
    source_request = db.get(RoomEvent, source_id) if isinstance(source_id, str) else None
    retry_context = None
    if source_request and isinstance(source_request.payload, dict) and source_request.payload.get('teammate_retry_context'):
        from app.services.retry_lineage import next_context
        try:
            retry_context = next_context(db, failure, source_request, owner_id)
        except LLMFailure:
            return _blocked(base, 'teammate_result_incomplete')
    elif source_request and retry_result_incomplete(db, failure, source_request):
        if failure.payload.get('code') == 'teammate_result_changed':
            return _blocked(base, 'teammate_result_incomplete')
        try:
            retry_context = retry_snapshot(db, failure, source_request, owner_id)
        except LLMFailure:
            return _blocked(base, "teammate_result_incomplete")
        if db.query(RequestRetryRecord).filter_by(owner_id=owner_id, room_id=room_id,
                source_failure_event_id=failure_event_id).first():
            return _blocked(base, "failure_superseded")
    from app.services.events import canonical_reply_scope_failure
    system_reply = canonical_reply_scope_failure(db,failure,owner_id)
    canonical = (_canonical_tool_review_change(db, failure, room_id, owner_id)
                 if tool_source else (system_reply[0].event_id,1) if system_reply else _canonical_failure(failure, room_id))
    if canonical is None and retry_context and failure.payload.get('code') in {'teammate_result_incomplete', 'teammate_result_changed'}:
        diagnostic = failure.payload.get('diagnostic')
        if (failure.actor_type == 'agent' and failure.kind == 'failure'
                and canonical_uuid(failure.event_id) and canonical_uuid(source_request.event_id)
                and isinstance(diagnostic, dict) and diagnostic.get('trigger_event_id') == source_request.event_id
                and diagnostic.get('failure_code') == 'unexpected_error'
                and diagnostic.get('reason') == 'unexpected_error' and diagnostic.get('error_class') == 'llm_failure'
                and type(failure.payload.get('round')) is int and 1 <= failure.payload['round'] <= 4
                and validated_detail(json.dumps(diagnostic), room_id, failure.actor_id) is not None):
            canonical = (source_request.event_id, failure.payload['round'])
    if canonical is None:
        return _blocked(base, "source_invalid")
    trigger_id = canonical[0]
    agent_id = canonical[1] if tool_source else system_reply[1] if system_reply else failure.actor_id
    request = db.get(RoomEvent, trigger_id)
    trigger = db.get(RoomTrigger, trigger_id)
    content = request.payload.get("content") if request and isinstance(request.payload, dict) else None
    selected = trigger.selected_agents if trigger is not None else None
    from app.services.teammates import linked_failure
    delegated_failure = not tool_source and not system_reply and linked_failure(db, room_id, trigger_id, failure)
    if delegated_failure:
        from app.services.teammates import retry_content, retry_result_incomplete
        if retry_result_incomplete(db, failure, request) and retry_context is None:
            return _blocked(base, "teammate_result_incomplete")
        if retry_context is None:
            content = retry_content(db, failure, request)
        elif retry_context.get('version') == 1 and (set(request.payload) - {'content', 'output_kind', 'legacy_message_id'} or request.payload.get('output_kind', 'auto') not in ('auto', 'text')):
            return _blocked(base, 'context_unavailable')
    policy_valid = bool(
        trigger is not None
        and type(trigger.max_tool_calls) is int and 0 <= trigger.max_tool_calls <= 64
        and type(trigger.max_executor_seconds) is int and 1 <= trigger.max_executor_seconds <= 3600
        and type(trigger.max_rounds) is int and 1 <= trigger.max_rounds <= 4
    )
    if (request is None or request.room_id != room_id or request.actor_type != "user"
            or request.actor_id != owner_id or request.kind != "message"
            or not canonical_uuid(request.event_id) or not isinstance(content, str)
            or not content.strip() or len(content) > 65536
            or not isinstance(selected, list) or len(selected) > 64
            or any(type(item) is not int or item <= 0 for item in selected)
            or len(set(selected)) != len(selected) or (selected.count(agent_id) != 1 and not delegated_failure)
            or trigger.room_id != room_id or not policy_valid
            or (tool_source and failure.seq <= request.seq)):
        return _blocked(base, "source_invalid")
    if tool_source:
        payload = request.payload
        # Retry only plain-text roots whose full context is carried by this event.
        if (set(payload) - {'content', 'legacy_message_id', 'output_kind', 'reply_to'}
                or payload.get('output_kind', 'auto') not in ('auto', 'text')
                or request.idempotency_key and request.idempotency_key.startswith('file-question:')):
            return _blocked(base, 'context_unavailable')
        from app.models.teammate import TeammateHandoff
        from app.models.bot_message import BotMessage
        from app.models.bot_batch import BotBatch
        if any(db.query(model).filter_by(root_request_event_id=trigger_id).first()
               for model in (TeammateHandoff, BotMessage, BotBatch)):
            return _blocked(base, 'context_unavailable')
    base["source_request"] = {
        "event_id": request.event_id, "seq": request.seq, "preview": _preview(content),
        "href": f"/chat/{room_id}?{urlencode({'seq': request.seq})}",
    }
    agent = db.query(Agent).filter_by(id=agent_id, user_id=owner_id).first()
    if agent is not None:
        base["agent"] = {
            "agent_id": agent.id, "name_snapshot": str(agent.name if tool_source else system_reply[3].actor_name_snapshot if system_reply else failure.actor_name_snapshot)[:200],
            "current_name": str(agent.name)[:200],
        }
    base["limits"] = {
        "max_rounds": 1, "max_tool_calls": trigger.max_tool_calls,
        "max_executor_seconds": trigger.max_executor_seconds,
    }
    if not room.is_active or not project.is_active:
        return _blocked(base, "room_read_only")
    if trigger.state not in TERMINAL_TRIGGERS:
        return _blocked(base, "source_not_terminal")
    later = None if tool_source else db.query(RoomEvent.event_id).filter(
        RoomEvent.room_id == room_id, RoomEvent.seq > failure.seq,
        RoomEvent.actor_type == "agent", RoomEvent.actor_id == agent_id,
        RoomEvent.kind.in_(("message", "failure", "activity")),
        RoomEvent.payload["trigger_event_id"].as_string() == trigger_id,
    ).first()
    if later is not None:
        return _blocked(base, "failure_superseded")
    other_open = db.query(RoomTrigger.event_id).filter(
        RoomTrigger.room_id == room_id, RoomTrigger.event_id != trigger_id,
        RoomTrigger.state.in_(("pending", "running")),
    ).first()
    if other_open is not None:
        return _blocked(base, "room_busy")
    member = db.get(RoomMembership, (room_id, agent_id))
    run = db.get(RoomAgentRun, (room_id, agent_id))
    if agent is None or not agent.is_active or member is None or not member.is_active or run is None or run.paused:
        return _blocked(base, "agent_unavailable")
    if run.state in {"stopped", "paused"} or run.state not in {"idle", "running", "waiting"}:
        return _blocked(base, "agent_unavailable")
    if run.state != "idle":
        return _blocked(base, "agent_busy")
    connection = db.query(ProviderConnection).filter_by(
        id=agent.connection_id, user_id=owner_id, enabled=True,
    ).first() if agent.connection_id else None
    if (connection is None or not isinstance(agent.model_id, str)
            or not 1 <= len(agent.model_id) <= 200 or not agent.model_id.isprintable()
            or not _valid_connection_contract(connection, agent.model_id)
            or connection.auth_required and not connection.secret_ref):
        return _blocked(base, "model_unavailable")
    base["current_model"] = {
        "connection_id": connection.id, "model_id": agent.model_id,
        "provider_kind": str(connection.kind)[:40],
    }
    from app.services.execution_scopes import root_actions
    actions = root_actions(db, owner_id, room_id, trigger_id)
    if tool_source or retry_context:
        # The normal owner-scoped projection must not silently omit root actions.
        all_actions = db.query(ToolAction.action_id).filter_by(trigger_event_id=trigger_id).all()
        if {row[0] for row in all_actions} != {action.action_id for action in actions}:
            return _blocked(base, 'source_invalid')
    from app.services.teammates import root_rows
    allowed_action_agents = set(selected) | {aid for handoff in root_rows(db, room_id, trigger_id)
        for aid in (handoff.sender_agent_id, handoff.recipient_agent_id)}
    from app.services.bot_messages import root_rows as bot_root_rows
    allowed_action_agents |= {aid for r in bot_root_rows(db, trigger_id) if r.owner_id == owner_id for aid in (r.sender_agent_id,r.recipient_agent_id)}
    action_projection = []
    for action in actions:
        if (not canonical_uuid(action.action_id) or action.agent_id not in allowed_action_agents
                or action.state not in TERMINAL_ACTIONS | UNSETTLED_ACTIONS | UNCERTAIN_ACTIONS
                or not isinstance(action.family, str) or not action.family):
            return _blocked(base, "source_invalid")
        action_projection.append([action.action_id, action.agent_id, action.family, action.state])
        if action.state in UNSETTLED_ACTIONS:
            return _blocked(base, "action_unsettled")
        if action.state in UNCERTAIN_ACTIONS:
            return _blocked(base, "action_uncertain")
        if (tool_source or retry_context) and action.started_at is not None and action.state in {'failed', 'denied', 'cancelled'}:
            return _blocked(base, "action_uncertain")
        if action.state == "completed" and action.family not in READ_ONLY_FAMILIES:
            return _blocked(base, "completed_effect")

    if retry_context:
        delivery_ids = frozenset()
        if retry_context.get('version') == 2:
            from app.services.teammate_delivery_evidence import safe_activity_ids_for_root
            try:
                delivery_ids = safe_activity_ids_for_root(db, owner_id, room_id, trigger_id, failure)
            except LLMFailure:
                return _blocked(base, 'teammate_result_incomplete')
        if not teammate_retry_effects_safe(db, owner_id, room_id, trigger_id, delivery_activity_ids=delivery_ids):
            return _blocked(base, 'completed_effect')

    high_water = db.query(func.max(RoomEvent.seq)).filter_by(room_id=room_id).scalar() or 0
    memory_state = memory_snapshot(db, room_id, agent.id)
    if request.seq <= memory_state["binding"]["context_after_seq"]:
        return _blocked(base, "memory_context_changed")
    reply_ref=request.payload.get('reply_to')
    if reply_ref is not None:
        # A delegated retry cannot acquire a personal transcript reference merely
        # because the original Owner used it in a different execution scope.
        if delegated_failure:return _blocked(base,'context_unavailable')
        try:
            from app.services.events import reply_target
            source=reply_target(db,room_id,owner_id,reply_ref,before_seq=request.seq)
        except HTTPException:return _blocked(base,'context_unavailable')
        retired={item['event_id'] for item in memory_state['binding'].get('retired_persistence_responses',[])}
        if source.seq<=memory_state['binding']['context_after_seq'] or source.event_id in retired:
            return _blocked(base,'memory_context_changed')
        from app.services.context import _event_dict
        if _event_dict(source,db,owner_id,request.event_id).get('reply_source_redacted'):
            return _blocked(base,'context_unavailable')
    plan = {
        "memory_binding": memory_state["binding"],
        "owner_id": owner_id, "room_id": room_id, "failure_event_id": failure.event_id,
        "failure_seq": failure.seq, "request_event_id": request.event_id,
        "request_seq": request.seq, "content_digest": _hash(content),
        "failure_agent_id": agent_id, "failure_actor_name": failure.actor_name_snapshot,
        "selected_agents": selected, "trigger_state": trigger.state,
        "source_policy": [trigger.max_rounds, trigger.max_tool_calls, trigger.max_executor_seconds],
        "room_high_water": high_water,
        "membership": [member.is_active, run.state, run.paused, run.generation,
                       _hash(run.memory or {}), _hash(run.checkpoint or {})],
        "agent": [agent.id, agent.name, agent.is_active, agent.connection_id, agent.model_id,
                  _hash(agent.system_prompt or ""), agent.temperature, agent.max_tokens],
        "connection": [connection.id, connection.kind, connection.base_url, connection.default_model_id,
                       connection.enabled, connection.auth_required, bool(connection.secret_ref),
                       connection.readiness_revision, _hash(connection.capabilities or {}),
                       connection.provider_id, connection.protocol_id, connection.auth_method_id,
                       connection.public_config_version, _hash(connection.public_config or {}),
                       connection.auth_grant_revision, connection.credential_state],
        "actions": action_projection,
    }
    if retry_context is not None:
        retry_context['admission_digest'] = retry_admission_digest(db, owner_id, room_id, agent.id)
        plan['teammate_retry_context'] = retry_context
        base['teammate_recovery'] = {'version': retry_context['version'], 'direct_count': 1,
            'nested_count': len(retry_context['sources']) - 1,
            'total_chars': sum(item['length'] for item in retry_context['sources']),
            'single_attempt': retry_context['version'] == 1}
        if retry_context['version'] == 2:
            base['teammate_recovery']['attempt_number'] = len(retry_context['chain']) + 1
    if tool_source:
        plan['tool_source'] = {'payload': _hash(failure.payload), 'request_payload': _hash(request.payload),
                               'review_id': failure.payload['review_id']}
    if reply_ref is not None:plan['reply_source']={'ref':reply_ref,'payload':_hash(source.payload)}
    return {**base, "eligible": True, "blocked_reason": None, "plan_hash": _hash(plan)}


def _result(record: RequestRetryRecord, replayed: bool) -> dict:
    return {
        "version": 1, "room_id": record.room_id,
        "source_failure_event_id": record.source_failure_event_id,
        "source_trigger_event_id": record.source_trigger_event_id,
        "source_request_event_id": record.source_request_event_id,
        "retry_event_id": record.retry_event_id, "retry_seq": record.retry_seq,
        "agent_id": record.agent_id, "replayed": replayed,
        "href": f"/chat/{record.room_id}?{urlencode({'seq': record.retry_seq})}",
    }


def execute_retry(db: Session, owner_id: int, room_id: int, failure_event_id: str,
                  plan_hash: str, idempotency_key: str) -> dict:
    if (not idempotency_key or len(idempotency_key) > 200
            or any(not char.isprintable() for char in idempotency_key)):
        raise HTTPException(422, {"code": "request_retry_idempotency_key_invalid"})
    digest = _hash({"room_id": room_id, "failure_event_id": failure_event_id, "plan_hash": plan_hash})
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        prior = db.get(RequestRetryRecord, (owner_id, idempotency_key))
        if prior is not None:
            if prior.request_digest != digest:
                raise HTTPException(409, {"code": "request_retry_idempotency_conflict"})
            db.commit()
            return _result(prior, True)
        preview = build_retry_preflight(db, owner_id, room_id, failure_event_id)
        if not preview["eligible"]:
            raise HTTPException(409, {
                "code": "request_retry_blocked", "reason": preview["blocked_reason"],
            })
        if preview["plan_hash"] != plan_hash:
            raise HTTPException(409, {"code": "request_retry_plan_stale"})
        request = db.get(RoomEvent, preview["source_request"]["event_id"])
        internal_key = "request-retry:" + _hash([owner_id, idempotency_key])
        from app.services.teammates import retry_content
        failure = db.get(RoomEvent, failure_event_id)
        from app.services.teammates import retry_result_incomplete, retry_snapshot
        if request.payload.get('teammate_retry_context'):
            from app.services.retry_lineage import next_context
            resume_context = next_context(db, failure, request, owner_id)
        else:
            resume_context = retry_snapshot(db, failure, request, owner_id) if retry_result_incomplete(db, failure, request) else None
        if resume_context is not None:
            resume_context['admission_digest'] = retry_admission_digest(db, owner_id, room_id, preview['agent']['agent_id'])
        retry_payload = {
            "content": request.payload['content'] if failure.kind == 'tool_result' or resume_context else retry_content(db, failure, request),
            "retry_of_failure_event_id": failure_event_id,
            "retry_of_trigger_event_id": request.event_id,
            "retry_agent_id": preview["agent"]["agent_id"],
        }
        if resume_context is not None:
            retry_payload['teammate_retry_context'] = resume_context
        if failure.kind == 'tool_result' and request.payload.get('output_kind') == 'text':
            retry_payload['output_kind'] = 'text'
        if request.payload.get('reply_to') is not None:retry_payload['reply_to']=request.payload['reply_to']
        retry = stage_event(db, room_id, ActorRef("user", owner_id), "message", retry_payload, internal_key)
        limits = preview["limits"]
        db.add(RoomTrigger(
            event_id=retry.event_id, room_id=room_id, state="pending",
            selected_agents=[preview["agent"]["agent_id"]], max_rounds=1,
            max_tool_calls=limits["max_tool_calls"],
            max_executor_seconds=limits["max_executor_seconds"], current_round=0,
        ))
        record = RequestRetryRecord(
            owner_id=owner_id, idempotency_key=idempotency_key, request_digest=digest,
            plan_hash=plan_hash, room_id=room_id, source_failure_event_id=failure_event_id,
            source_trigger_event_id=request.event_id, source_request_event_id=request.event_id,
            retry_event_id=retry.event_id, retry_seq=retry.seq,
            agent_id=preview["agent"]["agent_id"],
        )
        db.add(record)
        db.commit()
        return _result(record, False)
    except BaseException:
        db.rollback()
        raise
