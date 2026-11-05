"""Owner consent decisions and exact, durable grant matching."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import update, text as sql_text

from app.models.collaboration import BroadMode, RoomAgentRun, RoomGrant, ToolAction
from app.models.models import Conversation
from app.services.action_review import ReviewChanged
from app.services.events import ActorRef, stage_event
from app.services.review_execution import (canonical_review_id, grant_review_matches, approval_time,
                                            grant_lifetime_valid, session_review_supported)

if TYPE_CHECKING:
    from app.services.review_snapshot import ReviewSnapshot

OPAQUE_ACTIONS = frozenset({"request_vault_login", "mcp_tool_call", "browser_interact", "pointer_click", "keyboard_text", "keyboard_chord"})


class ReviewRequired(Exception):
    """An unstarted opaque approval expired; commit its explicit re-review."""


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class GrantRef:
    grant_id: int | None = None
    scope: str | None = None
    room_id: int | None = None
    agent_id: int | None = None
    session_id: str | None = None
    families: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    expires_at: datetime | None = None
    review_id: str | None = None
    review: "ReviewSnapshot | None" = None
    approval_review_id: str | None = None
    broad_revision: datetime | None = None
    approved_at: datetime | None = None
    run_generation: int | None = None
    execution_binding: dict | None = None


def _fresh(expiry):
    return expiry is not None and expiry > now()


def broad_state(db, owner_id: int, session_id: str) -> dict:
    mode = db.get(BroadMode, owner_id)
    active = bool(mode and mode.enabled and (
        (mode.session_id is None and mode.expires_at is None)
        or (mode.session_id == session_id and _fresh(mode.expires_at))))
    return {"enabled": active, "scope": "broad" if active else None,
            "expires_at": mode.expires_at.isoformat() + "Z" if active and mode.expires_at else None}


def set_broad(db, owner_id: int, session_id: str, enabled: bool, expires_minutes: int | None,
              always: bool = False) -> dict:
    if enabled and (always == (expires_minutes is not None)
                    or (expires_minutes is not None and (not isinstance(expires_minutes, int)
                        or isinstance(expires_minutes, bool) or not 1 <= expires_minutes <= 60))):
        raise HTTPException(422, "Broad mode requires either always or a 1–60 minute expiry")
    if not enabled and always:
        raise HTTPException(422, "Disable broad mode without an enable option")
    mode = db.get(BroadMode, owner_id)
    if mode is None:
        mode = BroadMode(owner_id=owner_id)
        db.add(mode)
    mode.enabled = enabled
    mode.session_id = session_id if enabled and not always else None
    mode.expires_at = now() + timedelta(minutes=expires_minutes) if enabled and not always else None
    mode.updated_at = now()
    db.commit()
    return broad_state(db, owner_id, session_id)


def decide(db, action_id: str, room_id: int, owner_id: int, decision: str,
           scope: str | None, session_id: str, *, review_id: str | None = None,
           acknowledge_unverified_context: bool = False, send_image_to_model: bool = False, broker=None) -> dict:
    """Commit the inspected review, grant, action and audit as one decision.

    The broker is a server-owned object, never a request-supplied verifier.
    This decision does not replace the later claim/effect preconditions.
    """
    if decision not in ("approve", "deny") or (decision == "approve" and scope not in ("once", "room_session")):
        raise HTTPException(422, "Invalid consent decision")
    if db.new or db.dirty or db.deleted:
        raise HTTPException(409, "Decision requires a clean transaction")
    try:
        if db.bind.dialect.name == "sqlite":
            db.execute(sql_text("BEGIN IMMEDIATE"))
        db.expire_all()  # Request/auth lookups must not hide another session's change.
        room = db.query(Conversation).filter_by(id=room_id, user_id=owner_id, is_active=True).first()
        action = db.get(ToolAction, action_id, populate_existing=True)
        if room is None or action is None or action.room_id != room_id:
            raise HTTPException(404, "Action not found")
        if action.state != "waiting" or action.session_id != session_id:
            raise HTTPException(409, "Action is no longer awaiting approval")
        if type(send_image_to_model) is not bool or (send_image_to_model and (decision != "approve" or action.family not in {"screenshot", "browser_screenshot"})):
            raise HTTPException(422, "Image disclosure requires approval of a screenshot")
        if decision == "deny":
            count = db.execute(update(ToolAction).where(ToolAction.action_id == action_id,
                ToolAction.state == "waiting", ToolAction.session_id == session_id).values(state="denied")
                .execution_options(synchronize_session=False)).rowcount
            if count != 1:
                raise HTTPException(409, "Action is no longer awaiting approval")
            stage_event(db, room_id, ActorRef("user", owner_id), "activity",
                {"action_id": action_id, "status": "denied", "scope": None}, f"consent-deny:{action_id}")
            db.commit()
            return {"action_id": action_id, "state": "denied"}
        if broker is None or broker.session_id != session_id:
            raise ReviewChanged("Trusted review verifier unavailable")
        snapshot = broker.require_review(db, action, owner_id=owner_id, review_id=review_id)
        delegated = bool(action.execution_binding and action.execution_binding.get('context_mode') == 'delegated')
        from app.services.request_retry import tool_retry_requires_once
        retry_once = tool_retry_requires_once(db, action)
        if (delegated or action.family in ('mcp_tool_call','request_vault_login','synthesize_voice_memo')) and scope != 'once':
            raise HTTPException(409, 'Delegated actions require a fresh one-time approval')
        if retry_once and scope != 'once':
            raise HTTPException(409, 'This retry requires a fresh one-time approval')
        opaque = action.family in OPAQUE_ACTIONS
        if opaque and acknowledge_unverified_context is not True:
            raise HTTPException(409, "Inspect the current context before allowing this action")
        if scope == "room_session" and not session_review_supported(snapshot):
            raise HTTPException(409, "Session approval requires an observed workflow context")
        if (scope == "room_session" and action.family.startswith(("pointer_", "keyboard_"))
                and not action.workflow_id):
            raise HTTPException(422, "Desktop session grant requires a workflow ID")
        approved_at = now()
        from app.services.review_snapshot import snapshot_value
        retained = snapshot_value(snapshot)
        grant = RoomGrant(room_id=room_id, agent_id=action.agent_id, decision="approved",
            scope={"kind": scope, "session_id": session_id, "families": [action.family],
                "targets": [action.target if scope == "once" else action.scope_target],
                "workflow_id": action.workflow_id if scope == "room_session" else None,
                "payload_digest": action.payload_digest if scope == "once" else None,
                "action_id": action_id if scope == "once" else None,
                "review_version": 1, "approval_review_id": review_id,
                "authority_binding": retained["authority"], "target_pins": retained["targets"],
                "acknowledge_unverified_context": bool(opaque and acknowledge_unverified_context),
                "approved_at": approved_at.isoformat()+"Z",
                **({'execution_binding': action.execution_binding} if delegated else {}),
                **({"lifetime": "session"} if scope == "room_session" else {})},
            # The separate 60-second opaque gate applies at START. Do not
            # revoke a running action merely because its consent is older.
            expires_at=None if scope == "room_session" else approved_at+timedelta(minutes=5))
        db.add(grant); db.flush()
        count = db.execute(update(ToolAction).where(
            ToolAction.action_id == action_id, ToolAction.room_id == room_id,
            ToolAction.state == "waiting", ToolAction.session_id == session_id,
            ToolAction.review_id == review_id, ToolAction.approved_at.is_(None),
            ToolAction.approval_owner_id.is_(None), ToolAction.grant_id.is_(None),
            ToolAction.approval_scope.is_(None),
        ).values(grant_id=grant.id, state="approved", approval_scope=scope,
                 approved_at=approved_at, approval_owner_id=owner_id, send_image_to_model=send_image_to_model)
            .execution_options(synchronize_session=False)).rowcount
        if count != 1:
            raise ReviewChanged("Action was decided while approving")
        stage_event(db, room_id, ActorRef("user", owner_id), "activity",
            {"action_id": action_id, "status": "approved", "scope": scope, "review_id": review_id,
             "approval_owner_id": owner_id, "approved_at": approved_at.isoformat()+"Z",
             "grant_id": grant.id, "acknowledge_unverified_context": bool(opaque and acknowledge_unverified_context),
             "review_snapshot": retained, "send_image_to_model": send_image_to_model},
            f"consent-approve:{action_id}:{review_id}")
        result = {"action_id": action_id, "state": "approved", "scope": scope,
                  "review_id": review_id, "grant_id": grant.id, "send_image_to_model": send_image_to_model,
                  "expires_at": grant.expires_at.isoformat()+"Z" if grant.expires_at else None}
        db.commit()
        return result
    except ReviewChanged:
        db.rollback()
        raise HTTPException(409, "Action review changed; inspect the current request again") from None
    except BaseException:
        db.rollback()
        raise


def revoke(db, grant_id: int, room_id: int, owner_id: int) -> None:
    room = db.query(Conversation).filter_by(id=room_id, user_id=owner_id, is_active=True).first()
    grant = db.get(RoomGrant, grant_id)
    if room is None or grant is None or grant.room_id != room_id:
        raise HTTPException(404, "Grant not found")
    grant.decision = "revoked"
    scope = grant.scope or {}
    from app.services.tool_broker import record_action_result
    for action in db.query(ToolAction).filter(ToolAction.room_id == room_id,
                                             ToolAction.agent_id == grant.agent_id,
                                             ToolAction.state.in_(("pending", "waiting", "approved"))).all():
        target = action.target if scope.get("kind") == "once" else action.scope_target
        if (action.session_id == scope.get("session_id") and action.family in scope.get("families", ())
                and target in scope.get("targets", ()) and action.grant_id in (None, grant_id)
                and (scope.get("kind") != "room_session" or scope.get("workflow_id") == action.workflow_id)
                and (scope.get("kind") != "once" or scope.get("action_id") == action.action_id)):
            record_action_result(db, action.action_id, "cancelled", "Grant revoked before execution.")
    db.commit()


def matches(grant: RoomGrant, action: ToolAction, session_id: str, *, review=None, for_expiry=False) -> bool:
    scope = grant.scope or {}
    if not isinstance(scope, dict) or review is None:
        return False
    kind = scope.get("kind")
    clock = now()
    if not grant_review_matches(scope, action, review,
                                opaque=action.family in OPAQUE_ACTIONS, clock=clock):
        return False
    expired_opaque = (for_expiry and kind == "once" and action.family in OPAQUE_ACTIONS and action.approved_at is not None
                      and action.approved_at + timedelta(seconds=60) <= clock)
    return bool(grant.decision == "approved"
                and (grant_lifetime_valid(scope, grant.expires_at, session_id=session_id, clock=clock)
                     or (expired_opaque and grant.expires_at is not None))
                and grant.room_id == action.room_id and grant.agent_id == action.agent_id
                and scope.get("session_id") == session_id == action.session_id
                and action.family in scope.get("families", ())
                and (action.target if kind == "once" else action.scope_target) in scope.get("targets", ())
                and (kind != "room_session" or scope.get("workflow_id") == action.workflow_id)
                and (kind != "once" or (scope.get("action_id") == action.action_id
                                        and scope.get("payload_digest") == action.payload_digest))
                and (action.state != "approved" or (
                    action.grant_id == grant.id and action.approval_scope == kind
                    and action.approval_owner_id == review.authority.owner_id
                    and action.review_id == scope.get("approval_review_id")
                    and action.approved_at is not None
                    and action.approved_at.isoformat()+"Z" == scope.get("approved_at"))))


def claim(db, action: ToolAction, session_id: str, grant_id: int | None = None, *, broker=None, require_action_review: bool = False) -> GrantRef | None:
    """Stage a claim inside the broker's locked start transaction.

    Every mode, including broad, re-observes the original server review. A
    caller cannot omit that boundary and obtain the old legacy authorization.
    """
    if broker is None or broker.session_id != session_id:
        return None
    if (action.state not in ("waiting", "approved") or action.started_at is not None
            or not canonical_review_id(action.review_id)):
        raise ReviewChanged("Action is not a reviewed unstarted proposal")
    if action.state == "waiting" and any(value is not None for value in (
            action.approved_at, action.approval_owner_id, action.grant_id, action.approval_scope)):
        raise ReviewChanged("Waiting action contains an earlier approval")
    from app.services.request_retry import tool_retry_requires_once
    retry_once = tool_retry_requires_once(db, action)
    require_action_review = require_action_review or retry_once
    review = broker.execution_review(db, action)
    room = db.get(Conversation, action.room_id)
    if room is None or not room.is_active or action.session_id != session_id:
        return None
    run = db.get(RoomAgentRun, (action.room_id, action.agent_id))
    generation = (run.generation if run is not None and not run.paused and run.state != "stopped"
                  and run.trigger_event_id == action.trigger_event_id else None)
    if grant_id is not None:
        candidates = [db.get(RoomGrant, grant_id)]
    else:
        candidates = db.query(RoomGrant).filter_by(room_id=action.room_id, agent_id=action.agent_id,
                                                   decision="approved").order_by(RoomGrant.id.desc()).all()
    for grant in candidates:
        if grant is None or not matches(grant, action, session_id, review=review, for_expiry=True):
            continue
        kind = grant.scope["kind"]
        if action.family in ('request_vault_login','synthesize_voice_memo') and kind != 'once':
            continue
        if retry_once and kind != 'once':
            continue
        if require_action_review and grant.scope.get('approval_review_id') != action.review_id:
            continue
        if kind == "once" and action.family in OPAQUE_ACTIONS and action.approved_at + timedelta(seconds=60) <= now():
            # Preserve the old approval event/snapshot. This action has never
            # started and gets a different token; no consent is renewed.
            old_id = action.review_id
            grant.decision = "revoked"
            action.state, action.review_id = "waiting", str(uuid4())
            action.grant_id = action.approval_scope = None
            action.approved_at = action.approval_owner_id = None
            action.send_image_to_model = False
            stage_event(db, action.room_id, ActorRef("system", None), "activity",
                {"action_id": action.action_id, "status": "review_required", "reason": "approval_expired",
                 "previous_review_id": old_id, "previous_review_snapshot": review.model_dump(mode="json")},
                f"review-expired:{action.action_id}:{old_id}")
            raise ReviewRequired
        if kind == "once":
            changed = db.execute(update(RoomGrant).where(RoomGrant.id == grant.id,
                          RoomGrant.decision == "approved").values(decision="consumed")).rowcount
            if changed != 1:
                continue
        action.grant_id, action.approval_scope = grant.id, kind
        return GrantRef(grant.id, kind, grant.room_id, grant.agent_id, session_id,
                        tuple(grant.scope["families"]), tuple(grant.scope["targets"]), grant.expires_at,
                        action.review_id, review, grant.scope["approval_review_id"],
                        approved_at=approval_time(grant.scope), run_generation=generation,
                        execution_binding=action.execution_binding)
    if (not (action.execution_binding and action.execution_binding.get('context_mode') == 'delegated')
            and not require_action_review and grant_id is None and action.state == "waiting" and action.family not in OPAQUE_ACTIONS and action.family != 'synthesize_voice_memo'
            and broad_state(db, room.user_id, session_id)["enabled"]):
        mode = db.get(BroadMode, room.user_id)
        action.approval_scope = "broad"
        return GrantRef(None, "broad", action.room_id, action.agent_id, session_id,
                        ("*",), ("*",), mode.expires_at, action.review_id, review,
                        broad_revision=mode.updated_at, run_generation=generation, execution_binding=action.execution_binding)
    return None
