from datetime import datetime, timedelta

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text

from app.api.routes import auth
from app.main import create_app
from app.services.core_auth_broker import OwnerBinding
from test_remembered_login import NoUiLoginStore


@pytest.fixture
def remembered_client(sqlite_url, monkeypatch):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'synthetic-continuity-session')
    headers = {'X-Gamja-Session': 'synthetic-continuity-session'}
    with TestClient(create_app(database_url=sqlite_url)) as client:
        store = NoUiLoginStore()
        client.app.state.login_secret_store = store
        account = {'email': 'owner@example.com', 'username': 'Owner',
                   'password': 'synthetic-test-password'}
        assert client.post('/api/v1/auth/register', headers=headers, json=account).status_code == 200
        assert client.post('/api/v1/auth/login', headers=headers,
                           json={**account, 'remember_device': True}).status_code == 200
        yield client, store, headers


def test_wrong_owner_is_rejected_before_store_access_or_bind(remembered_client, monkeypatch):
    client, store, headers = remembered_client
    reads = store.no_ui_reads
    binds = []
    monkeypatch.setattr(auth, 'bind_owner_and_reconcile', lambda *a, **kw: binds.append(1))
    monkeypatch.setattr(auth, 'secret_store', lambda *a: pytest.fail('wrong-owner store initialization'))
    response = client.post('/api/v1/auth/restore', headers=headers,
                           json={'expected_owner_id': 999})
    assert response.status_code == 401
    assert store.no_ui_reads == reads and binds == []


@pytest.mark.parametrize('body', [None, {'expected_owner_id': 1}])
def test_same_owner_restore_remains_compatible_and_noninteractive(remembered_client, body):
    client, store, headers = remembered_client
    response = client.post('/api/v1/auth/restore', headers=headers,
                           **({} if body is None else {'json': body}))
    assert response.status_code == 200 and store.ui_reads == 0
    profile = client.get('/api/v1/auth/me', headers={**headers,
                         'Authorization': 'Bearer ' + response.json()['access_token']})
    assert profile.status_code == 200 and profile.json()['id'] == 1


@pytest.mark.parametrize('body', [
    {'expected_owner_id': 0}, {'expected_owner_id': -1}, {'expected_owner_id': True},
    {'expected_owner_id': '1'}, {'expected_owner_id': 1.0},
    {'expected_owner_id': 1, 'unknown': 1}, {},
])
def test_owner_hint_is_strict_and_does_not_read_credentials(remembered_client, body):
    client, store, headers = remembered_client
    reads = store.no_ui_reads
    assert client.post('/api/v1/auth/restore', headers=headers, json=body).status_code == 422
    assert store.no_ui_reads == reads


@pytest.mark.parametrize('mutation', [
    'DELETE FROM remembered_login WHERE id = 1',
    'UPDATE remembered_login SET revision = revision + 1 WHERE id = 1',
    "UPDATE remembered_login SET secret_ref = 'synthetic-replacement' WHERE id = 1",
    'UPDATE remembered_login SET owner_id = 2 WHERE id = 1',
])
def test_changed_grant_during_read_never_binds_or_restores(remembered_client, monkeypatch, mutation):
    client, store, headers = remembered_client
    if 'owner_id = 2' in mutation:
        other = {'email': 'other@example.com', 'username': 'Other',
                 'password': 'synthetic-test-password'}
        assert client.post('/api/v1/auth/register', headers=headers, json=other).status_code == 200
    original_read = store.get_no_ui
    binds = []
    monkeypatch.setattr(auth, 'bind_owner_and_reconcile', lambda *a, **kw: binds.append(1))
    def changed_read(ref):
        value = original_read(ref)
        with client.app.state.session_factory() as db:
            db.execute(text(mutation))
            db.commit()
        return value
    monkeypatch.setattr(store, 'get_no_ui', changed_read)
    assert client.post('/api/v1/auth/restore', headers=headers,
                       json={'expected_owner_id': 1}).status_code == 401
    assert binds == [] and store.ui_reads == 0


def test_expired_or_revoked_grant_cannot_continue(remembered_client):
    client, store, headers = remembered_client
    with client.app.state.session_factory() as db:
        db.execute(text('UPDATE remembered_login SET expires_at = :expired WHERE id = 1'),
                   {'expired': (datetime.utcnow() - timedelta(days=1)).isoformat()})
        db.commit()
    assert client.post('/api/v1/auth/restore', headers=headers,
                       json={'expected_owner_id': 1}).status_code == 401
    assert client.post('/api/v1/auth/logout', headers=headers).status_code == 204
    reads = store.no_ui_reads
    assert client.post('/api/v1/auth/restore', headers=headers,
                       json={'expected_owner_id': 1}).status_code == 401
    assert store.no_ui_reads == reads


def test_same_owner_continuity_preserves_live_core_binding(remembered_client, monkeypatch):
    client, store, headers = remembered_client
    binding = OwnerBinding(1, 'ab' * 32)
    provider = client.app.state.owner_epoch_provider
    monkeypatch.setattr(provider, 'snapshot', lambda: binding)
    monkeypatch.setattr(provider, 'bind', lambda *args: pytest.fail('live Core binding rotated'))
    response = client.post('/api/v1/auth/restore', headers=headers,
                           json={'expected_owner_id': 1})
    assert response.status_code == 200 and provider.snapshot() is binding
    assert store.ui_reads == 0


def test_continuity_does_not_override_another_live_core_owner(remembered_client, monkeypatch):
    client, store, headers = remembered_client
    provider = client.app.state.owner_epoch_provider
    monkeypatch.setattr(provider, 'snapshot', lambda: OwnerBinding(2, 'cd' * 32))
    monkeypatch.setattr(provider, 'bind', lambda *args: pytest.fail('foreign Core owner rebound'))
    monkeypatch.setattr(auth, 'secret_store', lambda *args: pytest.fail('foreign owner store accessed'))
    reads = store.no_ui_reads
    assert client.post('/api/v1/auth/restore', headers=headers,
                       json={'expected_owner_id': 1}).status_code == 401
    assert store.no_ui_reads == reads


def test_core_owner_change_during_read_cancels_continuity(remembered_client, monkeypatch):
    client, store, headers = remembered_client
    provider = client.app.state.owner_epoch_provider
    binding = [OwnerBinding(1, 'ab' * 32)]
    monkeypatch.setattr(provider, 'snapshot', lambda: binding[0])
    monkeypatch.setattr(provider, 'bind', lambda *args: pytest.fail('foreign Core owner rebound'))
    original_read = store.get_no_ui
    def changed_read(ref):
        value = original_read(ref)
        binding[0] = OwnerBinding(2, 'cd' * 32)
        return value
    monkeypatch.setattr(store, 'get_no_ui', changed_read)
    assert client.post('/api/v1/auth/restore', headers=headers,
                       json={'expected_owner_id': 1}).status_code == 401


def test_hintless_same_owner_restore_preserves_verified_live_binding(remembered_client, monkeypatch):
    client, store, headers = remembered_client
    provider = client.app.state.owner_epoch_provider
    binding = OwnerBinding(1, 'ab' * 32)
    monkeypatch.setattr(provider, 'snapshot', lambda: binding)
    monkeypatch.setattr(provider, 'current', lambda owner: binding.owner_epoch if owner == 1 else None)
    monkeypatch.setattr(provider, 'bind', lambda *_: pytest.fail('verified Core binding rotated'))
    reads = store.no_ui_reads
    response = client.post('/api/v1/auth/restore', headers=headers)
    assert response.status_code == 200 and store.no_ui_reads == reads + 1 and store.ui_reads == 0
    assert provider.snapshot() is binding


def test_hintless_invalid_remembered_proof_never_checks_or_binds_owner(remembered_client, monkeypatch):
    client, store, headers = remembered_client
    provider = client.app.state.owner_epoch_provider
    monkeypatch.setattr(provider, 'snapshot', lambda: OwnerBinding(1, 'ab' * 32))
    monkeypatch.setattr(provider, 'current', lambda *_: pytest.fail('proof must be verified first'))
    monkeypatch.setattr(provider, 'bind', lambda *_: pytest.fail('invalid proof rebound owner'))
    monkeypatch.setattr(store, 'get_no_ui', lambda _: 'wrong-synthetic-credential')
    assert client.post('/api/v1/auth/restore', headers=headers).status_code == 401


def test_hintless_changed_remembered_row_never_checks_or_binds_owner(remembered_client, monkeypatch):
    client, store, headers = remembered_client
    provider = client.app.state.owner_epoch_provider
    monkeypatch.setattr(provider, 'snapshot', lambda: OwnerBinding(1, 'ab' * 32))
    monkeypatch.setattr(provider, 'current', lambda *_: pytest.fail('changed row must be rejected first'))
    monkeypatch.setattr(provider, 'bind', lambda *_: pytest.fail('changed row rebound owner'))
    original = store.get_no_ui
    def changed(ref):
        credential = original(ref)
        with client.app.state.session_factory() as db:
            db.execute(text('UPDATE remembered_login SET revision = revision + 1 WHERE id = 1'));db.commit()
        return credential
    monkeypatch.setattr(store, 'get_no_ui', changed)
    assert client.post('/api/v1/auth/restore', headers=headers).status_code == 401


@pytest.mark.parametrize('binding,current', [
    (None, None), (OwnerBinding(1, 'ab' * 32), None),
    (OwnerBinding(1, 'ab' * 32), 'cd' * 32), (OwnerBinding(2, 'cd' * 32), None),
])
def test_hintless_unknown_stale_or_other_owner_keeps_existing_bind_path(remembered_client, monkeypatch, binding, current):
    client, store, headers = remembered_client
    provider = client.app.state.owner_epoch_provider
    binds = []
    monkeypatch.setattr(provider, 'snapshot', lambda: binding)
    monkeypatch.setattr(provider, 'current', lambda _: current)
    monkeypatch.setattr(auth, 'bind_owner_and_reconcile', lambda _request, _db, owner, **_: binds.append(owner))
    assert client.post('/api/v1/auth/restore', headers=headers).status_code == 200
    assert binds == [1] and store.ui_reads == 0


def test_explicit_login_and_logout_keep_bind_and_unbind_paths(remembered_client, monkeypatch):
    client, _, headers = remembered_client
    provider = client.app.state.owner_epoch_provider
    binding = OwnerBinding(1, 'ab' * 32);binds = [];unbinds = []
    monkeypatch.setattr(provider, 'snapshot', lambda: binding)
    monkeypatch.setattr(auth, 'bind_owner_and_reconcile', lambda _request, _db, owner, **_: binds.append(owner))
    monkeypatch.setattr(provider, 'compare_and_unbind', lambda expected: unbinds.append(expected))
    login={'email':'owner@example.com','password':'synthetic-test-password','preserve_remembered_login':True}
    assert client.post('/api/v1/auth/login', headers=headers, json=login).status_code == 200
    assert binds == [1]
    assert client.post('/api/v1/auth/logout', headers=headers).status_code == 204
    assert unbinds == [binding]
