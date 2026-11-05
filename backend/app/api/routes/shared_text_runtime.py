"""Open a recipient-private execution room for one canonical shared Bot."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User, Agent, Project, Conversation
from app.models.collaboration import ProviderConnection
from app.models.shared_text_runtime import SharedTextRoom
from app.services.room_service import stage_room_from_setup
from app.services.team_authority_adapter import _uuid

router=APIRouter()
HEADERS={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}
class OpenSharedRoom(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    session_generation:str=Field(min_length=36,max_length=36)
    publication_id:str=Field(min_length=36,max_length=36)
    project_id:int=Field(gt=0)
    connection_id:int=Field(gt=0)
    model_id:str=Field(min_length=1,max_length=200)

def authority(request,owner,session_generation,publication_id):
    epochs=getattr(request.app.state,'owner_epoch_provider',None);epoch=epochs.current(owner.id) if epochs else None
    adapter=getattr(request.app.state,'team_connection',None)
    provider=getattr(request.app.state,'provider_client',None)
    if getattr(provider,'available',False) is not True:raise HTTPException(503,{'code':'shared_provider_unavailable'})
    if not adapter or not epoch or adapter.owner_id!=owner.id or adapter.owner_epoch!=epoch or adapter.generation!=session_generation or not adapter.team_id:
        raise HTTPException(409,{'code':'shared_session_unavailable'})
    try:
        _uuid(publication_id);claims=adapter._assertion()
        publication=adapter.command('get_publication',{'publication_id':publication_id})
        if (publication.get('state')!='active' or publication.get('publication_id')!=publication_id
                or publication.get('team_id')!=adapter.team_id or claims.get('sub')!=adapter._subject):raise ValueError()
    except Exception:raise HTTPException(503,{'code':'shared_authority_unavailable'}) from None
    if epochs.current(owner.id)!=epoch or getattr(request.app.state,'team_connection',None) is not adapter or adapter.generation!=session_generation:
        raise HTTPException(409,{'code':'shared_session_changed'})
    return adapter

def response(row,session_generation):
    return JSONResponse({'state':'ready','room_id':row.room_id,'agent_id':row.agent_id,'publication_id':row.publication_id,
                         'session_generation':session_generation,'owner_id':row.owner_id,'issuer':row.issuer,'account_id':row.subject,
                         'team_id':row.team_id,'project_id':row._project_id,
                         'connection_id':row.connection_id,'model_id':row.model_id,'text_only':True},headers=HEADERS)

@router.get('/team-connection/shared-rooms/{publication_id}')
def get_shared_room(publication_id:str,session_generation:str,request:Request,owner:User=Depends(get_current_user),db:Session=Depends(get_db)):
    with request.app.state.owner_recovery_lock:
        adapter=authority(request,owner,session_generation,publication_id)
        row=db.query(SharedTextRoom).filter_by(owner_id=owner.id,issuer=adapter.issuer,subject=adapter._subject,
            team_id=adapter.team_id,publication_id=publication_id).first()
        if not row:raise HTTPException(404,{'code':'shared_room_not_found'})
        room=db.get(Conversation,row.room_id)
        if not room:raise HTTPException(409,{'code':'shared_room_unavailable'})
        project=db.get(Project,room.project_id)
        if not room.is_active or not project or not project.is_active:raise HTTPException(409,{'code':'shared_room_closed'})
        row._project_id=room.project_id
        return response(row,session_generation)

@router.post('/team-connection/shared-rooms')
def open_shared_room(value:OpenSharedRoom,request:Request,owner:User=Depends(get_current_user),db:Session=Depends(get_db)):
    with request.app.state.owner_recovery_lock:
        adapter=authority(request,owner,value.session_generation,value.publication_id)
        db.rollback();db.execute(text('BEGIN IMMEDIATE'))
        try:
            row=db.query(SharedTextRoom).filter_by(owner_id=owner.id,issuer=adapter.issuer,subject=adapter._subject,
                team_id=adapter.team_id,publication_id=value.publication_id).first()
            if row:
                room=db.get(Conversation,row.room_id)
                if (not room or not room.is_active or room.project_id!=value.project_id or row.connection_id!=value.connection_id or row.model_id!=value.model_id):
                    raise HTTPException(409,{'code':'shared_room_configuration_conflict'})
            else:
                project=db.query(Project).filter_by(id=value.project_id,user_id=owner.id,is_active=True).first()
                connection=db.query(ProviderConnection).filter_by(id=value.connection_id,user_id=owner.id,enabled=True).first()
                if not project or not connection:raise HTTPException(404,{'code':'shared_local_configuration_unavailable'})
                agent=Agent(user_id=owner.id,name='Shared Bot',description='',system_prompt='',model=value.model_id,
                    connection_id=value.connection_id,model_id=value.model_id,temperature='auto',max_tokens=0,is_active=True)
                db.add(agent);db.flush()
                room=stage_room_from_setup(db,owner.id,project.id,'Shared Bot',[agent.id],
                    {'max_rounds':1,'max_tool_calls':0,'max_executor_seconds':60})
                row=SharedTextRoom(owner_id=owner.id,issuer=adapter.issuer,subject=adapter._subject,team_id=adapter.team_id,
                    publication_id=value.publication_id,room_id=room.id,agent_id=agent.id,connection_id=connection.id,model_id=value.model_id)
                db.add(row);db.flush()
            row._project_id=room.project_id
            db.commit();return response(row,value.session_generation)
        except BaseException:
            db.rollback();raise

@router.get('/conversations/{room_id}/shared-binding')
def room_binding(room_id:int,request:Request,owner:User=Depends(get_current_user),db:Session=Depends(get_db)):
    room=db.query(Conversation).filter_by(id=room_id,user_id=owner.id).first()
    if not room:raise HTTPException(404,{'code':'room_not_found'})
    row=db.query(SharedTextRoom).filter_by(room_id=room_id,owner_id=owner.id).first()
    if not row:return JSONResponse({'kind':'ordinary'},headers=HEADERS)
    with request.app.state.owner_recovery_lock:
        adapter=getattr(request.app.state,'team_connection',None)
        if not adapter or adapter.owner_id!=owner.id or adapter.issuer!=row.issuer or adapter._subject!=row.subject or adapter.team_id!=row.team_id:
            return JSONResponse({'kind':'shared','state':'unavailable','text_only':True,'shared_private_memory_available':False,'shared_personal_apps_available':False},headers=HEADERS)
        try:authority(request,owner,adapter.generation,row.publication_id)
        except HTTPException:
            return JSONResponse({'kind':'shared','state':'unavailable','text_only':True,'shared_private_memory_available':False,'shared_personal_apps_available':False},headers=HEADERS)
        return JSONResponse({'kind':'shared','state':'ready','room_id':row.room_id,'agent_id':row.agent_id,
            'team_id':row.team_id,'publication_id':row.publication_id,'account_id':row.subject,'issuer':row.issuer,
            'profile_id':adapter.profile_id,'key_fingerprint':adapter.key_fingerprint,
            'session_generation':adapter.generation,'text_only':True,'shared_private_memory_available':True,
            'shared_personal_apps_available':bool(getattr(request.app.state,'shared_connector_runtime_ready',False) and getattr(getattr(request.app.state,'mcp_client',None),'available',False))},headers=HEADERS)

@router.get('/conversations/{room_id}/shared-private-memory')
def shared_private_memory(room_id:int,session_generation:str,request:Request,owner:User=Depends(get_current_user),db:Session=Depends(get_db)):
    from app.services.shared_private_memory import read_snapshot
    from app.services.llm_service import LLMFailure
    try:result=read_snapshot(db,owner.id,room_id,session_generation)
    except LLMFailure as error:
        if error.code=='shared_private_memory_session_changed':raise HTTPException(409,{'code':'team_session_changed'}) from None
        raise HTTPException(404,{'code':'shared_private_memory_unavailable'}) from None
    return JSONResponse(result,headers=HEADERS)

@router.get('/conversations/{room_id}/shared-private-memory/receipts/{root_event_id}')
def shared_private_memory_receipt(room_id:int,root_event_id:str,agent_id:int,call_id:str,session_generation:str,
                                  request:Request,owner:User=Depends(get_current_user),db:Session=Depends(get_db)):
    from app.services.shared_private_memory import read_receipt
    from app.services.llm_service import LLMFailure
    with request.app.state.owner_recovery_lock:
        adapter=getattr(request.app.state,'team_connection',None)
        if not adapter or adapter.generation!=session_generation:raise HTTPException(409,{'code':'team_session_changed'})
        try:receipt=read_receipt(db,owner.id,room_id,root_event_id,agent_id,call_id)
        except LLMFailure:raise HTTPException(404,{'code':'shared_private_memory_unavailable'}) from None
        if receipt is None:raise HTTPException(404,{'code':'shared_private_memory_receipt_not_found'})
        if getattr(request.app.state,'team_connection',None) is not adapter or adapter.generation!=session_generation:
            raise HTTPException(409,{'code':'team_session_changed'})
        return JSONResponse({'receipt':receipt,'session_generation':session_generation},headers=HEADERS)
