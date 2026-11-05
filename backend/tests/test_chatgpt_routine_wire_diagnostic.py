import json
from test_room_state import room_client
from app.services.providers.base import ProviderResult, wire_tool_name
from app.services.providers.chatgpt import chatgpt_wire_diagnostic
from app.services.scheduler import _safe_wire_diagnostic


def test_wire_diagnostic_records_only_allowlisted_names_and_schema_hashes():
    secret = "PRIVATE_TOKEN_DO_NOT_CAPTURE"
    body = {"instructions": secret, "input": [{"arguments": secret}], "tools": [
        {"type": "namespace", "name": "gamja", "tools": [
            {"type": "function", "name": "test_bot_routine", "parameters": {"type":"object", "description":secret}},
            {"type": "function", "name": wire_tool_name("read_file"), "parameters": {"type":"object"}},
            {"type": "function", "name": "mcp_private_tool", "parameters": {"secret":secret}},
        ]}]}
    result = chatgpt_wire_diagnostic(body)
    assert result['stage'] == 'post_returned' and result['count'] == 3
    assert {v['name'] for v in result['tools']} == {'test_bot_routine', wire_tool_name('read_file')}
    assert all(len(v['schema_sha256']) == 64 for v in result['tools'])
    assert secret not in json.dumps(result) and 'mcp_private_tool' not in json.dumps(result)
    assert _safe_wire_diagnostic(result) == result
    assert _safe_wire_diagnostic({**result, 'raw':secret}) is None
    assert _safe_wire_diagnostic({**result, 'tools':[{'name':'unknown','schema_sha256':'a'*64}]}) is None
    assert ProviderResult().wire_diagnostic is None


def test_scheduler_records_root_bound_wire_metadata_and_passes_owner_context(room_client, monkeypatch):
    from test_room_state import headers
    from test_bot_creation import private_team
    from app.models.collaboration import RoomEvent
    from app.services import routine_persistence
    client, (owner, _, project, _, _) = room_client
    auth = headers(client, owner)
    _, room, _ = private_team(client, owner, project)
    seen = []
    original = routine_persistence.metadata
    def metadata(db, owner_id, agent_id, *, trigger_event_id, room_id):
        seen.append((owner_id, agent_id, trigger_event_id, room_id))
        return original(db, owner_id, agent_id, trigger_event_id=trigger_event_id, room_id=room_id)
    monkeypatch.setattr(routine_persistence, 'metadata', metadata)
    async def provider(request):
        return ProviderResult(text='Synthetic', wire_diagnostic={
            'version':1, 'stage':'post_returned', 'count':1,
            'tools':[{'name':'test_bot_routine', 'schema_sha256':'a'*64}]})
    client.app.state.scheduler.provider = provider
    result = client.post(f'/api/v1/conversations/{room}/messages', headers=auth,
                         json={'content':'@Ada synthetic request'})
    assert result.status_code == 201 and seen
    with client.app.state.session_factory() as db:
        answer = db.query(RoomEvent).filter_by(room_id=room, actor_type='agent', kind='message').order_by(RoomEvent.seq.desc()).first()
        root_id = answer.payload['trigger_event_id']
        assert seen[-1][2:] == (root_id, room)
        assert answer.payload['provider_wire_tools'][0]['stage'] == 'post_returned'
        assert answer.payload['provider_wire_tools'][0]['request_index'] >= 1
