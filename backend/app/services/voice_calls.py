"""Text event backed call lifecycle; microphone and audio remain on the client."""
from datetime import datetime
from fastapi import HTTPException
from sqlalchemy import text
from app.models.collaboration import RoomEvent
from app.models.voice_call import VoiceCall, VoiceCallTurn
from app.services import bot_profile, events, room_service
from app.services.request_inspector import build_request_inspector

LEASE_SECONDS = 90

def now():
    return datetime.utcnow()

def epoch(request, owner_id):
    provider = getattr(request.app.state, 'owner_epoch_provider', None)
    return provider.current(owner_id) if provider is not None else None

def begin(db):
    db.rollback()
    db.execute(text('BEGIN IMMEDIATE'))

def reconcile(db, owner_id, current_epoch):
    stamp = now()
    for call in db.query(VoiceCall).filter_by(owner_id=owner_id, status='active').all():
        if not current_epoch or call.owner_epoch != current_epoch or call.lease_expires_at <= stamp:
            call.status = 'interrupted'
            call.ended_at = min(stamp, call.lease_expires_at)
    db.flush()

def require_call(db, owner_id, room_id, call_id):
    room_service.require_room(db, room_id, owner_id, active=False)
    call = db.get(VoiceCall, call_id)
    if call is None or call.owner_id != owner_id or call.room_id != room_id:
        raise HTTPException(404, {'code':'voice_call_not_found'})
    return call

def ref(event):
    return {'event_id':event.event_id, 'seq':event.seq}

def detail(db, call):
    stamp = call.ended_at or now()
    turns = []
    rows = (db.query(VoiceCallTurn, RoomEvent).join(RoomEvent, RoomEvent.event_id == VoiceCallTurn.user_event_id)
            .filter(VoiceCallTurn.call_id == call.id).order_by(RoomEvent.seq).all())
    for turn, user_event in rows:
        inspection = build_request_inspector(db, call.owner_id, call.room_id, user_event.event_id)
        completion = inspection['completion']
        phase = completion['state']
        if phase == 'completed' and completion['outcome_status'] == 'failed': phase = 'failed'
        elif phase == 'completed' and completion['outcome_status'] != 'succeeded': phase = 'unknown'
        elif phase not in {'queued','running','waiting_approval','completed','failed','cancelled','interrupted'}: phase = 'unknown'
        replies = (db.query(RoomEvent).filter(RoomEvent.room_id == call.room_id, RoomEvent.actor_type == 'agent',
                    RoomEvent.actor_id == call.agent_id, RoomEvent.kind == 'message',
                    RoomEvent.payload['trigger_event_id'].as_string() == user_event.event_id)
                    .order_by(RoomEvent.seq).all())
        reply = replies[-1] if replies else None
        turns.append({'client_turn_id':turn.client_turn_id, 'text':user_event.payload['content'],
                      'user_event_ref':ref(user_event), 'phase':phase,
                      'reply_event_ref':ref(reply) if reply else None,
                      'reply_text':reply.payload.get('content') if reply else None,
                      'replies':[{'event_ref':ref(item), 'text':item.payload.get('content')} for item in replies]})
    def iso(value): return value.isoformat() + 'Z' if value else None
    return {'id':call.id, 'owner_id':call.owner_id, 'room_id':call.room_id, 'agent_id':call.agent_id,
            'status':call.status, 'started_at':iso(call.started_at), 'ended_at':iso(call.ended_at),
            'duration_ms':max(0,int((stamp-call.started_at).total_seconds()*1000)), 'turns':turns}

def assert_personal(db, owner_id, room_id, agent_id):
    if not bot_profile.is_personal_conversation(db, owner_id, agent_id, room_id):
        raise HTTPException(409, {'code':'voice_personal_room_unavailable'})

def assert_no_pending(db, call):
    last = (db.query(VoiceCallTurn, RoomEvent).join(RoomEvent, RoomEvent.event_id == VoiceCallTurn.user_event_id)
            .filter(VoiceCallTurn.call_id == call.id).order_by(RoomEvent.seq.desc()).first())
    if last:
        inspection = build_request_inspector(db, call.owner_id, call.room_id, last[1].event_id)
        completion = inspection['completion']
        if completion['state'] in {'queued','running','waiting_approval','unknown'} or completion['outcome_status'] == 'unknown':
            raise HTTPException(409, {'code':'voice_turn_pending', 'user_event_ref':ref(last[1])})
