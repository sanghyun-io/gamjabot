"""Read cross-room routing facts; model tools remain the only Agent send route."""
from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.services.bot_messages import list_room, retry, retry_preview
from app.schemas.bot_message import BotMessageRetryWrite

router = APIRouter()


@router.get('/conversations/{room_id}/creations')
def creations(room_id: int, db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    from app.services.bot_creation import list_room
    return list_room(db,owner.id,room_id)


@router.get('/creations/{creation_id}')
def creation(creation_id: str, db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    from app.services.bot_creation import get
    return get(db,owner.id,creation_id)


@router.get('/conversations/{room_id}/batches')
def batch_listing(room_id: int, db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    from app.services.bot_batches import list_room as batches
    return batches(db,room_id,owner.id)


@router.get('/conversations/{room_id}/scope-stops')
def latest_scope_stop(room_id: int, db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    from app.services.room_service import require_room
    from app.models.execution_scope import ScopeStopReceipt
    from app.services.execution_scopes import stop_public
    require_room(db,room_id,owner.id,active=False)
    row=db.query(ScopeStopReceipt).filter_by(room_id=room_id,owner_id=owner.id).order_by(
        ScopeStopReceipt.created_at.desc(),ScopeStopReceipt.stop_id.desc()).first()
    return {'version':1,'room_id':room_id,'latest':stop_public(row) if row else None}


@router.get('/scope-stops/{stop_id}')
def scope_stop(stop_id: str, db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    from fastapi import HTTPException
    from app.models.execution_scope import ScopeStopReceipt
    from app.services.execution_scopes import stop_public
    row=db.query(ScopeStopReceipt).filter_by(stop_id=stop_id,owner_id=owner.id).first()
    if not row:raise HTTPException(404,{'code':'scope_stop_not_found'})
    return stop_public(row)


@router.post('/scope-stops/{stop_id}/confirm')
async def confirm_scope_stop(stop_id: str, request: Request, owner: User = Depends(get_current_user)):
    from app.services.execution_scopes import confirm_stop
    result=await confirm_stop(request.app.state,stop_id,owner.id)
    request.app.state.event_hub.publish(result['room_id'])
    return result


@router.get('/messages/{message_id}/actions')
def message_actions(message_id: str, db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    from fastapi import HTTPException
    from app.models.bot_message import BotMessage
    from app.models.collaboration import ToolAction
    from app.models.models import Conversation
    from app.services.execution_scopes import public_binding
    from app.services.bot_messages import event_ref
    row = db.query(BotMessage).filter_by(message_id=message_id, owner_id=owner.id).first()
    if not row:
        raise HTTPException(404, {'code': 'bot_message_not_found'})
    actions = db.query(ToolAction).join(Conversation, Conversation.id == ToolAction.room_id).filter(
        ToolAction.trigger_event_id == row.root_request_event_id, Conversation.user_id == owner.id,
        ToolAction.execution_binding['root_request_event_id'].as_string() == row.root_request_event_id,
    ).order_by(ToolAction.requested_at, ToolAction.action_id).limit(512).all()
    keys = ('action_id','room_id','agent_id','trigger_event_id','family','target','expected_effect','arguments',
        'state','grant_id','requested_at','started_at','completed_at','review_id','approved_at','approval_owner_id')
    return {'version':1,'message_id':message_id,'root_ref':event_ref(db,row.root_request_event_id),
        'actions':[{**{key:getattr(a,key) for key in keys},'actor_type':'agent','actor_id':a.agent_id,
            'scope':a.approval_scope,'review_status':'stored' if a.review_id and a.review_snapshot else 'unavailable',
            **public_binding(db,a)} for a in actions]}


@router.get("/conversations/{room_id}/messages")
async def listing(room_id: int, request: Request, db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    value = list_room(db, room_id, owner.id)
    executor = request.app.state.local_executor
    if hasattr(executor, 'prepare_scoped_execution'):
        await executor.prepare_scoped_execution()
    ready = bool(getattr(executor, 'scoped_execution_ready', False))
    value['capabilities']['delegated_os_execution'] = ready
    for message in value['messages']:
        message['capabilities']['delegated_os_execution'] = ready
    return value


@router.get("/messages/{message_id}/retry")
def preview_retry(message_id: str, db: Session = Depends(get_db), owner: User = Depends(get_current_user)):
    return retry_preview(db, owner.id, message_id)


@router.post("/messages/{message_id}/retry")
async def retry_message(message_id: str, value: BotMessageRetryWrite, request: Request,
                        idempotency_key: str | None = Header(None), db: Session = Depends(get_db),
                        owner: User = Depends(get_current_user)):
    result = retry(db, owner.id, message_id, value, idempotency_key)
    request.app.state.event_hub.publish(result["root_room_id"])
    await request.app.state.scheduler.schedule_user_event(result["root_room_id"], result["retry_event_id"])
    return result
