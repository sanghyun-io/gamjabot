"""Owner draft admission and exact reviewed synthetic MCP execution."""
import asyncio,json
from types import SimpleNamespace
import pytest
from test_room_state import room_client,headers
from test_bot_creation import private_team
from test_execution_scopes import FakeExecutor
from test_mcp_delegated import bind_tool
from test_primary_checkins import model_use
from app.models.collaboration import ToolAction,RoomEvent
from app.models.mcp import MCPOwnerDraft,MCPBinding,MCPTool
from app.models.owner_routines import OwnerRoutineRun
from app.schemas.owner_routines import RoutineTest
from app.services import owner_routines as routines,mcp_drafts,mcp_registry
from app.services.providers import ProviderResult,ToolCall
from app.services.tool_broker import ToolBroker


def test_descriptors_hide_credentials_and_unknowns():
    schema={'type':'object','properties':{'to':{'type':'array','items':{'type':'string'}},'message':{'type':'object','properties':{'body':{'type':'string'},'api_key':{'type':'string'},'password':{'type':'string'}}},'hidden':{'type':'string','writeOnly':True},'cc':{'type':'array','items':{'type':'string','maxLength':40},'maxItems':5}}}
    raw={'to':['a@example.invalid'],'message':{'body':'hello','api_key':'secret-value','password':'pass'},'hidden':'hidden','extra':'unknown'}
    descriptors,values=mcp_drafts.fields(schema,raw)
    assert set(values)=={'/to','/message/body'} and {x['path'] for x in descriptors}==set(values)|{'/cc'}
    assert next(x for x in descriptors if x['path']=='/cc')['max_items']==5
    assert 'secret-value' not in json.dumps(values)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['none','binding','owner_epoch','stop','invalid'])
async def test_draft_apply_requires_new_once_and_executes_edited_payload(room_client,monkeypatch,failure):
    client,(owner,other,project,_,_)=room_client;auth=headers(client,owner);bot,room,_=private_team(client,owner,project)
    scheduler=client.app.state.scheduler;calls=[];requests=[]
    class Bridge:
        available=True;core_epoch='b'*64
        async def close(self):pass
        async def configure(self,p):return {'state':'configured'}
        async def bind(self,p):return {'state':'bound'}
        async def revoke(self,p):return {'state':'revoked'}
        async def credential_status(self,p):return {'state':'missing'}
        async def discover(self,p):return {'state':'failed'}
        async def issue(self,p):calls.append(('issue',p));return {'state':'issued','nonce':'a'*64}
        async def start(self,p):calls.append(('start',p));return {'state':'completed','result':{'echo':p['arguments']['message']}}
        async def cancel(self,p):calls.append(('cancel',p));return {'state':'cancel_requested'}
    bridge=Bridge();client.app.state.mcp_client=bridge
    monkeypatch.setattr(client.app.state.owner_epoch_provider,'current',lambda _:'c'*64)
    scheduler.broker=ToolBroker(client.app.state.session_factory,FakeExecutor(),owner_epochs=SimpleNamespace(current=lambda _:'c'*64))
    with client.app.state.session_factory() as db:
        meta=bind_tool(db,owner,bot)
        tool=db.get(MCPTool,meta['tool_id'])
        schema={'type':'object','properties':{'message':{'type':'string',**({'minLength':5} if failure=='invalid' else {})},'api_key':{'type':'string'}},'required':['message']}
        tool.input_schema=schema;tool.schema_canonical=mcp_registry.canonical(schema);tool.schema_sha256=mcp_registry.digest(schema);db.commit()
        meta=mcp_registry.offered(db,owner,bot)[0]['mcp_binding']
    async def provider(request):
        requests.append(request)
        if not request.observations:
            offered=[t for t in request.tools if t.get('mcp_binding')]
            return ProviderResult(tool_calls=[ToolCall('draft-call',offered[0]['name'],{'message':'ORIGINAL','api_key':'SYNTHETIC_PRIVATE'})],continuation={'call_ids':['draft-call']})
        assert 'OWNER_EDIT' not in request.observations[0].text
        return ProviderResult(text='SYNTHETIC_DONE',model_use={**model_use(request),'watermark_seq':request.watermark_seq})
    scheduler.provider=provider
    definition={'name':'Synthetic','instruction':'Use synthetic echo','enabled':False,'schedule':{'kind':'interval','interval_minutes':5,'timezone':'UTC'}}
    made=client.post(f'/api/v1/bots/{bot}/routines',headers={**auth,'Idempotency-Key':'draft-routine'},json=definition)
    with client.app.state.session_factory() as db:claim,_=routines.test_claim(db,owner,bot,made.json()['routine']['routine_id'],RoutineTest(expected_revision=1),'draft-test')
    task=asyncio.create_task(routines.execute(scheduler,claim['run']['run_id']));action_id=None
    try:
        for _ in range(150):
            await asyncio.sleep(.02)
            with client.app.state.session_factory() as db:
                action=db.query(ToolAction).filter_by(room_id=room,agent_id=bot,state='waiting').first()
                if action:action_id=action.action_id;break
        assert action_id and not calls
        base=f'/api/v1/tools/rooms/{room}/actions/{action_id}'
        original=client.get(base+'/preview',headers=auth).json();review=original['review_id']
        got=client.get(base+'/draft',headers=auth);assert got.status_code==200,got.text
        assert got.json()['available'] and got.json()['values']=={'/message':'ORIGINAL'}
        assert client.get(base+'/draft',headers=headers(client,other)).status_code==404
        body={'expected_revision':0,'expected_review_id':review,'edits':{'/message':'X' if failure=='invalid' else 'OWNER_EDIT'}}
        assert client.put(base+'/draft',headers={**auth,'Idempotency-Key':'save'},json={**body,'edits':{'/secret':'X'}}).status_code==422
        saved=client.put(base+'/draft',headers={**auth,'Idempotency-Key':'save'},json=body);assert saved.status_code==200,saved.text
        assert client.get(base+'/draft/commands/save',headers=auth).json()==saved.json()
        assert client.get(base+'/draft',headers=auth).json()['pending'] is True
        if failure!='none':
            if failure=='invalid':pass
            elif failure=='binding':
                with client.app.state.session_factory() as db:
                    binding=db.get(MCPBinding,(owner,bot,meta['server_id']));binding.enabled=False;binding.revision+=1;db.commit()
            elif failure=='owner_epoch':scheduler.broker.owner_epochs.current=lambda _:'d'*64
            else:
                scheduler.cancel_routine(owner,routine_id=made.json()['routine']['routine_id'])
                await asyncio.wait_for(task,5)
            rejected=client.post(base+'/draft/apply',headers={**auth,'Idempotency-Key':'stale-apply'},json={'expected_revision':1,'expected_review_id':review})
            assert rejected.status_code==409 and not calls
            return
        early={'decision':'approve','scope':'once','review_id':review,'acknowledge_unverified_context':True}
        assert client.post(base+'/decision',headers=auth,json=early).status_code==409
        retained=scheduler.broker._previews.pop(action_id)
        try:
            lost=client.get(base+'/draft',headers=auth).json()
            assert not lost['available'] and lost['pending'] and lost['edits']=={'/message':'OWNER_EDIT'}
            assert client.post(base+'/draft/apply',headers={**auth,'Idempotency-Key':'lost'},json={'expected_revision':1,'expected_review_id':review}).status_code==409
        finally:scheduler.broker._previews[action_id]=retained
        assert client.put(base+'/draft',headers={**auth,'Idempotency-Key':'stale'},json=body).status_code==409
        cleared=client.put(base+'/draft',headers={**auth,'Idempotency-Key':'clear'},json={'expected_revision':1,'expected_review_id':review,'edits':{}})
        assert cleared.status_code==200 and cleared.json()['revision']==2
        assert client.get(base+'/draft',headers=auth).json()['pending'] is False
        saved2=client.put(base+'/draft',headers={**auth,'Idempotency-Key':'save2'},json={**body,'expected_revision':2})
        assert saved2.status_code==200 and saved2.json()['revision']==3
        def competing(key):
            return client.put(base+'/draft',headers={**auth,'Idempotency-Key':key},json={**body,'expected_revision':3})
        first,second=await asyncio.gather(asyncio.to_thread(competing,'race1'),asyncio.to_thread(competing,'race2'))
        assert sorted([first.status_code,second.status_code])==[200,409]
        applied=client.post(base+'/draft/apply',headers={**auth,'Idempotency-Key':'apply'},json={'expected_revision':4,'expected_review_id':review})
        assert applied.status_code==200,applied.text
        new_review=applied.json()['review_id'];assert new_review!=review and not calls
        replay=client.post(base+'/draft/apply',headers={**auth,'Idempotency-Key':'apply'},json={'expected_revision':4,'expected_review_id':review})
        assert replay.status_code==200 and replay.json()==applied.json()
        assert client.get(base+'/draft/commands/apply',headers=auth).json()==applied.json()
        after=client.get(base+'/draft',headers=auth).json();assert after['pending'] is False and after['edits']=={'/message':'OWNER_EDIT'} and after['revision']==5
        old_decision={'decision':'approve','scope':'once','review_id':review,'acknowledge_unverified_context':True}
        assert client.post(base+'/decision',headers=auth,json=old_decision).status_code==409
        current=client.get(base+'/preview',headers=auth);assert current.status_code==200,current.text
        assert current.json()['arguments']=={'message':'OWNER_EDIT','api_key':'[redacted]'} and current.json()['review_id']==new_review
        activity=client.get(f'/api/v1/tools/rooms/{room}/activity',headers=auth).json()
        shown=next(item for item in activity if item['action_id']==action_id)
        assert shown['review_id']==new_review and shown['mcp_binding']==current.json()['mcp_binding']
        assert client.post(base+'/decision',headers=auth,json={**old_decision,'review_id':new_review}).status_code==200
        assert client.put(base+'/draft',headers={**auth,'Idempotency-Key':'after-approval'},json={'expected_revision':5,'expected_review_id':new_review,'edits':{}}).status_code==409
        await asyncio.wait_for(task,5)
        assert [x[0] for x in calls]==['issue','start'] and calls[0][1]['arguments']=={'message':'OWNER_EDIT','api_key':'SYNTHETIC_PRIVATE'}
        with client.app.state.session_factory() as db:
            run=db.get(OwnerRoutineRun,claim['run']['run_id']);assert run.state=='completed'
            draft=db.get(MCPOwnerDraft,action_id);assert draft.original_review_id==review and draft.applied_review_id==new_review
            assert 'SYNTHETIC_PRIVATE' not in json.dumps({'edits':draft.edits,'original_summary':draft.original_summary})
            action=db.get(ToolAction,action_id);assert action.observation['owner_edited'] is True and 'OWNER_EDIT' not in action.observation['text']
            event=db.query(RoomEvent).filter_by(room_id=room,idempotency_key='tool-result:'+action_id).one()
            assert event.payload['owner_edited'] is True and 'OWNER_EDIT' in event.payload['text']
            from app.services.resume_boundary import stored_results
            root=db.get(RoomEvent,run.root_event_id)
            resumed=stored_results(db,room,run.root_event_id,bot,root.seq)
            assert resumed and 'OWNER_EDIT' not in json.dumps(resumed)
            from app.models.models import Conversation
            db.get(Conversation,room).is_active=False;db.commit()
        assert client.put(base+'/draft',headers={**auth,'Idempotency-Key':'inactive-active-broker'},json={'expected_revision':5,'expected_review_id':new_review,'edits':{}}).status_code==404
        scheduler.broker=None  # Readback remains available after retained broker state is lost.
        after_stop=client.get(base+'/draft',headers=auth)
        assert after_stop.status_code==200
        stored=after_stop.json()
        assert stored['available'] is False and stored['fields']==[] and stored['values']=={}
        assert stored['edits']=={'/message':'OWNER_EDIT'} and stored['pending'] is False and stored['original_review_id']==review
        assert set(stored['original_summary'])=={'payload_digest','arguments_sha256','schema_sha256'}
        assert all(isinstance(v,str) and len(v)==64 for v in stored['original_summary'].values())
        assert 'SYNTHETIC_PRIVATE' not in json.dumps(stored)
        inactive_activity=client.get(f'/api/v1/tools/rooms/{room}/activity',headers=auth)
        assert inactive_activity.status_code==200
        record=next(item for item in inactive_activity.json() if item['action_id']==action_id)
        assert record['owner_draft']=={'revision':5,'pending':False,'applied':True}
        assert client.get(base+'/draft/commands/apply',headers=auth).json()==applied.json()
        assert client.get(base+'/draft',headers=headers(client,other)).status_code==404
        assert client.get(base+'/draft/commands/apply',headers=headers(client,other)).status_code==404
        assert client.put(base+'/draft',headers={**auth,'Idempotency-Key':'inactive'},json={'expected_revision':5,'expected_review_id':new_review,'edits':{}}).status_code==503
    finally:
        if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)


def test_draft_migration_preserves_revision46_data_and_is_idempotent(tmp_path,monkeypatch):
    import sqlite3
    from sqlalchemy import create_engine
    from app.core import migrate as migration
    path=tmp_path/'draft-migration.sqlite';monkeypatch.setenv('GAMJA_APP_DATA_DIR',str(tmp_path/'appdata'))
    engine=create_engine(f'sqlite:///{path}')
    with monkeypatch.context() as scoped:
        scoped.setattr(migration,'REVISIONS',migration.REVISIONS[:46]);migration.migrate_database(engine)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO users(id,email,username,hashed_password,is_active) VALUES (1,'synthetic@example.invalid','synthetic','fake',1)");db.commit()
        before=db.execute('SELECT * FROM users').fetchall()
        ddl=db.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name!='schema_version' ORDER BY type,name").fetchall()
    migration.migrate_database(engine);migration.migrate_database(engine);engine.dispose()
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT * FROM users').fetchall()==before
        after=db.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT IN ('schema_version','mcp_owner_drafts') ORDER BY type,name").fetchall()
        assert after==ddl
        assert db.execute('SELECT COUNT(*) FROM mcp_owner_drafts').fetchone()==(0,)
        assert db.execute('SELECT max(version) FROM schema_version').fetchone()==(47,)


def test_owner_edited_result_never_enters_recent_or_extractive_provider_context():
    from app.services.context_projection import project_turn_context
    events=[]
    for seq in range(1,31):
        event={'event_id':f'id-{seq}','room_id':1,'seq':seq,'actor_type':'user','actor_id':1,
               'actor_name_snapshot':'Synthetic','kind':'message','payload':{'content':f'history {seq}'}}
        if seq in (10,29):
            event.update(kind='tool_result',actor_type='tool',actor_id=None,
                         payload={'text':'OWNER_PRIVATE_EDIT_ECHO','owner_edited':True})
        events.append(event)
    projection=project_turn_context(events=events,extractive_checkpoints=[],watermark_seq=30,active_event_id='id-30',agent_id=1,system='role',memory={},tools=[])
    assert 'OWNER_PRIVATE_EDIT_ECHO' not in json.dumps(projection.messages)
    assert projection.checkpoint_plan is not None and 'OWNER_PRIVATE_EDIT_ECHO' not in projection.checkpoint_plan.content
    assert any('Owner-edited action result is available to the Owner.' in m['content'] for m in projection.messages)
