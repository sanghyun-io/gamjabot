from app.services import execution_scopes
"""
Conversation API routes
"""
import asyncio
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, Response, status, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy import func, update, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from typing import List, Literal
import json

from app.core.database import get_db
from app.api.routes.auth import get_current_user, user_from_token
from app.core.local_auth import authorize_ws
from app.models.models import User, Conversation, Project, Message
from app.models.collaboration import RoomDraft, RoomEvent, RoomReadState, RoomScheduleSettings
from app.schemas.conversation import (ConversationCloneWrite, ConversationCreate, ConversationResponse,
                                      ConversationTitleWrite, MessageCreate, MessageResponse,
                                      RoomDraftResponse, RoomDraftWrite, RoomMemberResponse,
                                      RoomReadStateResponse, RoomReadStateWrite, RoomRunResponse)
from app.schemas.event import MessageEventCreate, RoomEventResponse
from app.schemas.room_launch import RoomLaunchWrite
from app.schemas.context_preview import ContextPreviewResponse, ContextPreviewWrite
from app.schemas.request_inspector import RequestInspectorResponse
from app.schemas.request_retry import RequestRetryPreflight, RequestRetryResult, RequestRetryWrite
from app.schemas.request_finalize import (
    FinalizePreviewWrite, FinalizeWrite, RequestFinalizePreview, RequestFinalizeResult,
)
from app.services import events, room_service
from app.services.context_preview import build_context_preview
from app.services.request_inspector import build_request_inspector, canonical_uuid
from app.services.request_retry import build_retry_preflight, execute_retry
from app.services.request_finalize import build_finalize_preview, execute_finalize
from app.services.scheduler import validate_limits
from app.services.context import CheckpointSourceBusy, append_room_summary

router = APIRouter()


def _empty_draft() -> dict:
    return {"content": "", "version": 0, "idempotency_key": None, "updated_at": None, "reply_to":None}


def _draft_conflict(current_version: int) -> HTTPException:
    return HTTPException(409, {"code": "draft_version_conflict", "current_version": current_version})


def _current_draft(db: Session, owner_id: int, room_id: int) -> RoomDraft | None:
    db.expire_all()
    return db.get(RoomDraft, (owner_id, room_id))


def _current_read_state(db: Session, owner_id: int, room_id: int) -> RoomReadState | None:
    db.expire_all()
    return db.get(RoomReadState, (owner_id, room_id))


def _is_put_replay(draft: RoomDraft, content: str, expected_version: int, reply_to=None) -> bool:
    return expected_version + 1 == draft.version and content == draft.content and draft.reply_to==reply_to


def _empty_read_state() -> dict:
    return {"last_read_seq": 0, "updated_at": None}


def _contiguous_event_high_water(db: Session, room_id: int) -> int:
    count, first, last = db.query(
        func.count(RoomEvent.seq), func.min(RoomEvent.seq), func.max(RoomEvent.seq)
    ).filter(RoomEvent.room_id == room_id).one()
    if not count or first != 1:
        return 0
    if count == last:
        return last
    expected = 1
    for (seq,) in db.query(RoomEvent.seq).filter(RoomEvent.room_id == room_id).order_by(RoomEvent.seq):
        if seq != expected:
            return expected - 1
        expected += 1
    return expected - 1


@router.post("/", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    conversation_data: ConversationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new conversation"""
    # Check project exists
    project = db.query(Project).filter(
        Project.id == conversation_data.project_id,
        Project.user_id == current_user.id,
        Project.is_active == True
    ).first()
    
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )
    
    new_conversation = Conversation(
        user_id=current_user.id,
        project_id=conversation_data.project_id,
        title=conversation_data.title
    )
    
    db.add(new_conversation)
    db.commit()
    db.refresh(new_conversation)
    
    return new_conversation


@router.post("/launch", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
def launch_conversation(
    launch_data: RoomLaunchWrite,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return room_service.launch_room(db, current_user.id, launch_data, idempotency_key)


@router.post("/{conversation_id}/context-preview", response_model=ContextPreviewResponse)
def preview_context(
    conversation_id: int,
    body: ContextPreviewWrite,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    broker = getattr(request.app.state.scheduler, "broker", None)
    settings = db.get(RoomScheduleSettings, conversation_id)
    tools = ([] if settings is not None and settings.max_tool_calls == 0
             else list(getattr(broker, "tool_schemas", []) or []))
    mcp=getattr(request.app.state,'mcp_client',None)
    mcp_available=(mcp is not None and getattr(mcp,'available',False) is True and
        all(callable(getattr(mcp,name,None)) for name in ('configure','bind','revoke','issue','start','cancel','discover','credential_status')))
    return build_context_preview(
        db, current_user, conversation_id, body.content, tools,
        requires_verified_tools=bool(getattr(broker, "requires_verified_tools", False)),mcp_available=mcp_available,
        reply_to=body.reply_to.model_dump() if body.reply_to else None,
        page_target=body.page_target.model_dump() if body.page_target else None,
    )


@router.get("/project/{project_id}", response_model=List[ConversationResponse])
async def list_conversations_by_project(
    project_id: int,
    room_status: Literal["active", "closed", "all"] = Query("active", alias="status"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all conversations for a project"""
    project = db.query(Project).filter_by(id=project_id, user_id=current_user.id).first()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    query = db.query(Conversation).filter(
        Conversation.user_id == current_user.id,
        Conversation.project_id == project_id,
    )
    if room_status != "all":
        query = query.filter(Conversation.is_active.is_(room_status == "active"))
    from app.models.shared_text_runtime import SharedTextRoom
    query=query.filter(~Conversation.id.in_(db.query(SharedTextRoom.room_id)))
    return query.order_by(Conversation.updated_at.desc(), Conversation.id.desc()).all()


@router.get("/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get conversation by ID"""
    return room_service.require_room(db,conversation_id,current_user.id,active=False)


@router.get("/{conversation_id}/requests/{trigger_event_id}", response_model=RequestInspectorResponse)
def inspect_request_execution(
    conversation_id: int,
    trigger_event_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if conversation_id <= 0 or not canonical_uuid(trigger_event_id):
        raise HTTPException(422, {"code": "request_execution_invalid"})
    room_service.require_room(db,conversation_id,current_user.id,active=False)
    return build_request_inspector(db, current_user.id, conversation_id, trigger_event_id)


@router.post("/{conversation_id}/requests/{trigger_event_id}/finalize/preview",
             response_model=RequestFinalizePreview)
def preview_request_finalize(
    conversation_id: int,
    trigger_event_id: str,
    body: FinalizePreviewWrite,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if conversation_id <= 0 or not canonical_uuid(trigger_event_id):
        raise HTTPException(422, {"code": "request_finalize_invalid"})
    room_service.require_room(db,conversation_id,current_user.id,active=False)
    return build_finalize_preview(db, current_user.id, conversation_id,
                                  trigger_event_id, body.agent_id)


@router.post("/{conversation_id}/requests/{trigger_event_id}/finalize",
             response_model=RequestFinalizeResult, status_code=status.HTTP_201_CREATED)
async def finalize_request(
    conversation_id: int,
    trigger_event_id: str,
    body: FinalizeWrite,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if conversation_id <= 0 or not canonical_uuid(trigger_event_id):
        raise HTTPException(422, {"code": "request_finalize_invalid"})
    room_service.require_room(db,conversation_id,current_user.id)
    result = execute_finalize(
        db, current_user.id, conversation_id, trigger_event_id,
        body.agent_id, body.plan_hash, idempotency_key,
    )
    request.app.state.event_hub.publish(conversation_id)
    await request.app.state.scheduler.schedule_user_event(
        conversation_id, result["finalize_event_id"], wait=False,
    )
    return result


@router.get("/{conversation_id}/failures/{failure_event_id}/retry",
            response_model=RequestRetryPreflight)
def preview_request_retry(
    conversation_id: int,
    failure_event_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if conversation_id <= 0 or not canonical_uuid(failure_event_id):
        raise HTTPException(422, {"code": "request_retry_invalid"})
    room_service.require_room(db,conversation_id,current_user.id,active=False)
    return build_retry_preflight(db, current_user.id, conversation_id, failure_event_id)


@router.post("/{conversation_id}/failures/{failure_event_id}/retry",
             response_model=RequestRetryResult, status_code=status.HTTP_201_CREATED)
async def retry_failed_request(
    conversation_id: int,
    failure_event_id: str,
    body: RequestRetryWrite,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if conversation_id <= 0 or not canonical_uuid(failure_event_id):
        raise HTTPException(422, {"code": "request_retry_invalid"})
    room_service.require_room(db,conversation_id,current_user.id)
    result = execute_retry(
        db, current_user.id, conversation_id, failure_event_id,
        body.plan_hash, idempotency_key,
    )
    request.app.state.event_hub.publish(conversation_id)
    await request.app.state.scheduler.schedule_user_event(
        conversation_id, result["retry_event_id"], wait=False,
    )
    return result


@router.patch("/{conversation_id}", response_model=ConversationResponse)
def rename_conversation(
    conversation_id: int,
    title_data: ConversationTitleWrite,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room_service.require_room(db,conversation_id,current_user.id,active=False)
    return room_service.rename_room(db, conversation_id, current_user.id, title_data.title)


@router.post("/{conversation_id}/clone", response_model=ConversationResponse,
             status_code=status.HTTP_201_CREATED)
def clone_conversation(
    conversation_id: int,
    clone_data: ConversationCloneWrite,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return room_service.clone_room(
        db, conversation_id, current_user.id, clone_data.project_id, clone_data.title)


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete conversation (soft delete)"""
    room_service.require_room(db, conversation_id, current_user.id)
    room_service.close_room(db, conversation_id)
    request.app.state.scheduler.cancel(conversation_id)
    await execution_scopes.stop_room_resources(request.app.state, conversation_id)
    request.app.state.event_hub.publish(conversation_id)
    
    return None


@router.get("/{conversation_id}/draft", response_model=RoomDraftResponse)
def get_room_draft(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room_service.require_room(db, conversation_id, current_user.id, active=False)
    return db.get(RoomDraft, (current_user.id, conversation_id)) or _empty_draft()


@router.put("/{conversation_id}/draft", response_model=RoomDraftResponse)
def put_room_draft(
    conversation_id: int,
    draft_data: RoomDraftWrite,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room_service.require_room(db, conversation_id, current_user.id)
    draft = db.get(RoomDraft, (current_user.id, conversation_id))
    reference=draft_data.reply_to.model_dump() if draft_data.reply_to else None
    if draft and draft.reply_to is not None and 'reply_to' not in draft_data.model_fields_set:
        raise HTTPException(409,{'code':'reply_draft_field_required'})
    if draft is None:
        if draft_data.expected_version != 0:
            raise _draft_conflict(0)
        if not draft_data.content and reference is None:
            return _empty_draft()
        if reference is not None:events.reply_target(db,conversation_id,current_user.id,reference)
        draft = RoomDraft(
            owner_id=current_user.id,
            room_id=conversation_id,
            content=draft_data.content,
            version=1,
            idempotency_key=str(uuid4()) if draft_data.content else None,
            reply_to_event_id=reference['event_id'] if reference else None,
            reply_to_seq=reference['seq'] if reference else None,
            updated_at=datetime.utcnow(),
        )
        db.add(draft)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            current = _current_draft(db, current_user.id, conversation_id)
            if current is not None and _is_put_replay(current, draft_data.content, draft_data.expected_version,reference):
                return current
            raise _draft_conflict(current.version if current else 0)
        db.refresh(draft)
        return draft

    if draft_data.expected_version != draft.version:
        if _is_put_replay(draft, draft_data.content, draft_data.expected_version,reference):
            return draft
        raise _draft_conflict(draft.version)
    if draft_data.content == draft.content and reference==draft.reply_to:
        return draft

    if reference is not None:events.reply_target(db,conversation_id,current_user.id,reference)

    next_key = str(uuid4()) if draft_data.content else None
    changed = db.execute(
        update(RoomDraft)
        .where(RoomDraft.owner_id == current_user.id, RoomDraft.room_id == conversation_id,
               RoomDraft.version == draft_data.expected_version)
        .values(content=draft_data.content, version=draft_data.expected_version + 1,
                idempotency_key=next_key, reply_to_event_id=reference['event_id'] if reference else None,
                reply_to_seq=reference['seq'] if reference else None, updated_at=datetime.utcnow())
    ).rowcount
    if not changed:
        db.rollback()
        current = _current_draft(db, current_user.id, conversation_id)
        if current is not None and _is_put_replay(current, draft_data.content, draft_data.expected_version,reference):
            return current
        raise _draft_conflict(current.version if current else 0)
    db.commit()
    return _current_draft(db, current_user.id, conversation_id)


@router.delete("/{conversation_id}/draft", status_code=status.HTTP_204_NO_CONTENT)
def clear_room_draft(
    conversation_id: int,
    expected_version: int = Query(..., ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room_service.require_room(db, conversation_id, current_user.id)
    draft = db.get(RoomDraft, (current_user.id, conversation_id))
    if draft is None:
        if expected_version == 0:
            return None
        raise _draft_conflict(0)
    if not draft.content and draft.reply_to is None:
        if expected_version in (draft.version, draft.version - 1):
            return None
        raise _draft_conflict(draft.version)
    if expected_version != draft.version:
        raise _draft_conflict(draft.version)

    changed = db.execute(
        update(RoomDraft)
        .where(RoomDraft.owner_id == current_user.id, RoomDraft.room_id == conversation_id,
               RoomDraft.version == expected_version)
        .values(content="", version=expected_version + 1, idempotency_key=None,reply_to_event_id=None,reply_to_seq=None,
                updated_at=datetime.utcnow())
    ).rowcount
    if not changed:
        db.rollback()
        current = _current_draft(db, current_user.id, conversation_id)
        if current is not None and not current.content and current.reply_to is None and current.version == expected_version + 1:
            return None
        raise _draft_conflict(current.version if current else 0)
    db.commit()
    return None


@router.get("/{conversation_id}/read-state", response_model=RoomReadStateResponse)
def get_room_read_state(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room_service.require_room(db, conversation_id, current_user.id, active=False)
    return db.get(RoomReadState, (current_user.id, conversation_id)) or _empty_read_state()


@router.put("/{conversation_id}/read-state", response_model=RoomReadStateResponse)
def put_room_read_state(
    conversation_id: int,
    state_data: RoomReadStateWrite,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    room_service.require_room(db, conversation_id, current_user.id, active=False)
    high_water = _contiguous_event_high_water(db, conversation_id)
    if state_data.last_read_seq > high_water:
        raise HTTPException(422, {
            "code": "read_seq_not_contiguous", "contiguous_high_water": high_water,
        })

    state = db.get(RoomReadState, (current_user.id, conversation_id))
    if state is None:
        if state_data.last_read_seq == 0:
            return _empty_read_state()
        state = RoomReadState(
            owner_id=current_user.id, room_id=conversation_id,
            last_read_seq=state_data.last_read_seq, updated_at=datetime.utcnow(),
        )
        db.add(state)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            state = _current_read_state(db, current_user.id, conversation_id)
            if state is None:
                raise
            if state.last_read_seq >= state_data.last_read_seq:
                return state
        else:
            db.refresh(state)
            return state

    if state.last_read_seq >= state_data.last_read_seq:
        return state
    db.execute(
        update(RoomReadState)
        .where(RoomReadState.owner_id == current_user.id, RoomReadState.room_id == conversation_id,
               RoomReadState.last_read_seq < state_data.last_read_seq)
        .values(last_read_seq=state_data.last_read_seq, updated_at=datetime.utcnow())
    )
    db.commit()
    return _current_read_state(db, current_user.id, conversation_id)


@router.get("/{conversation_id}/messages", response_model=List[MessageResponse])
async def list_messages(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all messages in a conversation"""
    room_service.require_room(db, conversation_id, current_user.id, active=False)
    history = db.query(events.RoomEvent).filter_by(room_id=conversation_id, kind="message").order_by(events.RoomEvent.seq).all()
    return [{"id": event.payload["legacy_message_id"], "conversation_id": conversation_id,
             "agent_id": event.actor_id if event.actor_type == "agent" else None,
             "role": event.payload.get("role", "assistant" if event.actor_type == "agent" else "user" if event.actor_type == "user" else "system"),
             "content": event.payload["content"], "created_at": event.created_at} for event in history]


@router.post("/{conversation_id}/messages", response_model=List[MessageResponse], status_code=status.HTTP_201_CREATED)
async def send_message(
    conversation_id: int,
    message_data: MessageCreate,
    request: Request,
    idempotency_key: str | None = Header(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Send a message and get agent responses"""
    from app.services.model_features import output_request
    output_request(message_data.content, local_speech=bool(getattr(request.app.state.local_executor, 'speech_available', False) and getattr(getattr(request.app.state.local_executor, 'speech_client', None), 'available', False)))
    # Check conversation exists
    conversation = room_service.require_room(db, conversation_id, current_user.id)
    
    try:
        event = events.append_event(db, conversation_id, events.ActorRef("user", current_user.id),
                                    "message", {"content": message_data.content}, idempotency_key)
        if not event._duplicate:
            request.app.state.event_hub.publish(conversation_id)
        responses = await request.app.state.scheduler.schedule_user_event(conversation_id, event.event_id, wait=True)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    failure = db.query(events.RoomEvent).filter_by(room_id=conversation_id, actor_type="agent", kind="failure").order_by(
        events.RoomEvent.seq.desc()).first()
    if failure and failure.payload.get("trigger_event_id") == event.event_id:
        raise HTTPException(502, {"code": failure.payload["code"], "message": failure.payload["message"]})
    return responses


@router.get('/{conversation_id}/page-message/receipt', response_model=RoomEventResponse)
def page_message_receipt(conversation_id: int, response: Response,
                         idempotency_key: str = Header(..., alias='Idempotency-Key'),
                         db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    room_service.require_room(db,conversation_id,current_user.id,active=False)
    if not idempotency_key or len(idempotency_key)>200:
        raise HTTPException(404,{'code':'page_receipt_not_found'})
    event=db.query(events.RoomEvent).filter_by(room_id=conversation_id,actor_type='user',actor_id=current_user.id,
                                               kind='message',idempotency_key=idempotency_key).first()
    if not event or not isinstance(event.payload,dict) or not isinstance(event.payload.get('page_target'),dict):
        raise HTTPException(404,{'code':'page_receipt_not_found'})
    response.headers['Cache-Control']='no-store'
    return event


@router.post("/{conversation_id}/routine-reference/resolve")
def resolve_routine_reference(conversation_id: int, value: MessageEventCreate,
                              idempotency_key: str | None = Header(None), db: Session = Depends(get_db),
                              current_user: User = Depends(get_current_user)):
    """Resolve/fence this exact request without sending a message or model call."""
    from app.services import routine_reference_fence as fence
    if (value.routine_ref is None or value.reply_to is not None or value.page_target is not None
            or value.page_selection is not None or value.output_kind != 'auto'
            or not isinstance(idempotency_key,str) or not 1<=len(idempotency_key)<=200 or not idempotency_key.isprintable()):
        raise HTTPException(422,{'code':'routine_reference_invalid'})
    payload={'content':value.content,'routine_ref':value.routine_ref.model_dump()}
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        room_service.require_room(db,conversation_id,current_user.id,active=False)
        result=fence.resolve(db,current_user.id,conversation_id,idempotency_key,payload)
        db.commit();return result
    except BaseException:
        db.rollback();raise


@router.post("/{conversation_id}/events", response_model=RoomEventResponse, status_code=status.HTTP_201_CREATED)
async def post_event(conversation_id: int, request: Request, event_data: dict = Body(...),
               idempotency_key: str | None = Header(None), db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    room_service.require_room(db, conversation_id, current_user.id)
    from app.models.shared_text_runtime import SharedTextRoom
    shared_text=bool(db.query(SharedTextRoom).filter_by(room_id=conversation_id).first())
    if shared_text:
        if (not isinstance(event_data,dict) or not set(event_data)<= {'content','output_kind','reply_to','page_target'}
                or event_data.get('output_kind','auto') not in ('auto','text')
                or event_data.get('reply_to') is not None or event_data.get('page_target') is not None):
            raise HTTPException(422,{'code':'shared_text_only'})
        event_data={'content':event_data.get('content')}
    try:
        message = MessageEventCreate.model_validate(event_data)
        if message.page_target is not None and (not isinstance(idempotency_key,str) or not 1<=len(idempotency_key)<=200 or not idempotency_key.isprintable()):
            raise HTTPException(422,{'code':'page_idempotency_key_required'})
        if message.routine_ref is not None and (message.page_target is not None or message.reply_to is not None or message.output_kind!='auto'):
            raise HTTPException(422,{'code':'routine_reference_incompatible'})
        if message.routine_ref is not None and (not isinstance(idempotency_key,str) or not 1<=len(idempotency_key)<=200 or not idempotency_key.isprintable()):
            raise HTTPException(422,{'code':'routine_reference_key_required'})
        if message.page_selection is not None and message.page_target is None:
            raise HTTPException(422,{'code':'page_selection_requires_target'})
        from app.services.model_features import output_request
        output_request(message.content,message.output_kind, local_speech=bool(getattr(request.app.state.local_executor, 'speech_available', False) and getattr(getattr(request.app.state.local_executor, 'speech_client', None), 'available', False)))
        payload={"content": message.content, **({"output_kind":message.output_kind} if message.output_kind!="auto" else {}),
                 **({'reply_to':message.reply_to.model_dump()} if message.reply_to else {}),
                 **({'page_target':message.page_target.model_dump()} if message.page_target else {}),
                 **({'page_selection':message.page_selection.model_dump()} if message.page_selection else {}),
                 **({'routine_ref':message.routine_ref.model_dump()} if message.routine_ref else {})}
        if message.reply_to is not None or message.page_target is not None or message.routine_ref is not None:
            db.rollback();db.execute(text('BEGIN IMMEDIATE'))
            try:
                room_service.require_room(db,conversation_id,current_user.id)
                try:
                    event=events.stage_event(db,conversation_id,events.ActorRef('user',current_user.id),'message',payload,idempotency_key)
                except HTTPException as exc:
                    from app.services import routine_reference_fence as fence
                    detail=exc.detail if isinstance(exc.detail,dict) else {}
                    code=detail.get('code')
                    if message.routine_ref is not None and code in fence.REJECTIONS and not detail.get('fenced'):
                        fence.insert(db,current_user.id,conversation_id,idempotency_key,payload,code)
                        db.commit()
                        raise HTTPException(fence.rejection_status(code),{'code':code,'fenced':True}) from None
                    raise
                request.app.state.scheduler._stage_trigger(db,event)
                db.commit()
            except BaseException:db.rollback();raise
        else:
            event=events.append_event(db,conversation_id,events.ActorRef('user',current_user.id),'message',payload,idempotency_key)
    except (ValueError, ValidationError) as error:
        raise HTTPException(422, str(error)) from error
    request.app.state.event_hub.publish(conversation_id)
    await request.app.state.scheduler.schedule_user_event(conversation_id, event.event_id)
    return event


@router.get("/{conversation_id}/schedule")
def get_schedule(conversation_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    room_service.require_room(db, conversation_id, current_user.id, active=False)
    row = db.get(RoomScheduleSettings, conversation_id)
    return {"max_rounds": row.max_rounds if row else 2, "max_tool_calls": row.max_tool_calls if row else 8,
            "max_executor_seconds": row.max_executor_seconds if row else 60}


@router.put("/{conversation_id}/schedule")
def set_schedule(conversation_id: int, settings: dict = Body(...), db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    room_service.require_room(db, conversation_id, current_user.id)
    try:
        validate_limits(settings)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    if set(settings) != {"max_rounds", "max_tool_calls", "max_executor_seconds"}:
        raise HTTPException(422, "Invalid schedule limits")
    row = db.get(RoomScheduleSettings, conversation_id)
    if row is None:
        row = RoomScheduleSettings(room_id=conversation_id)
        db.add(row)
    for key, value in settings.items():
        setattr(row, key, value)
    db.commit()
    return settings


@router.post("/{conversation_id}/continue", response_model=RoomEventResponse, status_code=status.HTTP_201_CREATED)
async def continue_room(conversation_id: int, request: Request, body: dict = Body(...),
                        db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    room_service.require_room(db, conversation_id, current_user.id)
    content = body.get("content", "Continue the room discussion.")
    limits = {key: body[key] for key in ("max_rounds", "max_tool_calls", "max_executor_seconds") if key in body}
    if set(body) - {"content", *limits} or not isinstance(content, str) or not content.strip():
        raise HTTPException(422, "Invalid continuation")
    try:
        validate_limits(limits)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    event = events.append_event(db, conversation_id, events.ActorRef("user", current_user.id), "message", {"content": content})
    request.app.state.event_hub.publish(conversation_id)
    try:
        await request.app.state.scheduler.schedule_user_event(conversation_id, event.event_id, limits=limits)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return event


@router.get("/{conversation_id}/events", response_model=List[RoomEventResponse])
def get_events(conversation_id: int, after_seq: int = 0, limit: int = 100,
               db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    room_service.require_room(db, conversation_id, current_user.id, active=False)
    try:
        return events.events_after(db, conversation_id, after_seq, limit)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get('/{conversation_id}/events/{root_event_id}/replies')
def get_reply_thread(conversation_id:int,root_event_id:str,root_seq:int=Query(gt=0,le=9223372036854775807),
                     after_seq:int=Query(default=0,ge=0,le=9223372036854775807),upper_seq:int|None=Query(default=None,gt=0,le=9223372036854775807),
                     limit:int=Query(default=50,ge=1,le=100),db:Session=Depends(get_db),
                     current_user:User=Depends(get_current_user)):
    room_service.require_room(db,conversation_id,current_user.id,active=False)
    result=events.reply_thread_page(db,conversation_id,current_user.id,root_event_id,root_seq,
                                    after_seq=after_seq,upper_seq=upper_seq,limit=limit)
    result['items']=[RoomEventResponse.model_validate(item).model_dump(mode='json') for item in result['items']]
    return result

@router.get('/{conversation_id}/events/{root_event_id}/thread')
def get_descendant_thread(conversation_id:int,root_event_id:str,root_seq:int=Query(gt=0,le=9223372036854775807),
                          after_seq:int=Query(default=0,ge=0,le=9223372036854775807),upper_seq:int|None=Query(default=None,gt=0,le=9223372036854775807),
                          limit:int=Query(default=50,ge=1,le=100),db:Session=Depends(get_db),
                          current_user:User=Depends(get_current_user)):
    room_service.require_room(db,conversation_id,current_user.id,active=False)
    result=events.descendant_thread_page(db,conversation_id,current_user.id,root_event_id,root_seq,
                                         after_seq=after_seq,upper_seq=upper_seq,limit=limit)
    for item in result['items']:
        item['event']=RoomEventResponse.model_validate(item['event']).model_dump(mode='json')
    return result


@router.post("/{conversation_id}/summaries", response_model=RoomEventResponse, status_code=status.HTTP_201_CREATED)
def post_summary(conversation_id: int, request: Request, body: dict = Body(...),
                 idempotency_key: str | None = Header(None),
                 db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    room_service.require_room(db, conversation_id, current_user.id)
    if set(body) != {"source_start_seq", "source_end_seq", "content"}:
        raise HTTPException(422, "Invalid summary input")
    try:
        event = append_room_summary(db, conversation_id, body["source_start_seq"], body["source_end_seq"],
                                    body["content"], idempotency_key)
    except CheckpointSourceBusy as error:
        raise HTTPException(409, {"code": "checkpoint_source_busy", "message": str(error)}) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    request.app.state.event_hub.publish(conversation_id)
    return event


@router.get("/{conversation_id}/members", response_model=List[RoomMemberResponse])
def list_room_members(conversation_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return room_service.list_members(db, conversation_id, current_user.id)


@router.post("/{conversation_id}/members/{agent_id}", response_model=RoomMemberResponse, status_code=status.HTTP_201_CREATED)
def add_room_member(conversation_id: int, agent_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    room_service.require_room(db, conversation_id, current_user.id)
    return room_service.add_member(db, conversation_id, agent_id)


@router.delete("/{conversation_id}/members/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_room_member(conversation_id: int, agent_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    room_service.require_room(db, conversation_id, current_user.id)
    room_service.remove_member(db, conversation_id, agent_id)
    request.app.state.scheduler.cancel(conversation_id, agent_id)
    await execution_scopes.stop_room_resources(request.app.state, conversation_id, agent_id)


@router.post("/{conversation_id}/members/{agent_id}/pause", response_model=RoomRunResponse)
async def pause_room_member(conversation_id: int, agent_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    room_service.require_room(db, conversation_id, current_user.id)
    result = room_service.set_paused(db, conversation_id, agent_id, True)
    # Park this request and settle all participants in its current wave before resuming.
    request.app.state.scheduler.pause(conversation_id)
    await execution_scopes.stop_room_resources(request.app.state, conversation_id)
    request.app.state.scheduler.hub.publish(conversation_id)
    return result


@router.post("/{conversation_id}/members/{agent_id}/resume", response_model=RoomRunResponse)
async def resume_room_member(conversation_id: int, agent_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    room_service.require_room(db, conversation_id, current_user.id)
    scheduler = request.app.state.scheduler
    worker = scheduler._workers.get(conversation_id)
    if worker and not worker.done():
        raise HTTPException(409, {'code': 'resume_settling'})
    from app.services import resume_boundary
    result, trigger_id = resume_boundary.resume(db, conversation_id, agent_id)
    if trigger_id:
        from app.models.collaboration import RoomTrigger
        trigger = db.get(RoomTrigger, trigger_id)
        if trigger:
            scheduler._wake(trigger.room_id)
            scheduler.hub.publish(trigger.room_id)
    scheduler.hub.publish(conversation_id)
    return result


@router.post("/{conversation_id}/stop", status_code=status.HTTP_204_NO_CONTENT)
async def stop_room(conversation_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    room_service.require_room(db, conversation_id, current_user.id)
    room_service.stop_room(db, conversation_id)
    request.app.state.scheduler.cancel(conversation_id)
    receipt = await execution_scopes.stop_room_resources(request.app.state, conversation_id)
    request.app.state.event_hub.publish(conversation_id)
    return Response(status_code=204, headers={'X-Gamja-Scope-Stop-Id':receipt['stop_id']} if receipt else {})


@router.post("/{conversation_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_room_work(conversation_id: int, request: Request, db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    room_service.require_room(db, conversation_id, current_user.id)
    room_service.cancel_room_work(db, conversation_id)
    request.app.state.scheduler.cancel(conversation_id)
    receipt = await execution_scopes.stop_room_resources(request.app.state, conversation_id)
    events.append_event(db, conversation_id, events.ActorRef("system", None), "activity", {"status": "cancelled"})
    request.app.state.event_hub.publish(conversation_id)
    return Response(status_code=204, headers={'X-Gamja-Scope-Stop-Id':receipt['stop_id']} if receipt else {})


@router.websocket("/{conversation_id}/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    conversation_id: int,
    db: Session = Depends(get_db)
):
    """WebSocket endpoint for real-time chat"""
    if not authorize_ws(websocket):
        await websocket.close(code=1008)
        return

    protocols = [item.strip() for item in websocket.headers.get("sec-websocket-protocol", "").split(",")]
    user_tokens = [item.removeprefix("gamja.user.") for item in protocols if item.startswith("gamja.user.")]
    if len(user_tokens) != 1:
        await websocket.close(code=1008)
        return
    try:
        current_user = user_from_token(user_tokens[0], db)
    except HTTPException:
        await websocket.close(code=1008)
        return

    conversation = db.query(Conversation).join(Project, Project.id == Conversation.project_id).filter(
        Conversation.id == conversation_id,
        Conversation.user_id == current_user.id,
        Conversation.is_active == True,
        Project.is_active == True,
    ).first()
    if not conversation:
        await websocket.close(code=1008)
        return
    try:room_service.require_room(db,conversation_id,current_user.id)
    except HTTPException:
        await websocket.close(code=1008)
        return

    try:
        cursor = int(websocket.query_params.get("after_seq", "0"))
        if cursor < 0:
            raise ValueError
    except ValueError:
        await websocket.close(code=1008)
        return
    hub = websocket.app.state.event_hub
    wake = hub.subscribe(conversation_id)
    await websocket.accept(subprotocol="gamja.chat")

    async def stream_events():
        nonlocal cursor
        reader = websocket.app.state.session_factory()
        reader.info['app_state']=websocket.app.state
        try:
            while True:
                reader.rollback()  # Each replay query sees the latest committed snapshot.
                room_service.require_room(reader, conversation_id, current_user.id)
                batch = events.events_after(reader, conversation_id, cursor, 500)
                for event in batch:
                    wire = RoomEventResponse.model_validate(event).model_dump(mode="json")
                    wire["type"] = "event"
                    if event.kind == "message":
                        wire["content"] = event.payload["content"]  # legacy socket consumer
                    await websocket.send_json(wire)
                    cursor = event.seq
                if len(batch) == 500:
                    continue
                await wake.get()
        finally:
            reader.close()

    async def receive_commands():
        while True:
            data = await websocket.receive_text()
            try:
                command = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                await websocket.close(code=1008)
                return
            if (not isinstance(command, dict) or command.get("type") != "message"
                    or set(command) - {"type", "content", "idempotency_key"}):
                await websocket.close(code=1008)
                return
            content = command.get("content")
            key = command.get("idempotency_key")
            if (not isinstance(content, str) or not content.strip()
                    or (key is not None and (not isinstance(key, str) or not key or len(key) > 200))):
                await websocket.send_json({"type": "error", "detail": "Invalid message command"})
                continue
            db.rollback()
            room_service.require_room(db, conversation_id, current_user.id)
            try:
                event = events.append_event(db, conversation_id, events.ActorRef("user", current_user.id),
                                            "message", {"content": content}, key)
                if not event._duplicate:
                    hub.publish(conversation_id)
                await websocket.app.state.scheduler.schedule_user_event(conversation_id, event.event_id)
            except ValueError:
                await websocket.send_json({"type": "error", "detail": "Invalid message command"})

    sender = asyncio.create_task(stream_events())
    receiver = asyncio.create_task(receive_commands())
    try:
        done, pending = await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except WebSocketDisconnect:
        pass
    except HTTPException:
        await websocket.close(code=1008)
    except Exception:
        await websocket.close(code=1011, reason="Internal server error")
    finally:
        sender.cancel()
        receiver.cancel()
        await asyncio.gather(sender, receiver, return_exceptions=True)
        hub.unsubscribe(conversation_id, wake)
