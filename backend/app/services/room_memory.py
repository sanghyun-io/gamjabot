"""Explicit Owner memory commands. Changes and their receipts commit atomically."""
from datetime import datetime, timezone
from uuid import uuid4
import json
from fastapi import HTTPException
from sqlalchemy import text
from app.models.models import Agent, Conversation, Project, User
from app.models.collaboration import RoomMembership, RoomAgentRun, RoomEvent, RoomTrigger, ToolAction
from app.models.room_memory import RoomMemoryEntry, RoomMemoryState, RoomMemoryAgentState, RoomMemoryCommand
from app.schemas.room_memory import MemoryWrite, MemoryEdit, MemoryDelete, MemoryLegacy
from app.services.memory_context import snapshot, digest
from app.services.events import ActorRef, stage_event
from app.services.result_content import safe_url
from app.services.peer_reply import canonical_id


def fail(code, status=422):
    raise HTTPException(status, {'code':'memory_'+code})


def room_access(db,owner,room,active=False):
    user=db.get(User,owner)
    if not user or not user.is_active:fail('not_found',404)
    result = db.query(Conversation).join(Project,Project.id==Conversation.project_id).filter(
        Conversation.id==room,Conversation.user_id==owner,Project.user_id==owner).first()
    if not result:fail('not_found',404)
    if active and (not result.is_active or not db.get(Project,result.project_id).is_active):fail('read_only',409)
    return result


def target(db,owner,room,agent,active=True):
    a=db.get(Agent,agent);m=db.get(RoomMembership,(room,agent))
    if not a or a.user_id!=owner or not m or active and (not a.is_active or not m.is_active):fail('target_not_found',404)
    return m


def state_for(db,room):
    s=db.get(RoomMemoryState,room)
    if not s:
        s=RoomMemoryState(room_id=room,revision=0,common_context_after_seq=0);db.add(s);db.flush()
    return s


def local_for(db,room,agent):
    s=db.get(RoomMemoryAgentState,(room,agent))
    if not s:
        s=RoomMemoryAgentState(room_id=room,agent_id=agent,revision=0,context_after_seq=0,legacy_memory_disabled=False);db.add(s);db.flush()
    return s


def require_idle(db,room):
    if db.query(RoomTrigger).filter(RoomTrigger.room_id==room,RoomTrigger.state.in_(('pending','running'))).first():fail('room_busy',409)
    for run in db.query(RoomAgentRun).filter_by(room_id=room):
        if run.state not in {'idle','paused','stopped'} or run.active_tool or (run.pending_task or {}).get('work_wave'):
            fail('room_busy',409)
    # An uncertain effect is not silently acknowledged by editing memory.
    if db.query(ToolAction).filter(ToolAction.room_id==room,ToolAction.state.notin_(('completed','failed','denied','cancelled'))).first():fail('room_busy',409)


def entry(db,room,mid):
    if not canonical_id(mid):fail('invalid_id')
    e=db.query(RoomMemoryEntry).filter_by(room_id=room,memory_id=mid).first()
    if not e:fail('not_found',404)
    return e


def stamp(t):
    return t.replace(tzinfo=timezone.utc).isoformat() if t and t.tzinfo is None else t.isoformat() if t else None


def dto(e):
    return {k:getattr(e,k) for k in ('memory_id','room_id','category','content','scope','target_agent_id','source_kind',
            'source_event_id','source_seq','source_url','source_snapshot','revision','created_by','updated_by')} | {
        k:stamp(getattr(e,k)) for k in ('created_at','updated_at','deleted_at')}


def listing(db,owner,room):
    room_access(db,owner,room)
    s=db.get(RoomMemoryState,room);agents=[]
    for m in db.query(RoomMembership).filter_by(room_id=room).order_by(RoomMembership.agent_id):
        if not db.query(Agent).filter_by(id=m.agent_id,user_id=owner).first():continue
        snap=snapshot(db,room,m.agent_id);local=db.get(RoomMemoryAgentState,(room,m.agent_id));run=db.get(RoomAgentRun,(room,m.agent_id))
        raw_legacy=dict(run.memory or {}) if run else {}
        agents.append({'agent_id':m.agent_id,'is_active':bool(m.is_active),'revision':local.revision if local else 0,
            'context_after_seq':snap['binding']['context_after_seq'],'legacy_disabled':bool(local and local.legacy_memory_disabled),
            'legacy':raw_legacy,'legacy_digest':digest(raw_legacy)})
    return {'room_id':room,'revision':s.revision if s else 0,'common_context_after_seq':s.common_context_after_seq if s else 0,
            'items':[dto(e) for e in db.query(RoomMemoryEntry).filter_by(room_id=room,deleted_at=None).order_by(RoomMemoryEntry.memory_id)],'agents':agents}


def detail(db,owner,room,mid):
    room_access(db,owner,room);return dto(entry(db,room,mid))


def assign(db,owner,room,e,v):
    content=v.content.strip()
    if not content:fail('content_empty')
    if v.scope=='agent':target(db,owner,room,v.target_agent_id)
    source=v.source;source_snapshot={}
    if source.kind=='event':
        event=db.query(RoomEvent).filter_by(room_id=room,event_id=source.event_id,seq=source.seq).first()
        if not event:fail('source_not_found',404)
        # A summary is a reference, never an automatic instruction or verified fact.
        source_snapshot={'actor_type':event.actor_type,'actor_id':event.actor_id,'name':event.actor_name_snapshot}
    if source.kind=='link' and not safe_url(source.url):fail('source_url_invalid')
    e.category=v.category;e.content=content;e.scope=v.scope;e.target_agent_id=v.target_agent_id
    e.source_kind=source.kind;e.source_event_id=source.event_id;e.source_seq=source.seq;e.source_url=source.url;e.source_snapshot=source_snapshot


def mutate(db,owner,room,operation,value,key,mid=None,agent=None):
    if not isinstance(key,str) or not 1<=len(key)<=200 or not key.isprintable():fail('idempotency_invalid')
    classes={'add':MemoryWrite,'edit':MemoryEdit,'delete':MemoryDelete,'legacy':MemoryLegacy}
    if operation not in classes or not isinstance(value,classes[operation]):fail('command_invalid')
    request_hash=digest([room,operation,mid,agent,value.model_dump()]);db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        room_access(db,owner,room)  # Fresh access before even a receipt replay.
        if mid is not None:e=entry(db,room,mid)
        prior=db.get(RoomMemoryCommand,(owner,key))
        if prior:
            if prior.request_digest!=request_hash or prior.room_id!=room:fail('idempotency_conflict',409)
            result={**prior.response,'replayed':True};db.commit();return result
        room_access(db,owner,room,active=True);require_idle(db,room);state=state_for(db,room)
        if state.revision!=value.expected_room_revision:fail('revision_conflict',409)
        now=datetime.now(timezone.utc);old_scope=None;old_target=None;legacy_agent=None
        if operation in {'edit','delete'}:
            if e.deleted_at is not None or e.revision!=value.expected_revision:fail('revision_conflict',409)
            old_scope,old_target=e.scope,e.target_agent_id
        if operation=='legacy':
            target(db,owner,room,agent,active=False);local=local_for(db,room,agent);run=db.get(RoomAgentRun,(room,agent))
            legacy=dict(run.memory or {}) if run else {}
            if local.revision!=value.expected_agent_revision or local.legacy_memory_disabled or digest(legacy)!=value.expected_legacy_digest:
                fail('revision_conflict',409)
            if not legacy:fail('legacy_empty')
            local.legacy_memory_disabled=True;legacy_agent=agent
            if value.operation=='replace':
                v=value.replacement
                if v.expected_room_revision!=state.revision or v.scope!='agent' or v.target_agent_id!=agent:fail('legacy_replacement_scope')
            else:v=None
        else:v=value
        if operation=='add' or operation=='legacy' and v is not None:
            e=RoomMemoryEntry(memory_id=str(uuid4()),room_id=room,revision=1,created_by=owner,updated_by=owner,created_at=now,updated_at=now)
            assign(db,owner,room,e,v);db.add(e)
        elif operation=='edit':assign(db,owner,room,e,value);e.revision+=1;e.updated_at=now;e.updated_by=owner
        elif operation=='delete':e.deleted_at=now;e.revision+=1;e.updated_at=now;e.updated_by=owner
        state.revision+=1;db.flush()
        if db.query(RoomMemoryEntry).filter_by(room_id=room,deleted_at=None).count()>64:fail('too_many_entries')
        audit=stage_event(db,room,ActorRef('user',owner),'activity',{'room_memory':{'operation':operation,'memory_id':e.memory_id if operation!='legacy' or v is not None else None,'revision':state.revision}},'memory:'+digest([owner,key]))
        cut=operation in {'edit','delete','legacy'}
        if cut:
            scopes=[(old_scope,old_target)]
            if operation in {'edit','delete'}:scopes.append((e.scope,e.target_agent_id))
            if any(s=='room' for s,a in scopes):state.common_context_after_seq=max(state.common_context_after_seq,audit.seq)
            ids={a for s,a in scopes if s=='agent'}|({legacy_agent} if legacy_agent is not None else set())
            for a in ids:
                local=local_for(db,room,a);local.revision+=1;local.context_after_seq=max(local.context_after_seq,audit.seq)
        db.flush()
        # Validate every current participant; no truncated/implicitly omitted memory.
        for member in db.query(RoomMembership).filter_by(room_id=room,is_active=True):
            snap=snapshot(db,room,member.agent_id)
            if len(json.dumps(snap['memory'],ensure_ascii=False))>8000:fail('context_too_large')
        response={'room_id':room,'memory_id':e.memory_id if operation!='legacy' or v is not None else None,
                  'revision':state.revision,'entry_revision':e.revision if operation!='legacy' or v is not None else None,
                  'activity_event_id':audit.event_id,'activity_seq':audit.seq,'replayed':False}
        db.add(RoomMemoryCommand(owner_id=owner,idempotency_key=key,room_id=room,request_digest=request_hash,response=response,created_at=now))
        db.commit();return response
    except BaseException:
        db.rollback();raise
