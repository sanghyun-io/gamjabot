from test_room_state import room_client, headers
from test_bot_creation import private_team
from app.models.collaboration import RoomEvent
from app.services.providers import ProviderResult
from app.services.llm_service import LLMFailure
from app.services.scheduler import _admitted_tool_diagnostic


def test_safe_names_only():
    data = _admitted_tool_diagnostic([{'name': 'create_bot_routine'}, {'name': 'private_secret'},
                                      {'name': 'bad\nname'}, {'name': 'read_file'}], None)
    assert data['count'] == 4
    assert data['known_names'] == ['create_bot_routine', 'read_file']
    assert 'private_secret' not in str(data) and data['scope'] == 'scheduler_admitted_request_tools'


def test_admission_on_result_and_failure(room_client):
    client, (owner, _, project, _, _) = room_client
    auth = headers(client, owner)
    _, room, _ = private_team(client, owner, project)
    scheduler = client.app.state.scheduler
    async def provider(request):
        assert 'create_bot_routine' in {t['name'] for t in request.tools}
        assert 'never claim success before its actual receipt' in request.system
        return ProviderResult(text='Synthetic response')
    scheduler.provider = provider
    assert client.post(f'/api/v1/conversations/{room}/messages', headers=auth,
                       json={'content': '@Ada create a synthetic routine'}).status_code == 201
    async def failure(request):
        raise LLMFailure('synthetic_failure', 'Synthetic failure')
    scheduler.provider = failure
    assert client.post(f'/api/v1/conversations/{room}/messages', headers=auth,
                       json={'content': '@Ada another request'}).status_code == 502
    with client.app.state.session_factory() as db:
        rows = db.query(RoomEvent).filter_by(room_id=room, actor_type='agent').order_by(RoomEvent.seq).all()
        for row in rows[-2:]:
            d = row.payload['scheduler_request_tools']
            assert d['scope'] == 'scheduler_admitted_request_tools'
            assert 'create_bot_routine' in d['known_names'] and d['count'] >= len(d['known_names'])
        assert rows[-1].kind == 'failure'


def test_unavailable_tools_are_not_claimed(room_client):
    client, (owner, _, project, _, _) = room_client
    auth = headers(client, owner)
    _, room, _ = private_team(client, owner, project)
    scheduler = client.app.state.scheduler
    scheduler.broker = None
    async def provider(request):
        assert request.tools == []
        assert 'Routine changes and Tests are unavailable' in request.system
        return ProviderResult(text='Unavailable')
    scheduler.provider = provider
    assert client.post(f'/api/v1/conversations/{room}/messages', headers=auth,
                       json={'content': '@Ada create a routine'}).status_code == 201
    with client.app.state.session_factory() as db:
        row = db.query(RoomEvent).filter_by(room_id=room, actor_type='agent', kind='message').order_by(RoomEvent.seq.desc()).first()
        assert row.payload['scheduler_request_tools']['blocked_reason'] == 'broker_unavailable'
        assert row.payload['scheduler_request_tools']['count'] == 0
