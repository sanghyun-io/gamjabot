"""Authenticated local notification controls; no native or network send."""
import re
from fastapi import APIRouter,Depends,HTTPException,Query,Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.schemas.local_notification import Epoch,SettingWrite,Finish
from app.services import local_notification as service
router=APIRouter()
def out(value):return JSONResponse(value,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

def identity(request,owner,expected=None):
    provider=getattr(request.app.state,'owner_epoch_provider',None)
    broker=getattr(request.app.state,'auth_broker_client',None)
    try:
        epoch=provider.current(owner) if provider else None
        core=getattr(broker,'current_generation_fingerprint',None) if broker else None
    except Exception:raise HTTPException(503,{'code':'notification_owner_unavailable'}) from None
    if not isinstance(epoch,str) or not re.fullmatch(r'[a-f0-9]{64}',epoch) or not isinstance(core,str) or not re.fullmatch(r'[a-f0-9]{64}',core):
        raise HTTPException(503,{'code':'notification_owner_unavailable'})
    if expected is not None and expected!=epoch:raise HTTPException(409,{'code':'notification_owner_changed'})
    return epoch,core

def guard(request,owner,expected):
    initial=identity(request,owner,expected)
    def current():
        if identity(request,owner,expected)!=initial:raise HTTPException(409,{'code':'notification_owner_changed'})
    return initial[0],current

@router.get('/local-notifications/status')
def status(request:Request,owner:User=Depends(get_current_user)):
    epoch,_=identity(request,owner.id)
    return out({'version':1,'owner_id':owner.id,'owner_epoch':epoch})
@router.get('/local-notifications/settings')
def settings(request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    identity(request,owner.id)
    return out(service.settings(db,owner.id))
@router.put('/local-notifications/settings/{agent_id}')
def setting(agent_id:int,value:SettingWrite,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    epoch,current=guard(request,owner.id,value.expected_owner_epoch)
    return out(service.write_setting(db,owner.id,agent_id,value,current))
@router.get('/local-notifications/commands/{operation_id}')
def command(operation_id:str,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    identity(request,owner.id)
    return out(service.command(db,owner.id,operation_id))
@router.post('/local-notifications/commands/{agent_id}/resolve')
def resolve_command(agent_id:int,value:SettingWrite,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    _,current=guard(request,owner.id,value.expected_owner_epoch)
    return out(service.resolve_setting_command(db,owner.id,agent_id,value,current))
@router.post('/local-notifications/sessions')
def session(value:Epoch,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    epoch,current=guard(request,owner.id,value.expected_owner_epoch)
    return out(service.open_session(db,owner.id,epoch,current))
@router.post('/local-notifications/sessions/{session_id}/next')
def next_item(session_id:str,value:Epoch,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    epoch,current=guard(request,owner.id,value.expected_owner_epoch)
    return out(service.next_item(db,owner.id,epoch,session_id,current))
@router.post('/local-notifications/deliveries/{delivery_id}/finish')
def finish(delivery_id:str,value:Finish,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    epoch,current=guard(request,owner.id,value.expected_owner_epoch)
    return out(service.finish(db,owner.id,epoch,delivery_id,value.state,current))
@router.get('/local-notifications/deliveries/{delivery_id}')
def resolve(delivery_id:str,request:Request,expected_owner_epoch:str=Query(pattern=r'^[a-f0-9]{64}$'),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    epoch,current=guard(request,owner.id,expected_owner_epoch)
    result=service.resolve(db,owner.id,epoch,delivery_id);current();return out(result)

@router.get('/local-notifications/deliveries/{delivery_id}/ready')
def ready(delivery_id:str,request:Request,expected_owner_epoch:str=Query(pattern=r'^[a-f0-9]{64}$'),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    epoch,current=guard(request,owner.id,expected_owner_epoch)
    result=service.ready(db,owner.id,epoch,delivery_id);current();return out(result)
