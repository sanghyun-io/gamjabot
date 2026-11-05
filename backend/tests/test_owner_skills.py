"""Synthetic Owner skill library and frozen request boundaries."""
import json
from test_room_state import room_client,headers,create_room
from test_bot_creation import private_team
from app.models.collaboration import RoomEvent
from app.services.memory_context import snapshot,require_current
from app.services.providers import ProviderResult
from app.services.llm_service import LLMFailure
import pytest


def make(client,owner,key='skill-one',**overrides):
    body={'name':'Synthetic workflow','description':'A test method','content':'Inspect input then summarize.'}
    body.update(overrides)
    return client.post('/api/v1/skills',headers={**headers(client,owner),'Idempotency-Key':key},json=body)

def test_library_versions_cas_binding_and_stale_context(room_client):
    client,(owner,other,project,_,_)=room_client;own=headers(client,owner)
    agent,room,_=private_team(client,owner,project)
    created=make(client,owner);assert created.status_code==200,created.text
    skill=created.json();sid=skill['skill_id']
    assert make(client,owner).json()==skill
    assert make(client,owner,name='different').status_code==409
    assert client.get('/api/v1/skills/commands/skill-one',headers=own).json()==skill
    assert client.get('/api/v1/skills/commands/skill-one',headers=headers(client,other)).status_code==404
    assert client.get('/api/v1/skills',headers=own,params={'q':'workflow'}).json()['items']==[skill]
    assert client.get(f'/api/v1/skills/{sid}',headers=headers(client,other)).status_code==404
    with client.app.state.session_factory() as db:before=snapshot(db,room,agent)['digest']
    binding=client.put(f'/api/v1/bots/{agent}/skills/{sid}',headers=own,json={'expected_revision':0,'enabled':True})
    assert binding.status_code==200,binding.text
    assert client.put(f'/api/v1/bots/{agent}/skills/{sid}',headers=own,json={'expected_revision':0,'enabled':True}).status_code==409
    assert client.get(f'/api/v1/bots/{agent}/skills',headers=own).json()['items'][0]['enabled']
    with client.app.state.session_factory() as db:
        current=snapshot(db,room,agent);assert 'Inspect input then summarize.' in str(current['memory'])
        with pytest.raises(LLMFailure):require_current(db,room,agent,before)
        admitted=current['digest']
    body={'expected_revision':1,'name':skill['name'],'description':skill['description'],'content':'Updated method','active':True}
    edit=client.patch(f'/api/v1/skills/{sid}',headers=own,json=body);assert edit.status_code==200,edit.text
    assert client.patch(f'/api/v1/skills/{sid}',headers=own,json=body).status_code==409
    assert [x['revision'] for x in client.get(f'/api/v1/skills/{sid}/versions',headers=own).json()['versions']]==[2,1]
    with client.app.state.session_factory() as db:
        with pytest.raises(LLMFailure):require_current(db,room,agent,admitted)
    body.update(expected_revision=2,active=False)
    assert client.patch(f'/api/v1/skills/{sid}',headers=own,json=body).status_code==200
    with client.app.state.session_factory() as db:assert 'Updated method' not in str(snapshot(db,room,agent)['memory'])
    assert client.put(f'/api/v1/bots/{agent}/skills/{sid}',headers=own,json={'expected_revision':1,'enabled':True}).status_code==409


def test_draft_requires_completed_direct_owner_result_and_remains_inactive(room_client):
    client,(owner,other,project,_,_)=room_client;own=headers(client,owner)
    agent,room,_=private_team(client,owner,project)
    async def provider(request):return ProviderResult(text='SYNTHETIC_COMPLETED_RESULT')
    client.app.state.scheduler.provider=provider
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'@Ada Finish the synthetic task'})
    assert response.status_code==201,response.text
    with client.app.state.session_factory() as db:
        root=db.query(RoomEvent).filter_by(room_id=room,actor_type='user',kind='message').order_by(RoomEvent.seq.desc()).first()
        result=db.query(RoomEvent).filter_by(room_id=room,actor_type='agent',kind='message').order_by(RoomEvent.seq.desc()).first()
        body={'room_id':room,'agent_id':agent,'root_event_id':root.event_id,'result_event_id':result.event_id,
              'name':'Draft','description':'Owner-authored draft','content':'Review then validate.'}
    value=client.post('/api/v1/skills/from-request',headers={**own,'Idempotency-Key':'draft-one'},json=body)
    assert value.status_code==200,value.text
    draft=value.json();assert draft['active'] is False and draft['source']['result_event_id']==body['result_event_id']
    assert draft['content']=='Review then validate.' and 'SYNTHETIC_COMPLETED_RESULT' not in json.dumps(draft)
    assert client.post('/api/v1/skills/from-request',headers={**headers(client,other),'Idempotency-Key':'other-draft'},json=body).status_code in (404,409)
    assert client.post('/api/v1/skills/from-request',headers={**own,'Idempotency-Key':'bad-result'},json={**body,'result_event_id':body['root_event_id']}).status_code==409
    assert client.put(f"/api/v1/bots/{agent}/skills/{draft['skill_id']}",headers=own,json={'expected_revision':0,'enabled':True}).status_code==409


def test_skill_edit_during_provider_await_blocks_stale_answer(room_client):
    from app.schemas.owner_skills import SkillEdit
    from app.services.owner_skills import edit
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner)
    agent,room,_=private_team(client,owner,project);skill=make(client,owner).json();sid=skill['skill_id']
    assert client.put(f'/api/v1/bots/{agent}/skills/{sid}',headers=own,json={'expected_revision':0,'enabled':True}).status_code==200
    async def provider(request):
        assert 'Inspect input then summarize.' in request.memory
        with client.app.state.session_factory() as db:
            edit(db,owner,sid,SkillEdit(expected_revision=1,name=skill['name'],description=skill['description'],content='New guidance',active=True))
        return ProviderResult(text='STALE_SKILL_ANSWER')
    client.app.state.scheduler.provider=provider
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'Use the synthetic workflow'})
    assert response.status_code==502,response.text
    with client.app.state.session_factory() as db:
        assert not db.query(RoomEvent).filter_by(room_id=room,kind='message',actor_type='agent').all()
        failure=db.query(RoomEvent).filter_by(room_id=room,kind='failure').one()
        assert failure.payload['code']=='memory_context_changed'


def test_delegated_bot_receives_skill_without_private_memory(room_client):
    from test_bot_creation import tool
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner)
    source,room,_=private_team(client,owner,project);target,_,_=private_team(client,owner,project)
    skill=make(client,owner,content='PRIVATE_SKILL_SENTINEL').json()
    assert client.post(f'/api/v1/bots/{target}/memories',headers={**own,'Idempotency-Key':'private-target'},json={'expected_scope_revision':0,'category':'fact','content':'PRIVATE_MEMORY_SENTINEL'}).status_code==200
    assert client.put(f"/api/v1/bots/{target}/skills/{skill['skill_id']}",headers=own,json={'expected_revision':0,'enabled':True}).status_code==200
    seen=[]
    async def provider(request):
        if request.agent_id==target:
            seen.append(request);assert 'PRIVATE_SKILL_SENTINEL' in str(request.memory)
            assert 'PRIVATE_MEMORY_SENTINEL' not in str(request.memory)
            assert 'PRIVATE_MEMORY_SENTINEL' not in str(request.events)
            return ProviderResult(text='SYNTHETIC_DELEGATED_REPLY')
        if 'SYNTHETIC_DELEGATED_REPLY' in (request.system+json.dumps(request.events)):
            return ProviderResult(text='Final synthesis')
        if request.observations:return ProviderResult(text='Waiting for result')
        return tool('message_bot',{'agent_id':target,'content':'Independent synthetic task'},'delegate')
    client.app.state.scheduler.provider=provider
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'Ask the other Bot to review.'})
    assert response.status_code==201,response.text
    assert len(seen)==1


def test_enabling_skill_respects_existing_memory_budget_and_rolls_back(room_client):
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner)
    agent,room,_=private_team(client,owner,project)
    item=client.post(f'/api/v1/bots/{agent}/memories',headers={**own,'Idempotency-Key':'large-memory'},json={
        'expected_scope_revision':0,'category':'fact','content':'x'*1900})
    assert item.status_code==200,item.text
    skill=make(client,owner,content='y'*6100).json()
    binding=client.put(f"/api/v1/bots/{agent}/skills/{skill['skill_id']}",headers=own,json={'expected_revision':0,'enabled':True})
    assert binding.status_code==409 and binding.json()['detail']['code']=='skill_context_limit'
    assert client.get(f'/api/v1/bots/{agent}/skills',headers=own).json()['items'][0]['revision']==0


def test_legacy_bot_uses_enabled_skill_in_owner_room_without_private_memory(room_client):
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner)
    bot,_,_=private_team(client,owner,project)
    group=create_room(client,owner,project,'Owner group')
    assert client.post(f'/api/v1/conversations/{group}/members/{bot}',headers=own).status_code==201
    assert client.post(f'/api/v1/bots/{bot}/memories',headers={**own,'Idempotency-Key':'legacy-fact'},json={
        'expected_scope_revision':0,'category':'fact','content':'LEGACY_PRIVATE_SENTINEL'}).status_code==200
    skill=make(client,owner,content='OWNER_SKILL_SENTINEL').json()
    assert client.put(f"/api/v1/bots/{bot}/skills/{skill['skill_id']}",headers=own,json={'expected_revision':0,'enabled':True}).status_code==200
    with client.app.state.session_factory() as db:
        projection=snapshot(db,group,bot)
        assert 'OWNER_SKILL_SENTINEL' in str(projection['memory'])
        assert 'LEGACY_PRIVATE_SENTINEL' not in str(projection['memory'])
        assert projection['binding']['bot_memory_policy']['mode']=='canonical_only'


def test_delegated_skill_revoke_during_provider_await_blocks_stale_answer(room_client):
    from test_bot_creation import tool
    from app.schemas.owner_skills import SkillEnable
    from app.services.owner_skills import enable
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner)
    source,room,_=private_team(client,owner,project);target,target_room,_=private_team(client,owner,project)
    skill=make(client,owner,content='SYNTHETIC_SKILL').json();sid=skill['skill_id']
    assert client.put(f'/api/v1/bots/{target}/skills/{sid}',headers=own,json={'expected_revision':0,'enabled':True}).status_code==200
    async def provider(request):
        if request.agent_id==target:
            assert 'SYNTHETIC_SKILL' in str(request.memory)
            with client.app.state.session_factory() as db:
                enable(db,owner,target,sid,SkillEnable(expected_revision=1,enabled=False))
            return ProviderResult(text='STALE_DELEGATED_ANSWER')
        if request.observations:return ProviderResult(text='Waiting for result')
        return tool('message_bot',{'agent_id':target,'content':'Independent task'},'delegation')
    client.app.state.scheduler.provider=provider
    client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'Ask the other Bot to review.'})
    with client.app.state.session_factory() as db:
        messages=db.query(RoomEvent).filter_by(room_id=target_room,actor_type='agent',kind='message').all()
        assert not any('STALE_DELEGATED_ANSWER' in str(event.payload) for event in messages)
        failure=db.query(RoomEvent).filter_by(room_id=target_room,actor_type='agent',kind='failure').all()
        assert any(event.payload.get('code')=='memory_context_changed' for event in failure)
