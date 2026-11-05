"""Synthetic API regression checks for descendant reply threads."""
from test_room_state import room_client, headers, create_room
from app.services.events import append_event, ActorRef
from app.models.collaboration import RoomEvent


def add(db, room, actor, content, parent=None):
    payload = {'content': content}
    if parent:
        payload['reply_to'] = {'event_id': parent.event_id, 'seq': parent.seq}
    return append_event(db, room, actor, 'message', payload)


def make_room(room_client, name):
    client, (owner, other, project, agent, foreign) = room_client
    auth = headers(client, owner)
    room = create_room(client, owner, project, name)
    assert client.post(f'/api/v1/conversations/{room}/members/{agent}', headers=auth).status_code == 201
    return client, owner, other, project, agent, foreign, auth, room


def test_nested_snapshot_owner(room_client):
    client, owner, other, project, agent, foreign, auth, room = make_room(room_client, 'Synthetic')
    otherroom = create_room(client, owner, project, 'Other')
    with client.app.state.session_factory() as db:
        root = add(db, room, ActorRef('agent', agent), 'root')
        a = add(db, room, ActorRef('user', owner), 'a', root)
        b = add(db, room, ActorRef('agent', agent), 'b', a)
        c = add(db, room, ActorRef('user', owner), 'c', b)
        sibling = add(db, room, ActorRef('agent', agent), 'sibling', root)
        empty = add(db, room, ActorRef('agent', agent), 'empty')
        bad = add(db, room, ActorRef('agent', agent), 'bad', root)
        bad.payload = {'content': 'bad', 'reply_to': {'event_id': root.event_id, 'seq': root.seq + 1}}
        forged = add(db, room, ActorRef('agent', agent), 'forged', root)
        forged.actor_id = foreign
        db.commit()
        rid, root_seq, empty_id, empty_seq = root.event_id, root.seq, empty.event_id, empty.seq
        expected = [(a.event_id, 1, root.event_id), (b.event_id, 2, a.event_id),
                    (c.event_id, 3, b.event_id), (sibling.event_id, 1, root.event_id)]
    path = f'/api/v1/conversations/{room}/events/{rid}/thread'
    params = {'root_seq': root_seq, 'limit': 2}
    response = client.get(path, headers=auth, params=params)
    assert response.status_code == 200, response.text
    page = response.json()
    assert page['version'] == 2 and page['has_more']
    upper, cursor, items = page['upper_seq'], page['next_after_seq'], list(page['items'])
    with client.app.state.session_factory() as db:
        late = add(db, room, ActorRef('user', owner), 'late', db.get(RoomEvent, rid))
        late_id = late.event_id
    while cursor is not None:
        response = client.get(path, headers=auth, params={**params, 'upper_seq': upper, 'after_seq': cursor})
        assert response.status_code == 200, response.text
        page = response.json()
        assert page['upper_seq'] == upper
        items.extend(page['items'])
        cursor = page['next_after_seq']
        assert page['has_more'] == (cursor is not None)
    assert [(item['event']['event_id'], item['depth'], item['parent']['event_id']) for item in items] == expected
    fresh = client.get(path, headers=auth, params={'root_seq': root_seq}).json()
    assert late_id in [item['event']['event_id'] for item in fresh['items']]
    empty_page = client.get(f'/api/v1/conversations/{room}/events/{empty_id}/thread', headers=auth,
                            params={'root_seq': empty_seq}).json()
    assert empty_page['items'] == [] and empty_page['depth_limited'] is False
    assert client.get(path, headers=headers(client, other), params=params).status_code == 404
    assert client.get(f'/api/v1/conversations/{otherroom}/events/{rid}/thread', headers=auth, params=params).status_code == 404
    assert client.get(path, headers=auth, params={'root_seq': root_seq + 1}).status_code == 404
    assert client.get(path, headers=auth, params={'root_seq': root_seq, 'after_seq': 2}).status_code == 422
    assert client.get(path, headers=auth, params={'root_seq': root_seq, 'upper_seq': upper, 'after_seq': upper + 1}).status_code == 422
    with client.app.state.session_factory() as db:
        db.delete(db.get(RoomEvent, rid))
        db.commit()
    assert client.get(path, headers=auth, params=params).status_code == 404


def test_depth_limit(room_client):
    client, owner, other, project, agent, foreign, auth, room = make_room(room_client, 'Depth')
    with client.app.state.session_factory() as db:
        root = add(db, room, ActorRef('agent', agent), 'root')
        rid, root_seq, node = root.event_id, root.seq, root
        for _ in range(65):
            node = add(db, room, ActorRef('agent', agent), 'depth', node)
    response = client.get(f'/api/v1/conversations/{room}/events/{rid}/thread', headers=auth,
                          params={'root_seq': root_seq, 'limit': 100})
    assert response.status_code == 200, response.text
    value = response.json()
    assert len(value['items']) == 64 and value['depth_limited'] is True
    assert value['items'][-1]['depth'] == 64 and value['has_more'] is False
