"""Synthetic conversational Skill tools, provenance and receipts."""
import json
import pytest
from test_room_state import room_client,headers
from test_bot_creation import private_team,tool
from app.models.collaboration import RoomEvent
from app.services.providers import ProviderResult
from app.services.skill_persistence import TOOL_SCHEMAS
from app.services.providers.base import wire_tool_name,normalized_call,ProviderFailure


def catalog(request):
    marker='{"completed_task_refs":'
    pos=request.system.rfind(marker)
    assert pos>=0
    return json.loads(request.system[pos:])

def test_direct_owner_create_draft_then_edit_active_skill_with_receipt(room_client):
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner)
    bot,room,_=private_team(client,owner,project)
    async def initial(request):return ProviderResult(text='COMPLETED_SYNTHETIC_METHOD')
    scheduler=client.app.state.scheduler;scheduler.provider=initial
    first=client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'@Ada complete the synthetic workflow'})
    assert first.status_code==201,first.text
    seen=[]
    async def save(request):
        if request.observations:
            seen.append(json.loads(request.observations[-1].text));return ProviderResult(text='Draft created.')
        refs=catalog(request)['completed_task_refs'];assert refs
        return tool('save_owner_skill',{'source_result_seq':refs[0]['result_seq'],'mode':'draft','name':'Synthetic method',
            'description':'A reusable method','content':'Inspect, validate, and report; ask approval for external actions.'},'save-skill')
    scheduler.provider=save
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'@Ada save the completed method as a Skill draft'})
    assert response.status_code==201,response.text
    receipt=seen[0];sid=receipt['skill_id'];assert receipt['operation']=='create_draft' and receipt['active'] is False
    stored=client.get(f'/api/v1/skills/{sid}',headers=own).json()
    assert stored['source']==receipt['source'] and stored['revision']==1 and stored['content']==receipt['content']
    persisted=client.get(f"/api/v1/bots/{bot}/skill-persistence/{receipt['receipt_id']}",headers=own).json()
    assert persisted['result_ref']==receipt['result_ref'] and persisted['root_ref']==receipt['root_ref']
    with client.app.state.session_factory() as db:
        event=db.get(RoomEvent,receipt['result_ref']['event_id']);assert event.actor_type=='system' and event.payload['category']=='skill_persistence'
    assert client.get(f"/api/v1/bots/{bot}/skill-persistence/{receipt['receipt_id']}",headers=headers(client,2)).status_code==404
    patch={'expected_revision':1,'name':stored['name'],'description':stored['description'],'content':stored['content'],'active':True}
    assert client.patch(f'/api/v1/skills/{sid}',headers=own,json=patch).status_code==200
    assert client.put(f'/api/v1/bots/{bot}/skills/{sid}',headers=own,json={'expected_revision':0,'enabled':True}).status_code==200
    edits=[]
    async def edit(request):
        if request.observations:
            edits.append(json.loads(request.observations[-1].text));return ProviderResult(text='Skill corrected.')
        refs=catalog(request)['owner_skill_catalog'];target=next(x for x in refs if x['skill_id']==sid)
        return tool('edit_owner_skill',{'skill_id':sid,'revision':target['revision'],'name':target['name'],
            'description':'Updated','content':'Check inputs twice before reporting.'},'edit-skill')
    scheduler.provider=edit
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'@Ada update my synthetic method'})
    assert response.status_code==201,response.text
    assert edits[0]['revision']==3 and edits[0]['active'] is True
    stored=client.get(f'/api/v1/skills/{sid}',headers=own).json()
    assert stored['source']==receipt['source'] and stored['content']=='Check inputs twice before reporting.'
    assert [v['revision'] for v in client.get(f'/api/v1/skills/{sid}/versions',headers=own).json()['versions']]==[3,2,1]


def test_plain_wire_names_accept_only_offered_schema_and_no_alias():
    with pytest.raises(ProviderFailure):normalized_call('id','save_skill_draft',{},TOOL_SCHEMAS)
    for entry in TOOL_SCHEMAS:
        name=entry['name'];assert wire_tool_name(name)==name
        with pytest.raises(ProviderFailure):normalized_call('id',name,{},[])
        with pytest.raises(ProviderFailure):normalized_call('id','gamja_'+name.encode().hex(),{},TOOL_SCHEMAS)


def test_unoffered_source_and_stale_skill_revision_have_no_receipt(room_client):
    from app.models.owner_skills import OwnerSkill
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner)
    bot,room,_=private_team(client,owner,project)
    created=client.post('/api/v1/skills',headers={**own,'Idempotency-Key':'existing'},json={
        'name':'Existing','description':'','content':'Original'}).json()
    async def invalid_source(request):
        return tool('save_owner_skill',{'source_result_seq':999,'mode':'for_this_bot','name':'Forged','description':'','content':'Forged'},'invalid')
    scheduler=client.app.state.scheduler;scheduler.provider=invalid_source
    assert client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'@Ada create a draft'}).status_code==502
    async def stale_edit(request):
        return tool('edit_owner_skill',{'skill_id':created['skill_id'],'revision':2,'name':'Changed','description':'','content':'Changed'},'stale')
    scheduler.provider=stale_edit
    assert client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'@Ada edit the Skill'}).status_code==502
    with client.app.state.session_factory() as db:
        assert db.query(OwnerSkill).filter_by(owner_id=owner).count()==1
        assert db.get(OwnerSkill,created['skill_id']).content=='Original'
        assert not [e for e in db.query(RoomEvent).filter_by(room_id=room,kind='activity').all() if e.payload.get('category')=='skill_persistence']


def test_stopped_generation_cannot_save_skill(room_client):
    from app.models.owner_skills import OwnerSkill
    from app.services.room_service import cancel_room_work
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner)
    bot,room,_=private_team(client,owner,project)
    async def provider(request):
        with client.app.state.session_factory() as db:cancel_room_work(db,room)
        return tool('save_owner_skill',{'source_result_seq':1,'mode':'for_this_bot','name':'Not saved','description':'','content':'Not saved'},'stopped')
    client.app.state.scheduler.provider=provider
    client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'@Ada save this as a draft'})
    with client.app.state.session_factory() as db:
        assert db.query(OwnerSkill).filter_by(owner_id=owner).count()==0
        assert not [e for e in db.query(RoomEvent).filter_by(room_id=room,kind='activity').all() if e.payload.get('category')=='skill_persistence']


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['go','oauth'])
@pytest.mark.parametrize('family,args',[
    ('save_owner_skill',{'source_result_seq':2,'mode':'for_this_bot','name':'Sample','description':'','content':'Synthetic method'}),
    ('edit_owner_skill',{'skill_id':'00000000-0000-4000-8000-000000000001','revision':1,'name':'Sample','description':'','content':'Synthetic edit'}),
])
async def test_direct_provider_wire_shapes(monkeypatch,kind,family,args):
    from app.services.connections import ConnectionConfig
    from app.services.providers.base import ProviderRequest
    from app.services.providers.opencode_go import OpenCodeGoMessagesAdapter
    from app.services.providers.chatgpt import ChatGPTAdapter
    from app.services import connections
    base='https://api.openai.com/v1';model='synthetic-model'
    caps={'verified':{'model_id':model,'base_url':base,'tools':True},'model_features':{model:{
        'model_id':model,'base_url':base,'config_revision':1,'tested':{'text_generation':True,'tool_calling':True},'tool_families':{}}}}
    cfg=ConnectionConfig(1,'chatgpt' if kind=='oauth' else 'opencode-go',model,base,'synthetic-token',caps,
        provider_id='chatgpt' if kind=='oauth' else 'opencode-go',protocol_id='openai_responses' if kind=='oauth' else 'anthropic_messages',config_revision=1)
    request=ProviderRequest(connection=cfg,model_id=model,room_id=1,agent_id=1,tools=TOOL_SCHEMAS)
    adapter=ChatGPTAdapter() if kind=='oauth' else OpenCodeGoMessagesAdapter()
    if kind=='oauth':
        async def discover(_):return [{'id':model}],False,None
        monkeypatch.setattr(connections,'discover_models',discover)
        async def post(request,path,headers,body):
            assert family in {v['name'] for v in body['tools'][0]['tools']}
            return {'output':[{'type':'function_call','namespace':'gamja','call_id':'call','name':family,'arguments':json.dumps(args)}]}
    else:
        monkeypatch.setattr(adapter,'_headers',lambda _: {})
        async def post(request,path,headers,body):
            assert family in {v['name'] for v in body['tools']}
            return {'content':[{'type':'tool_use','id':'call','name':family,'input':args}]}
    monkeypatch.setattr(adapter,'_post',post)
    result=await adapter.generate(request)
    assert result.tool_calls[0].family==family and result.tool_calls[0].arguments==args


def test_legacy_owner_room_offers_tools_but_delegated_does_not(room_client):
    from test_room_state import create_room
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner)
    bot,_,_=private_team(client,owner,project);room=create_room(client,owner,project,'Other Owner room')
    assert client.post(f'/api/v1/conversations/{room}/members/{bot}',headers=own).status_code==201
    seen=[]
    async def provider(request):
        seen.append({x['name'] for x in request.tools});return ProviderResult(text='Observed')
    client.app.state.scheduler.provider=provider
    assert client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'@Ada inspect synthetic work'}).status_code==201
    assert {'save_owner_skill','edit_owner_skill'}<=seen[0]
    target,_,_=private_team(client,owner,project);calls=[]
    async def delegate(request):
        if request.agent_id==target:
            calls.append({x['name'] for x in request.tools});return ProviderResult(text='COLLEAGUE_RESULT')
        if 'COLLEAGUE_RESULT' in request.system+json.dumps(request.events):return ProviderResult(text='Done')
        if request.observations:return ProviderResult(text='Waiting')
        return tool('message_bot',{'agent_id':target,'content':'Independent task'},'delegation')
    client.app.state.scheduler.provider=delegate
    assert client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'@Ada ask the other Bot'}).status_code==201
    assert len(calls)==1 and not ({'save_owner_skill','edit_owner_skill'} & calls[0])


def test_normal_save_is_ready_only_for_creator_and_continues(room_client):
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner)
    bot,room,_=private_team(client,owner,project);peer,_,_=private_team(client,owner,project)
    async def initial(request):return ProviderResult(text='COMPLETED_READY_SOURCE')
    scheduler=client.app.state.scheduler;scheduler.provider=initial
    assert client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'@Ada perform the method'}).status_code==201
    receipts=[]
    async def save(request):
        if request.observations:
            receipts.append(json.loads(request.observations[-1].text))
            assert 'READY_INSTRUCTION' in request.memory
            return ProviderResult(text='Saved for future use.')
        refs=catalog(request)['completed_task_refs'];assert refs
        return tool('save_owner_skill',{'source_result_seq':refs[0]['result_seq'],'mode':'for_this_bot',
            'name':'Ready method','description':'Synthetic','content':'READY_INSTRUCTION'},'save-ready')
    scheduler.provider=save
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'@Ada save the method for future use'})
    assert response.status_code==201,response.text
    receipt=receipts[0];sid=receipt['skill_id']
    assert receipt['operation']=='create_ready' and receipt['active'] is True
    assert receipt['creator_binding']=={'agent_id':bot,'skill_id':sid,'revision':1,'enabled':True}
    listing=client.get(f'/api/v1/bots/{bot}/skills',headers=own).json()['items']
    assert next(x for x in listing if x['skill_id']==sid)=={'skill_id':sid,'revision':1,'enabled':True}
    peers=client.get(f'/api/v1/bots/{peer}/skills',headers=own).json()['items']
    assert next(x for x in peers if x['skill_id']==sid)=={'skill_id':sid,'revision':0,'enabled':False}
    assert client.get(f"/api/v1/bots/{bot}/skill-persistence/{receipt['receipt_id']}",headers=own).json()['creator_binding']==receipt['creator_binding']


def test_ready_save_over_budget_rolls_back_everything(room_client):
    from app.models.owner_skills import OwnerSkill,OwnerSkillBinding,OwnerSkillCommand
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner)
    bot,room,_=private_team(client,owner,project)
    async def initial(request):return ProviderResult(text='SYNTHETIC_SOURCE')
    scheduler=client.app.state.scheduler;scheduler.provider=initial
    assert client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'@Ada finish first'}).status_code==201
    async def save(request):
        refs=catalog(request)['completed_task_refs']
        return tool('save_owner_skill',{'source_result_seq':refs[0]['result_seq'],'mode':'for_this_bot',
            'name':'Too large','description':'','content':'X'*8000},'oversized')
    scheduler.provider=save
    assert client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'@Ada save it'}).status_code==502
    with client.app.state.session_factory() as db:
        assert db.query(OwnerSkill).filter_by(owner_id=owner).count()==0
        assert db.query(OwnerSkillBinding).filter_by(owner_id=owner).count()==0
        assert db.query(OwnerSkillCommand).filter_by(owner_id=owner).count()==0
        assert not [e for e in db.query(RoomEvent).filter_by(room_id=room,kind='activity').all() if e.payload.get('category')=='skill_persistence']
