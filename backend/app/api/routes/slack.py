"""Owner-scoped Slack configuration; secret material stays in Core."""
import asyncio, re
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.models.slack import SlackConfig
from app.schemas.slack import SlackConfigWrite, SlackReplyResolutionWrite
from app.services import slack as service
router = APIRouter()
def out(value): return JSONResponse(value,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

def identity(request, owner):
    bridge=getattr(request.app.state,'mcp_client',None)
    provider=getattr(request.app.state,'owner_epoch_provider',None)
    epoch=provider.current(owner) if provider else None
    core=getattr(bridge,'core_epoch',None)
    if (bridge is None or not bridge.available or not isinstance(epoch,str) or not re.fullmatch(r'[a-f0-9]{64}',epoch)
        or not isinstance(core,str) or not re.fullmatch(r'[a-f0-9]{64}',core)):
        raise HTTPException(503,{'code':'slack_core_unavailable'})
    return bridge, {'version':1,'owner_id':owner,'owner_epoch':epoch,'core_epoch':core}

def current(request,owner,bridge,expected):
    candidate, actual=identity(request,owner)
    if candidate is not bridge or actual != expected: raise HTTPException(409,{'code':'slack_owner_changed'})

def matches(result,row,binding):
    if not isinstance(result,dict) or result.get('state')!='completed' or not isinstance(result.get('result'),dict): return False
    r=result['result']; v=service.config_dto(row)
    keys=('owner_id','operation_id','account_id','credential_revision','enabled','workspace_id','app_id','channel_id','sender_ids','agent_id','project_id','connection_id','model_id')
    expected={k:(row.owner_id if k=='owner_id' else v[k]) for k in keys}
    return (all(r.get(k)==value for k,value in expected.items()) and r.get('config_revision')==row.revision
        and r.get('owner_epoch')==binding['owner_epoch'] and r.get('core_epoch')==binding['core_epoch']
        and 'pending' in r and r['pending'] is None and (not row.enabled or r.get('credential_state')=='stored'))

@router.get('/slack/targets')
def targets(db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.targets(db,owner.id))
@router.get('/slack/config')
def config(db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.config_dto(db.get(SlackConfig,owner.id)))
@router.get('/slack/events')
def events(db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.events(db,owner.id))
@router.get('/slack/replies/unresolved')
def unresolved_replies(request:Request,cursor:str|None=None,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    if getattr(request.state,'_gamja_mobile_binding',None) is not None:raise HTTPException(403,{'code':'slack_owner_required'})
    return out(service.unresolved_replies(db,owner.id,cursor))
@router.get('/slack/replies/{run_id}/resolution')
def reply_resolution(run_id:str,operation_id:str,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    if getattr(request.state,'_gamja_mobile_binding',None) is not None:raise HTTPException(403,{'code':'slack_owner_required'})
    from app.models.slack import SlackReplyResolution
    if not service.uuid_valid(run_id) or not service.uuid_valid(operation_id):raise HTTPException(422,{'code':'slack_resolution_invalid'})
    row=db.get(SlackReplyResolution,(owner.id,operation_id))
    if not row or row.run_id!=run_id:raise HTTPException(404,{'code':'slack_resolution_not_found'})
    return out({'version':1,'run_id':row.run_id,'operation_id':row.operation_id,'decision':row.decision})
@router.post('/slack/replies/{run_id}/resolution')
async def resolve_reply(run_id:str,value:SlackReplyResolutionWrite,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    if getattr(request.state,'_gamja_mobile_binding',None) is not None:raise HTTPException(403,{'code':'slack_owner_required'})
    async with request.app.state.slack_locks.setdefault(owner.id,asyncio.Lock()):
        with request.app.state.owner_recovery_lock:
            bridge,binding=identity(request,owner.id)
            if value.expected_owner_epoch!=binding['owner_epoch'] or value.expected_core_epoch!=binding['core_epoch']:
                raise HTTPException(409,{'code':'slack_owner_changed'})
            current(request,owner.id,bridge,binding)
            return out(service.resolve_reply(db,owner.id,binding['owner_epoch'],run_id,value))
@router.put('/slack/config')
async def configure(value:SlackConfigWrite,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    locks=request.app.state.slack_locks
    async with locks.setdefault(owner.id,asyncio.Lock()):
        return await configure_locked(value,request,db,owner)

async def configure_locked(value,request,db,owner):
    bridge,binding=identity(request,owner.id)
    if value.expected_owner_epoch!=binding['owner_epoch'] or value.expected_core_epoch!=binding['core_epoch']:
        raise HTTPException(409,{'code':'slack_owner_changed'})
    reserved=service.reserve(db,owner.id,value)
    try: current(request,owner.id,bridge,binding)
    except Exception: raise HTTPException(503,{'code':'slack_config_pending'}) from None
    # Prevent an existing run from continuing after an owner scope change.
    scheduler=request.app.state.scheduler
    with scheduler.sessions() as session:
        from app.models.slack import SlackRun
        rows=session.query(SlackRun).filter_by(owner_id=owner.id,state='running').all()
        for row in rows:
            token=scheduler._cancellations.get((row.room_id,row.agent_id))
            if token:token.set()
    payload={**binding,**value.model_dump(exclude={'expected_revision','expected_owner_epoch','expected_core_epoch'}),'expected_config_revision':value.expected_revision}
    try:
        result=await asyncio.wait_for(bridge.slack_configure(payload),10)
        current(request,owner.id,bridge,binding)
    except Exception:
        raise HTTPException(503,{'code':'slack_config_pending'}) from None
    row=db.get(SlackConfig,owner.id,populate_existing=True)
    if not row or not matches(result,row,binding): raise HTTPException(503,{'code':'slack_config_pending'})
    return out(service.finish(db,owner.id,value.operation_id,reserved['revision']))

@router.post('/slack/config/reconcile')
async def reconcile(request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    async with request.app.state.slack_locks.setdefault(owner.id,asyncio.Lock()):
        return await reconcile_locked(request,db,owner)

async def reconcile_locked(request,db,owner):
    row=db.get(SlackConfig,owner.id)
    if not row or row.state!='pending': return out(service.config_dto(row))
    operation_id=row.operation_id;revision=row.revision
    bridge,binding=identity(request,owner.id)
    try:
        result=await asyncio.wait_for(bridge.slack_status({**binding,'operation_id':operation_id,'config_revision':revision,'credential_revision':row.credential_revision}),5)
        current(request,owner.id,bridge,binding)
    except Exception: raise HTTPException(503,{'code':'slack_config_pending'}) from None
    db.expire_all();row=db.get(SlackConfig,owner.id)
    if not row or row.operation_id!=operation_id or row.revision!=revision or not matches(result,row,binding):
        raise HTTPException(503,{'code':'slack_config_pending'})
    return out(service.finish(db,owner.id,operation_id,revision))

@router.get('/slack/transport')
async def transport(request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    row=db.get(SlackConfig,owner.id)
    if not row or row.state!='configured': return out({'version':1,'state':'unconfirmed'})
    try:
        bridge,binding=identity(request,owner.id)
        result=await asyncio.wait_for(bridge.slack_status({**binding,'operation_id':row.operation_id,'config_revision':row.revision,'credential_revision':row.credential_revision}),5)
        current(request,owner.id,bridge,binding)
        db.expire_all();row=db.get(SlackConfig,owner.id)
        if not row or row.state!='configured' or not matches(result,row,binding): raise ValueError()
        status=result['result'].get('transport_state')
        if status not in ('disabled','connecting','connected','unavailable','disconnected','permission_required','identity_mismatch'): raise ValueError()
        return out({'version':1,'state':status})
    except Exception: return out({'version':1,'state':'unconfirmed'})

@router.get('/slack/commands/{operation_id}')
def command(operation_id:str,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    from app.models.slack import SlackCommand
    if not service.uuid_valid(operation_id): raise HTTPException(422,{'code':'slack_config_invalid'})
    row=db.get(SlackCommand,(owner.id,operation_id))
    if not row: raise HTTPException(404,{'code':'slack_command_not_found'})
    return out({'version':1,'operation_id':row.operation_id,'revision':row.revision,'state':row.state})
