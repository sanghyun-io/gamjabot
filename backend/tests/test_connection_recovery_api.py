"""Owner-scoped public API for durable connection recovery."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.routes.auth import create_access_token
from app.main import RuntimeConfig, create_app
from app.models.collaboration import ProviderConnection
from app.models.models import User
from app.services.auth_operations import AuthOperationError
from app.services.connection_recovery import LifecycleResult


SESSION = "connection-recovery-api-session"
OPERATION = "a" * 64
OTHER_OPERATION = "b" * 64


class RecordingCoordinator:
    def __init__(self):
        self.calls = []
        self.expected_connection = None
        self.disconnect_result = LifecycleResult(
            OPERATION, "succeeded",
            result={"credential_state": "missing", "revoke_state": "skipped"},
        )
        self.status_result = LifecycleResult(OPERATION, "finalizing")
        self.cancel_result = LifecycleResult(
            OPERATION, "cancelled",
            result={"credential_state": "refresh_required", "revoke_state": "skipped"},
            error_code="operation_cancelled",
        )

    def disconnect(self, db, row, revoke_remote, *, idempotency_key=None,
                   remote_revoker=None):
        self.calls.append(("disconnect", row.user_id, row.id, revoke_remote,
                           idempotency_key, remote_revoker))
        return self.disconnect_result

    def operation_status(self, db, owner_id, connection_id, operation_id):
        self.calls.append(("status", owner_id, connection_id, operation_id))
        if (operation_id == OTHER_OPERATION
                or connection_id != self.expected_connection):
            raise AuthOperationError("configuration_changed")
        return self.status_result

    def cancel_operation(self, db, owner_id, connection_id, operation_id):
        self.calls.append(("cancel", owner_id, connection_id, operation_id))
        if (operation_id == OTHER_OPERATION
                or connection_id != self.expected_connection):
            raise AuthOperationError("configuration_changed")
        return self.cancel_result

    def reconcile_owner(self, db, owner_id, *, limit=25):
        self.calls.append(("reconcile", owner_id, limit))
        return []

    def sweep_terminal_receipts(self, db, owner_id, *, terminal_before, limit=25):
        self.calls.append(("sweep", owner_id, limit))
        return 0


class BindProvider:
    def __init__(self, acknowledged):
        self.acknowledged = acknowledged
        self.calls = []
        self.binding = None

    def bind(self, owner_id):
        self.calls.append(owner_id)
        self.binding = (SimpleNamespace(owner_id=owner_id)
                        if self.acknowledged else None)
        return self.binding

    def snapshot(self):
        return self.binding


@pytest.fixture
def api(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", SESSION)
    app = create_app(runtime_config=RuntimeConfig(
        database_url=sqlite_url, local_session_token=SESSION))
    with TestClient(app, raise_server_exceptions=False) as client:
        with app.state.session_factory() as db:
            owner = User(email="owner-recovery@example.com", username="owner-recovery",
                         hashed_password="unused")
            foreign = User(email="foreign-recovery@example.com", username="foreign-recovery",
                           hashed_password="unused")
            db.add_all((owner, foreign))
            db.flush()
            rows = [
                ProviderConnection(
                    user_id=user.id, kind="compatible", name=f"Connection {user.id}",
                    base_url="https://example.test/v1", secret_ref=None,
                    default_model_id="vendor/model", enabled=True, auth_required=False,
                    capabilities={"declared": {}, "verified": {}}, readiness_revision=1,
                    provider_id="ollama", protocol_id="openai_chat_completions",
                    auth_method_id="local_none", public_config_version=1,
                    public_config={}, auth_grant_revision=0,
                    credential_state="not_required",
                )
                for user in (owner, foreign, owner)
            ]
            db.add_all(rows)
            db.commit()
            owner_id, foreign_id = owner.id, foreign.id
            owner_connection, foreign_connection, other_owner_connection = (
                rows[0].id, rows[1].id, rows[2].id)
        coordinator = RecordingCoordinator()
        coordinator.expected_connection = owner_connection
        app.state.connection_recovery = coordinator

        def headers(owner_id):
            return {
                "X-Gamja-Session": SESSION,
                "Authorization": f"Bearer {create_access_token({'sub': owner_id})}",
            }

        yield SimpleNamespace(
            client=client, app=app, coordinator=coordinator,
            owner_id=owner_id, foreign_id=foreign_id,
            owner_connection=owner_connection,
            other_owner_connection=other_owner_connection,
            foreign_connection=foreign_connection, headers=headers,
        )


def test_disconnect_keeps_exact_legacy_200_and_passes_idempotency_key(api):
    response = api.client.post(
        f"/api/v1/connections/{api.owner_connection}/disconnect",
        headers={**api.headers(api.owner_id), "Idempotency-Key": "client-retry-1"},
        json={"version": 1, "revoke_remote": False},
    )

    assert response.status_code == 200
    assert response.json() == {
        "credential_state": "missing", "revoke_state": "skipped"}
    assert api.coordinator.calls == [(
        "disconnect", api.owner_id, api.owner_connection, False,
        "client-retry-1", None,
    )]


@pytest.mark.parametrize("state", ["pending", "finalizing"])
def test_disconnect_pending_response_is_the_closed_202_projection(api, state):
    api.coordinator.disconnect_result = LifecycleResult(OPERATION, state)

    response = api.client.post(
        f"/api/v1/connections/{api.owner_connection}/disconnect",
        headers=api.headers(api.owner_id),
        json={"version": 1, "revoke_remote": True},
    )

    assert response.status_code == 202
    assert response.json() == {
        "version": 1,
        "operation_id": OPERATION,
        "state": state,
        "expires_at": None,
        "poll_interval": 1,
    }


def test_status_and_cancel_expose_only_confirmed_closed_truth(api):
    path = f"/api/v1/connections/{api.owner_connection}/auth/operations/{OPERATION}"

    status_response = api.client.get(path, headers=api.headers(api.owner_id))
    cancel_response = api.client.delete(path, headers=api.headers(api.owner_id))

    assert status_response.status_code == 202
    assert status_response.json() == {
        "version": 1, "operation_id": OPERATION, "state": "finalizing",
        "expires_at": None, "poll_interval": 1,
    }
    assert cancel_response.status_code == 200
    assert cancel_response.json() == {
        "version": 1, "operation_id": OPERATION, "state": "cancelled",
        "result": {
            "credential_state": "refresh_required", "revoke_state": "skipped"},
        "error_code": "operation_cancelled",
    }


@pytest.mark.parametrize("method", ["get", "delete"])
def test_operation_is_exactly_owner_and_connection_scoped(api, method):
    foreign_path = (
        f"/api/v1/connections/{api.owner_connection}/auth/operations/{OPERATION}")
    mismatched_path = (
        f"/api/v1/connections/{api.other_owner_connection}/auth/operations/{OPERATION}")

    foreign = getattr(api.client, method)(foreign_path, headers=api.headers(api.foreign_id))
    assert foreign.status_code == 404
    assert api.coordinator.calls == []
    mismatched = getattr(api.client, method)(mismatched_path, headers=api.headers(api.owner_id))

    assert mismatched.status_code == 404
    assert api.coordinator.calls == [
        ("status" if method == "get" else "cancel",
         api.owner_id, api.other_owner_connection, OPERATION)]


def test_private_or_open_lifecycle_values_never_cross_the_api(api, caplog):
    api.coordinator.status_result = LifecycleResult(
        OPERATION, "failed",
        result={"credential_state": "present", "revoke_state": "skipped",
                "nonce": "sentinel-private-nonce"},
        error_code="internal_error",
    )
    path = f"/api/v1/connections/{api.owner_connection}/auth/operations/{OPERATION}"

    response = api.client.get(path, headers=api.headers(api.owner_id))

    assert response.status_code == 500
    assert "sentinel" not in response.text
    assert "nonce" not in response.text
    assert "sentinel" not in caplog.text
    assert "nonce" not in caplog.text


def test_owner_recovery_starts_only_after_a_successful_private_bind_ack(
        sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", SESSION)
    app = create_app(runtime_config=RuntimeConfig(
        database_url=sqlite_url, local_session_token=SESSION))
    coordinator = RecordingCoordinator()
    headers = {"X-Gamja-Session": SESSION}
    account = {
        "email": "bind-recovery@example.com", "username": "bind-recovery",
        "password": "long-test-password",
    }
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/register", headers=headers, json=account).status_code == 200
        app.state.connection_recovery = coordinator
        unbound = BindProvider(acknowledged=False)
        app.state.owner_epoch_provider = unbound
        assert client.post("/api/v1/auth/login", headers=headers,
                           json={**account, "preserve_remembered_login": True}).status_code == 200
        assert [call for call in coordinator.calls if call[0] == "reconcile"] == []

        bound = BindProvider(acknowledged=True)
        app.state.owner_epoch_provider = bound
        assert client.post("/api/v1/auth/login", headers=headers,
                           json={**account, "preserve_remembered_login": True}).status_code == 200

    reconcile = [call for call in coordinator.calls if call[0] == "reconcile"]
    assert reconcile == [("reconcile", 1, 25)]


def test_first_canonical_owner_request_after_restart_binds_then_reconciles(api):
    provider = BindProvider(acknowledged=True)
    api.app.state.owner_epoch_provider = provider

    response = api.client.get(
        "/api/v1/auth/me", headers=api.headers(api.owner_id))
    again = api.client.get(
        "/api/v1/auth/me", headers=api.headers(api.owner_id))

    assert response.status_code == again.status_code == 200
    assert provider.calls == [api.owner_id]
    assert [call for call in api.coordinator.calls if call[0] == "reconcile"] == [
        ("reconcile", api.owner_id, 25)]


def test_runtime_wires_recovery_only_for_the_private_broker(
        sqlite_url, monkeypatch):
    from app.services import core_auth_broker

    class Broker:
        available = True

        def __init__(self):
            self.actions = []

        def set_poison_callback(self, callback):
            self.callback = callback

        def close(self):
            self.actions.append("close")

    broker = Broker()
    monkeypatch.setattr(core_auth_broker.CoreAuthBrokerClient, "from_fd",
                        lambda _descriptor: broker)
    app = create_app(runtime_config=RuntimeConfig(
        database_url=sqlite_url, local_session_token=SESSION, auth_broker_fd=99))

    assert not hasattr(app.state, "connection_recovery")
    with TestClient(app):
        assert app.state.connection_recovery._broker is broker
        assert broker.actions == []
    assert broker.actions == ["close"]


def test_managed_runtime_without_coordinator_fails_closed_before_legacy_mutation(
        api, monkeypatch):
    from app.api.routes import connections as connection_routes

    api.app.state.provider_recovery_mode = "core_managed_recovery_required"
    del api.app.state.connection_recovery

    def forbidden_legacy_disconnect(*_args, **_kwargs):
        raise AssertionError("managed recovery must not use the legacy mutation path")

    monkeypatch.setattr(
        connection_routes.connections, "disconnect_connection",
        forbidden_legacy_disconnect,
    )

    response = api.client.post(
        f"/api/v1/connections/{api.owner_connection}/disconnect",
        headers={
            **api.headers(api.owner_id),
            "X-Gamja-Provider-Recovery-Mode": "legacy",
        },
        json={"version": 1, "revoke_remote": False},
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "owner_epoch_unavailable"}}
    body_spoof = api.client.post(
        f"/api/v1/connections/{api.owner_connection}/disconnect",
        headers=api.headers(api.owner_id),
        json={
            "version": 1,
            "revoke_remote": False,
            "provider_recovery_mode": "legacy",
        },
    )
    assert body_spoof.status_code == 422
    assert body_spoof.json() == {
        "detail": {"code": "invalid_request", "message": "Invalid connection input."}}
    with api.app.state.session_factory() as db:
        row = db.get(ProviderConnection, api.owner_connection)
        assert row.credential_state == "not_required"
        assert row.secret_ref is None


def test_explicit_legacy_runtime_without_coordinator_keeps_immediate_200(api):
    api.app.state.provider_recovery_mode = "legacy"
    del api.app.state.connection_recovery

    response = api.client.post(
        f"/api/v1/connections/{api.owner_connection}/disconnect",
        headers=api.headers(api.owner_id),
        json={"version": 1, "revoke_remote": False},
    )

    assert response.status_code == 200
    assert response.json() == {
        "credential_state": "missing", "revoke_state": "skipped"}


def test_managed_broker_attach_failure_keeps_login_available_and_recovery_closed(
        sqlite_url, monkeypatch):
    from app.services import core_auth_broker
    from app.services.core_auth_broker import CoreAuthBrokerUnavailable

    monkeypatch.setattr(
        core_auth_broker.CoreAuthBrokerClient, "from_fd",
        lambda _descriptor: (_ for _ in ()).throw(CoreAuthBrokerUnavailable()),
    )

    app = create_app(runtime_config=RuntimeConfig(
        database_url=sqlite_url,
        local_session_token=SESSION,
        auth_broker_fd=99,
        provider_recovery_mode="core_managed_recovery_required",
    ))

    assert not hasattr(app.state, "connection_recovery")
    assert app.state.provider_recovery_mode == "core_managed_recovery_required"
    with TestClient(app) as client:
        from app.api.routes.auth import get_password_hash

        with app.state.session_factory() as db:
            db.add(User(
                email="attach-failure@example.com", username="attach-failure",
                hashed_password=get_password_hash("passphrase"),
            ))
            db.commit()
        response = client.post(
            "/api/v1/auth/login",
            headers={"X-Gamja-Session": SESSION},
            json={
                "email": "attach-failure@example.com",
                "password": "passphrase",
                "remember_device": False,
            },
        )
        assert response.status_code == 200
        assert set(response.json()) == {"access_token", "token_type"}


class ActualRecoveryBroker:
    current_generation_fingerprint = "c" * 64

    def __init__(self, *, prepare_state="prepared"):
        self.prepare_state = prepare_state
        self.generation_reads = 0
        self.calls = []

    def operation_allocate(self, owner_id, current_owner_epoch):
        self.calls.append(("allocate", owner_id, current_owner_epoch))
        return OPERATION

    def credential_prepare(self, binding, *, exact_old_reference=None):
        from app.services.core_auth_broker import AuthPrivateReply, AuthPublicState

        self.calls.append(("prepare", binding["operation_id"], exact_old_reference))
        return AuthPrivateReply(AuthPublicState(
            version=1,
            operation_id=binding["operation_id"],
            state=self.prepare_state,
            code=(None if self.prepare_state == "prepared"
                  else "provider_temporary_failure"),
        ))

    def credential_discard_recovery(self, selector, action, **_kwargs):
        self.calls.append((action, selector["operation_id"]))
        return SimpleNamespace(
            operation_id=selector["operation_id"], state="absent",
            material_state="none", receipt_state="none",
            binding_fingerprint=None, code=None,
        )


class ActualOwnerEpochs:
    def __init__(self):
        self.available = True
        self.calls = 0

    def current(self, _owner_id):
        self.calls += 1
        return "d" * 64 if self.available else None


def actual_coordinator(broker, owner_epochs):
    from app.services.connection_recovery import ConnectionRecoveryCoordinator

    return ConnectionRecoveryCoordinator(
        broker=broker, owner_epochs=owner_epochs,
    )


def test_actual_service_replays_terminal_reply_loss_before_owner_or_core_access(api):
    broker = ActualRecoveryBroker()
    owner_epochs = ActualOwnerEpochs()
    api.app.state.connection_recovery = actual_coordinator(broker, owner_epochs)
    headers = {**api.headers(api.owner_id), "Idempotency-Key": "reply-loss-key"}
    path = f"/api/v1/connections/{api.owner_connection}/disconnect"

    first = api.client.post(
        path, headers=headers, json={"version": 1, "revoke_remote": False})
    first_epoch_calls = owner_epochs.calls
    owner_epochs.available = False
    second = api.client.post(
        path, headers=headers, json={"version": 1, "revoke_remote": False})
    conflict = api.client.post(
        path, headers=headers, json={"version": 1, "revoke_remote": True})

    expected = {"credential_state": "missing", "revoke_state": "skipped"}
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == expected
    assert owner_epochs.calls == first_epoch_calls
    assert [call[0] for call in broker.calls] == ["allocate"]
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": {"code": "idempotency_conflict"}}
    with api.app.state.session_factory() as db:
        count = db.execute(__import__("sqlalchemy").text(
            "SELECT count(*) FROM connection_auth_operations WHERE owner_id=:owner"),
            {"owner": api.owner_id}).scalar_one()
    assert count == 1


@pytest.mark.parametrize(
    ("method", "terminal_state", "terminal_code"),
    [("get", "failed", "operation_interrupted"),
     ("delete", "cancelled", "operation_cancelled")],
)
def test_actual_service_pending_then_status_or_cancel_requires_restored_truth(
        api, method, terminal_state, terminal_code):
    with api.app.state.session_factory() as db:
        row = db.get(ProviderConnection, api.owner_connection)
        row.secret_ref = f"{api.owner_id}:{api.owner_connection}:" + "e" * 32
        row.auth_required = True
        row.auth_method_id = "api_key"
        row.credential_state = "refresh_required"
        row.auth_grant_revision = 3
        db.commit()
    broker = ActualRecoveryBroker(prepare_state="recovering")
    owner_epochs = ActualOwnerEpochs()
    api.app.state.connection_recovery = actual_coordinator(broker, owner_epochs)
    start = api.client.post(
        f"/api/v1/connections/{api.owner_connection}/disconnect",
        headers=api.headers(api.owner_id),
        json={"version": 1, "revoke_remote": False},
    )
    operation_path = (
        f"/api/v1/connections/{api.owner_connection}/auth/operations/{OPERATION}")

    terminal = getattr(api.client, method)(
        operation_path, headers=api.headers(api.owner_id))

    assert start.status_code == 202
    assert start.json() == {
        "version": 1, "operation_id": OPERATION, "state": "pending",
        "expires_at": None, "poll_interval": 1,
    }
    assert terminal.status_code == 200
    assert terminal.json() == {
        "version": 1, "operation_id": OPERATION, "state": terminal_state,
        "result": {
            "credential_state": "refresh_required", "revoke_state": "skipped"},
        "error_code": terminal_code,
    }
    with api.app.state.session_factory() as db:
        row = db.get(ProviderConnection, api.owner_connection)
        assert row.secret_ref is not None
        assert row.credential_state == "refresh_required"
