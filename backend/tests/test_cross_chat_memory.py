import json
from test_room_state import room_client,headers,create_room
from test_bot_creation import private_team,tool
from app.services.memory_context import snapshot
from app.services.providers import ProviderResult
from test_review_preview import preview_fixture


def test_policy_group_exclusion_common_owner_boundary_and_forget(room_client):
    client,(owner,other,project,_,_)=room_client;own=headers(client,owner)
    agent,dm,_=private_team(client,owner,project)
    peer,peer_dm,_=private_team(client,owner,project)
    group=create_room(client,owner,project,'Group')
    for aid in (agent,peer):assert client.post(f'/api/v1/conversations/{group}/members/{aid}',headers=own).status_code==201
    added=client.post(f'/api/v1/bots/{agent}/memories',headers={**own,'Idempotency-Key':'private-one'},json={
        'expected_scope_revision':0,'category':'fact','content':'PRIVATE_SYNTHETIC'}).json()
    with client.app.state.session_factory() as db:
        assert 'PRIVATE_SYNTHETIC' not in str(snapshot(db,group,agent)['memory'])
    policy=client.put(f'/api/v1/bots/{agent}/memory-policy',headers=own,json={'expected_revision':0,'mode':'owner_rooms'})
    assert policy.status_code==200,policy.text
    with client.app.state.session_factory() as db:
        assert 'PRIVATE_SYNTHETIC' in str(snapshot(db,group,agent)['memory'])
        assert 'PRIVATE_SYNTHETIC' not in str(snapshot(db,group,peer)['memory'])
    hidden=client.patch(f"/api/v1/bots/{agent}/memories/{added['memory_id']}/visibility",headers={**own,'Idempotency-Key':'hide'},json={
        'expected_scope_revision':1,'expected_revision':1,'group_allowed':False})
    assert hidden.status_code==200,hidden.text
    with client.app.state.session_factory() as db:
        assert 'PRIVATE_SYNTHETIC' not in str(snapshot(db,group,agent)['memory'])
        assert 'PRIVATE_SYNTHETIC' in str(snapshot(db,dm,agent)['memory'])
    res=client.post('/api/v1/bots/memory/common',headers={**own,'Idempotency-Key':'common-add'},json={
        'expected_scope_revision':0,'kind':'timezone','content':'Etc/UTC','origin':{'agent_id':agent,'source_kind':'manual'}})
    assert res.status_code==200,res.text
    item=res.json();assert item['origin']['source_kind']=='manual' and item['root_ref'] is None
    with client.app.state.session_factory() as db:
        assert 'Etc/UTC' in str(snapshot(db,peer_dm,peer)['memory'])
    assert client.get('/api/v1/bots/memory/common',headers=headers(client,other)).json()['items']==[]
    removed=client.request('DELETE',f"/api/v1/bots/memory/common/{item['fact_id']}",headers={**own,'Idempotency-Key':'common-delete'},json={'expected_scope_revision':1,'expected_revision':1})
    assert removed.status_code==200,removed.text
    with client.app.state.session_factory() as db:
        assert 'Etc/UTC' not in str(snapshot(db,peer_dm,peer)['memory'])


def test_natural_common_and_group_memory_direct_owner_root(room_client):
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner)
    agent,dm,_=private_team(client,owner,project);group=create_room(client,owner,project,'Group')
    assert client.post(f'/api/v1/conversations/{group}/members/{agent}',headers=own).status_code==201
    assert client.put(f'/api/v1/bots/{agent}/memory-policy',headers=own,json={'expected_revision':0,'mode':'owner_rooms'}).status_code==200
    calls=[]
    async def provider(request):
        if not request.observations:
            names={t['name'] for t in request.tools}
            assert 'save_owner_fact' in names and 'save_bot_memory' in names and 'change_bot_role' not in names
            return tool('save_owner_fact',{'kind':'name','content':'SYNTHETIC_NAME'},'fact')
        value=json.loads(request.observations[-1].text);calls.append(value)
        return ProviderResult(text='Saved as an Owner-shared fact.')
    client.app.state.scheduler.provider=provider
    response=client.post(f'/api/v1/conversations/{group}/messages',headers=own,json={'content':'Remember my name across my Bots.'})
    assert response.status_code==201,response.text
    assert len(calls)==1 and calls[0]['scope']=='owner_common' and calls[0]['origin']['source_kind']=='event'
    assert calls[0]['root_ref']['actor_type']=='user' and calls[0]['result_ref']['actor_type']=='system'
    listing=client.get('/api/v1/bots/memory/common',headers=own).json()
    assert listing['items'][0]['content']=='SYNTHETIC_NAME'


def test_change_during_provider_await_rejects_stale_answer(room_client):
    from app.schemas.owner_common_memory import MemoryPolicyWrite
    from app.services.memory_policy import set_policy
    from app.models.collaboration import RoomEvent
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner)
    agent,room,_=private_team(client,owner,project)
    async def provider(request):
        with client.app.state.session_factory() as db:
            set_policy(db,owner,agent,MemoryPolicyWrite(expected_revision=0,mode='owner_rooms'))
        return ProviderResult(text='STALE_PRIVATE_RESPONSE')
    client.app.state.scheduler.provider=provider
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'Synthetic request'})
    assert response.status_code==502,response.text
    with client.app.state.session_factory() as db:
        assert not db.query(RoomEvent).filter_by(room_id=room,kind='message',actor_type='agent').all()
        failure=db.query(RoomEvent).filter_by(room_id=room,kind='failure').one()
        assert failure.payload['code']=='memory_context_changed'


def test_natural_scope_visibility_and_shared_fact_change(room_client):
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner)
    agent,room,_=private_team(client,owner,project);receipts=[]
    async def provider(request):
        if not request.observations:return tool('set_bot_memory_scope',{'mode':'owner_rooms'},'scope')
        value=json.loads(request.observations[-1].text);receipts.append(value)
        if len(receipts)==1:return tool('save_bot_memory',{'content':'SYNTHETIC_PRIVATE'},'memory')
        if len(receipts)==2:return tool('set_memory_group_access',{'memory_id':value['memory_id'],'group_allowed':False},'hide')
        if len(receipts)==3:return tool('save_owner_fact',{'kind':'timezone','content':'Etc/UTC'},'common')
        if len(receipts)==4:return tool('edit_owner_fact',{'fact_id':value['memory_id'],'content':'Asia/Seoul'},'edit')
        if len(receipts)==5:return tool('forget_owner_fact',{'fact_id':value['memory_id']},'forget')
        return ProviderResult(text='Saved the shared and private settings.')
    client.app.state.scheduler.provider=provider
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'Set my synthetic memory scope and shared timezone, then forget that timezone.'})
    assert response.status_code==201,response.text
    assert [r['operation'] for r in receipts]==['policy','add','visibility','add','edit','delete']
    assert receipts[3]['scope']=='owner_common' and receipts[3]['root_ref']['actor_type']=='user'
    assert receipts[2]['content']=='' and receipts[2]['group_allowed'] is False
    assert client.get(f'/api/v1/bots/{agent}/memory-policy',headers=own).json()['mode']=='owner_rooms'
    assert client.get('/api/v1/bots/memory/common',headers=own).json()['items']==[]


def test_delegated_bot_cannot_read_shared_owner_or_private_memory(room_client):
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner)
    source,room,_=private_team(client,owner,project);target,target_room,_=private_team(client,owner,project)
    assert client.post('/api/v1/bots/memory/common',headers={**own,'Idempotency-Key':'common-shared'},json={
        'expected_scope_revision':0,'kind':'name','content':'OWNER_PRIVATE_SYNTHETIC','origin':{'agent_id':source,'source_kind':'manual'}}).status_code==200
    assert client.post(f'/api/v1/bots/{target}/memories',headers={**own,'Idempotency-Key':'target-private'},json={
        'expected_scope_revision':0,'category':'fact','content':'TARGET_PRIVATE_SYNTHETIC'}).status_code==200
    received=[]
    async def provider(request):
        if request.agent_id==target:
            received.append(request)
            assert 'OWNER_PRIVATE_SYNTHETIC' not in str(request.memory)
            assert 'TARGET_PRIVATE_SYNTHETIC' not in str(request.memory)
            assert 'save_owner_fact' not in {t['name'] for t in request.tools}
            return ProviderResult(text='REAL_DELEGATED_REPLY')
        if 'REAL_DELEGATED_REPLY' in (request.system+json.dumps(request.events)):
            return ProviderResult(text='Final synthesis')
        if request.observations:return ProviderResult(text='Waiting for the actual reply.')
        return tool('message_bot',{'agent_id':target,'content':'Synthetic independent task'},'delegate')
    client.app.state.scheduler.provider=provider
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'Ask the other Bot to review this task.'})
    assert response.status_code==201,response.text
    assert len(received)==1


def test_tool_review_rejects_changed_memory_binding(room_client):
    from types import SimpleNamespace
    from app.services.tool_broker import ToolBroker
    from app.services.action_review import ReviewChanged
    from app.services.memory_policy import set_policy
    from app.schemas.owner_common_memory import MemoryPolicyWrite
    import pytest
    client,(owner,_,project,_,_)=room_client
    agent,room,_=private_team(client,owner,project);broker=ToolBroker(client.app.state.session_factory)
    action=SimpleNamespace(action_id='synthetic-action',room_id=room,agent_id=agent)
    with client.app.state.session_factory() as db:
        old=snapshot(db,room,agent)['digest'];broker.bind_memory(action.action_id,old,None)
        set_policy(db,owner,agent,MemoryPolicyWrite(expected_revision=0,mode='owner_rooms'))
        with pytest.raises(ReviewChanged):broker._require_memory_binding(db,action)


def test_deleting_origin_bot_removes_its_shared_facts(room_client):
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner)
    agent,_,_=private_team(client,owner,project)
    added=client.post('/api/v1/bots/memory/common',headers={**own,'Idempotency-Key':'origin-fact'},json={
        'expected_scope_revision':0,'kind':'name','content':'SYNTHETIC','origin':{'agent_id':agent,'source_kind':'manual'}})
    assert added.status_code==200,added.text
    assert client.delete(f'/api/v1/agents/{agent}',headers=own).status_code==204
    remaining=client.get('/api/v1/bots/memory/common',headers=own).json()
    assert remaining['items']==[] and remaining['revision']==2


def test_other_owned_room_can_save_private_memory_but_not_change_role(room_client):
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner)
    agent,dm,_=private_team(client,owner,project);second=create_room(client,owner,project,'Second')
    assert client.post(f'/api/v1/conversations/{second}/members/{agent}',headers=own).status_code==201
    assert client.put(f'/api/v1/bots/{agent}/memory-policy',headers=own,json={'expected_revision':0,'mode':'owner_rooms'}).status_code==200
    receipts=[]
    async def provider(request):
        names={t['name'] for t in request.tools}
        assert 'save_bot_memory' in names and 'change_bot_role' not in names and 'create_bot' not in names
        if request.observations:
            receipts.append(json.loads(request.observations[-1].text));return ProviderResult(text='Saved.')
        return tool('save_bot_memory',{'content':'OTHER_ROOM_FACT'},'save')
    client.app.state.scheduler.provider=provider
    result=client.post(f'/api/v1/conversations/{second}/messages',headers=own,json={'content':'Remember this fact.'})
    assert result.status_code==201,result.text
    assert len(receipts)==1 and receipts[0]['root_ref']['room_id']==second
    with client.app.state.session_factory() as db:assert 'OTHER_ROOM_FACT' in str(snapshot(db,dm,agent)['memory'])


def test_group_exclusion_survives_single_bot_audience(room_client):
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner)
    agent,_,_=private_team(client,owner,project);group=create_room(client,owner,project,'Expandable group')
    assert client.post(f'/api/v1/conversations/{group}/members/{agent}',headers=own).status_code==201
    entry=client.post(f'/api/v1/bots/{agent}/memories',headers={**own,'Idempotency-Key':'g-private'},json={
        'expected_scope_revision':0,'category':'fact','content':'GROUP_EXCLUDED_SECRET'}).json()
    assert client.put(f'/api/v1/bots/{agent}/memory-policy',headers=own,json={'expected_revision':0,'mode':'owner_rooms'}).status_code==200
    assert client.patch(f"/api/v1/bots/{agent}/memories/{entry['memory_id']}/visibility",headers={**own,'Idempotency-Key':'exclude'},json={
        'expected_scope_revision':1,'expected_revision':1,'group_allowed':False}).status_code==200
    with client.app.state.session_factory() as db:
        assert 'GROUP_EXCLUDED_SECRET' not in str(snapshot(db,group,agent)['memory'])


def test_approved_native_action_does_not_start_after_policy_change(preview_fixture):
    from test_review_claim import inspected,locked_claim
    from app.services.action_review import ReviewChanged
    from app.services.memory_policy import set_policy
    from app.schemas.owner_common_memory import MemoryPolicyWrite
    import pytest
    client,owner,broker,path,proposal=preview_fixture
    action=proposal()
    with broker.sessions() as db:
        old=snapshot(db,action.room_id,action.agent_id)['digest']
    broker.bind_memory(action.action_id,old,None)
    inspected(client,owner,broker,action)
    with broker.sessions() as db:
        set_policy(db,owner,action.agent_id,MemoryPolicyWrite(expected_revision=0,mode='owner_rooms'))
    with pytest.raises(ReviewChanged):locked_claim(broker,action)
    assert path.read_bytes()==b'AAAA'


import pytest
@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['go','oauth'])
@pytest.mark.parametrize('family,args',[
    ('save_owner_fact',{'kind':'name','content':'SYNTHETIC'}),
    ('set_bot_memory_scope',{'mode':'owner_rooms'}),
])
async def test_common_and_policy_offered_through_provider_adapters(monkeypatch,kind,family,args):
    from app.services.connections import ConnectionConfig
    from app.services.providers.base import ProviderRequest,wire_tool_name
    from app.services.providers.opencode_go import OpenCodeGoMessagesAdapter
    from app.services.providers.chatgpt import ChatGPTAdapter
    from app.services.bot_persistence import EXTENDED_SCHEMAS
    from app.services import connections
    base='https://api.openai.com/v1';model='synthetic-model'
    caps={'verified':{'model_id':model,'base_url':base,'tools':True},'model_features':{model:{
        'model_id':model,'base_url':base,'config_revision':1,'tested':{'text_generation':True,'tool_calling':True},'tool_families':{}}}}
    cfg=ConnectionConfig(1,'chatgpt' if kind=='oauth' else 'opencode-go',model,base,'synthetic-token',caps,
        provider_id='chatgpt' if kind=='oauth' else 'opencode-go',
        protocol_id='openai_responses' if kind=='oauth' else 'anthropic_messages',config_revision=1)
    req=ProviderRequest(connection=cfg,model_id=model,room_id=1,agent_id=1,tools=EXTENDED_SCHEMAS)
    wire=wire_tool_name(family);adapter=ChatGPTAdapter() if kind=='oauth' else OpenCodeGoMessagesAdapter()
    if kind=='oauth':
        async def discover(_):return [{'id':model}],False,None
        monkeypatch.setattr(connections,'discover_models',discover)
        async def post(request,path,headers,body):
            assert wire in {t['name'] for t in body['tools'][0]['tools']}
            return {'output':[{'type':'function_call','namespace':'gamja','call_id':'call','name':wire,'arguments':json.dumps(args)}]}
    else:
        monkeypatch.setattr(adapter,'_headers',lambda _: {})
        async def post(request,path,headers,body):
            assert wire in {t['name'] for t in body['tools']}
            return {'content':[{'type':'tool_use','id':'call','name':wire,'input':args}]}
    monkeypatch.setattr(adapter,'_post',post)
    result=await adapter.generate(req)
    assert result.tool_calls[0].family==family and result.tool_calls[0].arguments==args


def test_forget_without_canonical_room_advances_other_room_floor(room_client):
    from test_teammates import team
    from app.services.events import append_event,ActorRef
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner)
    room=create_room(client,owner,project,'Noncanonical');agent,=team(client,owner,room,('Standalone',))
    assert client.put(f'/api/v1/bots/{agent}/memory-policy',headers=own,json={'expected_revision':0,'mode':'owner_rooms'}).status_code==200
    item=client.post(f'/api/v1/bots/{agent}/memories',headers={**own,'Idempotency-Key':'orphan-add'},json={
        'expected_scope_revision':0,'category':'fact','content':'OLD_FACT'}).json()
    with client.app.state.session_factory() as db:
        old=append_event(db,room,ActorRef('user',owner),'message',{'content':'OLD_FACT'});old_seq=old.seq
    response=client.request('DELETE',f"/api/v1/bots/{agent}/memories/{item['memory_id']}",
        headers={**own,'Idempotency-Key':'orphan-delete'},json={'expected_scope_revision':1,'expected_revision':1})
    assert response.status_code==200,response.text
    with client.app.state.session_factory() as db:
        current=snapshot(db,room,agent)
        assert current['binding']['context_after_seq']>=old_seq and 'OLD_FACT' not in str(current['memory'])


def test_common_forget_advances_each_bot_history_and_replay_is_bounded(room_client):
    from app.services.events import append_event, ActorRef
    client, (owner, _, project, _, _) = room_client
    own = headers(client, owner)
    agent, dm, _ = private_team(client, owner, project)
    peer, peer_dm, _ = private_team(client, owner, project)
    body = {'expected_scope_revision': 0, 'kind': 'name', 'content': 'Synthetic Name',
            'origin': {'agent_id': agent, 'source_kind': 'manual'}}
    key = {**own, 'Idempotency-Key': 'common-replay'}
    first = client.post('/api/v1/bots/memory/common', headers=key, json=body)
    assert first.status_code == 200, first.text
    replay = client.post('/api/v1/bots/memory/common', headers=key, json=body)
    assert replay.status_code == 200 and replay.json()['fact_id'] == first.json()['fact_id']
    assert replay.json()['replayed'] is True
    assert client.post('/api/v1/bots/memory/common', headers=key, json={**body, 'content': 'Different'}).status_code == 409
    assert client.post('/api/v1/bots/memory/common', headers={**own, 'Idempotency-Key': 'stale-common'}, json=body).status_code == 409
    with client.app.state.session_factory() as db:
        seqs = [append_event(db, room, ActorRef('user', owner), 'message', {'content': 'Synthetic Name'}).seq for room in (dm, peer_dm)]
    removed = client.request('DELETE', f"/api/v1/bots/memory/common/{first.json()['fact_id']}",
        headers={**own, 'Idempotency-Key': 'common-forget'}, json={'expected_scope_revision': 1, 'expected_revision': 1})
    assert removed.status_code == 200, removed.text
    with client.app.state.session_factory() as db:
        for room, bot, seq in zip((dm, peer_dm), (agent, peer), seqs):
            current = snapshot(db, room, bot)
            assert current['binding']['context_after_seq'] >= seq
            assert 'Synthetic Name' not in str(current['memory'])


def test_collaboration_and_creation_recheck_memory_under_effect_lock(room_client):
    import pytest
    from test_bot_creation import seed_permit, ARGS
    from app.services import bot_creation, bot_messages, bot_batches, teammates
    from app.services.llm_service import LLMFailure
    from app.models.bot_creation import BotCreation
    from app.models.bot_message import BotMessage
    from app.models.bot_batch import BotBatch
    from app.models.teammate import TeammateHandoff
    client, (owner, _, project, _, _) = room_client
    own = headers(client, owner)
    sender, room, _ = private_team(client, owner, project)
    recipient, _, _ = private_team(client, owner, project)
    another, _, _ = private_team(client, owner, project)
    permit = seed_permit(client, owner, room, sender)
    with client.app.state.session_factory() as db:
        admitted = snapshot(db, room, sender)['digest']
    changed = client.put(f'/api/v1/bots/{sender}/memory-policy', headers=own,
        json={'expected_revision': 0, 'mode': 'owner_rooms'})
    assert changed.status_code == 200, changed.text
    effects = [
        lambda db: bot_creation.create(db, permit, 'stale-create', ARGS, provider_calls=1, expected_memory_digest=admitted),
        lambda db: bot_messages.send(db, room, room, permit.root_id, sender, permit.generation, 'stale-message',
            {'agent_id': recipient, 'content': 'old data'}, expected_memory_digest=admitted),
        lambda db: bot_batches.send(db, room, room, permit.root_id, sender, permit.generation, 'stale-batch',
            {'tasks': [{'agent_id': recipient, 'content': 'old data'}, {'agent_id': another, 'content': 'old data'}]},
            expected_memory_digest=admitted),
        lambda db: teammates.send(db, room, permit.root_id, sender, permit.generation, 'stale-team',
            {'agent_id': recipient, 'content': 'old data'}, expected_memory_digest=admitted),
    ]
    for effect in effects:
        with client.app.state.session_factory() as db:
            with pytest.raises(LLMFailure) as exc:
                effect(db)
            assert exc.value.code == 'memory_context_changed'
    with client.app.state.session_factory() as db:
        assert all(db.query(model).count() == 0 for model in (BotCreation, BotMessage, BotBatch, TeammateHandoff))


def test_own_memory_change_then_delegation_uses_advanced_digest(room_client):
    client,(owner,_,project,_,_)=room_client
    sender,room,_=private_team(client,owner,project)
    recipient,_,_=private_team(client,owner,project)
    calls=[]
    async def provider(request):
        if request.agent_id==recipient:
            return ProviderResult(text='SYNTHETIC_COLLEAGUE_RESULT')
        calls.append(1)
        if len(calls)==1:
            return tool('save_owner_fact',{'kind':'name','content':'Synthetic Name'},'save')
        if len(calls)==2:
            return tool('message_bot',{'agent_id':recipient,'content':'Review the task'},'send')
        return ProviderResult(text='SYNTHETIC_FINAL')
    client.app.state.scheduler.provider=provider
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=headers(client,owner),json={'content':'Remember my synthetic name and ask the other Bot to review.'})
    assert response.status_code==201,response.text
    from app.models.bot_message import BotMessage
    with client.app.state.session_factory() as db:
        assert db.query(BotMessage).filter_by(sender_agent_id=sender,recipient_agent_id=recipient).count()==1
