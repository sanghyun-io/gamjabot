"""Explicit Owner-shared facts, never copied into a Bot's private store."""
from datetime import datetime,timezone
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import text
from app.models.owner_common_memory import OwnerCommonState,OwnerCommonEntry,OwnerCommonCommand
from app.models.models import Agent
from app.services import bot_memory
from app.services.bot_messages import event_ref


def fail(code,status=409):raise HTTPException(status,{'code':'owner_common_'+code})
def stamp(v):return v.replace(tzinfo=timezone.utc).isoformat() if v else None

def dto(row):
    return {'fact_id':row.fact_id,'kind':row.kind,'content':row.content,'revision':row.revision,
        'origin':{'agent_id':row.origin_agent_id,'source_kind':row.origin_kind,'room_id':row.origin_room_id,
                  'event_id':row.origin_event_id,'seq':row.origin_seq},'visibility':'owner_bots',
        'created_at':stamp(row.created_at),'updated_at':stamp(row.updated_at)}

def listing(db,owner):
    state=db.get(OwnerCommonState,owner)
    rows=db.query(OwnerCommonEntry).filter_by(owner_id=owner,deleted_at=None).order_by(OwnerCommonEntry.kind).all()
    return {'version':1,'scope':'owner_common','revision':state.revision if state else 0,'items':[dto(r) for r in rows]}

def mutate_locked(db,owner,operation,args,key,*,natural_origin=None):
    if not isinstance(key,str) or not 1<=len(key)<=200 or not key.isprintable():fail('idempotency_invalid',422)
    digest=bot_memory._digest([operation,args,natural_origin]);prior=db.get(OwnerCommonCommand,(owner,key))
    if prior:
        if prior.request_digest!=digest:fail('idempotency_conflict')
        return {**prior.response,'replayed':True}
    state=db.get(OwnerCommonState,owner);revision=state.revision if state else 0
    expected=args.get('expected_scope_revision')
    if expected is not None and (type(expected) is not int or expected!=revision):fail('revision_conflict')
    kind=args.get('kind');fact_id=args.get('fact_id')
    if operation=='add':
        if kind not in ('name','timezone'):fail('invalid',422)
        if db.query(OwnerCommonEntry).filter_by(owner_id=owner,kind=kind,deleted_at=None).first():fail('kind_exists')
        origin=natural_origin or args.get('origin')
        if not isinstance(origin,dict) or type(origin.get('agent_id')) is not int:fail('origin_invalid',422)
        bot_memory._agent(db,owner,origin['agent_id'],active=True)
        if natural_origin is None and (set(origin)!={'agent_id','source_kind'} or origin['source_kind']!='manual'):fail('origin_invalid',422)
        row=OwnerCommonEntry(fact_id=str(uuid4()),owner_id=owner,kind=kind,origin_agent_id=origin['agent_id'],
            origin_kind='event' if natural_origin else 'manual',origin_room_id=origin.get('room_id'),
            origin_event_id=origin.get('event_id'),origin_seq=origin.get('seq'),revision=1,
            created_at=datetime.now(timezone.utc),updated_at=datetime.now(timezone.utc))
        db.add(row)
    else:
        row=db.query(OwnerCommonEntry).filter_by(owner_id=owner,fact_id=fact_id,deleted_at=None).first()
        if not row:fail('not_found',404)
        expected_entry=args.get('expected_revision')
        if expected_entry is not None and (type(expected_entry) is not int or row.revision!=expected_entry):fail('revision_conflict')
        row.revision+=1;row.updated_at=datetime.now(timezone.utc)
    if operation in ('add','edit'):
        content=args.get('content')
        if not isinstance(content,str) or not 1<=len(content.strip())<=2000:fail('invalid',422)
        row.content=content.strip()
    elif operation=='delete':row.deleted_at=datetime.now(timezone.utc)
    else:fail('invalid',422)
    if state is None:state=OwnerCommonState(owner_id=owner,revision=0);db.add(state)
    state.revision+=1;db.flush()
    if operation in ('edit','delete'):
        from app.services.memory_policy import advance_floors
        agents=[v for v, in db.query(Agent.id).filter_by(user_id=owner).all()]
        advance_floors(db,owner,agents)
    db.flush()
    if operation!='delete':
        for agent_id, in db.query(Agent.id).filter_by(user_id=owner,is_active=True).all():
            bot_memory._budget(db,owner,agent_id,None)
    response={'version':1,'scope':'owner_common','fact_id':row.fact_id,'kind':row.kind,'revision':state.revision,
        'entry_revision':row.revision,'origin':dto(row)['origin'],'operation':operation,
        'root_ref':event_ref(db,natural_origin['event_id']) if natural_origin else None,'result_ref':None,'replayed':False}
    db.add(OwnerCommonCommand(owner_id=owner,idempotency_key=key,request_digest=digest,response=response,
        created_at=datetime.now(timezone.utc)));db.flush()
    return response

def mutate(db,owner,operation,value,key,fact_id=None):
    args=value.model_dump();args['fact_id']=fact_id
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        result=mutate_locked(db,owner,operation,args,key);db.commit();return result
    except BaseException:db.rollback();raise


def remove_origin(db,owner,agent_id):
    """Soft-delete shared facts when their original Bot is deleted."""
    rows=db.query(OwnerCommonEntry).filter_by(owner_id=owner,origin_agent_id=agent_id,deleted_at=None).all()
    if not rows:return
    state=db.get(OwnerCommonState,owner)
    if state is None:raise RuntimeError('Owner common state is unavailable')
    at=datetime.now(timezone.utc)
    for row in rows:row.deleted_at=at;row.updated_at=at;row.revision+=1;state.revision+=1
    db.flush()
    from app.services.memory_policy import advance_floors
    agents=[v for v, in db.query(Agent.id).filter_by(user_id=owner).all()]
    advance_floors(db,owner,agents)
