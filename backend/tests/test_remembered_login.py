from fastapi.testclient import TestClient
from sqlalchemy import text
import hashlib
import pytest

from app.core.config import settings
from app.main import create_app
from app.services.secrets import (CoreSecretStore, SecretInteractionRequired,
                                  SecretReentryRequired, SecretStoreUnavailable)


class FakeLoginStore:
    def __init__(self):
        self.value = None
        self.values = {}
        self.deleted = []
        self.fail_put = False
        self.fail_get = False
        self.fail_delete = False

    def put(self, ref, value):
        if self.fail_put:
            raise RuntimeError('synthetic Keychain failure')
        self.values[ref] = value
        self.value = value

    def get(self, ref):
        if self.fail_get:
            raise RuntimeError('synthetic Keychain read failure')
        return self.values.get(ref)

    def get_no_ui(self, ref):
        return self.get(ref)

    def delete(self, ref):
        if self.fail_delete:
            raise RuntimeError('synthetic Keychain deletion failure')
        self.deleted.append(ref)
        removed = self.values.pop(ref, None)
        if removed == self.value:
            self.value = next(reversed(self.values.values()), None)


class NoUiLoginStore(FakeLoginStore):
    def __init__(self):
        super().__init__()
        self.no_ui_reads = 0
        self.ui_reads = 0

    def get_no_ui(self, ref):
        self.no_ui_reads += 1
        return self.values.get(ref)

    def get(self, ref):
        self.ui_reads += 1
        return self.value


def test_remembered_login_restores_after_jwt_key_changes_and_logout_revokes(sqlite_url, monkeypatch):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'remember-test-session')
    h = {'X-Gamja-Session': 'remember-test-session'}
    keychain = FakeLoginStore()
    old_key = settings.SECRET_KEY
    try:
        with TestClient(create_app(database_url=sqlite_url)) as client:
            client.app.state.login_secret_store = keychain
            account = {'email': 'owner@example.com', 'username': 'owner', 'password': 'long-test-password'}
            assert client.post('/api/v1/auth/register', headers=h, json=account).status_code == 200
            assert client.post('/api/v1/auth/restore', headers=h).status_code == 401
            login = client.post('/api/v1/auth/login', headers=h, json={**account, 'remember_device': True})
            assert login.status_code == 200
            first_token = login.json()['access_token']
            assert keychain.value and account['password'] not in keychain.value and first_token not in keychain.value
            with client.app.state.session_factory() as db:
                saved = db.execute(text('SELECT secret_hash FROM remembered_login WHERE id = 1')).scalar_one()
                assert len(saved) == 64 and keychain.value not in saved and first_token not in saved
            assert client.post('/api/v1/auth/restore').status_code == 401
        settings.SECRET_KEY = 'different-key-on-restart'
        with TestClient(create_app(database_url=sqlite_url)) as client:
            client.app.state.login_secret_store = keychain
            assert client.get('/api/v1/auth/me', headers={**h, 'Authorization': f'Bearer {first_token}'}).status_code == 401
            restored = client.post('/api/v1/auth/restore', headers=h)
            assert restored.status_code == 200
            new_token = restored.json()['access_token']
            assert client.get('/api/v1/auth/me', headers={**h, 'Authorization': f'Bearer {new_token}'}).json()['id'] == 1
            keychain.fail_delete = True
            # Logout uses the local-session boundary, so an expired JWT cannot trap a remembered login.
            assert client.post('/api/v1/auth/logout', headers={**h, 'Authorization': 'Bearer expired'}).status_code == 204
            assert client.post('/api/v1/auth/restore', headers=h).status_code == 401
            assert client.post('/api/v1/auth/logout').status_code == 401
    finally:
        settings.SECRET_KEY = old_key


def test_keychain_save_failure_never_claims_remembered_and_plain_login_still_works(sqlite_url, monkeypatch):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'remember-test-session')
    h = {'X-Gamja-Session': 'remember-test-session'}
    keychain = FakeLoginStore()
    keychain.fail_put = True
    with TestClient(create_app(database_url=sqlite_url)) as client:
        client.app.state.login_secret_store = keychain
        account = {'email': 'owner@example.com', 'username': 'owner', 'password': 'long-test-password'}
        assert client.post('/api/v1/auth/register', headers=h, json=account).status_code == 200
        assert client.post('/api/v1/auth/login', headers=h, json={**account, 'remember_device': True}).status_code == 503
        assert client.post('/api/v1/auth/restore', headers=h).status_code == 401
        assert client.post('/api/v1/auth/login', headers=h, json=account).status_code == 200


def test_failed_rotation_keeps_legacy_database_and_secret(sqlite_url, monkeypatch):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'remember-test-session')
    headers = {'X-Gamja-Session': 'remember-test-session'}
    store = FakeLoginStore()
    store.values['current'] = store.value = 'legacy-device-credential'
    account = {'email': 'owner@example.com', 'username': 'owner', 'password': 'long-test-password'}
    with TestClient(create_app(database_url=sqlite_url)) as client:
        client.app.state.login_secret_store = store
        assert client.post('/api/v1/auth/register', headers=headers, json=account).status_code == 200
        with client.app.state.session_factory() as db:
            db.execute(text("""INSERT INTO remembered_login
                (id,owner_id,secret_hash,expires_at,secret_ref,revision)
                VALUES (1,1,:hash,'2099-01-01','current',1)"""),
                       {'hash': hashlib.sha256(store.value.encode()).hexdigest()})
            db.commit()
        store.fail_put = True

        response = client.post('/api/v1/auth/login', headers=headers,
                               json={**account, 'remember_device': True})

        assert response.status_code == 503
        with client.app.state.session_factory() as db:
            assert db.execute(text("SELECT secret_ref,revision FROM remembered_login WHERE id=1")).one() == ('current', 1)
        assert store.values == {'current': 'legacy-device-credential'}
        assert len(store.deleted) == 1 and store.deleted[0] != 'current'


def test_successful_rotation_commits_uuid_ref_and_keeps_legacy_secret_for_rollback(sqlite_url, monkeypatch):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'remember-test-session')
    headers = {'X-Gamja-Session': 'remember-test-session'}
    store = FakeLoginStore()
    store.values['current'] = store.value = 'legacy-device-credential'
    account = {'email': 'owner@example.com', 'username': 'owner', 'password': 'long-test-password'}
    with TestClient(create_app(database_url=sqlite_url)) as client:
        client.app.state.login_secret_store = store
        assert client.post('/api/v1/auth/register', headers=headers, json=account).status_code == 200
        with client.app.state.session_factory() as db:
            db.execute(text("""INSERT INTO remembered_login
                (id,owner_id,secret_hash,expires_at,secret_ref,revision)
                VALUES (1,1,:hash,'2099-01-01','current',1)"""),
                       {'hash': hashlib.sha256(store.value.encode()).hexdigest()})
            db.commit()

        assert client.post('/api/v1/auth/login', headers=headers,
                           json={**account, 'remember_device': True}).status_code == 200

        with client.app.state.session_factory() as db:
            ref, revision = db.execute(text(
                "SELECT secret_ref,revision FROM remembered_login WHERE id=1")).one()
        assert len(ref) == 32 and all(char in '0123456789abcdef' for char in ref)
        assert revision == 2
        assert store.values['current'] == 'legacy-device-credential'
        assert ref in store.values and ref not in store.deleted
        assert client.post('/api/v1/auth/restore', headers=headers).status_code == 200


def test_rotation_readback_failure_cleans_only_uncommitted_uuid(sqlite_url, monkeypatch):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'remember-test-session')
    headers = {'X-Gamja-Session': 'remember-test-session'}
    store = FakeLoginStore()
    store.values['current'] = store.value = 'legacy-device-credential'
    account = {'email': 'owner@example.com', 'username': 'owner', 'password': 'long-test-password'}
    with TestClient(create_app(database_url=sqlite_url)) as client:
        client.app.state.login_secret_store = store
        assert client.post('/api/v1/auth/register', headers=headers, json=account).status_code == 200
        with client.app.state.session_factory() as db:
            db.execute(text("""INSERT INTO remembered_login
                (id,owner_id,secret_hash,expires_at,secret_ref,revision)
                VALUES (1,1,:hash,'2099-01-01','current',1)"""),
                       {'hash': hashlib.sha256(store.value.encode()).hexdigest()})
            db.commit()
        store.fail_get = True

        response = client.post('/api/v1/auth/login', headers=headers,
                               json={**account, 'remember_device': True})

        assert response.status_code == 503
        with client.app.state.session_factory() as db:
            assert db.execute(text("SELECT secret_ref,revision FROM remembered_login WHERE id=1")).one() == ('current', 1)
        assert store.values == {'current': 'legacy-device-credential'}
        assert store.deleted and store.deleted != ['current']


def test_lost_commit_response_keeps_the_committed_uuid(sqlite_url, monkeypatch):
    from app.services.remembered_login import remember

    class LostCommitResponse:
        def __init__(self, session):
            self.session = session

        def __getattr__(self, name):
            return getattr(self.session, name)

        def commit(self):
            self.session.commit()
            raise RuntimeError('synthetic lost commit response')

    store = FakeLoginStore()
    with TestClient(create_app(database_url=sqlite_url)) as client:
        with client.app.state.session_factory() as db:
            db.execute(text("""INSERT INTO users (id,email,username,hashed_password,is_active)
                VALUES (1,'owner@example.com','owner','unused',1)"""))
            db.commit()
            remember(LostCommitResponse(db), store, 1)
        with client.app.state.session_factory() as check:
            reference = check.execute(text(
                "SELECT secret_ref FROM remembered_login WHERE id=1")).scalar_one()

    assert len(reference) == 32 and reference in store.values
    assert reference not in store.deleted


def test_unreadable_commit_result_returns_finite_uncertain_and_retains_uuid(sqlite_url, monkeypatch):
    from fastapi import HTTPException
    from app.services import remembered_login

    class LostCommitResponse:
        def __init__(self, session):
            self.session = session

        def __getattr__(self, name):
            return getattr(self.session, name)

        def commit(self):
            self.session.commit()
            raise RuntimeError('synthetic lost commit response')

    store = FakeLoginStore()
    with TestClient(create_app(database_url=sqlite_url)) as client:
        with client.app.state.session_factory() as db:
            db.execute(text("""INSERT INTO users (id,email,username,hashed_password,is_active)
                VALUES (1,'owner@example.com','owner','unused',1)"""))
            db.commit()
            monkeypatch.setattr(remembered_login, 'Session', lambda **_kwargs: (_ for _ in ()).throw(
                RuntimeError('synthetic unreadable commit result')))
            with pytest.raises(HTTPException) as raised:
                remembered_login.remember(LostCommitResponse(db), store, 1)
        with client.app.state.session_factory() as check:
            reference = check.execute(text(
                "SELECT secret_ref FROM remembered_login WHERE id=1")).scalar_one()

    assert raised.value.status_code == 503
    assert isinstance(raised.value.detail, dict)
    assert raised.value.detail['code'] == 'secret_store_unavailable'
    assert len(raised.value.detail['correlation_id']) == 32
    assert reference in store.values and reference not in store.deleted


def test_precommit_database_failure_keeps_old_pair_and_cleans_staged_uuid(sqlite_url):
    from fastapi import HTTPException
    from app.services.remembered_login import remember

    class FailedBeforeCommit:
        def __init__(self, session):
            self.session = session

        def __getattr__(self, name):
            return getattr(self.session, name)

        def commit(self):
            raise RuntimeError('synthetic precommit failure')

    store = FakeLoginStore()
    store.values['current'] = store.value = 'legacy-device-credential'
    with TestClient(create_app(database_url=sqlite_url)) as client:
        with client.app.state.session_factory() as seed:
            seed.execute(text("""INSERT INTO users (id,email,username,hashed_password,is_active)
                VALUES (1,'owner@example.com','owner','unused',1)"""))
            seed.execute(text("""INSERT INTO remembered_login
                (id,owner_id,secret_hash,expires_at,secret_ref,revision)
                VALUES (1,1,:hash,'2099-01-01','current',1)"""),
                         {'hash': hashlib.sha256(store.value.encode()).hexdigest()})
            seed.commit()
        with client.app.state.session_factory() as db:
            with pytest.raises(HTTPException) as raised:
                remember(FailedBeforeCommit(db), store, 1)
        with client.app.state.session_factory() as check:
            assert check.execute(text(
                "SELECT secret_ref,revision FROM remembered_login WHERE id=1")).one() == ('current', 1)

    assert raised.value.status_code == 503
    assert store.values == {'current': 'legacy-device-credential'}
    assert len(store.deleted) == 1 and store.deleted[0] != 'current'


def test_concurrent_rotation_cas_loser_keeps_winner_and_legacy(sqlite_url, monkeypatch):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'remember-test-session')
    headers = {'X-Gamja-Session': 'remember-test-session'}
    winner_ref = 'b' * 32
    account = {'email': 'owner@example.com', 'username': 'owner', 'password': 'long-test-password'}

    with TestClient(create_app(database_url=sqlite_url)) as client:
        class ConcurrentStore(FakeLoginStore):
            def put(self, ref, value):
                super().put(ref, value)
                self.values[winner_ref] = 'concurrent-device-credential'
                with client.app.state.session_factory() as concurrent:
                    concurrent.execute(text("""UPDATE remembered_login
                        SET secret_ref=:ref,secret_hash=:hash,revision=2 WHERE id=1"""), {
                            'ref': winner_ref,
                            'hash': hashlib.sha256(self.values[winner_ref].encode()).hexdigest(),
                        })
                    concurrent.commit()

        store = ConcurrentStore()
        store.values['current'] = store.value = 'legacy-device-credential'
        client.app.state.login_secret_store = store
        assert client.post('/api/v1/auth/register', headers=headers, json=account).status_code == 200
        with client.app.state.session_factory() as db:
            db.execute(text("""INSERT INTO remembered_login
                (id,owner_id,secret_hash,expires_at,secret_ref,revision)
                VALUES (1,1,:hash,'2099-01-01','current',1)"""),
                       {'hash': hashlib.sha256(store.value.encode()).hexdigest()})
            db.commit()

        response = client.post('/api/v1/auth/login', headers=headers,
                               json={**account, 'remember_device': True})

        assert response.status_code == 409
        with client.app.state.session_factory() as db:
            assert db.execute(text("SELECT secret_ref,revision FROM remembered_login WHERE id=1")).one() == (winner_ref, 2)
        assert store.values['current'] == 'legacy-device-credential'
        assert store.values[winner_ref] == 'concurrent-device-credential'
        assert len(store.deleted) == 1 and store.deleted[0] not in ('current', winner_ref)


def test_plain_login_revokes_dynamic_reference_after_database_commit(sqlite_url, monkeypatch):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'remember-test-session')
    headers = {'X-Gamja-Session': 'remember-test-session'}
    account = {'email': 'owner@example.com', 'username': 'owner', 'password': 'long-test-password'}
    store = FakeLoginStore()
    with TestClient(create_app(database_url=sqlite_url)) as client:
        client.app.state.login_secret_store = store
        assert client.post('/api/v1/auth/register', headers=headers, json=account).status_code == 200
        assert client.post('/api/v1/auth/login', headers=headers,
                           json={**account, 'remember_device': True}).status_code == 200
        with client.app.state.session_factory() as db:
            reference = db.execute(text(
                "SELECT secret_ref FROM remembered_login WHERE id=1")).scalar_one()

        assert client.post('/api/v1/auth/login', headers=headers, json=account).status_code == 200

        with client.app.state.session_factory() as db:
            assert db.execute(text("SELECT 1 FROM remembered_login WHERE id=1")).first() is None
        assert reference in store.deleted and reference not in store.values


def test_revoke_cas_race_reports_failure_without_deleting_stale_secret(sqlite_url):
    from fastapi import HTTPException
    from app.services.remembered_login import revoke

    winner_ref = 'c' * 32
    store = FakeLoginStore()
    store.values['current'] = store.value = 'legacy-device-credential'
    with TestClient(create_app(database_url=sqlite_url)) as client:
        with client.app.state.session_factory() as seed:
            seed.execute(text("""INSERT INTO users (id,email,username,hashed_password,is_active)
                VALUES (1,'owner@example.com','owner','unused',1)"""))
            seed.execute(text("""INSERT INTO remembered_login
                (id,owner_id,secret_hash,expires_at,secret_ref,revision)
                VALUES (1,1,:hash,'2099-01-01','current',1)"""),
                         {'hash': hashlib.sha256(store.value.encode()).hexdigest()})
            seed.commit()

        class RacingSession:
            def __init__(self, session):
                self.session = session

            def __getattr__(self, name):
                return getattr(self.session, name)

            def execute(self, statement, parameters=None):
                if str(statement).lstrip().startswith('DELETE FROM remembered_login'):
                    store.values[winner_ref] = 'concurrent-device-credential'
                    with client.app.state.session_factory() as concurrent:
                        concurrent.execute(text("""UPDATE remembered_login
                            SET secret_ref=:ref,secret_hash=:hash,revision=2 WHERE id=1"""), {
                                'ref': winner_ref,
                                'hash': hashlib.sha256(store.values[winner_ref].encode()).hexdigest(),
                            })
                        concurrent.commit()
                return self.session.execute(statement, parameters or {})

        with client.app.state.session_factory() as db:
            with pytest.raises(HTTPException) as raised:
                revoke(RacingSession(db), store)
        with client.app.state.session_factory() as check:
            assert check.execute(text(
                "SELECT secret_ref,revision FROM remembered_login WHERE id=1")).one() == (winner_ref, 2)

    assert raised.value.status_code == 409
    assert store.deleted == []
    assert store.values['current'] == 'legacy-device-credential'
    assert store.values[winner_ref] == 'concurrent-device-credential'


def test_automatic_restore_uses_noninteractive_secret_read(sqlite_url, monkeypatch):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'remember-test-session')
    h = {'X-Gamja-Session': 'remember-test-session'}
    keychain = NoUiLoginStore()
    with TestClient(create_app(database_url=sqlite_url)) as client:
        client.app.state.login_secret_store = keychain
        account = {'email': 'owner@example.com', 'username': 'owner', 'password': 'long-test-password'}
        assert client.post('/api/v1/auth/register', headers=h, json=account).status_code == 200
        assert client.post('/api/v1/auth/login', headers=h, json={**account, 'remember_device': True}).status_code == 200
        assert client.post('/api/v1/auth/restore', headers=h).status_code == 200
    assert keychain.no_ui_reads == 2  # save readback plus restore
    assert keychain.ui_reads == 0


def test_core_credential_restore_requests_manual_sign_in_without_retrying_keychain(sqlite_url, monkeypatch):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'remember-test-session')
    h = {'X-Gamja-Session': 'remember-test-session'}

    class NoCallsCore:
        def call(self, *_args):
            raise AssertionError('stable Core recovery must not repeat the failed read')

    with TestClient(create_app(database_url=sqlite_url)) as client:
        client.app.state.login_secret_store = CoreSecretStore(NoCallsCore(), 'remembered_login')
        response = client.post('/api/v1/auth/restore/interactive', headers=h)

    assert response.status_code == 202
    assert response.json() == {'state': 'reentry_required', 'action': 'sign_in_again'}


def test_manual_account_switch_revokes_prior_remembered_account(sqlite_url, monkeypatch):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'remember-test-session')
    h = {'X-Gamja-Session': 'remember-test-session'}
    keychain = FakeLoginStore()
    with TestClient(create_app(database_url=sqlite_url)) as client:
        client.app.state.login_secret_store = keychain
        a = {'email': 'a@example.com', 'username': 'a', 'password': 'long-test-password'}
        b = {'email': 'b@example.com', 'username': 'b', 'password': 'long-test-password'}
        for account in (a, b):
            assert client.post('/api/v1/auth/register', headers=h, json=account).status_code == 200
        assert client.post('/api/v1/auth/login', headers=h, json={**a, 'remember_device': True}).status_code == 200
        assert client.post('/api/v1/auth/restore', headers=h).status_code == 200
        assert client.post('/api/v1/auth/login', headers=h, json=b).status_code == 200
        assert client.post('/api/v1/auth/restore', headers=h).status_code == 401


def test_cli_login_preserves_existing_remembered_login(sqlite_url, monkeypatch):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'remember-test-session')
    h = {'X-Gamja-Session': 'remember-test-session'}
    keychain = FakeLoginStore()
    with TestClient(create_app(database_url=sqlite_url)) as client:
        client.app.state.login_secret_store = keychain
        account = {'email': 'owner@example.com', 'username': 'owner', 'password': 'long-test-password'}
        assert client.post('/api/v1/auth/register', headers=h, json=account).status_code == 200
        assert client.post('/api/v1/auth/login', headers=h, json={**account, 'remember_device': True}).status_code == 200
        saved_secret = keychain.value
        assert client.post('/api/v1/auth/login', headers=h, json={
            **account, 'remember_device': False, 'preserve_remembered_login': True,
        }).status_code == 200
        assert keychain.value == saved_secret
        assert client.post('/api/v1/auth/restore', headers=h).status_code == 200


@pytest.mark.parametrize(("failure", "code", "reason"), [
    (SecretReentryRequired(), "credential_locked", "credential_reentry_required"),
    (SecretInteractionRequired("keychain_interaction_required", -25308),
     "credential_locked", "keychain_interaction_required"),
    (SecretStoreUnavailable("keychain_query_failed", -25293),
     "secret_store_unavailable", "keychain_query_failed"),
])
def test_restore_failure_is_classified_without_secret_text(sqlite_url, monkeypatch, failure, code, reason):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'remember-test-session')
    h = {'X-Gamja-Session': 'remember-test-session'}

    class FailingStore:
        def get_no_ui(self, _ref):
            raise failure

    with TestClient(create_app(database_url=sqlite_url)) as client:
        client.app.state.login_secret_store = FailingStore()
        response = client.post('/api/v1/auth/restore', headers=h)
        assert response.status_code == 503
        detail = response.json()['detail']
        assert set(detail) == ({'code', 'correlation_id', 'action'} if reason == 'credential_reentry_required'
                               else {'code', 'correlation_id'})
        assert detail['code'] == code
        assert len(detail['correlation_id']) == 32
        diagnostic = client.get(f"/api/v1/auth/restore/diagnostics/{detail['correlation_id']}", headers=h)
        assert diagnostic.status_code == 200
        assert diagnostic.json() == {
            'correlation_id': detail['correlation_id'], 'stage': 'auth_restore',
            'code': code, 'reason': reason,
            'error_class': ('credential_reentry_required' if reason == 'credential_reentry_required' else
                            'secret_interaction_required' if code == 'credential_locked' else 'secret_store_unavailable'),
            **({'os_status': -25308 if code == 'credential_locked' else -25293}
               if reason != 'credential_reentry_required' else {}),
        }
        if reason == 'credential_reentry_required':
            assert detail['action'] == 'sign_in_again'


def test_restore_failure_never_echoes_untrusted_reason(sqlite_url, monkeypatch):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'remember-test-session')
    h = {'X-Gamja-Session': 'remember-test-session'}

    class FailingStore:
        def get_no_ui(self, _ref):
            raise SecretStoreUnavailable('sentinel-secret-value', -25293)

    with TestClient(create_app(database_url=sqlite_url)) as client:
        client.app.state.login_secret_store = FailingStore()
        response = client.post('/api/v1/auth/restore', headers=h)
        encoded = response.text
        correlation = response.json()['detail']['correlation_id']
        diagnostic = client.get(f'/api/v1/auth/restore/diagnostics/{correlation}', headers=h)
        assert 'sentinel-secret-value' not in encoded
        assert 'sentinel-secret-value' not in diagnostic.text
        assert diagnostic.json()['reason'] == 'keychain_store_unavailable'


@pytest.mark.parametrize(("failure", "code", "reason", "expected_cleanup"), [
    (SecretStoreUnavailable("credential_channel_unavailable", error_class="bridge_transport"),
     "secret_store_unavailable", "credential_channel_unavailable", "success"),
    (SecretStoreUnavailable("credential_response_invalid", error_class="bridge_protocol"),
     "secret_store_unavailable", "credential_response_invalid", "success"),
    (SecretStoreUnavailable("credential_request_invalid", error_class="request_validation"),
     "secret_store_unavailable", "credential_request_invalid", "success"),
    (SecretStoreUnavailable("keychain_query_failed", -25293, operation="sec_item_add", error_class="native_os_status"),
     "secret_store_unavailable", "keychain_query_failed", "success"),
    (SecretInteractionRequired("keychain_interaction_required", -25308, operation="sec_item_add"),
     "credential_locked", "keychain_interaction_required", "success"),
])
def test_remember_failure_records_diagnostic_with_cleanup_status(sqlite_url, monkeypatch, failure, code, reason, expected_cleanup):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'remember-test-session')
    h = {'X-Gamja-Session': 'remember-test-session'}

    class FailingStore:
        def __init__(self):
            self.deleted = []
        def put(self, _ref, _value):
            raise failure
        def delete(self, ref):
            self.deleted.append(ref)

    store = FailingStore()
    with TestClient(create_app(database_url=sqlite_url)) as client:
        client.app.state.login_secret_store = store
        account = {'email': 'owner@example.com', 'username': 'owner', 'password': 'long-test-password'}
        assert client.post('/api/v1/auth/register', headers=h, json=account).status_code == 200
        response = client.post('/api/v1/auth/login', headers=h, json={**account, 'remember_device': True})
        assert response.status_code == 503
        detail = response.json()['detail']
        assert set(detail) == {'code', 'correlation_id'}
        assert detail['code'] == code
        assert len(detail['correlation_id']) == 32
        diagnostic = client.get(f"/api/v1/auth/remember/diagnostics/{detail['correlation_id']}", headers=h)
        assert diagnostic.status_code == 200
        diag_json = diagnostic.json()
        assert diag_json['correlation_id'] == detail['correlation_id']
        assert diag_json['stage'] == 'auth_remember'
        assert diag_json['code'] == code
        assert diag_json['reason'] == reason
        assert diag_json['cleanup'] == expected_cleanup
        if hasattr(failure, 'operation') and failure.operation:
            assert diag_json['operation'] == failure.operation
        if hasattr(failure, 'os_status') and failure.os_status is not None:
            assert diag_json['os_status'] == failure.os_status


def test_remember_failure_with_cleanup_failure_reports_failed_status(sqlite_url, monkeypatch):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'remember-test-session')
    h = {'X-Gamja-Session': 'remember-test-session'}

    class FailingStore:
        def put(self, _ref, _value):
            raise SecretStoreUnavailable("keychain_query_failed", -25293, operation="sec_item_add")
        def delete(self, _ref):
            raise RuntimeError("synthetic cleanup failure")

    with TestClient(create_app(database_url=sqlite_url)) as client:
        client.app.state.login_secret_store = FailingStore()
        account = {'email': 'owner@example.com', 'username': 'owner', 'password': 'long-test-password'}
        assert client.post('/api/v1/auth/register', headers=h, json=account).status_code == 200
        response = client.post('/api/v1/auth/login', headers=h, json={**account, 'remember_device': True})
        assert response.status_code == 503
        correlation = response.json()['detail']['correlation_id']
        diagnostic = client.get(f"/api/v1/auth/remember/diagnostics/{correlation}", headers=h)
        assert diagnostic.status_code == 200
        assert diagnostic.json()['cleanup'] == 'failed'


def test_remember_readback_failure_records_runtime_error_diagnostic(sqlite_url, monkeypatch):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'remember-test-session')
    h = {'X-Gamja-Session': 'remember-test-session'}

    class ReadbackFailingStore:
        def __init__(self):
            self.deleted = []
        def put(self, ref, value):
            self.values[ref] = value
        def get_no_ui(self, ref):
            return None  # Simulate readback failure
        def delete(self, ref):
            self.deleted.append(ref)
        values = {}

    store = ReadbackFailingStore()
    with TestClient(create_app(database_url=sqlite_url)) as client:
        client.app.state.login_secret_store = store
        account = {'email': 'owner@example.com', 'username': 'owner', 'password': 'long-test-password'}
        assert client.post('/api/v1/auth/register', headers=h, json=account).status_code == 200
        response = client.post('/api/v1/auth/login', headers=h, json={**account, 'remember_device': True})
        assert response.status_code == 503
        correlation = response.json()['detail']['correlation_id']
        diagnostic = client.get(f"/api/v1/auth/remember/diagnostics/{correlation}", headers=h)
        assert diagnostic.status_code == 200
        diag_json = diagnostic.json()
        assert diag_json['stage'] == 'auth_remember'
        assert diag_json['code'] == 'secret_store_unavailable'
        assert diag_json['reason'] == 'automatic_login_readback_failed'
        assert diag_json['error_class'] == 'store_unavailable'
        assert diag_json['cleanup'] == 'success'


def test_remember_diagnostic_never_contains_secret_value_or_reference(sqlite_url, monkeypatch):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'remember-test-session')
    h = {'X-Gamja-Session': 'remember-test-session'}

    class FailingStore:
        def put(self, _ref, _value):
            raise SecretStoreUnavailable('sentinel-secret-reason', -25293)
        def delete(self, _ref):
            pass

    with TestClient(create_app(database_url=sqlite_url)) as client:
        client.app.state.login_secret_store = FailingStore()
        account = {'email': 'owner@example.com', 'username': 'owner', 'password': 'long-test-password'}
        assert client.post('/api/v1/auth/register', headers=h, json=account).status_code == 200
        response = client.post('/api/v1/auth/login', headers=h, json={**account, 'remember_device': True})
        correlation = response.json()['detail']['correlation_id']
        diagnostic = client.get(f"/api/v1/auth/remember/diagnostics/{correlation}", headers=h)
        diag_text = diagnostic.text
        assert 'sentinel-secret-reason' not in diag_text
        assert 'password' not in diag_text.lower()
        assert 'secret' not in diag_text.lower() or 'secret_store_unavailable' in diag_text


def test_explicit_temporary_login_preserves_matching_saved_slot_without_keychain(sqlite_url, monkeypatch):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'remember-test-session')
    headers = {'X-Gamja-Session': 'remember-test-session'}
    store = FakeLoginStore()
    with TestClient(create_app(database_url=sqlite_url)) as client:
        client.app.state.login_secret_store = store
        account = {'email': 'same@example.com', 'username': 'same', 'password': 'long-test-password'}
        assert client.post('/api/v1/auth/register', headers=headers, json=account).status_code == 200
        assert client.post('/api/v1/auth/login', headers=headers, json={**account, 'remember_device': True}).status_code == 200
        with client.app.state.session_factory() as db:
            original = tuple(db.execute(text('SELECT owner_id,secret_ref,secret_hash,revision FROM remembered_login WHERE id=1')).one())
        saved = dict(store.values)
        binds = []
        monkeypatch.setattr(client.app.state.owner_epoch_provider, 'bind', lambda owner: binds.append(owner))
        monkeypatch.setattr(store, 'get_no_ui', lambda _ref: (_ for _ in ()).throw(AssertionError('unexpected keychain read')))
        monkeypatch.setattr(store, 'delete', lambda _ref: (_ for _ in ()).throw(AssertionError('unexpected keychain deletion')))
        response = client.post('/api/v1/auth/login/session-only', headers=headers, json={**account, 'preserve_saved_login_for_session': True})
        assert response.status_code == 200 and binds == [original[0]]
        with client.app.state.session_factory() as db:
            assert tuple(db.execute(text('SELECT owner_id,secret_ref,secret_hash,revision FROM remembered_login WHERE id=1')).one()) == original
        assert store.values == saved
        assert client.get('/api/v1/auth/me', headers={**headers, 'Authorization': 'Bearer '+response.json()['access_token']}).json()['id'] == original[0]


def test_explicit_temporary_login_rejects_other_owner_and_conflicting_choices(sqlite_url, monkeypatch):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'remember-test-session')
    headers = {'X-Gamja-Session': 'remember-test-session'}
    store = FakeLoginStore()
    with TestClient(create_app(database_url=sqlite_url)) as client:
        client.app.state.login_secret_store = store
        a = {'email': 'a@example.com', 'username': 'a', 'password': 'long-test-password'}
        b = {'email': 'b@example.com', 'username': 'b', 'password': 'long-test-password'}
        for account in (a, b): assert client.post('/api/v1/auth/register', headers=headers, json=account).status_code == 200
        assert client.post('/api/v1/auth/login', headers=headers, json={**a, 'remember_device': True}).status_code == 200
        with client.app.state.session_factory() as db:
            original = tuple(db.execute(text('SELECT owner_id,secret_ref,revision FROM remembered_login WHERE id=1')).one())
        saved = dict(store.values)
        response = client.post('/api/v1/auth/login/session-only', headers=headers, json={**b, 'preserve_saved_login_for_session': True})
        assert response.status_code == 409 and response.json()['detail']['code'] == 'saved_login_owner_mismatch'
        assert client.post('/api/v1/auth/login/session-only', headers=headers, json={**a, 'preserve_saved_login_for_session': True, 'remember_device': True}).status_code == 422
        assert client.post('/api/v1/auth/login/session-only', headers=headers, json={**a, 'preserve_saved_login_for_session': 'true'}).status_code == 422
        assert client.post('/api/v1/auth/login', headers=headers, json={**a, 'preserve_saved_login_for_session': True}).status_code == 422
        assert client.post('/api/v1/auth/login/session-only', headers=headers, json=a).status_code == 422
        with client.app.state.session_factory() as db:
            assert tuple(db.execute(text('SELECT owner_id,secret_ref,revision FROM remembered_login WHERE id=1')).one()) == original
        assert store.values == saved


def test_explicit_temporary_login_rejects_slot_cas_conflict_before_rebinding(sqlite_url, monkeypatch):
    from sqlalchemy.orm import Session
    from types import SimpleNamespace
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'remember-test-session')
    headers = {'X-Gamja-Session': 'remember-test-session'}
    store = FakeLoginStore()
    with TestClient(create_app(database_url=sqlite_url)) as client:
        client.app.state.login_secret_store = store
        account = {'email': 'cas@example.com', 'username': 'cas', 'password': 'long-test-password'}
        assert client.post('/api/v1/auth/register', headers=headers, json=account).status_code == 200
        assert client.post('/api/v1/auth/login', headers=headers, json={**account, 'remember_device': True}).status_code == 200
        with client.app.state.session_factory() as db:
            original = tuple(db.execute(text('SELECT owner_id,secret_ref,secret_hash,revision FROM remembered_login WHERE id=1')).one())
        saved = dict(store.values)
        binds = []
        monkeypatch.setattr(client.app.state.owner_epoch_provider, 'bind', lambda owner: binds.append(owner))
        execute = Session.execute
        def simulated_conflict(session, statement, *args, **kwargs):
            if str(statement).lstrip().startswith('UPDATE remembered_login SET revision=revision'):
                return SimpleNamespace(rowcount=0)
            return execute(session, statement, *args, **kwargs)
        monkeypatch.setattr(Session, 'execute', simulated_conflict)
        response = client.post('/api/v1/auth/login/session-only', headers=headers, json={**account, 'preserve_saved_login_for_session': True})
        assert response.status_code == 409 and response.json()['detail']['code'] == 'saved_login_changed'
        assert binds == [] and store.values == saved
        with client.app.state.session_factory() as db:
            assert tuple(db.execute(text('SELECT owner_id,secret_ref,secret_hash,revision FROM remembered_login WHERE id=1')).one()) == original


def test_explicit_temporary_login_rejects_concurrent_slot_revision_change(sqlite_url, monkeypatch):
    from sqlalchemy.orm import Session
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'remember-test-session')
    headers = {'X-Gamja-Session': 'remember-test-session'}
    store = FakeLoginStore()
    with TestClient(create_app(database_url=sqlite_url)) as client:
        client.app.state.login_secret_store = store
        account = {'email': 'race@example.com', 'username': 'race', 'password': 'long-test-password'}
        assert client.post('/api/v1/auth/register', headers=headers, json=account).status_code == 200
        assert client.post('/api/v1/auth/login', headers=headers, json={**account, 'remember_device': True}).status_code == 200
        with client.app.state.session_factory() as db:
            original = tuple(db.execute(text('SELECT owner_id,secret_ref,revision FROM remembered_login WHERE id=1')).one())
        saved = dict(store.values)
        binds = []
        monkeypatch.setattr(client.app.state.owner_epoch_provider, 'bind', lambda owner: binds.append(owner))
        execute = Session.execute
        switched = []
        class RacingResult:
            def __init__(self, result): self.result = result
            def first(self):
                value = self.result.first()
                with client.app.state.session_factory() as rival:
                    execute(rival, text('UPDATE remembered_login SET revision=revision+1 WHERE id=1'))
                    rival.commit()
                switched.append(True)
                return value
        def execute_with_race(session, statement, *args, **kwargs):
            result = execute(session, statement, *args, **kwargs)
            if not switched and str(statement).startswith('SELECT owner_id,secret_ref,revision FROM remembered_login'):
                return RacingResult(result)
            return result
        monkeypatch.setattr(Session, 'execute', execute_with_race)
        response = client.post('/api/v1/auth/login/session-only', headers=headers, json={**account, 'preserve_saved_login_for_session': True})
        assert response.status_code == 409 and response.json()['detail']['code'] == 'saved_login_changed'
        assert switched == [True] and binds == [] and store.values == saved
        with client.app.state.session_factory() as db:
            winner = tuple(db.execute(text('SELECT owner_id,secret_ref,revision FROM remembered_login WHERE id=1')).one())
        assert winner == (original[0], original[1], original[2]+1)
