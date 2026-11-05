from test_room_state import room_client, headers
from app.models.collaboration import RoomEvent
from app.services.bot_profile import trusted_memory_import

def test_import_owner_cas_replay_and_provenance(room_client):
    client, (owner, other, _, agent, _) = room_client
    own = headers(client, owner)
    calls = []
    async def schedule(room, event): calls.append((room, event))
    client.app.state.scheduler.schedule_user_event = schedule
    body = {'expected_primary_revision': 1, 'expected_agent_id': agent, 'source_label': ' Other AI ', 'content': ' synthetic answer '}
    key = {**own, 'Idempotency-Key': 'import-one'}
    assert client.post('/api/v1/bots/primary/memory-import', headers=key, json=body).status_code == 409
    assert client.put('/api/v1/bots/primary', headers=own, json={'agent_id': agent, 'expected_revision': 0}).status_code == 200
    assert client.post('/api/v1/bots/primary/memory-import', headers=key, json=body).status_code == 409
    room = client.post(f'/api/v1/bots/{agent}/conversation', headers=own, json={}).json()['room_id']
    first = client.post('/api/v1/bots/primary/memory-import', headers=key, json=body)
    assert first.status_code == 200, first.text
    result = first.json(); assert result['room_id'] == room and not result['replayed']
    with client.app.state.session_factory() as db:
        event = db.get(RoomEvent, result['event_id'])
        assert event.payload['memory_import']['source_label'] == 'Other AI'
        assert 'synthetic answer' in event.payload['content']
        assert trusted_memory_import(db, owner, room, event.event_id, agent)
        assert not trusted_memory_import(db, other, room, event.event_id, agent)
    assert client.put('/api/v1/bots/primary', headers=own, json={'agent_id': None, 'expected_revision': 1}).status_code == 200
    replay = client.post('/api/v1/bots/primary/memory-import', headers=key, json=body)
    assert replay.status_code == 200 and replay.json() == dict(result, replayed=True)
    assert client.post('/api/v1/bots/primary/memory-import', headers=key, json={**body, 'content': 'different'}).status_code == 409
    assert calls == [(room, result['event_id']), (room, result['event_id'])]


def test_import_tools_and_saved_provenance(room_client):
    import asyncio
    from test_bot_creation import private_team, tool
    from app.services.providers import ProviderResult
    client, (owner, _, project, _, _) = room_client
    agent, room, _ = private_team(client, owner, project)
    own = headers(client, owner)
    assert client.put('/api/v1/bots/primary', headers=own, json={'agent_id': agent, 'expected_revision': 0}).status_code == 200
    scheduler = client.app.state.scheduler
    original = scheduler.schedule_user_event
    async def defer(room, event): pass
    scheduler.schedule_user_event = defer
    result = client.post('/api/v1/bots/primary/memory-import', headers={**own, 'Idempotency-Key': 'save'}, json={
        'expected_primary_revision': 1, 'expected_agent_id': agent, 'source_label': 'AI', 'content': 'I like green'}).json()
    scheduler.schedule_user_event = original
    seen = []
    async def provider(request):
        seen.append({x['name'] for x in request.tools})
        if not request.observations:
            return tool('save_bot_memory', {'category': 'preference', 'content': 'I like green'}, 'save')
        return ProviderResult(text='Saved.')
    scheduler.provider = provider
    async def run_import():
        await scheduler.schedule_user_event(room, result['event_id'], wait=True)
    client.portal.call(run_import)
    assert seen and all(names == {'save_bot_memory'} for names in seen)
    with client.app.state.session_factory() as db:
        all_events = db.query(RoomEvent).filter_by(room_id=room).all()
        assert any(e.kind == 'message' and e.actor_type == 'agent' and e.payload.get('trigger_event_id') == result['event_id'] for e in all_events), [(e.kind,e.payload) for e in all_events]
    listing = client.get(f'/api/v1/bots/{agent}/memories', headers=own).json()
    assert len(listing['items']) == 1
    entry = listing['items'][0]
    assert entry['group_allowed'] is False
    assert entry['source_snapshot']['memory_import']['source_label'] == 'AI'
    edit = client.patch(f"/api/v1/bots/{agent}/memories/{entry['memory_id']}", headers={**own, 'Idempotency-Key':'edit'}, json={
        'expected_scope_revision': listing['revision'], 'expected_revision': entry['revision'], 'category':'preference', 'content':'I like blue'})
    assert edit.status_code == 200, edit.text
    assert client.get(f'/api/v1/bots/{agent}/memories', headers=own).json()['items'][0]['source_snapshot']['memory_import']['source_label'] == 'AI'


def test_replay_does_not_cancel_and_later_history_redacts_import(room_client):
    from test_bot_creation import private_team
    from app.services.providers import ProviderResult
    client, (owner, _, project, _, _) = room_client
    agent, room, _ = private_team(client, owner, project)
    own = headers(client, owner)
    assert client.put('/api/v1/bots/primary', headers=own, json={'agent_id': agent, 'expected_revision': 0}).status_code == 200
    scheduler = client.app.state.scheduler
    calls = []
    scheduler.cancel_checkin = lambda *a, **k: calls.append('checkin')
    scheduler.cancel_routine = lambda *a, **k: calls.append('routine')
    observed = []
    async def provider(request):
        observed.append(request)
        return ProviderResult(text='Synthetic response')
    scheduler.provider = provider
    imported_text = 'x' * (12000 - len('MALICIOUS_PRIVATE_SENTINEL')) + 'MALICIOUS_PRIVATE_SENTINEL'
    body = {'expected_primary_revision':1, 'expected_agent_id':agent, 'source_label':'Other AI', 'content':imported_text}
    key = {**own, 'Idempotency-Key':'replay'}
    first = client.post('/api/v1/bots/primary/memory-import', headers=key, json=body)
    assert first.status_code == 200, first.text
    async def finish():
        await scheduler.schedule_user_event(room, first.json()['event_id'], wait=True)
    client.portal.call(finish)
    with client.app.state.session_factory() as db:
        failures = [e.payload for e in db.query(RoomEvent).filter_by(room_id=room, kind='failure').all()]
    assert observed, failures
    assert imported_text in observed[0].events[-1]['payload']['content']
    calls.clear()
    replay = client.post('/api/v1/bots/primary/memory-import', headers=key, json=body)
    assert replay.status_code == 200 and replay.json()['replayed']
    assert calls == []
    response = client.post(f'/api/v1/conversations/{room}/messages', headers=own, json={'content':'A separate question'})
    assert response.status_code == 201, response.text
    import json
    later = json.dumps(observed[-1].events, ensure_ascii=False)
    assert 'MALICIOUS_PRIVATE_SENTINEL' not in later
    assert 'Original text omitted' in later
    with client.app.state.session_factory() as db:
        assert 'MALICIOUS_PRIVATE_SENTINEL' in db.get(RoomEvent, first.json()['event_id']).payload['content']


def test_import_false_save_claim_is_failure_and_history_is_excluded(room_client):
    from test_bot_creation import private_team
    from app.services.events import ActorRef, append_event
    from app.services.providers import ProviderResult
    client, (owner, _, project, _, _) = room_client
    agent, room, _ = private_team(client, owner, project)
    own = headers(client, owner)
    with client.app.state.session_factory() as db:
        append_event(db, room, ActorRef('user', owner), 'message', {'content':'OLD_UNRELATED_CHAT_SENTINEL'})
    assert client.put('/api/v1/bots/primary', headers=own, json={'agent_id':agent,'expected_revision':0}).status_code == 200
    scheduler = client.app.state.scheduler
    original = scheduler.schedule_user_event
    async def defer(room, event): pass
    scheduler.schedule_user_event = defer
    body = {'expected_primary_revision':1,'expected_agent_id':agent,'source_label':'AI','content':'SYNTHETIC_IMPORT_FACT'}
    result = client.post('/api/v1/bots/primary/memory-import', headers={**own,'Idempotency-Key':'false-claim'}, json=body).json()
    scheduler.schedule_user_event = original
    observed = []
    async def provider(request):
        observed.append(request)
        return ProviderResult(text='I saved all memories and have a receipt.')
    scheduler.provider = provider
    async def run(): await scheduler.schedule_user_event(room, result['event_id'], wait=True)
    client.portal.call(run)
    assert observed and len(observed[0].events) == 1
    assert 'OLD_UNRELATED_CHAT_SENTINEL' not in str(observed[0].events)
    assert 'SYNTHETIC_IMPORT_FACT' in str(observed[0].events)
    with client.app.state.session_factory() as db:
        events = db.query(RoomEvent).filter_by(room_id=room).all()
        assert any(e.kind == 'failure' and e.payload.get('code') == 'memory_import_not_saved' and e.payload.get('trigger_event_id') == result['event_id'] for e in events)
        assert not any(e.actor_type == 'agent' and e.kind == 'message' and e.payload.get('trigger_event_id') == result['event_id'] for e in events)
        assert not any(e.kind == 'activity' and e.payload.get('status') == 'passed' and e.payload.get('trigger_event_id') == result['event_id'] for e in events)
    assert client.get(f'/api/v1/bots/{agent}/memories', headers=own).json()['items'] == []
