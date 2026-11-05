from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.models.voice_call import VoiceCall, VoiceCallTurn
from app.schemas.voice_call import VoiceCallCreate, VoiceCallTurnCreate
from app.services import events, voice_calls as vc
from app.services.model_features import output_request
from datetime import timedelta

router = APIRouter()

@router.get('/{room_id}/voice-calls')
def list_calls(room_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    vc.begin(db)
    try:
        vc.room_service.require_room(db, room_id, user.id, active=False)
        vc.reconcile(db, user.id, vc.epoch(request, user.id))
        calls = db.query(VoiceCall).filter_by(owner_id=user.id, room_id=room_id).order_by(VoiceCall.started_at.desc()).all()
        result = {'calls':[vc.detail(db, call) for call in calls]}
        db.commit(); return result
    except BaseException: db.rollback(); raise

@router.post('/{room_id}/voice-calls', status_code=201)
def create_call(room_id: int, body: VoiceCallCreate, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    vc.begin(db)
    try:
        current_epoch = vc.epoch(request, user.id)
        vc.reconcile(db, user.id, current_epoch)
        vc.room_service.require_room(db, room_id, user.id)
        vc.assert_personal(db, user.id, room_id, body.agent_id)
        if not current_epoch: raise HTTPException(409, {'code':'voice_owner_unavailable'})
        prior = db.get(VoiceCall, body.client_call_id)
        if prior:
            if (prior.owner_id, prior.room_id, prior.agent_id) != (user.id, room_id, body.agent_id):
                raise HTTPException(409, {'code':'voice_call_id_conflict'})
            result = vc.detail(db, prior); db.commit(); return result
        active = db.query(VoiceCall).filter_by(owner_id=user.id, status='active').first()
        if active:
            info = vc.detail(db, active)
            raise HTTPException(409, {'code':'active_voice_call', 'call':info})
        stamp = vc.now()
        call = VoiceCall(id=body.client_call_id, owner_id=user.id, room_id=room_id, agent_id=body.agent_id,
                         owner_epoch=current_epoch, status='active', started_at=stamp,
                         lease_expires_at=stamp + timedelta(seconds=vc.LEASE_SECONDS))
        db.add(call); db.flush(); result = vc.detail(db, call); db.commit(); return result
    except BaseException: db.rollback(); raise

@router.get('/{room_id}/voice-calls/{call_id}')
def get_call(room_id: int, call_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    vc.begin(db)
    try:
        vc.reconcile(db, user.id, vc.epoch(request, user.id))
        call = vc.require_call(db, user.id, room_id, call_id)
        result = vc.detail(db, call); db.commit(); return result
    except BaseException: db.rollback(); raise

@router.post('/{room_id}/voice-calls/{call_id}/heartbeat')
def heartbeat(room_id: int, call_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    vc.begin(db)
    try:
        vc.reconcile(db, user.id, vc.epoch(request, user.id))
        call = vc.require_call(db, user.id, room_id, call_id)
        if call.status == 'active':
            if not vc.bot_profile.is_personal_conversation(db, user.id, call.agent_id, room_id):
                call.status = 'interrupted'; call.ended_at = vc.now()
            else: call.lease_expires_at = vc.now() + timedelta(seconds=vc.LEASE_SECONDS)
        result = vc.detail(db, call); db.commit(); return result
    except BaseException: db.rollback(); raise

@router.post('/{room_id}/voice-calls/{call_id}/end')
def end_call(room_id: int, call_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    vc.begin(db)
    try:
        vc.reconcile(db, user.id, vc.epoch(request, user.id))
        call = vc.require_call(db, user.id, room_id, call_id)
        if call.status == 'active': call.status = 'ended'; call.ended_at = vc.now()
        result = vc.detail(db, call); db.commit(); return result
    except BaseException: db.rollback(); raise

@router.post('/{room_id}/voice-calls/{call_id}/turns', status_code=201)
async def add_turn(room_id: int, call_id: str, body: VoiceCallTurnCreate, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not body.text.strip(): raise HTTPException(422, {'code':'voice_text_required'})
    output_request(body.text, local_speech=bool(getattr(request.app.state.local_executor, 'speech_available', False) and getattr(getattr(request.app.state.local_executor, 'speech_client', None), 'available', False)))
    vc.begin(db)
    try:
        vc.reconcile(db, user.id, vc.epoch(request, user.id))
        call = vc.require_call(db, user.id, room_id, call_id)
        prior = db.get(VoiceCallTurn, (call_id, body.client_turn_id))
        if prior:
            event = db.get(events.RoomEvent, prior.user_event_id)
            if not event or event.payload.get('content') != body.text:
                raise HTTPException(409, {'code':'voice_turn_id_conflict'})
            result = vc.detail(db, call); db.commit(); return result
        if call.status != 'active': raise HTTPException(409, {'code':'voice_call_not_active'})
        vc.assert_personal(db, user.id, room_id, call.agent_id)
        vc.assert_no_pending(db, call)
        event = events.stage_event(db, room_id, events.ActorRef('user', user.id), 'message',
                                   {'content':body.text}, 'voice:'+call_id+':'+body.client_turn_id)
        trigger = request.app.state.scheduler._stage_trigger(db, event)
        if trigger.selected_agents != [call.agent_id]: raise HTTPException(409, {'code':'voice_agent_selection_changed'})
        db.add(VoiceCallTurn(call_id=call_id, client_turn_id=body.client_turn_id, user_event_id=event.event_id))
        db.flush(); result = vc.detail(db, call); db.commit()
    except BaseException: db.rollback(); raise
    request.app.state.event_hub.publish(room_id)
    await request.app.state.scheduler.schedule_user_event(room_id, event.event_id)
    return result
