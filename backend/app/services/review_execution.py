"""Pre-start review checks; conditional executor effects remain a separate gate.

Only broker-observed snapshots reach these helpers. They never reconstruct a
missing review or promote an unsupported observation to execution authority.
"""

import json
from datetime import datetime, timezone
from uuid import UUID

from app.services.action_review import ReviewChanged
from app.services.review_runtime import BrowserTarget, DesktopTarget, OwnedProcessTarget, MCPReviewTarget, VaultProfileTarget, VoiceMemoTarget
from app.services.review_snapshot import ReviewSnapshot, StoredTarget


def _json_equal(left, right):
    try:
        a = json.dumps(left, sort_keys=True, allow_nan=False, separators=(",", ":"))
        b = json.dumps(right, sort_keys=True, allow_nan=False, separators=(",", ":"))
        return len(a.encode("utf-8")) <= 65_536 and a == b
    except (TypeError, ValueError, OverflowError):
        return False


def bound_authority(scope, review):
    return (isinstance(scope, dict) and type(scope.get("review_version")) is int
            and scope["review_version"] == 1
            and _json_equal(scope.get("authority_binding"), review.authority.model_dump(mode="json")))


def canonical_review_id(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value and UUID(value).version == 4
    except (ValueError, TypeError, AttributeError):
        return False


def execution_supported(review: ReviewSnapshot):
    if review.authority.os_user_status != "verified":
        raise ReviewChanged("Local execution identity is unsupported")
    for target in review.targets:
        if isinstance(target, StoredTarget):
            allowed = ("verified",) if target.role in ("executable", "cwd") else ("verified", "absent")
        elif isinstance(target, OwnedProcessTarget):
            allowed = ("verified",)
        elif isinstance(target, BrowserTarget):
            # Absent is a known fresh temporary context, not a service account.
            allowed = ("verified", "absent")
        elif isinstance(target, VaultProfileTarget):
            allowed = ("verified",)
        elif isinstance(target, VoiceMemoTarget):
            allowed = ("configured",)
        elif isinstance(target, MCPReviewTarget):
            allowed = ("configured",)
        elif isinstance(target, DesktopTarget):
            # Actual lease/IPC proof is still required at the native executor.
            allowed = ("configured",)
        else:
            raise ReviewChanged("Execution target type is unsupported")
        if target.status not in allowed:
            raise ReviewChanged("Execution target observation is unavailable")


def approval_time(scope):
    value = scope.get("approved_at")
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            return None
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError, OverflowError):
        return None


def grant_lifetime_valid(scope, expiry, *, session_id, clock):
    """A missing expiry is valid only for an explicit current-session grant.

    Older finite grants keep their deadlines. Legacy/malformed NULL expiry is
    never promoted into session permission, and callers supply the live broker
    session rather than taking a session identifier from the grant itself.
    """
    if (not isinstance(scope, dict) or not isinstance(session_id, str)
            or scope.get("session_id") != session_id):
        return False
    if expiry is None:
        return scope.get("kind") == "room_session" and scope.get("lifetime") == "session"
    return ("lifetime" not in scope and isinstance(expiry, datetime) and expiry > clock)


def session_review_supported(review):
    if review.action.family in ("mcp_tool_call","request_vault_login","synthesize_voice_memo"):return False
    """Reusable opaque consent needs an already observed owned workflow."""
    if review.action.family == "browser_interact":
        return bool(review.action.workflow_id and len(review.targets) == 1
                    and isinstance(review.targets[0], BrowserTarget)
                    and review.targets[0].status == "verified")
    if review.action.family in ("pointer_click", "keyboard_text", "keyboard_chord"):
        return bool(review.action.workflow_id and len(review.targets) == 1
                    and isinstance(review.targets[0], DesktopTarget)
                    and review.targets[0].status == "configured")
    return True


def grant_review_matches(scope, action, review: ReviewSnapshot, *, opaque: bool, clock):
    """Compare stored server binding, including prior file/launch target pins.

    A new action's observation cannot overwrite a reusable grant's pins. The
    later conditional executor must provide trusted successful postconditions
    before pins may advance; this module does not manufacture them.
    """
    binding = getattr(action, 'execution_binding', None)
    if binding and binding.get('context_mode') == 'delegated' and (
            scope.get('kind') != 'once' or not _json_equal(scope.get('execution_binding'), binding)):
        return False
    if (type(scope.get("review_version")) is not int or scope["review_version"] != 1
            or not canonical_review_id(scope.get("approval_review_id"))
            or not _json_equal(scope.get("authority_binding"), review.authority.model_dump(mode="json"))
            or type(scope.get("acknowledge_unverified_context")) is not bool):
        return False
    if any(not isinstance(scope.get(key), list) or not scope[key]
           or len(scope[key]) > 32 or any(not isinstance(value, str) for value in scope[key])
           for key in ("families", "targets")):
        return False
    approved = approval_time(scope)
    if approved is None or approved > clock:
        return False
    kind = scope.get("kind")
    if opaque and (scope["acknowledge_unverified_context"] is not True
                   or (kind == "room_session" and not session_review_supported(review))):
        return False
    if kind == "once":
        if (scope["approval_review_id"] != action.review_id
                or action.state != "approved" or action.approved_at != approved
                or action.approval_owner_id != review.authority.owner_id
                or action.approval_scope != kind):
            return False
    elif kind != "room_session":
        return False
    pins = [target.model_dump(mode="json") for target in review.targets]
    # A navigation grant may create its first temporary context. Each later
    # action still binds its current context from registration through claim.
    if kind == "once" or opaque or not action.family.startswith("browser_"):
        if not _json_equal(scope.get("target_pins"), pins):
            return False
    else:
        old = scope.get("target_pins")
        if not isinstance(old, list) or len(old) != 1:
            return False
        try:
            target = BrowserTarget.model_validate_json(json.dumps(old[0], allow_nan=False))
            if (target.status not in ("absent", "verified") or any(
                    getattr(target, field) != getattr(review.targets[0], field) for field in
                    ("room_id", "agent_id", "session_id", "workflow_id", "profile"))):
                return False
        except (ValueError, TypeError, OverflowError):
            return False
    return True
