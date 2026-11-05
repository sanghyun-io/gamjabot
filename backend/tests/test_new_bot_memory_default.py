"""New Bot defaults do not migrate existing Bot memory or copy parent data."""
from test_room_state import room_client, headers, create_room
from test_bot_creation import private_team, seed_permit, ARGS
from app.models.owner_common_memory import BotMemoryPolicy
from app.services import bot_creation
from app.services.memory_context import snapshot


def test_new_bot_default_and_legacy_dm_resolution(room_client):
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner)
    from app.models.models import Agent
    legacy,legacy_dm,_=private_team(client,owner,project)
    with client.app.state.session_factory() as db:
        connection_id=db.get(Agent,legacy).connection_id
        assert db.get(BotMemoryPolicy,(owner,legacy)) is None
    created=client.post('/api/v1/agents/',headers=own,json={'name':'New Bot','description':'Synthetic',
        'system_prompt':'Synthetic role','connection_id':connection_id,'model_id':'exact'})
    assert created.status_code==201,created.text
    new=created.json()['id']
    dm=client.post(f'/api/v1/bots/{new}/conversation',headers=own,json={}).json()['room_id']
    group=create_room(client,owner,project,'Synthetic cross-chat')
    for agent in (new,legacy):
        assert client.post(f'/api/v1/conversations/{group}/members/{agent}',headers=own).status_code==201
        value=client.post(f'/api/v1/bots/{agent}/memories',headers={**own,'Idempotency-Key':f'memory-{agent}'},json={
            'expected_scope_revision':0,'category':'fact','content':f'PRIVATE_{agent}'})
        assert value.status_code==200,value.text
    assert client.get(f'/api/v1/bots/{new}/memory-policy',headers=own).json()['mode']=='owner_rooms'
    assert client.get(f'/api/v1/bots/{new}/memory-policy',headers=own).json()['revision']==0
    assert client.post(f'/api/v1/bots/{legacy}/conversation',headers=own,json={}).status_code==200
    assert client.get(f'/api/v1/bots/{legacy}/memory-policy',headers=own).json()['mode']=='canonical_only'
    with client.app.state.session_factory() as db:
        assert db.get(BotMemoryPolicy,(owner,legacy)) is None
        assert f'PRIVATE_{new}' in str(snapshot(db,group,new)['memory'])
        assert f'PRIVATE_{legacy}' not in str(snapshot(db,group,legacy)['memory'])
        assert f'PRIVATE_{new}' not in str(snapshot(db,group,legacy)['memory'])


def test_model_created_bot_gets_default_without_copying_parent_private_memory(room_client):
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner)
    parent,room,_=private_team(client,owner,project)
    added=client.post(f'/api/v1/bots/{parent}/memories',headers={**own,'Idempotency-Key':'parent-fact'},json={
        'expected_scope_revision':0,'category':'fact','content':'PARENT_PRIVATE_ONLY'})
    assert added.status_code==200,added.text
    permit=seed_permit(client,owner,room,parent)
    with client.app.state.session_factory() as db:
        result=bot_creation.create(db,permit,'new-child',ARGS,provider_calls=1)
    child=result['created_agent']['id'];child_room=result['conversation']['room_id']
    policy=client.get(f'/api/v1/bots/{child}/memory-policy',headers=own).json()
    assert policy['mode']=='owner_rooms' and policy['revision']==0
    with client.app.state.session_factory() as db:
        assert 'PARENT_PRIVATE_ONLY' not in str(snapshot(db,child_room,child)['memory'])
