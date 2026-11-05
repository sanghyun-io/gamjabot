"""Explicit Bot-private memory and privacy boundaries on disposable SQLite."""
import pytest
from test_room_state import room_client, headers, create_room
from app.models.collaboration import RoomAgentRun
from app.models.models import Agent
from app.services.memory_context import snapshot


def add(client, owner, agent, content='PRIVATE_GREEN_91', revision=0, key='memory-add'):
    return client.post(f'/api/v1/bots/{agent}/memories',headers={**headers(client,owner),'Idempotency-Key':key},json={
        'expected_scope_revision':revision,'category':'preference','content':content})


def test_explicit_memory_replay_cas_and_owner_boundary(room_client):
    client,(owner,other,_,agent,_) = room_client
    first=add(client,owner,agent)
    assert first.status_code==200,first.text
    item=first.json();assert item['revision']==1 and item['entry_revision']==1
    replay=add(client,owner,agent);assert replay.status_code==200 and replay.json()['replayed'] is True
    assert replay.json()['memory_id']==item['memory_id']
    assert add(client,owner,agent,'changed',key='memory-add').status_code==409
    assert add(client,owner,agent,'stale revision',key='new-key').status_code==409
    assert client.get(f'/api/v1/bots/{agent}/memories',headers=headers(client,other)).status_code==404
    entries=client.get(f'/api/v1/bots/{agent}/memories',headers=headers(client,owner)).json()
    assert len(entries['items'])==1 and entries['items'][0]['content']=='PRIVATE_GREEN_91'
    assert entries['scope']=='bot_private' and entries['authority']=='context_only'


def test_private_memory_enters_only_exact_canonical_bot_room(room_client):
    client,(owner,_,project,agent,_) = room_client
    own=headers(client,owner)
    private=client.post(f'/api/v1/bots/{agent}/conversation',headers=own,json={}).json()['room_id']
    assert add(client,owner,agent).status_code==200
    group=create_room(client,owner,project,'Explicit separate room')
    assert client.post(f'/api/v1/conversations/{group}/members/{agent}',headers=own).status_code==201
    with client.app.state.session_factory() as db:
        peer=Agent(user_id=owner,name='Peer',description='d',system_prompt='p');db.add(peer);db.commit();peer_id=peer.id
    other_private=client.post(f'/api/v1/bots/{peer_id}/conversation',headers=own,json={}).json()['room_id']
    with client.app.state.session_factory() as db:
        selected=snapshot(db,private,agent)
        assert selected['bot_private']['eligible'] is True
        assert selected['memory']['bot_private'][0]['content']=='PRIVATE_GREEN_91'
        separate=snapshot(db,group,agent)
        assert separate['bot_private']['eligible'] is False and 'PRIVATE_GREEN_91' not in str(separate['memory'])
        peer_context=snapshot(db,other_private,peer_id)
        assert 'PRIVATE_GREEN_91' not in str(peer_context['memory'])


def test_edit_delete_invalidates_old_context_and_busy_turn_rejects_writes(room_client):
    client,(owner,_,_,agent,_) = room_client
    own=headers(client,owner)
    room=client.post(f'/api/v1/bots/{agent}/conversation',headers=own,json={}).json()['room_id']
    first=add(client,owner,agent).json();mid=first['memory_id']
    with client.app.state.session_factory() as db:
        old=snapshot(db,room,agent)
        run=db.get(RoomAgentRun,(room,agent));run.state='running';db.commit()
    busy=add(client,owner,agent,'not while running',revision=1,key='busy-add')
    assert busy.status_code==409 and busy.json()['detail']['code']=='bot_memory_busy'
    with client.app.state.session_factory() as db:
        run=db.get(RoomAgentRun,(room,agent));run.state='idle';db.commit()
    edited=client.patch(f'/api/v1/bots/{agent}/memories/{mid}',headers={**own,'Idempotency-Key':'memory-edit'},json={
        'expected_scope_revision':1,'expected_revision':1,'category':'preference','content':'PRIVATE_BLUE_17'})
    assert edited.status_code==200,edited.text
    with client.app.state.session_factory() as db:
        changed=snapshot(db,room,agent)
        assert old['digest']!=changed['digest']
        assert changed['binding']['context_after_seq']>=edited.json()['activity_seq']
        assert 'PRIVATE_GREEN_91' not in str(changed['memory'])
    removed=client.request('DELETE',f'/api/v1/bots/{agent}/memories/{mid}',headers={**own,'Idempotency-Key':'memory-delete'},json={
        'expected_scope_revision':2,'expected_revision':2})
    assert removed.status_code==200,removed.text
    with client.app.state.session_factory() as db:
        forgotten=snapshot(db,room,agent)
        assert changed['digest']!=forgotten['digest']
        assert forgotten['bot_private']['revision']==3 and forgotten['bot_private']['entries']==[]
        assert 'PRIVATE_BLUE_17' not in str(forgotten['memory'])
