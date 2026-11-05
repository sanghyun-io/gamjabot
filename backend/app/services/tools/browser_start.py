"""Original review checks across Python browser waits.

These checks precede the public Playwright call. They do not guard its later
internal actionability waits or prove a renderer mutation-start deadline.
Context IDs identify owned temporary objects, not external service accounts.
"""
from datetime import timedelta
import os
import time

from app.services.action_review import ReviewChanged
from app.services.grants import GrantRef, now
from app.services.resource_authority import resource_live
from app.services.review_execution import canonical_review_id, execution_supported
from app.services.review_runtime import BrowserTarget
from app.services.review_snapshot import ReviewSnapshot, _binding


class BrowserStart:
    def __init__(self, browser, key, action, ref, session):
        self.browser, self.key, self.action, self.ref = browser, key, action, ref
        self.session = self.page = self.context = self.context_id = None
        self._review()
        targets = ref.review.targets
        if len(targets) != 1 or not isinstance(targets[0], BrowserTarget):
            raise ReviewChanged("Browser review context is unavailable")
        self.target = targets[0]
        if any(getattr(self.target, name) != getattr(action, name) for name in
               ("room_id", "agent_id", "session_id", "workflow_id")):
            raise ReviewChanged("Browser review ownership changed")
        if self.target.status == "verified":
            self._pin(session)
        elif self.target.status != "absent" or session is not None:
            raise ReviewChanged("Reviewed browser context changed")

    def _review(self):
        from app.services.tool_broker import prepare
        action, ref = self.action, self.ref
        try:
            if (action.family != "browser_interact" or not isinstance(ref, GrantRef)
                    or not isinstance(ref.review, ReviewSnapshot)
                    or not canonical_review_id(ref.review_id)
                    or action.review_id != ref.review_id or _binding(action) != ref.review.action
                    or ref.review.authority.euid != os.geteuid()):
                raise ReviewChanged("Browser operation review changed")
            prepared = prepare(action.family, action.arguments, action.workflow_id)
            if any(getattr(prepared, k) != getattr(ref.review.action, k) for k in
                   ("family", "target", "scope_target", "expected_effect", "payload_digest")):
                raise ReviewChanged("Browser operation payload changed")
            execution_supported(ref.review)
            profiles = getattr(self.browser,'profiles',None)
            from app.services.resource_authority import require_scope_start
            proof=require_scope_start(self.browser.sessions,action,ref)
            target=ref.review.targets[0]
            metadata = profiles.selected(action,proof) if profiles else None
            if ((metadata['id'] if metadata else None), (metadata['revision'] if metadata else None)) != (ref.review.targets[0].profile_id,ref.review.targets[0].profile_revision):
                raise ReviewChanged('Selected browser profile changed')
            if not resource_live(self.browser.sessions, ref, ref.run_generation,
                                 owner_epochs=self.browser.owner_epochs, allow_completed=False):
                raise ReviewChanged("Browser operation authority changed")
            clock, approved = now(), ref.approved_at
            if (ref.scope not in ("once", "room_session") or approved is None or approved > clock
                    or (ref.scope == "room_session" and self.browser.sessions is None)
                    or (ref.scope == "once" and approved + timedelta(seconds=60) <= clock)):
                raise ReviewChanged("Browser operation start approval expired")
        except ReviewChanged:
            raise
        except (ValueError, TypeError, AttributeError, OverflowError) as error:
            raise ReviewChanged("Browser operation review is unavailable") from error

    def _pin(self, session):
        if session is None:
            raise ReviewChanged("Reviewed browser context is missing")
        if (self.target.status == "verified" and self.target.context_id != session.context_id):
            raise ReviewChanged("Reviewed browser context changed")
        self.session, self.page, self.context = session, session.page, session.context
        self.context_id = session.context_id
        self.check(session)

    def created(self, session):
        # Only a review of absence may bind this invocation's newly-created
        # owned context. A lost verified context is never silently recreated.
        if self.session is not None or self.target.status != "absent":
            raise ReviewChanged("Browser context cannot be rebound")
        self._pin(session)

    def check(self, session):
        self._review()
        action = self.action
        from app.services.resource_binding import freeze_binding
        proof=freeze_binding(getattr(self.ref,'execution_binding',None))
        if (getattr(session,'profile_id',None)!=self.target.profile_id or getattr(session,'controller',None)!='agent'
                or freeze_binding(getattr(session,'execution_binding',None))!=proof):
            raise ReviewChanged('Reviewed browser profile or execution scope changed')
        if (session is not self.session or self.browser.workflows.get(self.key) is not session
                or session.page is not self.page or session.context is not self.context
                or session.context_id != self.context_id
                or (session.room_id, session.agent_id, session.workflow_id)
                != (action.room_id, action.agent_id, action.workflow_id)
                or session.deadline <= time.monotonic() or session.page.is_closed()
                or not session.authority._live(self.ref)):
            raise ReviewChanged("Browser operation context changed")
