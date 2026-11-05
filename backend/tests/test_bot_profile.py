"""Personal Bot identity and canonical-room contracts on disposable SQLite."""
from test_room_state import room_client, headers
from app.models.models import Agent, Conversation
from app.models.collaboration import RoomEvent


def test_primary_and_resolve_are_explicit_owner_scoped_and_never_dispatch(room_client):
    client, (owner, other, _, agent, foreign) = room_client
    own = headers(client, owner)
    calls = []
    async def forbidden(_request):
        calls.append(True)
        raise AssertionError('Choosing or opening a Bot must not call a model')
    client.app.state.scheduler.provider = forbidden
    initial = client.get('/api/v1/bots/primary', headers=own)
    assert initial.status_code == 200 and initial.json()['revision'] == 0
    assert initial.json()['availability'] == 'unset'
    selected = client.put('/api/v1/bots/primary', headers=own, json={'agent_id':agent,'expected_revision':0})
    assert selected.status_code == 200 and selected.json()['agent_id'] == agent
    assert selected.json()['conversation'] is None
    assert client.put('/api/v1/bots/primary', headers=own, json={'agent_id':foreign,'expected_revision':1}).status_code == 404
    assert client.put('/api/v1/bots/primary', headers=own, json={'agent_id':None,'expected_revision':0}).status_code == 409
    first = client.post(f'/api/v1/bots/{agent}/conversation', headers=own, json={})
    again = client.post(f'/api/v1/bots/{agent}/conversation', headers=own, json={})
    assert first.status_code == again.status_code == 200, first.text
    assert first.json()['room_id'] == again.json()['room_id']
    assert first.json()['created'] is True and again.json()['replayed'] is True
    room = first.json()['room_id']
    assert client.post(f'/api/v1/bots/{agent}/conversation', headers=headers(client,other), json={}).status_code == 404
    assert client.get(f'/api/v1/bots/conversations/{room}', headers=headers(client,other)).status_code == 404
    assert client.get('/api/v1/bots/primary', headers=headers(client,other)).json()['availability'] == 'unset'
    with client.app.state.session_factory() as db:
        assert db.query(RoomEvent).filter_by(room_id=room).count() == 0
        assert db.get(Conversation,room).user_id == owner
    assert calls == []


def test_personal_room_membership_cannot_silently_become_group(room_client):
    client, (owner, _, _, agent, _) = room_client
    own = headers(client,owner)
    with client.app.state.session_factory() as db:
        peer = Agent(user_id=owner,name='Another',description='d',system_prompt='p')
        db.add(peer);db.commit();peer_id=peer.id
    room = client.post(f'/api/v1/bots/{agent}/conversation',headers=own,json={}).json()['room_id']
    info = client.get(f'/api/v1/bots/conversations/{room}',headers=own).json()
    assert info['personal_agent_id'] == agent and info['can_change_members'] is False
    assert client.post(f'/api/v1/conversations/{room}/members/{peer_id}',headers=own).status_code == 409
    assert client.delete(f'/api/v1/conversations/{room}/members/{agent}',headers=own).status_code == 409
    assert [x['agent_id'] for x in client.get(f'/api/v1/conversations/{room}/members',headers=own).json()] == [agent]


def test_closed_private_room_requires_explicit_idempotent_replacement(room_client):
    client, (owner, _, _, agent, _) = room_client
    own = headers(client,owner)
    old = client.post(f'/api/v1/bots/{agent}/conversation',headers=own,json={}).json()['room_id']
    assert client.post(f'/api/v1/conversations/{old}/stop',headers=own).status_code == 204
    assert client.post(f'/api/v1/bots/{agent}/conversation',headers=own,json={}).status_code == 409
    body={'replace_closed_room_id':old}
    replacement=client.post(f'/api/v1/bots/{agent}/conversation',headers=own,json=body)
    assert replacement.status_code == 200, replacement.text
    new=replacement.json()['room_id'];assert new != old
    replay=client.post(f'/api/v1/bots/{agent}/conversation',headers=own,json=body)
    assert replay.status_code == 200 and replay.json()['room_id']==new
    with client.app.state.session_factory() as db:
        assert db.get(Conversation,old).is_active is False
        assert db.get(Conversation,new).is_active is True
