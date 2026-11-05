import json
from functools import partial
from types import SimpleNamespace
import pytest
from test_room_state import room_client, headers, create_room
from test_teammates import team, send_call
from app.models.collaboration import RoomEvent
from app.services.providers import ProviderResult, ToolCall
from app.services.providers.base import ProviderFailure


@pytest.mark.parametrize('case', ['success', 'stopped', 'effect', 'new_child', 'source_before_post', 'concurrent', 'bot_creation', 'routine_test'])
def test_explicit_repeat_retry_source_and_idempotency(room_client, case):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, 'Synthetic retry lineage')
    ada, ben = team(client, owner, room)
    client.app.state.owner_epoch_provider = SimpleNamespace(snapshot=lambda: object(), current=lambda _: 'epoch')
    async def initial(request):
        if request.agent_id == ben: return ProviderResult(text='X' * 4100 + 'TAIL')
        if 'You have resumed' in request.system: return ProviderResult(text='Premature')
        if request.observations: return ProviderResult(text='Waiting')
        return send_call(ben, 'Check')
    client.app.state.scheduler.provider = initial
    client.post(f'/api/v1/conversations/{room}/messages', headers=headers(client, owner), json={'content': '@Ada ask Ben'})
    handoff, = client.get(f'/api/v1/conversations/{room}/teammates', headers=headers(client, owner)).json()['handoffs']
    url = f"/api/v1/conversations/{room}/failures/{handoff['return_event_id']}/retry"
    preview = client.get(url, headers=headers(client, owner)).json()
    assert preview['eligible'], preview
    schedule = client.app.state.scheduler.schedule_user_event
    async def parked(*args, **kwargs): return None
    client.app.state.scheduler.schedule_user_event = parked
    posted = client.post(url, headers={**headers(client, owner), 'Idempotency-Key': 'lineage-one'}, json={'plan_hash': preview['plan_hash']})
    assert posted.status_code == 201, posted.text
    retry_id = posted.json()['retry_event_id']
    client.app.state.scheduler.schedule_user_event = schedule
    async def unavailable(request): raise ProviderFailure('provider_unavailable', 'synthetic outage')
    client.app.state.scheduler.provider = unavailable
    client.portal.call(partial(schedule, room, retry_id, wait=True))
    with client.app.state.session_factory() as db:
        failures = [e for e in db.query(RoomEvent).filter_by(room_id=room, kind='failure').all() if (e.payload or {}).get('trigger_event_id') == retry_id]
        assert len(failures) == 1
        failure_id = failures[0].event_id
    if case in ('stopped', 'effect', 'new_child', 'bot_creation', 'routine_test'):
        from uuid import uuid4
        from datetime import datetime
        from app.models.collaboration import RoomTrigger, ToolAction
        from app.models.teammate import TeammateHandoff
        with client.app.state.session_factory() as db:
            if case == 'stopped':
                db.get(RoomTrigger, retry_id).state = 'cancelled'
            elif case == 'effect':
                db.add(ToolAction(action_id=str(uuid4()), room_id=room, agent_id=ada,
                    trigger_event_id=retry_id, family='write_file', arguments={}, state='started'))
            elif case == 'bot_creation':
                from app.models.bot_creation import BotCreation
                db.add(BotCreation(creation_id=str(uuid4()), owner_id=owner, root_room_id=room,
                    root_request_event_id=retry_id, source_agent_id=ada, source_generation=1,
                    call_id='synthetic', payload_digest='a'*64, source_profile_fingerprint='b'*64,
                    created_agent_id=ben, created_room_id=room, request_event_id=retry_id,
                    result_event_id=retry_id, response={}, created_at=datetime.now()))
            elif case == 'routine_test':
                from app.models.owner_routines import OwnerRoutine, OwnerRoutinePendingTest
                rid = str(uuid4())
                db.add(OwnerRoutine(routine_id=rid, owner_id=owner, agent_id=ada, room_id=room,
                    revision=1, name='synthetic', instruction='x', schedule={}, enabled=False,
                    profile_fingerprint='c'*64, created_at=datetime.now(), updated_at=datetime.now()))
                db.add(OwnerRoutinePendingTest(request_id=str(uuid4()), owner_id=owner, agent_id=ada,
                    routine_id=rid, config_revision=1, room_id=room, root_request_event_id=retry_id,
                    source_generation=1, activity_event_id=retry_id, state='pending', created_at=datetime.now()))
            else:
                db.add(TeammateHandoff(handoff_id=str(uuid4()), owner_id=owner, room_id=room,
                    root_request_event_id=retry_id, sender_agent_id=ada, recipient_agent_id=ben,
                    sender_name='Ada', recipient_name='Ben', content='child', delivery_key='child',
                    state='cancelled', reply_state='cancelled', depth=1, request_event_id=retry_id,
                    created_at=datetime.now(), updated_at=datetime.now()))
            db.commit()
    url = f'/api/v1/conversations/{room}/failures/{failure_id}/retry'
    preview = client.get(url, headers=headers(client, owner)).json()
    if case in ('stopped', 'effect', 'new_child', 'bot_creation', 'routine_test'):
        assert not preview['eligible'], preview
        return
    assert preview['eligible'], preview
    assert preview['teammate_recovery']['version'] == 2
    assert preview['teammate_recovery']['attempt_number'] == 2
    assert preview['teammate_recovery']['single_attempt'] is False
    if case == 'source_before_post':
        with client.app.state.session_factory() as db:
            source = db.get(RoomEvent, handoff['result_event_id'])
            source.payload = {**source.payload, 'content': 'changed after preflight'}
            db.commit()
        rejected = client.post(url, headers={**headers(client, owner), 'Idempotency-Key': 'stale'},
                               json={'plan_hash': preview['plan_hash']})
        assert rejected.status_code == 409
        return
    client.app.state.scheduler.schedule_user_event = parked
    if case == 'concurrent':
        from concurrent.futures import ThreadPoolExecutor
        def post(key):
            return client.post(url, headers={**headers(client, owner), 'Idempotency-Key': key},
                               json={'plan_hash': preview['plan_hash']}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(post, ['race-one', 'race-two']))
        assert sorted(statuses) == [201, 409]
        return
    h = {**headers(client, owner), 'Idempotency-Key': 'lineage-two'}
    created = client.post(url, headers=h, json={'plan_hash': preview['plan_hash']})
    assert created.status_code == 201, created.text
    again = client.post(url, headers=h, json={'plan_hash': preview['plan_hash']})
    assert again.status_code == 201 and again.json()['replayed'] is True
    client.app.state.scheduler.schedule_user_event = schedule
    second_id = created.json()['retry_event_id']
    client.portal.call(partial(schedule, room, second_id, wait=True))
    with client.app.state.session_factory() as db:
        second_failure, = [e for e in db.query(RoomEvent).filter_by(room_id=room, kind='failure').all()
                           if (e.payload or {}).get('trigger_event_id') == second_id]
        second_failure_id = second_failure.event_id
    url = f'/api/v1/conversations/{room}/failures/{second_failure_id}/retry'
    preview = client.get(url, headers=headers(client, owner)).json()
    assert preview['eligible'], preview
    assert preview['teammate_recovery']['attempt_number'] == 3
    client.app.state.scheduler.schedule_user_event = parked
    created = client.post(url, headers={**headers(client, owner), 'Idempotency-Key': 'lineage-three'},
                          json={'plan_hash': preview['plan_hash']})
    assert created.status_code == 201, created.text
    observed = []
    async def complete(request):
        if request.observations:
            part = json.loads(request.observations[-1].text)
            observed.append(part['content'])
            if part['next_offset'] is None:
                return ProviderResult(text='RECOVERED: ' + ''.join(observed)[-4:])
            slot, digest, offset = part['slot'], part['digest'], part['next_offset']
        else:
            source = json.JSONDecoder().raw_decode(request.system.split('Sources: ', 1)[1])[0][0]
            slot, digest, offset = source['slot'], source['digest'], 0
        return ProviderResult(tool_calls=[ToolCall('part-'+str(offset), 'read_teammate_result',
            {'slot': slot, 'digest': digest, 'offset': offset})], continuation={'call_ids': ['part-'+str(offset)]})
    client.app.state.scheduler.provider = complete
    client.app.state.scheduler.schedule_user_event = schedule
    client.portal.call(partial(schedule, room, created.json()['retry_event_id'], wait=True))
    assert ''.join(observed).endswith('TAIL')
    with client.app.state.session_factory() as db:
        assert any((e.payload or {}).get('content') == 'RECOVERED: TAIL' for e in db.query(RoomEvent).filter_by(room_id=room, kind='message'))
        from app.services.teammates import retry_source_current
        root = db.get(RoomEvent, created.json()['retry_event_id'])
        retry_source_current(db, root.payload['teammate_retry_context'], room_id=room, retry_id=root.event_id, owner_id=owner)
        source = db.get(RoomEvent, handoff['result_event_id'])
        source.payload = {**source.payload, 'content': 'changed'}
        db.commit()
        from app.services.llm_service import LLMFailure
        with pytest.raises(LLMFailure):
            retry_source_current(db, root.payload['teammate_retry_context'], room_id=room, retry_id=root.event_id, owner_id=owner)
