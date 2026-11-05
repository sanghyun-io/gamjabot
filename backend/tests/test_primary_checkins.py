import asyncio
from datetime import timedelta
import pytest
from test_room_state import room_client,headers
from test_bot_creation import private_team
from app.models.primary_checkins import PrimaryCheckin,PrimaryCheckinRun
from app.models.collaboration import RoomAgentRun,RoomEvent
from app.services import primary_checkins as service
from app.services.providers import ProviderResult

def model_use(request):
    return {'connection_id':request.connection_id,'model_id':request.model_id,'provider_kind':request.connection.provider_kind}


def seed_due(client,owner,project,history=False):
    agent,room,_=private_team(client,owner,project)
    own=headers(client,owner)
    assert client.put('/api/v1/bots/primary',headers=own,json={'agent_id':agent,'expected_revision':0}).status_code==200
    data={'expected_revision':0,'expected_primary_revision':1,'enabled':True,'prompt':'Ask about the synthetic project','interval_minutes':1}
    response=client.put('/api/v1/bots/primary/check-ins',headers=own,json=data)
    assert response.status_code==200,response.text
    with client.app.state.session_factory() as db:
        if history:
            from app.services.events import stage_event,ActorRef
            from app.services.room_memory import local_for
            old=stage_event(db,room,ActorRef('user',owner),'message',{'content':'BEFORE_FORGET_BOUNDARY'})
            local_for(db,room,agent).context_after_seq=old.seq
            stage_event(db,room,ActorRef('user',owner),'message',{'content':'CURRENT_CONVERSATION'})
        row=db.get(PrimaryCheckin,owner);row.next_run_at=service.now()-timedelta(minutes=3);db.commit()
        run_id=service.claim(db)
    assert run_id
    return agent,room,run_id

@pytest.mark.asyncio
async def test_due_one_text_only_result_and_owner_root(room_client):
    client,(owner,other,project,_,_)=room_client
    agent,room,rid=seed_due(client,owner,project);seen=[]
    async def provider(request):
        seen.append(request);return ProviderResult(text='SYNTHETIC_CHECKIN',model_use=model_use(request))
    client.app.state.scheduler.provider=provider
    await service.execute(client.app.state.scheduler,rid)
    with client.app.state.session_factory() as db:
        row=db.get(PrimaryCheckinRun,rid);assert row.state=='completed',row.failure_code
        root=db.get(RoomEvent,row.root_event_id);result=db.get(RoomEvent,row.result_event_id)
        assert root.actor_type=='system' and root.kind=='activity' and root.payload['category']=='primary_checkin'
        expected_root_id=root.event_id
        assert result.actor_type=='agent' and result.payload['content']=='SYNTHETIC_CHECKIN'
        assert result.payload['model_use']['model_id']==seen[0].model_id
        assert service.claim(db) is None
    from app.services.providers.base import attributed_messages
    wire=attributed_messages(seen[0])
    assert wire[-1]['role']=='user' and wire[-1]['content'].startswith('SERVER-SCHEDULED CHECK-IN')
    assert len(seen)==1 and not seen[0].tools and seen[0].trigger_event_id==expected_root_id
    assert client.get('/api/v1/bots/primary/check-ins/runs',headers=headers(client,other)).json()['runs']==[]

@pytest.mark.asyncio
async def test_disable_inflight_blocks_late_answer(room_client):
    client,(owner,_,project,_,_)=room_client
    agent,room,rid=seed_due(client,owner,project);entered=asyncio.Event();release=asyncio.Event()
    async def provider(request):
        entered.set();await release.wait();return ProviderResult(text='MUST_NOT_PUBLISH',model_use=model_use(request))
    client.app.state.scheduler.provider=provider
    task=asyncio.create_task(service.execute(client.app.state.scheduler,rid));await asyncio.wait_for(entered.wait(),2)
    with client.app.state.session_factory() as db:
        row=db.get(PrimaryCheckin,owner);row.enabled=False;row.revision+=1;db.commit()
    release.set();await task
    with client.app.state.session_factory() as db:
        row=db.get(PrimaryCheckinRun,rid);assert row.state=='cancelled' and row.result_event_id is None
        assert db.get(RoomAgentRun,(room,agent)).state=='idle'


def test_restart_reconciles_without_replay(room_client):
    client,(owner,_,project,_,_)=room_client
    agent,room,rid=seed_due(client,owner,project)
    with client.app.state.session_factory() as db:
        service.reconcile(db);row=db.get(PrimaryCheckinRun,rid)
        assert row.state=='interrupted' and row.result_event_id is None
        assert db.get(RoomAgentRun,(room,agent)).state=='idle'
        assert service.claim(db) is None

@pytest.mark.asyncio
async def test_stop_during_provider_prevents_result(room_client):
    client,(owner,_,project,_,_)=room_client
    agent,room,rid=seed_due(client,owner,project);entered=asyncio.Event();release=asyncio.Event()
    async def provider(request):
        entered.set();await release.wait();return ProviderResult(text='LATE_RESULT',model_use=model_use(request))
    scheduler=client.app.state.scheduler;scheduler.provider=provider
    task=asyncio.create_task(service.execute(scheduler,rid));await asyncio.wait_for(entered.wait(),2)
    from app.services import room_service
    with client.app.state.session_factory() as db:room_service.stop_room(db,room)
    scheduler.cancel(room);release.set();await task
    with client.app.state.session_factory() as db:
        row=db.get(PrimaryCheckinRun,rid);assert row.state=='cancelled' and row.result_event_id is None
        assert db.get(RoomAgentRun,(room,agent)).state=='stopped'

@pytest.mark.asyncio
async def test_replacement_and_forged_origin_never_dispatch(room_client):
    client,(owner,_,project,_,_)=room_client
    agent,room,rid=seed_due(client,owner,project);seen=[]
    async def provider(request):seen.append(request);return ProviderResult(text='MUST_NOT_RUN',model_use=model_use(request))
    scheduler=client.app.state.scheduler;scheduler.provider=provider
    with client.app.state.session_factory() as db:
        row=db.get(PrimaryCheckinRun,rid);event=db.get(RoomEvent,row.root_event_id)
        event.payload={**event.payload,'category':'other'};db.commit()
    await service.execute(scheduler,rid)
    assert not seen
    with client.app.state.session_factory() as db:
        assert db.get(PrimaryCheckinRun,rid).state=='cancelled'
    assert client.put('/api/v1/bots/primary',headers=headers(client,owner),json={'agent_id':None,'expected_revision':1}).status_code==200
    status=client.get('/api/v1/bots/primary/check-ins',headers=headers(client,owner)).json()
    assert status['availability']=='primary_changed' and status['primary_revision']==2 and status['next_run_at'] is None

@pytest.mark.asyncio
async def test_server_root_never_enters_user_queue_and_config_cas(room_client):
    client,(owner,_,project,_,_)=room_client
    agent,room,rid=seed_due(client,owner,project)
    with client.app.state.session_factory() as db:
        row=db.get(PrimaryCheckinRun,rid);root=row.root_event_id
    with pytest.raises(ValueError):
        await client.app.state.scheduler.schedule_user_event(room,root)
    bad=client.put('/api/v1/bots/primary/check-ins',headers=headers(client,owner),json={
        'expected_revision':0,'expected_primary_revision':1,'enabled':False,'prompt':'', 'interval_minutes':1})
    assert bad.status_code==409
    with client.app.state.session_factory() as db:
        assert db.get(PrimaryCheckinRun,rid).state=='running'
        service.reconcile(db)

@pytest.mark.asyncio
async def test_shutdown_marks_inflight_interrupted(room_client):
    client,(owner,_,project,_,_)=room_client
    _,_,rid=seed_due(client,owner,project);entered=asyncio.Event()
    async def provider(request):entered.set();await asyncio.Event().wait()
    scheduler=client.app.state.scheduler;scheduler.provider=provider
    task=asyncio.create_task(service.execute(scheduler,rid));await asyncio.wait_for(entered.wait(),2)
    scheduler._closing=True;task.cancel();await task;scheduler._closing=False
    with client.app.state.session_factory() as db:
        row=db.get(PrimaryCheckinRun,rid);assert row.state=='interrupted' and row.result_event_id is None

@pytest.mark.asyncio
@pytest.mark.parametrize('change',['provider','memory','primary'])
async def test_stale_binding_after_provider_cannot_publish(room_client,change):
    from app.models.collaboration import ProviderConnection
    from app.models.bot_profile import BotOwnerProfile
    from app.models.bot_memory import BotMemoryState
    client,(owner,_,project,_,_)=room_client
    agent,room,rid=seed_due(client,owner,project)
    async def provider(request):
        with client.app.state.session_factory() as db:
            if change=='provider':
                row=db.get(ProviderConnection,request.connection_id);row.readiness_revision+=1
            elif change=='primary':
                row=db.get(BotOwnerProfile,owner);row.revision+=1
            else:
                db.add(BotMemoryState(owner_id=owner,agent_id=agent,revision=1))
            db.commit()
        return ProviderResult(text='MUST_NOT_PUBLISH',model_use=model_use(request))
    client.app.state.scheduler.provider=provider
    await service.execute(client.app.state.scheduler,rid)
    with client.app.state.session_factory() as db:
        row=db.get(PrimaryCheckinRun,rid);assert row.state in ('cancelled','failed') and row.result_event_id is None


@pytest.mark.asyncio
async def test_background_context_respects_forget_floor_and_model_identity(room_client):
    client,(owner,_,project,_,_)=room_client
    _,_,rid=seed_due(client,owner,project,history=True);seen=[]
    async def provider(request):
        seen.append(request)
        return ProviderResult(text='MUST_NOT_SAVE',model_use={'model_id':'wrong','connection_id':request.connection_id})
    client.app.state.scheduler.provider=provider
    await service.execute(client.app.state.scheduler,rid)
    assert len(seen)==1 and 'CURRENT_CONVERSATION' in str(seen[0].events)
    assert 'BEFORE_FORGET_BOUNDARY' not in str(seen[0].events) and seen[0].trigger_event_id is not None
    assert not seen[0].tools
    with client.app.state.session_factory() as db:
        row=db.get(PrimaryCheckinRun,rid)
        assert row.state=='failed' and row.result_event_id is None

@pytest.mark.asyncio
async def test_new_owner_request_cancels_only_same_room_checkin(room_client):
    from app.services.events import ActorRef,append_event
    client,(owner,_,project,_,_)=room_client
    agent,room,rid=seed_due(client,owner,project);entered=asyncio.Event()
    async def provider(request):
        if request.server_instruction is not None:
            entered.set();await asyncio.Event().wait()
        return ProviderResult(text='OWNER_REQUEST_REPLY',model_use=model_use(request))
    scheduler=client.app.state.scheduler;scheduler.provider=provider
    task=asyncio.create_task(service.execute(scheduler,rid));await asyncio.wait_for(entered.wait(),2)
    with client.app.state.session_factory() as db:
        event=append_event(db,room,ActorRef('user',owner),'message',{'content':'A new actual owner request'})
        root=event.event_id
    await scheduler.schedule_user_event(room,root)
    await asyncio.wait_for(task,2)
    worker=scheduler._workers.get(room)
    if worker:await asyncio.wait_for(worker,3)
    scheduler._workers.pop(room,None)  # This synthetic worker belongs to the test loop.
    with client.app.state.session_factory() as db:
        assert db.get(PrimaryCheckinRun,rid).state=='cancelled'
        assert db.query(RoomEvent).filter_by(room_id=room,actor_type='agent',kind='message').count()==1

@pytest.mark.asyncio
@pytest.mark.parametrize('action',['disable','replace'])
async def test_owner_api_change_cancels_inflight(room_client,action):
    client,(owner,_,project,_,_)=room_client
    _,room,rid=seed_due(client,owner,project);entered=asyncio.Event();release=asyncio.Event()
    async def provider(request):
        entered.set();await release.wait();return ProviderResult(text='MUST_NOT_SAVE',model_use=model_use(request))
    scheduler=client.app.state.scheduler;scheduler.provider=provider
    task=asyncio.create_task(service.execute(scheduler,rid));await asyncio.wait_for(entered.wait(),2)
    if action=='disable':
        path='/api/v1/bots/primary/check-ins';body={'expected_revision':1,'expected_primary_revision':1,
            'enabled':False,'prompt':'','interval_minutes':1}
    else:path='/api/v1/bots/primary';body={'agent_id':None,'expected_revision':1}
    response=await asyncio.to_thread(client.put,path,headers=headers(client,owner),json=body)
    assert response.status_code==200,response.text
    release.set();await asyncio.wait_for(task,2)
    with client.app.state.session_factory() as db:
        row=db.get(PrimaryCheckinRun,rid);assert row.state=='cancelled' and row.result_event_id is None

@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['go-messages','anthropic','openai','oauth-responses'])
async def test_blank_history_sends_server_instruction_through_adapter(room_client,monkeypatch,kind):
    from app.services import connections
    from app.services.providers.opencode_go import OpenCodeGoMessagesAdapter
    from app.services.providers.chatgpt import ChatGPTAdapter
    from app.services.providers.anthropic import AnthropicAdapter
    from app.services.providers.openai import OpenAIAdapter
    client,(owner,_,project,_,_)=room_client
    _,_,rid=seed_due(client,owner,project);bodies=[]
    adapter={'go-messages':OpenCodeGoMessagesAdapter,'anthropic':AnthropicAdapter,'openai':OpenAIAdapter,'oauth-responses':ChatGPTAdapter}[kind]()
    if kind in ('go-messages','anthropic'):
        monkeypatch.setattr(adapter,'_headers',lambda request:{})
        async def post(request,path,headers,body):
            bodies.append(body)
            return {'content':[{'type':'text','text':'SYNTHETIC_PROVIDER_REPLY'}]}
    elif kind=='openai':
        from app.services import model_features
        async def enforce(_):pass
        monkeypatch.setattr(model_features,'enforce',enforce)
        async def post(request,path,headers,body):
            bodies.append(body)
            return {'choices':[{'message':{'content':'SYNTHETIC_PROVIDER_REPLY'}}]}
    else:
        async def discover(config):return [{'id':config.model_id}],False,None
        monkeypatch.setattr(connections,'discover_models',discover)
        async def post(request,path,headers,body):
            bodies.append(body)
            return {'output':[{'type':'message','content':[{'type':'output_text','text':'SYNTHETIC_PROVIDER_REPLY'}]}]}
    monkeypatch.setattr(adapter,'_post',post)
    async def provider(request):
        request.connection.api_key='synthetic-token'
        return await adapter.generate(request)
    client.app.state.scheduler.provider=provider
    await service.execute(client.app.state.scheduler,rid)
    with client.app.state.session_factory() as db:
        row=db.get(PrimaryCheckinRun,rid);assert row.state=='completed',row.failure_code
    messages=bodies[0]['input'] if kind=='oauth-responses' else bodies[0]['messages']
    assert messages[-1]['role']=='user' and messages[-1]['content'].startswith('SERVER-SCHEDULED CHECK-IN')
    assert 'tools' not in bodies[0]
