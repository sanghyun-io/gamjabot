from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from test_room_state import room_client

from app.services.context_projection import execution_context_event
from app.services.llm_service import LLMFailure
from app.services.teammate_delivery_evidence import cross_room_sources


@dataclass(frozen=True)
class Ref:
    room_id: int
    event_id: str
    seq: int
    actor_type: str
    actor_id: int
    kind: str


class DB:
    def __init__(self, events):
        self.events = {event.event_id: event for event in events}

    def get(self, model, key, **kwargs):
        return self.events.get(key)


def setup_origin(text='result', batch=False):
    ref = Ref(2, 'peer', 5, 'agent', 9, 'message')
    task = Ref(1, 'task', 2, 'agent', 3, 'message')
    origin = SimpleNamespace(context_mode='user_room', root_ref=task, task_ref=task,
        source_task_ref=None, task_content='task', execution_room_id=1, fingerprint='fingerprint',
        job_id='job', peer_result_ref=ref, peer_result_content=text[:4000],
        peer_result_original_chars=len(text), peer_result_included_chars=min(len(text), 4000),
        peer_result_omitted_chars=max(0, len(text)-4000), failure_code=None,
        batch_id=None, batch_results=())
    event = SimpleNamespace(**vars(ref), payload={'content': text})
    request = SimpleNamespace(watermark_seq=10, events=[execution_context_event(origin, 10)])
    return DB([event]), origin, request


def test_exact_cross_room_excerpt_is_recorded_without_content():
    text = 'x' * 5000
    db, origin, request = setup_origin(text)
    source = cross_room_sources(db, origin, request)[0]['source']
    assert source['full_length'] == 5000
    assert source['included_length'] == 4000
    assert text[:20] not in str(source)
    db.events['peer'].payload['content'] = 'y' + text[1:]
    with pytest.raises(LLMFailure):
        cross_room_sources(db, origin, request)


def test_missing_or_changed_projected_envelope_is_rejected():
    db, origin, request = setup_origin()
    request.events = []
    with pytest.raises(LLMFailure):
        cross_room_sources(db, origin, request)


def test_missing_peer_is_not_an_authored_result():
    db, origin, _ = setup_origin()
    origin.peer_result_ref = None
    origin.peer_result_content = None
    origin.peer_result_original_chars = origin.peer_result_included_chars = origin.peer_result_omitted_chars = 0
    request = SimpleNamespace(watermark_seq=10, events=[execution_context_event(origin, 10)])
    assert cross_room_sources(db, origin, request)[0]['source'] is None


def test_batch_uses_projected_per_member_quota_and_distinct_sources():
    db, origin, _ = setup_origin('z' * 5000)
    origin.batch_id = 'batch'
    origin.peer_result_ref = None
    origin.peer_result_content = None
    origin.peer_result_original_chars = origin.peer_result_included_chars = origin.peer_result_omitted_chars = 0
    request_ref = Ref(1, 'request', 2, 'agent', 3, 'message')
    delivery_ref = Ref(2, 'delivery', 3, 'system', None, 'activity')
    ref2 = Ref(3, 'other', 6, 'agent', 10, 'message')
    db.events['other'] = SimpleNamespace(**vars(ref2), payload={'content': 'q' * 5000})
    peers = []
    for index, (event_id, agent_id, ref, char) in enumerate((('a', 9, Ref(2, 'peer', 5, 'agent', 9, 'message'), 'z'), ('b', 10, ref2, 'q'))):
        peers.append(SimpleNamespace(member_index=index, message_id=event_id, agent_id=agent_id,
            name='Bot', request_ref=request_ref, delivery_ref=delivery_ref, result_ref=ref,
            content=char * 4000, original_chars=5000, included_chars=4000, omitted_chars=1000,
            failure_code=None, state='completed'))
    origin.batch_results = tuple(peers)
    request = SimpleNamespace(watermark_seq=10, events=[execution_context_event(origin, 10)])
    result = cross_room_sources(db, origin, request)
    assert [entry['source_id'] for entry in result] == ['a', 'b']
    assert all(0 < entry['source']['included_length'] <= 4000 for entry in result)
    assert all(entry['source']['full_length'] == 5000 for entry in result)


def test_batch_scheduler_persists_acknowledged_content_free_evidence(room_client):
    from test_room_state import headers
    from test_bot_messages import cross_team
    from test_bot_batches import batch_call
    from app.models.teammate import TeammateDeliveryReceipt
    from app.services.providers import ProviderResult
    import json

    client, (owner, _, project, _, _) = room_client
    room, (ada, ben, cy), _ = cross_team(client, owner, project, nested=True)
    async def provider(request):
        body = request.system + json.dumps(request.events, ensure_ascii=False)
        if request.agent_id == ada:
            if 'BEN_SENTINEL' in body and 'CY_SENTINEL' in body:
                return ProviderResult(text='joined')
            return batch_call([(ben, 'first task'), (cy, 'second task')])
        return ProviderResult(text='BEN_SENTINEL' if request.agent_id == ben else 'CY_SENTINEL')
    client.app.state.scheduler.provider = provider
    response = client.post(f'/api/v1/conversations/{room}/messages', headers=headers(client, owner),
                           json={'content': '@Ada combine the two results'})
    assert response.status_code == 201, response.text
    with client.app.state.session_factory() as db:
        rows = db.query(TeammateDeliveryReceipt).filter_by(owner_id=owner, job_kind='bot_batch').all()
        assert len(rows) == 1 and rows[0].state == 'acknowledged'
        assert len(rows[0].sources_json) == 2
        assert 'BEN_SENTINEL' not in str(rows[0].sources_json)
        assert 'CY_SENTINEL' not in str(rows[0].sources_json)
        from datetime import datetime, timedelta
        from app.models.collaboration import RoomEvent
        from app.services.teammate_delivery_evidence import sources_for_root, safe_activity_ids_for_root
        root = db.get(RoomEvent, rows[0].root_request_event_id)
        failure = SimpleNamespace(room_id=room, seq=root.seq + 10000, actor_type='agent', actor_id=ada,
                                  kind='failure', payload={'trigger_event_id': root.event_id},
                                  created_at=datetime.utcnow() + timedelta(seconds=5))
        sources = sources_for_root(db, owner, room, root.event_id, failure)
        assert len(sources) == 2
        assert {source['length'] for source in sources} == {len('BEN_SENTINEL'), len('CY_SENTINEL')}
        assert len(safe_activity_ids_for_root(db, owner, room, root.event_id, failure)) == 4
        waiting = db.query(RoomEvent).filter(RoomEvent.payload['trigger_event_id'].as_string() == root.event_id,
            RoomEvent.payload['category'].as_string() == 'bot_batch_waiting').one()
        waiting.payload = {**waiting.payload, 'generation': waiting.payload['generation'] + 1}
        db.flush()
        with pytest.raises(LLMFailure):
            safe_activity_ids_for_root(db, owner, room, root.event_id, failure)
        db.rollback()


def test_retry_read_never_exposes_beyond_approved_excerpt(monkeypatch):
    from app.services import teammates
    monkeypatch.setattr(teammates, 'retry_context_current', lambda *args, **kwargs: None)
    db, _, _ = setup_origin('allowed' + 'PRIVATE_TAIL')
    item = {'event_id': 'peer', 'length': 7, 'digest': 'excerpt-digest'}
    result = teammates.read_retry_result(db, {'sources': [item]}, {'peer': 0},
        room_id=1, retry_id='retry', owner_id=1, agent_id=3, generation=1,
        arguments={'slot': 'peer', 'offset': 0, 'digest': 'excerpt-digest'})
    assert result['content'] == 'allowed'
    assert result['total_chars'] == 7 and result['next_offset'] is None


def test_same_room_scheduler_persists_exact_sender_quote(room_client):
    from test_room_state import create_room, headers
    from test_teammates import team, send_call
    from app.models.teammate import TeammateDeliveryReceipt
    from app.services.providers import ProviderResult

    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, 'same-room receipt')
    ada, ben = team(client, owner, room)
    async def provider(request):
        if request.agent_id == ben:
            return ProviderResult(text='TEAMMATE_SECRET_SENTINEL')
        if 'You have resumed' in request.system:
            return ProviderResult(text='joined')
        if request.observations:
            return ProviderResult(text='waiting')
        return send_call(ben, 'do a subtask')
    client.app.state.scheduler.provider = provider
    response = client.post(f'/api/v1/conversations/{room}/messages', headers=headers(client, owner),
                           json={'content': '@Ada delegate and combine'})
    assert response.status_code == 201, response.text
    with client.app.state.session_factory() as db:
        rows = db.query(TeammateDeliveryReceipt).filter_by(owner_id=owner, job_kind='teammate').all()
        assert len(rows) == 1 and rows[0].state == 'acknowledged'
        assert len(rows[0].sources_json) == 1
        assert rows[0].sources_json[0]['source']['included_length'] == len('TEAMMATE_SECRET_SENTINEL')
        assert 'TEAMMATE_SECRET_SENTINEL' not in str(rows[0].sources_json)
        from datetime import datetime, timedelta
        from app.models.collaboration import RoomEvent
        from app.services.teammate_delivery_evidence import sources_for_root
        root = db.get(RoomEvent, rows[0].root_request_event_id)
        failure = SimpleNamespace(room_id=room, seq=root.seq + 10000, actor_type='agent', actor_id=ada,
                                  kind='failure', payload={'trigger_event_id': root.event_id},
                                  created_at=datetime.utcnow() + timedelta(seconds=5))
        sources = sources_for_root(db, owner, room, root.event_id, failure)
        assert len(sources) == 1 and sources[0]['length'] == len('TEAMMATE_SECRET_SENTINEL')
        from app.models.teammate import TeammateHandoff
        import copy
        receipt = rows[0]
        original_json = copy.deepcopy(receipt.sources_json)
        forged = copy.deepcopy(original_json)
        forged[0]['source_id'] = 'forged-handoff'
        receipt.sources_json = forged
        with pytest.raises(LLMFailure):
            sources_for_root(db, owner, room, root.event_id, failure)
        receipt.sources_json = original_json
        receipt.state = 'attempted'
        receipt.acknowledged_at = None
        with pytest.raises(LLMFailure):
            sources_for_root(db, owner, room, root.event_id, failure)
        db.rollback()
        receipt = db.query(TeammateDeliveryReceipt).filter_by(job_kind='teammate').one()
        row = db.get(TeammateHandoff, receipt.job_id)
        row.parent_handoff_id = row.handoff_id
        row.depth = 2
        with pytest.raises(LLMFailure):
            sources_for_root(db, owner, room, root.event_id, failure)
        db.rollback()
        with pytest.raises(LLMFailure):
            sources_for_root(db, owner + 10000, room, root.event_id, failure)
        receipt = db.query(TeammateDeliveryReceipt).filter_by(job_kind='teammate').one()
        source_event = db.get(RoomEvent, sources[0]['event_id'])
        source_event.payload = {**source_event.payload, 'content': 'changed after delivery'}
        db.flush()
        with pytest.raises(LLMFailure):
            sources_for_root(db, owner, room, root.event_id, failure)
        db.rollback()
        receipt = db.query(TeammateDeliveryReceipt).filter_by(job_kind='teammate').one()
        db.delete(receipt)
        db.flush()
        with pytest.raises(LLMFailure):
            sources_for_root(db, owner, room, root.event_id, failure)
        db.rollback()


def test_long_slot_receipt_records_only_acknowledged_observation(room_client):
    import json
    import re
    from test_room_state import create_room, headers
    from test_teammates import team, send_call
    from app.models.teammate import TeammateDeliveryReceipt
    from app.services.providers import ProviderResult, ToolCall

    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, 'slot receipt')
    ada, ben = team(client, owner, room)
    client.app.state.owner_epoch_provider = SimpleNamespace(snapshot=lambda: object(), current=lambda _: 'epoch')
    async def provider(request):
        if request.agent_id == ben:
            return ProviderResult(text='x' * 4000 + 'SECRET_TAIL')
        if 'You have resumed' in request.system:
            if request.observations:
                return ProviderResult(text='done')
            match = re.search(r'with digest ([0-9a-f]{64}) and offset (\d+)', request.system)
            return ProviderResult(tool_calls=[ToolCall('read-tail', 'read_teammate_result',
                {'digest': match.group(1), 'offset': int(match.group(2))})], continuation={'call_ids': ['read-tail']})
        if request.observations:
            return ProviderResult(text='waiting')
        return send_call(ben, 'do a long subtask')
    client.app.state.scheduler.provider = provider
    response = client.post(f'/api/v1/conversations/{room}/messages', headers=headers(client, owner),
                           json={'content': '@Ada delegate and combine'})
    assert response.status_code == 201, response.text
    with client.app.state.session_factory() as db:
        rows = db.query(TeammateDeliveryReceipt).filter_by(owner_id=owner, job_kind='teammate', state='acknowledged').all()
        chunks = [source for row in rows for source in row.sources_json if source['source_type'] == 'read_chunk']
        assert len(chunks) == 1 and chunks[0]['offset'] == 4000 and chunks[0]['length'] == len('SECRET_TAIL')
        assert 'SECRET_TAIL' not in json.dumps(chunks)


def test_acknowledged_chunk_coverage_requires_matching_contiguous_data():
    from hashlib import sha256
    from app.services.teammate_delivery_evidence import verified_receipt_sources
    text = 'abcdefghij'
    digest = lambda s: sha256(s.encode()).hexdigest()
    db, origin, request = setup_origin(text)
    source = cross_room_sources(db, origin, request)[0]
    source['source']['included_length'] = 4
    source['source']['included_digest'] = digest(text[:4])
    source['observed_offset'] = 10
    chunk = {'source_type': 'read_chunk', 'source_id': 'peer', 'offset': 4, 'length': 6,
        'digest': digest(text[4:]), 'total_length': 10, 'total_digest': digest(text),
        'observation_digest': digest('observation')}
    receipt = SimpleNamespace(state='acknowledged', acknowledged_at=object(), sources_json=[source, chunk])
    assert verified_receipt_sources(db, [receipt])['peer']['included_length'] == 10
    chunk['offset'] = 5
    with pytest.raises(LLMFailure):
        verified_receipt_sources(db, [receipt])
    chunk['offset'] = 4
    receipt.state = 'attempted'
    with pytest.raises(LLMFailure):
        verified_receipt_sources(db, [receipt])


def test_nested_same_room_recovers_only_sender_visible_receipts(room_client):
    from datetime import datetime, timedelta
    from test_room_state import create_room, headers
    from test_teammates import team, send_call
    from app.models.teammate import TeammateDeliveryReceipt
    from app.models.collaboration import RoomEvent
    from app.services.providers import ProviderResult
    from app.services.teammate_delivery_evidence import sources_for_root

    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, 'nested receipt')
    ada, ben, cy = team(client, owner, room, ('Ada', 'Ben', 'Cy'))
    client.app.state.owner_epoch_provider = SimpleNamespace(snapshot=lambda: object(), current=lambda _: 'epoch')
    async def provider(request):
        if request.agent_id == cy:
            return ProviderResult(text='CY_CONTRIBUTION')
        if 'You have resumed' in request.system:
            return ProviderResult(text='BEN_RESULT' if request.agent_id == ben else 'ADA_FINAL')
        if request.observations:
            return ProviderResult(text='waiting')
        return send_call(cy if request.agent_id == ben else ben, 'do the next part')
    client.app.state.scheduler.provider = provider
    response = client.post(f'/api/v1/conversations/{room}/messages', headers=headers(client, owner),
                           json={'content': '@Ada delegate and combine'})
    assert response.status_code == 201, response.text
    with client.app.state.session_factory() as db:
        receipts = db.query(TeammateDeliveryReceipt).filter_by(owner_id=owner, job_kind='teammate').all()
        assert len(receipts) == 2
        root = db.get(RoomEvent, receipts[0].root_request_event_id)
        failure = SimpleNamespace(room_id=room, seq=root.seq + 10000, actor_type='agent', actor_id=ada,
            kind='failure', payload={'trigger_event_id': root.event_id}, created_at=datetime.utcnow()+timedelta(seconds=5))
        sources = sources_for_root(db, owner, room, root.event_id, failure)
        assert {source['length'] for source in sources} == {len('BEN_RESULT'), len('waiting')}
        assert len(sources) == 2 and all(source['actor_id'] == ben for source in sources)
