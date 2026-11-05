"""Owner-scoped, replay-safe finalization of one canonical request."""

from hashlib import sha256
import json
from urllib.parse import urlencode

from fastapi import HTTPException
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.models.collaboration import (
    ProviderConnection, RequestFinalizeRecord, RoomAgentRun, RoomEvent, RoomMembership,
    RoomTrigger, ToolAction,
)
from app.models.models import Agent, Conversation, Project, User
from app.services.context_projection import ProjectionError, project_finalize_context
from app.services.connections import ConnectionConfig, validated_connection_contract
from app.services.events import ActorRef, stage_event
from app.services.request_inspector import build_request_inspector, canonical_uuid
from app.services.memory_context import snapshot as memory_snapshot


FINALIZE_CONTENT = (
    "Create a final result from the linked request and peer responses. State the conclusion, "
    "what changed after peer review, remaining uncertainty, and the next action."
)

TERMINAL_TRIGGERS = {"done", "cancelled", "interrupted"}
UNSETTLED_ACTIONS = {"pending", "waiting", "approved"}
UNCERTAIN_ACTIONS = {"started", "uncertain"}
TERMINAL_ACTIONS = {"completed", "failed", "denied", "cancelled"}
READ_ONLY_FAMILIES = {"read_file", "screenshot", "browser_inspect"}
LIMITS = {"max_rounds": 1, "max_tool_calls": 0, "max_executor_seconds": 60}


def _hash(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode()).hexdigest()


def _event_dict(event: RoomEvent) -> dict:
    return {"event_id": event.event_id, "room_id": event.room_id, "seq": event.seq,
            "actor_type": event.actor_type, "actor_id": event.actor_id,
            "actor_name_snapshot": event.actor_name_snapshot, "kind": event.kind,
            "payload": dict(event.payload)}


def _blocked(base: dict, reason: str) -> dict:
    return {**base, "eligible": False, "blocked_reason": reason, "plan_hash": None}


def _not_found() -> None:
    raise HTTPException(404, {"code": "request_finalize_not_found"})


def _base(room_id: int) -> dict:
    return {
        "version": 1, "room_id": room_id, "source_request": None,
        "source_completion": None, "outcomes": [], "outcome_count": 0,
        "outcomes_truncated": False, "excluded_agents": [], "agent": None,
        "current_model": None, "limits": LIMITS, "source_coverage": None,
    }


def _canonical_model(agent: Agent | None, connection: ProviderConnection | None) -> bool:
    if not (agent is not None and connection is not None and agent.is_active and connection.enabled
            and isinstance(agent.model_id, str) and 1 <= len(agent.model_id) <= 200
            and agent.model_id.isprintable()
            and (not connection.auth_required or bool(connection.secret_ref))):
        return False
    try:
        validated_connection_contract(ConnectionConfig(
            connection.id, connection.kind, agent.model_id, connection.base_url, None,
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


def finalize_binding_digest(agent: Agent, connection: ProviderConnection) -> str:
    """Hash only the public execution settings accepted by finalize preview.

    The credential reference and value never enter this digest.  Presence and
    readiness revision are enough to invalidate an accepted paid action when
    credential/configuration state changes.
    """
    return _hash({
        "agent": [agent.id, agent.user_id, agent.name, bool(agent.is_active),
                  agent.connection_id, agent.model_id, _hash(agent.system_prompt or ""),
                  agent.temperature, agent.max_tokens],
        "connection": [connection.id, connection.user_id, connection.kind,
                       connection.base_url, bool(connection.enabled),
                       bool(connection.auth_required), bool(connection.secret_ref),
                       connection.readiness_revision, _hash(connection.capabilities or {}),
                       connection.provider_id, connection.protocol_id, connection.auth_method_id,
                       connection.public_config_version, _hash(connection.public_config or {}),
                       connection.auth_grant_revision, connection.credential_state],
    })


def build_finalize_preview(db: Session, owner_id: int, room_id: int,
                           source_request_event_id: str, agent_id: int,
                           *, include_execution_facts: bool = False) -> dict:
    base = _base(room_id)
    try:
        inspector = build_request_inspector(db, owner_id, room_id, source_request_event_id)
    except HTTPException as error:
        if error.status_code == 404:
            _not_found()
        raise
    base.update(
        source_request={
            "event_id": inspector["request"]["event_id"],
            "seq": inspector["request"]["seq"],
            "preview": inspector["request"]["preview"],
            "href": inspector["request"]["href"],
        },
        source_completion=inspector["completion"],
        outcomes=inspector["outcomes"][:256],
        outcome_count=inspector["outcome_count"],
        outcomes_truncated=inspector["outcome_count"] > 256,
    )
    room = db.get(Conversation, room_id)
    project = db.get(Project, room.project_id) if room else None
    source = db.get(RoomEvent, source_request_event_id)
    trigger = db.get(RoomTrigger, source_request_event_id)
    if room is None or project is None or source is None:
        _not_found()
    source_content = source.payload.get("content") if isinstance(source.payload, dict) else None
    if not isinstance(source_content, str) or not source_content.strip() or len(source_content) > 65536:
        return _blocked(base, "source_invalid")
    if not room.is_active or not project.is_active:
        return _blocked(base, "room_read_only")
    selected = trigger.selected_agents if trigger else None
    if (trigger is None or trigger.room_id != room_id or not isinstance(selected, list)
            or len(selected) > 64 or any(type(value) is not int or value <= 0 for value in selected)
            or len(set(selected)) != len(selected)):
        return _blocked(base, "source_invalid")
    if trigger.state not in TERMINAL_TRIGGERS:
        return _blocked(base, "source_not_terminal")
    completion = inspector["completion"]
    outcomes = inspector["outcomes"]
    if not outcomes and inspector["outcome_count"] == 0:
        return _blocked(base, "source_no_useful_result")
    if inspector["outcome_count"] > 256 or inspector["outcomes_truncated"]:
        return _blocked(base, "source_too_large")
    if completion["outcome_status"] == "unknown":
        return _blocked(base, "source_outcome_unknown")
    if completion["state"] not in {"completed", "cancelled", "interrupted"}:
        return _blocked(base, "source_not_terminal")
    messages = [item for item in outcomes if item["kind"] == "message"]
    failures = [item for item in outcomes if item["kind"] == "failure"]
    if not messages:
        return _blocked(base, "source_all_failed" if failures else "source_all_passed")

    action_count = inspector["action_count"]
    if action_count > 512 or inspector["actions_truncated"]:
        return _blocked(base, "source_too_large")
    from app.services.execution_scopes import root_actions
    actions = root_actions(db, owner_id, room_id, source_request_event_id)
    if len(actions) != action_count:
        return _blocked(base, "source_invalid")
    from app.services.bot_messages import root_rows as bot_root_rows
    action_agents = set(selected) | {aid for r in bot_root_rows(db, source_request_event_id) if r.owner_id == owner_id for aid in (r.sender_agent_id,r.recipient_agent_id)}
    action_projection = []
    for action in actions:
        if (not canonical_uuid(action.action_id) or action.agent_id not in action_agents
                or action.state not in TERMINAL_ACTIONS | UNSETTLED_ACTIONS | UNCERTAIN_ACTIONS
                or not isinstance(action.family, str) or not action.family):
            return _blocked(base, "source_invalid")
        action_projection.append([action.action_id, action.agent_id, action.family, action.state])
        if action.state in UNSETTLED_ACTIONS:
            return _blocked(base, "action_unsettled")
        if action.state in UNCERTAIN_ACTIONS:
            return _blocked(base, "action_uncertain")
        if action.state == "completed" and action.family not in READ_ONLY_FAMILIES:
            return _blocked(base, "completed_effect")
    agent = db.query(Agent).filter_by(id=agent_id, user_id=owner_id).first()
    if agent is None:
        _not_found()
    if agent_id not in selected:
        return _blocked(base, "agent_not_selected")
    membership = db.get(RoomMembership, (room_id, agent_id))
    run = db.get(RoomAgentRun, (room_id, agent_id))
    snapshot = next((item["agent_name_snapshot"] for item in outcomes
                     if item["agent_id"] == agent_id), agent.name if agent else "")
    if agent is not None:
        base["agent"] = {
            "agent_id": agent.id, "name_snapshot": str(snapshot)[:200],
            "current_name": str(agent.name)[:200],
        }
    if (agent is None or membership is None or not membership.is_active or run is None
            or run.paused or run.state in {"paused", "stopped"}
            or run.state not in {"idle", "running", "waiting"}):
        return _blocked(base, "agent_unavailable")
    if run.state != "idle":
        return _blocked(base, "agent_busy")
    connection = (db.query(ProviderConnection).filter_by(
        id=agent.connection_id, user_id=owner_id,
    ).first() if agent.connection_id else None)
    if not _canonical_model(agent, connection):
        return _blocked(base, "model_unavailable")
    base["current_model"] = {
        "connection_id": connection.id, "model_id": agent.model_id,
        "provider_kind": connection.kind,
    }
    other_open = db.query(RoomTrigger.event_id).filter(
        RoomTrigger.room_id == room_id,
        RoomTrigger.event_id != source_request_event_id,
        RoomTrigger.state.in_(("pending", "running")),
    ).first()
    if other_open is not None:
        return _blocked(base, "room_busy")

    source_rows = db.query(RoomEvent).join(Conversation, Conversation.id == RoomEvent.room_id).filter(
        Conversation.user_id == owner_id,
        RoomEvent.event_id.in_([item["event_id"] for item in outcomes]),
    ).order_by(RoomEvent.seq, RoomEvent.event_id).all()
    if len(source_rows) != len(outcomes):
        return _blocked(base, "source_invalid")
    if any(row.kind == "message" and (
            not isinstance(row.payload.get("content"), str)
            or not row.payload.get("content", "").strip()) for row in source_rows):
        return _blocked(base, "source_outcome_unknown")
    high_water = db.query(func.max(RoomEvent.seq)).filter_by(room_id=room_id).scalar() or 0
    owner = db.get(User, owner_id)
    preview_active = {
        "event_id": "00000000-0000-4000-8000-000000000000", "room_id": room_id,
        "seq": high_water + 1, "actor_type": "user", "actor_id": owner_id,
        "actor_name_snapshot": owner.username if owner else "Owner", "kind": "message",
        "payload": {"content": FINALIZE_CONTENT},
    }
    try:
        from app.services.context import _event_dict as safe_event_dict
        projection = project_finalize_context(
            source_request=safe_event_dict(source, db, owner_id), outcomes=[safe_event_dict(row, db, owner_id) for row in source_rows],
            active_event=preview_active, agent_id=agent_id, system=agent.system_prompt or "",
        )
    except ProjectionError:
        return _blocked(base, "context_too_large")
    base["source_coverage"] = projection.source_coverage
    excluded_by_agent = {}
    for item in outcomes:
        if item["kind"] not in {"failure", "passed"}:
            continue
        candidate = {"agent_id": item["agent_id"], "name_snapshot": item["agent_name_snapshot"],
                     "reason": "failed" if item["kind"] == "failure" else "passed"}
        prior = excluded_by_agent.get(item["agent_id"])
        if prior is None or candidate["reason"] == "failed":
            excluded_by_agent[item["agent_id"]] = candidate
    base["excluded_agents"] = list(excluded_by_agent.values())[:64]
    plan = {
        "memory_binding": memory_snapshot(db, room_id, agent.id)["binding"],
        "owner_id": owner_id, "room_id": room_id,
        "source_request": [source.event_id, source.seq, _hash(source.payload)],
        "source_trigger": [trigger.state, selected, trigger.max_rounds, trigger.max_tool_calls,
                           trigger.max_executor_seconds, trigger.current_round],
        "outcomes": [[row.event_id, row.seq, row.actor_id, row.kind, _hash(row.payload)]
                     for row in source_rows],
        "completion": completion, "actions": action_projection, "room_high_water": high_water,
        "membership": [membership.is_active, run.state, run.paused, run.generation],
        "agent": [agent.id, agent.name, agent.connection_id, agent.model_id,
                  _hash(agent.system_prompt or ""), agent.temperature, agent.max_tokens],
        "connection": [connection.id, connection.kind, connection.base_url,
                       connection.enabled, connection.auth_required, bool(connection.secret_ref),
                       connection.readiness_revision, _hash(connection.capabilities or {}),
                       connection.provider_id, connection.protocol_id, connection.auth_method_id,
                       connection.public_config_version, _hash(connection.public_config or {}),
                       connection.auth_grant_revision, connection.credential_state],
        "projection": [projection.fingerprint, projection.source_coverage],
    }
    result = {**base, "eligible": True, "blocked_reason": None, "plan_hash": _hash(plan)}
    if include_execution_facts:
        result["_execution_binding_digest"] = finalize_binding_digest(agent, connection)
        result["_projection_fingerprint"] = projection.fingerprint
    return result


def _result(record: RequestFinalizeRecord, replayed: bool) -> dict:
    return {
        "version": 1, "room_id": record.room_id,
        "source_request_event_id": record.source_request_event_id,
        "source_outcome_event_ids": list(record.source_outcome_event_ids),
        "finalize_event_id": record.finalize_event_id, "finalize_seq": record.finalize_seq,
        "agent_id": record.agent_id, "replayed": replayed,
        "href": f"/chat/{record.room_id}?{urlencode({'seq': record.finalize_seq})}",
    }


def execute_finalize(db: Session, owner_id: int, room_id: int, source_request_event_id: str,
                     agent_id: int, plan_hash: str, idempotency_key: str) -> dict:
    if (not idempotency_key or len(idempotency_key) > 200
            or any(not character.isprintable() for character in idempotency_key)):
        raise HTTPException(422, {"code": "request_finalize_idempotency_key_invalid"})
    digest = _hash({"room_id": room_id, "source_request_event_id": source_request_event_id,
                    "agent_id": agent_id})
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        prior = db.get(RequestFinalizeRecord, (owner_id, idempotency_key))
        if prior is not None:
            if prior.request_digest != digest:
                raise HTTPException(409, {"code": "finalize_idempotency_conflict"})
            db.commit()
            return _result(prior, True)
        preview = build_finalize_preview(
            db, owner_id, room_id, source_request_event_id, agent_id,
            include_execution_facts=True,
        )
        if not preview["eligible"]:
            raise HTTPException(409, {"code": "finalize_blocked",
                                      "reason": preview["blocked_reason"]})
        if preview["plan_hash"] != plan_hash:
            raise HTTPException(409, {"code": "finalize_plan_stale"})
        internal_key = "request-finalize:" + _hash([owner_id, idempotency_key])
        event = stage_event(db, room_id, ActorRef("user", owner_id), "message", {
            "content": FINALIZE_CONTENT,
            "finalize": {"version": 1, "source_request_event_id": source_request_event_id,
                         "source_outcome_event_ids": [item["event_id"] for item in preview["outcomes"]],
                         "agent_id": agent_id,
                         "source_status": {
                             "outcome_status": preview["source_completion"]["outcome_status"],
                             "unanswered_turn_count": sum(item["kind"] == "passed" for item in preview["outcomes"]),
                             "omitted_message_count": len(preview["source_coverage"]["omitted_event_ids"]),
                             "truncated_event_count": len(preview["source_coverage"]["truncations"]),
                         }},
        }, internal_key)
        db.add(RoomTrigger(
            event_id=event.event_id, room_id=room_id, state="pending",
            selected_agents=[agent_id], max_rounds=1, max_tool_calls=0,
            max_executor_seconds=60, current_round=0,
        ))
        record = RequestFinalizeRecord(
            owner_id=owner_id, idempotency_key=idempotency_key, request_digest=digest,
            plan_hash=plan_hash, room_id=room_id,
            source_request_event_id=source_request_event_id,
            source_outcome_event_ids=[item["event_id"] for item in preview["outcomes"]],
            finalize_event_id=event.event_id, finalize_seq=event.seq, agent_id=agent_id,
            execution_binding_digest=preview["_execution_binding_digest"],
            projection_fingerprint=preview["_projection_fingerprint"],
        )
        db.add(record)
        db.commit()
        return _result(record, False)
    except BaseException:
        db.rollback()
        raise
