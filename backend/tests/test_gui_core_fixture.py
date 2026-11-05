"""Source-only GUI fixture isolation and canonical domain regression."""
import importlib.util
from pathlib import Path
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def fixture_app(sqlite_url, monkeypatch):
    path = Path(__file__).resolve().parents[2] / 'scripts/fixtures/gui_core_engine.py'
    spec = importlib.util.spec_from_file_location('gui_core_engine_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.install_fixture(delay_seconds=.2, patch=monkeypatch.setattr)
    from app.main import RuntimeConfig, create_app
    return lambda: create_app(runtime_config=RuntimeConfig(
        database_url=sqlite_url, local_session_token='fixture-ui-session-token-' * 2)), module


def test_automatic_restore_uses_memory_store_before_empty_database_check(fixture_app):
    factory, _ = fixture_app
    app = factory()
    assert app.state.login_secret_store is not app.state.secret_store
    assert not hasattr(app.state, 'session_factory')
    with TestClient(app) as api:
        headers = {'X-Gamja-Session': 'fixture-ui-session-token-' * 2, 'Origin': 'tauri://localhost'}
        assert api.post('/api/v1/auth/restore', headers=headers).status_code == 401
        assert app.state.login_secret_store.no_ui_reads == 1
        assert app.state.secret_store.no_ui_reads == 0
        from app.services.secrets import PlatformSecretStore
        from app.services.remembered_login import LoginSecretStore
        for kind in (PlatformSecretStore, LoginSecretStore):
            with pytest.raises(RuntimeError, match='fixture_native_store_denied'):
                kind()


def test_ui_endpoint_domain_path_keeps_job_queue_and_schedule_settings(fixture_app):
    factory, _ = fixture_app
    session = 'fixture-ui-session-token-' * 2
    headers = {'X-Gamja-Session': session, 'Origin': 'tauri://localhost'}
    app = factory()
    with TestClient(app) as api:
        assert api.get('/api/v1/projects/').status_code == 401
        assert api.get('/api/v1/projects/', headers=headers).status_code == 403
        def call(method, path, body=None):
            result = api.request(method, '/api/v1' + path, json=body, headers=headers)
            assert result.status_code < 400, (path, result.status_code)
            return result.json() if result.content else None
        call('POST', '/auth/register', {'email': 'fixture@example.com', 'username': 'Fixture', 'password': 'synthetic-password'})
        login = call('POST', '/auth/login', {'email': 'fixture@example.com', 'password': 'synthetic-password', 'remember_device': True})
        headers['Authorization'] = 'Bearer ' + login['access_token']
        assert call('POST', '/auth/restore')['access_token']
        connection = call('POST', '/connections', {'provider_kind': 'compatible', 'label': 'Fixture',
            'base_url': 'http://127.0.0.1:8765/v1', 'allow_local_http': True,
            'default_model_id': 'exact', 'api_key': 'synthetic-provider-secret'})['id']
        assert call('POST', f'/connections/{connection}/test')['ok']
        agent = call('POST', '/agents/', {'name': 'Fixture worker', 'description': 'Synthetic', 'system_prompt': 'Synthetic local work',
            'connection_id': connection, 'model_id': 'exact'})['id']
        project = call('POST', '/projects/', {'name': 'Fixture project'})['id']
        call('POST', f'/projects/{project}/agents/{agent}')
        room = call('POST', '/conversations/', {'project_id': project, 'title': 'Fixture room'})['id']
        call('POST', f'/conversations/{room}/members/{agent}')
        limits = {'max_rounds': 1, 'max_tool_calls': 1, 'max_executor_seconds': 3}
        assert call('PUT', f'/conversations/{room}/schedule', limits) == limits
        first = call('POST', f'/conversations/{room}/events', {'content': 'first fixture job'})
        second = call('POST', f'/conversations/{room}/events', {'content': 'queued fixture job'})
        from app.models.collaboration import RoomTrigger
        with app.state.session_factory() as db:
            states = {row.state for row in db.query(RoomTrigger).filter_by(room_id=room)}
            assert states == {'running', 'pending'}
        # No GUI request is needed to advance either job: dispose the UI client
        # token, then inspect completion through the canonical API after a pause.
        old_headers = headers.copy()
        headers.pop('X-Gamja-Session')
        deadline = time.monotonic() + 3
        headers.update(old_headers)
        while True:
            events = call('GET', f'/conversations/{room}/events')
            replies = [event for event in events if event['actor_type'] == 'agent' and event['kind'] == 'message']
            if len(replies) == 2 or time.monotonic() >= deadline:
                break
            time.sleep(.02)
        assert len(replies) == 2
        assert first['event_id'] != second['event_id']
        assert call('GET', f'/conversations/{room}/schedule') == limits
        assert app.state.fixture_provider_calls == 2
        assert app.state.fixture_transport_calls > 0
    restarted = factory()
    with TestClient(restarted) as api:
        headers.pop('Authorization')
        response = api.post('/api/v1/auth/login', json={'email': 'fixture@example.com', 'password': 'synthetic-password'}, headers=headers)
        assert response.status_code == 200
        headers['Authorization'] = 'Bearer ' + response.json()['access_token']
        assert api.get(f'/api/v1/conversations/{room}/schedule', headers=headers).json() == limits
        with restarted.state.session_factory() as db:
            assert {row.state for row in db.query(RoomTrigger).filter_by(room_id=room)} == {'done'}


def test_fixture_denies_effects_and_invalid_delay(fixture_app):
    _, module = fixture_app
    import asyncio
    with pytest.raises(RuntimeError, match='fixture_executor_denied'):
        asyncio.run(module.NoEffectsExecutor()(None, None, None))
    for delay in (-1, 0, 61, float('nan')):
        with pytest.raises(ValueError):
            module.install_fixture(delay_seconds=delay)
