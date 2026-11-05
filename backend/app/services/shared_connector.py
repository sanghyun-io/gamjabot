"""Recipient-bound connector links and user account-use decisions.

This is separate from risky MCP action approval. Core revocation must succeed
before an active authorization can be changed or discarded.
"""
import json
from hashlib import sha256
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import text
from app.models.models import Agent
from app.models.collaboration import RoomAgentRun,RoomEvent,ToolAction
from app.models.mcp import MCPServer,MCPBinding,MCPDiscovery,MCPTool
from app.models.shared_text_runtime import SharedTextRoom
from app.models.shared_connector_clear_all import SharedConnectorClearBarrier
from app.models.shared_connector import SharedConnectorLink,SharedConnectorPreference,SharedConnectorRequest,SharedConnectorCommand
from app.services import mcp_registry,shared_text_runtime
from app.services.events import ActorRef,stage_event


def fail(code,status=409):raise HTTPException(status,{'code':code})
def fingerprint(server):
 if not server.credential_ref:fail('shared_connector_credential_missing')
 mcp_registry.validate_credential_target(server.owner_id,server,server.credential_ref)
 return sha256(server.credential_ref.encode('utf-8')).hexdigest()
def room_for(db,owner,adapter,publication_id):
 row=db.query(SharedTextRoom).filter_by(owner_id=owner,issuer=adapter.issuer,subject=adapter._subject,team_id=adapter.team_id,publication_id=publication_id).first()
 if not row:fail('shared_room_not_found',404)
 return row

def require_clear_barrier_absent(db,owner,adapter):
 if db.get(SharedConnectorClearBarrier,(owner,adapter.issuer,adapter._subject,adapter.key_fingerprint)) is not None:
  fail('shared_connector_clear_in_progress')

def current_link(db,owner,adapter,server_id):
 require_clear_barrier_absent(db,owner,adapter)
 link=db.query(SharedConnectorLink).filter_by(owner_id=owner,server_id=server_id).first()
 server=db.get(MCPServer,server_id)
 if not link or link.state!='active' or link.issuer!=adapter.issuer or link.subject!=adapter._subject or link.key_fingerprint!=adapter.key_fingerprint:fail('shared_connector_unavailable',404)
 if not server or server.owner_id!=owner or not server.enabled or server.revision!=link.server_revision or fingerprint(server)!=link.credential_ref_sha256:
  fail('shared_connector_changed')
 return link,server

def link_view(link,server):
 return {'link_id':link.link_id,'server_id':link.server_id,'server_name':server.name,'server_revision':link.server_revision,
         'link_revision':link.revision,'preferences_revision':link.preferences_revision,'state':link.state,'provider_account_verified':False}

def list_for(db,owner,adapter,publication_id):
 room=room_for(db,owner,adapter,publication_id)
 servers=db.query(MCPServer).filter_by(owner_id=owner).order_by(MCPServer.name,MCPServer.server_id).limit(100).all()
 links=db.query(SharedConnectorLink).filter_by(owner_id=owner).all()
 by_server={row.server_id:row for row in links}
 items=[]
 for server in servers:
  link=by_server.get(server.server_id)
  if link and (link.issuer!=adapter.issuer or link.subject!=adapter._subject or link.key_fingerprint!=adapter.key_fingerprint):continue
  discovery=db.query(MCPDiscovery).filter_by(owner_id=owner,server_id=server.server_id,server_revision=server.revision,state='completed').first()
  binding=db.get(MCPBinding,(owner,room.agent_id,server.server_id))
  item={'server_id':server.server_id,'server_name':server.name,'server_revision':server.revision,'enabled':bool(server.enabled),
        'assigned_to_current_bot':bool(binding and binding.enabled and link and link.state=='active' and link.server_revision==server.revision and link.credential_ref_sha256==sha256((server.credential_ref or '').encode()).hexdigest()),
        'credential_configured':bool(server.credential_ref),'discovery_ready':bool(discovery),'provider_account_verified':False,
        'link':link_view(link,server) if link else None}
  if link:
   item['preferences']=[{'consent_id':p.consent_id,'scope':p.scope,'team_id':p.team_id,'publication_id':p.publication_id,
      'revision':p.revision,'state':p.state} for p in db.query(SharedConnectorPreference).filter_by(link_id=link.link_id,state='active').all()
      if p.scope=='all' or (p.team_id==adapter.team_id and p.publication_id==publication_id)]
   item['pending']=[request_view(r,link,server,db) for r in db.query(SharedConnectorRequest).filter_by(link_id=link.link_id,room_id=room.room_id,
      session_generation=adapter.generation,state='pending').all()]
  items.append(item)
 return {'version':1,'issuer':adapter.issuer,'account_id':adapter._subject,'team_id':adapter.team_id,'publication_id':publication_id,
         'session_generation':adapter.generation,'profile_id':adapter.profile_id,'key_fingerprint':adapter.key_fingerprint,'items':items,'owner_shared_apps_available':False}

def request_view(row,link,server,db):
 tool=db.get(MCPTool,row.tool_id)
 tool_name=tool.name if tool and tool.server_id==server.server_id and tool.server_revision==server.revision else None
 return {'tool_name':tool_name,'request_id':row.request_id,'root_event_id':row.root_event_id,'action_id':row.action_id,'room_id':row.room_id,
  'agent_id':row.agent_id,'run_generation':row.run_generation,'server_id':server.server_id,'server_name':server.name,
  'server_revision':server.revision,'link_id':link.link_id,'link_revision':link.revision,'session_generation':row.session_generation,
  'state':row.state,'choices':['once','bot','all','skip']}

def begin_command(db,owner,adapter,value):
 key=(owner,adapter.issuer,adapter._subject,value.operation_id)
 digest=mcp_registry.digest(value.model_dump(mode='json'))
 prior=db.get(SharedConnectorCommand,key)
 if prior:
  if prior.request_digest!=digest or prior.session_generation!=adapter.generation or prior.key_fingerprint!=adapter.key_fingerprint:fail('shared_connector_operation_conflict')
  return prior,False
 row=SharedConnectorCommand(owner_id=owner,issuer=adapter.issuer,subject=adapter._subject,operation_id=value.operation_id,
  request_digest=digest,key_fingerprint=adapter.key_fingerprint,session_generation=adapter.generation,team_id=adapter.team_id,publication_id=value.publication_id,
  state='pending',response=json.dumps({'operation_id':value.operation_id,'state':'pending'}))
 db.add(row);db.flush();return row,True

def apply_command(db,owner,adapter,value):
 require_clear_barrier_absent(db,owner,adapter)
 room=room_for(db,owner,adapter,value.publication_id)
 server=db.query(MCPServer).filter_by(owner_id=owner,server_id=value.server_id).first()
 if not server:fail('shared_connector_unavailable',404)
 link=db.query(SharedConnectorLink).filter_by(owner_id=owner,server_id=server.server_id).first()
 revision=link.revision if link else 0
 if revision!=value.expected_revision:fail('shared_connector_conflict')
 if value.expected_preference_revision is not None and (not link or link.preferences_revision!=value.expected_preference_revision):fail('shared_connector_preference_conflict')
 if value.operation=='link':
  if link and (link.issuer!=adapter.issuer or link.subject!=adapter._subject or link.key_fingerprint!=adapter.key_fingerprint):fail('shared_connector_account_conflict')
  if not server.enabled or not server.credential_ref:fail('shared_connector_credential_missing')
  ref_hash=fingerprint(server)
  if not db.query(MCPDiscovery).filter_by(owner_id=owner,server_id=server.server_id,server_revision=server.revision,state='completed').first():fail('shared_connector_discovery_required')
  if link is None:
   link=SharedConnectorLink(link_id=str(uuid4()),owner_id=owner,issuer=adapter.issuer,subject=adapter._subject,key_fingerprint=adapter.key_fingerprint,server_id=server.server_id,server_revision=server.revision,credential_ref_sha256=ref_hash,revision=1,preferences_revision=1,state='active');db.add(link);db.flush()
  elif link.state!='active' or link.server_revision!=server.revision or link.credential_ref_sha256!=ref_hash:
   if db.query(SharedConnectorRequest).filter(SharedConnectorRequest.link_id==link.link_id,SharedConnectorRequest.state.in_(('allowed','consumed','uncertain'))).first():fail('shared_connector_revoke_required')
   link.server_revision=server.revision;link.credential_ref_sha256=ref_hash;link.revision+=1;link.state='active'
  binding=db.get(MCPBinding,(owner,room.agent_id,server.server_id))
  if binding is None:
   binding=MCPBinding(owner_id=owner,agent_id=room.agent_id,server_id=server.server_id,revision=1,enabled=True,tool_ids=None);db.add(binding)
  elif not binding.enabled:
   binding.revision+=1;binding.enabled=True
  db.flush();mcp_registry.offered(db,owner,room.agent_id)
 elif value.operation in ('unlink','clear'):
  link,server=current_link(db,owner,adapter,server.server_id)
  if db.query(SharedConnectorRequest).filter(SharedConnectorRequest.link_id==link.link_id,SharedConnectorRequest.state.in_(('allowed','consumed','uncertain'))).first():fail('shared_connector_revoke_required')
  for pref in db.query(SharedConnectorPreference).filter_by(link_id=link.link_id,state='active').all():pref.state='revoked';pref.revision+=1
  link.preferences_revision+=1
  for pending in db.query(SharedConnectorRequest).filter_by(link_id=link.link_id,state='pending').all():pending.state='revoked'
  if value.operation=='unlink':
   link.state='revoked'
   for b in db.query(MCPBinding).join(SharedTextRoom,SharedTextRoom.agent_id==MCPBinding.agent_id).filter(MCPBinding.owner_id==owner,MCPBinding.server_id==server.server_id,SharedTextRoom.issuer==link.issuer,SharedTextRoom.subject==link.subject).all():b.enabled=False;b.revision+=1
  link.revision+=1
 elif value.operation=='decide':
  link,server=current_link(db,owner,adapter,server.server_id)
  pending=db.query(SharedConnectorRequest).filter_by(request_id=value.request_id,link_id=link.link_id,room_id=room.room_id,
   session_generation=adapter.generation,team_id=adapter.team_id,publication_id=value.publication_id,state='pending').first()
  if not pending:fail('shared_connector_request_changed')
  run=db.get(RoomAgentRun,(room.room_id,room.agent_id));root=db.get(RoomEvent,pending.root_event_id);action=db.get(ToolAction,pending.action_id)
  if not run or run.state!='running' or run.generation!=pending.run_generation or run.trigger_event_id!=pending.root_event_id or not root or root.actor_type!='user' or root.actor_id!=owner or (action and (action.room_id!=room.room_id or action.trigger_event_id!=root.event_id)):fail('shared_connector_request_changed')
  if value.choice=='skip':pending.state='skipped'
  else:
   pending.state='allowed'
   if value.choice=='once':pending.consent_id=str(uuid4());pending.consent_scope='once';pending.consent_revision=1;pending.preference_revision=link.preferences_revision
   else:
    scope=value.choice;team=adapter.team_id if scope=='bot' else '*';publication=value.publication_id if scope=='bot' else '*'
    pref=db.query(SharedConnectorPreference).filter_by(link_id=link.link_id,scope=scope,team_id=team,publication_id=publication).first()
    if not pref:pref=SharedConnectorPreference(consent_id=str(uuid4()),link_id=link.link_id,scope=scope,team_id=team,publication_id=publication,revision=0,state='revoked');db.add(pref)
    pref.revision+=1;pref.state='active';link.preferences_revision+=1;pending.consent_id=pref.consent_id;pending.consent_scope=scope;pending.consent_revision=pref.revision;pending.preference_revision=link.preferences_revision
 db.flush()
 return {'version':1,'operation':value.operation,'server_id':server.server_id,'link':link_view(link,server),
         **({'request':request_view(pending,link,server,db)} if value.operation=='decide' else {})}


def stage_request(db,app_state,turn,meta,action_id):
    """Caller holds Owner lock, SQLite writer transaction and owns the commit."""
    adapter=getattr(app_state,'team_connection',None)
    if not adapter:fail('shared_session_unavailable')
    shared_text_runtime.verify(app_state,db,turn.room_id,turn.agent_id,turn.root_id,turn.generation,turn)
    link,server=current_link(db,turn.owner_id,adapter,meta['server_id'])
    mcp_registry.current_tool(db,turn.owner_id,turn.agent_id,meta)
    request=SharedConnectorRequest(request_id=str(uuid4()),link_id=link.link_id,authority_id=str(uuid4()),
        room_id=turn.room_id,agent_id=turn.agent_id,root_event_id=turn.root_id,run_generation=turn.generation,
        action_id=action_id,session_generation=turn.session_generation,owner_epoch=turn.owner_epoch,publication_revision=turn.revision,revocation_epoch=turn.revocation_epoch,authority_revision=turn.authority_revision,team_id=turn.team_id,publication_id=turn.publication_id,
        state='pending',tool_id=meta['tool_id'],consent_revision=0,preference_revision=0)
    pref=db.query(SharedConnectorPreference).filter_by(link_id=link.link_id,scope='bot',team_id=turn.team_id,publication_id=turn.publication_id,state='active').first()
    if pref is None:pref=db.query(SharedConnectorPreference).filter_by(link_id=link.link_id,scope='all',team_id='*',publication_id='*',state='active').first()
    if pref:
        request.state='allowed';request.consent_id=pref.consent_id;request.consent_scope=pref.scope
        request.consent_revision=pref.revision;request.preference_revision=link.preferences_revision
    db.add(request);db.flush()
    stage_event(db,turn.room_id,ActorRef('system',None),'activity',{'category':'shared_connector_consent',
        'status':request.state,'trigger_event_id':turn.root_id,'request_id':request.request_id,'action_id':action_id},
        'shared-connector:'+request.request_id)
    return request


def require_request(db,app_state,turn,request_id,meta):
    adapter=getattr(app_state,'team_connection',None)
    if not adapter:fail('shared_session_unavailable')
    shared_text_runtime.verify(app_state,db,turn.room_id,turn.agent_id,turn.root_id,turn.generation,turn)
    row=db.get(SharedConnectorRequest,request_id,populate_existing=True)
    if not row or row.room_id!=turn.room_id or row.agent_id!=turn.agent_id or row.root_event_id!=turn.root_id or row.run_generation!=turn.generation or row.session_generation!=turn.session_generation or row.team_id!=turn.team_id or row.publication_id!=turn.publication_id or row.tool_id!=meta['tool_id'] or row.owner_epoch!=turn.owner_epoch or row.publication_revision!=turn.revision or row.revocation_epoch!=turn.revocation_epoch or row.authority_revision!=turn.authority_revision:
        fail('shared_connector_request_changed')
    link,server=current_link(db,turn.owner_id,adapter,meta['server_id'])
    if row.link_id!=link.link_id:fail('shared_connector_request_changed')
    mcp_registry.current_tool(db,turn.owner_id,turn.agent_id,meta)
    if row.state in ('allowed','consumed') and row.preference_revision!=link.preferences_revision:fail('shared_connector_consent_changed')
    if row.state in ('allowed','consumed') and row.consent_scope in ('bot','all'):
        pref=db.get(SharedConnectorPreference,row.consent_id,populate_existing=True)
        if not pref or pref.link_id!=link.link_id or pref.state!='active' or pref.scope!=row.consent_scope or pref.revision!=row.consent_revision:
            fail('shared_connector_consent_changed')
        if pref.scope=='bot' and (pref.team_id!=turn.team_id or pref.publication_id!=turn.publication_id):fail('shared_connector_consent_changed')
    return row,link,server


def eligible_offers(db,app_state,turn):
 adapter=getattr(app_state,'team_connection',None)
 if not adapter:return []
 shared_text_runtime.verify(app_state,db,turn.room_id,turn.agent_id,turn.root_id,turn.generation,turn)
 offered=mcp_registry.offered(db,turn.owner_id,turn.agent_id);result=[]
 for item in offered:
  try:current_link(db,turn.owner_id,adapter,item['mcp_binding']['server_id'])
  except HTTPException:continue
  result.append(item)
 return result


def review_binding(db,app_state,turn,request_id,meta):
 row,link,server=require_request(db,app_state,turn,request_id,meta)
 if row.state not in ('allowed','consumed') or row.consent_scope not in ('once','bot','all') or not row.consent_id:fail('shared_connector_consent_changed')
 bridge=getattr(app_state,'mcp_client',None)
 if not bridge or not bridge.available:fail('shared_connector_unavailable')
 return {'version':1,'owner_id':turn.owner_id,'owner_epoch':turn.owner_epoch,'core_epoch':bridge.core_epoch,
  'issuer':turn.issuer,'subject':turn.subject,'team_id':turn.team_id,'publication_id':turn.publication_id,
  'session_generation':turn.session_generation,'room_id':turn.room_id,'agent_id':turn.agent_id,'root_event_id':turn.root_id,
  'generation':turn.generation,'server_id':server.server_id,'config_revision':meta['config_revision'],
  'binding_revision':meta['binding_revision'],'link_id':link.link_id,'link_revision':link.revision,'authority_id':row.authority_id,
  'preference_revision':row.preference_revision,'consent_id':row.consent_id,'consent_revision':row.consent_revision,
  'consent_scope':row.consent_scope,'credential_ref_sha256':link.credential_ref_sha256}


def require_review_binding(db,app_state,base,meta):
 row=db.query(SharedConnectorRequest).filter_by(authority_id=base.get('authority_id')).first()
 if not row:fail('shared_connector_consent_changed')
 turn=shared_text_runtime.verify(app_state,db,row.room_id,row.agent_id,row.root_event_id,row.run_generation)
 if turn is None or review_binding(db,app_state,turn,row.request_id,meta)!=base:fail('shared_connector_consent_changed')
 return row


def revoke_session_blocking(app_state,adapter):
    """Called from synchronous team-session mutation while Owner lock is held.

    A successful Core response accepts cancellation, not proof that an already
    dispatched external action had no effect. On failure links remain blocked.
    """
    import asyncio
    bridge=getattr(app_state,'mcp_client',None)
    with app_state.session_factory() as db:
        rows=(db.query(SharedConnectorRequest,SharedConnectorLink).join(SharedConnectorLink,SharedConnectorLink.link_id==SharedConnectorRequest.link_id)
              .filter(SharedConnectorLink.owner_id==adapter.owner_id,SharedConnectorLink.issuer==adapter.issuer,
                      SharedConnectorLink.subject==adapter._subject,SharedConnectorRequest.session_generation==adapter.generation,
                      SharedConnectorRequest.state.in_(('pending','allowed','consumed','uncertain'))).all())
        targets=[(row.request_id,row.authority_id,link.link_id) for row,link in rows]
    if not targets:return True
    async def revoke_all():
        async def one(authority_id):
            return await bridge.shared_revoke({'version':1,'owner_id':adapter.owner_id,'owner_epoch':adapter.owner_epoch,
                'core_epoch':bridge.core_epoch,'authority_id':authority_id,'session_generation':adapter.generation})
        return await asyncio.gather(*(one(authority_id) for _,authority_id,_ in targets),return_exceptions=True)
    results=[]
    try:
        if bridge is None or not bridge.available:raise ValueError()
        future=asyncio.run_coroutine_threadsafe(revoke_all(),bridge._reader_task.get_loop())
        results=future.result(timeout=5)
    except Exception:results=[]
    ok=len(results)==len(targets) and all(isinstance(value,dict) and value.get('state')=='revoked' for value in results)
    with app_state.session_factory() as db:
        try:
            db.execute(text('BEGIN IMMEDIATE'))
            for index,(request_id,_,link_id) in enumerate(targets):
                row=db.get(SharedConnectorRequest,request_id)
                if row and row.state in ('pending','allowed','consumed','uncertain'):
                    success=index<len(results) and isinstance(results[index],dict) and results[index].get('state')=='revoked'
                    row.state='revoked' if success else 'uncertain'
                    if not success:
                        link=db.get(SharedConnectorLink,link_id)
                        if link:link.state='uncertain'
            db.commit()
        except BaseException:
            db.rollback();return False
    return ok


def stage_global_bindings(db,app_state,turn):
    """Materialize an explicitly granted all-shared-Bots preference for this handle."""
    adapter=getattr(app_state,'team_connection',None)
    if not adapter:fail('shared_session_unavailable')
    shared_text_runtime.verify(app_state,db,turn.room_id,turn.agent_id,turn.root_id,turn.generation,turn)
    links=db.query(SharedConnectorLink).filter_by(owner_id=turn.owner_id,issuer=turn.issuer,subject=turn.subject,state='active').all()
    for link in links:
        pref=db.query(SharedConnectorPreference).filter_by(link_id=link.link_id,scope='all',team_id='*',publication_id='*',state='active').first()
        if not pref:continue
        try:_,server=current_link(db,turn.owner_id,adapter,link.server_id)
        except HTTPException:continue
        if not db.query(MCPDiscovery).filter_by(owner_id=turn.owner_id,server_id=server.server_id,server_revision=server.revision,state='completed').first():continue
        binding=db.get(MCPBinding,(turn.owner_id,turn.agent_id,server.server_id))
        if binding is None:
            db.add(MCPBinding(owner_id=turn.owner_id,agent_id=turn.agent_id,server_id=server.server_id,revision=1,enabled=True,tool_ids=None))
    db.flush()


def abandon_pending(app_state,request_id):
    with app_state.owner_recovery_lock:
        with app_state.session_factory() as db:
            try:
                db.execute(text('BEGIN IMMEDIATE'))
                row=db.get(SharedConnectorRequest,request_id)
                if row and row.state=='pending':row.state='revoked'
                db.commit()
            except BaseException:db.rollback();raise


def clear_all_snapshot(db,owner,adapter,publication_id):
    """Return a bounded, deterministic account snapshot and a public preview."""
    links=db.query(SharedConnectorLink).filter_by(owner_id=owner,issuer=adapter.issuer,subject=adapter._subject,
        key_fingerprint=adapter.key_fingerprint).order_by(SharedConnectorLink.link_id).limit(101).all()
    if len(links)>100:fail('shared_connector_clear_limit')
    private=[];public=[];total_requests=0;total_preferences=0;blocked=False
    for link in links:
        server=db.get(MCPServer,link.server_id)
        if not server or server.owner_id!=owner:fail('shared_connector_changed')
        prefs=db.query(SharedConnectorPreference).filter_by(link_id=link.link_id,state='active').order_by(SharedConnectorPreference.consent_id).limit(1001).all()
        total_preferences+=len(prefs)
        if total_preferences>1000:fail('shared_connector_clear_limit')
        requests=db.query(SharedConnectorRequest).filter(SharedConnectorRequest.link_id==link.link_id,
            SharedConnectorRequest.state.in_(('pending','allowed','consumed','uncertain'))).order_by(SharedConnectorRequest.request_id).limit(1001).all()
        total_requests+=len(requests)
        if total_requests>1000:fail('shared_connector_clear_limit')
        if link.state=='uncertain' or any(row.state=='uncertain' for row in requests):blocked=True
        raw={'link_id':link.link_id,'server_id':link.server_id,'link_revision':link.revision,
             'preferences_revision':link.preferences_revision,'link_state':link.state,'link_server_revision':link.server_revision,
             'link_credential_ref_sha256':link.credential_ref_sha256,'server_revision':server.revision,
             'credential_ref_sha256':sha256((server.credential_ref or '').encode('utf-8')).hexdigest(),
             'preferences':[{'consent_id':p.consent_id,'scope':p.scope,'team_id':p.team_id,'publication_id':p.publication_id,'revision':p.revision} for p in prefs],
             'requests':[{'request_id':r.request_id,'authority_id':r.authority_id,'session_generation':r.session_generation,
                          'owner_epoch':r.owner_epoch,'state':r.state} for r in requests]}
        private.append(raw)
        public.append({'link_id':link.link_id,'server_id':link.server_id,'server_name':server.name,
                       'link_revision':link.revision,'preferences_revision':link.preferences_revision,
                       'preference_count':len(prefs),'pending_count':sum(r.state in ('pending','allowed') for r in requests),
                       'inflight_count':sum(r.state in ('consumed','uncertain') for r in requests)})
    material={'version':1,'owner_id':owner,'issuer':adapter.issuer,'account_id':adapter._subject,
              'key_fingerprint':adapter.key_fingerprint,'team_id':adapter.team_id,'publication_id':publication_id,'session_generation':adapter.generation,'links':private}
    barrier=db.get(SharedConnectorClearBarrier,(owner,adapter.issuer,adapter._subject,adapter.key_fingerprint))
    return {'digest':mcp_registry.digest(material),'private':private,'links':public,
            'state':barrier.state if barrier else 'blocked' if blocked else 'ready'}
