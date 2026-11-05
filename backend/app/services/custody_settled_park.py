"""Experimental read-only failure release check; not wired to custody execution.

Call only while holding the SQLite write lock used to serialize cancellation.
The caller must independently confirm the Core returned a safely parked browser.
"""
from datetime import timedelta
from app.models.collaboration import RoomAgentRun, RoomEvent, RoomGrant, ToolAction
from app.models.teammate import TeammateHandoff
from app.services import execution_scopes, teammates
from app.services.action_review import ReviewChanged, observe_authority
from app.services.review_snapshot import retained_review
from app.services.review_execution import approval_time, bound_authority
from app.services.teammate_custody_authority import TaskIdentity


def require_failed_readonly(db, identity, *, failure_event_id, action_ids, workflow_id, owner_epochs):
    if (not isinstance(identity,TaskIdentity) or not isinstance(action_ids,(set,frozenset))
            or not action_ids or len(action_ids)>64 or not isinstance(workflow_id,str) or not workflow_id
            or owner_epochs is None):
        raise ReviewChanged('Browser return evidence is unavailable')
    try:
        value=identity.as_dict()
        if value.get('task_kind')=='cross' or value.get('scope_lineage') is not None:
            raise ReviewChanged('Unscoped teammate task required')
        owner=value['owner_id'];room=value['room_id'];agent=value['agent_id'];root=value['root_request_event_id']
        generation=value['run_generation'];phase=value['phase'];chain=value['lineage']
        if (phase not in ('response','return') or not isinstance(chain,list) or not 1<=len(chain)<=teammates.MAX_DEPTH
                or execution_scopes.root_scope(db,root) is not None):
            raise ReviewChanged('Browser task lineage changed')
        _,owned_room,trigger=teammates._root(db,room,root)
        if owned_room.user_id!=owner or trigger.state!='running':raise ReviewChanged('Original request stopped')
        teammates._member(db,owned_room,agent)
        run=db.get(RoomAgentRun,(room,agent),populate_existing=True)
        marker=(run.pending_task or {}).get('custody_teammate_job') if run else None
        if (not run or run.state!='idle' or run.paused or run.generation!=generation or run.trigger_event_id!=root
                or marker!={'handoff_id':value['handoff_id'],'phase':phase}):raise ReviewChanged('Browser turn changed')
        previous=None
        for item in chain:
            row=db.get(TeammateHandoff,item['handoff_id'],populate_existing=True)
            if (not row or (row.owner_id,row.room_id,row.root_request_event_id)!=(owner,room,root)
                    or (row.sender_agent_id,row.recipient_agent_id)!=(item['sender_agent_id'],item['recipient_agent_id'])
                    or row.parent_handoff_id!=previous or row.state in ('cancelled','interrupted') or row.reply_state=='cancelled'):
                raise ReviewChanged('Browser task ancestry changed')
            previous=row.handoff_id
        if previous!=value['handoff_id']:raise ReviewChanged('Browser task ancestry changed')
        if phase=='response':
            if row.state!='failed' or row.recipient_agent_id!=agent or row.result_event_id!=failure_event_id:raise ReviewChanged('Browser failure changed')
        elif row.reply_state!='failed' or row.sender_agent_id!=agent or row.return_event_id!=failure_event_id:
            raise ReviewChanged('Browser failure changed')
        failure=db.get(RoomEvent,failure_event_id,populate_existing=True)
        tag=failure.payload.get('teammate_handoff') if failure and isinstance(failure.payload,dict) else None
        if (not failure or failure.room_id!=room or failure.actor_type!='agent' or failure.actor_id!=agent or failure.kind!='failure'
                or failure.payload.get('trigger_event_id')!=root or failure.payload.get('generation')!=generation
                or failure.payload.get('round')!=run.round
                or not isinstance(tag,dict) or tag.get('handoff_id')!=value['handoff_id'] or tag.get('phase')!=phase):
            raise ReviewChanged('Browser failure changed')
        observed={action.action_id for action in db.query(ToolAction).filter_by(
            room_id=room,agent_id=agent,trigger_event_id=root,workflow_id=workflow_id).all()}
        if observed!=action_ids:raise ReviewChanged('Browser workflow actions changed')
        for action_id in action_ids:
            action=db.get(ToolAction,action_id,populate_existing=True)
            if (not action or (action.room_id,action.agent_id,action.trigger_event_id,action.workflow_id)!=(room,agent,root,workflow_id)
                    or action.execution_binding is not None or action.family not in ('browser_navigate','browser_inspect')
                    or action.state not in ('completed','denied') or not action.completed_at):
                raise ReviewChanged('Browser action not settled')
            result=db.query(RoomEvent).filter_by(room_id=room,kind='tool_result',idempotency_key='tool-result:'+action_id).one_or_none()
            if (not result or result.actor_type!='tool' or result.payload.get('action_id')!=action_id
                    or result.payload.get('agent_id')!=agent or result.payload.get('state')!=action.state):
                raise ReviewChanged('Browser result not settled')
            review=retained_review(action)
            if (review.authority.owner_id!=owner or review.authority.proposer.owner_epoch is None
                    or owner_epochs.current(owner)!=review.authority.proposer.owner_epoch
                    or action.state=='completed' and (action.approval_scope!='once' or action.approval_owner_id!=owner)):
                raise ReviewChanged('Browser approval changed')
            if action.state=='completed':
                grant=db.get(RoomGrant,action.grant_id,populate_existing=True) if action.grant_id else None
                scope=grant.scope if grant else None
                approved=approval_time(scope) if isinstance(scope,dict) else None
                if (not grant or grant.decision!='consumed' or (grant.room_id,grant.agent_id)!=(room,agent)
                        or scope.get('kind')!='once' or scope.get('action_id')!=action_id
                        or scope.get('session_id')!=action.session_id or scope.get('approval_review_id')!=action.review_id
                        or scope.get('payload_digest')!=action.payload_digest or scope.get('families')!=[action.family]
                        or scope.get('targets')!=[action.target] or not bound_authority(scope,review)
                        or scope.get('target_pins')!=[target.model_dump(mode='json') for target in review.targets]
                        or not approved or approved!=action.approved_at or not action.started_at
                        or not approved<=action.started_at<approved+timedelta(seconds=60)
                        or not grant.expires_at or grant.expires_at<=action.started_at or action.completed_at<action.started_at):
                    raise ReviewChanged('Browser original approval is unavailable')
            if observe_authority(db,room_id=room,agent_id=agent,session_id=action.session_id,
                    proposer=review.authority.proposer,owner_epochs=owner_epochs)!=review.authority:
                raise ReviewChanged('Browser owner changed')
        return True
    except ReviewChanged:raise
    except Exception as error:raise ReviewChanged('Browser return evidence is unavailable') from error
