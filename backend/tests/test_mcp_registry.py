from test_room_state import room_client,headers
from test_bot_creation import private_team
from app.services import mcp_registry


def test_owner_registry_preview_discovery_binding_and_revoke(room_client,monkeypatch):
    client,(owner,other,project,_,_)=room_client;auth=headers(client,owner);bot,_,_=private_team(client,owner,project)
    body={'name':'Synthetic MCP','enabled':True,'transport':{'type':'http','url':'https://example.invalid/mcp'}}
    key={**auth,'Idempotency-Key':'mcp-create'}
    created=client.post('/api/v1/mcp/servers',headers=key,json=body);assert created.status_code==200,created.text
    server=created.json()['server'];sid=server['server_id'];assert server['revision']==1
    assert client.get('/api/v1/mcp/commands/mcp-create',headers=auth).json()==created.json()
    assert client.get(f'/api/v1/mcp/servers/{sid}',headers=headers(client,other)).status_code==404
    assert client.put(f'/api/v1/bots/{bot}/mcp/{sid}',headers=auth,json={'expected_revision':0,'expected_server_revision':1,'enabled':True}).status_code==503
    preview=client.post(f'/api/v1/mcp/servers/{sid}/discovery-preview',headers=auth,json={'expected_revision':1}).json()
    # The fixture only returns synthetic schemas; no network or subprocess occurs.
    class Bridge:
        available=True;core_epoch='b'*64
        async def close(self):pass
        async def configure(self,payload):return {'state':'configured'}
        async def bind(self,payload):return {'state':'bound'}
        async def revoke(self,payload):return {'state':'revoked'}
        async def issue(self,payload):return {'state':'issued','nonce':'a'*64}
        async def start(self,payload):return {'state':'completed','result':{}}
        async def cancel(self,payload):return {'state':'cancel_requested'}
        async def credential_status(self,payload):return {'state':'missing'}
        async def discover(self,payload):
            schema={'type':'object','properties':{'value':{'type':'string'}},'required':['value'],'additionalProperties':False}
            return {'state':'completed','result':{'tools':[{'name':name,'description':'Synthetic test only','input_schema':schema,'schema_canonical':mcp_registry.canonical(schema),'schema_sha256':mcp_registry.digest(schema)} for name in ('synthetic_one','synthetic_two')]}}
    client.app.state.mcp_client=Bridge()
    from types import SimpleNamespace
    monkeypatch.setattr(client.app.state.owner_epoch_provider,'current',lambda owner_id:'c'*64)
    key={**auth,'Idempotency-Key':'mcp-discover'}
    value={'expected_revision':1,'preview_id':preview['preview_id']}
    discovered=client.post(f'/api/v1/mcp/servers/{sid}/discover',headers=key,json=value)
    assert discovered.status_code==200,discovered.text
    assert discovered.json()['discovery']['state']=='running'
    receipt=client.get('/api/v1/mcp/commands/mcp-discover',headers=auth).json()
    assert receipt['discovery']['state']=='completed'
    discovered_tools=client.get(f'/api/v1/mcp/servers/{sid}/tools',headers=auth).json()['items']
    assert len(discovered_tools)==2
    selected=discovered_tools[0]['tool_id']
    b=client.put(f'/api/v1/bots/{bot}/mcp/{sid}',headers=auth,json={'expected_revision':0,'expected_server_revision':1,'enabled':True,'tool_ids':[selected]});assert b.status_code==200
    with client.app.state.session_factory() as db:assert len(mcp_registry.offered(db,owner,bot))==1
    edited=client.patch(f'/api/v1/mcp/servers/{sid}',headers=auth,json={**body,'expected_revision':1,'name':'Revised'})
    assert edited.status_code==200 and edited.json()['revision']==2
    assert client.get(f'/api/v1/mcp/servers/{sid}/tools',headers=auth).json()['items']==[]
    binding=client.get(f'/api/v1/bots/{bot}/mcp',headers=auth).json()['items'][0]
    assert binding['revision']==2 and not binding['enabled']
    stale=client.put(f'/api/v1/bots/{bot}/mcp/{sid}',headers=auth,json={'expected_revision':2,'expected_server_revision':1,'enabled':True})
    assert stale.status_code==409 and stale.json()['detail']['code']=='mcp_revision_conflict'


def test_reject_unsafe_target_and_missing_executor(room_client):
    client,(owner,_,_,_,_)=room_client;auth=headers(client,owner);key={**auth,'Idempotency-Key':'test'}
    for target in ({'type':'http','url':'https://u:secret@example.invalid/mcp'}, {'type':'stdio','executable':'relative','argv':[]}):
        assert client.post('/api/v1/mcp/servers',headers=key,json={'name':'bad','enabled':True,'transport':target}).status_code==422
    s=client.post('/api/v1/mcp/servers',headers=key,json={'name':'valid','enabled':True,'transport':{'type':'stdio','executable':'/usr/bin/true','argv':[]}}).json()['server']
    p=client.post(f"/api/v1/mcp/servers/{s['server_id']}/discovery-preview",headers=auth,json={'expected_revision':1}).json()
    r=client.post(f"/api/v1/mcp/servers/{s['server_id']}/discover",headers={**auth,'Idempotency-Key':'missing'},json={'expected_revision':1,'preview_id':p['preview_id']})
    assert r.status_code==503
    assert client.get('/api/v1/mcp/commands/missing',headers=auth).status_code==404


def test_synthetic_mcp_model_review_once_and_execution(room_client,monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from test_execution_scopes import FakeExecutor
    from app.services.tool_broker import TOOL_SCHEMAS
    from app.services.providers import ProviderResult,ToolCall
    from app.models.collaboration import ToolAction
    client,(owner,_,project,_,_)=room_client;auth=headers(client,owner);bot,room,_=private_team(client,owner,project)
    calls=[]
    class Bridge:
        available=True;core_epoch='b'*64
        async def close(self):pass
        async def configure(self,p):return {'state':'configured'}
        async def bind(self,p):return {'state':'bound'}
        async def revoke(self,p):return {'state':'revoked'}
        async def credential_status(self,p):return {'state':'missing'}
        async def cancel(self,p):calls.append(('cancel',p));return {'state':'cancel_requested'}
        async def discover(self,p):
            schema={'type':'object','properties':{'value':{'type':'string'},'page_token':{'type':'string'}},'required':['value'],'additionalProperties':False}
            return {'state':'completed','result':{'tools':[{'name':'synthetic_tool','description':'Return a synthetic value','input_schema':schema,'schema_canonical':mcp_registry.canonical(schema),'schema_sha256':mcp_registry.digest(schema)}]}}
        async def issue(self,p):calls.append(('issue',p));return {'state':'issued','nonce':'a'*64}
        async def start(self,p):
            calls.append(('start',p))
            if getattr(self,'block',False):
                import asyncio
                await asyncio.Event().wait()
            return {'state':'completed','result':{'content':'SYNTHETIC_RESULT'}}
    bridge=Bridge();client.app.state.mcp_client=bridge
    monkeypatch.setattr(client.app.state.owner_epoch_provider,'current',lambda owner_id:'c'*64)
    from app.services.tool_broker import ToolBroker
    broker=ToolBroker(client.app.state.session_factory,FakeExecutor(),owner_epochs=SimpleNamespace(current=lambda owner_id:'c'*64))
    client.app.state.scheduler.broker=broker
    body={'name':'Synthetic','enabled':True,'transport':{'type':'http','url':'https://example.invalid/mcp'}}
    server=client.post('/api/v1/mcp/servers',headers={**auth,'Idempotency-Key':'synthetic-server'},json=body).json()['server'];sid=server['server_id']
    p=client.post(f'/api/v1/mcp/servers/{sid}/discovery-preview',headers=auth,json={'expected_revision':1}).json()
    receipt=client.post(f'/api/v1/mcp/servers/{sid}/discover',headers={**auth,'Idempotency-Key':'synthetic-discover'},json={'expected_revision':1,'preview_id':p['preview_id']})
    assert receipt.status_code==200,receipt.text
    for _ in range(100):
        if client.get('/api/v1/mcp/commands/synthetic-discover',headers=auth).json()['discovery']['state']=='completed':break
        time.sleep(.01)
    assert client.put(f'/api/v1/bots/{bot}/mcp/{sid}',headers=auth,json={'expected_revision':0,'expected_server_revision':1,'enabled':True}).status_code==200
    async def provider(request):
        if request.observations:return ProviderResult(text='Synthetic completed')
        matches=[t for t in request.tools if t['name'].startswith('mcp_')]
        assert len(matches)==1
        return ProviderResult(tool_calls=[ToolCall('call-mcp',matches[0]['name'],{'value':'hello','page_token':'cursor-value'})],continuation={'call_ids':['call-mcp']})
    client.app.state.scheduler.provider=provider
    with ThreadPoolExecutor(max_workers=1) as pool:
        work=pool.submit(client.post,f'/api/v1/conversations/{room}/messages',headers=auth,json={'content':'@Ada use the synthetic tool'})
        action_id=None
        for _ in range(200):
            with client.app.state.session_factory() as db:
                action=db.query(ToolAction).filter_by(room_id=room,family='mcp_tool_call',state='waiting').first()
                if action:action_id=action.action_id;break
            time.sleep(.01)
        assert action_id,work.result(timeout=1).text if work.done() else 'no reviewed action'
        p=client.get(f'/api/v1/tools/rooms/{room}/actions/{action_id}/preview',headers=auth);assert p.status_code==200,p.text
        detail=p.json();assert detail['approval_policy']['scopes']==['once'] and detail['mcp_binding']['server_id']==sid
        assert detail['arguments']=={'value':'hello','page_token':'[redacted]'} and calls==[]
        approval=client.post(f'/api/v1/tools/rooms/{room}/actions/{action_id}/decision',headers=auth,
            json={'decision':'approve','scope':'once','review_id':detail['review_id'],'acknowledge_unverified_context':True})
        assert approval.status_code==200,approval.text
        response=work.result(timeout=4);assert response.status_code==201,response.text
    assert [name for name,_ in calls]==['issue','start']
    assert calls[1][1]['nonce']=='a'*64 and calls[1][1]['binding']['tool_name']=='synthetic_tool'
    assert calls[1][1]['arguments']['page_token']=='cursor-value'
    # Revoke a later pending proposal: neither approval nor a Core issue may use it.
    with ThreadPoolExecutor(max_workers=1) as pool:
        work=pool.submit(client.post,f'/api/v1/conversations/{room}/messages',headers=auth,json={'content':'@Ada use the synthetic tool again'})
        second=None
        for _ in range(200):
            with client.app.state.session_factory() as db:
                action=db.query(ToolAction).filter_by(room_id=room,family='mcp_tool_call',state='waiting').first()
                if action:second=action.action_id;break
            time.sleep(.01)
        assert second
        p=client.get(f'/api/v1/tools/rooms/{room}/actions/{second}/preview',headers=auth);assert p.status_code==200
        rev=client.put(f'/api/v1/bots/{bot}/mcp/{sid}',headers=auth,json={'expected_revision':1,'expected_server_revision':1,'enabled':False})
        assert rev.status_code==200
        denied=client.post(f'/api/v1/tools/rooms/{room}/actions/{second}/decision',headers=auth,
            json={'decision':'approve','scope':'once','review_id':p.json()['review_id'],'acknowledge_unverified_context':True})
        assert denied.status_code==409 and [name for name,_ in calls]==['issue','start']
        client.post(f'/api/v1/conversations/{room}/cancel',headers=auth)
        work.result(timeout=4)

    # Stop after the Core accepted start: cancellation is sent and outcome stays uncertain.
    assert client.put(f'/api/v1/bots/{bot}/mcp/{sid}',headers=auth,json={'expected_revision':2,'expected_server_revision':1,'enabled':True}).status_code==200
    bridge.block=True
    with ThreadPoolExecutor(max_workers=1) as pool:
        work=pool.submit(client.post,f'/api/v1/conversations/{room}/messages',headers=auth,json={'content':'@Ada use the synthetic tool a third time'})
        third=None
        for _ in range(200):
            with client.app.state.session_factory() as db:
                action=db.query(ToolAction).filter_by(room_id=room,family='mcp_tool_call',state='waiting').first()
                if action:third=action.action_id;break
            time.sleep(.01)
        assert third, work.result(timeout=1).text if work.done() else 'third action missing'
        detail=client.get(f'/api/v1/tools/rooms/{room}/actions/{third}/preview',headers=auth).json()
        approval=client.post(f'/api/v1/tools/rooms/{room}/actions/{third}/decision',headers=auth,
            json={'decision':'approve','scope':'once','review_id':detail['review_id'],'acknowledge_unverified_context':True})
        assert approval.status_code==200
        for _ in range(200):
            if len([name for name,_ in calls if name=='start'])==2:break
            time.sleep(.01)
        assert len([name for name,_ in calls if name=='start'])==2
        assert client.post(f'/api/v1/conversations/{room}/stop',headers=auth).status_code==204
        work.result(timeout=4)
        for _ in range(100):
            with client.app.state.session_factory() as db:state=db.get(ToolAction,third).state
            if state=='uncertain':break
            time.sleep(.01)
        assert state=='uncertain' and any(name=='cancel' for name,_ in calls)


def test_stdio_preview_pins_executable_before_discovery(room_client,tmp_path,monkeypatch):
    import os
    client,(owner,_,_,_,_)=room_client;auth=headers(client,owner)
    executable=tmp_path/'synthetic-executable';executable.write_text('first synthetic');executable.chmod(0o700)
    class Bridge:
        available=True;core_epoch='b'*64
        async def close(self):pass
        async def configure(self,p):return {'state':'configured'}
        async def bind(self,p):return {'state':'bound'}
        async def revoke(self,p):return {'state':'revoked'}
        async def issue(self,p):return {'state':'issued','nonce':'a'*64}
        async def start(self,p):return {'state':'uncertain'}
        async def cancel(self,p):return {'state':'cancel_requested'}
        async def credential_status(self,p):return {'state':'missing'}
        async def discover(self,p):raise AssertionError('Changed executable must not start')
    client.app.state.mcp_client=Bridge();monkeypatch.setattr(client.app.state.owner_epoch_provider,'current',lambda owner_id:'c'*64)
    body={'name':'Local','enabled':True,'transport':{'type':'stdio','executable':str(executable),'argv':[]}}
    server=client.post('/api/v1/mcp/servers',headers={**auth,'Idempotency-Key':'local-server'},json=body).json()['server'];sid=server['server_id']
    preview=client.post(f'/api/v1/mcp/servers/{sid}/discovery-preview',headers=auth,json={'expected_revision':1}).json()
    assert preview['executable_review']['status']=='verified'
    replacement=tmp_path/'replacement';replacement.write_text('second synthetic');replacement.chmod(0o700);os.replace(replacement,executable)
    response=client.post(f'/api/v1/mcp/servers/{sid}/discover',headers={**auth,'Idempotency-Key':'local-discovery'},json={'expected_revision':1,'preview_id':preview['preview_id']})
    assert response.status_code==409 and response.json()['detail']['code']=='mcp_discovery_preview_changed'


def test_http_secret_ref_attach_requires_core_status(room_client,monkeypatch):
    client,(owner,_,_,_,_)=room_client;auth=headers(client,owner);observed=[]
    class Bridge:
        available=True;core_epoch='b'*64
        async def close(self):pass
        async def configure(self,p):observed.append(('configure',p));return {'state':'configured'}
        async def bind(self,p):return {'state':'bound'}
        async def revoke(self,p):observed.append(('revoke',p));return {'state':'revoked'}
        async def credential_status(self,p):observed.append(('status',p));return {'state':'present'}
        async def issue(self,p):return {'state':'issued','nonce':'a'*64}
        async def start(self,p):return {'state':'uncertain'}
        async def cancel(self,p):return {'state':'cancel_requested'}
        async def discover(self,p):raise AssertionError('No discovery should start')
    client.app.state.mcp_client=Bridge();monkeypatch.setattr(client.app.state.owner_epoch_provider,'current',lambda owner_id:'c'*64)
    body={'name':'HTTP','enabled':True,'transport':{'type':'http','url':'https://example.invalid/mcp'}}
    server=client.post('/api/v1/mcp/servers',headers={**auth,'Idempotency-Key':'secret-server'},json=body).json()['server'];sid=server['server_id']
    preview=client.post(f'/api/v1/mcp/servers/{sid}/credential-preview',headers=auth,json={'expected_revision':1});assert preview.status_code==200
    ref=f'mcp:{owner}:{sid}:'+('a'*32)
    response=client.put(f'/api/v1/mcp/servers/{sid}/credential',headers=auth,json={'expected_revision':1,'credential_ref':ref})
    assert response.status_code==200,response.text
    result=response.json();assert result['revision']==2 and result['credential_ref']==ref and result['credential_state']=='configured'
    assert [x[0] for x in observed].count('status')==1 and 'revoke' in [x[0] for x in observed]
    assert client.get(f'/api/v1/mcp/servers/{sid}',headers=auth).json()['credential_ref']==ref


async def _mcp_adapter_shape(kind,monkeypatch):
    import json
    from app.services.connections import ConnectionConfig
    from app.services.providers.base import ProviderRequest,wire_tool_name
    from app.services.providers.chatgpt import ChatGPTAdapter
    from app.services.providers.opencode_go import OpenCodeGoMessagesAdapter
    from app.services import connections
    alias='mcp_'+('a'*20);schema={'type':'object','properties':{'value':{'type':'string'}},'required':['value'],'additionalProperties':False}
    tool={'name':alias,'description':'Untrusted synthetic MCP description','parameters':schema,'mcp_binding':{'tool_id':'a'*64}}
    if kind=='oauth':
        model='synthetic';base='https://api.openai.com/v1';provider='chatgpt';protocol='openai_responses'
    else:model='qwen3.7-plus';base='https://opencode.ai/zen/go/v1';provider='opencode-go';protocol='anthropic_messages'
    caps={'declared':{'tools':True},'verified':{'model_id':model,'base_url':base,'tools':True},
        'model_features':{model:{'model_id':model,'base_url':base,'config_revision':1,'reported':{'text_generation':True,'tool_calling':True},'tested':{'text_generation':True,'tool_calling':True}}}}
    cfg=ConnectionConfig(1,provider,model,base,'synthetic-only',caps,provider_id=provider,protocol_id=protocol,owner_id=1,config_revision=1)
    req=ProviderRequest(connection=cfg,model_id=model,room_id=1,agent_id=1,tools=[tool]);wire=wire_tool_name(alias)
    if kind=='oauth':
        async def discover(_):return ([{'id':model}],None,None)
        monkeypatch.setattr(connections,'discover_models',discover);adapter=ChatGPTAdapter()
        async def post(_request,_path,_headers,body):
            assert body['tools'][0]['tools'][0]['name']==wire
            return {'output':[{'type':'function_call','namespace':'gamja','name':wire,'call_id':'call','arguments':json.dumps({'value':'hello'})}]}
    else:
        adapter=OpenCodeGoMessagesAdapter()
        async def post(_request,_path,_headers,body):
            assert body['tools'][0]['name']==wire
            return {'content':[{'type':'tool_use','id':'call','name':wire,'input':{'value':'hello'}}]}
    monkeypatch.setattr(adapter,'_post',post)
    result=await adapter.generate(req)
    assert result.tool_calls[0].family==alias and result.tool_calls[0].arguments=={'value':'hello'}

import pytest
@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['oauth','go'])
async def test_mcp_provider_alias_exact_offered_schema(kind,monkeypatch):
    await _mcp_adapter_shape(kind,monkeypatch)


def test_credential_preview_reports_only_safe_core_failure_category(room_client,monkeypatch):
    client,(owner,_,_,_,_)=room_client;auth=headers(client,owner)
    class Bridge:
        available=True;core_epoch='b'*64;failure_reason=None
        async def close(self):pass
        async def configure(self,p):return {'state':'failed','code':'mcp_configuration_epoch_changed'}
        async def bind(self,p):raise AssertionError('must stop')
        async def revoke(self,p):raise AssertionError('no automatic retry')
        async def issue(self,p):pass
        async def start(self,p):pass
        async def cancel(self,p):pass
        async def discover(self,p):pass
        async def credential_status(self,p):pass
    client.app.state.mcp_client=Bridge();monkeypatch.setattr(client.app.state.owner_epoch_provider,'current',lambda owner_id:'c'*64)
    body={'name':'HTTP','enabled':True,'transport':{'type':'http','url':'https://example.invalid/mcp'}}
    server=client.post('/api/v1/mcp/servers',headers={**auth,'Idempotency-Key':'mcp-epoch-diagnostic'},json=body).json()['server']
    response=client.post(f"/api/v1/mcp/servers/{server['server_id']}/credential-preview",headers=auth,json={'expected_revision':1})
    assert response.status_code==503
    assert response.json()['detail']=={'code':'mcp_transport_unavailable','stage':'configure','reason':'mcp_configuration_epoch_changed'}
    async def unsafe_reply(_):return {'state':'failed','code':'synthetic-secret'}
    client.app.state.mcp_client.configure=unsafe_reply
    response=client.post(f"/api/v1/mcp/servers/{server['server_id']}/credential-preview",headers=auth,json={'expected_revision':1})
    assert response.json()['detail']=={'code':'mcp_transport_unavailable','stage':'configure','reason':'channel_error'}
    client.app.state.mcp_client.available=False;client.app.state.mcp_client.failure_reason='channel_eof'
    response=client.post(f"/api/v1/mcp/servers/{server['server_id']}/credential-preview",headers=auth,json={'expected_revision':1})
    assert response.json()['detail']=={'code':'mcp_transport_unavailable','stage':'channel','reason':'channel_eof'}


def test_discovery_uncertain_preserves_only_safe_reason_without_replay(room_client,monkeypatch):
    client,(owner,_,_,_,_)=room_client;auth=headers(client,owner);calls=[]
    class Bridge:
        available=True;core_epoch='b'*64
        async def close(self):pass
        async def configure(self,p):return {'state':'configured'}
        async def bind(self,p):return {'state':'bound'}
        async def revoke(self,p):raise AssertionError('must not retry')
        async def issue(self,p):pass
        async def start(self,p):pass
        async def cancel(self,p):pass
        async def credential_status(self,p):pass
        async def discover(self,p):calls.append(1);return {'state':'uncertain','code':'mcp_disc_schema_uncertain'}
    client.app.state.mcp_client=Bridge();monkeypatch.setattr(client.app.state.owner_epoch_provider,'current',lambda owner_id:'c'*64)
    server=client.post('/api/v1/mcp/servers',headers={**auth,'Idempotency-Key':'disc-safety'},json={'name':'Synthetic','enabled':True,'transport':{'type':'http','url':'https://example.invalid/mcp'}}).json()['server'];sid=server['server_id']
    preview=client.post(f'/api/v1/mcp/servers/{sid}/discovery-preview',headers=auth,json={'expected_revision':1}).json()
    payload={'expected_revision':1,'preview_id':preview['preview_id']};key={**auth,'Idempotency-Key':'disc-safety-run'}
    assert client.post(f'/api/v1/mcp/servers/{sid}/discover',headers=key,json=payload).status_code==200
    receipt=client.get('/api/v1/mcp/commands/disc-safety-run',headers=auth).json()['discovery']
    assert receipt['state']=='unknown' and receipt['failure_code']=='mcp_disc_schema_uncertain' and len(calls)==1


def test_large_schema_discovery_preserves_original_and_context_budget(room_client,monkeypatch):
    client,(owner,_,project,_,_)=room_client;auth=headers(client,owner);bot,_,_=private_team(client,owner,project)
    schema={'type':'object','properties':{'value':{'type':'string','description':'x'*62_000}}}
    serialized=mcp_registry.canonical(schema);digest=mcp_registry.digest(schema)
    assert 16_000<len(serialized.encode('utf-8'))<=mcp_registry.MAX_MCP_SCHEMA_BYTES
    class Bridge:
        available=True;core_epoch='b'*64
        async def close(self):pass
        async def configure(self,p):return {'state':'configured'}
        async def bind(self,p):return {'state':'bound'}
        async def revoke(self,p):return {'state':'revoked'}
        async def issue(self,p):pass
        async def start(self,p):pass
        async def cancel(self,p):pass
        async def credential_status(self,p):pass
        async def discover(self,p):return {'state':'completed','result':{'tools':[{'name':f'synthetic_{i}','description':'synthetic','input_schema':schema,'schema_canonical':serialized,'schema_sha256':digest} for i in range(7)]}}
    client.app.state.mcp_client=Bridge();monkeypatch.setattr(client.app.state.owner_epoch_provider,'current',lambda owner_id:'c'*64)
    server=client.post('/api/v1/mcp/servers',headers={**auth,'Idempotency-Key':'large-schema-server'},json={'name':'Synthetic','enabled':True,'transport':{'type':'http','url':'https://example.invalid/mcp'}}).json()['server'];sid=server['server_id']
    preview=client.post(f'/api/v1/mcp/servers/{sid}/discovery-preview',headers=auth,json={'expected_revision':1}).json()
    assert client.post(f'/api/v1/mcp/servers/{sid}/discover',headers={**auth,'Idempotency-Key':'large-schema-run'},json={'expected_revision':1,'preview_id':preview['preview_id']}).status_code==200
    receipt=client.get('/api/v1/mcp/commands/large-schema-run',headers=auth).json()['discovery'];assert receipt['state']=='completed'
    tools=client.get(f'/api/v1/mcp/servers/{sid}/tools',headers=auth).json()['items'];assert len(tools)==7 and all(t['input_schema']==schema for t in tools)
    all_tools=client.put(f'/api/v1/bots/{bot}/mcp/{sid}',headers=auth,json={'expected_revision':0,'expected_server_revision':1,'enabled':True})
    assert all_tools.status_code==409 and all_tools.json()['detail']['code']=='mcp_context_limit'
    selected=client.put(f'/api/v1/bots/{bot}/mcp/{sid}',headers=auth,json={'expected_revision':0,'expected_server_revision':1,'enabled':True,'tool_ids':[tools[0]['tool_id']]})
    assert selected.status_code==200
