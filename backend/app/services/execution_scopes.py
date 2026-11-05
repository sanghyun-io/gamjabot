"""DB-owned task lineage. Model arguments never choose execution authority."""
from datetime import datetime, timedelta, timezone
import json
import math
from uuid import NAMESPACE_URL, uuid5
from sqlalchemy import update
from app.models.execution_scope import ExecutionScope
from app.models.collaboration import RoomAgentRun, RoomEvent, RoomMembership, RoomTrigger, ToolAction
from app.models.models import Agent, Conversation, Project, User
from app.services.action_review import ReviewChanged


def now():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def identity(root_id, key):
    return str(uuid5(NAMESPACE_URL, 'gamja-execution:' + root_id + ':' + key))


def root_scope(db, root_id, *, create=False, deadline=None):
    if not root_id:
        return None
    sid = identity(root_id, 'root')
    row = db.get(ExecutionScope, sid)
    if row is not None or not create:
        return row
    event, trigger = db.get(RoomEvent, root_id), db.get(RoomTrigger, root_id)
    room = db.get(Conversation, event.room_id) if event else None
    if not event or not trigger or not room or event.actor_type != 'user' or event.actor_id != room.user_id:
        raise ReviewChanged('Original Owner request is unavailable')
    # Existing original-turn actions are charged when collaboration first opens
    # a scope; no pre-handoff executor work is silently reset.
    actions = db.query(ToolAction).filter_by(trigger_event_id=root_id).all()
    used = sum(max(0, int((a.completed_at-a.started_at).total_seconds()*1000))
        for a in actions if a.started_at and a.completed_at)
    unresolved = any(a.started_at and not a.completed_at for a in actions)
    row = ExecutionScope(scope_id=sid, root_request_event_id=root_id, owner_id=room.user_id,
        root_room_id=room.id, kind='root', epoch=0, state='active',
        deadline_at=deadline or now()+timedelta(seconds=300), native_calls=len(actions),
        max_native_calls=trigger.max_tool_calls, executor_used_ms=used,
        executor_reserved_ms=trigger.max_executor_seconds*1000 if unresolved else 0,
        max_executor_ms=trigger.max_executor_seconds*1000)
    db.add(row); db.flush()
    return row


def _child(db, root, key, parent, *, kind, room_id, agent_id, job_id=None, job_kind=None):
    sid = identity(root.root_request_event_id, key)
    row = db.get(ExecutionScope, sid)
    if row is None:
        row = ExecutionScope(scope_id=sid, root_request_event_id=root.root_request_event_id,
            owner_id=root.owner_id, root_room_id=root.root_room_id, parent_scope_id=parent.scope_id,
            kind=kind, execution_room_id=room_id, agent_id=agent_id, job_id=job_id, job_kind=job_kind,
            epoch=0, state='active', deadline_at=root.deadline_at)
        db.add(row); db.flush()
    if (row.parent_scope_id, row.execution_room_id, row.agent_id) != (parent.scope_id, room_id, agent_id):
        raise ReviewChanged('Task scope cannot change its execution identity')
    return row


def branch(db, root, room_id, agent_id):
    return _child(db, root, f'branch:{room_id}:{agent_id}', root,
        kind='branch', room_id=room_id, agent_id=agent_id)


def job_scope(db, row, kind='bot_message'):
    root = root_scope(db, row.root_request_event_id, create=True,
        deadline=getattr(row, 'deadline_at', None))
    from app.models.bot_message import BotMessage
    from app.models.teammate import TeammateHandoff
    parent_id = getattr(row, 'parent_job_id', None) if kind == 'bot_message' else row.parent_handoff_id
    parent_kind = getattr(row, 'parent_kind', None) if kind == 'bot_message' else 'teammate'
    if parent_id:
        parent_type = BotMessage if parent_kind == 'bot_message' else TeammateHandoff if parent_kind == 'teammate' else None
        parent = db.get(parent_type, parent_id) if parent_type else None
        child_room = getattr(row, 'sender_room_id', getattr(row, 'room_id', None))
        if (not parent or parent.root_request_event_id != root.root_request_event_id
                or parent.owner_id != root.owner_id or row.owner_id != root.owner_id
                or getattr(parent, 'root_room_id', getattr(parent, 'room_id', None)) != root.root_room_id
                or getattr(row, 'root_room_id', getattr(row, 'room_id', None)) != root.root_room_id):
            raise ReviewChanged('Parent task scope is unavailable')
        parent_room = getattr(parent, 'room_id', None)
        sender = (getattr(parent, 'sender_room_id', parent_room), parent.sender_agent_id)
        recipient = (getattr(parent, 'recipient_room_id', parent_room), parent.recipient_agent_id)
        caller = (child_room, row.sender_agent_id)
        if (caller == sender) == (caller == recipient):
            raise ReviewChanged('Parent task sender does not match an execution role')
        delivery = job_scope(db, parent, parent_kind)
        source = delivery if caller == recipient else db.get(ExecutionScope, delivery.parent_scope_id)
        if (not source or (source.execution_room_id, source.agent_id) != caller
                or source.owner_id != root.owner_id or source.root_room_id != root.root_room_id
                or source.root_request_event_id != root.root_request_event_id):
            raise ReviewChanged('Parent task source scope is unavailable')
    else:
        source = branch(db, root, getattr(row, 'sender_room_id', root.root_room_id), row.sender_agent_id)
    return _child(db, root, kind+':'+getattr(row, 'message_id', getattr(row, 'handoff_id', '')),
        source, kind='delivery', room_id=getattr(row, 'recipient_room_id', root.root_room_id),
        agent_id=row.recipient_agent_id, job_id=getattr(row, 'message_id', getattr(row, 'handoff_id', None)), job_kind=kind)


def lineage(db, scope):
    rows=[]
    while scope is not None:
        if len(rows)>=8 or any(r.scope_id==scope.scope_id for r in rows):
            raise ReviewChanged('Task lineage exceeds its bound')
        rows.append(scope); scope=db.get(ExecutionScope,scope.parent_scope_id) if scope.parent_scope_id else None
    return list(reversed(rows))


def bind(db, room_id, agent_id, root_event_id, generation, cross_job=None, teammate_job=None):
    root=root_scope(db,root_event_id)
    if root is None and not cross_job:
        return None
    context_mode='user_room'; job_id=None; phase=None
    if cross_job:
        from app.services.bot_messages import resolve_execution_origin
        from app.models.bot_message import BotMessage
        from app.models.bot_batch import BotBatch
        origin=resolve_execution_origin(db,cross_job['message_id'],cross_job['phase'],room_id,agent_id,generation)
        row=db.get(BotMessage,cross_job['message_id']); batch=db.get(BotBatch,cross_job['message_id'])
        if row is not None and batch is not None:raise ReviewChanged('Ambiguous task identity')
        if batch is not None:
            scope=db.get(ExecutionScope,batch.source_scope_id)
        else:
            delivery=job_scope(db,row)
            scope=delivery if cross_job['phase']=='response' else db.get(ExecutionScope,delivery.parent_scope_id)
        context_mode=origin['context_mode'];job_id=cross_job['message_id'];phase=cross_job['phase']
    elif teammate_job:
        from app.models.teammate import TeammateHandoff
        delivery=job_scope(db,db.get(TeammateHandoff,teammate_job['handoff_id']),'teammate')
        scope=delivery if teammate_job['phase']=='response' else db.get(ExecutionScope,delivery.parent_scope_id)
    else:
        scope=branch(db,root,room_id,agent_id)
    chain=lineage(db,scope);root=chain[0]
    proof={'version':1,'root_scope_id':root.scope_id,'scope_id':scope.scope_id,'owner_id':root.owner_id,
        'root_room_id':root.root_room_id,'root_request_event_id':root_event_id,'execution_room_id':room_id,
        'agent_id':agent_id,'run_generation':generation,'job_id':job_id,'phase':phase,'context_mode':context_mode,
        'deadline_at':aware(scope.deadline_at).isoformat(),
        'lineage':[{'scope_id':r.scope_id,'epoch':r.epoch} for r in chain]}
    require_binding(db,proof)
    return proof


def require_binding(db, proof, *, for_start=True):
    try:
        if not isinstance(proof,dict) or type(proof.get('version')) is not int or proof.get('version')!=1 or type(proof.get('run_generation')) is not int:
            raise ReviewChanged('Task authority is unavailable')
        from app.services.resource_binding import freeze_binding
        proof=freeze_binding(proof).as_dict()
        scope=db.get(ExecutionScope,proof['scope_id'],populate_existing=True)
        rows=lineage(db,scope) if scope else []
        if (not rows or rows[0].scope_id!=proof['root_scope_id'] or len(rows)!=len(proof['lineage'])
                or [{'scope_id':r.scope_id,'epoch':r.epoch} for r in rows]!=proof['lineage']
                or any(r.state!='active' or aware(r.deadline_at)<=now() for r in rows)
                or (scope.execution_room_id,scope.agent_id)!=(proof['execution_room_id'],proof['agent_id'])
                or rows[0].owner_id!=proof['owner_id'] or rows[0].root_room_id!=proof['root_room_id']
                or rows[0].root_request_event_id!=proof['root_request_event_id']
                or aware(scope.deadline_at).isoformat()!=proof['deadline_at']):
            raise ReviewChanged('Task authority changed')
        event=db.get(RoomEvent,proof['root_request_event_id'],populate_existing=True)
        trigger=db.get(RoomTrigger,proof['root_request_event_id'],populate_existing=True)
        room=db.get(Conversation,proof['execution_room_id'],populate_existing=True)
        source=db.get(Conversation,proof['root_room_id'],populate_existing=True)
        agent=db.get(Agent,proof['agent_id'],populate_existing=True)
        user=db.get(User,proof['owner_id'],populate_existing=True)
        member=db.get(RoomMembership,(proof['execution_room_id'],proof['agent_id']),populate_existing=True)
        run=db.get(RoomAgentRun,(proof['execution_room_id'],proof['agent_id']),populate_existing=True)
        projects=[db.get(Project,r.project_id,populate_existing=True) if r else None for r in (room,source)]
        if (not event or event.room_id!=proof['root_room_id'] or event.actor_type!='user' or event.actor_id!=proof['owner_id']
                or not trigger or trigger.state in {'cancelled','interrupted'}
                or for_start and trigger.state!='running'
                or not user or not user.is_active or not agent or not agent.is_active or agent.user_id!=user.id
                or any(not r or not r.is_active or r.user_id!=user.id for r in (room,source))
                or any(not p or not p.is_active or p.user_id!=user.id for p in projects)
                or not member or not member.is_active or not run or run.paused or run.state=='stopped'
                or run.generation!=proof['run_generation'] or run.trigger_event_id!=event.event_id
                or for_start and run.state!='running'):
            raise ReviewChanged('Original request execution changed')
        if for_start and isinstance(event.payload, dict) and 'teammate_retry_context' in event.payload:
            from app.services.teammates import retry_source_current
            from app.services.llm_service import LLMFailure
            try:
                retry_source_current(db, event.payload['teammate_retry_context'], room_id=proof['root_room_id'],
                                     retry_id=event.event_id, owner_id=proof['owner_id'])
            except LLMFailure:
                raise ReviewChanged('The teammate retry source changed') from None
        if for_start and proof['job_id']:
            from app.services.bot_messages import resolve_execution_origin
            origin=resolve_execution_origin(db,proof['job_id'],proof['phase'],room.id,agent.id,run.generation)
            if origin['context_mode']!=proof['context_mode']:
                raise ReviewChanged('Task purpose changed')
        if proof['job_id']:
            from app.models.bot_message import BotMessage
            from app.models.bot_batch import BotBatch
            job=db.get(BotMessage,proof['job_id'])
            batch=db.get(BotBatch,proof['job_id'])
            if job is not None and batch is not None:raise ReviewChanged('Ambiguous task identity')
            delivery=db.get(ExecutionScope,identity(proof['root_request_event_id'],'bot_message:'+proof['job_id']))
            expected=(db.get(ExecutionScope,batch.source_scope_id) if batch and proof['phase']=='return' else
                delivery if proof['phase']=='response' else db.get(ExecutionScope,delivery.parent_scope_id) if delivery else None)
            job=job or batch
            if (not job or not expected or expected.scope_id!=scope.scope_id or job.owner_id!=proof['owner_id']
                    or job.root_request_event_id!=proof['root_request_event_id']
                    or (proof['phase']=='response' and proof['context_mode']!='delegated')
                    or proof['phase'] not in {'response','return'}):
                raise ReviewChanged('Task lineage changed')
        if proof['context_mode'] not in {'user_room','delegated'}:
            raise ReviewChanged('Task purpose is unsupported')
        return scope
    except (KeyError,TypeError,AttributeError,ValueError) as exc:
        if isinstance(exc,ReviewChanged):raise
        raise ReviewChanged('Task authority is unavailable') from exc


def claim_proposal(db, action):
    proof=action.execution_binding
    if not proof:return
    require_binding(db,proof)
    root=db.get(ExecutionScope,proof['root_scope_id'])
    changed=db.execute(update(ExecutionScope).where(ExecutionScope.scope_id==root.scope_id,
        ExecutionScope.native_calls<ExecutionScope.max_native_calls).values(native_calls=ExecutionScope.native_calls+1)).rowcount
    if changed!=1:
        from app.services.llm_service import LLMFailure
        raise LLMFailure('tool_budget_exhausted','The original request shared native action budget is exhausted.')


def reserve_executor(db, action):
    proof=action.execution_binding
    if not proof:return None
    require_binding(db,proof)
    root=db.get(ExecutionScope,proof['root_scope_id'],populate_existing=True)
    if root.executor_reserved_ms:
        prior=db.query(ToolAction).filter_by(trigger_event_id=proof['root_request_event_id']).all()
        held=sum(a.executor_reservation_ms or 0 for a in prior)
        legacy=[a for a in prior if not a.execution_binding]
        if not held and not any(a.started_at and not a.completed_at for a in legacy):
            # Collaboration may open while an already approved legacy action
            # runs. Preserve it, then account its real settlement without
            # transferring its old grant or stranding the shared reservation.
            root.executor_reserved_ms=0
            root.executor_used_ms=(root.max_executor_ms if any(a.state=='uncertain' for a in legacy) else
                sum(max(0,int((a.completed_at-a.started_at).total_seconds()*1000)) for a in legacy if a.started_at and a.completed_at))
            db.flush()
    remaining=root.max_executor_ms-root.executor_used_ms-root.executor_reserved_ms
    if root.executor_reserved_ms:return 0
    if remaining<=0:
        from app.services.llm_service import LLMFailure
        raise LLMFailure('executor_budget_exhausted','The original request shared executor budget is exhausted.')
    changed=db.execute(update(ExecutionScope).where(ExecutionScope.scope_id==root.scope_id,
        ExecutionScope.executor_reserved_ms==0,ExecutionScope.executor_used_ms==root.executor_used_ms
        ).values(executor_reserved_ms=remaining)).rowcount
    if changed!=1:return 0
    action.executor_reservation_ms=remaining
    return remaining


def settle_executor(db, action, elapsed, *, uncertain=False):
    held=action.executor_reservation_ms
    if not action.execution_binding or not held:return
    root=db.get(ExecutionScope,action.execution_binding['root_scope_id'])
    charged=held if uncertain or not isinstance(elapsed,(float,int)) or not math.isfinite(elapsed) else min(held,max(0,math.ceil(elapsed*1000)))
    root.executor_reserved_ms=max(0,root.executor_reserved_ms-held)
    root.executor_used_ms+=charged
    action.executor_reservation_ms=None


def revoke_room(db, room_id, agent_id=None):
    allrows=db.query(ExecutionScope).all()
    seeds={r.scope_id for r in allrows if r.state in {'active','paused'} and
        ((r.kind=='root' and r.root_room_id==room_id and agent_id is None) or
         (r.execution_room_id==room_id and (agent_id is None or r.agent_id==agent_id)))}
    selected=set(seeds)
    for _ in range(8):
        more={r.scope_id for r in allrows if r.parent_scope_id in selected}
        if more<=selected:break
        selected|=more
    refs=[]
    for row in allrows:
        if row.scope_id in selected and row.state in {'active','paused'}:
            row.state='revoked';row.epoch+=1;row.revocation_state='pending'
            refs.append({'scope_id':row.scope_id,'epoch':row.epoch})
    db.flush()
    return refs


def revocations(db, room_id):
    rows=db.query(ExecutionScope).all()
    selected={r.scope_id for r in rows if r.state in {'revoked','paused'} and (r.root_room_id==room_id or r.execution_room_id==room_id)}
    for _ in range(8):
        selected|={r.scope_id for r in rows if r.parent_scope_id in selected}
    return [{'scope_id':r.scope_id,'epoch':r.epoch} for r in rows if r.scope_id in selected and r.state in {'revoked','paused'}]


def record_revocations(db, receipts):
    for receipt in receipts:
        row=db.get(ExecutionScope,receipt.get('scope_id'))
        if row and row.epoch==receipt.get('epoch') and row.state in {'revoked','paused'}:
            row.revocation_state='settled' if receipt.get('state')=='settled' else 'unknown'


def public_binding(db, action):
    proof=action.execution_binding
    if not proof:return {'execution_binding':None,'root_ref':None,'scope_status':None,'revocation_state':None}
    from app.services.bot_messages import event_ref
    row=db.get(ExecutionScope,proof['scope_id'])
    return {'execution_binding':proof,'root_ref':event_ref(db,proof['root_request_event_id']),
        'scope_status':('revoked' if row and row.state in {'revoked','paused'} else 'expired' if not row or aware(row.deadline_at)<=now() else 'active'),
        'revocation_state':row.revocation_state if row else 'unknown'}


def stop_public(row):
    return {'version':1,'stop_id':row.stop_id,'room_id':row.room_id,'created_at':aware(row.created_at).isoformat(),
        'state':row.state,'revocations':row.revocations,'receipts':row.receipts}


def _stop_receipt(db, room_id, refs):
    from app.models.execution_scope import ScopeStopReceipt
    from uuid import uuid4
    room=db.get(Conversation,room_id)
    row=ScopeStopReceipt(stop_id=str(uuid4()),owner_id=room.user_id,room_id=room_id,
        revocations=json.loads(json.dumps(refs)),receipts=[],state='pending',created_at=now())
    db.add(row);db.commit();return row.stop_id


async def confirm_stop(app_state, stop_id, owner_id):
    from app.models.execution_scope import ScopeStopReceipt
    from fastapi import HTTPException
    with app_state.session_factory() as db:
        saved=db.get(ScopeStopReceipt,stop_id)
        if not saved or saved.owner_id!=owner_id:raise HTTPException(404,{'code':'scope_stop_not_found'})
        refs=json.loads(json.dumps(saved.revocations))
        for ref in refs:
            row=db.get(ExecutionScope,ref['scope_id'])
            if not row or row.owner_id!=owner_id or row.epoch!=ref['epoch'] or row.state not in {'revoked','paused'}:
                raise HTTPException(409,{'code':'scope_stop_changed'})
        ref_ids={ref['scope_id'] for ref in refs}
        # Include cancelled/uncertain: Stop may have settled the DB action before
        # the asynchronously issued Core nonce has acknowledged cancellation.
        mcp_ids=set();mcp_uncertain_ids=set()
        for action in db.query(ToolAction).filter(ToolAction.family=='mcp_tool_call',
                ToolAction.state.in_(('pending','waiting','approved','started','cancelled','uncertain'))):
            proof=action.execution_binding
            if isinstance(proof,dict) and proof.get('owner_id')==owner_id:
                related={e.get('scope_id') for e in proof.get('lineage',[]) if isinstance(e,dict) and e.get('scope_id') in ref_ids}
                mcp_ids.update(related)
                if action.started_at is not None or action.state in {'started','uncertain'}:
                    mcp_uncertain_ids.update(related)
        native_ids=set();browser_ids=set()
        for action in db.query(ToolAction).filter(ToolAction.family!='mcp_tool_call'):
            proof=action.execution_binding
            if isinstance(proof,dict) and proof.get('owner_id')==owner_id:
                related={e.get('scope_id') for e in proof.get('lineage',[]) if isinstance(e,dict) and e.get('scope_id') in ref_ids}
                native_ids.update(related)
                if action.family in ('browser_navigate','browser_inspect','browser_interact','browser_screenshot'):
                    browser_ids.update(related)
        core_ids=mcp_ids|browser_ids
        core_refs=[ref for ref in refs if ref['scope_id'] in core_ids]
        os_refs=[ref for ref in refs if ref['scope_id'] not in mcp_ids or ref['scope_id'] in native_ids]
    core_settled = not core_refs
    if core_refs:
        bridge=getattr(app_state,'mcp_client',None);provider=getattr(app_state,'owner_epoch_provider',None)
        try:
            epoch=provider.current(owner_id) if provider else None
            core=getattr(bridge,'core_epoch',None)
            if (getattr(bridge,'available',False) is not True or not callable(getattr(bridge,'scope_revoke',None))
                    or not isinstance(epoch,str) or len(epoch)!=64 or not isinstance(core,str) or len(core)!=64):
                raise ValueError('MCP scope channel unavailable')
            response=await bridge.scope_revoke({'version':1,'owner_id':owner_id,'owner_epoch':epoch,
                'core_epoch':core,'scopes':core_refs})
            core_settled=isinstance(response,dict) and response.get('state')=='revoked'
        except Exception:core_settled=False
    stop=getattr(app_state.local_executor,'stop_scopes',None)
    try:receipts=await stop(os_refs) if stop and os_refs else []
    except Exception:receipts=[]
    received={(r.get('scope_id'),r.get('epoch')):r for r in receipts if isinstance(r,dict)} if isinstance(receipts,list) else {}
    receipts=[{'scope_id':r['scope_id'],'epoch':r['epoch'],
        'state':'settled' if (r not in os_refs or received.get((r['scope_id'],r['epoch']),{}).get('state')=='settled')
            and (r['scope_id'] not in core_ids or core_settled)
            and r['scope_id'] not in mcp_uncertain_ids else 'unknown'} for r in refs]
    with app_state.session_factory() as db:
        record_revocations(db,receipts)
        saved=db.get(ScopeStopReceipt,stop_id)
        saved.receipts=receipts;saved.state='settled' if all(r['state']=='settled' for r in receipts) else 'unknown'
        db.commit();return stop_public(saved)


async def stop_room_resources(app_state, room_id, agent_id=None):
    # Freeze once before awaiting cleanup. Control retries use this receipt,
    # never a new room cancellation or a later query for newly created roots.
    with app_state.session_factory() as db:
        refs=revocations(db,room_id)
        stop_id=_stop_receipt(db,room_id,refs) if refs else None
        owner_id=db.get(Conversation,room_id).user_id
    try:await app_state.local_executor.stop_room(room_id,agent_id)
    except Exception:
        if stop_id:
            from app.models.execution_scope import ScopeStopReceipt
            with app_state.session_factory() as db:
                saved=db.get(ScopeStopReceipt,stop_id);saved.state='unknown';db.commit()
        raise
    return await confirm_stop(app_state,stop_id,owner_id) if stop_id else None


def root_actions(db, owner_id, room_id, root_id):
    from sqlalchemy import or_
    return db.query(ToolAction).join(Conversation,Conversation.id==ToolAction.room_id).join(
        Agent,Agent.id==ToolAction.agent_id).filter(ToolAction.trigger_event_id==root_id,
        Conversation.user_id==owner_id,Agent.user_id==owner_id,
        or_(ToolAction.room_id==room_id,ToolAction.execution_binding['root_request_event_id'].as_string()==root_id)
        ).order_by(ToolAction.action_id).all()


def _descendants(db, seeds):
    rows=db.query(ExecutionScope).all();selected=set(seeds)
    for _ in range(8):selected|={r.scope_id for r in rows if r.parent_scope_id in selected}
    return [r for r in rows if r.scope_id in selected]


def pause_tasks(db, room_id, agent_id, *, root_pause=False):
    roots=db.query(ExecutionScope).filter_by(kind='root',root_room_id=room_id,state='active').all() if root_pause else []
    seeds={r.scope_id for r in roots}
    if not seeds:
        seeds={r.scope_id for r in db.query(ExecutionScope).filter_by(execution_room_id=room_id,agent_id=agent_id,state='active').all()}
    rows=_descendants(db,seeds)
    for row in rows:
        if row.state=='active':
            row.state='paused';row.epoch+=1;row.revocation_state='pending'
    from app.models.bot_message import BotMessage
    for job in db.query(BotMessage).all():
        delivery=db.get(ExecutionScope,identity(job.root_request_event_id,'bot_message:'+job.message_id))
        if not delivery:continue
        if delivery.scope_id in {r.scope_id for r in rows} and job.state in {'pending','running'}:job.state='pending'
        if delivery.parent_scope_id in {r.scope_id for r in rows} and job.reply_state in {'pending','running'}:job.reply_state='pending'
    from app.models.teammate import TeammateHandoff
    for job in db.query(TeammateHandoff).all():
        delivery=db.get(ExecutionScope,identity(job.root_request_event_id,'teammate:'+job.handoff_id))
        if not delivery:continue
        if delivery.scope_id in {r.scope_id for r in rows} and job.state=='running':job.state='pending'
        if delivery.parent_scope_id in {r.scope_id for r in rows} and job.reply_state=='running':job.reply_state='pending'
    from app.models.bot_batch import BotBatch
    for batch in db.query(BotBatch).all():
        if batch.source_scope_id in {r.scope_id for r in rows} and batch.reply_state=='running':batch.reply_state='pending'
    return rows


def resume_tasks(db, scope_id):
    rows=_descendants(db,{scope_id})
    if any(r.state=='revoked' or aware(r.deadline_at)<=now() for r in rows):
        raise ReviewChanged('Paused task expired or was stopped')
    if any(r.state=='paused' and r.revocation_state!='settled' for r in rows):
        raise ReviewChanged('Paused resource cleanup is not confirmed')
    ids={r.scope_id for r in rows}
    actions=db.query(ToolAction).all()
    if any(a.execution_binding and any(p['scope_id'] in ids for p in a.execution_binding['lineage'])
           and a.state in {'started','uncertain','pending','waiting','approved'} for a in actions):
        raise ReviewChanged('Paused effect is unsettled or unknown')
    for row in rows:
        if row.state=='paused':row.state='active';row.epoch+=1;row.revocation_state=None
    for run in db.query(RoomAgentRun).all():
        pending=dict(run.pending_task or {})
        if pending.get('execution_job') and pending.get('execution_binding',{}).get('scope_id') in ids:
            pending['cross_resumed']=True;run.pending_task=pending
    from app.models.bot_message import BotMessage
    for job in db.query(BotMessage).all():
        delivery=db.get(ExecutionScope,identity(job.root_request_event_id,'bot_message:'+job.message_id))
        if not delivery:continue
        if delivery.scope_id in ids and job.state=='paused':job.state='pending'
        if delivery.parent_scope_id in ids and job.reply_state=='paused':job.reply_state='pending'


def paused_actions(db, scopes):
    from app.services.tool_broker import stage_action_result
    from app.models.collaboration import RoomGrant
    ids={r.scope_id for r in scopes}
    for action in db.query(ToolAction).all():
        if not action.execution_binding or not any(e['scope_id'] in ids for e in action.execution_binding['lineage']):continue
        if action.state not in {'pending','waiting','approved','started'}:continue
        grant=db.get(RoomGrant,action.grant_id) if action.grant_id else None
        if grant and grant.scope.get('kind')=='once' and grant.decision=='approved':grant.decision='revoked'
        stage_action_result(db,action.action_id,'uncertain' if action.state=='started' else 'cancelled',
            'Paused after start; effect unknown.' if action.state=='started' else 'Paused; review a new proposal after resuming.')


def settle_revoked_actions(db):
    from app.services.tool_broker import stage_action_result
    from app.models.collaboration import RoomGrant
    revoked={s.scope_id for s in db.query(ExecutionScope).filter_by(state='revoked').all()}
    for action in db.query(ToolAction).filter(ToolAction.state.in_(('pending','waiting','approved','started'))).all():
        if not action.execution_binding or not any(e['scope_id'] in revoked for e in action.execution_binding['lineage']):continue
        grant=db.get(RoomGrant,action.grant_id) if action.grant_id else None
        if grant and grant.decision=='approved' and grant.scope.get('kind')=='once':grant.decision='revoked'
        state='uncertain' if action.state=='started' else 'cancelled'
        stage_action_result(db,action.action_id,state,'Source task stopped; effect unknown.' if state=='uncertain' else 'Source task stopped before execution.')
        settle_executor(db,action,0,uncertain=state=='uncertain')
