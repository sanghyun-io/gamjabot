"""Preview and explicitly send one owner-reviewed Skill reference."""
import hashlib
from fastapi import APIRouter,Depends,Header,HTTPException,Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.api.routes.library_file_questions import _require_no_reply_draft
from app.core.database import get_db
from app.models.models import User
from app.models.collaboration import RoomEvent,RoomTrigger
from app.schemas.event import RoomEventResponse
from app.schemas.skill_input import SkillInputPreview,SkillInputSend
from app.services import skill_input,room_service
from app.services.events import ActorRef,stage_event

router=APIRouter()
HEADERS={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}
def key(value):
    if not isinstance(value,str) or not 1<=len(value)<=200 or not value.isprintable():
        raise HTTPException(422,{'code':'skill_input_invalid_key'})
    return 'skill-input:'+hashlib.sha256(value.encode()).hexdigest()

def public(value,prepared):
    return {'basis':prepared['basis'],'proof':prepared['proof'],'skill_id':value.skill_id,'skill_revision':value.skill_revision,
            'binding_revision':value.binding_revision,'bot_id':value.bot_id,'question':value.question,
            'variables':prepared['variables'],'expanded_reference':prepared['expanded_reference'],'target':prepared['target']}

@router.post('/{room_id}/skill-use/preview')
def preview(room_id:int,value:SkillInputPreview,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    _require_no_reply_draft(db,owner.id,room_id)
    return JSONResponse(public(value,skill_input.prepare(db,owner.id,room_id,value,getattr(request.app.state,'owner_epoch_provider',None))),headers=HEADERS)

@router.post('/{room_id}/skill-use')
async def send(room_id:int,value:SkillInputSend,request:Request,token:str|None=Header(None,alias='Idempotency-Key'),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    command=key(token); room_service.require_room(db,room_id,owner.id)
    prior=db.query(RoomEvent).filter_by(room_id=room_id,idempotency_key=command).first()
    if prior:
        expected={'version':1,'bot_id':value.bot_id,'skill_id':value.skill_id,'skill_revision':value.skill_revision,
                  'binding_revision':value.binding_revision,'values':value.values,'basis':value.basis}
        if (prior.actor_type!='user' or prior.actor_id!=owner.id or prior.kind!='message' or prior.payload.get('content')!=value.question
                or not skill_input.valid_context(prior.payload.get('skill_context'),value.question,owner=owner.id,room=room_id)
                or {k:v for k,v in prior.payload['skill_context'].items() if k!='proof'}!=expected):
            raise HTTPException(409,{'code':'skill_input_idempotency_conflict'})
        return JSONResponse(RoomEventResponse.model_validate(prior).model_dump(mode='json'),headers=HEADERS)
    epochs=getattr(request.app.state,'owner_epoch_provider',None)
    _require_no_reply_draft(db,owner.id,room_id)
    prepared=skill_input.prepare(db,owner.id,room_id,value,epochs)
    if prepared['basis']!=value.basis:raise HTTPException(409,{'code':'skill_input_changed'})
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        _require_no_reply_draft(db,owner.id,room_id)
        if skill_input.prepare(db,owner.id,room_id,value,epochs)['basis']!=value.basis:
            raise HTTPException(409,{'code':'skill_input_changed'})
        epoch=epochs.current(owner.id) if epochs else None
        if not epoch or skill_input.files.digest(epoch.encode())!=prepared['proof']['owner_epoch_sha256']:
            raise HTTPException(409,{'code':'skill_input_changed'})
        ctx={'version':1,'bot_id':value.bot_id,'skill_id':value.skill_id,'skill_revision':value.skill_revision,
             'binding_revision':value.binding_revision,'values':value.values,'basis':value.basis,'proof':prepared['proof']}
        event=stage_event(db,room_id,ActorRef('user',owner.id),'message',{'content':value.question,'skill_context':ctx},command)
        # Queue admission and the reviewed message commit as one transaction;
        # restart must never discover an unqueued Skill request and replay it.
        request.app.state.scheduler._stage_trigger(db,event)
        db.commit();db.refresh(event);event_id=event.event_id;duplicate=event._duplicate
    except BaseException:db.rollback();raise
    if not duplicate:
        request.app.state.event_hub.publish(room_id)
        await request.app.state.scheduler.schedule_user_event(room_id,event_id)
    elif db.get(RoomTrigger,event_id) is None:
        await request.app.state.scheduler.schedule_user_event(room_id,event_id)
    return JSONResponse(RoomEventResponse.model_validate(event).model_dump(mode='json'),status_code=200 if duplicate else 201,headers=HEADERS)

@router.get('/{room_id}/skill-use/receipt')
def receipt(room_id:int,token:str|None=Header(None,alias='Idempotency-Key'),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    room_service.require_room(db,room_id,owner.id,active=False)
    event=db.query(RoomEvent).filter_by(room_id=room_id,idempotency_key=key(token),actor_type='user',actor_id=owner.id,kind='message').first()
    if not event or not isinstance(event.payload,dict) or not skill_input.valid_context(event.payload.get('skill_context'),event.payload.get('content'),owner=owner.id,room=room_id):
        raise HTTPException(404,{'code':'skill_input_receipt_not_found'})
    return JSONResponse(RoomEventResponse.model_validate(event).model_dump(mode='json'),headers=HEADERS)
