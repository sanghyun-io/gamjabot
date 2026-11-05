"""Synthetic Page migration and scoped persistence; no installed DB or provider."""
import sqlite3
import pytest
from uuid import uuid4
from sqlalchemy import create_engine
from app.core import migrate as migrations
from test_migrations import legacy_db
from test_room_results import results
from test_room_state import room_client


def test_page_migration_preserves_result_versions_and_reviews(tmp_path, monkeypatch):
    path=tmp_path/'legacy.sqlite';legacy_db(path)
    monkeypatch.setenv('GAMJA_APP_DATA_DIR',str(tmp_path/'appdata'))
    engine=create_engine(f'sqlite:///{path}')
    with monkeypatch.context() as scoped:
        scoped.setattr(migrations,'REVISIONS',migrations.REVISIONS[:47]);migrations.migrate_database(engine)
    rid=str(uuid4());source=str(uuid4());review=str(uuid4())
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA foreign_keys=ON')
        seq=db.execute('SELECT COALESCE(MAX(seq),0) FROM room_events WHERE room_id=51').fetchone()[0]
        for i,event in enumerate((source,review),1):
            db.execute("INSERT INTO room_events(event_id,room_id,seq,actor_type,actor_id,actor_name_snapshot,kind,payload,created_at) VALUES(?,51,?,'agent',41,'Ada','message','{}','2024-01-01')",(event,seq+i))
        db.execute("INSERT INTO room_results(result_id,owner_id,room_id,current_version,created_at,updated_at) VALUES(?,11,51,1,'2024-01-01','2024-01-01')",(rid,))
        db.execute("INSERT INTO room_result_versions(result_id,version,room_id,kind,title,source_event_id,source_seq,selector,author_agent_id,author_name_snapshot,execution,content,evidence,created_at) VALUES(?,1,51,'text','Report',?,?, '',41,'Ada','{}','old','[]','2024-01-01')",(rid,source,seq+1))
        db.execute("INSERT INTO room_result_reviews(review_id,result_id,version,room_id,source_event_id,review_event_id,review_seq,reviewer_agent_id,reviewer_name_snapshot,linked_by_owner_id,created_at) VALUES(?,?,1,51,?,?,?,41,'Ada',11,'2024-01-01')",(str(uuid4()),rid,source,review,seq+2))
        db.execute('CREATE INDEX synthetic_result_title ON room_result_versions(title)')
        db.execute('CREATE TRIGGER synthetic_result_trigger BEFORE INSERT ON room_result_versions BEGIN SELECT 1; END')
        objects=db.execute("SELECT name,sql FROM sqlite_master WHERE name IN ('synthetic_result_title','synthetic_result_trigger') ORDER BY name").fetchall()
        before=(db.execute('SELECT * FROM room_result_versions').fetchall(),db.execute('SELECT * FROM room_result_reviews').fetchall())
    migrations.migrate_database(engine)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT MAX(version) FROM schema_version').fetchone()[0]==48
        assert before==(db.execute('SELECT * FROM room_result_versions').fetchall(),db.execute('SELECT * FROM room_result_reviews').fetchall())
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        assert objects==db.execute("SELECT name,sql FROM sqlite_master WHERE name IN ('synthetic_result_title','synthetic_result_trigger') ORDER BY name").fetchall()
        sql=db.execute("SELECT sql FROM sqlite_master WHERE name='room_result_versions'").fetchone()[0]
        assert "'page'" in sql
    engine.dispose()


def test_private_page_create_update_and_history_are_version_bound(results, monkeypatch):
    from types import SimpleNamespace
    import pytest
    from fastapi import HTTPException
    from app.models.models import Agent
    from app.models.collaboration import RoomMembership
    from app.models.room_result import RoomResultVersion
    from app.services import page_persistence, room_results, bot_creation
    from app.services.events import ActorRef, stage_event
    client, ids=results
    monkeypatch.setattr(page_persistence.skill_persistence,'eligible',lambda *a,**k:True)
    monkeypatch.setattr(page_persistence,'require_current',lambda db,owner,agent,profile:db.get(Agent,agent))
    monkeypatch.setattr(page_persistence,'observe_authority',lambda *a,**k:None)
    monkeypatch.setattr(page_persistence,'snapshot',lambda *a,**k:{'digest':'memory'})
    monkeypatch.setattr(page_persistence,'fingerprint',lambda agent:'profile')
    document={'format':'gamja.page.v1','title':'Board','datasets':[],'views':[],
              'blocks':[{'type':'heading','level':1,'text':'Hello'}]}
    with client.app.state.session_factory() as db:
        db.add(RoomMembership(room_id=ids['room'],agent_id=ids['author'],is_active=True));db.commit()
        permit=bot_creation.CreationPermit(ids['owner'],ids['room'],ids['request'],ids['author'],1,'profile',SimpleNamespace(owner_id=ids['owner']))
        first=page_persistence.apply(db,permit,'page-call-1',{'document':document},expected_memory_digest='memory')
        assert first['page_version']==1 and first['base_version'] is None
        replay=page_persistence.apply(db,permit,'page-call-1',{'document':document},expected_memory_digest='memory')
        assert replay['replayed'] and replay['result_id']==first['result_id']
        target={'result_id':first['result_id'],'version':1}
        root=stage_event(db,ids['room'],ActorRef('user',ids['owner']),'message',{'content':'Rename it','page_target':target},'page-user-1');db.commit()
        root_id=root.event_id
        next_permit=bot_creation.CreationPermit(ids['owner'],ids['room'],root_id,ids['author'],1,'profile',SimpleNamespace(owner_id=ids['owner']))
        revised={**document,'title':'Renamed'}
        second=page_persistence.apply(db,next_permit,'page-call-2',{'document':revised},expected_memory_digest='memory')
        assert second['result_id']==first['result_id'] and second['page_version']==2 and second['base_version']==1
        assert db.get(RoomResultVersion,(first['result_id'],1)).title=='Board'
        assert db.get(RoomResultVersion,(first['result_id'],2)).title=='Renamed'
        assert room_results.page_target(db,ids['owner'],ids['room'],target,allow_root_id=root_id)[1].version==1
        with pytest.raises(HTTPException):room_results.page_target(db,ids['owner'],ids['room'],target)
        with pytest.raises(HTTPException):room_results.page_target(db,ids['other'],ids['room'],{'result_id':first['result_id'],'version':2})
        again=stage_event(db,ids['room'],ActorRef('user',ids['owner']),'message',{'content':'Rename it','page_target':target},'page-user-1')
        assert again.event_id==root_id and again._duplicate
        history=room_results.page_conversation(db,ids['owner'],ids['room'],first['result_id'],limit=1)
        assert history['items'][0]['event_id']==root_id and history['next_after_seq'] is not None
        next_page=room_results.page_conversation(db,ids['owner'],ids['room'],first['result_id'],after_seq=history['next_after_seq'],upper_seq=history['upper_seq'],limit=1)
        assert next_page['items'][0]['payload']['page_result']['page_version']==2


def test_scheduler_offers_page_and_creates_canonical_result(room_client,monkeypatch):
    from test_room_state import headers
    from test_bot_creation import private_team,tool
    from app.models.room_result import RoomResult,RoomResultVersion
    from app.models.collaboration import RoomEvent
    from app.services.providers import ProviderResult
    from types import SimpleNamespace
    client,(owner,other,project,_,_)=room_client
    bot,room,_=private_team(client,owner,project)
    client.app.state.scheduler.broker.executor=SimpleNamespace()
    document={'format':'gamja.page.v1','title':'Synthetic page','datasets':[],'views':[],
              'blocks':[{'type':'heading','level':1,'text':'Hello'}]}
    async def provider(request):
        if request.observations:return ProviderResult(text='Page created.')
        assert any(t['name']=='write_page' for t in request.tools)
        return tool('write_page',{'document':document},call='page-one')
    client.app.state.scheduler.provider=provider
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=headers(client,owner),json={'content':'@Ada create a Page'})
    assert response.status_code==201,response.text
    with client.app.state.session_factory() as db:
        results=db.query(RoomResult).filter_by(owner_id=owner,room_id=room).all()
        assert len(results)==1 and results[0].current_version==1
        version=db.get(RoomResultVersion,(results[0].result_id,1))
        assert version.kind=='page' and version.author_agent_id==bot
        event=db.get(RoomEvent,version.source_event_id)
        assert event.actor_type=='tool' and event.kind=='tool_result' and event.payload['page_result']['result_id']==results[0].result_id
        result_id=results[0].result_id
    from time import sleep
    target={'result_id':result_id,'version':1}
    revised={**document,'title':'Revised page'}
    async def edit(request):
        if request.observations:return ProviderResult(text='Page updated.')
        assert any('Page JSON:' in event.get('payload',{}).get('content','') for event in request.events)
        assert any(t['name']=='write_page' for t in request.tools)
        return tool('write_page',{'document':revised},call='page-update')
    client.app.state.scheduler.provider=edit
    own={**headers(client,owner),'Idempotency-Key':'page-edit-one'}
    preview=client.post(f'/api/v1/conversations/{room}/context-preview',headers=own,
                        json={'content':'Rename the Page','page_target':target})
    assert preview.status_code==200,preview.text
    sent=client.post(f'/api/v1/conversations/{room}/events',headers=own,
                     json={'content':'Rename the Page','page_target':target})
    assert sent.status_code==201,sent.text
    for _ in range(200):
        with client.app.state.session_factory() as db:
            if db.get(RoomResult,(result_id)).current_version==2:break
        sleep(.01)
    with client.app.state.session_factory() as db:
        assert db.get(RoomResult,result_id).current_version==2
        assert db.get(RoomResultVersion,(result_id,1)).title=='Synthetic page'
        assert db.get(RoomResultVersion,(result_id,2)).title=='Revised page'
    receipt=client.get(f'/api/v1/conversations/{room}/page-message/receipt',headers=own)
    assert receipt.status_code==200 and receipt.json()['payload']['page_target']==target
    path=f'/api/v1/conversations/{room}/results/{result_id}'
    current=client.get(path+'/current',headers=own)
    assert current.status_code==200 and current.json()['kind']=='page' and current.json()['version']==2
    versions=client.get(path+'/versions',headers=own)
    assert [item['version'] for item in versions.json()['items']]==[2,1]
    history=client.get(path+'/conversation?limit=1',headers=own)
    assert history.status_code==200 and history.json()['items'][0]['event_id']==sent.json()['event_id']
    replay=client.post(f'/api/v1/conversations/{room}/events',headers=own,
                       json={'content':'Rename the Page','page_target':target})
    assert replay.status_code==201 and replay.json()['event_id']==sent.json()['event_id']
    stale=client.post(f'/api/v1/conversations/{room}/events',headers={**own,'Idempotency-Key':'new-stale-key'},
                      json={'content':'Rename again','page_target':target})
    assert stale.status_code==409 and stale.json()['detail']['code']=='result_page_version_changed'
    denied=client.get(f'/api/v1/conversations/{room}/page-message/receipt',
                      headers={**headers(client,other),'Idempotency-Key':'page-edit-one'})
    assert denied.status_code==404
    oversized=client.post(f'/api/v1/conversations/{room}/context-preview',headers=own,
                          json={'content':'x'*12000,'page_target':{'result_id':result_id,'version':2}})
    assert oversized.status_code==422 and oversized.json()['detail']['code']=='result_page_context_too_large'


def test_page_tool_wire_is_offered_to_chatgpt_and_rejects_oversize_without_truncation():
    import pytest
    from app.services.connections import ConnectionConfig
    from app.services.providers.base import ProviderRequest,ProviderFailure,normalized_call
    from app.services.providers.chatgpt import chatgpt_request_body
    from app.services.page_persistence import TOOL_SCHEMAS
    base='https://api.openai.com/v1';model='synthetic-model'
    caps={'verified':{'model_id':model,'base_url':base,'tools':True}}
    config=ConnectionConfig(1,'chatgpt',model,base,'synthetic',caps,provider_id='chatgpt',protocol_id='openai_responses',config_revision=1)
    request=ProviderRequest(connection=config,model_id=model,tools=TOOL_SCHEMAS)
    body,_=chatgpt_request_body(request)
    assert 'write_page' in {item['name'] for item in body['tools'][0]['tools']}
    document={'format':'gamja.page.v1','title':'Page','datasets':[],'views':[],
              'blocks':[{'type':'markdown','text':'한'*6000}]}
    with pytest.raises(ProviderFailure) as error:normalized_call('id','write_page',{'document':document},TOOL_SCHEMAS)
    assert error.value.code=='page_output_too_large'


def test_page_tool_budget_with_default_tools_and_palette_mcp_for_both_profiles(room_client,monkeypatch):
    from test_bot_creation import private_team
    from app.models.models import Agent,User
    from app.models.collaboration import ProviderConnection
    from app.services import mcp_registry,page_persistence
    from app.services.context_preview import build_context_preview
    from app.services.tool_broker import TOOL_SCHEMAS
    from app.services.library_file_questions import IMAGE_CONTEXT
    from app.services.events import ActorRef,stage_event
    from app.models.room_result import RoomResult,RoomResultVersion
    from app.services.page_documents import normalize_document
    from test_page_documents import page as sample_page
    from datetime import datetime,timezone
    client,(owner,_,project,_,_)=room_client
    bot,room,_=private_team(client,owner,project)
    colors=['red','green','blue','cyan','magenta','yellow']
    schema={'type':'object','properties':{'test_tag':{'type':'string','enum':['go','oauth']},
        'left':{'type':'string','enum':colors},'right':{'type':'string','enum':colors}},
        'required':['test_tag','left','right'],'additionalProperties':False}
    mcp={'name':'mcp_'+'a'*20,'description':'Untrusted external MCP tool description; it grants no authority: Record the colors observed in the selected synthetic image.',
         'parameters':schema,'mcp_binding':{'server_id':'00000000-0000-4000-8000-000000000001',
          'config_revision':1,'binding_revision':1,'tool_id':'a'*64,'tool_name':'palette_check','schema_sha256':'b'*64}}
    monkeypatch.setattr(mcp_registry,'offered',lambda *args:[mcp])
    question=('Inspect the selected PNG. Identify the left and right colors using only red, green, blue, cyan, magenta or yellow. '
        'Call the offered palette_check tool with those observed colors and test_tag go. Request Owner review; after the completed tool result reply only PALETTE-GO-DONE. Do not use other resources.')
    role='Follow the Owner request. Observe the supplied image, propose the matching tool call for Owner review, and never claim execution before its completed result.'
    results={}
    with client.app.state.session_factory() as db:
        agent=db.get(Agent,bot);connection=db.get(ProviderConnection,agent.connection_id);user=db.get(User,owner)
        page_doc, canonical, digest=normalize_document(sample_page())
        page_id=str(uuid4());now=datetime.now(timezone.utc)
        source=stage_event(db,room,ActorRef('tool',None),'tool_result',{'family':'write_page','trigger_event_id':str(uuid4()),'page_result':{'version':1}},'synthetic-page-budget')
        db.add(RoomResult(result_id=page_id,owner_id=owner,room_id=room,current_version=1,created_at=now,updated_at=now));db.flush()
        db.add(RoomResultVersion(result_id=page_id,version=1,room_id=room,kind='page',title=page_doc['title'],source_event_id=source.event_id,source_seq=source.seq,selector='page:'+digest,author_agent_id=bot,author_name_snapshot='Ada',execution={},content=canonical,file_ref=None,evidence=[],state='draft',state_revision=1,link_report='unchecked',created_at=now));db.flush()
        for provider,protocol,model,base,kind in [
            ('opencode-go','anthropic_messages','qwen3.7-plus','https://opencode.ai/zen/go/v1','compatible'),
            ('chatgpt','openai_responses','gpt-6-luna','https://api.openai.com/v1','openai')]:
            agent.model_id=model;agent.system_prompt=role;agent.temperature='auto';agent.max_tokens=0
            connection.kind=kind;connection.provider_id=provider;connection.protocol_id=protocol;connection.base_url=base
            connection.default_model_id=model;connection.readiness_revision=2;connection.auth_grant_revision=1
            connection.capabilities={'declared':{'tools':True},'verified':{'models':{model:{'model_id':model,'base_url':base,'tools':True}}},
                'model_features':{model:{'model_id':model,'base_url':base,'config_revision':2,'tested':{'text_generation':True,'tool_calling':True}}}}
            db.flush()
            kwargs=dict(requires_verified_tools=False,mcp_available=True,file_projection={'agent_id':bot,'content':question+IMAGE_CONTEXT})
            saved=page_persistence.TOOL_SCHEMAS
            monkeypatch.setattr(page_persistence,'TOOL_SCHEMAS',[])
            before=build_context_preview(db,user,room,question,TOOL_SCHEMAS,**kwargs)['agents'][0]
            monkeypatch.setattr(page_persistence,'TOOL_SCHEMAS',saved)
            after=build_context_preview(db,user,room,question,TOOL_SCHEMAS,**kwargs)['agents'][0]
            assert before['projection_status']=='ready' and after['projection_status']=='ready', (provider,before,after)
            results[provider]=(before['budget']['used_chars'],after['budget']['used_chars'])
            assert 0<results[provider][0]<results[provider][1]<=32000
            create=build_context_preview(db,user,room,'Create a dashboard Page from these items.',TOOL_SCHEMAS,
                                         requires_verified_tools=False,mcp_available=True)['agents'][0]
            edit=build_context_preview(db,user,room,'Add a summary card.',TOOL_SCHEMAS,
                                       requires_verified_tools=False,mcp_available=True,
                                       page_target={'result_id':page_id,'version':1})['agents'][0]
            assert create['projection_status']=='ready' and edit['projection_status']=='ready', (provider,create,edit)
            assert 'This is a managed Page.' in edit['system_text']
            assert 'File tools do not update the Page.' in edit['system_text']
            assert 'This is a managed Page.' not in create['system_text']
            print(provider,'Page create/edit projected chars',create['budget']['used_chars'],edit['budget']['used_chars'],'Page JSON chars',len(canonical))
    print('synthetic default tools + palette MCP exact projected chars:',results)


@pytest.mark.parametrize('changed_scope',['none','multi','mcp','owner'])
def test_page_sidechat_mcp_once_then_write_same_page(room_client,monkeypatch,changed_scope):
    from time import sleep
    from types import SimpleNamespace
    from copy import deepcopy
    from test_page_documents import page as sample_page
    from test_room_state import headers
    from test_bot_creation import private_team
    from test_execution_scopes import FakeExecutor
    from test_mcp_delegated import bind_tool
    from app.models.collaboration import ToolAction,RoomAgentRun,RoomEvent
    from app.models.room_result import RoomResult,RoomResultVersion
    from app.services.tool_broker import ToolBroker
    from app.services.providers import ProviderResult,ToolCall
    from app.services.providers.base import wire_tool_name
    def tool(name,args,call='create-one'):
        return ProviderResult(tool_calls=[ToolCall(call,name,args)],continuation={'call_ids':[call],
            'assistant_content':[{'type':'tool_use','id':call,'name':wire_tool_name(name),'input':args}]})
    client,(owner,_,project,_,_)=room_client;auth=headers(client,owner)
    bot,room,_=private_team(client,owner,project);scheduler=client.app.state.scheduler
    if changed_scope=='multi':
        from app.models.collaboration import RoomScheduleSettings
        with client.app.state.session_factory() as db:
            settings=db.get(RoomScheduleSettings,room)
            if settings is None:settings=RoomScheduleSettings(room_id=room);db.add(settings)
            settings.max_rounds=1;settings.max_tool_calls=4;settings.max_executor_seconds=60;db.commit()
    scheduler.broker.executor=SimpleNamespace()
    original={**sample_page(),'title':'Before'}
    original['datasets'][0]['id']='Sales-2026'
    original['views'][0]['dataset']='Sales-2026'
    original['datasets'][0]['columns'][0]['key']='Name-Item'
    for row in original['datasets'][0]['rows']:
        if 'name' in row:row['Name-Item']=row.pop('name')
    original['blocks'][3]['field']='Name-Item'
    original['blocks'][-1]['tabs'][0]['blocks'][0]['columns'][0]='Name-Item'
    original['blocks'][-1]['tabs'][1]['blocks'][0]['category']='Name-Item'
    original['datasets'][0]['columns'].append({'key':'region','label':'Region','type':'text'})
    original['datasets'][0]['rows'].append({'Name-Item':'Third','total':9,'active':True,'date':'2026-11-01','region':'East'})
    original['views'][0]['filters'].append('region_filter')
    original['blocks'].append({'type':'control','id':'region_filter','view':'main','field':'region','mode':'select','label':'Region'})
    with client.app.state.session_factory() as db:
        from app.models.models import Agent
        agent=db.get(Agent,bot)
        agent.system_prompt='Follow the Owner request. Observe the supplied image, propose the matching tool call for Owner review, and never claim execution before its completed result.'
        db.commit()
    # A Go-shaped connection exercises the same bounded projection as the
    # supported provider; credentials stay in this test's in-memory store.
    from test_connections import FakeSecrets, _version_one_payload
    from app.models.collaboration import ProviderConnection
    client.app.state.secret_store=FakeSecrets()
    connection_response=client.post('/api/v1/connections',headers=auth,json=_version_one_payload(
        provider_id='opencode-go',protocol_id='anthropic_messages',label='Synthetic Go',
        default_model_id='qwen3.7-plus',fields={},api_key='synthetic-only',capabilities={}))
    assert connection_response.status_code==201,connection_response.text
    with client.app.state.session_factory() as db:
        connection=db.get(ProviderConnection,connection_response.json()['id'])
        model='qwen3.7-plus';agent=db.get(Agent,bot)
        agent.connection_id=connection.id;agent.model_id=model
        connection.capabilities={'declared':{'tools':True},'verified':{'models':{model:{'model_id':model,'base_url':connection.base_url,'tools':True}}},'model_features':{model:{'model_id':model,'base_url':connection.base_url,
            'config_revision':connection.readiness_revision,'tested':{'text_generation':True,'tool_calling':True}}}}
        db.commit()
    async def create(request):
        if request.observations:return ProviderResult(text='Created')
        return tool('write_page',{'document':original},call='initial-page')
    scheduler.provider=create
    made=client.post(f'/api/v1/conversations/{room}/messages',headers=auth,json={'content':'@Ada create Page'})
    assert made.status_code==201,made.text
    with client.app.state.session_factory() as db:
        page=db.query(RoomResult).filter_by(owner_id=owner,room_id=room).one();page_id=page.result_id
        meta=bind_tool(db,owner,bot)
    calls=[]
    class Bridge:
        available=True;core_epoch='b'*64
        async def close(self):pass
        async def configure(self,p):return {'state':'configured'}
        async def bind(self,p):return {'state':'bound'}
        async def revoke(self,p):return {'state':'revoked'}
        async def credential_status(self,p):return {'state':'missing'}
        async def discover(self,p):return {'state':'failed'}
        async def issue(self,p):calls.append('issue');return {'state':'issued','nonce':'a'*64}
        async def start(self,p):calls.append('start');return {'state':'completed','result':{'content':'SYNTHETIC_MCP_RESULT'}}
        async def cancel(self,p):return {'state':'cancel_requested'}
    bridge=Bridge();client.app.state.mcp_client=bridge
    changed={'value':False}
    epoch=lambda _: ('d' if changed['value'] and changed_scope=='owner' else 'c')*64
    monkeypatch.setattr(client.app.state.owner_epoch_provider,'current',epoch)
    scheduler.broker=ToolBroker(client.app.state.session_factory,FakeExecutor(),owner_epochs=SimpleNamespace(current=epoch))
    if changed_scope in {'mcp','owner'}:
        from app.services import page_persistence,mcp_registry
        real_apply=page_persistence.apply;real_digest=mcp_registry.digest
        def apply(*args,**kwargs):
            try:return real_apply(*args,**kwargs)
            except page_persistence.PageValidationFailure:
                changed['value']=True
                raise
        monkeypatch.setattr(page_persistence,'apply',apply)
        if changed_scope=='mcp':
            monkeypatch.setattr(mcp_registry,'digest',lambda tools: 'f'*64 if changed['value'] else real_digest(tools))
    updated={**original,'title':'After'}
    invalid=deepcopy(updated);invalid['blocks'][0].pop('text')
    invalid_row=deepcopy(updated);invalid_row['datasets'][0]['rows'][0]['UNKNOWN_ROW_KEY']='private-value'
    requests=[]
    async def edit(request):
        requests.append(request)
        if not request.observations:
            assert 'This is a managed Page.' in request.system
            assert 'File tools do not update the Page.' in request.system
            assert any(t['name']=='write_page' for t in request.tools)
            family=next(t['name'] for t in request.tools if t.get('mcp_binding'))
            return tool(family,{'message':'synthetic'},call='mcp-one')
        if 'SYNTHETIC_MCP_RESULT' in request.observations[-1].text:
            return tool('write_page',{'document':invalid},call='page-invalid')
        if request.observations[-1].state=='denied':
            assert [t['name'] for t in request.tools]==['write_page']
            assert request.tools[0]['parameters']['properties']['document']['properties']['datasets']['items']['properties']['columns']
            if changed_scope=='multi' and len(requests)==3:
                assert 'object_shape' in request.observations[-1].text
                return tool('write_page',{'document':invalid_row},call='page-invalid-row')
            assert ('reference' if changed_scope=='multi' else 'object_shape') in request.observations[-1].text
            return tool('write_page',{'document':updated},call='page-update')
        assert any(t.get('mcp_binding') for t in request.tools)
        assert [t['name'] for t in request.tools]==[t['name'] for t in requests[0].tools]
        return ProviderResult(text='Updated after MCP.')
    scheduler.provider=edit
    sent=client.post(f'/api/v1/conversations/{room}/events',headers={**auth,'Idempotency-Key':'page-mcp-edit'},json={'content':'Read the source then update the Page','page_target':{'result_id':page_id,'version':1}})
    assert sent.status_code==201,sent.text
    action_id=None
    for _ in range(300):
        with client.app.state.session_factory() as db:
            action=db.query(ToolAction).filter_by(room_id=room,trigger_event_id=sent.json()['event_id'],state='waiting').first()
            if action:action_id=action.action_id;break
        sleep(.01)
    assert action_id and not calls
    preview=client.get(f'/api/v1/tools/rooms/{room}/actions/{action_id}/preview',headers=auth)
    assert preview.status_code==200,preview.text
    proof=preview.json()
    approved=client.post(f'/api/v1/tools/rooms/{room}/actions/{action_id}/decision',headers=auth,json={
        'decision':'approve','scope':'once','review_id':proof['review_id'],
        'acknowledge_unverified_context':proof['approval_policy']['unverified_context_required']})
    assert approved.status_code==200,approved.text
    for _ in range(300):
        with client.app.state.session_factory() as db:
            done=db.query(RoomEvent).filter_by(room_id=room,actor_type='agent',kind='message').all()
            if changed_scope in {'mcp','owner'} and db.query(RoomEvent).filter_by(room_id=room,kind='failure').first():break
            if (db.get(RoomResult,page_id).current_version==2 and db.get(RoomAgentRun,(room,bot)).state=='idle'
                    and any(e.payload.get('trigger_event_id')==sent.json()['event_id'] and e.payload.get('content')=='Updated after MCP.' for e in done)):break
        sleep(.01)
    with client.app.state.session_factory() as db:
        if changed_scope in {'mcp','owner'}:
            failures=db.query(RoomEvent).filter_by(room_id=room,kind='failure').all()
            assert len(failures)==1
            assert failures[0].payload['code'] in ({'mcp_context_changed'} if changed_scope=='mcp' else {'review_changed','owner_changed','context_changed','page_changed'})
            assert db.get(RoomResult,page_id).current_version==1
            assert len(requests)==2 and calls==['issue','start']
            return
        if db.get(RoomResult,page_id).current_version!=2:
            print('synthetic failures',[(e.payload.get('code'),e.payload.get('message')) for e in db.query(RoomEvent).filter_by(room_id=room,kind='failure').all()])
        assert db.get(RoomResult,page_id).current_version==2
        assert db.get(RoomResultVersion,(page_id,2)).title=='After'
        assert db.get(RoomAgentRun,(room,bot)).state=='idle'
        assert any(e.payload.get('trigger_event_id')==sent.json()['event_id'] and e.payload.get('content')=='Updated after MCP.'
                   for e in db.query(RoomEvent).filter_by(room_id=room,actor_type='agent',kind='message').all())
    assert calls==['issue','start']
    assert len(requests)==(5 if changed_scope=='multi' else 4)
    assert any('Page JSON' in str(e.get('payload',{}).get('content','')) and 'p_' in str(e.get('payload',{}).get('content','')) for e in requests[0].events)
    _measure_page_continuation_wires([requests[0],requests[1],requests[-1]],updated,requests[2],invalid)


def _measure_page_continuation_wires(requests,document,repair_request,invalid):
    """Serialize the real synthetic scheduler profile without network calls."""
    import json
    from dataclasses import replace
    from app.services.context import provider_text_size
    from app.services.providers.base import attributed_messages,continuation_scope,observation_text,wire_tool_name,provider_json_text
    from app.services.providers.go_budget import go_initial_body,go_continuation_body
    r0,r1,r2=requests
    family=next(t['name'] for t in r0.tools if t.get('mcp_binding'))
    assert any(t['name']=='write_page' for t in r0.tools)
    assert len([t for t in r0.tools if t.get('mcp_binding')])==1
    output={}
    for label,provider,protocol,model,base in [
        ('go_messages','opencode-go','anthropic_messages','qwen3.7-plus','https://opencode.ai/zen/go/v1'),
        ('go_openai_shape','opencode-go','openai_chat_completions','synthetic-openai','https://opencode.ai/zen/go/v1'),
        ('oauth','chatgpt','openai_responses','gpt-6-luna','https://api.openai.com/v1')]:
        caps={'declared':{'tools':True},'verified':{'models':{model:{'model_id':model,'base_url':base,'tools':True}}},
              'model_features':{model:{'model_id':model,'base_url':base,'config_revision':2,'tested':{'text_generation':True,'tool_calling':True}}}}
        def variant(req):
            conn=replace(req.connection,provider_kind='compatible' if provider=='opencode-go' else 'openai',
                         provider_id=provider,protocol_id=protocol,base_url=base,model_id=model,capabilities=caps,config_revision=2)
            return replace(req,connection=conn,model_id=model)
        p0,p1,p2=map(variant,(r0,r1,r2));repair=variant(repair_request);name=wire_tool_name(family)
        if provider=='opencode-go':
            def state(call_id,tool_name,args):
                if protocol=='anthropic_messages':return {'assistant_content':[{'type':'tool_use','id':call_id,'name':tool_name,'input':args}]}
                return {'assistant':{'role':'assistant','content':None,'tool_calls':[{'id':call_id,'type':'function','function':{'name':tool_name,'arguments':json.dumps(args)}}]}}
            initial=len(provider_json_text(go_initial_body(protocol=protocol,model=model,system=p0.system,tools=p0.tools,messages=attributed_messages(p0),temperature=p0.temperature,max_tokens=p0.max_tokens)))
            def continuation(p,st):
                body=go_continuation_body(protocol=protocol,model=model,system=p.system,tools=p.tools,messages=attributed_messages(p),state=st,observations=p.observations,temperature=p.temperature,max_tokens=p.max_tokens)
                return len(provider_json_text(body))
            mcp=continuation(p1,state('mcp-one',name,{'message':'synthetic'}))
            correction=continuation(repair,state('page-invalid','write_page',{'document':invalid}))
            page=continuation(p2,state('page-update','write_page',{'document':document}))
        else:
            initial=provider_text_size(replace(p0,continuation=None,observations=[]))
            mcp_call={'type':'function_call','namespace':'gamja','call_id':'mcp-one','name':name,'arguments':json.dumps({'message':'synthetic'})}
            st1={'provider_kind':'chatgpt',**continuation_scope(p1),'input':[*attributed_messages(p0),mcp_call],'call_ids':['mcp-one']}
            mcp=provider_text_size(replace(p1,continuation=st1))
            mcp_result={'type':'function_call_output','call_id':'mcp-one','output':observation_text(p1.observations[0])}
            invalid_call={'type':'function_call','namespace':'gamja','call_id':'page-invalid','name':'write_page','arguments':json.dumps({'document':invalid},ensure_ascii=False)}
            invalid_state={'provider_kind':'chatgpt',**continuation_scope(repair),'input':[*st1['input'],mcp_result,invalid_call],'call_ids':['page-invalid']}
            correction=provider_text_size(replace(repair,continuation=invalid_state))
            invalid_result={'type':'function_call_output','call_id':'page-invalid','output':observation_text(repair.observations[0])}
            page_call={'type':'function_call','namespace':'gamja','call_id':'page-update','name':'write_page','arguments':json.dumps({'document':document},ensure_ascii=False)}
            st2={'provider_kind':'chatgpt',**continuation_scope(p2),'input':[*invalid_state['input'],invalid_result,page_call],'call_ids':['page-update']}
            page=provider_text_size(replace(p2,continuation=st2))
        output[label]={'initial':initial,'mcp_continuation':mcp,'correction':correction,'page_continuation':page}
    print('exact synthetic scheduler role+Page+all tools+MCP wire characters:',output)
    # Current text-only Go and bound Responses continuations use the 64k
    # serialized hard limit; the former 32k assertion predates that contract.
    from app.services.context import MAX_GO_CONTINUATION_WIRE_CHARS
    assert all(size <= MAX_GO_CONTINUATION_WIRE_CHARS
               for measurements in output.values() for size in measurements.values()), output


@pytest.mark.asyncio
async def test_provider_compact_utf8_transport_preserves_body_and_headers(monkeypatch):
    import json
    from types import SimpleNamespace
    import httpx
    from app.services.providers import base, chatgpt
    body={'model':'합성','items':[{'text':'한글 and red','count':1}]}
    seen=[]
    def handle(request):
        seen.append(request)
        assert request.headers['authorization']=='Bearer synthetic'
        assert request.headers['content-type']=='application/json'
        assert json.loads(request.content)==body
        assert request.content==base.provider_json_bytes(body)
        assert request.content.decode('utf-8')==base.provider_json_text(body)
        assert len(request.content.decode('utf-8'))==len(base.provider_json_text(body))<len(request.content)
        if request.url.path.endswith('/responses'):
            return httpx.Response(200,headers={'content-type':'text/event-stream'},text='data: {"type":"response.completed","response":{"status":"completed"}}\n\n')
        return httpx.Response(200,json={'ok':True})
    request=SimpleNamespace(connection=object(),cancel=None)
    monkeypatch.setattr(base,'check_options',lambda _:None)
    monkeypatch.setattr(base,'provider_runtime_origin',lambda _:(SimpleNamespace(provider_id='synthetic'),'https://example.invalid'))
    generic=base.ProviderAdapter(httpx.MockTransport(handle));generic.kind='synthetic'
    assert (await generic._post_unmetered(request,'/generate',{'Authorization':'Bearer synthetic'},body))['ok']
    from app.services import connections
    monkeypatch.setattr(connections,'provider_runtime_origin',lambda _:(SimpleNamespace(provider_id='chatgpt'),'https://api.openai.com/v1'))
    oauth=chatgpt.ChatGPTAdapter(httpx.MockTransport(handle))
    assert (await oauth._post_unmetered(request,'/responses',{'Authorization':'Bearer synthetic'},body))['status']=='completed'
    assert len(seen)==2


def test_typed_correction_wire_defers_page_content_to_authoritative_validator():
    from copy import deepcopy
    from test_page_documents import page as sample_page
    from app.services.page_persistence import correction_tool_schema
    from app.services.providers.base import normalized_call
    from app.services.scheduler import validate_tool_calls
    from app.services.page_documents import normalize_document
    document=deepcopy(sample_page());document['datasets'][0]['id']='Sales-Upper'
    document['views'][0]['dataset']='Sales-Upper'
    offered=[correction_tool_schema()]
    assert offered[0]['parameters']['properties']['document']['properties']['datasets']['items']['properties']['columns']
    call=normalized_call('synthetic-id','write_page',{'document':document},offered)
    validate_tool_calls([call],offered,{'call_ids':['synthetic-id']})
    normalized,_,_=normalize_document(call.arguments['document'])
    assert normalized['datasets'][0]['id'].startswith('p_')
    assert normalized['views'][0]['dataset']==normalized['datasets'][0]['id']


def test_correction_schema_requires_aggregate_for_metric_and_chart():
    from copy import deepcopy
    from jsonschema import Draft202012Validator,ValidationError
    from test_page_documents import page as sample_page
    from app.services.page_persistence import correction_tool_schema
    from app.services.page_documents import normalize_document
    schema=correction_tool_schema()['parameters']['properties']['document']
    Draft202012Validator.check_schema(schema)
    document=sample_page()
    Draft202012Validator(schema).validate(document)
    assert normalize_document(document)[0]==document
    for index,block in enumerate(({'type':'metric','view':'main','label':'Rows'},
                                  {'type':'chart','view':'main','kind':'bar','category':'name','title':'Counts'})):
        candidate=deepcopy(document);candidate['blocks']=[block]
        with pytest.raises(ValidationError):Draft202012Validator(schema).validate(candidate)
        candidate['blocks'][0]['aggregate']='count'
        # The other controls are omitted from this minimal example.
        candidate['views'][0]['filters']=[]
        Draft202012Validator(schema).validate(candidate)
        assert normalize_document(candidate)[0]==candidate


def test_correction_example_uses_declared_flat_row_keys_and_preserves_values():
    import json
    from copy import deepcopy
    from jsonschema import Draft202012Validator,ValidationError
    from app.services.page_persistence import correction_tool_schema
    from app.services.page_documents import normalize_document,PageDocumentError
    tool=correction_tool_schema()
    sample=json.loads(tool['description'].split('Complete example: ',1)[1].removesuffix('.'))
    schema=tool['parameters']['properties']['document']
    Draft202012Validator(schema).validate(sample)
    normalized,_,_=normalize_document(sample)
    assert normalized==sample and sample['datasets'][0]['rows']==[{'group':'A','amount':2}]
    invalid=deepcopy(sample);invalid['datasets'][0]['rows'][0]['private-key']='private-value'
    with pytest.raises(PageDocumentError) as error:normalize_document(invalid)
    assert error.value.code=='page_document_reference' and error.value.path=='datasets[0].rows[0].cells'
    assert 'private' not in repr(vars(error.value))
    nested=deepcopy(sample);nested['datasets'][0]['rows'][0]['amount']={'nested':'x'}
    with pytest.raises(ValidationError):Draft202012Validator(schema).validate(nested)


@pytest.mark.parametrize('case',['corrected','missing_root','second_invalid','aggregate_missing','cas','unknown','prose','overflow'])
def test_page_validation_single_safe_correction(room_client,monkeypatch,case):
    from types import SimpleNamespace
    from test_room_state import headers
    from test_bot_creation import private_team,tool
    from app.models.room_result import RoomResult
    from app.models.collaboration import RoomEvent
    from app.services import page_persistence
    from app.services.llm_service import LLMFailure
    from app.services.providers import ProviderResult
    client,(owner,_,project,_,_)=room_client
    bot,room,_=private_team(client,owner,project)
    from app.models.collaboration import RoomScheduleSettings
    with client.app.state.session_factory() as db:
        settings=db.get(RoomScheduleSettings,room)
        if settings is None:settings=RoomScheduleSettings(room_id=room);db.add(settings)
        settings.max_rounds=1;settings.max_tool_calls=4;settings.max_executor_seconds=60;db.commit()
    scheduler=client.app.state.scheduler;scheduler.broker.executor=SimpleNamespace()
    if case=='overflow':
        from app.services import scheduler as scheduler_module
        normal_size=scheduler_module.provider_text_size
        def size(request):
            if (request.observations and request.observations[-1].state=='completed'
                    and 'page_version' in request.observations[-1].text):return 32001
            return normal_size(request)
        monkeypatch.setattr(scheduler_module,'provider_text_size',size)
    valid={'format':'gamja.page.v1','title':'Board','datasets':[],'views':[],
           'blocks':[{'type':'heading','level':1,'text':'Hello'}]}
    if case=='aggregate_missing':
        valid={'format':'gamja.page.v1','title':'Board','datasets':[{'id':'d','columns':[{'key':'x','label':'X','type':'text'}],'rows':[]}],
               'views':[{'id':'v','dataset':'d','filters':[]}], 'blocks':[{'type':'heading','level':1,'text':'Hello'}]}
    invalid={**valid,'blocks':[{'type':'heading','level':1}]}
    if case=='missing_root':invalid={key:value for key,value in valid.items() if key!='format'}
    seen=[]
    if case in {'cas','unknown'}:
        def fail(*args,**kwargs):
            raise LLMFailure('page_changed' if case=='cas' else 'provider_unavailable','Page write unavailable.')
        monkeypatch.setattr(page_persistence,'apply',fail)
    async def provider(request):
        seen.append(request.observations)
        if not request.observations:
            return tool('write_page',{'document':invalid},call='page-first')
        if request.observations[-1].state=='denied':
            assert 'nothing was saved' in request.observations[-1].text
            assert 'object_shape' in request.observations[-1].text
            if case=='prose':return ProviderResult(text='Page saved.')
            if case=='aggregate_missing':
                second={**valid,'blocks':[{'type':'heading','level':1,'text':'Hello'} for _ in range(4)]+[{'type':'metric','view':'v','label':'Rows'}]}
                return tool('write_page',{'document':second},call='page-second')
            return tool('write_page',{'document':invalid if case=='second_invalid' else valid},call='page-second')
        return ProviderResult(text='Page saved.')
    scheduler.provider=provider
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=headers(client,owner),json={'content':'@Ada create a Page'})
    assert response.status_code==(201 if case in {'corrected','missing_root'} else 502),response.text
    with client.app.state.session_factory() as db:
        saved=db.query(RoomResult).filter_by(owner_id=owner,room_id=room).all()
        failures=db.query(RoomEvent).filter_by(room_id=room,kind='failure').all()
        corrections=[e for e in db.query(RoomEvent).filter_by(room_id=room,kind='activity').all()
                     if e.payload.get('category')=='page_validation_correction']
        assert len(saved)==(1 if case in {'corrected','missing_root','overflow'} else 0)
        assert len(corrections)==(0 if case in {'cas','unknown'} else 3 if case in {'second_invalid','aggregate_missing'} else 1)
        if corrections:assert corrections[0].payload['path']==('document.format' if case=='missing_root' else 'blocks[0].text') and corrections[0].payload['reason']=='object_shape'
        assert bool(failures)==(case not in {'corrected','missing_root'})
        if case=='second_invalid':assert failures[0].payload['page_validation']=={'path':'blocks[0].text','reason':'object_shape'}
        if case=='aggregate_missing':
            assert failures[0].payload['page_validation']=={'path':'blocks[4].aggregate','reason':'object_shape',
                'block_shape':{'issue':'missing_required','block_type':'metric','missing_field':'aggregate'}}
            assert corrections[1].payload['block_shape']=={'issue':'missing_required','block_type':'metric','missing_field':'aggregate'}
            assert any('metric block is missing required field aggregate' in o.text for batch in seen for o in batch)

        if case in {'corrected','missing_root','overflow'}:assert saved[0].current_version==1
        else:assert not any(e.payload.get('content')=='Page saved.' for e in db.query(RoomEvent).filter_by(room_id=room,kind='message',actor_type='agent').all())
        if case=='overflow':
            assert failures[0].payload['code']=='context_too_large'
            assert len([e for e in db.query(RoomEvent).filter_by(room_id=room,kind='tool_result').all() if e.payload.get('family')=='write_page'])==1
    assert len(seen)==({'corrected':3,'missing_root':3,'second_invalid':4,'aggregate_missing':4,'cas':1,'unknown':1,'prose':2,'overflow':2}[case])


def test_two_presave_corrections_then_valid_page_within_budget(room_client):
    import json
    from copy import deepcopy
    from types import SimpleNamespace
    from test_room_state import headers
    from test_bot_creation import private_team,tool
    from test_page_documents import page as sample_page
    from app.models.collaboration import RoomEvent,RoomScheduleSettings
    from app.models.room_result import RoomResult,RoomResultVersion
    from app.services.providers import ProviderResult
    client,(owner,_,project,_,_)=room_client;bot,room,_=private_team(client,owner,project)
    with client.app.state.session_factory() as db:
        settings=db.get(RoomScheduleSettings,room)
        if settings is None:settings=RoomScheduleSettings(room_id=room);db.add(settings)
        settings.max_rounds=1;settings.max_tool_calls=4;settings.max_executor_seconds=60;db.commit()
    scheduler=client.app.state.scheduler;scheduler.broker.executor=SimpleNamespace()
    valid=sample_page();first=deepcopy(valid);first['blocks'][0].pop('text')
    second=deepcopy(valid);second['datasets'][0]['rows'][0]['NAME']='private-shape-value'
    calls=[]
    async def provider(request):
        calls.append(request)
        if len(calls)==1:return tool('write_page',{'document':first},call='first-invalid')
        if len(calls)==2:
            assert [x['name'] for x in request.tools]==['write_page']
            return tool('write_page',{'document':second},call='second-invalid')
        if len(calls)==3:
            assert [x['name'] for x in request.tools]==['write_page']
            assert 'reference' in request.observations[-1].text
            return tool('write_page',{'document':valid},call='valid-page')
        assert len(request.tools)>1
        return ProviderResult(text='Page saved')
    scheduler.provider=provider
    result=client.post(f'/api/v1/conversations/{room}/messages',headers=headers(client,owner),json={'content':'@Ada create Page'})
    assert result.status_code==201,result.text
    with client.app.state.session_factory() as db:
        page=db.query(RoomResult).filter_by(owner_id=owner,room_id=room).one()
        saved=db.get(RoomResultVersion,(page.result_id,1));document=json.loads(saved.content)
        corrections=[e for e in db.query(RoomEvent).filter_by(room_id=room,kind='activity').order_by(RoomEvent.seq) if e.payload.get('category')=='page_validation_correction']
        assert len(corrections)==2 and [e.payload['attempt'] for e in corrections]==[1,2]
        assert all(e.payload['agent_id']==bot for e in corrections)
        shape=corrections[1].payload['row_shape'];assert shape['unknown_keys']==1 and shape['unique_casefold_matches']==1
        assert 'NAME' not in json.dumps(shape) and 'private-shape-value' not in json.dumps(shape)
        assert document['datasets'][0]['rows']==valid['datasets'][0]['rows']
    assert len(calls)==4


def test_interrupted_presave_correction_never_regenerates_or_replays(room_client):
    from types import SimpleNamespace
    from test_bot_creation import private_team
    from app.models.collaboration import RoomAgentRun,RoomEvent,RoomTrigger
    from app.models.room_result import RoomResult
    from app.services.events import ActorRef,append_event,stage_event
    from app.services import resume_boundary
    client,(owner,_,project,_,_)=room_client;bot,room,_=private_team(client,owner,project)
    scheduler=client.app.state.scheduler;scheduler.broker.executor=SimpleNamespace();calls=[]
    async def provider(request):calls.append(request);raise AssertionError('must not regenerate')
    scheduler.provider=provider
    with client.app.state.session_factory() as db:
        root=append_event(db,room,ActorRef('user',owner),'message',{'content':'@Ada create Page'})
        root_id=root.event_id;watermark=root.seq;scheduler._create_trigger(db,root)
        trigger=db.get(RoomTrigger,root_id);trigger.state='running';trigger.current_round=1
        run=db.get(RoomAgentRun,(room,bot));run.state='idle';run.paused=False
        run.pending_task={'resume_boundary':{'trigger':root_id,'round':1,'guard':resume_boundary.guard(db,room,bot)}}
        stage_event(db,room,ActorRef('system',None),'activity',{'category':'page_validation_correction','trigger_event_id':root_id,
            'agent_id':bot,'attempt':1,'reason':'object_shape','path':'blocks[0]'},f'page-validation-correction:{root_id}:{bot}:1')
        db.commit()
    outcome=client.portal.call(lambda:scheduler._turn(room,root_id,bot,1,watermark))
    assert outcome=='failure' and calls==[]
    with client.app.state.session_factory() as db:
        failures=[e for e in db.query(RoomEvent).filter_by(room_id=room,kind='failure') if e.payload.get('trigger_event_id')==root_id]
        assert len(failures)==1 and failures[0].payload['code']=='page_correction_interrupted'
        assert db.query(RoomResult).filter_by(room_id=room).count()==0


@pytest.mark.parametrize("budget_exhausted",[False,True])
def test_resume_after_saved_page_keeps_result_without_replaying_write(room_client,budget_exhausted):
    from datetime import datetime,timezone
    from types import SimpleNamespace
    from uuid import uuid4
    from test_bot_creation import private_team,tool
    from app.models.collaboration import RoomAgentRun,RoomEvent,RoomTrigger
    from app.models.room_result import RoomResult,RoomResultVersion
    from app.services.events import ActorRef,append_event,stage_event
    from app.services.providers import ProviderResult
    from app.services import resume_boundary
    client,(owner,_,project,_,_)=room_client;bot,room,_=private_team(client,owner,project)
    scheduler=client.app.state.scheduler;scheduler.broker.executor=SimpleNamespace();calls=[]
    async def provider(request):
        calls.append(request)
        if budget_exhausted:return tool('write_page',{'document':{}},call='must-not-write')
        return ProviderResult(text='The saved Page remains available.')
    scheduler.provider=provider
    with client.app.state.session_factory() as db:
        root=append_event(db,room,ActorRef('user',owner),'message',{'content':'@Ada make a Page'})
        root_id=root.event_id;watermark=root.seq;scheduler._create_trigger(db,root)
        trigger=db.get(RoomTrigger,root_id);trigger.state='running';trigger.current_round=1
        if budget_exhausted:trigger.max_tool_calls=2
        stage_event(db,room,ActorRef('system',None),'activity',{'category':'page_validation_correction','trigger_event_id':root_id,
            'agent_id':bot,'attempt':1,'reason':'object_shape','path':'blocks[0]'},f'page-validation-correction:{root_id}:{bot}:1')
        page_id=str(uuid4());page_event=stage_event(db,room,ActorRef('tool',None),'tool_result',
            {'family':'write_page','status':'completed','trigger_event_id':root_id,'page_result':{'agent_id':bot,'result_id':page_id,'page_version':1}},'page-event:synthetic')
        now=datetime.now(timezone.utc);db.add(RoomResult(result_id=page_id,owner_id=owner,room_id=room,current_version=1,created_at=now,updated_at=now));db.flush()
        db.add(RoomResultVersion(result_id=page_id,version=1,room_id=room,kind='page',title='Saved',source_event_id=page_event.event_id,source_seq=page_event.seq,
            selector='synthetic',author_agent_id=bot,author_name_snapshot='Ada',execution={'trigger_event_id':root_id},content='{}',file_ref=None,evidence=[],state='draft',state_revision=1,link_report='unchecked',created_at=now))
        run=db.get(RoomAgentRun,(room,bot));run.state='idle';run.paused=False
        run.pending_task={'resume_boundary':{'trigger':root_id,'round':1,'guard':resume_boundary.guard(db,room,bot)}};db.commit()
    outcome=client.portal.call(lambda:scheduler._turn(room,root_id,bot,1,watermark))
    assert outcome==('failure' if budget_exhausted else 'message') and len(calls)==1
    with client.app.state.session_factory() as db:
        assert db.query(RoomResult).filter_by(room_id=room).count()==1 and db.get(RoomResult,(page_id,)).current_version==1
        writes=[e for e in db.query(RoomEvent).filter_by(room_id=room,kind='tool_result') if e.payload.get('family')=='write_page']
        assert len(writes)==1
        if budget_exhausted:
            failures=[e for e in db.query(RoomEvent).filter_by(room_id=room,kind='failure') if e.payload.get('trigger_event_id')==root_id]
            assert len(failures)==1 and failures[0].payload['code']=='tool_budget_exhausted'


def test_terminal_row_reference_failure_exposes_only_bounded_shape(room_client):
    import json
    from copy import deepcopy
    from types import SimpleNamespace
    from test_page_documents import page as sample_page
    from test_room_state import headers
    from test_bot_creation import private_team,tool
    from app.models.collaboration import RoomEvent,RoomScheduleSettings
    client,(owner,_,project,_,_)=room_client;bot,room,_=private_team(client,owner,project)
    with client.app.state.session_factory() as db:
        settings=db.get(RoomScheduleSettings,room)
        if settings is None:settings=RoomScheduleSettings(room_id=room);db.add(settings)
        settings.max_rounds=1;settings.max_tool_calls=1;settings.max_executor_seconds=60;db.commit()
    doc=deepcopy(sample_page());doc['datasets'][0]['rows'][0]['PRIVATE_UNKNOWN']={'payload':'PRIVATE_VALUE'}
    scheduler=client.app.state.scheduler;scheduler.broker.executor=SimpleNamespace();calls=[]
    async def provider(request):calls.append(1);return tool('write_page',{'document':doc},call='invalid')
    scheduler.provider=provider
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=headers(client,owner),json={'content':'@Ada create Page'})
    assert response.status_code==502 and len(calls)==1
    with client.app.state.session_factory() as db:
        failure=db.query(RoomEvent).filter_by(room_id=room,kind='failure').one().payload
        diag=failure['page_validation'];assert diag['reason']=='reference' and diag['path']=='datasets[0].rows[0].cells'
        assert diag['row_shape']['unknown_keys']==1 and diag['row_shape']['unknown_value_shapes']['object']==1
        assert 'PRIVATE_UNKNOWN' not in json.dumps(failure) and 'PRIVATE_VALUE' not in json.dumps(failure)


@pytest.mark.parametrize('kind',['metric','chart'])
def test_page_block_shape_guidance_is_fixed_and_content_free(kind):
    from app.services.page_documents import normalize_document, PageDocumentError, safe_block_shape
    from app.services.page_persistence import PageValidationFailure, block_shape_guidance
    block={'type':kind,'view':'v',**({'label':'Rows'} if kind=='metric' else {'kind':'bar','category':'x','title':'Rows'})}
    doc={'format':'gamja.page.v1','title':'Board','datasets':[{'id':'d','columns':[{'key':'x','label':'X','type':'text'}],'rows':[]}],
         'views':[{'id':'v','dataset':'d','filters':[]}],'blocks':[block]}
    with pytest.raises(PageDocumentError) as caught: normalize_document(doc)
    failure=PageValidationFailure(caught.value)
    assert failure.path=='blocks[0].aggregate'
    assert failure.block_shape=={'issue':'missing_required','block_type':kind,'missing_field':'aggregate'}
    assert f'{kind} block is missing required field aggregate' in block_shape_guidance(failure.block_shape)
    extra={**block,'aggregate':'count','private_synthetic_key':'private_synthetic_value'}
    with pytest.raises(PageDocumentError) as caught: normalize_document({**doc,'blocks':[extra]})
    failure=PageValidationFailure(caught.value)
    assert failure.block_shape=={'issue':'extra_fields','block_type':kind,'missing_field':None}
    assert 'private_synthetic' not in str(failure.block_shape)+block_shape_guidance(failure.block_shape)
    assert safe_block_shape({'issue':'missing_required','block_type':kind,'missing_field':'private_synthetic_key'}) is None
    assert safe_block_shape({'issue':'extra_fields','block_type':['untrusted'],'missing_field':None}) is None
