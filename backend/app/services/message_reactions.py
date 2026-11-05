"""Owner-only message reactions, stored as immutable room activities."""
import re
from uuid import UUID
from fastapi import HTTPException
from sqlalchemy import text
from app.models.collaboration import RoomEvent
from app.models.models import Agent
from app.services import events,room_service

EMOJI=frozenset(('👍','❤️','😂','🎉','😮','👀'))
CATEGORY='message_reaction'

def fail(status,code):raise HTTPException(status,{'code':code})
def key_for(key):
    if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',key):fail(422,'reaction_idempotency_invalid')
    return 'reaction:'+key

def valid_id(value):
    try:return isinstance(value,str) and str(UUID(value))==value
    except (ValueError,AttributeError,TypeError):return False

def target(db,owner,room_id,event_id):
    room_service.require_room(db,room_id,owner)
    if not valid_id(event_id):fail(404,'reaction_target_not_found')
    event=db.get(RoomEvent,event_id)
    if not event or event.room_id!=room_id or event.kind!='message' or not isinstance(event.payload,dict) or not isinstance(event.payload.get('content'),str):
        fail(404,'reaction_target_not_found')
    if event.actor_type=='user' and event.actor_id==owner:return event
    if event.actor_type=='agent' and event.actor_id is not None:
        agent=db.get(Agent,event.actor_id)
        if agent and agent.user_id==owner:return event
    fail(404,'reaction_target_not_found')

def current(db,owner,room_id,event_id):
    return (db.query(RoomEvent).filter(RoomEvent.room_id==room_id,RoomEvent.kind=='activity',
        RoomEvent.actor_type=='user',RoomEvent.actor_id==owner,
        RoomEvent.payload['category'].as_string()==CATEGORY,
        RoomEvent.payload['version'].as_integer()==1,
        RoomEvent.payload['target_event_id'].as_string()==event_id)
        .order_by(RoomEvent.seq.desc()).first())

def response(db,owner,room_id,event_id,applied=None,replayed=False):
    latest=current(db,owner,room_id,event_id)
    result={'version':1,'room_id':room_id,'target_event_id':event_id,'owner_id':owner,
        'current_seq':latest.seq if latest else 0,'current_event_id':latest.event_id if latest else None,
        'emoji':latest.payload['emoji'] if latest else None,'replayed':replayed}
    if applied:result.update(applied_event_id=applied.event_id,applied_seq=applied.seq)
    return result

def read(db,owner,room_id,event_id):
    target(db,owner,room_id,event_id)
    return response(db,owner,room_id,event_id)

def receipt(db,owner,room_id,event_id,key):
    target(db,owner,room_id,event_id);stored=key_for(key)
    prior=db.query(RoomEvent).filter_by(room_id=room_id,idempotency_key=stored).first()
    if not (prior and prior.kind=='activity' and prior.actor_type=='user' and prior.actor_id==owner
            and isinstance(prior.payload,dict) and prior.payload.get('category')==CATEGORY
            and type(prior.payload.get('version')) is int and prior.payload['version']==1
            and prior.payload.get('target_event_id')==event_id):fail(404,'reaction_command_not_found')
    return response(db,owner,room_id,event_id,prior,True)

def write(db,owner,room_id,event_id,value,key):
    stored=key_for(key)
    payload={'category':CATEGORY,'version':1,'target_event_id':event_id,'emoji':value.emoji,'expected_seq':value.expected_seq}
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        target(db,owner,room_id,event_id)
        prior=db.query(RoomEvent).filter_by(room_id=room_id,idempotency_key=stored).first()
        if prior:
            if prior.kind!='activity' or prior.actor_type!='user' or prior.actor_id!=owner or prior.payload!=payload:
                fail(409,'reaction_idempotency_conflict')
            result=response(db,owner,room_id,event_id,prior,True);db.commit();return result,False
        latest=current(db,owner,room_id,event_id)
        if (latest.seq if latest else 0)!=value.expected_seq:fail(409,'reaction_stale')
        event=events.stage_event(db,room_id,events.ActorRef('user',owner),'activity',payload,stored)
        result=response(db,owner,room_id,event_id,event,False);db.commit();return result,True
    except BaseException:db.rollback();raise
