"""Read-only lifetime checks for resources that outlive an action result.

Retain the original server review and grant; never rebase a resource onto a
new live Owner. Targets are not re-observed during their own ongoing effects.
This does not implement the separate opaque-operation start deadline.
"""
import os

from app.models.collaboration import BroadMode, RoomAgentRun, RoomGrant, ToolAction
from app.services.action_review import observe_authority
from app.services.grants import GrantRef, OPAQUE_ACTIONS, broad_state, now
from app.services.review_execution import (_json_equal, approval_time, bound_authority, canonical_review_id,
                                            grant_lifetime_valid)
from app.services.review_snapshot import ReviewSnapshot, retained_review, same_review_observations


def run_generation(sessions, room_id, agent_id):
    if sessions is None:
        return None
    try:
        with sessions() as db:
            run = db.get(RoomAgentRun, (room_id, agent_id))
            return run.generation if run else None
    except Exception:
        return None


def require_scope_start(sessions,action,ref):
    """A scope is extra confinement, never a replacement for a fresh grant."""
    from app.services.resource_binding import freeze_binding
    from app.services.action_review import ReviewChanged
    proof=freeze_binding(getattr(ref,'execution_binding',None))
    reviewed=freeze_binding(getattr(getattr(getattr(ref,'review',None),'action',None),'execution_binding',None))
    action_proof=freeze_binding(getattr(action,'execution_binding',None))
    if proof!=reviewed or proof!=action_proof:
        raise ReviewChanged('Resource scope changed')
    if proof is None:return None
    if (sessions is None or
            (proof.execution_room_id,proof.agent_id,proof.run_generation)!=(action.room_id,action.agent_id,ref.run_generation)):
        raise ReviewChanged('Resource scope changed')
    from app.services.execution_scopes import require_binding
    with sessions() as db:require_binding(db,proof.as_dict(),for_start=True)
    return proof


def resource_live(sessions, ref, generation, *, owner_epochs=None, allow_completed=True):
    """Check the retained resource authority, not a new action's permission.

    sessions=None is the pre-existing direct executor fixture mode. It has no
    DB/account proof and is never used by the configured application runtime.
    """
    if sessions is None:
        return True
    try:
        if (not isinstance(ref, GrantRef) or not isinstance(ref.review, ReviewSnapshot)
                or not canonical_review_id(ref.review_id) or type(generation) is not int
                or ref.run_generation != generation or type(ref.run_generation) is not int
                or ref.review.authority.os_user_status != "verified"
                or ref.review.authority.euid != os.geteuid()):
            return False
        original = ref.review
        binding = original.action
        if binding.family not in ("request_vault_login", "start_process", "browser_navigate", "browser_inspect", "browser_interact", "browser_screenshot",
                "screenshot", "pointer_click", "pointer_move", "pointer_scroll", "keyboard_text", "keyboard_chord"):
            return False
        if (ref.room_id, ref.agent_id, ref.session_id) != (
                binding.room_id, binding.agent_id, binding.session_id):
            return False
        with sessions() as db:
            action = db.get(ToolAction, binding.action_id)
            run = db.get(RoomAgentRun, (ref.room_id, ref.agent_id))
            states = ("started", "completed") if allow_completed else ("started",)
            if (action is None or action.state not in states
                    or action.review_id != ref.review_id or action.started_at is None
                    or action.grant_id != ref.grant_id or action.approval_scope != ref.scope
                    or run is None or run.generation != generation or run.paused or run.state == "stopped"
                    or run.trigger_event_id != binding.trigger_event_id
                    or not same_review_observations(retained_review(action), original)):
                return False
            from app.services.resource_binding import freeze_binding
            proof=freeze_binding(getattr(ref,'execution_binding',None))
            reviewed=freeze_binding(getattr(original.action,'execution_binding',None))
            if proof != reviewed:
                return False
            if proof is not None:
                from app.services.execution_scopes import require_binding
                require_binding(db,proof.as_dict(),for_start=False)
            authority = observe_authority(db, room_id=ref.room_id, agent_id=ref.agent_id,
                session_id=ref.session_id, proposer=original.authority.proposer, owner_epochs=owner_epochs)
            if authority != original.authority:
                return False
            if ref.scope == "broad":
                mode = db.get(BroadMode, authority.owner_id)
                return bool(binding.family not in OPAQUE_ACTIONS and mode is not None
                    and ref.broad_revision is not None and mode.updated_at == ref.broad_revision
                    and mode.expires_at == ref.expires_at
                    and broad_state(db, authority.owner_id, ref.session_id)["enabled"])
            grant = db.get(RoomGrant, ref.grant_id) if ref.grant_id is not None else None
            scope = grant.scope if grant is not None else None
            if (ref.scope not in ("once", "room_session") or grant is None
                    or grant.decision != ("consumed" if ref.scope == "once" else "approved")
                    or grant.expires_at != ref.expires_at
                    or not grant_lifetime_valid(scope, grant.expires_at, session_id=ref.session_id, clock=now())
                    or grant.room_id != ref.room_id or grant.agent_id != ref.agent_id
                    or not bound_authority(scope, original)
                    or scope.get("kind") != ref.scope or scope.get("session_id") != ref.session_id
                    or not canonical_review_id(ref.approval_review_id)
                    or scope.get("approval_review_id") != ref.approval_review_id
                    or ref.approved_at is None or approval_time(scope) != ref.approved_at
                    or ref.approved_at > now()
                    or not _json_equal(scope.get("families"), list(ref.families))
                    or not _json_equal(scope.get("targets"), list(ref.targets))):
                return False
            if ref.scope == "room_session":
                return bool(scope.get("workflow_id") == binding.workflow_id
                    and (binding.family not in OPAQUE_ACTIONS
                         or scope.get("acknowledge_unverified_context") is True))
            return bool((binding.family not in OPAQUE_ACTIONS
                         or scope.get("acknowledge_unverified_context") is True)
                and scope.get("action_id") == binding.action_id
                and scope.get("payload_digest") == binding.payload_digest
                and ref.approval_review_id == ref.review_id
                and action.approved_at == ref.approved_at
                and action.approval_owner_id == original.authority.owner_id)
    except Exception:
        # An unavailable DB/Core epoch is not new authority. Monitors must
        # return false and terminate their owned resource, not strand it by
        # raising out of the monitoring loop. Never log private exception data.
        return False
