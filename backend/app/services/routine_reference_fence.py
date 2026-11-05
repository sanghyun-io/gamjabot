"""Transaction-local routine reference rejection fence; never stores message text."""
import hashlib,json
from fastapi import HTTPException
from sqlalchemy import text
from app.models.collaboration import RoomEvent

REJECTIONS={'routine_reference_changed','routine_reference_room_changed','routine_not_found','routine_agent_not_found'}

def rejection_status(code):
    return 404 if code in {'routine_not_found','routine_agent_not_found'} else 409

def digest(payload):
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')).hexdigest()

def existing(db,owner,room,key,payload):
    row=db.execute(text('SELECT request_digest,code FROM routine_reference_fences WHERE owner_id=:owner AND room_id=:room AND idempotency_key=:key'),{'owner':owner,'room':room,'key':key}).first()
    if row is None:return None
    if row.request_digest != digest(payload):raise HTTPException(409,{'code':'routine_reference_key_conflict'})
    return row.code

def insert(db,owner,room,key,payload,code):
    if code not in REJECTIONS:raise ValueError('unfenceable rejection')
    db.execute(text('INSERT INTO routine_reference_fences (owner_id,room_id,idempotency_key,request_digest,code) VALUES (:owner,:room,:key,:digest,:code)'),{'owner':owner,'room':room,'key':key,'digest':digest(payload),'code':code})

def resolve(db,owner,room,key,payload):
    """Caller owns BEGIN IMMEDIATE, room check and commit; never creates an event."""
    previous=db.query(RoomEvent).filter_by(room_id=room,idempotency_key=key).first()
    if previous:
        old=dict(previous.payload);old.pop('legacy_message_id',None)
        if previous.actor_type!='user' or previous.actor_id!=owner or previous.kind!='message' or old!=payload:
            raise HTTPException(409,{'code':'routine_reference_key_conflict'})
        return {'version':1,'state':'accepted','event_id':previous.event_id,'seq':previous.seq}
    code=existing(db,owner,room,key,payload)
    if code:return {'version':1,'state':'rejected','code':code}
    from app.services.routine_persistence import validate_reference
    try:validate_reference(db,owner,room,payload['routine_ref'])
    except HTTPException as exc:
        code=exc.detail.get('code') if isinstance(exc.detail,dict) else None
        if code not in REJECTIONS:raise
        insert(db,owner,room,key,payload,code)
        return {'version':1,'state':'rejected','code':code}
    return {'version':1,'state':'pending'}
