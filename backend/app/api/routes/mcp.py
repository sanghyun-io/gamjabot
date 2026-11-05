import asyncio
import re
from fastapi import APIRouter,Depends,Header,Request,HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.models.models import User
from app.models.mcp import MCPBinding, MCPDiscovery
from app.schemas.mcp import MCPCreate,MCPEdit,MCPRevision,MCPDiscover,MCPBindingWrite,MCPCredential
from app.services import mcp_registry as service
router=APIRouter()
def out(v):return JSONResponse(v,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

_MCP_CHANNEL_REASONS=frozenset({'channel_absent','channel_unavailable','channel_eof','channel_protocol','channel_closed','channel_io','channel_error'})
_MCP_CONFIG_REASONS=frozenset({'mcp_configuration_epoch_changed','mcp_configuration_requires_revoke','mcp_owner_changed','mcp_configuration_changed','mcp_authority_unavailable','invalid_mcp_configuration','invalid_mcp_transport','mcp_transport_changed','invalid_mcp_url','mcp_binding_changed','invalid_mcp_binding'})

def transport_failure(stage,reason):
    # Deliberately expose only fixed categories; never include Core response or exception text.
    if stage not in {'channel','configure','bind'}:stage='channel'
    if reason not in _MCP_CHANNEL_REASONS | _MCP_CONFIG_REASONS:reason='channel_error'
    return HTTPException(503,{'code':'mcp_transport_unavailable','stage':stage,'reason':reason})

def channel_reason(bridge):
    if bridge is None:return 'channel_absent'
    reason=getattr(bridge,'failure_reason',None)
    return reason if isinstance(reason,str) and reason in _MCP_CHANNEL_REASONS else 'channel_unavailable'

def reply_reason(result):
    if isinstance(result,dict) and result.get('state')=='failed':
        code=result.get('code')
        if isinstance(code,str) and code in _MCP_CONFIG_REASONS:return code
    return 'channel_error'

def client_for(request):
    bridge=getattr(request.app.state,'mcp_client',None)
    methods=('configure','bind','revoke','issue','start','cancel','discover','credential_status')
    if bridge is None or getattr(bridge,'available',False) is not True or not all(callable(getattr(bridge,m,None)) for m in methods):
        raise transport_failure('channel',channel_reason(bridge))
    return bridge

def channel_identity(request,bridge,owner):
    provider=getattr(request.app.state,'owner_epoch_provider',None)
    epoch=provider.current(owner) if provider else None
    core=getattr(bridge,'core_epoch',None)
    if not isinstance(epoch,str) or not re.fullmatch(r'[a-f0-9]{64}',epoch) or not isinstance(core,str) or not re.fullmatch(r'[a-f0-9]{64}',core):
        raise HTTPException(503,{'code':'mcp_owner_epoch_unavailable'})
    return {'owner_id':owner,'owner_epoch':epoch,'core_epoch':core}

def server_lock(request,server_id):
    return service.server_lock(request.app.state,server_id)

async def revoke(bridge,identity,server_id):
    try:result=await bridge.revoke({'version':1,**identity,'server_id':server_id})
    except Exception:raise HTTPException(503,{'code':'mcp_transport_unavailable'}) from None
    if not isinstance(result,dict) or result.get('state')!='revoked':raise HTTPException(503,{'code':'mcp_transport_unavailable'})

async def publish(db,bridge,identity,server_id):
    owner=identity['owner_id']
    server=service.owned(db,owner,server_id)
    revision=server.revision
    payload={'version':1,**identity,'server_id':server_id,'config_revision':revision,
             'transport':server.transport,'transport_canonical':service.canonical(server.transport),'transport_sha256':service.digest(server.transport),'credential_ref':server.credential_ref}
    bindings=[{'version':1,**identity,'server_id':server_id,'config_revision':revision,'agent_id':b.agent_id,
               'binding_revision':b.revision,'enabled':True,'allowed_tools':service.allowed_tools(db,server,b)} for b in db.query(MCPBinding).filter_by(owner_id=owner,server_id=server_id,enabled=True)]
    db.rollback()  # No SQL transaction remains open during the Core await.
    try:result=await bridge.configure(payload)
    except Exception:raise transport_failure('configure',channel_reason(bridge) if not bridge.available else 'channel_error') from None
    if not isinstance(result,dict) or result.get('state')!='configured':raise transport_failure('configure',reply_reason(result))
    for item in bindings:
        try:result=await bridge.bind(item)
        except Exception:raise transport_failure('bind',channel_reason(bridge) if not bridge.available else 'channel_error') from None
        if not isinstance(result,dict) or result.get('state')!='bound':raise transport_failure('bind',reply_reason(result))


@router.get('/mcp/servers')
def list_servers(db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.listing(db,owner.id))
@router.post('/mcp/servers')
def create(value:MCPCreate,key:str=Header(alias='Idempotency-Key'),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.create(db,owner.id,value,key))
@router.get('/mcp/commands/{key}')
def command(key:str,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.command(db,owner.id,key))
@router.get('/mcp/servers/{server_id}')
def get_server(server_id:str,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.server_dto(db,service.owned(db,owner.id,server_id)))
@router.patch('/mcp/servers/{server_id}')
async def edit(server_id:str,value:MCPEdit,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    bridge=client_for(request);identity=channel_identity(request,bridge,owner.id)
    async with server_lock(request,server_id):
        row=service.owned(db,owner.id,server_id)
        if row.revision!=value.expected_revision:service.fail(409,'mcp_revision_conflict')
        db.rollback()
        await revoke(bridge,identity,server_id)
        result=service.edit(db,owner.id,server_id,value)
        if result['enabled']:await publish(db,bridge,identity,server_id)
        return out(result)
@router.post('/mcp/servers/{server_id}/credential-preview')
async def credential_preview(server_id:str,value:MCPRevision,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    bridge=client_for(request);identity=channel_identity(request,bridge,owner.id)
    async with server_lock(request,server_id):
        server=service.owned(db,owner.id,server_id)
        if server.revision!=value.expected_revision:service.fail(409,'mcp_revision_conflict')
        await publish(db,bridge,identity,server_id)
        return out({'version':1,'server':service.server_dto(db,server)})
@router.put('/mcp/servers/{server_id}/credential')
async def attach_credential(server_id:str,value:MCPCredential,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    import re
    bridge=client_for(request);identity=channel_identity(request,bridge,owner.id)
    async with server_lock(request,server_id):
        server=service.owned(db,owner.id,server_id)
        if server.revision!=value.expected_revision:service.fail(409,'mcp_revision_conflict')
        if server.transport.get('type')!='http':service.fail(409,'mcp_stdio_credential_unsupported')
        kind=service.validate_credential_target(owner.id,server,value.credential_ref)
        def current():
            if client_for(request) is not bridge or channel_identity(request,bridge,owner.id)!=identity:
                service.fail(409,'mcp_owner_epoch_unavailable')
        epoch_provider=getattr(request.app.state,'owner_epoch_provider',None)
        epoch=epoch_provider.current(owner.id) if epoch_provider else None
        if not isinstance(epoch,str) or not re.fullmatch(r'[a-f0-9]{64}',epoch):service.fail(409,'mcp_owner_epoch_unavailable')
        revision=server.revision
        await publish(db,bridge,identity,server_id)
        if kind=='notion_oauth':current()
        try:status=await bridge.credential_status({'version':1,'owner_id':owner.id,'owner_epoch':epoch,
            'core_epoch':bridge.core_epoch,'server_id':server_id,'config_revision':revision,'credential_ref':value.credential_ref})
        except Exception:raise HTTPException(503,{'code':'mcp_transport_unavailable'}) from None
        if not isinstance(status,dict) or status.get('state')!='present':service.fail(409,'mcp_credential_unavailable')
        if kind=='notion_oauth':current()
        await revoke(bridge,identity,server_id)
        if kind=='notion_oauth':current()
        result=service.attach_credential(db,owner.id,server_id,value,check_current=current if kind=='notion_oauth' else None)
        if result['enabled']:await publish(db,bridge,identity,server_id)
        return out(result)
@router.post('/mcp/servers/{server_id}/discovery-preview')
def preview(server_id:str,value:MCPRevision,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.preview(db,owner.id,server_id,value.expected_revision))
@router.post('/mcp/servers/{server_id}/discover')
async def discover(server_id:str,value:MCPDiscover,request:Request,key:str=Header(alias='Idempotency-Key'),db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    owner_id=owner.id  # Request sessions close before the background Core response arrives.
    bridge=client_for(request);identity=channel_identity(request,bridge,owner_id)
    async with server_lock(request,server_id):
        row=service.owned(db,owner_id,server_id)
        if row.revision!=value.expected_revision:service.fail(409,'mcp_revision_conflict')
        await publish(db,bridge,identity,server_id)
        receipt,run_id=service.claim(db,owner_id,server_id,value,key)
        if run_id is None:return out(receipt)
        run_row=db.get(MCPDiscovery,run_id)
        payload={'version':1,**identity,'server_id':row.server_id,'config_revision':row.revision,
                 'transport_sha256':service.digest(row.transport),'discovery_id':run_id,'preview_id':value.preview_id,
                 'executable_review':run_row.executable_review}
        sessions=request.app.state.session_factory
        async def run():
            stage='bridge'
            try:
                result=await bridge.discover(payload)
                stage='finalize'
                async with server_lock(request,server_id):
                    with sessions() as run_db:
                        state=result.get('state') if isinstance(result,dict) else None
                        if state=='completed':
                            # Revoke outstanding grants before replacing the discovery snapshot.
                            await revoke(bridge,identity,server_id)
                            try:service.finish(run_db,owner_id,run_id,result=result.get('result'))
                            except HTTPException as error:
                                detail=error.detail if isinstance(error.detail,dict) else {}
                                reason=detail.get('code')
                                if error.status_code!=422 or not isinstance(reason,str) or reason not in service.DISCOVERY_INVALID_REASONS:raise
                                service.finish(run_db,owner_id,run_id,error=reason)
                            current=service.owned(run_db,owner_id,server_id)
                            if current.enabled:await publish(run_db,bridge,identity,server_id)
                        elif state=='failed':service.finish(run_db,owner_id,run_id,error='mcp_discovery_failed')
                        else:
                            code=result.get('code') if isinstance(result,dict) else None
                            service.mark_unknown(run_db,owner_id,run_id,reason=code)
            except BaseException:
                reason=channel_reason(bridge)
                safe={'channel_eof':'mcp_disc_channel_eof','channel_protocol':'mcp_disc_channel_protocol',
                      'channel_closed':'mcp_disc_channel_closed','channel_io':'mcp_disc_channel_io'}
                failure=safe.get(reason,'mcp_disc_channel_error') if stage=='bridge' else 'mcp_disc_finalize_uncertain'
                with sessions() as run_db:service.mark_unknown(run_db,owner_id,run_id,reason=failure)
        tasks=getattr(request.app.state,'mcp_discovery_tasks',None)
        if tasks is None:tasks=set();request.app.state.mcp_discovery_tasks=tasks
        task=asyncio.create_task(run());tasks.add(task);task.add_done_callback(tasks.discard)
        return out(receipt)
@router.get('/mcp/servers/{server_id}/tools')
def tools(server_id:str,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.tools(db,owner.id,server_id))
@router.get('/bots/{agent_id}/mcp')
def bindings(agent_id:int,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    return out(service.bindings(db,owner.id,agent_id))
@router.put('/bots/{agent_id}/mcp/{server_id}')
async def bind(agent_id:int,server_id:str,value:MCPBindingWrite,request:Request,db:Session=Depends(get_db),owner:User=Depends(get_current_user)):
    bridge=client_for(request);identity=channel_identity(request,bridge,owner.id)
    async with server_lock(request,server_id):
        service.check_binding(db,owner.id,agent_id,server_id,value)
        db.rollback()
        await revoke(bridge,identity,server_id)
        result=service.bind(db,owner.id,agent_id,server_id,value)
        if service.owned(db,owner.id,server_id).enabled:await publish(db,bridge,identity,server_id)
        return out(result)
