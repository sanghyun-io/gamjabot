import asyncio
from datetime import UTC, datetime
import json
import socket
import sqlite3
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.collaboration import ProviderConnection
from app.models.models import User


class FakeSecrets:
    def __init__(self):
        self.values = {}

    def put(self, ref, value):
        self.values[ref] = value

    def get(self, ref):
        return self.values.get(ref)

    def get_no_ui(self, ref):
        return self.values.get(ref)

    def delete(self, ref):
        self.values.pop(ref, None)


class BlockingSecrets(FakeSecrets):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def get_no_ui(self, ref):
        self.entered.set()
        self.release.wait(3)
        return super().get_no_ui(ref)


class _NativeTestExecution:
    request_id = 2

    def __init__(self, client, model_id):
        self.client = client
        self.model_id = model_id

    def result(self, timeout=None):
        assert timeout is None
        self.client.events.append('terminal')
        return {
            'ok': True, 'response_id': f'response-{len(self.client.calls)}',
            'model': self.model_id, 'text': 'OK', 'tool_calls': [],
            'physical_model_request_count': 1,
        }

    def cancel(self):
        self.client.events.append('cancel')


def _native_test_core():
    from app.services.core_provider_transport import CoreProviderClient

    class Client(CoreProviderClient):
        def __init__(self):
            self.bindings = []
            self.calls = []
            self.events = []

        def bind(self, binding, *, timeout=None):
            assert timeout is None
            self.events.append('bind')
            self.bindings.append(binding)
            return 'ab' * 32

        def begin_execute(self, handle, call):
            assert handle == 'ab' * 32
            self.events.append('execute')
            self.calls.append(call)
            return _NativeTestExecution(self, call['model_id'])

        def release(self, handle, *, timeout=None):
            assert timeout is None and handle == 'ab' * 32
            self.events.append('release')

        def close(self):
            pass

    return Client()


@pytest.fixture
def setup(sqlite_url, monkeypatch):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'connections-test-token')
    secrets = FakeSecrets()
    with TestClient(create_app(database_url=sqlite_url)) as client:
        client.app.state.secret_store = secrets
        db = client.app.state.session_factory()
        users = [User(email=f'{n}@example.test', username=n, hashed_password='unused') for n in ('owner', 'other')]
        db.add_all(users)
        db.commit()
        ids = [user.id for user in users]
        db.close()
        def headers(owner=0):
            return {'X-Gamja-Session': 'connections-test-token', 'Authorization': f'Bearer {create_access_token({"sub": ids[owner]})}'}
        yield client, secrets, headers, sqlite_url


def test_independent_connections_owner_and_secret_redaction(setup):
    client, secrets, headers, url = setup
    first = client.post('/api/v1/connections', headers=headers(), json={'provider_kind': 'openai', 'label': 'First', 'default_model_id': 'gpt-4o-mini', 'api_key': 'first-test-secret'})
    second = client.post('/api/v1/connections', headers=headers(), json={'provider_kind': 'openai', 'label': 'Second', 'default_model_id': 'gpt-4.1', 'api_key': 'second-test-secret'})
    assert first.status_code == second.status_code == 201
    a, b = first.json(), second.json()
    assert a['id'] != b['id'] and 'secret_ref' not in a and 'secret_ref' not in b
    assert 'first-test-secret' not in first.text + second.text
    assert client.patch(f"/api/v1/connections/{a['id']}", headers=headers(), json={'default_model_id': 'gpt-4.1-mini', 'api_key': 'replacement-secret'}).status_code == 200
    assert client.get('/api/v1/connections', headers=headers()).json()[1]['default_model_id'] == 'gpt-4.1'
    assert client.get('/api/v1/connections', headers=headers(1)).json() == []
    assert client.patch(f"/api/v1/connections/{a['id']}", headers=headers(1), json={'label': 'stolen'}).status_code == 404
    assert client.delete(f"/api/v1/connections/{a['id']}", headers=headers(1)).status_code == 404
    assert client.post(f"/api/v1/connections/{a['id']}/test", headers=headers(1)).status_code == 404
    assert 'second-test-secret' in secrets.values.values()
    with sqlite3.connect(url.removeprefix('sqlite:///')) as db:
        dump = '\n'.join(db.iterdump())
    for secret in ('first-test-secret', 'second-test-secret', 'replacement-secret'):
        assert secret not in dump


def test_url_and_auth_modes(setup):
    client, _, headers, _ = setup
    base = {'provider_kind': 'compatible', 'label': 'Local', 'default_model_id': 'local-model'}
    for endpoint in ('http://evil.example/v1', 'https://user:pass@example.com/v1', 'https://example.com/v1?key=secret', 'https://example.com/v1#x', 'http://127.0.0.1.evil.test/v1', 'https://@example.com/v1', 'https://example.com/v1?', 'https://example.com/v1#'):
        assert client.post('/api/v1/connections', headers=headers(), json={**base, 'base_url': endpoint, 'api_key': 'test-key'}).status_code == 422
    assert client.post('/api/v1/connections', headers=headers(), json={**base, 'base_url': 'http://127.0.0.1:1234/v1', 'auth_required': False}).status_code == 422
    local = client.post('/api/v1/connections', headers=headers(), json={**base, 'base_url': 'http://127.0.0.1:1234/v1', 'allow_local_http': True, 'auth_required': False})
    assert local.status_code == 201 and local.json()['is_local'] is True
    assert local.json()['has_secret'] is False


def test_versioned_connection_metadata_keeps_legacy_projection_and_named_profile_contract(setup):
    from app.services import connections

    client, _, _, _ = setup
    with client.app.state.session_factory() as db:
        owner_id = db.query(User).filter_by(username='owner').one().id
        row = ProviderConnection(
            user_id=owner_id, kind='compatible', name='Named profile',
            base_url=None, secret_ref='named-profile-ref', default_model_id='profile-model',
            enabled=True, auth_required=True, capabilities={},
            provider_id='openrouter', protocol_id='openai_chat_completions',
            auth_method_id='api_key', public_config_version=1, public_config={},
            auth_grant_revision=7, credential_state='present',
        )
        db.add(row)
        db.commit()
        config, reference = connections._connection_metadata(db, row.id, owner_id)

    assert config.provider_kind == 'compatible'
    assert config.provider_id == 'openrouter'
    assert config.protocol_id == 'openai_chat_completions'
    assert config.auth_method_id == 'api_key'
    assert config.public_fields == {}
    assert config.auth_grant_revision == 7
    assert reference == 'named-profile-ref'


def test_azure_api_key_catalog_is_selectable_but_create_requires_core_coordinator(setup):
    from app.schemas.connection import VersionedConnectionCreate

    client, secrets, headers, _ = setup
    body = {
        'version': 1, 'provider_id': 'azure-openai', 'protocol_id': 'azure_openai',
        'auth_method_id': 'api_key',
        'fields': {'resource_endpoint': 'https://example.openai.azure.com',
                   'api_version': 'v1'},
        'label': 'Azure', 'default_model_id': 'deployment-one',
        'api_key': 'private-direct-marker', 'enabled': True, 'capabilities': {},
    }
    assert VersionedConnectionCreate.model_validate(body).protocol_id == 'azure_openai'
    response = client.post('/api/v1/connections', headers=headers(), json=body)
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'owner_epoch_unavailable'
    with client.app.state.session_factory() as db:
        assert db.query(ProviderConnection).count() == 0
    assert secrets.values == {}


def test_reviewed_azure_direct_api_route_uses_coordinator_and_never_raw_store(
        setup, monkeypatch):
    from app.services import provider_catalog
    from app.services.connection_recovery import LifecycleResult

    client, secrets, headers, _ = setup
    descriptor = provider_catalog.get_descriptor('azure-openai')
    eligible = descriptor.model_copy(update={
        'methods': tuple(method.model_copy(update={
            'selectable': True, 'external_eligibility': 'verified',
            'delivery_phase': 'D2',
        }) for method in descriptor.methods),
    })
    monkeypatch.setitem(provider_catalog._BY_PROVIDER, 'azure-openai', eligible)
    calls = []
    class Coordinator:
        def commit_direct_api_key(self, db, row, secret):
            calls.append((row.provider_id, row.protocol_id, row.auth_method_id, secret))
            row.secret_ref = f'{row.user_id}:{row.id}:' + '8' * 32
            row.credential_state = 'present'
            row.readiness_revision = int(row.readiness_revision or 1) + 1
            row.auth_grant_revision = int(row.auth_grant_revision or 0) + 1
            db.commit()
            return LifecycleResult('a' * 64, 'succeeded')
    body = {
        'version': 1, 'provider_id': 'azure-openai', 'protocol_id': 'azure_openai',
        'auth_method_id': 'api_key',
        'fields': {'resource_endpoint': 'https://example.openai.azure.com',
                   'api_version': 'v1'},
        'label': 'Azure', 'default_model_id': 'deployment-one',
        'api_key': 'private-direct-marker', 'enabled': True, 'capabilities': {},
    }
    unavailable = client.post('/api/v1/connections', headers=headers(), json=body)
    assert unavailable.status_code == 503
    with client.app.state.session_factory() as db:
        assert db.query(ProviderConnection).count() == 0
    client.app.state.connection_recovery = Coordinator()
    created = client.post('/api/v1/connections', headers=headers(), json=body)
    assert created.status_code == 201, created.text
    assert calls == [('azure-openai', 'azure_openai', 'api_key', 'private-direct-marker')]
    assert secrets.values == {}
    assert 'private-direct-marker' not in created.text
    replaced = client.patch(
        f"/api/v1/connections/{created.json()['id']}", headers=headers(),
        json={**body, 'api_key': 'replacement-direct-marker'})
    assert replaced.status_code == 200, replaced.text
    assert calls[-1] == ('azure-openai', 'azure_openai', 'api_key',
                         'replacement-direct-marker')
    assert secrets.values == {}

    class ForbiddenStore:
        def get_no_ui(self, _reference):
            raise AssertionError('Core-managed Azure key must stay opaque')
    class OwnerEpochs:
        def snapshot(self):
            return SimpleNamespace(owner_id=1, owner_epoch='11' * 32)
        def current(self, _owner_id):
            return '11' * 32
    client.app.state.secret_store = ForbiddenStore()
    client.app.state.owner_epoch_provider = OwnerEpochs()
    client.app.state.provider_client = _native_test_core()
    client.app.state.connection_transport = httpx.MockTransport(
        lambda _request: pytest.fail('Azure API key Test must not use Python HTTP'))
    tested = client.post(
        f"/api/v1/connections/{created.json()['id']}/test?profile=text",
        headers=headers())
    assert tested.status_code == 200, tested.text
    assert tested.json()['ok'] is True, tested.text
    assert tested.json()['actual_request_count'] == 1

    from app.services import connections
    assert connections.is_core_managed_credential(SimpleNamespace(
        provider_id='azure-openai', protocol_id='azure_openai',
        auth_method_id='api_key')) is True
    assert connections.is_core_managed_credential(SimpleNamespace(
        provider_id='openrouter', protocol_id='openai_chat_completions',
        auth_method_id='api_key')) is False


@pytest.mark.asyncio
async def test_native_connection_resolution_keeps_opaque_binding_without_store_or_cache_reads(setup):
    from app.services import connections
    from app.services.connection_recovery import _public_config_fingerprint

    client, _, _, _ = setup
    calls = []

    class ForbiddenStore:
        def get_no_ui(self, reference):
            calls.append(('store', reference))
            raise AssertionError('native resolver must not read the credential store')

    class ForbiddenCache:
        def get(self, key):
            calls.append(('cache_get', key))
            raise AssertionError('native resolver must not read the credential cache')

        def reserve(self, key):
            calls.append(('cache_reserve', key))
            raise AssertionError('native resolver must not reserve the credential cache')

    with client.app.state.session_factory() as db:
        owner_id = db.query(User).filter_by(username='owner').one().id
        row = ProviderConnection(
            user_id=owner_id, kind='azure-openai', name='Native Azure', base_url=None,
            secret_ref=f'{owner_id}:71:' + '22' * 16, default_model_id='deployment-one',
            enabled=True, auth_required=True, capabilities={}, readiness_revision=3,
            provider_id='azure-openai', protocol_id='azure_openai',
            auth_method_id='native_chain', public_config_version=1,
            public_config={'resource_endpoint': 'https://example.openai.azure.com',
                           'api_version': 'v1'},
            auth_grant_revision=4, credential_state='present',
        )
        db.add(row)
        db.commit()
        expected_fingerprint = _public_config_fingerprint(row, 'native_chain')
        config = await connections.resolve_connection_async(
            db, row.id, owner_id, ForbiddenStore(), cache=ForbiddenCache())

    assert calls == []
    assert config.api_key is None
    assert config.owner_id == owner_id
    assert config.credential_ref == row.secret_ref
    assert config.config_revision == 3
    assert config.auth_grant_revision == 4
    assert config.public_config_fingerprint == expected_fingerprint
    assert config.descriptor_revision == 1
    assert config.public_fields == {
        'resource_endpoint': 'https://example.openai.azure.com',
        'api_version': 'v1',
    }


def test_readiness_history_preserves_unknown_native_request_count_without_coercion(setup):
    from app.services import readiness

    client, _, _, _ = setup
    with client.app.state.session_factory() as db:
        owner_id = db.query(User).filter_by(username='owner').one().id
        row = ProviderConnection(
            user_id=owner_id, kind='google-vertex', name='Native history',
            default_model_id='gemini-native', secret_ref=f'{owner_id}:81:' + '33' * 16,
            auth_required=True, enabled=True, capabilities={}, readiness_revision=2,
            provider_id='google-vertex', protocol_id='vertex_generate_content',
            auth_method_id='native_chain', public_config_version=1,
            public_config={'project_id': 'potato-project', 'location': 'us-central1'},
            auth_grant_revision=3, credential_state='present',
        )
        db.add(row)
        db.commit()
        fingerprint = readiness.test_fingerprint(row, 'gemini-native', 'text')
        recorded = readiness.record_test(
            db, owner_id=owner_id, connection_id=row.id, config_revision=2,
            fingerprint=fingerprint, model_id='gemini-native', ok=True,
            code='connected', auth_required=True, verified=None, duration_ms=1,
            profile='text', actual_request_count=None,
        )

    assert recorded['actual_request_count'] is None


def test_native_explicit_test_uses_core_capability_cas_and_preserves_unknown_meter(setup):
    client, _, headers, _ = setup
    with client.app.state.session_factory() as db:
        owner_id = db.query(User).filter_by(username='owner').one().id
        row = ProviderConnection(
            user_id=owner_id, kind='google-vertex', name='Native Test',
            default_model_id='gemini-native', secret_ref=f'{owner_id}:91:' + '44' * 16,
            auth_required=True, enabled=True,
            capabilities={'declared': {'temperature': True}, 'verified': {}},
            readiness_revision=3, provider_id='google-vertex',
            protocol_id='vertex_generate_content', auth_method_id='native_chain',
            public_config_version=1,
            public_config={'project_id': 'potato-project', 'location': 'us-central1'},
            auth_grant_revision=4, credential_state='present',
        )
        db.add(row)
        db.commit()
        connection_id = row.id

    class OwnerEpochs:
        def snapshot(self):
            return SimpleNamespace(owner_id=owner_id, owner_epoch='11' * 32)

        def current(self, selected_owner_id):
            assert selected_owner_id == owner_id
            return '11' * 32

    core = _native_test_core()
    http_calls = []
    client.app.state.owner_epoch_provider = OwnerEpochs()
    client.app.state.provider_client = core
    client.app.state.connection_transport = httpx.MockTransport(
        lambda request: http_calls.append(request) or pytest.fail('native Test used HTTPX'))

    response = client.post(
        f'/api/v1/connections/{connection_id}/test?profile=full', headers=headers())

    assert response.status_code == 200, response.text
    assert response.json()['ok'] is True
    assert response.json()['model_id'] == 'gemini-native'
    assert response.json()['actual_request_count'] == 2
    assert response.json()['record']['actual_request_count'] == 2
    assert response.json()['verified']['temperature'] is True
    assert http_calls == []
    assert len(core.calls) == 2
    assert [call['physical_model_request_budget'] for call in core.calls] == [2, 1]
    assert all(call['model_id'] == 'gemini-native' for call in core.calls)
    assert all(call['protocol'] == 'vertex_generate_content_v1' for call in core.calls)
    assert core.events == [
        'bind', 'execute', 'terminal', 'release',
        'bind', 'execute', 'terminal', 'release',
    ]
    with client.app.state.session_factory() as db:
        saved = db.get(ProviderConnection, connection_id)
        assert saved.readiness_revision == 3
        assert saved.auth_grant_revision == 4
        assert saved.capabilities['verified']['models']['gemini-native']['temperature'] is True


@pytest.mark.parametrize('terminal_fault', ['missing_count', 'over_budget', 'eof'])
def test_native_full_test_unknown_capability_fails_and_preserves_verified(
        setup, terminal_fault):
    from app.services.core_provider_transport import CoreProviderUnavailable

    client, _, headers, sqlite_url = setup
    original_capabilities = {
        'declared': {'temperature': True, 'tools': True},
        'verified': {'models': {'previous-model': {'temperature': True}}},
    }
    with client.app.state.session_factory() as db:
        owner_id = db.query(User).filter_by(username='owner').one().id
        row = ProviderConnection(
            user_id=owner_id, kind='google-vertex', name='Unknown terminal Test',
            default_model_id='gemini-native', secret_ref=f'{owner_id}:92:' + '55' * 16,
            auth_required=True, enabled=True, capabilities=original_capabilities,
            readiness_revision=3, provider_id='google-vertex',
            protocol_id='vertex_generate_content', auth_method_id='native_chain',
            public_config_version=1,
            public_config={'project_id': 'potato-project', 'location': 'us-central1'},
            auth_grant_revision=4, credential_state='present',
        )
        db.add(row)
        db.commit()
        connection_id = row.id
    with sqlite3.connect(sqlite_url.removeprefix('sqlite:///')) as raw_db:
        before_capabilities = raw_db.execute(
            'SELECT capabilities FROM provider_connections WHERE id=?',
            (connection_id,)).fetchone()[0]

    class OwnerEpochs:
        def snapshot(self):
            return SimpleNamespace(owner_id=owner_id, owner_epoch='11' * 32)

        def current(self, selected_owner_id):
            assert selected_owner_id == owner_id
            return '11' * 32

    core = _native_test_core()
    normal_begin = core.begin_execute

    class FaultExecution:
        request_id = 4

        def result(self, timeout=None):
            assert timeout is None
            core.events.append('terminal_fault')
            if terminal_fault == 'eof':
                raise CoreProviderUnavailable()
            payload = {
                'ok': True, 'response_id': 'response-fault',
                'model': 'gemini-native', 'text': 'OK', 'tool_calls': [],
            }
            if terminal_fault == 'over_budget':
                payload['physical_model_request_count'] = 4
            return payload

        def cancel(self):
            core.events.append('cancel')

    def begin_execute(handle, call):
        execution = normal_begin(handle, call)
        return FaultExecution() if len(core.calls) == 2 else execution

    core.begin_execute = begin_execute
    client.app.state.owner_epoch_provider = OwnerEpochs()
    client.app.state.provider_client = core
    client.app.state.connection_transport = httpx.MockTransport(
        lambda request: pytest.fail('native Test used HTTPX'))

    response = client.post(
        f'/api/v1/connections/{connection_id}/test?profile=full', headers=headers())

    assert response.status_code == 200, response.text
    assert response.json()['ok'] is False
    assert response.json()['code'] == 'provider_error'
    assert response.json()['actual_request_count'] is None
    assert response.json()['record']['ok'] is False
    assert response.json()['record']['code'] == 'provider_error'
    assert response.json()['record']['verified_capabilities'] is None
    assert response.json()['record']['actual_request_count'] is None
    assert len(core.calls) == 2
    assert [call['physical_model_request_budget'] for call in core.calls] == [4, 3]
    with client.app.state.session_factory() as db:
        saved = db.get(ProviderConnection, connection_id)
        assert saved.capabilities == original_capabilities
    with sqlite3.connect(sqlite_url.removeprefix('sqlite:///')) as raw_db:
        after_capabilities = raw_db.execute(
            'SELECT capabilities FROM provider_connections WHERE id=?',
            (connection_id,)).fetchone()[0]
    assert after_capabilities == before_capabilities


@pytest.mark.asyncio
async def test_native_strict_unsupported_terminal_stays_known_false():
    from app.services import connections

    config = connections.ConnectionConfig(
        93, 'google-vertex', 'gemini-native', None, None,
        {'declared': {'temperature': True}, 'verified': {}},
        provider_id='google-vertex', protocol_id='vertex_generate_content',
        auth_method_id='native_chain', public_config_version=1,
        public_fields={'project_id': 'potato-project', 'location': 'us-central1'},
        auth_grant_revision=4, credential_state='present',
        credential_ref='1:93:' + '66' * 16, owner_id=1, owner_epoch='11' * 32,
        config_revision=3, public_config_fingerprint='22' * 32,
        descriptor_revision=1,
    )
    core = _native_test_core()

    class UnsupportedExecution:
        request_id = 2

        def result(self, timeout=None):
            assert timeout is None
            core.events.append('terminal')
            return {
                'ok': False, 'error_code': 'unsupported_option',
                'text': '', 'tool_calls': [], 'physical_model_request_count': 1,
            }

        def cancel(self):
            core.events.append('cancel')

    normal_begin = core.begin_execute

    def begin_execute(handle, call):
        normal_begin(handle, call)
        return UnsupportedExecution()

    core.begin_execute = begin_execute
    budget = connections.PhysicalRequestBudget(1)

    options = await connections.probe_capabilities(
        config, None, core_transport=core, physical_request_budget=budget)

    assert options['temperature'] is False
    assert budget.actual_count == 1
    assert len(core.calls) == 1


def test_partial_versioned_metadata_fails_before_secret_store_access_and_legacy_row_stays_lossless(setup):
    from app.services import connections

    client, _, _, _ = setup
    reads = []
    store = SimpleNamespace(get_no_ui=lambda reference: reads.append(reference) or 'must-not-read')
    with client.app.state.session_factory() as db:
        owner_id = db.query(User).filter_by(username='owner').one().id
        partial = ProviderConnection(
            user_id=owner_id, kind='compatible', name='Partial typed row',
            base_url=None, secret_ref='partial-secret-ref', default_model_id='profile-model',
            enabled=True, auth_required=True, capabilities={}, provider_id='openrouter',
            protocol_id=None, auth_method_id='api_key', public_config_version=1,
            public_config={}, auth_grant_revision=1, credential_state='present',
        )
        legacy = ProviderConnection(
            user_id=owner_id, kind='openai', name='Legacy row', base_url=None,
            secret_ref='legacy-secret-ref', default_model_id='legacy-model', enabled=True,
            auth_required=True, capabilities={},
        )
        db.add_all((partial, legacy))
        db.commit()

        with pytest.raises(connections.ConnectionFailure) as invalid:
            connections.resolve_connection(db, partial.id, owner_id, store=store)
        legacy_config, legacy_reference = connections._connection_metadata(db, legacy.id, owner_id)

    assert invalid.value.code == 'invalid_configuration'
    assert reads == []
    assert legacy_reference == 'legacy-secret-ref'
    assert legacy_config.provider_kind == 'openai'
    assert legacy_config.provider_id is legacy_config.protocol_id is legacy_config.auth_method_id is None
    assert legacy_config.public_config_version is None
    assert legacy_config.public_fields is None
    assert legacy_config.auth_grant_revision is None
    assert legacy_config.credential_state is None


def test_persisted_typed_auth_semantics_fail_before_secret_read_and_keep_valid_modes(setup):
    from app.services import connections

    client, _, _, _ = setup
    reads = []
    store = SimpleNamespace(get_no_ui=lambda reference: reads.append(reference) or 'must-not-read')
    with client.app.state.session_factory() as db:
        owner_id = db.query(User).filter_by(username='owner').one().id
        invalid_local = ProviderConnection(
            user_id=owner_id, kind='compatible', name='Invalid local auth row',
            base_url='http://localhost:11434/v1', secret_ref='invalid-local-secret-ref',
            default_model_id='local/model', enabled=True, auth_required=True, capabilities={},
            provider_id='ollama', protocol_id='openai_chat_completions',
            auth_method_id='local_none', public_config_version=1, public_config={},
            auth_grant_revision=0, credential_state='not_required',
        )
        valid_local = ProviderConnection(
            user_id=owner_id, kind='compatible', name='Valid local row',
            base_url='http://localhost:11434/v1', secret_ref=None,
            default_model_id='local/model', enabled=True, auth_required=False, capabilities={},
            provider_id='ollama', protocol_id='openai_chat_completions',
            auth_method_id='local_none', public_config_version=1, public_config={},
            auth_grant_revision=0, credential_state='not_required',
        )
        valid_api_key = ProviderConnection(
            user_id=owner_id, kind='compatible', name='Valid key row',
            base_url='https://openrouter.ai/api/v1', secret_ref='valid-api-key-ref',
            default_model_id='vendor/model', enabled=True, auth_required=True, capabilities={},
            provider_id='openrouter', protocol_id='openai_chat_completions',
            auth_method_id='api_key', public_config_version=1, public_config={},
            auth_grant_revision=1, credential_state='present',
        )
        legacy = ProviderConnection(
            user_id=owner_id, kind='openai', name='Valid legacy row', base_url=None,
            secret_ref='valid-legacy-ref', default_model_id='legacy-model', enabled=True,
            auth_required=True, capabilities={},
        )
        db.add_all((invalid_local, valid_local, valid_api_key, legacy))
        db.commit()

        with pytest.raises(connections.ConnectionFailure) as invalid:
            connections.resolve_connection(db, invalid_local.id, owner_id, store=store)
        assert invalid.value.code == 'invalid_configuration'
        with pytest.raises(connections.ConnectionFailure) as invalid_discovery:
            asyncio.run(connections.resolve_discovery_connection_async(
                db, invalid_local.id, owner_id, store=store))
        assert invalid_discovery.value.code == 'invalid_configuration'
        assert connections._connection_metadata(db, valid_local.id, owner_id)[1] is None
        assert connections._connection_metadata(db, valid_api_key.id, owner_id)[1] == 'valid-api-key-ref'
        assert connections._connection_metadata(db, legacy.id, owner_id)[1] == 'valid-legacy-ref'

    assert reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize('provider_id,protocol_id,auth_method_id', [
    ('openai', 'openai_chat_completions', 'oauth_browser'),
    ('ollama', 'openai_chat_completions', 'api_key'),
    ('anthropic', 'openai_chat_completions', 'api_key'),
    ('openrouter', None, 'api_key'),
])
async def test_probe_rejects_exact_auth_protocol_and_partial_metadata_before_transport(
        provider_id, protocol_id, auth_method_id):
    from app.services import connections

    calls = []
    config = connections.ConnectionConfig(
        61, provider_id, 'profile-model', None, 'profile-key', {},
        provider_id=provider_id, protocol_id=protocol_id,
        auth_method_id=auth_method_id, public_config_version=1, public_fields={},
        auth_grant_revision=1, credential_state='present',
    )
    with pytest.raises(connections.ConnectionFailure) as invalid:
        await connections.probe(config, httpx.MockTransport(
            lambda wire: calls.append(wire) or httpx.Response(200, json={}),
        ))

    assert invalid.value.code == 'invalid_configuration'
    assert calls == []


@pytest.mark.asyncio
async def test_probe_rejects_non_loopback_localhost_resolution_before_transport(monkeypatch):
    from app.services import connections

    calls = []
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('203.0.113.10', 11434)),
    ])
    config = connections.ConnectionConfig(
        62, 'ollama', 'profile-model', None, None, {}, provider_id='ollama',
        protocol_id='openai_chat_completions', auth_method_id='local_none',
        public_config_version=1, public_fields={}, auth_grant_revision=1,
        credential_state='not_required',
    )
    with pytest.raises(connections.ConnectionFailure) as invalid:
        await connections.probe(config, httpx.MockTransport(
            lambda wire: calls.append(wire) or httpx.Response(200, json={}),
        ))

    assert invalid.value.code == 'invalid_configuration'
    assert calls == []


def test_legacy_compatible_coding_plan_origin_is_preserved_without_reclassification(setup, personal_agent_core):
    client, _, headers, _ = setup
    client.app.state.secret_store = personal_agent_core[0]
    base_url = 'https://coding-intl.dashscope.aliyuncs.com/v1'
    response = client.post('/api/v1/connections', headers=headers(), json={
        'provider_kind': 'compatible', 'label': 'Existing Coding Plan',
        'base_url': base_url, 'default_model_id': 'qwen3.7-plus',
        'api_key': 'legacy-coding-plan-key',
    })

    assert response.status_code == 201
    assert response.json()['provider_kind'] == 'compatible'
    assert response.json()['base_url'] == base_url


def test_probe_typed_failures_and_redirect_refusal(setup):
    client, secrets, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(), json={'provider_kind': 'compatible', 'label': 'Local', 'base_url': 'http://localhost:7777/v1', 'allow_local_http': True, 'default_model_id': 'one', 'api_key': 'secret-test-key'}).json()
    path = f"/api/v1/connections/{created['id']}/test"
    def response(status):
        return httpx.MockTransport(lambda request: httpx.Response(status, json={'error': 'secret-test-key'}, headers={'Location': 'https://evil.example'}))
    client.app.state.connection_transport = response(401)
    assert client.post(path, headers=headers()).json()['code'] == 'invalid_credential'
    client.app.state.connection_transport = response(302)
    assert client.post(path, headers=headers()).json()['code'] == 'redirect_refused'
    def unavailable(request):
        raise httpx.ConnectError('secret-test-key', request=request)
    client.app.state.connection_transport = httpx.MockTransport(unavailable)
    assert client.post(path, headers=headers()).json()['code'] == 'unreachable'
    secrets.values.clear()
    with client.app.state.session_factory() as db:
        ref = db.get(ProviderConnection, created['id']).secret_ref
    client.app.state.credential_cache.clear((1, created['id'], ref))
    assert client.post(path, headers=headers()).json()['code'] == 'missing_secret'
    assert 'secret-test-key' not in str(client.post(path, headers=headers()).json())


def test_validation_never_echoes_rejected_key(setup):
    client, _, headers, _ = setup
    response = client.post('/api/v1/connections', headers=headers(), json={'provider_kind': 'openai', 'label': 'Bad', 'default_model_id': 'model', 'api_key': ['raw-test-secret']})
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'invalid_request'
    assert 'raw-test-secret' not in response.text
    response = client.post('/api/v1/connections', headers=headers(), json={'provider_kind': 'compatible', 'label': 'Bad', 'default_model_id': 'model', 'api_key': 'raw-test-secret', 'base_url': 'https://user:raw-test-secret@example.com/v1'})
    assert response.status_code == 422
    assert 'raw-test-secret' not in response.text


def test_revision_four_preserves_existing_connection(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from app.core import migrate as migration
    path = tmp_path / 'preserve.sqlite'
    monkeypatch.setenv('GAMJA_APP_DATA_DIR', str(tmp_path / 'appdata'))
    engine = create_engine(f'sqlite:///{path}')
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, 'REVISIONS', migration.REVISIONS[:3])
        migration.migrate_database(engine)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO users(id,email,username,hashed_password) VALUES (1,'a@b.test','a','unused')")
        db.execute("INSERT INTO provider_connections(id,user_id,kind,name,base_url,secret_ref) VALUES (7,1,'openai','old',NULL,'opaque-ref')")
        before = db.execute('SELECT id,user_id,kind,name,base_url,secret_ref FROM provider_connections').fetchall()
    migration.migrate_database(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT id,user_id,kind,name,base_url,secret_ref FROM provider_connections').fetchall() == before
        assert db.execute('SELECT version FROM schema_version ORDER BY version').fetchall() == [(n,) for n in range(1, len(migration.REVISIONS) + 1)]
        assert db.execute('SELECT default_model_id,enabled,auth_required,capabilities FROM provider_connections').fetchone() == ('', 0, 1, '{}')


def test_store_failure_rolls_back_and_db_failure_restores_key(setup):
    from app.schemas.connection import ConnectionCreate
    from app.services import connections
    from app.services.secrets import SecretStoreUnavailable
    client, store, headers, _ = setup
    db = client.app.state.session_factory()
    owner = db.query(User).filter_by(username='owner').first()
    data = ConnectionCreate(provider_kind='openai', label='Save', default_model_id='gpt-4o-mini', api_key='first-secret')
    original_put = store.put
    def reject(ref, value):
        raise SecretStoreUnavailable()
    store.put = reject
    with pytest.raises(SecretStoreUnavailable):
        connections.create(db, owner.id, data, store)
    assert db.query(connections.ProviderConnection).count() == 0
    store.put = original_put
    row = connections.create(db, owner.id, data, store)
    reference = row.secret_ref
    real_commit = db.commit
    def reject_commit():
        raise RuntimeError('synthetic commit failure')
    db.commit = reject_commit
    with pytest.raises(connections.ConnectionFailure, match='storage_unavailable'):
        connections.update(db, row, {'api_key': 'replacement-secret'}, store)
    assert store.get(reference) == 'first-secret'
    assert 'replacement-secret' not in store.values.values()
    with pytest.raises(RuntimeError, match='synthetic commit failure'):
        connections.delete(db, row, store)
    assert store.get(reference) == 'first-secret'
    db.commit = real_commit
    db.close()
    assert client.get('/api/v1/connections', headers=headers()).json()[0]['label'] == 'Save'


def test_bound_agent_refuses_delete_and_secret_removed_after_unbind(setup):
    client, store, headers, url = setup
    row = client.post('/api/v1/connections', headers=headers(), json={'provider_kind': 'openai', 'label': 'Bound', 'default_model_id': 'gpt-4o-mini', 'api_key': 'delete-secret'}).json()
    with sqlite3.connect(url.removeprefix('sqlite:///')) as db:
        db.execute('INSERT INTO agents(user_id,name,description,system_prompt,model,temperature,max_tokens,is_active,connection_id) VALUES (1,\'Agent\',\'desc\',\'prompt\',\'old\',\'0.7\',100,1,?)', (row['id'],))
    denied = client.delete(f"/api/v1/connections/{row['id']}", headers=headers())
    assert denied.status_code == 409 and denied.json()['detail']['code'] == 'connection_in_use'
    with sqlite3.connect(url.removeprefix('sqlite:///')) as db:
        db.execute('UPDATE agents SET connection_id=NULL')
    assert client.delete(f"/api/v1/connections/{row['id']}", headers=headers()).status_code == 204
    assert 'delete-secret' not in store.values.values()


@pytest.mark.parametrize('kind,expected_path,body', [
    ('openai', '/v1/chat/completions', {'choices': [{'message': {'content': 'OK'}}]}),
    ('anthropic', '/v1/messages', {'content': [{'type': 'text', 'text': 'OK'}]}),
    ('gemini', '/v1beta/models/model:generateContent', {'candidates': [{'content': {'parts': [{'text': 'OK'}]}}]}),
    ('xai', '/v1/chat/completions', {'choices': [{'message': {'content': 'OK'}}]}),
])
def test_provider_probe_uses_exact_kind_and_model_with_fake_transport(setup, kind, expected_path, body):
    client, _, headers, _ = setup
    row = client.post('/api/v1/connections', headers=headers(), json={'provider_kind': kind, 'label': kind, 'default_model_id': 'model', 'api_key': 'probe-secret'}).json()
    requests = []
    def handler(request):
        requests.append(request)
        assert request.url.path == expected_path
        assert json.loads(request.content).get('model') == 'model' or kind == 'gemini'
        assert request.headers.get('authorization') == 'Bearer probe-secret' if kind in ('openai', 'xai') else request.headers.get('x-api-key' if kind == 'anthropic' else 'x-goog-api-key') == 'probe-secret'
        return httpx.Response(200, json=body)
    client.app.state.connection_transport = httpx.MockTransport(handler)
    status = client.post(f"/api/v1/connections/{row['id']}/test", headers=headers()).json()
    assert status['ok'] is True and len(requests) == 1
    assert 'probe-secret' not in str(status)


def test_slash_model_id_round_trips_through_connection_test_and_shared_schemas(setup):
    from pydantic import ValidationError
    from app.schemas.agent import AgentCreate
    from app.schemas.connection import ConnectionCreate, ConnectionTestPreview
    from app.schemas.starter import StarterAgentIntent
    from app.schemas.workspace import BlueprintAgent, BlueprintConnectionV1

    client, _, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(), json={
        'provider_kind': 'compatible', 'label': 'Slash model',
        'base_url': 'http://127.0.0.1:8765/v1', 'allow_local_http': True,
        'auth_required': False, 'default_model_id': 'vendor/default-model',
    })
    assert created.status_code == 201, created.text
    client.app.state.connection_transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}]}))
    tested = client.post(
        f"/api/v1/connections/{created.json()['id']}/test",
        headers=headers(), params={'model_id': 'vendor/exact-model', 'profile': 'text'})
    assert tested.status_code == 200, tested.text
    assert tested.json()['model_id'] == 'vendor/exact-model'

    assert ConnectionCreate(
        provider_kind='compatible', label='c', default_model_id='vendor/model',
        base_url='https://example.test/v1', api_key='x').default_model_id == 'vendor/model'
    assert ConnectionTestPreview(
        profile='text', model_id='vendor/model', max_request_count=1).model_id == 'vendor/model'
    assert AgentCreate(
        name='a', description='d', system_prompt='s', connection_id=1,
        model_id='vendor/model').model_id == 'vendor/model'
    assert StarterAgentIntent(
        role='proposer', name='a', connection_id=1,
        model_id='vendor/model').model_id == 'vendor/model'
    assert BlueprintConnectionV1(
        ref='connection', provider_kind='compatible', label='c',
        default_model_id='vendor/model').default_model_id == 'vendor/model'
    assert BlueprintAgent(
        ref='agent', name='a', description='d', system_prompt='s',
        connection_ref='connection', model_id='vendor/model', temperature='auto',
        max_tokens=0).model_id == 'vendor/model'
    for invalid in (
        'vendor model', 'vendor\\model', 'vendor/model?key=x', 'vendor/model\n',
        '.', '..', '/model', 'vendor//model', 'vendor/../model', 'vendor/model/',
    ):
        with pytest.raises(ValidationError):
            ConnectionTestPreview(profile='text', model_id=invalid, max_request_count=1)


def test_gemini_slash_model_id_is_one_percent_encoded_resource_segment(setup):
    client, _, headers, _ = setup
    row = client.post('/api/v1/connections', headers=headers(), json={
        'provider_kind': 'gemini', 'label': 'Gemini slash',
        'default_model_id': 'publisher/default', 'api_key': 'synthetic-key',
    })
    assert row.status_code == 201, row.text

    def handler(request):
        assert request.url.raw_path == b'/v1beta/models/publisher%2Fexact:generateContent'
        return httpx.Response(200, json={
            'candidates': [{'content': {'parts': [{'text': 'OK'}]}}],
        })

    client.app.state.connection_transport = httpx.MockTransport(handler)
    tested = client.post(
        f"/api/v1/connections/{row.json()['id']}/test",
        headers=headers(), params={'model_id': 'publisher/exact', 'profile': 'text'})
    assert tested.status_code == 200, tested.text
    assert tested.json()['ok'] is True


@pytest.mark.parametrize('invalid_model_id', (
    '..', 'vendor/../model', 'vendor//model', 'vendor/model?key=x',
    'vendor\\model', 'vendor/model\n',
))
def test_seeded_legacy_default_model_is_rejected_before_secret_or_transport(
        setup, invalid_model_id):
    client, _, headers, _ = setup
    reads, calls = [], []

    class CountingSecrets:
        def get_no_ui(self, reference):
            reads.append(reference)
            return 'must-not-be-read'

    with client.app.state.session_factory() as db:
        owner_id = db.query(User).filter_by(username='owner').one().id
        row = ProviderConnection(
            user_id=owner_id, kind='openai', name='Legacy invalid model',
            secret_ref='legacy-invalid-model-ref', default_model_id=invalid_model_id,
            enabled=True, auth_required=True, capabilities={},
        )
        db.add(row)
        db.commit()
        connection_id = row.id

    client.app.state.secret_store = CountingSecrets()
    client.app.state.connection_transport = httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(
            200, json={'choices': [{'message': {'content': 'must-not-send'}}]}))

    response = client.post(
        f'/api/v1/connections/{connection_id}/test', headers=headers())

    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'invalid_request'
    assert reads == [] and calls == []


def test_explicit_empty_test_model_never_falls_back_or_opens_secret_transport(setup):
    client, _, headers, _ = setup
    reads, calls = [], []

    class CountingSecrets:
        def get_no_ui(self, reference):
            reads.append(reference)
            return 'must-not-be-read'

    with client.app.state.session_factory() as db:
        owner_id = db.query(User).filter_by(username='owner').one().id
        row = ProviderConnection(
            user_id=owner_id, kind='openai', name='Explicit empty model',
            secret_ref='explicit-empty-model-ref', default_model_id='valid-default',
            enabled=True, auth_required=True, capabilities={},
        )
        db.add(row)
        db.commit()
        connection_id = row.id

    client.app.state.secret_store = CountingSecrets()
    client.app.state.connection_transport = httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(
            200, json={'choices': [{'message': {'content': 'must-not-send'}}]}))

    response = client.post(
        f'/api/v1/connections/{connection_id}/test', headers=headers(),
        params={'model_id': ''})

    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'invalid_request'
    assert reads == [] and calls == []


def test_connection_metadata_distinguishes_empty_override_from_none_fallback(setup):
    from app.services import connections

    client, _, _, _ = setup
    with client.app.state.session_factory() as db:
        owner_id = db.query(User).filter_by(username='owner').one().id
        row = ProviderConnection(
            user_id=owner_id, kind='openai', name='Override semantics',
            secret_ref='override-semantics-ref', default_model_id='valid-default',
            enabled=True, auth_required=True, capabilities={},
        )
        db.add(row)
        db.commit()

        fallback, reference = connections._connection_metadata(
            db, row.id, owner_id, model_id_override=None)
        with pytest.raises(connections.ConnectionFailure) as empty:
            connections._connection_metadata(
                db, row.id, owner_id, model_id_override='')

    assert fallback.model_id == 'valid-default'
    assert reference == 'override-semantics-ref'
    assert empty.value.code == 'missing_model'


@pytest.mark.asyncio
@pytest.mark.parametrize(('agent_model_id', 'expected_code'), (
    ('vendor/../model', 'invalid_configuration'),
    ('', 'provider_not_configured'),
))
async def test_seeded_legacy_agent_model_is_rejected_before_secret_or_inference_transport(
        setup, agent_model_id, expected_code):
    from app.models.collaboration import RoomEvent
    from app.models.models import Agent, Conversation, Project
    from app.services.llm_service import LLMFailure, LLMService

    client, _, _, _ = setup
    reads, calls = [], []

    class CountingSecrets:
        def get_no_ui(self, reference):
            reads.append(reference)
            return 'must-not-be-read'

    with client.app.state.session_factory() as db:
        owner_id = db.query(User).filter_by(username='owner').one().id
        connection = ProviderConnection(
            user_id=owner_id, kind='openai', name='Legacy Agent connection',
            secret_ref='legacy-agent-ref', default_model_id='valid-default',
            enabled=True, auth_required=True, capabilities={},
        )
        project = Project(user_id=owner_id, name='Legacy Agent project', description='')
        db.add_all((connection, project))
        db.flush()
        room = Conversation(user_id=owner_id, project_id=project.id, title='Legacy Agent room')
        agent = Agent(
            user_id=owner_id, name='Legacy Agent', description='', system_prompt='Reply.',
            connection_id=connection.id, model_id=agent_model_id,
            temperature='auto', max_tokens=0,
        )
        db.add_all((room, agent))
        db.flush()
        db.add(RoomEvent(
            event_id='11111111-1111-1111-1111-111111111111', room_id=room.id,
            seq=1, actor_type='user', actor_id=owner_id, actor_name_snapshot='Owner',
            kind='message', payload={'content': 'hello'}, created_at=datetime.now(UTC),
        ))
        db.commit()
        agent._room_id = room.id
        agent._provider_store = CountingSecrets()
        agent._provider_transport = httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(
                200, json={'choices': [{'message': {'content': 'must-not-send'}}]}))

        with pytest.raises(LLMFailure) as invalid:
            await LLMService().generate_response(agent, [], 'hello')

    assert invalid.value.code == expected_code
    assert reads == [] and calls == []


def test_authless_local_probe_never_opens_secret_store(setup, monkeypatch):
    client, _, headers, _ = setup
    client.app.state.secret_store = None
    from app.services import connections
    def denied_store():
        raise AssertionError('OS secret store must not be opened')
    monkeypatch.setattr(connections, 'PlatformSecretStore', denied_store)
    row = client.post('/api/v1/connections', headers=headers(), json={'provider_kind': 'compatible', 'label': 'Local authless', 'base_url': 'http://127.0.0.1:7654/v1', 'allow_local_http': True, 'auth_required': False, 'default_model_id': 'local'}).json()
    assert row['auth_required'] is False
    def handler(request):
        assert 'authorization' not in request.headers
        assert 'max_tokens' not in json.loads(request.content)
        return httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}]})
    client.app.state.connection_transport = httpx.MockTransport(handler)
    assert client.post(f"/api/v1/connections/{row['id']}/test", headers=headers()).json()['ok'] is True


@pytest.mark.asyncio
async def test_request_meter_blocks_n_plus_one_before_physical_transport():
    from app.services import connections

    physical_calls = []
    budget = connections.PhysicalRequestBudget(1)
    meter = connections.RequestMeter(
        httpx.MockTransport(lambda request: physical_calls.append(request)
                            or httpx.Response(200)),
        physical_request_budget=budget)
    request = httpx.Request('POST', 'https://provider.example/v1/model')
    assert (await meter.handle_async_request(request)).status_code == 200
    with pytest.raises(connections.ConnectionFailure):
        await meter.handle_async_request(request)
    assert len(physical_calls) == 1
    assert budget.actual_count == 1


def test_custom_capabilities_require_declaration_exact_probe_and_reverification(setup):
    client, _, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(), json={
        'provider_kind': 'compatible', 'label': 'Local options', 'base_url': 'http://127.0.0.1:8765/v1',
        'allow_local_http': True, 'auth_required': False, 'default_model_id': 'first',
        'capabilities': {'max_tokens': True, 'temperature': True, 'tools': True, 'vision': True}})
    assert created.status_code == 201, created.text
    connection_id = created.json()['id']
    seen = []
    vision_calls = []
    def handler(wire):
        body = json.loads(wire.content)
        seen.append(body)
        if 'tools' in body:
            observations = [item for item in body['messages'] if item['role'] == 'tool']
            if observations:
                assert len(observations) == 1 and observations[0]['tool_call_id'] == 'probe-call'
                assert 'probe' in observations[0]['content'].lower()
                return httpx.Response(200, json={'choices': [{'message': {'content': 'Probe complete.'}}]})
            name = body['tools'][0]['function']['name']
            return httpx.Response(200, json={'choices': [{'message': {'content': None, 'tool_calls': [
                {'id': 'probe-call', 'type': 'function', 'function': {'name': name, 'arguments': '{}'}}]}}]})
        if any('image_url' in part for message in body['messages'] for part in (message['content'] if isinstance(message['content'], list) else [])):
            vision_calls.append(body)
            return httpx.Response(200, json={'choices': [{'message': {'content': 'red' if len(vision_calls) == 1 else 'blue'}}]})
        return httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}]})
    client.app.state.connection_transport = httpx.MockTransport(handler)
    tested = client.post(f'/api/v1/connections/{connection_id}/test?model_id=second', headers=headers())
    assert tested.status_code == 200 and tested.json()['ok'] is True, tested.text
    assert tested.json()['profile'] == 'full'
    assert tested.json()['max_request_count'] == tested.json()['actual_request_count'] == 7
    assert tested.json()['record']['profile'] == 'full'
    assert tested.json()['record']['actual_request_count'] == 7
    saved = client.get('/api/v1/connections', headers=headers()).json()[0]['capabilities']
    second = saved['verified']['models']['second']
    assert all(second[option] for option in ('max_tokens', 'temperature', 'tools', 'vision'))
    assert 'first' not in saved['verified']['models']
    assert len(vision_calls) == 2
    assert len([body for body in seen if any(item['role'] == 'tool' for item in body['messages'])]) == 1
    assert {body['model'] for body in seen} == {'second'}
    changed = client.patch(f'/api/v1/connections/{connection_id}', headers=headers(), json={'base_url': 'http://127.0.0.1:9999/v1', 'allow_local_http': True})
    assert changed.status_code == 200 and changed.json()['capabilities']['verified'] == {}


@pytest.mark.parametrize('continuation', ['rejected', 'malformed'])
def test_custom_tool_probe_requires_successful_continuation_and_clears_stale_verification(setup, continuation):
    client, _, headers, _ = setup
    row = client.post('/api/v1/connections', headers=headers(), json={
        'provider_kind': 'compatible', 'label': 'Partial tools', 'base_url': 'http://127.0.0.1:8765/v1',
        'allow_local_http': True, 'auth_required': False, 'default_model_id': 'exact',
        'capabilities': {'tools': True}}).json()
    path = f"/api/v1/connections/{row['id']}/test"
    requests = []
    fail_continuation = False
    def handler(wire):
        nonlocal fail_continuation
        body = json.loads(wire.content)
        if 'tools' not in body:
            return httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}]})
        requests.append(body)
        if any(item['role'] == 'tool' for item in body['messages']):
            assert [item['tool_call_id'] for item in body['messages'] if item['role'] == 'tool'] == ['probe-call']
            if fail_continuation:
                return (httpx.Response(400, json={'error': 'follow-up rejected'}) if continuation == 'rejected'
                        else httpx.Response(200, json={'choices': [{'message': {'content': None}}]}))
            return httpx.Response(200, json={'choices': [{'message': {'content': 'Probe complete.'}}]})
        name = body['tools'][0]['function']['name']
        return httpx.Response(200, json={'choices': [{'message': {'content': None, 'tool_calls': [
            {'id': 'probe-call', 'type': 'function', 'function': {'name': name, 'arguments': '{}'}}]}}]})
    client.app.state.connection_transport = httpx.MockTransport(handler)
    assert client.post(path, headers=headers()).json()['verified']['tools'] is True
    fail_continuation = True
    tested = client.post(path, headers=headers())
    assert tested.status_code == 200 and tested.json()['verified']['tools'] is False
    saved = client.get('/api/v1/connections', headers=headers()).json()[0]['capabilities']
    assert saved['declared']['tools'] is True
    assert saved['verified']['models']['exact']['tools'] is False
    assert len(requests) == 4


@pytest.mark.asyncio
async def test_async_connection_test_does_not_block_event_loop_on_secret_store(setup):
    client, original, headers, _ = setup
    row = client.post('/api/v1/connections', headers=headers(), json={
        'provider_kind': 'openai', 'label': 'Slow key read', 'default_model_id': 'exact',
        'api_key': 'synthetic-key'}).json()
    blocking = BlockingSecrets()
    blocking.values.update(original.values)
    client.app.state.secret_store = blocking
    client.app.state.connection_transport = httpx.MockTransport(
        lambda wire: httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}]}))
    from app.api.routes.connections import test_connection as route_test
    with client.app.state.session_factory() as db:
        user = db.query(User).filter_by(username='owner').one()
        task = asyncio.create_task(route_test(row['id'], SimpleNamespace(app=client.app), db=db, user=user))
        guard = threading.Timer(2, blocking.release.set)
        guard.start()
        try:
            # The route has entered a blocking OS-store read. This unrelated coroutine
            # must run before the guard releases that read.
            assert await asyncio.to_thread(blocking.entered.wait, 1)
            assert not blocking.release.is_set()
            assert not db.in_transaction()
        finally:
            blocking.release.set()
            guard.cancel()
            guard.join()
        assert (await task)['ok'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('invalidate', [lambda cache: cache.clear_all(), lambda cache: cache.clear_owner(1)])
async def test_late_no_ui_read_cannot_refill_cache_after_global_invalidation(setup, invalidate):
    from app.services.connections import resolve_connection_async

    client, original, headers, _ = setup
    row = client.post('/api/v1/connections', headers=headers(), json={
        'provider_kind': 'openai', 'label': 'Race key', 'default_model_id': 'exact', 'api_key': 'synthetic-key'}).json()
    entered, release = threading.Event(), threading.Event()

    class BlockingSecrets(FakeSecrets):
        def get_no_ui(self, ref):
            entered.set()
            release.wait(2)
            return super().get_no_ui(ref)

    blocking = BlockingSecrets()
    blocking.values.update(original.values)
    with client.app.state.session_factory() as db:
        task = asyncio.create_task(resolve_connection_async(db, row['id'], 1, blocking,
                                                             cache=client.app.state.credential_cache))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            invalidate(client.app.state.credential_cache)
            release.set()
            assert (await task).api_key == 'synthetic-key'
            with client.app.state.session_factory() as check:
                ref = check.get(ProviderConnection, row['id']).secret_ref
            assert client.app.state.credential_cache.get((1, row['id'], ref)) is None
        finally:
            release.set()


@pytest.mark.parametrize('invalidate', [lambda cache: cache.clear_all(), lambda cache: cache.clear_owner(1)])
def test_sync_late_no_ui_read_cannot_refill_cache_after_global_invalidation(setup, invalidate):
    from app.services import connections

    client, original, headers, _ = setup
    row = client.post('/api/v1/connections', headers=headers(), json={
        'provider_kind': 'openai', 'label': 'Sync race key', 'default_model_id': 'exact', 'api_key': 'synthetic-key'}).json()
    entered, release = threading.Event(), threading.Event()

    class BlockingSecrets(FakeSecrets):
        def get_no_ui(self, ref):
            entered.set()
            release.wait(2)
            return super().get_no_ui(ref)

    blocking = BlockingSecrets()
    blocking.values.update(original.values)
    result = {}

    def resolve():
        try:
            with client.app.state.session_factory() as db:
                result['config'] = connections.resolve_connection(
                    db, row['id'], 1, blocking, cache=client.app.state.credential_cache)
        except Exception as error:
            result['error'] = error

    worker = threading.Thread(target=resolve)
    worker.start()
    try:
        assert entered.wait(1)
        invalidate(client.app.state.credential_cache)
        release.set()
        worker.join(2)
        assert not worker.is_alive()
        assert 'error' not in result
        assert result['config'].api_key == 'synthetic-key'
        with client.app.state.session_factory() as check:
            ref = check.get(ProviderConnection, row['id']).secret_ref
        assert client.app.state.credential_cache.get((1, row['id'], ref)) is None
    finally:
        release.set()
        worker.join(2)


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [
    {'capabilities': {'temperature': False}},
    {'api_key': 'rotated-synthetic-key', 'default_model_id': 'new-exact'},
])
async def test_delayed_probe_cannot_restore_changed_configuration(setup, changes):
    client, _, headers, _ = setup
    row = client.post('/api/v1/connections', headers=headers(), json={
        'provider_kind': 'compatible', 'label': 'Race', 'base_url': 'http://127.0.0.1:8765/v1',
        'allow_local_http': True, 'default_model_id': 'exact', 'api_key': 'original-synthetic-key',
        'capabilities': {'temperature': True}}).json()
    path = f"/api/v1/connections/{row['id']}"
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(wire):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}]})
    client.app.state.connection_transport = httpx.MockTransport(handler)
    from app.api.routes.connections import test_connection as route_test
    with client.app.state.session_factory() as db:
        user = db.query(User).filter_by(username='owner').one()
        running = asyncio.create_task(route_test(row['id'], SimpleNamespace(app=client.app), db=db, user=user))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            edited = await asyncio.wait_for(asyncio.to_thread(client.patch, path, headers=headers(), json=changes), 2)
            assert edited.status_code == 200, edited.text
        finally:
            release.set()
        outcome = await running
    assert outcome['ok'] is False and outcome['code'] == 'configuration_changed'
    saved = client.get('/api/v1/connections', headers=headers()).json()[0]
    assert saved['capabilities'] == edited.json()['capabilities']
    if 'capabilities' in changes:
        assert saved['capabilities']['declared']['temperature'] is False
    else:
        assert saved['default_model_id'] == 'new-exact'
        assert saved['capabilities']['verified'] == {}


@pytest.mark.asyncio
async def test_simultaneous_probes_cannot_overwrite_each_other(setup):
    client, _, headers, _ = setup
    row = client.post('/api/v1/connections', headers=headers(), json={
        'provider_kind': 'compatible', 'label': 'Probe race', 'base_url': 'http://127.0.0.1:8765/v1',
        'allow_local_http': True, 'auth_required': False, 'default_model_id': 'exact'}).json()
    entered, release = asyncio.Event(), asyncio.Event()
    seen = 0
    async def handler(wire):
        nonlocal seen
        seen += 1
        if seen == 2:
            entered.set()
        await release.wait()
        return httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}]})
    client.app.state.connection_transport = httpx.MockTransport(handler)
    from app.api.routes.connections import test_connection as route_test
    with client.app.state.session_factory() as first, client.app.state.session_factory() as second:
        user = first.query(User).filter_by(username='owner').one()
        tasks = [asyncio.create_task(route_test(row['id'], SimpleNamespace(app=client.app), model_id=model,
                                                db=db, user=user))
                 for model, db in [('first', first), ('second', second)]]
        try:
            await asyncio.wait_for(entered.wait(), 2)
        finally:
            release.set()
        results = await asyncio.gather(*tasks)
    assert sorted(result['ok'] for result in results) == [False, True]
    assert {result.get('code') for result in results if not result['ok']} == {'configuration_changed'}
    saved = client.get('/api/v1/connections', headers=headers()).json()[0]['capabilities']
    assert len(saved['verified']['models']) == 1


@pytest.mark.asyncio
async def test_deleted_connection_cannot_receive_delayed_probe_result(setup):
    client, _, headers, _ = setup
    row = client.post('/api/v1/connections', headers=headers(), json={
        'provider_kind': 'compatible', 'label': 'Delete race', 'base_url': 'http://127.0.0.1:8765/v1',
        'allow_local_http': True, 'auth_required': False, 'default_model_id': 'exact'}).json()
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(wire):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}]})
    client.app.state.connection_transport = httpx.MockTransport(handler)
    from app.api.routes.connections import test_connection as route_test
    with client.app.state.session_factory() as db:
        user = db.query(User).filter_by(username='owner').one()
        running = asyncio.create_task(route_test(row['id'], SimpleNamespace(app=client.app), db=db, user=user))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            deleted = await asyncio.wait_for(asyncio.to_thread(client.delete,
                f"/api/v1/connections/{row['id']}", headers=headers()), 2)
            assert deleted.status_code == 204
        finally:
            release.set()
        outcome = await running
    assert outcome['ok'] is False and outcome['code'] == 'configuration_changed'
    assert client.get('/api/v1/connections', headers=headers()).json() == []


@pytest.mark.asyncio
async def test_delayed_exact_test_losing_auth_grant_revision_records_stale_evidence(setup):
    client, secrets, _, _ = setup
    entered, release = asyncio.Event(), asyncio.Event()
    with client.app.state.session_factory() as db:
        owner = db.query(User).filter_by(username='owner').one()
        row = ProviderConnection(
            user_id=owner.id, kind='compatible', name='Grant race',
            base_url=None, secret_ref='grant-race-ref', default_model_id='vendor/model',
            enabled=True, auth_required=True, capabilities={'declared': {}, 'verified': {}},
            readiness_revision=7, provider_id='openrouter',
            protocol_id='openai_chat_completions', auth_method_id='api_key',
            public_config_version=1, public_config={}, auth_grant_revision=3,
            credential_state='present')
        db.add(row); db.commit(); db.refresh(row)
        connection_id, owner_id = row.id, owner.id
    secrets.values['grant-race-ref'] = 'synthetic-key'

    async def handler(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}]})

    client.app.state.connection_transport = httpx.MockTransport(handler)
    from app.api.routes.connections import test_connection as route_test
    with client.app.state.session_factory() as db:
        owner = db.get(User, owner_id)
        running = asyncio.create_task(route_test(
            connection_id, SimpleNamespace(app=client.app), profile='text', db=db, user=owner))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            with client.app.state.session_factory() as edit:
                edit.query(ProviderConnection).filter_by(id=connection_id).update({'auth_grant_revision': 4})
                edit.commit()
        finally:
            release.set()
        outcome = await running
    assert outcome['code'] == 'configuration_changed'
    assert outcome['actual_request_count'] == 1
    assert outcome['record']['config_current'] is False


def test_key_rotation_clears_exact_model_capability_verification(setup):
    client, _, headers, url = setup
    created = client.post('/api/v1/connections', headers=headers(), json={
        'provider_kind': 'openai', 'label': 'Rotating', 'default_model_id': 'model', 'api_key': 'first-test-key'}).json()
    with sqlite3.connect(url.removeprefix('sqlite:///')) as db:
        db.execute('UPDATE provider_connections SET capabilities=? WHERE id=?',
                   (json.dumps({'declared': {'tools': True}, 'verified': {'models': {'model': {'base_url': None, 'tools': True}}}}), created['id']))
    changed = client.patch(f"/api/v1/connections/{created['id']}", headers=headers(), json={'api_key': 'second-test-key'})
    assert changed.status_code == 200
    assert changed.json()['capabilities'] == {'declared': {'tools': True}, 'verified': {}}


def test_disabled_and_rate_limited_have_safe_status(setup):
    client, _, headers, _ = setup
    row = client.post('/api/v1/connections', headers=headers(), json={'provider_kind': 'openai', 'label': 'Disabled', 'default_model_id': 'gpt-4o-mini', 'api_key': 'status-secret', 'enabled': False}).json()
    path = f"/api/v1/connections/{row['id']}/test"
    assert client.post(path, headers=headers()).json()['code'] == 'connection_disabled'
    assert client.patch(f"/api/v1/connections/{row['id']}", headers=headers(), json={'enabled': True}).status_code == 200
    client.app.state.connection_transport = httpx.MockTransport(lambda request: httpx.Response(429, json={'error': 'status-secret'}))
    status = client.post(path, headers=headers()).json()
    assert status['code'] == 'rate_limited' and 'status-secret' not in str(status)


def test_opencode_go_test_uses_closed_headers_and_distinguishes_exact_usage_limit(setup):
    client, _, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(), json=_version_one_payload(
        provider_id='opencode-go', protocol_id='openai_chat_completions',
        label='Go test', default_model_id='glm-5.3-flash', fields={},
        api_key='synthetic-go-key', capabilities={},
    ))
    assert created.status_code == 201, created.text
    seen = []

    def go_limit(wire):
        seen.append(wire)
        return httpx.Response(429, json={
            'error': {'type': 'GoUsageLimitError', 'message': 'raw-upstream-plan-detail'},
        })

    client.app.state.connection_transport = httpx.MockTransport(go_limit)
    result = client.post(
        f"/api/v1/connections/{created.json()['id']}/test",
        headers=headers(), json={'profile': 'text'},
    )
    assert result.status_code == 200
    assert result.json()['code'] == 'quota_exhausted'
    assert result.json()['ok'] is False
    assert result.json()['record']['credential_observation'] == 'unknown'
    assert result.json()['record']['verified_capabilities'] is None
    assert 'raw-upstream-plan-detail' not in result.text
    assert len(seen) == 1
    assert str(seen[0].url) == 'https://opencode.ai/zen/go/v1/chat/completions'
    assert seen[0].headers['authorization'] == 'Bearer synthetic-go-key'
    assert seen[0].headers['user-agent'] == 'GamjaBot/1.0.0'
    assert len(seen[0].headers['x-opencode-session']) == 64

    client.app.state.connection_transport = httpx.MockTransport(
        lambda wire: httpx.Response(429, json={'code': 'AnotherLimit'}),
    )
    fallback = client.post(
        f"/api/v1/connections/{created.json()['id']}/test",
        headers=headers(), json={'profile': 'text'},
    )
    assert fallback.json()['code'] == 'rate_limited'


@pytest.mark.parametrize(('protocol_id', 'model_id'), [
    ('openai_chat_completions', 'qwen3.7-plus'),
    ('anthropic_messages', 'glm-5.3-flash'),
    ('openai_chat_completions', 'gpt-6-luna'),
    ('anthropic_messages', 'gpt-6-luna'),
])
def test_opencode_go_create_rejects_known_mismatch_and_responses_before_secret_store(
        setup, protocol_id, model_id):
    client, secrets, headers, _ = setup
    rejected = client.post('/api/v1/connections', headers=headers(), json=_version_one_payload(
        provider_id='opencode-go', protocol_id=protocol_id,
        label='Rejected Go', default_model_id=model_id, fields={},
        api_key='must-not-be-stored', capabilities={},
    ))
    assert rejected.status_code == 422
    assert rejected.json()['detail'] == {'code': 'invalid_model_protocol'}
    assert 'must-not-be-stored' not in secrets.values.values()


@pytest.mark.parametrize('protocol_id', [
    'openai_chat_completions', 'anthropic_messages',
])
def test_opencode_go_create_preserves_unknown_manual_model_explicit_protocol(
        setup, protocol_id):
    client, secrets, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(), json=_version_one_payload(
        provider_id='opencode-go', protocol_id=protocol_id,
        label='Future Go', default_model_id='future-owner-model', fields={},
        api_key='synthetic-future-key', capabilities={},
    ))
    assert created.status_code == 201, created.text
    assert created.json()['protocol_id'] == protocol_id
    assert created.json()['default_model_id'] == 'future-owner-model'
    assert 'synthetic-future-key' in secrets.values.values()


def test_crash_during_key_rotation_keeps_old_referenced_key(setup):
    from app.services import connections
    client, store, _, _ = setup
    db = client.app.state.session_factory()
    owner_id = db.query(User).filter_by(username='owner').one().id
    from app.schemas.connection import ConnectionCreate
    row = connections.create(db, owner_id, ConnectionCreate(provider_kind='openai', label='Rotating', default_model_id='model', api_key='old-key'), store)
    old_ref, row_id = row.secret_ref, row.id
    def crash():
        raise SystemExit('synthetic crash before DB commit')
    db.commit = crash
    with pytest.raises(SystemExit):
        connections.update(db, row, {'api_key': 'new-key'}, store)
    db.rollback()
    db.close()
    with client.app.state.session_factory() as fresh:
        assert fresh.query(connections.ProviderConnection).filter_by(id=row_id).one().secret_ref == old_ref
    assert store.get(old_ref) == 'old-key'
    assert 'new-key' in store.values.values()  # Orphan only; no active ref was overwritten.


def test_crash_during_delete_leaves_disabled_row_and_secret(setup):
    from app.services import connections
    client, store, _, _ = setup
    db = client.app.state.session_factory()
    owner_id = db.query(User).filter_by(username='owner').one().id
    from app.schemas.connection import ConnectionCreate
    row = connections.create(db, owner_id, ConnectionCreate(provider_kind='openai', label='Deleting', default_model_id='model', api_key='delete-key'), store)
    row_id, reference = row.id, row.secret_ref
    def crash(ref):
        raise SystemExit('synthetic crash after durable disable')
    store.delete = crash
    with pytest.raises(SystemExit):
        connections.delete(db, row, store)
    db.close()
    with client.app.state.session_factory() as fresh:
        persisted = fresh.query(connections.ProviderConnection).filter_by(id=row_id).one()
        assert persisted.enabled is False
        with pytest.raises(connections.ConnectionFailure) as error:
            connections.resolve_connection(fresh, row_id, owner_id, store)
        assert error.value.code == 'connection_disabled'
    assert store.get(reference) == 'delete-key'


def test_store_delete_failure_restores_enabled_row(setup):
    from app.services import connections
    from app.services.secrets import SecretStoreUnavailable
    client, store, _, _ = setup
    db = client.app.state.session_factory()
    owner_id = db.query(User).filter_by(username='owner').one().id
    from app.schemas.connection import ConnectionCreate
    row = connections.create(db, owner_id, ConnectionCreate(provider_kind='openai', label='Saved', default_model_id='model', api_key='saved-key'), store)
    row_id, reference = row.id, row.secret_ref
    store.delete = lambda ref: (_ for _ in ()).throw(SecretStoreUnavailable())
    with pytest.raises(SecretStoreUnavailable):
        connections.delete(db, row, store)
    db.close()
    with client.app.state.session_factory() as fresh:
        assert fresh.query(connections.ProviderConnection).filter_by(id=row_id).one().enabled is True
    assert store.get(reference) == 'saved-key'


def test_crash_after_delete_secret_removal_still_leaves_disabled_row(setup):
    from app.services import connections
    from app.schemas.connection import ConnectionCreate
    client, store, _, _ = setup
    db = client.app.state.session_factory()
    owner_id = db.query(User).filter_by(username='owner').one().id
    row = connections.create(db, owner_id, ConnectionCreate(provider_kind='openai', label='Deleting', default_model_id='model', api_key='delete-key'), store)
    row_id, reference = row.id, row.secret_ref
    real_commit = db.commit
    calls = 0
    def commit_then_crash():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SystemExit('synthetic crash after secret removal')
        real_commit()
    db.commit = commit_then_crash
    with pytest.raises(SystemExit):
        connections.delete(db, row, store)
    db.close()
    with client.app.state.session_factory() as fresh:
        assert fresh.query(connections.ProviderConnection).filter_by(id=row_id).one().enabled is False
    assert store.get(reference) is None


def test_unavailable_native_store_returns_safe_503(setup, monkeypatch):
    from app.services import connections
    from app.services.secrets import SecretStoreUnavailable
    client, _, headers, _ = setup
    del client.app.state.secret_store
    monkeypatch.setattr(connections, 'PlatformSecretStore', lambda: (_ for _ in ()).throw(SecretStoreUnavailable()))
    response = client.post('/api/v1/connections', headers=headers(), json={'provider_kind': 'openai', 'label': 'Unavailable', 'default_model_id': 'model', 'api_key': 'secret-in-request'})
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'secret_store_unavailable'
    assert 'secret-in-request' not in response.text


def test_native_store_failure_has_owner_scoped_restricted_diagnostic(setup):
    from app.services.secrets import SecretStoreUnavailable
    client, store, headers, _ = setup

    def reject(_ref, _value):
        raise SecretStoreUnavailable(
            'keychain_query_failed', -25293,
            operation='sec_item_add', error_class='native_os_status')

    store.put = reject
    response = client.post('/api/v1/connections', headers=headers(), json={
        'provider_kind': 'openai', 'label': 'Unavailable',
        'default_model_id': 'model', 'api_key': 'sentinel-provider-secret',
    })
    assert response.status_code == 503
    detail = response.json()['detail']
    assert set(detail) == {'ok', 'code', 'message', 'correlation_id'}
    assert detail['code'] == 'secret_store_unavailable'
    assert 'sentinel-provider-secret' not in response.text

    diagnostic = client.get(
        f"/api/v1/connections/diagnostics/{detail['correlation_id']}",
        headers=headers()).json()
    assert diagnostic == {
        'correlation_id': detail['correlation_id'],
        'stage': 'provider_credential',
        'code': 'secret_store_unavailable',
        'reason': 'keychain_query_failed',
        'error_class': 'native_os_status',
        'operation': 'sec_item_add',
        'os_status': -25293,
    }
    assert client.get(
        f"/api/v1/connections/diagnostics/{detail['correlation_id']}",
        headers=headers(1)).status_code == 404


def test_missing_core_credential_requests_in_app_reentry_without_losing_mapping(setup):
    from app.services import connections
    from app.services.secrets import SecretReentryRequired
    client, store, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(), json={
        'provider_kind': 'openai', 'label': 'Needs reentry',
        'default_model_id': 'model', 'api_key': 'synthetic-old-key',
    }).json()

    class MissingCoreStore:
        def get_no_ui(self, _reference):
            raise SecretReentryRequired()

    client.app.state.secret_store = MissingCoreStore()
    result = client.post(f"/api/v1/connections/{created['id']}/test", headers=headers()).json()
    assert {key: result[key] for key in ('ok', 'code', 'message')} == {
        'ok': False, 'code': 'missing_secret',
        'message': 'Re-enter the API key for this saved connection.'}
    assert result['record']['code'] == 'missing_secret'
    assert result['record']['credential_observation'] == 'missing'
    listed = client.get('/api/v1/connections', headers=headers()).json()
    assert [(row['id'], row['label'], row['has_secret']) for row in listed] == [
        (created['id'], 'Needs reentry', True),
    ]


def test_core_connection_unlock_requests_api_key_reentry_without_another_store_read(setup):
    client, _, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(), json={
        'provider_kind': 'openai', 'label': 'Needs reentry',
        'default_model_id': 'model', 'api_key': 'synthetic-old-key',
    }).json()

    class CoreStore:
        supports_interactive_unlock = False

        def get(self, _ref):
            raise AssertionError('stable Core recovery must not repeat the failed read')

    client.app.state.secret_store = CoreStore()
    response = client.post(f"/api/v1/connections/{created['id']}/unlock", headers=headers())

    assert response.status_code == 202
    assert response.json() == {'state': 'reentry_required', 'action': 'reenter_api_key'}


def test_postcommit_refresh_failure_never_removes_referenced_key(setup):
    from app.services import connections
    from app.schemas.connection import ConnectionCreate
    client, store, _, _ = setup
    db = client.app.state.session_factory()
    owner_id = db.query(User).filter_by(username='owner').one().id
    row = connections.create(db, owner_id, ConnectionCreate(provider_kind='openai', label='Refresh', default_model_id='model', api_key='old-key'), store)
    row_id = row.id
    db.refresh = lambda item: (_ for _ in ()).throw(RuntimeError('synthetic refresh failure'))
    with pytest.raises(connections.ConnectionFailure, match='storage_unavailable'):
        connections.update(db, row, {'api_key': 'new-key'}, store)
    db.close()
    with client.app.state.session_factory() as fresh:
        reference = fresh.query(connections.ProviderConnection).filter_by(id=row_id).one().secret_ref
    assert store.get(reference) == 'new-key'


def test_create_commit_then_raise_preserves_persisted_credential(setup):
    from app.services import connections
    from app.schemas.connection import ConnectionCreate
    client, store, _, _ = setup
    db = client.app.state.session_factory()
    owner_id = db.query(User).filter_by(username='owner').one().id
    real_commit = db.commit
    def commit_then_raise():
        real_commit()
        raise RuntimeError('sensitive synthetic postcommit detail')
    db.commit = commit_then_raise
    with pytest.raises(connections.ConnectionFailure) as error:
        connections.create(db, owner_id, ConnectionCreate(provider_kind='openai', label='Commit', default_model_id='model', api_key='create-key'), store)
    assert error.value.code == 'storage_unavailable'
    assert 'sensitive synthetic postcommit detail' not in str(error.value)
    db.close()
    with client.app.state.session_factory() as fresh:
        saved = fresh.query(connections.ProviderConnection).filter_by(user_id=owner_id, name='Commit').one()
        assert saved.enabled is True
        assert store.get(saved.secret_ref) == 'create-key'


def test_create_failed_commit_discards_unreferenced_key(setup):
    from app.services import connections
    from app.schemas.connection import ConnectionCreate
    client, store, _, _ = setup
    db = client.app.state.session_factory()
    owner_id = db.query(User).filter_by(username='owner').one().id
    db.commit = lambda: (_ for _ in ()).throw(RuntimeError('synthetic precommit failure'))
    with pytest.raises(connections.ConnectionFailure, match='storage_unavailable'):
        connections.create(db, owner_id, ConnectionCreate(provider_kind='openai', label='Not saved', default_model_id='model', api_key='unsaved-key'), store)
    db.close()
    with client.app.state.session_factory() as fresh:
        assert fresh.query(connections.ProviderConnection).filter_by(name='Not saved').count() == 0
    assert 'unsaved-key' not in store.values.values()


def test_update_commit_then_raise_preserves_new_referenced_credential(setup):
    from app.services import connections
    from app.schemas.connection import ConnectionCreate
    client, store, _, _ = setup
    db = client.app.state.session_factory()
    owner_id = db.query(User).filter_by(username='owner').one().id
    row = connections.create(db, owner_id, ConnectionCreate(provider_kind='openai', label='Commit', default_model_id='model', api_key='old-key'), store)
    row_id, old_ref = row.id, row.secret_ref
    real_commit = db.commit
    def commit_then_raise():
        real_commit()
        raise RuntimeError('sensitive synthetic postcommit detail')
    db.commit = commit_then_raise
    saved = connections.update(db, row, {'api_key': 'new-key'}, store)
    assert saved.id == row_id
    db.close()
    with client.app.state.session_factory() as fresh:
        saved = fresh.query(connections.ProviderConnection).filter_by(id=row_id).one()
        assert saved.enabled is True
        assert store.get(saved.secret_ref) == 'new-key'
    assert store.get(old_ref) is None


def test_after_commit_hook_failure_is_redacted_and_preserves_credential(setup):
    from sqlalchemy import event
    from app.services import connections
    from app.schemas.connection import ConnectionCreate
    client, store, _, _ = setup
    db = client.app.state.session_factory()
    owner_id = db.query(User).filter_by(username='owner').one().id
    row = connections.create(db, owner_id, ConnectionCreate(provider_kind='openai', label='Hook', default_model_id='model', api_key='old-key'), store)
    row_id, old_ref = row.id, row.secret_ref
    def fail_after_commit(session):
        raise RuntimeError('sensitive after_commit detail')
    event.listen(db, 'after_commit', fail_after_commit)
    try:
        saved = connections.update(db, row, {'api_key': 'new-key'}, store)
        assert saved.id == row_id
    finally:
        event.remove(db, 'after_commit', fail_after_commit)
        db.close()
    with client.app.state.session_factory() as fresh:
        saved = fresh.query(connections.ProviderConnection).filter_by(id=row_id).one()
        assert store.get(saved.secret_ref) == 'new-key'
    assert store.get(old_ref) is None


def test_unverifiable_commit_retains_new_key_instead_of_risking_dangling_ref(setup, monkeypatch):
    from app.services import connections
    from app.schemas.connection import ConnectionCreate
    client, store, _, _ = setup
    db = client.app.state.session_factory()
    owner_id = db.query(User).filter_by(username='owner').one().id
    row = connections.create(db, owner_id, ConnectionCreate(provider_kind='openai', label='Uncertain', default_model_id='model', api_key='old-key'), store)
    row_id, old_ref = row.id, row.secret_ref
    db.commit = lambda: (_ for _ in ()).throw(RuntimeError('commit outcome unknown'))
    monkeypatch.setattr(connections, 'Session', lambda **kwargs: (_ for _ in ()).throw(RuntimeError('cannot inspect persisted row')))
    with pytest.raises(connections.ConnectionFailure) as error:
        connections.update(db, row, {'api_key': 'new-key'}, store)
    assert error.value.code == 'storage_unavailable'
    db.close()
    with client.app.state.session_factory() as fresh:
        assert fresh.query(connections.ProviderConnection).filter_by(id=row_id).one().secret_ref == old_ref
    assert store.get(old_ref) == 'old-key'
    assert 'new-key' in store.values.values()  # Safe orphan when commit status cannot be checked.


def test_api_commit_error_is_typed_and_does_not_echo_key(setup):
    client, store, headers, _ = setup
    factory = client.app.state.session_factory
    def failing_factory():
        db = factory()
        real_commit = db.commit
        def commit_then_raise():
            real_commit()
            raise RuntimeError('key-shaped sensitive DB detail')
        db.commit = commit_then_raise
        return db
    client.app.state.session_factory = failing_factory
    try:
        response = client.post('/api/v1/connections', headers=headers(), json={
            'provider_kind': 'openai', 'label': 'API failure', 'default_model_id': 'model', 'api_key': 'secret-api-key'})
    finally:
        client.app.state.session_factory = factory
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'storage_unavailable'
    assert 'secret-api-key' not in response.text
    assert 'key-shaped sensitive DB detail' not in response.text
    with factory() as fresh:
        from app.models.collaboration import ProviderConnection
        row = fresh.query(ProviderConnection).filter_by(name='API failure').one()
        assert store.get(row.secret_ref) == 'secret-api-key'


def _version_one_payload(**overrides):
    payload = {
        'version': 1,
        'provider_id': 'openrouter',
        'protocol_id': 'openai_chat_completions',
        'auth_method_id': 'api_key',
        'fields': {},
        'label': 'Version one',
        'default_model_id': 'vendor/exact-model',
        'api_key': 'synthetic-version-one-key',
        'enabled': True,
        'capabilities': {'tools': True},
    }
    payload.update(overrides)
    return payload


def test_version_one_crud_rotates_grant_and_keeps_legacy_json_shape(setup):
    client, store, headers, _ = setup
    legacy = client.post('/api/v1/connections', headers=headers(), json={
        'provider_kind': 'openai', 'label': 'Legacy',
        'default_model_id': 'legacy-model', 'api_key': 'synthetic-legacy-key',
    })
    assert legacy.status_code == 201
    assert set(legacy.json()) == {
        'id', 'owner_id', 'provider_kind', 'label', 'base_url',
        'default_model_id', 'enabled', 'auth_required', 'has_secret',
        'is_local', 'capabilities',
    }

    created = client.post('/api/v1/connections', headers=headers(), json=_version_one_payload())
    assert created.status_code == 201, created.text
    body = created.json()
    assert body == {
        'id': body['id'], 'owner_id': body['owner_id'],
        'provider_kind': 'compatible', 'label': 'Version one',
        'base_url': 'https://openrouter.ai/api/v1',
        'default_model_id': 'vendor/exact-model', 'enabled': True,
        'auth_required': True, 'has_secret': True, 'is_local': False,
        'capabilities': {'declared': {'tools': True}, 'verified': {}},
        'version': 1, 'provider_id': 'openrouter',
        'protocol_id': 'openai_chat_completions', 'auth_method_id': 'api_key',
        'fields': {}, 'config_revision': 1, 'auth_grant_revision': 1,
        'credential_state': 'present',
    }
    assert 'secret_ref' not in body
    assert 'synthetic-version-one-key' not in created.text

    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, body['id'])
        first_ref = row.secret_ref
        assert store.get(first_ref) == 'synthetic-version-one-key'
        from app.services import connections
        config, reference = connections._connection_metadata(db, row.id, body['owner_id'])
        assert config.provider_id == 'openrouter'
        assert config.base_url == 'https://openrouter.ai/api/v1'
        assert reference == first_ref

    replacement = _version_one_payload(
        label='Renamed', api_key='synthetic-replacement-key')
    changed = client.patch(
        f"/api/v1/connections/{body['id']}", headers=headers(), json=replacement)
    assert changed.status_code == 200, changed.text
    assert changed.json()['provider_id'] == 'openrouter'
    assert changed.json()['config_revision'] == 2
    assert changed.json()['auth_grant_revision'] == 2
    assert changed.json()['credential_state'] == 'present'
    assert 'synthetic-replacement-key' not in changed.text
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, body['id'])
        assert row.secret_ref != first_ref
        assert store.get(row.secret_ref) == 'synthetic-replacement-key'
    assert store.get(first_ref) is None

    immutable = client.patch(
        f"/api/v1/connections/{body['id']}", headers=headers(),
        json=_version_one_payload(
            provider_id='deepseek', label='Must not change provider',
            api_key='synthetic-must-not-commit'))
    assert immutable.status_code == 422
    assert immutable.json()['detail']['code'] == 'provider_immutable'
    assert 'synthetic-must-not-commit' not in immutable.text


def test_version_one_local_none_is_typed_without_secret_access(setup):
    client, store, headers, _ = setup
    calls = []
    store.put = lambda *args: calls.append(('put', args))
    created = client.post('/api/v1/connections', headers=headers(), json={
        'version': 1, 'provider_id': 'ollama',
        'protocol_id': 'openai_chat_completions',
        'auth_method_id': 'local_none', 'fields': {},
        'label': 'Local typed', 'default_model_id': 'local/exact',
    })
    assert created.status_code == 201, created.text
    assert created.json()['base_url'] == 'http://localhost:11434/v1'
    assert created.json()['auth_required'] is False
    assert created.json()['credential_state'] == 'not_required'
    assert created.json()['auth_grant_revision'] == 0
    assert calls == []


def test_explicit_version_one_update_projects_legacy_row_without_replacing_its_grant(setup):
    client, store, headers, _ = setup
    legacy = client.post('/api/v1/connections', headers=headers(), json={
        'provider_kind': 'openai', 'label': 'Legacy to project',
        'default_model_id': 'legacy-model', 'api_key': 'synthetic-existing-grant',
    }).json()
    with client.app.state.session_factory() as db:
        old_ref = db.get(ProviderConnection, legacy['id']).secret_ref
    changed = client.patch(
        f"/api/v1/connections/{legacy['id']}", headers=headers(), json={
            'version': 1, 'provider_id': 'openai',
            'protocol_id': 'openai_chat_completions',
            'auth_method_id': 'api_key', 'fields': {},
            'label': 'Projected', 'default_model_id': 'legacy-model',
        })
    assert changed.status_code == 200, changed.text
    assert changed.json()['id'] == legacy['id']
    assert changed.json()['provider_id'] == 'openai'
    assert changed.json()['config_revision'] == 2
    assert changed.json()['auth_grant_revision'] == 1
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, legacy['id'])
        assert row.secret_ref == old_ref
        assert row.credential_state == 'present'
    assert store.get(old_ref) == 'synthetic-existing-grant'


def test_connection_revisions_are_owner_scoped_and_have_zero_secret_or_provider_calls(setup):
    client, store, headers, _ = setup
    legacy = client.post('/api/v1/connections', headers=headers(), json={
        'provider_kind': 'openai', 'label': 'Legacy revisions',
        'default_model_id': 'legacy-model', 'api_key': 'synthetic-legacy-key',
    }).json()
    typed = client.post('/api/v1/connections', headers=headers(),
                        json=_version_one_payload()).json()

    def forbidden(*_args, **_kwargs):
        raise AssertionError('revision reads must not access credentials or providers')

    store.get = store.get_no_ui = store.put = store.delete = forbidden
    client.app.state.connection_transport = forbidden
    assert client.get(
        f"/api/v1/connections/{legacy['id']}/revisions", headers=headers()).json() == {
            'version': 1, 'config_revision': 1, 'auth_grant_revision': 1,
        }
    assert client.get(
        f"/api/v1/connections/{typed['id']}/revisions", headers=headers()).json() == {
            'version': 1, 'config_revision': 1, 'auth_grant_revision': 1,
        }
    assert client.get(
        f"/api/v1/connections/{typed['id']}/revisions",
        headers=headers(1)).status_code == 404


def test_malformed_version_one_validation_never_echoes_nested_secret_input(setup):
    client, _, headers, _ = setup
    marker = 'nested-sensitive-validation-marker'
    response = client.post('/api/v1/connections', headers=headers(), json={
        **_version_one_payload(),
        'api_key': {'unexpected': marker},
        'extra': {'secret_ref': marker, 'token': marker},
    })
    assert response.status_code == 422
    assert response.json() == {
        'detail': {'code': 'invalid_request', 'message': 'Invalid connection input.'}}
    assert marker not in response.text
    assert 'secret_ref' not in response.text


def test_version_one_rotation_cas_loss_preserves_winner_and_old_credential(setup):
    from app.services import connections

    client, store, _, _ = setup
    with client.app.state.session_factory() as seed:
        owner_id = seed.query(User).filter_by(username='owner').one().id
        row = ProviderConnection(
            user_id=owner_id, kind='compatible', name='Before race',
            base_url='https://openrouter.ai/api/v1', secret_ref='1:1:' + 'a' * 32,
            default_model_id='vendor/model', enabled=True, auth_required=True,
            capabilities={'declared': {}, 'verified': {}}, readiness_revision=4,
            provider_id='openrouter', protocol_id='openai_chat_completions',
            auth_method_id='api_key', public_config_version=1, public_config={},
            auth_grant_revision=3, credential_state='present')
        seed.add(row); seed.commit(); seed.refresh(row)
        connection_id, old_ref = row.id, row.secret_ref
    store.values[old_ref] = 'synthetic-old-key'

    class RacingStore(FakeSecrets):
        def __init__(self):
            self.values = store.values
            self.raced = False

        def put(self, ref, value):
            super().put(ref, value)
            if value == 'synthetic-new-key' and not self.raced:
                self.raced = True
                with client.app.state.session_factory() as winner:
                    won = winner.get(ProviderConnection, connection_id)
                    won.name = 'Winner label'
                    won.readiness_revision += 1
                    winner.commit()

    racing = RacingStore()
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, connection_id)
        with pytest.raises(connections.ConnectionFailure) as error:
            connections.update(db, row, {'api_key': 'synthetic-new-key'}, racing)
        assert error.value.code == 'configuration_changed'
    with client.app.state.session_factory() as check:
        saved = check.get(ProviderConnection, connection_id)
        assert saved.name == 'Winner label'
        assert saved.secret_ref == old_ref
        assert saved.readiness_revision == 5
        assert saved.auth_grant_revision == 3
    assert racing.get(old_ref) == 'synthetic-old-key'
    assert 'synthetic-new-key' not in racing.values.values()


def test_version_one_rotation_commit_then_raise_runs_success_cleanup_and_cache_clear(setup):
    client, store, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(),
                          json=_version_one_payload()).json()
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        old_ref = row.secret_ref
    cache_key = (created['owner_id'], created['id'], old_ref)
    reservation = client.app.state.credential_cache.reserve(cache_key)
    client.app.state.credential_cache.put_if_current(
        cache_key, reservation, 'synthetic-cached-old-key')

    factory = client.app.state.session_factory

    def failing_factory():
        db = factory()
        real_commit = db.commit

        def commit_then_raise():
            real_commit()
            raise RuntimeError('synthetic postcommit detail')

        db.commit = commit_then_raise
        return db

    client.app.state.session_factory = failing_factory
    try:
        response = client.patch(
            f"/api/v1/connections/{created['id']}", headers=headers(),
            json=_version_one_payload(api_key='synthetic-committed-next-key'))
    finally:
        client.app.state.session_factory = factory
    assert response.status_code == 200, response.text
    assert response.json()['auth_grant_revision'] == 2
    assert client.app.state.credential_cache.get(cache_key) is None
    assert store.get(old_ref) is None
    with factory() as db:
        row = db.get(ProviderConnection, created['id'])
        assert store.get(row.secret_ref) == 'synthetic-committed-next-key'


def _prepared_ref(owner_id, connection_id, marker):
    return f'{owner_id}:{connection_id}:' + marker * 32


@pytest.mark.parametrize('transition,expected_revisions', [
    ('proven_new_grant', (2, 2)),
    ('same_grant_refresh', (1, 1)),
])
def test_prepared_grant_commit_distinguishes_new_grant_from_same_grant_refresh(
        setup, transition, expected_revisions):
    from app.services import connections

    client, store, headers, url = setup
    created = client.post('/api/v1/connections', headers=headers(),
                          json=_version_one_payload(label=transition)).json()
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        old_ref = row.secret_ref
        prepared_ref = _prepared_ref(row.user_id, row.id, 'd')
        store.values[prepared_ref] = 'synthetic-prepared-credential'
        saved = connections.commit_prepared_credential_grant(
            db,
            owner_id=row.user_id,
            connection_id=row.id,
            config_revision=1,
            auth_grant_revision=1,
            current_auth_method_id='api_key',
            target_auth_method_id='api_key',
            prepared_ref=prepared_ref,
            safe_grant_identity='opaque-safe-grant-identity',
            transition=transition,
            store=store,
        )
        assert (saved.readiness_revision, saved.auth_grant_revision) == expected_revisions
        assert saved.secret_ref == prepared_ref
    assert store.get(old_ref) is None
    assert store.get(prepared_ref) == 'synthetic-prepared-credential'
    with sqlite3.connect(url.removeprefix('sqlite:///')) as raw:
        persisted = repr(raw.execute('SELECT * FROM provider_connections').fetchall())
    assert 'opaque-safe-grant-identity' not in persisted


def test_prepared_grant_new_account_scope_or_consent_proof_bumps_both_revisions(setup):
    from app.services import connections

    client, store, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(),
                          json=_version_one_payload()).json()
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        prepared_ref = _prepared_ref(row.user_id, row.id, 'e')
        store.values[prepared_ref] = 'synthetic-new-consent-credential'
        saved = connections.commit_prepared_credential_grant(
            db,
            owner_id=row.user_id,
            connection_id=row.id,
            config_revision=1,
            auth_grant_revision=1,
            current_auth_method_id='api_key',
            target_auth_method_id='api_key',
            prepared_ref=prepared_ref,
            safe_grant_identity='opaque-account-scope-consent-change',
            transition='proven_new_grant',
            store=store,
        )
        assert (saved.readiness_revision, saved.auth_grant_revision) == (2, 2)


def test_prepared_same_grant_refresh_rejects_method_change_and_discards_prepared_ref(setup):
    from app.services import connections

    client, store, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(),
                          json=_version_one_payload()).json()
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        old_ref = row.secret_ref
        prepared_ref = _prepared_ref(row.user_id, row.id, 'f')
        store.values[prepared_ref] = 'synthetic-invalid-refresh'
        with pytest.raises(connections.ConnectionFailure) as error:
            connections.commit_prepared_credential_grant(
                db,
                owner_id=row.user_id,
                connection_id=row.id,
                config_revision=1,
                auth_grant_revision=1,
                current_auth_method_id='api_key',
                target_auth_method_id='local_none',
                prepared_ref=prepared_ref,
                safe_grant_identity='opaque-invalid-method-change',
                transition='same_grant_refresh',
                store=store,
            )
        assert error.value.code == 'invalid_configuration'
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        assert (row.secret_ref, row.readiness_revision,
                row.auth_grant_revision, row.auth_method_id) == (
                    old_ref, 1, 1, 'api_key')
    assert store.get(prepared_ref) is None


def test_prepared_new_grant_rejects_unknown_target_method_and_preserves_old_state(setup):
    from app.services import connections

    client, store, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(),
                          json=_version_one_payload()).json()
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        old_ref = row.secret_ref
        prepared_ref = _prepared_ref(row.user_id, row.id, '0')
        store.values[prepared_ref] = 'synthetic-invalid-method-grant'
        with pytest.raises(connections.ConnectionFailure) as error:
            connections.commit_prepared_credential_grant(
                db,
                owner_id=row.user_id,
                connection_id=row.id,
                config_revision=1,
                auth_grant_revision=1,
                current_auth_method_id='api_key',
                target_auth_method_id='unknown_method',
                prepared_ref=prepared_ref,
                safe_grant_identity='opaque-invalid-target-method',
                transition='proven_new_grant',
                store=store,
            )
        assert error.value.code == 'invalid_configuration'
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        assert (row.secret_ref, row.readiness_revision,
                row.auth_grant_revision, row.auth_method_id) == (
                    old_ref, 1, 1, 'api_key')
    assert store.get(old_ref) == 'synthetic-version-one-key'
    assert store.get(prepared_ref) is None


def test_prepared_grant_rejects_catalog_disabled_target_without_touching_old_state(setup):
    from app.models.collaboration import ConnectionModelCache, ConnectionTestHistory
    from app.services import connections, provider_catalog

    client, store, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(),
                          json=_version_one_payload(provider_id='openai')).json()
    owner_id, old_ref, _ = _seed_disconnect_evidence(client, created['id'])
    cache_key = (owner_id, created['id'], old_ref)
    generation = client.app.state.credential_cache.reserve(cache_key)
    client.app.state.credential_cache.put_if_current(
        cache_key, generation, 'synthetic-cached-old-key')
    prepared_ref = _prepared_ref(owner_id, created['id'], '5')
    store.values[prepared_ref] = 'synthetic-disabled-method-grant'
    disabled = {method.method_id: method for method in
                provider_catalog.get_descriptor('openai').methods}['oauth_browser']
    assert (disabled.code_state, disabled.external_eligibility,
            disabled.selectable) == ('absent', 'unknown', False)

    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        with pytest.raises(connections.ConnectionFailure) as error:
            connections.commit_prepared_credential_grant(
                db,
                owner_id=owner_id,
                connection_id=row.id,
                config_revision=1,
                auth_grant_revision=1,
                current_auth_method_id='api_key',
                target_auth_method_id='oauth_browser',
                prepared_ref=prepared_ref,
                safe_grant_identity='opaque-disabled-method-proof',
                transition='proven_new_grant',
                store=store,
            )
        assert error.value.code == 'invalid_configuration'
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        assert (row.secret_ref, row.credential_state, row.readiness_revision,
                row.auth_grant_revision, row.auth_method_id) == (
                    old_ref, 'present', 1, 1, 'api_key')
        assert db.query(ConnectionModelCache).filter_by(
            owner_id=owner_id, connection_id=row.id).count() == 1
        assert db.query(ConnectionTestHistory).filter_by(
            owner_id=owner_id, connection_id=row.id).count() == 1
    assert store.get(old_ref) == 'synthetic-version-one-key'
    assert store.get(prepared_ref) is None
    assert client.app.state.credential_cache.get(cache_key) == 'synthetic-cached-old-key'


@pytest.mark.parametrize('invalid_metadata', ['protocol', 'public_config'])
def test_prepared_grant_revalidates_target_protocol_and_public_config_before_commit(
        setup, invalid_metadata):
    from app.models.collaboration import ConnectionModelCache, ConnectionTestHistory
    from app.services import connections

    client, store, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(),
                          json=_version_one_payload()).json()
    owner_id, old_ref, _ = _seed_disconnect_evidence(client, created['id'])
    with client.app.state.session_factory() as corrupt:
        row = corrupt.get(ProviderConnection, created['id'])
        if invalid_metadata == 'protocol':
            row.protocol_id = 'anthropic_messages'
        else:
            row.public_config = {'unexpected': 'value'}
        corrupt.commit()
    prepared_ref = _prepared_ref(owner_id, created['id'], '6')
    store.values[prepared_ref] = 'synthetic-invalid-config-grant'

    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        with pytest.raises(connections.ConnectionFailure) as error:
            connections.commit_prepared_credential_grant(
                db,
                owner_id=owner_id,
                connection_id=row.id,
                config_revision=1,
                auth_grant_revision=1,
                current_auth_method_id='api_key',
                target_auth_method_id='api_key',
                prepared_ref=prepared_ref,
                safe_grant_identity='opaque-invalid-config-proof',
                transition='proven_new_grant',
                store=store,
            )
        assert error.value.code == 'invalid_configuration'
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        assert (row.secret_ref, row.credential_state,
                row.readiness_revision, row.auth_grant_revision) == (
                    old_ref, 'present', 1, 1)
        assert db.query(ConnectionModelCache).filter_by(
            owner_id=owner_id, connection_id=row.id).count() == 1
        assert db.query(ConnectionTestHistory).filter_by(
            owner_id=owner_id, connection_id=row.id).count() == 1
    assert store.get(old_ref) == 'synthetic-version-one-key'
    assert store.get(prepared_ref) is None


def test_disabled_target_cleanup_never_deletes_winner_referenced_prepared_ref(setup):
    from app.services import connections

    client, store, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(),
                          json=_version_one_payload(provider_id='openai')).json()
    prepared_ref = _prepared_ref(created['owner_id'], created['id'], '7')
    store.values[prepared_ref] = 'synthetic-winner-grant'
    with client.app.state.session_factory() as winner:
        connections.commit_prepared_credential_grant(
            winner,
            owner_id=created['owner_id'], connection_id=created['id'],
            config_revision=1, auth_grant_revision=1,
            current_auth_method_id='api_key', target_auth_method_id='api_key',
            prepared_ref=prepared_ref,
            safe_grant_identity='opaque-winner-proof',
            transition='proven_new_grant', store=store,
        )
    with client.app.state.session_factory() as loser:
        with pytest.raises(connections.ConnectionFailure):
            connections.commit_prepared_credential_grant(
                loser,
                owner_id=created['owner_id'], connection_id=created['id'],
                config_revision=1, auth_grant_revision=1,
                current_auth_method_id='api_key',
                target_auth_method_id='oauth_browser',
                prepared_ref=prepared_ref,
                safe_grant_identity='opaque-disabled-loser-proof',
                transition='proven_new_grant', store=store,
            )
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        assert (row.secret_ref, row.credential_state,
                row.readiness_revision, row.auth_grant_revision,
                row.auth_method_id) == (prepared_ref, 'present', 2, 2, 'api_key')
    assert store.get(prepared_ref) == 'synthetic-winner-grant'


def test_prepared_grant_cas_loss_discards_prepared_ref_and_preserves_winner(setup):
    from app.services import connections

    client, store, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(),
                          json=_version_one_payload()).json()
    with client.app.state.session_factory() as winner:
        row = winner.get(ProviderConnection, created['id'])
        old_ref = row.secret_ref
        row.name = 'CAS winner'
        row.readiness_revision = 2
        winner.commit()
    prepared_ref = _prepared_ref(created['owner_id'], created['id'], '1')
    store.values[prepared_ref] = 'synthetic-stale-prepared'
    with client.app.state.session_factory() as db:
        with pytest.raises(connections.ConnectionFailure) as error:
            connections.commit_prepared_credential_grant(
                db,
                owner_id=created['owner_id'],
                connection_id=created['id'],
                config_revision=1,
                auth_grant_revision=1,
                current_auth_method_id='api_key',
                target_auth_method_id='api_key',
                prepared_ref=prepared_ref,
                safe_grant_identity='opaque-stale-proof',
                transition='proven_new_grant',
                store=store,
            )
        assert error.value.code == 'configuration_changed'
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        assert (row.name, row.secret_ref, row.readiness_revision,
                row.auth_grant_revision) == ('CAS winner', old_ref, 2, 1)
    assert store.get(old_ref) == 'synthetic-version-one-key'
    assert store.get(prepared_ref) is None


def test_prepared_grant_owner_mismatch_discards_prepared_ref_without_state_change(setup):
    from app.services import connections

    client, store, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(),
                          json=_version_one_payload()).json()
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        old_ref = row.secret_ref
        other_id = db.query(User).filter_by(username='other').one().id
        prepared_ref = _prepared_ref(other_id, row.id, '3')
        store.values[prepared_ref] = 'synthetic-foreign-prepared'
        with pytest.raises(connections.ConnectionFailure) as error:
            connections.commit_prepared_credential_grant(
                db,
                owner_id=other_id,
                connection_id=row.id,
                config_revision=1,
                auth_grant_revision=1,
                current_auth_method_id='api_key',
                target_auth_method_id='api_key',
                prepared_ref=prepared_ref,
                safe_grant_identity='opaque-foreign-proof',
                transition='proven_new_grant',
                store=store,
            )
        assert error.value.code == 'configuration_changed'
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        assert (row.secret_ref, row.readiness_revision,
                row.auth_grant_revision) == (old_ref, 1, 1)
    assert store.get(old_ref) == 'synthetic-version-one-key'
    assert store.get(prepared_ref) is None


def test_prepared_grant_cas_loser_never_deletes_a_winner_referenced_prepared_ref(setup):
    from app.services import connections

    client, store, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(),
                          json=_version_one_payload()).json()
    prepared_ref = _prepared_ref(created['owner_id'], created['id'], '4')
    store.values[prepared_ref] = 'synthetic-shared-prepared'
    with client.app.state.session_factory() as winner:
        connections.commit_prepared_credential_grant(
            winner,
            owner_id=created['owner_id'],
            connection_id=created['id'],
            config_revision=1,
            auth_grant_revision=1,
            current_auth_method_id='api_key',
            target_auth_method_id='api_key',
            prepared_ref=prepared_ref,
            safe_grant_identity='opaque-winner-proof',
            transition='proven_new_grant',
            store=store,
        )
    with client.app.state.session_factory() as loser:
        with pytest.raises(connections.ConnectionFailure) as error:
            connections.commit_prepared_credential_grant(
                loser,
                owner_id=created['owner_id'],
                connection_id=created['id'],
                config_revision=1,
                auth_grant_revision=1,
                current_auth_method_id='api_key',
                target_auth_method_id='api_key',
                prepared_ref=prepared_ref,
                safe_grant_identity='opaque-loser-proof',
                transition='proven_new_grant',
                store=store,
            )
        assert error.value.code == 'configuration_changed'
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        assert (row.secret_ref, row.readiness_revision,
                row.auth_grant_revision) == (prepared_ref, 2, 2)
    assert store.get(prepared_ref) == 'synthetic-shared-prepared'


def test_prepared_grant_commit_failure_preserves_old_state_and_discards_prepared_ref(setup):
    from app.services import connections

    client, store, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(),
                          json=_version_one_payload()).json()
    prepared_ref = _prepared_ref(created['owner_id'], created['id'], '2')
    store.values[prepared_ref] = 'synthetic-uncommitted-prepared'
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        old_ref = row.secret_ref
        db.commit = lambda: (_ for _ in ()).throw(RuntimeError('synthetic commit failure'))
        with pytest.raises(connections.ConnectionFailure) as error:
            connections.commit_prepared_credential_grant(
                db,
                owner_id=row.user_id,
                connection_id=row.id,
                config_revision=1,
                auth_grant_revision=1,
                current_auth_method_id='api_key',
                target_auth_method_id='api_key',
                prepared_ref=prepared_ref,
                safe_grant_identity='opaque-uncommitted-proof',
                transition='proven_new_grant',
                store=store,
            )
        assert error.value.code == 'storage_unavailable'
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        assert (row.secret_ref, row.readiness_revision,
                row.auth_grant_revision) == (old_ref, 1, 1)
    assert store.get(old_ref) == 'synthetic-version-one-key'
    assert store.get(prepared_ref) is None


def test_version_one_rotation_stales_model_cache_and_exact_test_evidence(setup):
    from app.services import model_catalog

    client, _, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(),
                          json=_version_one_payload()).json()
    _seed_disconnect_evidence(client, created['id'])
    changed = client.patch(
        f"/api/v1/connections/{created['id']}", headers=headers(),
        json=_version_one_payload(api_key='synthetic-next-grant-key'))
    assert changed.status_code == 200
    with client.app.state.session_factory() as db:
        catalog = model_catalog.cached_models(db, created['owner_id'], created['id'])
    assert catalog.stale is True
    history = client.get(
        f"/api/v1/connections/{created['id']}/test-history",
        headers=headers()).json()
    assert history['items'][0]['config_current'] is False


def test_version_one_auth_method_change_removes_credential_and_bumps_both_revisions(setup):
    client, store, headers, _ = setup
    base = {
        'version': 1, 'provider_id': 'compatible',
        'protocol_id': 'openai_chat_completions', 'auth_method_id': 'api_key',
        'fields': {'base_url': 'http://127.0.0.1:1234/v1',
                   'allow_local_http': True},
        'label': 'Method switch', 'default_model_id': 'local/exact',
        'api_key': 'synthetic-method-key',
    }
    created = client.post('/api/v1/connections', headers=headers(), json=base).json()
    with client.app.state.session_factory() as db:
        old_ref = db.get(ProviderConnection, created['id']).secret_ref
    changed = client.patch(
        f"/api/v1/connections/{created['id']}", headers=headers(), json={
            **base, 'auth_method_id': 'local_none', 'api_key': None,
        })
    assert changed.status_code == 200, changed.text
    assert changed.json()['credential_state'] == 'not_required'
    assert changed.json()['has_secret'] is False
    assert changed.json()['config_revision'] == 2
    assert changed.json()['auth_grant_revision'] == 2
    assert store.get(old_ref) is None


def _seed_disconnect_evidence(client, connection_id):
    from app.models.collaboration import ConnectionModelCache, ConnectionTestHistory
    from app.services import readiness

    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, connection_id)
        fingerprint = readiness.test_fingerprint(row, row.default_model_id, 'text')
        db.add(ConnectionModelCache(
            owner_id=row.user_id, connection_id=row.id,
            config_revision=row.readiness_revision,
            auth_grant_revision=row.auth_grant_revision,
            config_fingerprint=fingerprint, observed_at=datetime.now(UTC),
            truncated=False,
            models=[{'id': row.default_model_id, 'label': row.default_model_id,
                     'source': 'provider'}],
        ))
        db.add(ConnectionTestHistory(
            owner_id=row.user_id, connection_id=row.id,
            config_revision=row.readiness_revision,
            config_fingerprint=fingerprint, model_id=row.default_model_id,
            ok=True, outcome_code='connected', credential_observation='verified',
            verified_capabilities={'tools': True}, duration_ms=1,
            profile='text', actual_request_count=1,
        ))
        db.commit()
        return row.user_id, row.secret_ref, fingerprint


def test_disconnect_removes_local_credential_and_cache_but_preserves_stale_history(setup):
    from app.models.collaboration import ConnectionModelCache, ConnectionTestHistory
    from app.services import connections, readiness

    client, store, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(),
                          json=_version_one_payload()).json()
    owner_id, old_ref, old_fingerprint = _seed_disconnect_evidence(
        client, created['id'])
    cache_key = connections._cache_key(owner_id, created['id'], old_ref)
    generation = client.app.state.credential_cache.reserve(cache_key)
    assert client.app.state.credential_cache.put_if_current(
        cache_key, generation, 'synthetic-cached-key') is True

    response = client.post(
        f"/api/v1/connections/{created['id']}/disconnect",
        headers=headers(), json={'version': 1, 'revoke_remote': False})
    assert response.status_code == 200, response.text
    assert response.json() == {
        'credential_state': 'missing', 'revoke_state': 'skipped'}
    assert store.get(old_ref) is None
    assert client.app.state.credential_cache.get(cache_key) is None
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        assert row.secret_ref is None
        assert row.credential_state == 'missing'
        assert row.readiness_revision == 2
        assert row.auth_grant_revision == 2
        assert db.query(ConnectionModelCache).filter_by(
            owner_id=owner_id, connection_id=row.id).count() == 0
        assert db.query(ConnectionTestHistory).filter_by(
            owner_id=owner_id, connection_id=row.id).count() == 1
        assert readiness.config_fingerprint(row) != old_fingerprint
    history = client.get(
        f"/api/v1/connections/{created['id']}/test-history",
        headers=headers()).json()
    assert history['items'][0]['config_current'] is False


def test_disconnect_remote_failure_never_restores_local_credential(setup):
    client, store, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(),
                          json=_version_one_payload()).json()
    with client.app.state.session_factory() as db:
        old_ref = db.get(ProviderConnection, created['id']).secret_ref

    def fail_remote(**_kwargs):
        raise RuntimeError('synthetic remote revoke detail')

    client.app.state.connection_revoker = fail_remote
    response = client.post(
        f"/api/v1/connections/{created['id']}/disconnect",
        headers=headers(), json={'version': 1, 'revoke_remote': True})
    assert response.status_code == 200
    assert response.json() == {
        'credential_state': 'missing',
        'revoke_state': 'remote_revoke_failed',
    }
    assert 'synthetic remote revoke detail' not in response.text
    assert store.get(old_ref) is None
    with client.app.state.session_factory() as db:
        assert db.get(ProviderConnection, created['id']).secret_ref is None


@pytest.mark.parametrize('winner', ['same_ref_config', 'new_ref', 'missing'])
def test_disconnect_cas_loss_never_deletes_or_rewrites_the_winner(setup, winner):
    from app.models.collaboration import ConnectionModelCache, ConnectionTestHistory
    from app.services import connections

    client, store, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(),
                          json=_version_one_payload()).json()
    owner_id, old_ref, _ = _seed_disconnect_evidence(client, created['id'])
    new_ref = f'{owner_id}:{created["id"]}:' + 'b' * 32
    with client.app.state.session_factory() as stale_db:
        stale_row = stale_db.get(ProviderConnection, created['id'])
        stale_db.expunge(stale_row)
    with client.app.state.session_factory() as winning_db:
        row = winning_db.get(ProviderConnection, created['id'])
        row.name = 'Concurrent winner'
        row.readiness_revision += 1
        if winner == 'new_ref':
            store.values[new_ref] = 'synthetic-winner-key'
            row.secret_ref = new_ref
            row.auth_grant_revision += 1
        elif winner == 'missing':
            row.secret_ref = None
            row.credential_state = 'missing'
            row.auth_grant_revision += 1
        winning_db.commit()

    with client.app.state.session_factory() as db:
        with pytest.raises(connections.ConnectionFailure) as error:
            connections.disconnect_connection(db, stale_row, False, store)
        assert error.value.code == 'configuration_changed'
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        assert row.name == 'Concurrent winner'
        assert db.query(ConnectionModelCache).filter_by(
            owner_id=owner_id, connection_id=row.id).count() == 1
        assert db.query(ConnectionTestHistory).filter_by(
            owner_id=owner_id, connection_id=row.id).count() == 1
        if winner == 'same_ref_config':
            assert row.secret_ref == old_ref
            assert store.get(old_ref) == 'synthetic-version-one-key'
        elif winner == 'new_ref':
            assert row.secret_ref == new_ref
            assert store.get(new_ref) == 'synthetic-winner-key'
            assert store.get(old_ref) == 'synthetic-version-one-key'
        else:
            assert row.secret_ref is None
            assert store.get(old_ref) == 'synthetic-version-one-key'


def test_disconnect_compensation_never_overwrites_a_rotation_winner(setup):
    from app.services.secrets import SecretStoreUnavailable

    client, store, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(),
                          json=_version_one_payload()).json()
    owner_id, old_ref, _ = _seed_disconnect_evidence(client, created['id'])
    new_ref = f'{owner_id}:{created["id"]}:' + 'c' * 32

    def fail_delete(_ref):
        raise SecretStoreUnavailable('keychain_store_unavailable')

    def race_before_compensation(ref):
        store.values[new_ref] = 'synthetic-new-winner-key'
        with client.app.state.session_factory() as winner:
            row = winner.get(ProviderConnection, created['id'])
            row.secret_ref = new_ref
            row.credential_state = 'present'
            row.auth_grant_revision += 1
            row.readiness_revision += 1
            winner.commit()
        return store.values.get(ref)

    store.delete = fail_delete
    store.get_no_ui = race_before_compensation
    response = client.post(
        f"/api/v1/connections/{created['id']}/disconnect",
        headers=headers(), json={'version': 1, 'revoke_remote': False})
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'credential_cleanup_unconfirmed'
    with client.app.state.session_factory() as db:
        assert db.get(ProviderConnection, created['id']).secret_ref == new_ref
    assert store.get(new_ref) == 'synthetic-new-winner-key'
    assert store.get(old_ref) == 'synthetic-version-one-key'


def test_disconnect_store_delete_failure_preserves_all_local_state(setup):
    from app.models.collaboration import ConnectionModelCache, ConnectionTestHistory
    from app.services.secrets import SecretStoreUnavailable

    client, store, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(),
                          json=_version_one_payload()).json()
    owner_id, old_ref, _ = _seed_disconnect_evidence(client, created['id'])

    def fail_delete(_ref):
        raise SecretStoreUnavailable('keychain_store_unavailable')

    store.delete = fail_delete
    response = client.post(
        f"/api/v1/connections/{created['id']}/disconnect",
        headers=headers(), json={'version': 1, 'revoke_remote': False})
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'secret_store_unavailable'
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        assert row.secret_ref == old_ref
        assert row.readiness_revision == 1
        assert row.auth_grant_revision == 1
        assert db.query(ConnectionModelCache).filter_by(
            owner_id=owner_id, connection_id=row.id).count() == 1
        assert db.query(ConnectionTestHistory).filter_by(
            owner_id=owner_id, connection_id=row.id).count() == 1


def test_disconnect_delete_failure_never_depends_on_restoring_the_old_value(setup):
    from app.models.collaboration import ConnectionModelCache, ConnectionTestHistory
    from app.services.secrets import SecretStoreUnavailable

    client, store, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(),
                          json=_version_one_payload()).json()
    owner_id, old_ref, _ = _seed_disconnect_evidence(client, created['id'])
    cache_key = (owner_id, created['id'], old_ref)
    generation = client.app.state.credential_cache.reserve(cache_key)
    client.app.state.credential_cache.put_if_current(
        cache_key, generation, 'synthetic-cached-old-key')

    def fail_delete(_ref):
        raise SecretStoreUnavailable('keychain_store_unavailable')

    def forbidden_restore(*_args):
        raise AssertionError('disconnect must not delete before its DB commit')

    store.delete = fail_delete
    store.put = forbidden_restore
    response = client.post(
        f"/api/v1/connections/{created['id']}/disconnect",
        headers=headers(), json={'version': 1, 'revoke_remote': False})
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'secret_store_unavailable'
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        assert (row.secret_ref, row.credential_state,
                row.readiness_revision, row.auth_grant_revision) == (
                    old_ref, 'present', 1, 1)
        assert db.query(ConnectionModelCache).filter_by(
            owner_id=owner_id, connection_id=row.id).count() == 1
        assert db.query(ConnectionTestHistory).filter_by(
            owner_id=owner_id, connection_id=row.id).count() == 1
    assert store.get(old_ref) == 'synthetic-version-one-key'
    assert client.app.state.credential_cache.get(cache_key) == 'synthetic-cached-old-key'


def test_disconnect_delete_after_side_effect_is_confirmed_as_success(setup):
    from app.services.secrets import SecretStoreUnavailable

    client, store, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(),
                          json=_version_one_payload()).json()
    _, old_ref, _ = _seed_disconnect_evidence(client, created['id'])
    original_delete = store.delete

    def delete_then_raise(ref):
        original_delete(ref)
        raise SecretStoreUnavailable('keychain_store_unavailable')

    store.delete = delete_then_raise
    response = client.post(
        f"/api/v1/connections/{created['id']}/disconnect",
        headers=headers(), json={'version': 1, 'revoke_remote': False})
    assert response.status_code == 200, response.text
    assert response.json() == {'credential_state': 'missing', 'revoke_state': 'skipped'}
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        assert (row.secret_ref, row.credential_state,
                row.readiness_revision, row.auth_grant_revision) == (
                    None, 'missing', 2, 2)
    assert store.get(old_ref) is None


def test_disconnect_unconfirmed_delete_stays_fail_closed_without_false_restore(setup):
    from app.models.collaboration import ConnectionModelCache
    from app.services.secrets import SecretStoreUnavailable

    client, store, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(),
                          json=_version_one_payload()).json()
    owner_id, old_ref, _ = _seed_disconnect_evidence(client, created['id'])
    cache_key = (owner_id, created['id'], old_ref)
    generation = client.app.state.credential_cache.reserve(cache_key)
    client.app.state.credential_cache.put_if_current(
        cache_key, generation, 'synthetic-cached-old-key')
    original_delete = store.delete

    def delete_then_raise(ref):
        original_delete(ref)
        raise SecretStoreUnavailable('keychain_store_unavailable')

    def presence_unavailable(_ref):
        raise SecretStoreUnavailable('keychain_query_failed')

    store.delete = delete_then_raise
    store.get_no_ui = presence_unavailable
    response = client.post(
        f"/api/v1/connections/{created['id']}/disconnect",
        headers=headers(), json={'version': 1, 'revoke_remote': False})
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'credential_cleanup_unconfirmed'
    assert 'synthetic-version-one-key' not in response.text
    assert old_ref not in response.text
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        assert (row.secret_ref, row.credential_state,
                row.readiness_revision, row.auth_grant_revision) == (
                    None, 'missing', 2, 2)
        assert db.query(ConnectionModelCache).filter_by(
            owner_id=owner_id, connection_id=row.id).count() == 0
    assert store.get(old_ref) is None
    assert client.app.state.credential_cache.get(cache_key) is None


def test_disconnect_compensation_preserves_cached_old_credential_for_same_ref_winner(setup):
    from app.services.secrets import SecretStoreUnavailable

    client, store, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(),
                          json=_version_one_payload()).json()
    owner_id, old_ref, _ = _seed_disconnect_evidence(client, created['id'])
    cache_key = (owner_id, created['id'], old_ref)
    generation = client.app.state.credential_cache.reserve(cache_key)
    client.app.state.credential_cache.put_if_current(
        cache_key, generation, 'synthetic-cached-old-key')

    def fail_delete(_ref):
        raise SecretStoreUnavailable('keychain_store_unavailable')

    def same_ref_config_winner(ref):
        with client.app.state.session_factory() as winner:
            row = winner.get(ProviderConnection, created['id'])
            row.name = 'Same-ref config winner'
            row.secret_ref = old_ref
            row.credential_state = 'present'
            row.readiness_revision += 1
            winner.commit()
        return store.values.get(ref)

    store.delete = fail_delete
    store.get_no_ui = same_ref_config_winner
    response = client.post(
        f"/api/v1/connections/{created['id']}/disconnect",
        headers=headers(), json={'version': 1, 'revoke_remote': False})
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'secret_store_unavailable'
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        assert (row.name, row.secret_ref, row.credential_state) == (
            'Same-ref config winner', old_ref, 'present')
    assert store.get(old_ref) == 'synthetic-version-one-key'
    assert client.app.state.credential_cache.get(cache_key) == 'synthetic-cached-old-key'


def test_disconnect_compensation_failure_stays_missing_and_clears_cached_old_credential(setup):
    from app.models.collaboration import ConnectionModelCache
    from app.services.secrets import SecretStoreUnavailable

    client, store, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(),
                          json=_version_one_payload()).json()
    owner_id, old_ref, _ = _seed_disconnect_evidence(client, created['id'])
    cache_key = (owner_id, created['id'], old_ref)
    generation = client.app.state.credential_cache.reserve(cache_key)
    client.app.state.credential_cache.put_if_current(
        cache_key, generation, 'synthetic-cached-old-key')

    def fail_delete(_ref):
        raise SecretStoreUnavailable('keychain_store_unavailable')

    def conflicting_cache_winner(ref):
        with client.app.state.session_factory() as winner:
            winner.add(ConnectionModelCache(
                owner_id=owner_id,
                connection_id=created['id'],
                config_revision=2,
                auth_grant_revision=2,
                config_fingerprint='f' * 64,
                observed_at=datetime.now(UTC),
                truncated=False,
                models=[],
            ))
            winner.commit()
        return store.values.get(ref)

    store.delete = fail_delete
    store.get_no_ui = conflicting_cache_winner
    response = client.post(
        f"/api/v1/connections/{created['id']}/disconnect",
        headers=headers(), json={'version': 1, 'revoke_remote': False})
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'credential_cleanup_unconfirmed'
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        assert (row.secret_ref, row.credential_state,
                row.readiness_revision, row.auth_grant_revision) == (
                    None, 'missing', 2, 2)
    assert store.get(old_ref) == 'synthetic-version-one-key'
    assert client.app.state.credential_cache.get(cache_key) is None


def test_disconnect_db_commit_failure_restores_referenced_secret_and_evidence(setup):
    from app.models.collaboration import ConnectionModelCache, ConnectionTestHistory
    from app.schemas.connection import VersionedConnectionCreate
    from app.services import connections

    client, store, _, _ = setup
    with client.app.state.session_factory() as seed:
        owner_id = seed.query(User).filter_by(username='owner').one().id
        row = connections.create(
            seed, owner_id,
            VersionedConnectionCreate(**_version_one_payload()),
            store,
        )
        connection_id, old_ref = row.id, row.secret_ref
    _seed_disconnect_evidence(client, connection_id)

    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, connection_id)
        db.commit = lambda: (_ for _ in ()).throw(RuntimeError('synthetic commit failure'))
        with pytest.raises(connections.ConnectionFailure) as error:
            connections.disconnect_connection(db, row, False, store)
        assert error.value.code == 'storage_unavailable'
    with client.app.state.session_factory() as check:
        row = check.get(ProviderConnection, connection_id)
        assert row.secret_ref == old_ref
        assert row.readiness_revision == 1
        assert row.auth_grant_revision == 1
        assert check.query(ConnectionModelCache).filter_by(
            owner_id=owner_id, connection_id=connection_id).count() == 1
        assert check.query(ConnectionTestHistory).filter_by(
            owner_id=owner_id, connection_id=connection_id).count() == 1
    assert store.get(old_ref) == 'synthetic-version-one-key'


def test_disconnect_commit_then_raise_keeps_confirmed_local_disconnect(setup):
    from app.models.collaboration import ConnectionModelCache, ConnectionTestHistory
    from app.schemas.connection import VersionedConnectionCreate
    from app.services import connections

    client, store, _, _ = setup
    with client.app.state.session_factory() as seed:
        owner_id = seed.query(User).filter_by(username='owner').one().id
        row = connections.create(
            seed, owner_id, VersionedConnectionCreate(**_version_one_payload()), store)
        connection_id, old_ref = row.id, row.secret_ref
    _seed_disconnect_evidence(client, connection_id)

    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, connection_id)
        real_commit = db.commit

        def commit_then_raise():
            real_commit()
            raise RuntimeError('synthetic postcommit detail')

        db.commit = commit_then_raise
        result = connections.disconnect_connection(db, row, False, store)
        assert result == connections.DisconnectResult('missing', 'skipped')
    with client.app.state.session_factory() as check:
        row = check.get(ProviderConnection, connection_id)
        assert row.secret_ref is None
        assert row.readiness_revision == 2
        assert row.auth_grant_revision == 2
        assert check.query(ConnectionModelCache).filter_by(
            owner_id=owner_id, connection_id=connection_id).count() == 0
        assert check.query(ConnectionTestHistory).filter_by(
            owner_id=owner_id, connection_id=connection_id).count() == 1
    assert store.get(old_ref) is None


def test_disconnect_remote_states_require_confirmed_success(setup):
    client, _, headers, _ = setup

    not_supported = client.post('/api/v1/connections', headers=headers(),
                                json=_version_one_payload(label='Unsupported')).json()
    response = client.post(
        f"/api/v1/connections/{not_supported['id']}/disconnect",
        headers=headers(), json={'version': 1, 'revoke_remote': True})
    assert response.json()['revoke_state'] == 'not_supported'

    confirmed = client.post('/api/v1/connections', headers=headers(),
                            json=_version_one_payload(label='Confirmed')).json()
    client.app.state.connection_revoker = lambda **_kwargs: 'revoked'
    response = client.post(
        f"/api/v1/connections/{confirmed['id']}/disconnect",
        headers=headers(), json={'version': 1, 'revoke_remote': True})
    assert response.json()['revoke_state'] == 'revoked'

    response = client.post(
        f"/api/v1/connections/{confirmed['id']}/disconnect",
        headers=headers(), json={'version': 1, 'revoke_remote': True})
    assert response.json()['revoke_state'] == 'already_invalid'


def test_disconnect_is_owner_scoped_and_never_exposes_secret_or_reference(setup):
    client, _, headers, _ = setup
    created = client.post('/api/v1/connections', headers=headers(),
                          json=_version_one_payload()).json()
    response = client.post(
        f"/api/v1/connections/{created['id']}/disconnect",
        headers=headers(1), json={'version': 1, 'revoke_remote': True})
    assert response.status_code == 404
    assert 'synthetic-version-one-key' not in response.text
    assert 'secret_ref' not in response.text


@pytest.mark.parametrize('provider_kind,base_url', [
    ('openai', None),
    ('anthropic', None),
    ('gemini', None),
    ('xai', None),
    ('compatible', 'https://compatible.example.test/v1'),
])
def test_all_five_legacy_kinds_keep_exact_crud_test_and_history_json(
        setup, provider_kind, base_url):
    client, _, headers, _ = setup
    payload = {
        'provider_kind': provider_kind,
        'label': f'Legacy {provider_kind}',
        'default_model_id': 'legacy/exact-model',
        'api_key': f'synthetic-{provider_kind}-key',
    }
    if base_url is not None:
        payload['base_url'] = base_url
    created_response = client.post('/api/v1/connections', headers=headers(), json=payload)
    assert created_response.status_code == 201
    created = created_response.json()
    expected = {
        'id': created['id'], 'owner_id': created['owner_id'],
        'provider_kind': provider_kind, 'label': f'Legacy {provider_kind}',
        'base_url': base_url, 'default_model_id': 'legacy/exact-model',
        'enabled': True, 'auth_required': True, 'has_secret': True,
        'is_local': False,
        'capabilities': {'declared': {}, 'verified': {}},
    }
    assert created == expected
    assert client.get('/api/v1/connections', headers=headers()).json() == [expected]

    updated_response = client.patch(
        f"/api/v1/connections/{created['id']}", headers=headers(),
        json={'label': f'Updated {provider_kind}'})
    expected['label'] = f'Updated {provider_kind}'
    assert updated_response.json() == expected

    def provider_reply(_request):
        if provider_kind == 'anthropic':
            return httpx.Response(200, json={'content': [{'type': 'text', 'text': 'OK'}]})
        if provider_kind == 'gemini':
            return httpx.Response(200, json={
                'candidates': [{'content': {'parts': [{'text': 'OK'}]}}]})
        return httpx.Response(200, json={
            'choices': [{'message': {'content': 'OK'}}]})

    client.app.state.connection_transport = httpx.MockTransport(provider_reply)
    tested = client.post(
        f"/api/v1/connections/{created['id']}/test?profile=text",
        headers=headers()).json()
    record = tested.pop('record')
    assert tested == {
        'ok': True, 'code': 'connected',
        'message': 'Connection test succeeded.',
        'profile': 'text', 'model_id': 'legacy/exact-model',
        'max_request_count': 1, 'actual_request_count': 1,
    }
    assert set(record) == {
        'id', 'connection_id', 'tested_at', 'duration_ms', 'model_id', 'ok',
        'code', 'config_fingerprint', 'config_current',
        'credential_observation', 'verified_capabilities', 'profile',
        'actual_request_count',
    }
    assert {key: record[key] for key in (
        'connection_id', 'model_id', 'ok', 'code', 'config_current',
        'credential_observation', 'verified_capabilities', 'profile',
        'actual_request_count')} == {
            'connection_id': created['id'], 'model_id': 'legacy/exact-model',
            'ok': True, 'code': 'connected', 'config_current': True,
            'credential_observation': 'verified', 'verified_capabilities': None,
            'profile': 'text', 'actual_request_count': 1,
        }
    history = client.get(
        f"/api/v1/connections/{created['id']}/test-history",
        headers=headers()).json()
    assert history['connection_id'] == created['id']
    assert len(history['current_config_fingerprint']) == 64
    assert history['items'] == [record]

    deleted = client.delete(
        f"/api/v1/connections/{created['id']}", headers=headers())
    assert deleted.status_code == 204 and deleted.content == b''
    assert client.get('/api/v1/connections', headers=headers()).json() == []
