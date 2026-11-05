"""Preview and send one explicit Owner file question to one Bot."""
import hashlib
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User,Agent
from app.models.collaboration import RoomEvent,RoomTrigger,RoomScheduleSettings,RoomDraft,ProviderConnection
from app.schemas.event import RoomEventResponse
from app.schemas.library_file_questions import FileQuestionPreview,FileQuestionSend
from app.services import library_file_questions as service,room_service
from app.services.context_preview import build_context_preview
from app.services.events import ActorRef,stage_event

router=APIRouter()
HEADERS={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}


def _key(value):
    if not isinstance(value,str) or not 1<=len(value)<=200 or not value.isprintable():raise HTTPException(422,{'code':'file_question_invalid_key'})
    return 'file-question:'+hashlib.sha256(value.encode('utf-8')).hexdigest()

def _resources(request):
    uploads=getattr(request.app.state.local_executor,'library_uploads',None)
    return uploads,getattr(request.app.state,'owner_epoch_provider',None)

def _require_no_reply_draft(db,owner,room_id):
    draft=db.get(RoomDraft,(owner,room_id),populate_existing=True)
    if draft and draft.reply_to is not None:
        raise HTTPException(409,{'code':'file_question_reply_conflict'})

def _context(db,owner,room,content,prepared,request):
    broker=getattr(request.app.state.scheduler,'broker',None)
    settings=db.get(RoomScheduleSettings,room)
    tools=[] if settings is not None and settings.max_tool_calls==0 else list(getattr(broker,'tool_schemas',[]) or [])
    if prepared.get('full_access'):
        if broker is None or settings is not None and settings.max_tool_calls==0:
            raise HTTPException(409,{'code':'file_question_tools_unavailable'})
        agent=db.get(Agent,prepared['target']['bot_id']);conn=db.get(ProviderConnection,agent.connection_id) if agent else None
        if not conn:raise HTTPException(409,{'code':'file_question_tools_unavailable'})
        from app.services.connections import ConnectionConfig
        from app.services.providers.base import ProviderRequest,verified
        config=ConnectionConfig(conn.id,conn.kind,agent.model_id,conn.base_url,None,conn.capabilities or {},provider_id=conn.provider_id,protocol_id=conn.protocol_id,auth_method_id=conn.auth_method_id,config_revision=conn.readiness_revision,auth_grant_revision=conn.auth_grant_revision)
        if getattr(broker,'requires_verified_tools',False) and not verified(ProviderRequest(connection=config,model_id=agent.model_id),'tools'):
            raise HTTPException(409,{'code':'file_question_tools_unavailable'})
        if conn.provider_id=='chatgpt':
            from app.services.providers.chatgpt import chatgpt_filtered_tools
            if not chatgpt_filtered_tools(config,[service.FILE_READ_SCHEMA]):raise HTTPException(409,{'code':'file_question_tools_unavailable'})
    if prepared.get('full_access') and (settings is None or settings.max_tool_calls!=0):tools=[*tools,service.FILE_READ_SCHEMA]
    mcp=getattr(request.app.state,'mcp_client',None)
    available=mcp is not None and getattr(mcp,'available',False) is True and all(callable(getattr(mcp,n,None)) for n in ('configure','bind','revoke','issue','start','cancel','discover','credential_status'))
    projection={'agent_id':prepared['target']['bot_id'],'root_event_id':None,
                'content':(content+service.IMAGE_CONTEXT
                           if prepared.get('mode')=='image' else service.project_content(content,prepared['excerpt'],prepared['extraction'],full_access=prepared.get('full_access',False)))}
    # Full-range capability was checked using the same provider verifier as
    # the scheduler above. Include its schema in the budget even when the
    # preview's older capability format would otherwise drop it.
    value=build_context_preview(db,owner,room,content,tools,requires_verified_tools=bool(getattr(broker,'requires_verified_tools',False)) and not prepared.get('full_access'),mcp_available=available,file_projection=projection)
    agents=value.get('agents',[])
    if value.get('selected_agent_ids')!=[prepared['target']['bot_id']] or len(agents)!=1 or agents[0].get('projection_status')!='ready':
        raise HTTPException(409,{'code':'file_question_context_unavailable'})
    return value


@router.post('/{room_id}/file-question/preview')
async def preview_file_question(room_id:int,value:FileQuestionPreview,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    room_service.require_room(db,room_id,owner.id);_require_no_reply_draft(db,owner.id,room_id)
    uploads,epochs=_resources(request)
    prepared=await service.prepare(db,owner.id,room_id,value.bot_id,value.upload_id,value.sha256,value.content,uploads,epochs,mode=value.mode,full_access=value.full_access)
    context=_context(db,owner,room_id,value.content,prepared,request)
    if value.mode=='image':
        return JSONResponse({k:prepared[k] for k in ('mode','basis','file','image','target')}|{'requires_image_consent':True,'context_preview':context},headers=HEADERS)
    return JSONResponse({k:prepared[k] for k in ('basis','file','extraction','excerpt','target')}|({'mode':'pdf_ocr'} if value.mode=='pdf_ocr' else {})|({k:prepared[k] for k in ('full_access','range','excerpt_sha256','excerpt_chars')} if value.full_access else {})|{'context_preview':context},headers=HEADERS)


@router.post('/{room_id}/file-question')
async def send_file_question(room_id:int,value:FileQuestionSend,request:Request,key:str|None=Header(None,alias='Idempotency-Key'),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    command=_key(key);uploads,epochs=_resources(request)
    room_service.require_room(db,room_id,owner.id)
    # Recovery never silently creates or re-sends an altered request.
    prior=db.query(RoomEvent).filter_by(room_id=room_id,idempotency_key=command).first()
    if prior:
        payload=prior.payload if isinstance(prior.payload,dict) else {};ctx=payload.get('file_context')
        if (prior.actor_type!='user' or prior.actor_id!=owner.id or prior.kind!='message' or payload.get('content')!=value.content
                or not service.valid_context(ctx,value.content) or ctx.get('version')!=(2 if value.mode=='image' else (5 if value.mode=='pdf_ocr' else 4) if value.full_access else 3 if value.mode=='pdf_ocr' else 1)
                or (ctx.get('bot_id'),ctx.get('upload_id'),ctx.get('sha256'),ctx.get('basis'))!=(value.bot_id,value.upload_id,value.sha256,value.basis)):
            raise HTTPException(409,{'code':'file_question_idempotency_conflict'})
        return JSONResponse(RoomEventResponse.model_validate(prior).model_dump(mode='json'),headers=HEADERS)
    _require_no_reply_draft(db,owner.id,room_id)
    prepared=await service.prepare(db,owner.id,room_id,value.bot_id,value.upload_id,value.sha256,value.content,uploads,epochs,mode=value.mode,full_access=value.full_access)
    if prepared['basis']!=value.basis:raise HTTPException(409,{'code':'file_question_changed'})
    _context(db,owner,room_id,value.content,prepared,request)
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        _require_no_reply_draft(db,owner.id,room_id)
        current,target=service._snapshot(db,owner.id,room_id,value.bot_id,epochs,mode=value.mode)
        meta,_=service._file(uploads,owner.id,value.bot_id,value.upload_id,value.sha256)
        expected=prepared['proof']
        if any(expected.get(k)!=v for k,v in current.items()) or target!=prepared['target'] or meta!=prepared['file']:
            raise HTTPException(409,{'code':'file_question_changed'})
        epoch=epochs.current(owner.id) if epochs else None
        if not epoch or service.digest(epoch.encode())!=expected['owner_epoch_sha256']:
            raise HTTPException(409,{'code':'file_question_changed'})
        file_context={'version':2 if value.mode=='image' else (5 if value.mode=='pdf_ocr' else 4) if value.full_access else 3 if value.mode=='pdf_ocr' else 1,'bot_id':value.bot_id,'upload_id':value.upload_id,'sha256':value.sha256,'basis':value.basis,'proof':expected}
        if value.mode=='image':file_context['image_consent']=True
        if value.full_access:file_context['full_access_consent']=True
        event=stage_event(db,room_id,ActorRef('user',owner.id),'message',{'content':value.content,'file_context':file_context},command)
        db.commit();db.refresh(event);event_id=event.event_id;duplicate=event._duplicate
    except BaseException:db.rollback();raise
    if not duplicate:
        request.app.state.event_hub.publish(room_id)
        if value.mode=='image':
            request.app.state.scheduler._image_roots[event_id]=(owner.id,room_id,value.bot_id)
        await request.app.state.scheduler.schedule_user_event(room_id,event_id)
    elif db.get(RoomTrigger,event_id) is None:
        await request.app.state.scheduler.schedule_user_event(room_id,event_id)
    return JSONResponse(RoomEventResponse.model_validate(event).model_dump(mode='json'),status_code=200 if duplicate else 201,headers=HEADERS)


@router.get('/{room_id}/file-question/receipt')
def file_question_receipt(room_id:int,key:str|None=Header(None,alias='Idempotency-Key'),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    room_service.require_room(db,room_id,owner.id,active=False);command=_key(key)
    event=db.query(RoomEvent).filter_by(room_id=room_id,idempotency_key=command,actor_type='user',actor_id=owner.id,kind='message').first()
    context=event.payload.get('file_context') if event and isinstance(event.payload,dict) else None
    if not service.valid_context(context,event.payload.get('content') if event else None):
        raise HTTPException(404,{'code':'file_question_receipt_not_found'})
    return JSONResponse(RoomEventResponse.model_validate(event).model_dump(mode='json'),headers=HEADERS)
