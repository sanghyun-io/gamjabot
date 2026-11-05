"""Synthetic retry -> new colleague -> retry acceptance, no real provider."""
import json
import pytest
from test_bot_messages import call_bot
from test_bot_batches import batch_call
from functools import partial
from types import SimpleNamespace

from test_room_state import room_client, headers, create_room
from test_teammates import team, send_call
from app.models.collaboration import RoomEvent
from app.services.providers import ProviderResult, ToolCall
from app.services.providers.base import ProviderFailure


@pytest.mark.parametrize('mode', ['single', 'batch'])
def test_retry_after_cross_room_colleagues_preserves_only_delivered_excerpts(room_client, mode):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, 'Synthetic retry child')
    ada, ben, cy, dee = team(client, owner, room, ('Ada', 'Ben', 'Cy', 'Dee'))
    for agent in (cy, dee):
        assert client.delete(f'/api/v1/conversations/{room}/members/{agent}', headers=headers(client, owner)).status_code == 204
        assert client.post(f'/api/v1/bots/{agent}/conversation', headers=headers(client, owner), json={}).status_code == 200
    client.app.state.owner_epoch_provider = SimpleNamespace(snapshot=lambda: object(), current=lambda _: 'epoch')
    async def initial(request):
        if request.agent_id == ben: return ProviderResult(text='A' * 4100 + 'BEN_TAIL')
        if 'You have resumed' in request.system: return ProviderResult(text='premature')
        if request.observations: return ProviderResult(text='waiting')
        return send_call(ben, 'Check')
    client.app.state.scheduler.provider = initial
    client.post(f'/api/v1/conversations/{room}/messages', headers=headers(client, owner), json={'content': '@Ada ask Ben'})
    initial_handoff, = client.get(f'/api/v1/conversations/{room}/teammates', headers=headers(client, owner)).json()['handoffs']
    url = f"/api/v1/conversations/{room}/failures/{initial_handoff['return_event_id']}/retry"
    first = client.get(url, headers=headers(client, owner)).json()
    assert first['eligible'], first
    schedule = client.app.state.scheduler.schedule_user_event
    async def parked(*args, **kwargs): return None
    client.app.state.scheduler.schedule_user_event = parked
    posted = client.post(url, headers={**headers(client, owner), 'Idempotency-Key': 'child-one'}, json={'plan_hash': first['plan_hash']})
    assert posted.status_code == 201, posted.text
    retry_id = posted.json()['retry_event_id']
    client.app.state.scheduler.schedule_user_event = schedule
    parts = []
    return_seen = []
    async def redelegate(request):
        if request.agent_id == cy: return ProviderResult(text='CY_ACTUAL_RESULT' + 'C' * 4100 + 'CY_PRIVATE_TAIL')
        if request.agent_id == dee: return ProviderResult(text='DEE_ACTUAL_RESULT')
        if any((event.get('payload') or {}).get('execution_context') for event in request.events):
            if not return_seen:
                return_seen.append(True)
                return ProviderResult(tool_calls=[ToolCall('return-list', 'list_bots', {})],
                    continuation={'call_ids': ['return-list']})
            raise ProviderFailure('provider_unavailable', 'synthetic outage after child delivery')
        if request.observations:
            obs = json.loads(request.observations[-1].text)
            if 'next_offset' not in obs: return ProviderResult(text='waiting')
            parts.append(obs['content'])
            if obs['next_offset'] is None:
                assert ''.join(parts).endswith('BEN_TAIL')
                return call_bot(cy, 'Check Ben result', 'child-request') if mode == 'single' else batch_call([(cy, 'Check Ben result'), (dee, 'Second view')], 'child-request')
            slot, digest, offset = obs['slot'], obs['digest'], obs['next_offset']
        else:
            source = json.JSONDecoder().raw_decode(request.system.split('Sources: ', 1)[1])[0][0]
            slot, digest, offset = source['slot'], source['digest'], 0
        return ProviderResult(tool_calls=[ToolCall('part-'+str(offset), 'read_teammate_result',
            {'slot': slot, 'digest': digest, 'offset': offset})], continuation={'call_ids': ['part-'+str(offset)]})
    client.app.state.scheduler.provider = redelegate
    client.portal.call(partial(schedule, room, retry_id, wait=True))
    with client.app.state.session_factory() as db:
        failures = [e for e in db.query(RoomEvent).filter_by(room_id=room, kind='failure').all()
                    if (e.payload or {}).get('trigger_event_id') == retry_id and (e.payload or {}).get('code') == 'provider_unavailable']
        assert len(failures) == 1
        failure_id = failures[0].event_id
    url = f'/api/v1/conversations/{room}/failures/{failure_id}/retry'
    preview = client.get(url, headers=headers(client, owner)).json()
    assert preview['eligible'], preview
    client.app.state.scheduler.schedule_user_event = parked
    posted = client.post(url, headers={**headers(client, owner), 'Idempotency-Key': 'child-two'}, json={'plan_hash': preview['plan_hash']})
    assert posted.status_code == 201, posted.text
    observed = {}
    async def complete(request):
        if request.observations:
            part = json.loads(request.observations[-1].text)
            observed.setdefault(part['slot'], '')
            observed[part['slot']] += part['content']
        sources = json.JSONDecoder().raw_decode(request.system.split('Sources: ', 1)[1])[0]
        for source in sources:
            offset = len(observed.get(source['slot'], ''))
            if offset < source['length']:
                call_id = 'read-'+source['slot']+'-'+str(offset)
                return ProviderResult(tool_calls=[ToolCall(call_id, 'read_teammate_result',
                    {'slot': source['slot'], 'digest': source['digest'], 'offset': offset})], continuation={'call_ids': [call_id]})
        assert any(text.endswith('BEN_TAIL') for text in observed.values())
        assert any('CY_ACTUAL_RESULT' in text for text in observed.values())
        assert not any('CY_PRIVATE_TAIL' in text for text in observed.values())
        if mode == 'batch': assert any('DEE_ACTUAL_RESULT' in text for text in observed.values())
        return ProviderResult(text='BOTH_RESULTS_RECOVERED')
    client.app.state.scheduler.provider = complete
    client.app.state.scheduler.schedule_user_event = schedule
    client.portal.call(partial(schedule, room, posted.json()['retry_event_id'], wait=True))
    with client.app.state.session_factory() as db:
        assert any((e.payload or {}).get('content') == 'BOTH_RESULTS_RECOVERED'
                   for e in db.query(RoomEvent).filter_by(room_id=room, kind='message')), (observed, [(e.kind, e.payload) for e in db.query(RoomEvent).filter_by(room_id=room).all() if e.kind == 'failure'])
