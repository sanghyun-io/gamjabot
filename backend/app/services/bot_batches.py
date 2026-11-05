"""Atomic bounded fan-out, terminal join, and exactly one source continuation."""
from datetime import timedelta
from hashlib import sha256
import json
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import func, or_, text, update
from app.models.bot_batch import BotBatch
from app.models.bot_message import BotMessage
from app.models.teammate import TeammateHandoff, TeammateBudget
from app.models.execution_scope import ExecutionScope
from app.models.collaboration import RoomAgentRun, RoomEvent, RoomTrigger
from app.models.models import Conversation
from app.schemas.bot_profile import BotConversationResolve
from app.services import bot_messages as messages, bot_profile, teammates, execution_scopes as scopes
from app.services.events import ActorRef, stage_event
from app.services.llm_service import LLMFailure


def fail(code='bot_batch_context_changed', message='Batch execution is no longer current.'):
    raise LLMFailure(code,message)


def metadata(row,phase,index=None):
    return {'version':1,'batch_id':row.batch_id,'phase':phase,'root_room_id':row.root_room_id,
        'root_request_event_id':row.root_request_event_id,**({'member_index':index} if index is not None else {})}


def members(db,row):
    values=db.query(BotMessage).filter_by(batch_id=row.batch_id).order_by(BotMessage.batch_index).all()
    if (not 2<=len(values)<=8 or [r.batch_index for r in values]!=list(range(len(values)))
            or len({r.recipient_agent_id for r in values})!=len(values)
            or any(r.owner_id!=row.owner_id or r.root_request_event_id!=row.root_request_event_id
                or r.sender_room_id!=row.sender_room_id or r.sender_agent_id!=row.sender_agent_id for r in values)):
        fail()
    return values


def root_rows(db,root_id):
    return db.query(BotBatch).filter_by(root_request_event_id=root_id).order_by(BotBatch.created_at,BotBatch.batch_id).all()


def child_pending(db,message_id):
    return any(r.reply_state in {'none','pending','running'} and r.state not in {'cancelled','interrupted'}
        for r in db.query(BotBatch).filter_by(parent_job_id=message_id,parent_kind='bot_message').all())


def member_pending(db,row,seen=None):
    seen=set() if seen is None else set(seen)
    if row.message_id in seen:fail()
    seen.add(row.message_id)
    if row.state in {'pending','running'}:return True
    for child in db.query(BotMessage).filter_by(parent_job_id=row.message_id,parent_kind='bot_message').all():
        if child.batch_id:continue
        if member_pending(db,child,seen) or child.reply_state in {'pending','running'}:return True
    return child_pending(db,row.message_id)


def member_value(db,row):
    waiting=member_pending(db,row)
    result=messages.effective_result(db,row) if not waiting else None
    state='running' if waiting and row.state not in {'pending','running'} else row.state
    code=row.failure_code
    if not waiting and row.state=='completed' and (not result or result.kind!='message'):
        state='failed';code='missing_result' if not result else 'no_answer'
    nested=db.query(BotBatch).filter_by(parent_job_id=row.message_id,parent_kind='bot_message').all()
    if not waiting and any(r.state!='completed' or r.reply_state!='delivered' for r in nested):
        code=code or 'nested_partial'
    return state,code,result


def receipt(db,row):
    source=db.get(Conversation,row.sender_room_id)
    values=[]
    for m in members(db,row):
        state,code,result=member_value(db,m);target=db.get(Conversation,m.recipient_room_id)
        values.append({'index':m.batch_index,'message_id':m.message_id,'recipient_agent_id':m.recipient_agent_id,
            'recipient_name':m.recipient_name,'target_room':{'room_id':m.recipient_room_id,'title':target.title if target else ''},
            'content':m.content,'state':state,'failure_code':code,
            'request_ref':messages.event_ref(db,m.request_event_id),'delivery_ref':messages.event_ref(db,m.delivery_event_id),
            'result_ref':messages.event_ref(db,m.result_event_id),
            'resolved_result_ref':messages.event_ref(db,result.event_id) if result else None})
    return {'version':1,'batch_id':row.batch_id,'root_ref':messages.event_ref(db,row.root_request_event_id),
        'source_room':{'room_id':row.sender_room_id,'title':source.title if source else ''},
        'sender_agent_id':row.sender_agent_id,'sender_name':row.sender_name,'parent_job_id':row.parent_job_id,
        'parent_kind':row.parent_kind,'source_scope_id':row.source_scope_id,'state':row.state,'reply_state':row.reply_state,
        'failure_code':row.failure_code,'request_ref':messages.event_ref(db,row.request_event_id),
        'return_delivery_ref':messages.event_ref(db,row.return_delivery_event_id),'return_ref':messages.event_ref(db,row.return_event_id),
        'members':values,'created_at':messages.aware(row.created_at).isoformat(),
        'updated_at':messages.aware(row.updated_at).isoformat(),'deadline_at':messages.aware(row.deadline_at).isoformat()}


def list_room(db,room_id,owner_id):
    from app.services.room_service import require_room
    require_room(db,room_id,owner_id,active=False)
    ids=db.query(BotMessage.batch_id).filter_by(recipient_room_id=room_id).filter(BotMessage.batch_id.is_not(None))
    rows=db.query(BotBatch).filter(BotBatch.owner_id==owner_id,or_(BotBatch.root_room_id==room_id,
        BotBatch.sender_room_id==room_id,BotBatch.batch_id.in_(ids))).order_by(BotBatch.created_at.desc(),BotBatch.batch_id).limit(101).all()
    return {'version':1,'room_id':room_id,'batches':[receipt(db,r) for r in rows[:100]],'batches_truncated':len(rows)>100}


def send(db,sender_room_id,root_room_id,root_id,sender_id,generation,call_id,arguments,
         *,parent_job_id=None,parent_kind=None,provider_calls=0,expected_memory_digest=None,execution_origin=None,retry_binding=None):
    tasks=arguments.get('tasks') if isinstance(arguments,dict) else None
    if (not isinstance(arguments,dict) or set(arguments)!={'tasks'} or not isinstance(tasks,list) or not 2<=len(tasks)<=8
            or any(not isinstance(t,dict) or set(t)!={'agent_id','content'} or type(t['agent_id']) is not int
                or t['agent_id']<=0 or t['agent_id']==sender_id or not isinstance(t['content'],str)
                or not t['content'].strip() or len(t['content'])>4000 for t in tasks)
            or len({t['agent_id'] for t in tasks})!=len(tasks)):
        fail('bot_batch_invalid_request','Choose 2–8 different active Bots and a task of at most 4000 characters for each.')
    _,root_room,_,_=messages._send_guard(db,sender_room_id,root_room_id,root_id,sender_id,generation,retry_binding)
    owner_id=root_room.user_id
    # Validate the complete recipient set before any dispatch-free DM resolve.
    for task in tasks:bot_profile.owned_agent(db,owner_id,task['agent_id'])
    targets=[]
    for task in tasks:
        target=bot_profile.canonical_conversation(db,owner_id,task['agent_id'],active_only=True)
        rid=target.id if target else bot_profile.resolve_conversation(db,owner_id,task['agent_id'],BotConversationResolve(),retry_binding=retry_binding)['room_id']
        targets.append(rid)
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        from app.services.memory_context import require_locked
        require_locked(db,sender_room_id,sender_id,expected_memory_digest,execution_origin=execution_origin)
        _,root_room,_,sender=messages._send_guard(db,sender_room_id,root_room_id,root_id,sender_id,generation,retry_binding)
        key=sha256(json.dumps([root_id,sender_room_id,sender_id,generation,call_id],separators=(',',':')).encode()).hexdigest()
        old=db.query(BotBatch).filter_by(root_request_event_id=root_id,delivery_key=key).first()
        if old:
            if [(m.recipient_agent_id,m.content) for m in members(db,old)]!=[(t['agent_id'],t['content']) for t in tasks]:
                fail('bot_batch_delivery_changed','One batch delivery identity cannot carry different tasks.')
            value=receipt(db,old);db.commit();return value
        parent=messages._parent(db,parent_job_id,parent_kind,root_id)
        depth=parent.depth+1 if parent else 1
        count=db.query(func.count(BotMessage.message_id)).filter_by(root_request_event_id=root_id).scalar()
        count+=db.query(func.count(TeammateHandoff.handoff_id)).filter_by(root_request_event_id=root_id).scalar()
        budget=db.get(TeammateBudget,root_id)
        used_providers=max(provider_calls,budget.provider_calls if budget else 0)
        if depth>teammates.MAX_DEPTH or count+len(tasks)>teammates.MAX_HANDOFFS or used_providers>=teammates.MAX_PROVIDER_CALLS:
            fail('teammate_budget_exhausted','This request reached its existing shared collaboration budget.')
        agents=[bot_profile.owned_agent(db,owner_id,t['agent_id']) for t in tasks]
        if any(not bot_profile.is_personal_conversation(db,owner_id,t['agent_id'],rid) for t,rid in zip(tasks,targets)):fail()
        at=messages.now();existing=messages.root_rows(db,root_id)
        deadline=min([messages.aware(r.deadline_at) for r in existing],default=at+timedelta(seconds=messages.MAX_WAIT_SECONDS))
        root=scopes.root_scope(db,root_id,create=True,deadline=deadline)
        source=scopes.job_scope(db,parent,parent_kind) if parent else scopes.branch(db,root,sender_room_id,sender_id)
        if source.execution_room_id!=sender_room_id or source.agent_id!=sender_id or any(s.state!='active' for s in scopes.lineage(db,source)):fail()
        batch=BotBatch(batch_id=str(uuid4()),owner_id=owner_id,root_room_id=root_room_id,root_request_event_id=root_id,
            sender_room_id=sender_room_id,sender_agent_id=sender_id,sender_name=sender.name,parent_job_id=parent_job_id,
            parent_kind=parent_kind,source_scope_id=source.scope_id,content='Parallel delegated tasks; use only the actual linked Bot outcomes.',
            delivery_key=key,state='pending',reply_state='none',created_at=at,updated_at=at,deadline_at=deadline)
        request=stage_event(db,sender_room_id,ActorRef('agent',sender_id),'message',
            {'content':'Requested parallel work from '+', '.join(a.name for a in agents)+'.',
             'trigger_event_id':root_id,'bot_batch':metadata(batch,'request')},'bot-batch-request:'+batch.batch_id)
        batch.request_event_id=request.event_id;db.add(batch);db.flush()
        for index,(task,rid,agent) in enumerate(zip(tasks,targets,agents)):
            row=BotMessage(message_id=str(uuid4()),owner_id=owner_id,root_room_id=root_room_id,root_request_event_id=root_id,
                sender_room_id=sender_room_id,recipient_room_id=rid,sender_agent_id=sender_id,recipient_agent_id=agent.id,
                sender_name=sender.name,recipient_name=agent.name,content=task['content'],delivery_key=sha256((key+':'+str(index)).encode()).hexdigest(),
                parent_job_id=parent_job_id,parent_kind=parent_kind,depth=depth,state='pending',reply_state='none',
                batch_id=batch.batch_id,batch_index=index,created_at=at,updated_at=at,deadline_at=deadline)
            req=stage_event(db,sender_room_id,ActorRef('agent',sender_id),'message',{'content':row.content,'trigger_event_id':root_id,
                'bot_message':messages.metadata(row,'request'),'bot_batch':metadata(batch,'branch_request',index)},'bot-message-request:'+row.message_id)
            row.request_event_id=req.event_id
            event=stage_event(db,rid,ActorRef('system',None),'activity',{'category':'bot_message','status':'pending','content':row.content,
                'trigger_event_id':root_id,'source_ref':messages.event_ref(db,req.event_id),
                'bot_message':messages.metadata(row,'delivery'),'bot_batch':metadata(batch,'branch_delivery',index)},'bot-message-delivery:'+row.message_id)
            row.delivery_event_id=event.event_id;db.add(row);scopes.job_scope(db,row)
        if db.get(TeammateBudget,root_id) is None:db.add(TeammateBudget(root_request_event_id=root_id,provider_calls=provider_calls))
        value=receipt(db,batch);db.commit();return value
    except BaseException:db.rollback();raise


def refresh(db,root_id,*,interrupted=False):
    for row in reversed(root_rows(db,root_id)):
        if row.reply_state in {'delivered','failed','cancelled'}:continue
        scope=db.get(ExecutionScope,row.source_scope_id)
        chain=scopes.lineage(db,scope) if scope else []
        if interrupted or not chain or any(s.state=='revoked' for s in chain):
            row.state='interrupted' if interrupted else 'cancelled';row.reply_state='cancelled';row.failure_code='interrupted' if interrupted else 'cancelled'
        elif messages.aware(row.deadline_at)<=messages.now():
            row.state='failed';row.reply_state='failed';row.failure_code='batch_deadline_exceeded'
        elif any(s.state=='paused' for s in chain):continue
        elif row.reply_state not in {'pending','running'}:
            values=[member_value(db,m) for m in members(db,row)]
            if any(state in {'pending','running'} for state,_,_ in values):
                row.state='running' if any(state!='pending' for state,_,_ in values) else 'pending'
            else:
                good=sum(state=='completed' and result is not None and result.kind=='message' for state,_,result in values)
                partial=any(code or state!='completed' for state,code,_ in values)
                row.state='completed' if good==len(values) and not partial else 'partial' if good else 'failed'
                row.reply_state='pending';row.failure_code='partial_results' if partial and good else 'no_results' if not good else None
        row.updated_at=messages.now()
    db.flush()


def pending(db,root_id):
    return any(r.reply_state in {'none','pending','running'} for r in root_rows(db,root_id))


def next_job(db,root_room_id,root_id,*,exclude_rooms=()):
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        messages._root(db,root_room_id,root_id);refresh(db,root_id)
        for row in reversed(root_rows(db,root_id)):
            if row.reply_state!='pending' or row.sender_room_id in exclude_rooms:continue
            scope=db.get(ExecutionScope,row.source_scope_id)
            if any(s.state!='active' for s in scopes.lineage(db,scope)):continue
            room=messages._room(db,row.sender_room_id,row.owner_id)
            try:teammates._member(db,room,row.sender_agent_id,idle=True)
            except LLMFailure:continue
            changed=db.execute(update(BotBatch).where(BotBatch.batch_id==row.batch_id,BotBatch.reply_state=='pending').values(reply_state='running')).rowcount
            if changed!=1:continue
            row.reply_state='running'
            if not row.return_delivery_event_id:
                event=stage_event(db,row.sender_room_id,ActorRef('system',None),'activity',
                    {'category':'bot_batch','status':row.state,'trigger_event_id':root_id,'bot_batch':metadata(row,'return_delivery')},'bot-batch-return-delivery:'+row.batch_id)
                row.return_delivery_event_id=event.event_id
            row.updated_at=messages.now();db.commit()
            return {'kind':'bot_batch','message_id':row.batch_id,'phase':'return','room_id':row.sender_room_id,
                'agent_id':row.sender_agent_id,'round':max(1,db.get(RoomTrigger,root_id).current_round)}
        db.commit();return None
    except BaseException:db.rollback();raise


def resolve_execution_origin(db,job_id,phase,execution_room_id,agent_id,generation=None):
    row=db.get(BotBatch,job_id,populate_existing=True)
    if row is None or db.get(BotMessage,job_id) is not None or phase!='return' or row.reply_state!='running':fail()
    _,room,_=messages._root(db,row.root_room_id,row.root_request_event_id)
    if row.owner_id!=room.user_id or (row.sender_room_id,row.sender_agent_id)!=(execution_room_id,agent_id):fail()
    source=messages._room(db,execution_room_id,row.owner_id);_,run=teammates._member(db,source,agent_id)
    scope=db.get(ExecutionScope,row.source_scope_id)
    if (not scope or scope.execution_room_id!=execution_room_id or scope.agent_id!=agent_id
            or any(s.state!='active' or messages.aware(s.deadline_at)<=messages.now() for s in scopes.lineage(db,scope))
            or generation is not None and (run.state!='running' or run.generation!=generation or run.trigger_event_id!=row.root_request_event_id)):
        fail()
    parent=messages._parent(db,row.parent_job_id,row.parent_kind,row.root_request_event_id)
    results=[]
    for m in members(db,row):
        state,code,result=member_value(db,m)
        if state in {'pending','running'}:fail()
        target=db.get(Conversation,m.recipient_room_id)
        if not target or target.user_id!=row.owner_id:fail()
        if result and (result.room_id!=m.recipient_room_id or result.actor_type!='agent' or result.actor_id!=m.recipient_agent_id):fail()
        content=str(result.payload.get('content') or result.payload.get('message') or result.payload.get('status') or '') if result else None
        results.append({'member_index':m.batch_index,'message_id':m.message_id,'agent_id':m.recipient_agent_id,'name':m.recipient_name,
            'request_ref':messages.event_ref(db,m.request_event_id),'delivery_ref':messages.event_ref(db,m.delivery_event_id),
            'result_ref':messages.event_ref(db,result.event_id) if result else None,'content':content[:4000] if content is not None else None,
            'original_chars':len(content) if content is not None else 0,'included_chars':min(len(content),4000) if content is not None else 0,
            'omitted_chars':max(0,len(content)-4000) if content is not None else 0,'failure_code':code,'state':state})
    return {'owner_id':row.owner_id,'root_room_id':row.root_room_id,'root_request_event_id':row.root_request_event_id,
        'execution_room_id':execution_room_id,'agent_id':agent_id,'job_id':job_id,'phase':'return','generation':generation,
        'context_mode':'delegated' if row.parent_kind=='bot_message' else 'user_room',
        'task_content':parent.content if parent else row.content,'task_ref':messages.event_ref(db,row.request_event_id),
        'source_task_ref':messages.event_ref(db,parent.request_event_id) if parent else None,
        'root_ref':messages.event_ref(db,row.root_request_event_id),'peer_result_ref':None,'peer_result_content':None,
        'peer_result_original_chars':0,'peer_result_included_chars':0,'peer_result_omitted_chars':0,
        'failure_code':row.failure_code,'batch_id':row.batch_id,'batch_results':results}


def finish_job(db,job,event):
    row=db.get(BotBatch,job['message_id'])
    if not row or row.reply_state!='running' or (event.room_id,event.actor_id)!=(row.sender_room_id,row.sender_agent_id):return
    waiting=bool(event.kind=='activity' and event.payload.get('batch_waiting'))
    row.reply_state='delivered' if event.kind=='message' or waiting else 'failed';row.return_event_id=event.event_id;row.updated_at=messages.now()
    if event.kind!='message' and not waiting:row.failure_code=str(event.payload.get('code') or 'no_answer')[:64]
    for m in members(db,row):m.reply_state='delivered' if row.reply_state=='delivered' else 'failed'


def release_unclaimed(db,job):
    row=db.get(BotBatch,job['message_id'])
    if row and row.reply_state=='running':row.reply_state='pending'


def cancel(db,*,interrupted=False):
    tokens=[]
    for root_id, in db.query(BotBatch.root_request_event_id).distinct().all():
        running=[r for r in root_rows(db,root_id) if r.reply_state=='running']
        refresh(db,root_id,interrupted=interrupted)
        for row in running:
            if row.reply_state=='cancelled':tokens.append((row.sender_room_id,row.sender_agent_id))
    return tokens
