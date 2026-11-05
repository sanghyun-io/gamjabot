"""Owner-managed skill instructions; they never grant execution authority."""
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4
import json,re
from fastapi import HTTPException
from sqlalchemy import text
from app.models.models import Agent,Conversation
from app.models.collaboration import RoomEvent,RoomMembership
from app.models.owner_skills import OwnerSkill,OwnerSkillVersion,OwnerSkillBinding,OwnerSkillCommand
from app.services.request_inspector import build_request_inspector

MAX_ACTIVE_SKILLS=8
MAX_ACTIVE_CHARS=8000

def fail(status,code):raise HTTPException(status,{'code':code})
def now():return datetime.now(timezone.utc)
def stamp(value):return value.replace(tzinfo=timezone.utc).isoformat()
def dto(row):
    return {'version':1,'skill_id':row.skill_id,'revision':row.revision,'name':row.name,
        'description':row.description,'content':row.content,'active':row.active,'source':row.source,
        'package_provenance':row.package_provenance,
        'package_modified':bool(row.package_provenance and sha256(row.content.encode('utf-8')).hexdigest()!=row.package_provenance['original_content_digest']),
        'created_at':stamp(row.created_at),'updated_at':stamp(row.updated_at)}
def owned(db,owner_id,skill_id):
    row=db.get(OwnerSkill,skill_id)
    if not row or row.owner_id!=owner_id:fail(404,'skill_not_found')
    return row
def agent_owned(db,owner_id,agent_id):
    agent=db.get(Agent,agent_id)
    if not agent or agent.user_id!=owner_id or not agent.is_active:fail(404,'skill_agent_not_found')
    return agent
def listing(db,owner_id,q=''):
    if len(q)>120:fail(422,'skill_query_invalid')
    query=db.query(OwnerSkill).filter_by(owner_id=owner_id)
    if q:query=query.filter(OwnerSkill.name.contains(q,autoescape=True) | OwnerSkill.description.contains(q,autoescape=True))
    return {'version':1,'items':[dto(r) for r in query.order_by(OwnerSkill.name,OwnerSkill.skill_id).limit(200)]}
def versions(db,owner_id,skill_id,before_revision=None):
    owned(db,owner_id,skill_id)
    if before_revision is not None and before_revision<1:fail(422,'skill_version_cursor_invalid')
    query=db.query(OwnerSkillVersion).filter_by(skill_id=skill_id)
    if before_revision is not None:query=query.filter(OwnerSkillVersion.revision<before_revision)
    rows=query.order_by(OwnerSkillVersion.revision.desc()).limit(201).all()
    return {'version':1,'skill_id':skill_id,'versions':[r.snapshot for r in rows[:200]],
            'next_before_revision':rows[199].revision if len(rows)>200 else None}
def command(db,owner_id,key):
    validate_key(key)
    row=db.get(OwnerSkillCommand,(owner_id,key))
    if row is None:fail(404,'skill_command_not_found')
    return row.response
def validate_key(key):
    if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',key):fail(422,'skill_idempotency_key_invalid')
def _source(db,owner_id,value):
    agent_owned(db,owner_id,value.agent_id)
    inspector=build_request_inspector(db,owner_id,value.room_id,value.root_event_id)
    root=db.get(RoomEvent,value.root_event_id);result=db.get(RoomEvent,value.result_event_id)
    outcomes=inspector.get('outcomes',[])
    if (inspector['completion'].get('outcome_status')!='succeeded' or inspector.get('outcomes_truncated')
            or not root or root.room_id!=value.room_id or root.actor_type!='user' or root.actor_id!=owner_id or root.kind!='message'
            or not result or result.room_id!=value.room_id or result.actor_type!='agent' or result.actor_id!=value.agent_id
            or result.kind!='message' or not isinstance(result.payload,dict)
            or result.payload.get('trigger_event_id')!=root.event_id or result.seq<=root.seq
            or not any(item.get('event_id')==result.event_id and item.get('agent_id')==value.agent_id
                and item.get('kind')=='message' and item.get('phase')=='succeeded' for item in outcomes)):
        fail(409,'skill_source_not_completed')
    return {'room_id':value.room_id,'root_event_id':root.event_id,'root_seq':root.seq,
            'result_event_id':result.event_id,'result_seq':result.seq,'agent_id':value.agent_id}
def create(db,owner_id,value,key,*,draft=False,demonstration_source=None):
    validate_key(key)
    material={'draft':draft,'value':value.model_dump()}
    if demonstration_source is not None: material['demonstration_source']=demonstration_source
    digest=sha256(json.dumps(material,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        existing=db.get(OwnerSkillCommand,(owner_id,key))
        if existing:
            if existing.request_digest!=digest:fail(409,'skill_idempotency_conflict')
            result=existing.response;db.commit();return result
        if db.query(OwnerSkill).filter_by(owner_id=owner_id).count()>=200:fail(409,'skill_library_limit')
        source=demonstration_source if demonstration_source is not None else (_source(db,owner_id,value) if draft else None)
        when=now();row=OwnerSkill(skill_id=str(uuid4()),owner_id=owner_id,revision=1,name=value.name,
            description=value.description,content=value.content,active=not draft,source=source,created_at=when,updated_at=when)
        db.add(row);db.flush();result=dto(row)
        db.add(OwnerSkillVersion(skill_id=row.skill_id,revision=1,snapshot=result))
        db.add(OwnerSkillCommand(owner_id=owner_id,command_key=key,request_digest=digest,response=result))
        db.commit();return result
    except BaseException:db.rollback();raise

def create_demonstration(db,owner_id,value,key,source):
    # Only the owner-bound demonstration service constructs this source.
    return create(db,owner_id,value,key,draft=True,demonstration_source=source)

def projected(db,owner_id,agent_id):
    rows=(db.query(OwnerSkill,OwnerSkillBinding).join(OwnerSkillBinding,OwnerSkillBinding.skill_id==OwnerSkill.skill_id)
        .filter(OwnerSkill.owner_id==owner_id,OwnerSkill.active.is_(True),OwnerSkillBinding.owner_id==owner_id,
            OwnerSkillBinding.agent_id==agent_id,OwnerSkillBinding.enabled.is_(True))
        .order_by(OwnerSkill.skill_id).all())
    items=[{'skill_id':r.skill_id,'revision':r.revision,'binding_revision':b.revision,'name':r.name,
            'description':r.description,'content':r.content,'authority':'reference_only'} for r,b in rows]
    if (len(items)>MAX_ACTIVE_SKILLS or sum(len(x['name'])+len(x['description'])+len(x['content']) for x in items)>MAX_ACTIVE_CHARS
            or len(json.dumps({'owner_skills':items},ensure_ascii=False))>MAX_ACTIVE_CHARS):
        fail(409,'skill_context_limit')
    return items

def _require_room_budgets(db,owner_id,agent_id):
    from app.services.memory_context import snapshot
    from app.services.context_projection import CHECKPOINT_CHARS
    rooms=(db.query(RoomMembership.room_id).join(Conversation,Conversation.id==RoomMembership.room_id)
        .filter(RoomMembership.agent_id==agent_id,RoomMembership.is_active.is_(True),
                Conversation.user_id==owner_id,Conversation.is_active.is_(True)).all())
    for (room_id,) in rooms:
        memory=snapshot(db,room_id,agent_id)['memory']
        if memory and len(json.dumps(memory,ensure_ascii=False))>CHECKPOINT_CHARS:
            fail(409,'skill_context_limit')


def edit(db,owner_id,skill_id,value):
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        row=owned(db,owner_id,skill_id)
        if row.revision!=value.expected_revision:fail(409,'skill_revision_conflict')
        row.name=value.name;row.description=value.description;row.content=value.content;row.active=value.active
        row.revision+=1;row.updated_at=now();db.flush()
        for binding in db.query(OwnerSkillBinding).filter_by(owner_id=owner_id,skill_id=skill_id,enabled=True):
            projected(db,owner_id,binding.agent_id)
            _require_room_budgets(db,owner_id,binding.agent_id)
        result=dto(row);db.add(OwnerSkillVersion(skill_id=skill_id,revision=row.revision,snapshot=result))
        db.commit();return result
    except BaseException:db.rollback();raise

def bindings(db,owner_id,agent_id):
    agent_owned(db,owner_id,agent_id)
    rows=db.query(OwnerSkill).filter_by(owner_id=owner_id).order_by(OwnerSkill.name,OwnerSkill.skill_id).limit(200).all()
    result=[]
    for skill in rows:
        binding=db.get(OwnerSkillBinding,(owner_id,agent_id,skill.skill_id))
        result.append({'skill_id':skill.skill_id,'revision':binding.revision if binding else 0,'enabled':bool(binding and binding.enabled)})
    return {'version':1,'agent_id':agent_id,'items':result}
def enable(db,owner_id,agent_id,skill_id,value):
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        agent_owned(db,owner_id,agent_id);skill=owned(db,owner_id,skill_id)
        row=db.get(OwnerSkillBinding,(owner_id,agent_id,skill_id));revision=row.revision if row else 0
        if revision!=value.expected_revision:fail(409,'skill_binding_conflict')
        if value.enabled and not skill.active:fail(409,'skill_inactive')
        if not row:
            row=OwnerSkillBinding(owner_id=owner_id,agent_id=agent_id,skill_id=skill_id,revision=0,enabled=False);db.add(row)
        row.enabled=value.enabled;row.revision+=1;db.flush();projected(db,owner_id,agent_id)
        if value.enabled:_require_room_budgets(db,owner_id,agent_id)
        result={'version':1,'agent_id':agent_id,'skill_id':skill_id,'revision':row.revision,'enabled':row.enabled}
        db.commit();return result
    except BaseException:db.rollback();raise
