"""Explicit recipient connector consent; separate from risky-action approval."""
import json
import asyncio
from fastapi import APIRouter,Depends,HTTPException,Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.api.routes.auth import get_current_user
from app.api.routes.shared_text_runtime import authority
from app.core.database import get_db
from app.models.models import User
from app.models.shared_connector import SharedConnectorLink,SharedConnectorPreference,SharedConnectorRequest,SharedConnectorCommand
from app.models.shared_connector_clear_all import SharedConnectorClearBarrier
from app.schemas.shared_connector import ConnectorCommand,ClearAllConnectors,RecoverClearAll
from app.models.shared_connector_clear_owner import SharedConnectorClearOwner
from app.services import shared_connector as service

router=APIRouter()
HEADERS={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}

def out(value):return JSONResponse(value,headers=HEADERS)
async def capable(request):
 bridge=getattr(request.app.state,'mcp_client',None)
 if not bridge or getattr(bridge,'available',False) is not True:return False
 try:
  result=await asyncio.wait_for(bridge.shared_capabilities({'version':1}),3)
  return result.get('state')=='completed' and result.get('result')=={'version':1,'shared_recipient_mcp':True}
 except Exception:return False

@router.get('/team-connection/shared-connectors')
async def list_connectors(publication_id:str,session_generation:str,request:Request,owner:User=Depends(get_current_user),db:Session=Depends(get_db)):
 supported=await capable(request)
 with request.app.state.owner_recovery_lock:
  adapter=authority(request,owner,session_generation,publication_id)
  result=service.list_for(db,owner.id,adapter,publication_id)
  result['shared_personal_apps_available']=supported and bool(getattr(request.app.state,'shared_connector_runtime_ready',False))
  return out(result)

@router.get('/team-connection/shared-connectors/commands/{operation_id}')
def receipt(operation_id:str,publication_id:str,session_generation:str,request:Request,owner:User=Depends(get_current_user),db:Session=Depends(get_db)):
 with request.app.state.owner_recovery_lock:
  adapter=authority(request,owner,session_generation,publication_id)
  row=db.get(SharedConnectorCommand,(owner.id,adapter.issuer,adapter._subject,operation_id))
  if not row or row.team_id!=adapter.team_id or row.publication_id!=publication_id or row.key_fingerprint!=adapter.key_fingerprint:raise HTTPException(404,{'code':'shared_connector_receipt_not_found'})
  return out({'receipt':json.loads(row.response),'session_generation':adapter.generation})

@router.post('/team-connection/shared-connectors/commands')
async def command(value:ConnectorCommand,request:Request,owner:User=Depends(get_current_user),db:Session=Depends(get_db)):
 if not await capable(request) or not getattr(request.app.state,'shared_connector_runtime_ready',False):
  raise HTTPException(503,{'code':'shared_personal_apps_unavailable'})
 bridge=request.app.state.mcp_client
 with request.app.state.owner_recovery_lock:
  adapter=authority(request,owner,value.session_generation,value.publication_id);epoch=adapter.owner_epoch;core=bridge.core_epoch
  db.rollback();db.execute(text('BEGIN IMMEDIATE'))
  try:
   service.require_clear_barrier_absent(db,owner.id,adapter)
   command,new=service.begin_command(db,owner.id,adapter,value)
   if not new:
    result=json.loads(command.response);db.commit();return out({'receipt':result,'session_generation':adapter.generation})
   link=db.query(SharedConnectorLink).filter_by(owner_id=owner.id,issuer=adapter.issuer,subject=adapter._subject,server_id=value.server_id).first()
   if value.expected_preference_revision is not None and (not link or link.preferences_revision!=value.expected_preference_revision):service.fail('shared_connector_preference_conflict')
   need_revoke=value.operation in ('unlink','clear') or (value.operation=='decide' and value.choice in ('bot','all'))
   if value.operation=='link' and link:
    server=db.get(service.MCPServer,value.server_id)
    need_revoke=bool(not server or link.state!='active' or link.server_revision!=server.revision or link.credential_ref_sha256!=service.fingerprint(server))
   targets=[]
   if link and need_revoke:
    for row in db.query(SharedConnectorRequest).filter(SharedConnectorRequest.link_id==link.link_id,SharedConnectorRequest.state.in_(('allowed','consumed'))).all():
     targets.append((row.request_id,row.authority_id,row.session_generation,row.state=='consumed'));row.state='uncertain'
    if targets:link.state='uncertain'
   db.commit()
  except BaseException:db.rollback();raise
 for _,authority_id,session,_ in targets:
  try:
   result=await asyncio.wait_for(bridge.shared_revoke({'version':1,'owner_id':owner.id,'owner_epoch':epoch,'core_epoch':core,'authority_id':authority_id,'session_generation':session}),3)
   if result.get('state')!='revoked':raise ValueError()
  except Exception:
   with request.app.state.owner_recovery_lock:
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    row=db.get(SharedConnectorCommand,(owner.id,adapter.issuer,adapter._subject,value.operation_id))
    if row:row.state='uncertain';row.response=json.dumps({'operation_id':value.operation_id,'state':'uncertain'});db.commit()
   raise HTTPException(503,{'code':'shared_connector_revoke_unconfirmed'}) from None
 with request.app.state.owner_recovery_lock:
  try:
   current=authority(request,owner,value.session_generation,value.publication_id)
   if current is not adapter or bridge.core_epoch!=core:raise ValueError()
   db.rollback();db.execute(text('BEGIN IMMEDIATE'))
   command=db.get(SharedConnectorCommand,(owner.id,adapter.issuer,adapter._subject,value.operation_id))
   if not command or command.state!='pending':raise ValueError()
   link=db.query(SharedConnectorLink).filter_by(owner_id=owner.id,issuer=adapter.issuer,subject=adapter._subject,server_id=value.server_id).first()
   if targets:
    if not link or link.state!='uncertain':raise ValueError()
    for request_id,_,_,_ in targets:
     row=db.get(SharedConnectorRequest,request_id)
     if not row or row.state!='uncertain':raise ValueError()
     row.state='revoked'
    link.state='active';db.flush()
   result=service.apply_command(db,owner.id,adapter,value)
   if any(inflight for _,_,_,inflight in targets):result['inflight_effects']='may_be_uncertain'
   response={'version':1,'operation_id':value.operation_id,'state':'committed','result':result}
   command.state='committed';command.response=json.dumps(response,sort_keys=True,separators=(',',':'));db.commit()
   return out({'receipt':response,'session_generation':adapter.generation})
  except BaseException:
   db.rollback()
   db.execute(text('BEGIN IMMEDIATE'))
   command=db.get(SharedConnectorCommand,(owner.id,adapter.issuer,adapter._subject,value.operation_id))
   if command and command.state=='pending':command.state='uncertain';command.response=json.dumps({'operation_id':value.operation_id,'state':'uncertain'});db.commit()
   raise


@router.get('/team-connection/shared-connectors/clear-all-preview')
def clear_all_preview(publication_id:str,session_generation:str,request:Request,owner:User=Depends(get_current_user),db:Session=Depends(get_db)):
 with request.app.state.owner_recovery_lock:
  adapter=authority(request,owner,session_generation,publication_id)
  snapshot=service.clear_all_snapshot(db,owner.id,adapter,publication_id)
  barrier=db.get(SharedConnectorClearBarrier,(owner.id,adapter.issuer,adapter._subject,adapter.key_fingerprint))
  original=db.get(SharedConnectorCommand,(owner.id,adapter.issuer,adapter._subject,barrier.operation_id)) if barrier else None
  recovery_operation_id=barrier.operation_id if original and original.team_id==adapter.team_id and original.publication_id==publication_id and original.key_fingerprint==adapter.key_fingerprint else None
  return out({'version':1,'owner_id':owner.id,'issuer':adapter.issuer,'account_id':adapter._subject,
   'team_id':adapter.team_id,'publication_id':publication_id,'profile_id':adapter.profile_id,
   'key_fingerprint':adapter.key_fingerprint,'session_generation':adapter.generation,
   'links':snapshot['links'],'snapshot_digest':snapshot['digest'],'state':snapshot['state'],
   'recovery_operation_id':recovery_operation_id})


async def _clear_all(value:ClearAllConnectors,request:Request,owner:User=Depends(get_current_user),db:Session=Depends(get_db)):
 if not await capable(request) or not getattr(request.app.state,'shared_connector_runtime_ready',False):
  raise HTTPException(503,{'code':'shared_personal_apps_unavailable'})
 bridge=request.app.state.mcp_client
 with request.app.state.owner_recovery_lock:
  adapter=authority(request,owner,value.session_generation,value.publication_id);epoch=adapter.owner_epoch;core=bridge.core_epoch
  db.rollback();db.execute(text('BEGIN IMMEDIATE'))
  try:
   key=(owner.id,adapter.issuer,adapter._subject,value.operation_id)
   digest=service.mcp_registry.digest({'operation':'clear_all',**value.model_dump(mode='json')})
   old=db.get(SharedConnectorCommand,key)
   if old:
    if old.request_digest!=digest or old.session_generation!=adapter.generation or old.key_fingerprint!=adapter.key_fingerprint:
     service.fail('shared_connector_operation_conflict')
    result=json.loads(old.response);db.commit();return out({'receipt':result,'session_generation':adapter.generation})
   snapshot=service.clear_all_snapshot(db,owner.id,adapter,value.publication_id)
   if snapshot['digest']!=value.expected_snapshot_digest:service.fail('shared_connector_snapshot_changed')
   if snapshot['state']!='ready':service.fail('shared_connector_clear_unavailable')
   frozen=snapshot['private'];targets=[]
   for link in frozen:
    for item in link['requests']:
     if item['owner_epoch']!=epoch:service.fail('shared_connector_clear_unavailable')
     targets.append(item)
   command=SharedConnectorCommand(owner_id=owner.id,issuer=adapter.issuer,subject=adapter._subject,
    key_fingerprint=adapter.key_fingerprint,operation_id=value.operation_id,request_digest=digest,
    session_generation=adapter.generation,team_id=adapter.team_id,publication_id=value.publication_id,
    state='pending',response=json.dumps({'version':1,'operation_id':value.operation_id,'state':'pending','operation':'clear_all'}))
   db.add(command)
   if targets:
    db.add(SharedConnectorClearBarrier(owner_id=owner.id,issuer=adapter.issuer,subject=adapter._subject,
       key_fingerprint=adapter.key_fingerprint,operation_id=value.operation_id,state='pending'))
    db.add(SharedConnectorClearOwner(owner_id=owner.id,issuer=adapter.issuer,subject=adapter._subject,
       key_fingerprint=adapter.key_fingerprint,operation_id=value.operation_id,instance_id=request.app.state.shared_clear_instance))
    for item in targets:
     row=db.get(SharedConnectorRequest,item['request_id']);row.state='uncertain'
   else:
    if authority(request,owner,value.session_generation,value.publication_id) is not adapter:raise ValueError()
    cleared_preferences=0;cleared_connections=0
    for item in frozen:
     if item['preferences']:
      link=db.get(SharedConnectorLink,item['link_id']);link.preferences_revision+=1;cleared_connections+=1
      for pref in item['preferences']:
       row=db.get(SharedConnectorPreference,pref['consent_id']);row.state='revoked';row.revision+=1;cleared_preferences+=1
    result={'operation':'clear_all','cleared_connections':cleared_connections,'cleared_preferences':cleared_preferences}
    response={'version':1,'operation_id':value.operation_id,'state':'committed','result':result}
    command.state='committed';command.response=json.dumps(response,sort_keys=True,separators=(',',':'))
   db.commit()
   if not targets:return out({'receipt':response,'session_generation':adapter.generation})
  except BaseException:db.rollback();raise

 async def revoke(item):
  try:
   result=await asyncio.wait_for(bridge.shared_revoke({'version':1,'owner_id':owner.id,'owner_epoch':epoch,
      'core_epoch':core,'authority_id':item['authority_id'],'session_generation':item['session_generation']}),3)
   return isinstance(result,dict) and result.get('state')=='revoked'
  except Exception:return False
 try:results=await asyncio.wait_for(asyncio.gather(*(revoke(item) for item in targets)),5)
 except Exception:results=[]

 with request.app.state.owner_recovery_lock:
  try:
   if len(results)!=len(targets) or not all(results):raise ValueError()
   current=authority(request,owner,value.session_generation,value.publication_id)
   if current is not adapter or not bridge.available or bridge.core_epoch!=core:raise ValueError()
   db.rollback();db.execute(text('BEGIN IMMEDIATE'))
   command=db.get(SharedConnectorCommand,(owner.id,adapter.issuer,adapter._subject,value.operation_id))
   barrier=db.get(SharedConnectorClearBarrier,(owner.id,adapter.issuer,adapter._subject,adapter.key_fingerprint))
   if not command or command.state!='pending' or not barrier or barrier.operation_id!=value.operation_id or barrier.state!='pending':raise ValueError()
   current_snapshot=service.clear_all_snapshot(db,owner.id,adapter,value.publication_id)['private']
   original_states={item['request_id']:item['state'] for item in targets}
   for link in current_snapshot:
    for item in link['requests']:
     if item['request_id'] in original_states:
      if item['state']!='uncertain':raise ValueError()
      item['state']=original_states[item['request_id']]
   if current_snapshot!=frozen:raise ValueError()
   cleared_preferences=0;cleared_connections=0
   for item in frozen:
    if item['preferences']:
     link=db.get(SharedConnectorLink,item['link_id']);link.preferences_revision+=1;cleared_connections+=1
     for pref in item['preferences']:
      row=db.get(SharedConnectorPreference,pref['consent_id']);row.state='revoked';row.revision+=1;cleared_preferences+=1
   for item in targets:
    row=db.get(SharedConnectorRequest,item['request_id'])
    row.state='uncertain' if item['state']=='consumed' else 'revoked'
   result={'operation':'clear_all','cleared_connections':cleared_connections,'cleared_preferences':cleared_preferences}
   if any(item['state']=='consumed' for item in targets):result['inflight_effects']='may_be_uncertain'
   response={'version':1,'operation_id':value.operation_id,'state':'committed','result':result}
   command.state='committed';command.response=json.dumps(response,sort_keys=True,separators=(',',':'));db.delete(barrier)
   fence=db.get(SharedConnectorClearOwner,(owner.id,adapter.issuer,adapter._subject,adapter.key_fingerprint))
   if fence:db.delete(fence)
   db.commit()
   return out({'receipt':response,'session_generation':adapter.generation})
  except BaseException:
   db.rollback()
   try:
    db.execute(text('BEGIN IMMEDIATE'))
    command=db.get(SharedConnectorCommand,(owner.id,adapter.issuer,adapter._subject,value.operation_id))
    barrier=db.get(SharedConnectorClearBarrier,(owner.id,adapter.issuer,adapter._subject,adapter.key_fingerprint))
    if command and command.state=='pending':
     command.state='uncertain';command.response=json.dumps({'version':1,'operation_id':value.operation_id,'state':'uncertain','operation':'clear_all'})
    if barrier and barrier.operation_id==value.operation_id:barrier.state='uncertain'
    db.commit()
   except BaseException:db.rollback()
   raise HTTPException(503,{'code':'shared_connector_clear_unconfirmed'}) from None


@router.post('/team-connection/shared-connectors/clear-all')
async def clear_all(value:ClearAllConnectors,request:Request,owner:User=Depends(get_current_user),db:Session=Depends(get_db)):
 key=(owner.id,value.operation_id)
 with request.app.state.owner_recovery_lock:
  if key in request.app.state.shared_clear_live:service.fail('shared_connector_operation_active')
  request.app.state.shared_clear_live.add(key)
 try:return await _clear_all(value,request,owner,db)
 finally:
  with request.app.state.owner_recovery_lock:request.app.state.shared_clear_live.discard(key)


def recovery_snapshot(request,db,owner,adapter,publication_id,operation_id):
 key=(owner.id,adapter.issuer,adapter._subject,adapter.key_fingerprint)
 barrier=db.get(SharedConnectorClearBarrier,key);fence=db.get(SharedConnectorClearOwner,key)
 old=db.get(SharedConnectorCommand,(owner.id,adapter.issuer,adapter._subject,operation_id))
 if (not barrier or barrier.operation_id!=operation_id or not fence or fence.operation_id!=operation_id
     or not old or old.team_id!=adapter.team_id or old.publication_id!=publication_id
     or old.key_fingerprint!=adapter.key_fingerprint or old.state not in ('pending','uncertain')):
  raise HTTPException(404,{'code':'shared_connector_recovery_not_found'})
 snap=service.clear_all_snapshot(db,owner.id,adapter,publication_id)
 live=request.app.state.shared_clear_live;instance=request.app.state.shared_clear_instance
 active=(fence.instance_id==instance and (owner.id,operation_id) in live) or (fence.recovery_instance_id==instance and (owner.id,fence.recovery_id) in live)
 if active:state='active';reason='handler_live'
 elif not request.app.state.shared_clear_fence_ready:state='unavailable';reason='fence_unavailable'
 elif any(item['link_state']=='uncertain' for item in snap['private']):state='unavailable';reason='link_uncertain'
 else:state='recoverable';reason='handler_finished' if fence.instance_id==instance else 'orphan_fenced'
 digest=service.mcp_registry.digest({'version':1,'operation':'clear_all_recovery','original_operation_id':operation_id,
  'old_state':old.state,'barrier_state':barrier.state,'instance':fence.instance_id,'recovery_id':fence.recovery_id,
  'recovery_instance':fence.recovery_instance_id,'snapshot':snap['digest']})
 return {'version':1,'owner_id':owner.id,'issuer':adapter.issuer,'account_id':adapter._subject,'team_id':adapter.team_id,
  'publication_id':publication_id,'profile_id':adapter.profile_id,'key_fingerprint':adapter.key_fingerprint,
  'session_generation':adapter.generation,'original_operation_id':operation_id,'old_status':'unknown',
  'state':state,'reason':reason,'links':snap['links'],'snapshot_digest':digest},snap,barrier,fence,old

@router.get('/team-connection/shared-connectors/clear-all-recovery-preview')
def recovery_preview(operation_id:str,publication_id:str,session_generation:str,request:Request,owner:User=Depends(get_current_user),db:Session=Depends(get_db)):
 with request.app.state.owner_recovery_lock:
  adapter=authority(request,owner,session_generation,publication_id)
  preview,*_=recovery_snapshot(request,db,owner,adapter,publication_id,operation_id)
  return out(preview)

@router.post('/team-connection/shared-connectors/clear-all-recover')
async def recover_clear_all(value:RecoverClearAll,request:Request,owner:User=Depends(get_current_user),db:Session=Depends(get_db)):
 if not await capable(request) or not getattr(request.app.state,'shared_connector_runtime_ready',False):
  raise HTTPException(503,{'code':'shared_personal_apps_unavailable'})
 bridge=request.app.state.mcp_client;live=request.app.state.shared_clear_live;live_key=(owner.id,value.operation_id)
 with request.app.state.owner_recovery_lock:
  adapter=authority(request,owner,value.session_generation,value.publication_id);epoch=adapter.owner_epoch;core=bridge.core_epoch
  db.rollback();db.execute(text('BEGIN IMMEDIATE'))
  try:
   command_key=(owner.id,adapter.issuer,adapter._subject,value.operation_id)
   digest=service.mcp_registry.digest({'operation':'clear_all_recovery',**value.model_dump(mode='json')})
   existing=db.get(SharedConnectorCommand,command_key)
   if existing:
    if existing.request_digest!=digest or existing.session_generation!=adapter.generation or existing.key_fingerprint!=adapter.key_fingerprint:service.fail('shared_connector_operation_conflict')
    result=json.loads(existing.response);db.commit();return out({'receipt':result,'session_generation':adapter.generation})
   preview,snapshot,barrier,fence,old=recovery_snapshot(request,db,owner,adapter,value.publication_id,value.original_operation_id)
   if preview['snapshot_digest']!=value.expected_snapshot_digest:service.fail('shared_connector_snapshot_changed')
   if preview['state']!='recoverable':service.fail('shared_connector_recovery_unavailable')
   frozen=snapshot['private'];targets=[item for link in frozen for item in link['requests']]
   if any(item['owner_epoch']!=epoch for item in targets):service.fail('shared_connector_recovery_unavailable')
   if live_key in live:service.fail('shared_connector_operation_active')
   live.add(live_key)
   if fence.recovery_id:
    previous=db.get(SharedConnectorCommand,(owner.id,adapter.issuer,adapter._subject,fence.recovery_id))
    if previous and previous.state=='pending':
     previous.state='uncertain';previous.response=json.dumps({'version':1,'operation_id':fence.recovery_id,'state':'uncertain','operation':'clear_all_recovery'})
   fence.recovery_id=value.operation_id;fence.recovery_instance_id=request.app.state.shared_clear_instance;barrier.state='pending'
   if old.state=='pending':
    old.state='uncertain';old.response=json.dumps({'version':1,'operation_id':value.original_operation_id,'state':'uncertain','operation':'clear_all'})
   for item in targets:db.get(SharedConnectorRequest,item['request_id']).state='uncertain'
   db.add(SharedConnectorCommand(owner_id=owner.id,issuer=adapter.issuer,subject=adapter._subject,key_fingerprint=adapter.key_fingerprint,
    operation_id=value.operation_id,request_digest=digest,session_generation=adapter.generation,team_id=adapter.team_id,
    publication_id=value.publication_id,state='pending',response=json.dumps({'version':1,'operation_id':value.operation_id,'state':'pending','operation':'clear_all_recovery'})))
   db.commit()
  except BaseException:
   db.rollback();live.discard(live_key);raise
 try:
  async def revoke(item):
   try:
    result=await asyncio.wait_for(bridge.shared_revoke({'version':1,'owner_id':owner.id,'owner_epoch':epoch,'core_epoch':core,
      'authority_id':item['authority_id'],'session_generation':item['session_generation']}),3)
    return isinstance(result,dict) and result.get('state')=='revoked'
   except Exception:return False
  try:results=await asyncio.wait_for(asyncio.gather(*(revoke(item) for item in targets)),5)
  except Exception:results=[]
  with request.app.state.owner_recovery_lock:
   try:
    if len(results)!=len(targets) or not all(results):raise ValueError()
    if authority(request,owner,value.session_generation,value.publication_id) is not adapter or not bridge.available or bridge.core_epoch!=core:raise ValueError()
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    key=(owner.id,adapter.issuer,adapter._subject,adapter.key_fingerprint)
    barrier=db.get(SharedConnectorClearBarrier,key);fence=db.get(SharedConnectorClearOwner,key);command=db.get(SharedConnectorCommand,command_key)
    if not barrier or barrier.operation_id!=value.original_operation_id or not fence or fence.recovery_id!=value.operation_id or not command or command.state!='pending':raise ValueError()
    current=service.clear_all_snapshot(db,owner.id,adapter,value.publication_id)['private']
    states={item['request_id']:item['state'] for item in targets}
    for link in current:
     for item in link['requests']:
      if item['request_id'] in states:
       if item['state']!='uncertain':raise ValueError()
       item['state']=states[item['request_id']]
    if current!=frozen:raise ValueError()
    connections=preferences=0
    for item in frozen:
     if item['preferences']:
      link=db.get(SharedConnectorLink,item['link_id']);link.preferences_revision+=1;connections+=1
      for pref in item['preferences']:
       row=db.get(SharedConnectorPreference,pref['consent_id']);row.state='revoked';row.revision+=1;preferences+=1
     for item_request in item['requests']:db.get(SharedConnectorRequest,item_request['request_id']).state='revoked'
    result={'operation':'clear_all_recovery','original_operation_id':value.original_operation_id,'old_effect_unknown':True,
     'cleared_connections':connections,'cleared_preferences':preferences,'inflight_effects':'may_be_uncertain'}
    response={'version':1,'operation_id':value.operation_id,'state':'committed','result':result}
    command.state='committed';command.response=json.dumps(response,sort_keys=True,separators=(',',':'))
    db.delete(barrier);db.delete(fence);db.commit()
    return out({'receipt':response,'session_generation':adapter.generation})
   except BaseException:
    db.rollback()
    try:
     db.execute(text('BEGIN IMMEDIATE'))
     command=db.get(SharedConnectorCommand,command_key);barrier=db.get(SharedConnectorClearBarrier,(owner.id,adapter.issuer,adapter._subject,adapter.key_fingerprint))
     if command and command.state=='pending':command.state='uncertain';command.response=json.dumps({'version':1,'operation_id':value.operation_id,'state':'uncertain','operation':'clear_all_recovery'})
     if barrier and barrier.operation_id==value.original_operation_id:barrier.state='uncertain'
     db.commit()
    except BaseException:db.rollback()
    raise HTTPException(503,{'code':'shared_connector_recovery_unconfirmed'}) from None
 finally:
  with request.app.state.owner_recovery_lock:live.discard(live_key)
