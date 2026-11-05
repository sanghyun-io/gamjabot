"""Durable same-trigger boundaries; never replay a tool proposal or stored credentials."""
from hashlib import sha256
import json
from fastapi import HTTPException
from sqlalchemy import text
from app.models.collaboration import RoomAgentRun, RoomTrigger, RoomEvent, ToolAction, ProviderConnection, RoomMembership
from app.models.models import Agent, Conversation
from app.services.memory_context import snapshot


def guard(db, room_id, agent_id):
    room=db.get(Conversation,room_id);agent=db.get(Agent,agent_id)
    connection=db.get(ProviderConnection,agent.connection_id) if agent else None
    if not room or not agent or agent.user_id != room.user_id or not agent.is_active:
        raise HTTPException(409, {'code':'resume_context_changed'})
    data={'owner':room.user_id,'agent':agent_id,'connection':agent.connection_id,'model':agent.model_id,
          'system':agent.system_prompt,'temperature':agent.temperature,'max_tokens':agent.max_tokens,
          'config_revision':connection.readiness_revision if connection else None,
          'memory':snapshot(db,room_id,agent_id)['digest']}
    return sha256(json.dumps(data,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


def settled_events(db, room_id, trigger_id, agent_id, round_number):
    return [e for e in db.query(RoomEvent).filter_by(room_id=room_id,actor_type='agent',actor_id=agent_id).all()
            if not e.payload.get('teammate_handoff') and not e.payload.get('bot_message') and not e.payload.get('bot_batch') and e.payload.get('trigger_event_id')==trigger_id and e.payload.get('round')==round_number
            and (e.kind in ('message','failure') or e.kind=='activity' and e.payload.get('status')=='passed')]


def resume_status(db, run):
    cross=(run.pending_task or {}).get('cross_resume_boundary')
    if run.paused and cross:
        from app.services.execution_scopes import _descendants, aware, now
        try:
            if cross.get('guard')!=cross_guard(db,run):return 'resume_context_changed'
            scopes=_descendants(db,{cross['scope_id']})
            if any(s.state=='revoked' or aware(s.deadline_at)<=now() for s in scopes):return 'resume_cancelled'
            if any(s.revocation_state!='settled' for s in scopes if s.state=='paused'):return 'resume_settling'
            ids={s.scope_id for s in scopes}
            if any(a.execution_binding and any(e['scope_id'] in ids for e in a.execution_binding['lineage'])
                    and a.state in {'started','uncertain'} for a in db.query(ToolAction).all()):return 'resume_effect_unknown'
            return 'ready'
        except (HTTPException,KeyError):return 'resume_context_changed'
    boundary=(run.pending_task or {}).get('resume_boundary')
    if not run.paused or not isinstance(boundary,dict):return None
    trigger=db.get(RoomTrigger,boundary.get('trigger'))
    if not trigger or trigger.room_id!=run.room_id or trigger.state!='paused':return 'resume_cancelled'
    from app.services.execution_scopes import root_scope, _descendants
    scope=root_scope(db,trigger.event_id)
    if scope:
        scope_rows=_descendants(db,{scope.scope_id})
        if any(s.state=='paused' and s.revocation_state!='settled' for s in scope_rows):return 'resume_settling'
        scoped_actions=db.query(ToolAction).filter_by(trigger_event_id=trigger.event_id).all()
        if any(a.execution_binding and a.state in {'started','uncertain'} for a in scoped_actions):return 'resume_effect_unknown'
    try:
        if boundary.get('guard') != guard(db,run.room_id,run.agent_id):return 'resume_context_changed'
    except HTTPException:return 'resume_context_changed'
    actions=db.query(ToolAction).filter_by(room_id=run.room_id,trigger_event_id=trigger.event_id).all()
    if any(a.state not in ('completed','failed','denied','cancelled','uncertain') for a in actions):return 'resume_settling'
    if any(a.state=='uncertain' for a in actions):return 'resume_effect_unknown'
    return 'ready'


def pause(db, room_id, agent_id):
    from app.services.room_service import get_run, _require_member
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    _require_member(db,room_id,agent_id);run=get_run(db,room_id,agent_id)
    if run.paused:return run
    if (run.pending_task or {}).get('execution_job'):
        return pause_cross(db,run)
    trigger=db.query(RoomTrigger).join(RoomEvent,RoomEvent.event_id==RoomTrigger.event_id).filter(
        RoomTrigger.room_id==room_id,RoomTrigger.state.in_(('running','pending','paused'))).order_by(RoomEvent.seq).first()
    if trigger and agent_id in (trigger.selected_agents or []):
        from app.services import execution_scopes
        if execution_scopes.root_scope(db,trigger.event_id):
            scopes=execution_scopes.pause_tasks(db,room_id,agent_id,root_pause=True)
            execution_scopes.paused_actions(db,scopes)
        trigger.state='paused'
        for aid in trigger.selected_agents:
            peer=db.get(RoomAgentRun,(room_id,aid))
            member=db.get(RoomMembership,(room_id,aid));agent=db.get(Agent,aid)
            if not peer or not member or not member.is_active or not agent or not agent.is_active:continue
            pending=dict(peer.pending_task or {})
            # Preserve the original boundary even on duplicate pauses.
            if (pending.get('resume_boundary') or {}).get('trigger') != trigger.event_id:
                pending['resume_boundary']={'trigger':trigger.event_id,'round':trigger.current_round or 1,
                    'guard':pending.get('execution_guard') or guard(db,room_id,aid)}
            peer.pending_task=pending;peer.generation+=1;peer.active_tool={}
            peer.state='paused' if aid==agent_id or peer.paused else 'idle'
    run.paused=True;run.state='paused';run.generation+=1;db.commit();db.refresh(run);return run


def resume(db, room_id, agent_id):
    from app.services.room_service import get_run, _require_member
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    _require_member(db,room_id,agent_id);run=get_run(db,room_id,agent_id)
    if not run.paused:return run,None
    if (run.pending_task or {}).get('cross_resume_boundary'):
        return resume_cross(db,run)
    boundary=(run.pending_task or {}).get('resume_boundary')
    trigger=None
    if isinstance(boundary,dict):
        status=resume_status(db,run)
        if status!='ready':raise HTTPException(409,{'code':status})
        trigger=db.get(RoomTrigger,boundary['trigger'])
        from app.services import execution_scopes
        scope=execution_scopes.root_scope(db,trigger.event_id)
        if scope:
            from app.services.action_review import ReviewChanged
            try:execution_scopes.resume_tasks(db,scope.scope_id)
            except ReviewChanged:raise HTTPException(409,{'code':'resume_settling'}) from None
        for aid in trigger.selected_agents:
            peer=db.get(RoomAgentRun,(room_id,aid))
            if not peer:continue
            b=(peer.pending_task or {}).get('resume_boundary')
            if b and b.get('guard')!=guard(db,room_id,aid):raise HTTPException(409,{'code':'resume_context_changed'})
        run.paused=False;run.state='idle';run.generation+=1
        others=[db.get(RoomAgentRun,(room_id,aid)) for aid in trigger.selected_agents if aid!=agent_id]
        if not any(peer and peer.paused for peer in others):trigger.state='pending'
    else:run.paused=False;run.state='idle';run.generation+=1
    db.commit();db.refresh(run)
    return run,trigger.event_id if trigger and trigger.state=='pending' else None


def stored_results(db, room_id, trigger_id, agent_id, watermark):
    # Only the already committed, public tool-result events are reused. No call arguments,
    # provider continuation, image bytes, cookies, or credential material is persisted here.
    from app.services.providers.base import provider_safe_event
    return [provider_safe_event({'event_id':e.event_id,'seq':e.seq,'actor_type':e.actor_type,'actor_id':e.actor_id,
             'actor_name_snapshot':e.actor_name_snapshot,'kind':e.kind,'payload':dict(e.payload)})
            for e in db.query(RoomEvent).filter(RoomEvent.room_id==room_id,RoomEvent.seq>watermark,
                RoomEvent.kind=='tool_result').order_by(RoomEvent.seq).all()
            if e.payload.get('trigger_event_id')==trigger_id and e.payload.get('agent_id')==agent_id]


def completed_action(db, room_id, trigger_id, agent_id, prepared, scope_id=None):
    """Exact completed operation identity, never a semantic guess or partial target match."""
    if not prepared or not prepared.payload_digest:return None
    query = db.query(ToolAction).filter_by(room_id=room_id,trigger_event_id=trigger_id,agent_id=agent_id,
        family=prepared.family, target=prepared.target,scope_target=prepared.scope_target,
        expected_effect=prepared.expected_effect,payload_digest=prepared.payload_digest,state='completed')
    if scope_id:query=query.filter(ToolAction.execution_binding['scope_id'].as_string()==scope_id)
    return query.order_by(ToolAction.completed_at.desc()).first()


def used_budget(db, room_id, trigger_id, agent_id, watermark):
    ids={e['payload'].get('action_id') for e in stored_results(db,room_id,trigger_id,agent_id,watermark)}
    calls=0;seconds=0.0
    for action in db.query(ToolAction).filter(ToolAction.action_id.in_([x for x in ids if x]),
            ToolAction.room_id==room_id,ToolAction.agent_id==agent_id,ToolAction.trigger_event_id==trigger_id):
        calls+=1
        if action.started_at:
            if not action.completed_at:raise HTTPException(409,{'code':'resume_effect_unknown'})
            seconds+=max(0.0,(action.completed_at-action.started_at).total_seconds())
    return calls,seconds


def cross_guard(db, run):
    from app.models.bot_message import BotMessage
    job=(run.pending_task or {}).get('execution_job') or {}
    from app.models.bot_batch import BotBatch
    row=db.get(BotMessage,job.get('message_id'))
    batch=db.get(BotBatch,job.get('message_id'))
    if row is not None and batch is not None:raise HTTPException(409,{'code':'resume_context_changed'})
    row=row or batch
    agent=db.get(Agent,run.agent_id);connection=db.get(ProviderConnection,agent.connection_id) if agent else None
    root=db.get(RoomEvent,row.root_request_event_id) if row else None
    if not row or not agent or not root or root.actor_id!=agent.user_id:
        raise HTTPException(409,{'code':'resume_context_changed'})
    result=db.get(RoomEvent,getattr(row,'result_event_id',None)) if getattr(row,'result_event_id',None) else None
    data=[getattr(row,'message_id',None) or row.batch_id,row.content,row.parent_job_id,row.parent_kind,root.payload,
        result.payload if result else None,job.get('phase'),agent.user_id,agent.connection_id,agent.model_id,
        agent.system_prompt,agent.temperature,agent.max_tokens,connection.readiness_revision if connection else None]
    if batch is not None:
        from app.services.bot_batches import members,member_value
        data.append([(m.message_id,m.content,member_value(db,m)[0:2],member_value(db,m)[2].event_id if member_value(db,m)[2] else None) for m in members(db,batch)])
    return sha256(json.dumps(data,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


def pause_cross(db, run):
    from app.services import execution_scopes
    proof=(run.pending_task or {}).get('execution_binding')
    if not proof:raise HTTPException(409,{'code':'resume_context_changed'})
    scopes=execution_scopes.pause_tasks(db,run.room_id,run.agent_id)
    execution_scopes.paused_actions(db,scopes)
    pending=dict(run.pending_task or {})
    pending['cross_resume_boundary']={'scope_id':proof['scope_id'],'trigger':run.trigger_event_id,
        'guard':cross_guard(db,run)}
    run.pending_task=pending;run.paused=True;run.state='paused';run.generation+=1;run.active_tool={}
    db.commit();db.refresh(run);return run


def resume_cross(db, run):
    from app.services import execution_scopes
    from app.services.action_review import ReviewChanged
    boundary=(run.pending_task or {}).get('cross_resume_boundary')
    status=resume_status(db,run)
    if status!='ready':raise HTTPException(409,{'code':status or 'resume_context_changed'})
    if not boundary or boundary.get('guard')!=cross_guard(db,run):
        raise HTTPException(409,{'code':'resume_context_changed'})
    root=db.get(RoomTrigger,boundary['trigger'])
    if not root or root.state!='running':raise HTTPException(409,{'code':'resume_cancelled'})
    try:execution_scopes.resume_tasks(db,boundary['scope_id'])
    except ReviewChanged:raise HTTPException(409,{'code':'resume_settling'}) from None
    run.paused=False;run.state='idle';run.generation+=1
    pending=dict(run.pending_task or {});pending.pop('cross_resume_boundary',None)
    pending['cross_resumed']=True;run.pending_task=pending
    db.commit();db.refresh(run)
    return run,root.event_id
