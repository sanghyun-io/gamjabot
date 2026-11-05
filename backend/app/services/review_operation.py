"""Final local operation-start checks, not external account/version proof.

Only the original server-retained review supplies identity and approval time.
The native transport carries the bounded deadline in its existing signed field.
"""
from datetime import timedelta, timezone
import os

from app.services.action_review import ReviewChanged
from app.services.grants import GrantRef, OPAQUE_ACTIONS, now
from app.services.resource_authority import resource_live
from app.services.review_execution import canonical_review_id, execution_supported
from app.services.review_runtime import DesktopTarget
from app.services.review_snapshot import ReviewSnapshot, _binding


def operation_start(action, ref, *, sessions, owner_epochs, context_id):
    """Validate before a native operation; return its opaque start deadline.

    Browser mutation after asynchronous waits remains a separate integration.
    """
    from app.services.tool_broker import prepare
    try:
        if (not isinstance(ref, GrantRef) or not isinstance(ref.review, ReviewSnapshot)
                or not canonical_review_id(ref.review_id) or action.review_id != ref.review_id
                or _binding(action) != ref.review.action
                or ref.review.authority.euid != os.geteuid()):
            raise ReviewChanged("Operation review changed")
        prepared = prepare(action.family, action.arguments, action.workflow_id)
        if any(getattr(prepared, k) != getattr(ref.review.action, k) for k in
               ("family", "target", "scope_target", "expected_effect", "payload_digest")):
            raise ReviewChanged("Operation payload changed")
        execution_supported(ref.review)
        if not resource_live(sessions, ref, ref.run_generation, owner_epochs=owner_epochs,
                             allow_completed=False):
            raise ReviewChanged("Operation authority changed")
        if (not isinstance(context_id, str) or len(context_id) != 32
                or any(c not in "0123456789abcdef" for c in context_id)
                or len(ref.review.targets) != 1):
            raise ReviewChanged("Operation context is unavailable")
        target = ref.review.targets[0]
        if not isinstance(target, DesktopTarget) or target.context_id != context_id:
            raise ReviewChanged("Reviewed native adapter changed")
        if action.family not in OPAQUE_ACTIONS:
            return None
        if ref.scope == "room_session":
            # resource_live has checked the original explicit consent and live
            # session. Native transport/lease deadlines still apply separately.
            if sessions is None:
                raise ReviewChanged("Session consent requires durable authority")
            return None
        approved = ref.approved_at
        clock = now()
        if (ref.scope != "once" or approved is None or approved > clock
                or approved + timedelta(seconds=60) <= clock):
            raise ReviewChanged("Operation start approval expired")
        return int((approved + timedelta(seconds=60)).replace(tzinfo=timezone.utc).timestamp() * 1000)
    except ReviewChanged:
        raise
    except (ValueError, TypeError, AttributeError, OverflowError) as error:
        raise ReviewChanged("Operation review is unavailable") from error
