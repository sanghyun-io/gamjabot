from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models.collaboration import ConnectionModelCache, ProviderConnection
from app.models.models import User
from app.services import auth_operations, connections
from app.services.core_auth_broker import AuthPrivateReply, AuthPublicState
from app.schemas.provider_auth import AuthOperationIntent


HASHES = tuple(character * 64 for character in "abcdef123456789")
OWNER_EPOCH, GENERATION, OLD_GENERATION = HASHES[:3]


class CrashPoint(RuntimeError):
    pass


class MemoryBroker:
    """Safe finite Core stand-in; secret bytes never enter this fixture."""

    def __init__(self):
        self.current_generation_fingerprint = GENERATION
        self.material = {}
        self.calls = []
        self.fail_ack = False

    def credential_prepare(self, binding, exact_old_reference=None):
        self.calls.append(("prepare", binding["operation_id"]))
        self.material[binding["operation_id"]] = {
            "binding": dict(binding), "recovery": True, "prepared": False,
            "receipt": None, "old_absent": False,
        }
        return self._auth_reply(binding["operation_id"], "prepared")

    def credential_commit(self, binding):
        self.calls.append(("commit", binding["operation_id"]))
        material = self.material[binding["operation_id"]]
        material["prepared"] = True
        return self._auth_reply(
            binding["operation_id"], "committing",
            prepared_ref=f'{binding["owner_id"]}:{binding["connection_id"]}:' + "9" * 32,
            commit_handle="8" * 64,
        )

    def credential_discard_exact(self, binding, action, **kwargs):
        self.calls.append((action, binding["operation_id"]))
        self._act(binding["operation_id"], action)
        return self._auth_reply(binding["operation_id"], "succeeded")

    def credential_discard_recovery(self, selector, action, *, binding_fingerprint=None,
                                    **kwargs):
        operation_id = selector["operation_id"]
        self.calls.append((action, operation_id))
        material = self.material.get(operation_id)
        if material is None:
            return self._reply(operation_id, "absent", None)
        if action == "inspect":
            return self._reply(operation_id, self._state(material), material)
        return self._act(operation_id, action)

    def _act(self, operation_id, action):
        material = self.material[operation_id]
        if action == "cleanup_committed":
            material["old_absent"] = True
            material["recovery"] = False
            material["prepared"] = False
            material["receipt"] = "cleaned"
        elif action == "restore_old":
            material["old_absent"] = False
            material["recovery"] = False
            material["receipt"] = "restored"
        elif action == "discard_prepared":
            material["prepared"] = False
        elif action == "acknowledge_terminal":
            if self.fail_ack:
                raise RuntimeError("synthetic ack failure")
            material["receipt"] = None
        return self._reply(operation_id, self._state(material), material)

    @staticmethod
    def _auth_reply(operation_id, state, *, prepared_ref=None, commit_handle=None):
        return AuthPrivateReply(AuthPublicState(
            version=1, operation_id=operation_id, state=state,
        ), prepared_ref=prepared_ref, commit_handle=commit_handle)

    @staticmethod
    def _state(material):
        if material["recovery"] or material["prepared"]:
            return "recovering"
        return material["receipt"] or "absent"

    @staticmethod
    def _reply(operation_id, state, material):
        if material is None:
            material_state, receipt_state, fingerprint, summary = "none", "none", None, None
        else:
            material_state = (
                "recovery_and_prepared" if material["recovery"] and material["prepared"]
                else "recovery" if material["recovery"]
                else "prepared" if material["prepared"] else "none"
            )
            receipt_state = "present" if material["receipt"] else "none"
            fingerprint = sha256(repr(sorted(material["binding"].items())).encode()).hexdigest()
            binding = material["binding"]
            summary = SimpleNamespace(**{
                key: binding[key] for key in (
                    "provider_id", "auth_method_id", "config_revision",
                    "auth_grant_revision", "public_config_fingerprint",
                )
            })
        return SimpleNamespace(
            operation_id=operation_id, state=state, material_state=material_state,
            receipt_state=receipt_state, binding_fingerprint=fingerprint,
            binding_summary=summary, code=None,
        )


class NoTouch:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def called(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            raise AssertionError(f"{name} must not be called")
        return called


@pytest.fixture
def recovery_db(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "recovery-test-token")
    with TestClient(create_app(database_url=sqlite_url)) as client:
        with client.app.state.session_factory() as db:
            owner = User(email="recovery@example.test", username="recovery-owner",
                         hashed_password="unused")
            db.add(owner)
            db.commit()
            row = ProviderConnection(
                user_id=owner.id, kind="compatible", name="Recovery fixture",
                base_url="https://openrouter.ai/api/v1",
                secret_ref=f"{owner.id}:31:" + "7" * 32,
                default_model_id="vendor/model", enabled=True, auth_required=True,
                capabilities={"declared": {}, "verified": {}}, readiness_revision=4,
                provider_id="openrouter", protocol_id="openai_chat_completions",
                auth_method_id="api_key", public_config_version=1, public_config={},
                auth_grant_revision=3, credential_state="present",
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            db.add(ConnectionModelCache(
                owner_id=owner.id, connection_id=row.id, config_revision=4,
                auth_grant_revision=3, config_fingerprint="6" * 64,
                observed_at=datetime.now(UTC), truncated=False,
                models=[{"id": "vendor/model", "label": "Model", "source": "provider"}],
            ))
            db.commit()
            yield client.app.state.session_factory, owner.id, row.id


def coordinator(broker, *, crash=None, cache=None, operation_id="4" * 64):
    from app.services.connection_recovery import ConnectionRecoveryCoordinator

    return ConnectionRecoveryCoordinator(
        broker=broker,
        owner_epochs=SimpleNamespace(current=lambda owner_id: OWNER_EPOCH),
        credential_cache=cache,
        operation_id_factory=lambda: operation_id,
        crash_hook=crash or (lambda _point: None),
    )


def test_nonterminal_operation_blocks_sync_async_and_discovery_before_dependencies(recovery_db):
    factory, owner_id, connection_id = recovery_db
    store = NoTouch()
    cache = NoTouch()
    with factory() as db:
        row = db.get(ProviderConnection, connection_id)
        projection = {
            "secret_ref": row.secret_ref, "credential_state": row.credential_state,
            "readiness_revision": row.readiness_revision,
            "auth_grant_revision": row.auth_grant_revision,
        }
        auth_operations.begin_operation(db, AuthOperationIntent(
            operation_id="5" * 64, owner_id=owner_id, connection_id=connection_id,
            kind="disconnect", intent_fingerprint="6" * 64,
            before_state_fingerprint=sha256(repr(projection).encode()).hexdigest(),
            current_generation_fingerprint=GENERATION, revoke_requested=False,
        ), None)

        with pytest.raises(connections.ConnectionFailure) as sync_error:
            connections.resolve_connection(db, connection_id, owner_id, store=store, cache=cache)
        with pytest.raises(connections.ConnectionFailure) as async_error:
            __import__("asyncio").run(connections.resolve_connection_async(
                db, connection_id, owner_id, store=store, cache=cache))
        with pytest.raises(connections.ConnectionFailure) as discovery_error:
            __import__("asyncio").run(connections.resolve_discovery_connection_async(
                db, connection_id, owner_id, store=store, cache=cache))

    assert {sync_error.value.code, async_error.value.code, discovery_error.value.code} == {
        "credential_operation_pending"}
    assert store.calls == cache.calls == []


def test_disconnect_orders_ledger_db_cleanup_terminal_and_ack(recovery_db):
    from app.services.connection_recovery import connection_state_fingerprint

    factory, owner_id, connection_id = recovery_db
    broker = MemoryBroker()
    observed = []

    def observe(point):
        with factory() as check:
            operation = auth_operations.blocking_operation(check, owner_id, connection_id)
            row = check.get(ProviderConnection, connection_id)
            observed.append((point, operation.phase if operation else "terminal", row.secret_ref))

    with factory() as db:
        row = db.get(ProviderConnection, connection_id)
        before = connection_state_fingerprint(db, row)
        result = coordinator(broker, crash=observe).disconnect(
            db, row, revoke_remote=False, idempotency_key=None)

    assert result.state == "succeeded"
    assert result.result == {"credential_state": "missing", "revoke_state": "skipped"}
    assert [call[0] for call in broker.calls] == [
        "prepare", "cleanup_committed", "inspect", "acknowledge_terminal"]
    assert observed[0][0] == "after_ledger_commit"
    assert observed[0][2] is not None
    assert ("after_db_commit", "committed_cleanup_pending", None) in observed
    assert observed[-1][0] == "after_ack"
    with factory() as check:
        row = check.get(ProviderConnection, connection_id)
        operation = auth_operations.operation(check, owner_id, result.operation_id)
        assert connection_state_fingerprint(check, row) != before
        assert (row.secret_ref, row.credential_state,
                row.readiness_revision, row.auth_grant_revision) == (None, "missing", 5, 4)
        assert operation.phase == "terminal" and operation.terminal_at is not None
        assert check.query(ConnectionModelCache).filter_by(
            owner_id=owner_id, connection_id=connection_id).count() == 0


def test_core_allocates_operation_id_before_ledger_and_ledger_precedes_prepare(recovery_db):
    from app.services.connection_recovery import ConnectionRecoveryCoordinator

    factory, owner_id, connection_id = recovery_db
    broker = MemoryBroker()
    allocated = "0" * 64

    def allocate(request_owner_id, owner_epoch):
        assert (request_owner_id, owner_epoch) == (owner_id, OWNER_EPOCH)
        broker.calls.append(("allocate", allocated))
        return allocated

    real_prepare = broker.credential_prepare

    def prepare_after_ledger(binding, exact_old_reference=None):
        with factory() as check:
            operation = auth_operations.operation(check, owner_id, allocated)
            assert operation.phase == "recovery_preparing"
        return real_prepare(binding, exact_old_reference)

    broker.operation_allocate = allocate
    broker.credential_prepare = prepare_after_ledger
    service = ConnectionRecoveryCoordinator(
        broker=broker,
        owner_epochs=SimpleNamespace(current=lambda candidate: OWNER_EPOCH),
    )
    with factory() as db:
        result = service.disconnect(
            db, db.get(ProviderConnection, connection_id), revoke_remote=False)
    assert result.operation_id == allocated
    assert [name for name, _ in broker.calls][:2] == ["allocate", "prepare"]


@pytest.mark.parametrize("point", [
    "after_ledger_commit", "after_recovery_staged", "after_db_commit",
    "after_cleanup", "after_terminal",
])
def test_restart_reconciles_each_disconnect_crash_point_without_blind_mutation(
        recovery_db, point):
    factory, owner_id, connection_id = recovery_db
    broker = MemoryBroker()

    def crash(name):
        if name == point:
            raise CrashPoint(name)

    with factory() as db:
        row = db.get(ProviderConnection, connection_id)
        with pytest.raises(CrashPoint):
            coordinator(broker, crash=crash).disconnect(db, row, revoke_remote=False)

    before_resume = len(broker.calls)
    with factory() as db:
        service = coordinator(broker)
        if point == "after_terminal":
            result = service.operation_status(db, owner_id, connection_id, "4" * 64)
            assert service.sweep_terminal_receipts(
                db, owner_id,
                terminal_before=datetime.now(UTC) + timedelta(seconds=1)) == 1
        else:
            result = service.reconcile_owner(db, owner_id)[0]
    assert result.state in {"succeeded", "failed"}
    with factory() as check:
        operation = auth_operations.operation(check, owner_id, "4" * 64)
        row = check.get(ProviderConnection, connection_id)
        assert operation.phase == "terminal" and operation.terminal_at is not None
        if result.state == "succeeded":
            assert row.secret_ref is None
        else:
            assert row.secret_ref is not None
    resumed_actions = [name for name, _ in broker.calls[before_resume:]]
    if point == "after_terminal":
        assert resumed_actions == ["inspect", "acknowledge_terminal"]


def test_cas_loss_and_unbound_owner_perform_no_recovery_action(recovery_db):
    factory, owner_id, connection_id = recovery_db
    broker = MemoryBroker()

    def race(point):
        if point == "after_recovery_staged":
            with factory() as winner:
                row = winner.get(ProviderConnection, connection_id)
                row.name = "CAS winner"
                row.readiness_revision += 1
                winner.commit()

    with factory() as db:
        row = db.get(ProviderConnection, connection_id)
        result = coordinator(broker, crash=race).disconnect(db, row, revoke_remote=False)
    assert result.state == "pending"
    assert [name for name, _ in broker.calls] == ["prepare"]

    broker.calls.clear()
    from app.services.connection_recovery import ConnectionRecoveryCoordinator
    unbound = ConnectionRecoveryCoordinator(
        broker=broker, owner_epochs=SimpleNamespace(current=lambda _owner: None))
    with factory() as db:
        pending = unbound.reconcile_owner(db, owner_id)
    assert pending[0].state == "pending"
    assert broker.calls == []


def test_already_current_adoption_requires_fresh_db_and_second_secure_inspection(recovery_db):
    factory, owner_id, connection_id = recovery_db
    broker = MemoryBroker()

    def stop_after_stage(point):
        if point == "after_recovery_staged":
            raise CrashPoint(point)

    with factory() as db:
        row = db.get(ProviderConnection, connection_id)
        with pytest.raises(CrashPoint):
            coordinator(broker, crash=stop_after_stage).disconnect(db, row, revoke_remote=False)

    with factory() as mutate:
        operation = auth_operations.operation(mutate, owner_id, "4" * 64)
        mutate.execute(__import__("sqlalchemy").text(
            "UPDATE connection_auth_operations SET current_generation_fingerprint=:generation "
            "WHERE operation_id=:operation_id"), {
                "generation": OLD_GENERATION, "operation_id": operation.operation_id})
        mutate.commit()

    raced = False

    def adoption_race(point):
        nonlocal raced
        if point == "before_generation_adoption" and not raced:
            raced = True
            with factory() as winner:
                winner.execute(__import__("sqlalchemy").text(
                    "UPDATE connection_auth_operations SET current_generation_fingerprint=:generation "
                    "WHERE operation_id=:operation_id"), {
                        "generation": GENERATION, "operation_id": "4" * 64})
                winner.commit()

    broker.calls.clear()
    with factory() as db:
        result = coordinator(broker, crash=adoption_race).reconcile_owner(db, owner_id)[0]
    assert result.state == "failed"
    assert [name for name, _ in broker.calls].count("inspect") >= 3
    first_action = next(index for index, call in enumerate(broker.calls)
                        if call[0] in {"restore_old", "cleanup_committed"})
    assert [name for name, _ in broker.calls[:first_action]].count("inspect") >= 2


def test_remote_attempt_unknown_is_not_retried_and_converges_to_conservative_failure(recovery_db):
    factory, owner_id, connection_id = recovery_db
    broker = MemoryBroker()
    remote_calls = []

    def crash(point):
        if point == "after_revoke_attempt_marker":
            raise CrashPoint(point)

    with factory() as db:
        row = db.get(ProviderConnection, connection_id)
        with pytest.raises(CrashPoint):
            coordinator(broker, crash=crash).disconnect(
                db, row, revoke_remote=True,
                remote_revoker=lambda **safe: remote_calls.append(safe) or "revoked")

    with factory() as db:
        result = coordinator(broker).reconcile_owner(db, owner_id)[0]
    assert remote_calls == []
    assert result.state == "succeeded"
    assert result.result["revoke_state"] == "remote_revoke_failed"


def test_terminal_sweeper_only_acknowledges_receipts(recovery_db):
    factory, owner_id, connection_id = recovery_db
    broker = MemoryBroker()
    broker.fail_ack = True
    with factory() as db:
        row = db.get(ProviderConnection, connection_id)
        result = coordinator(broker).disconnect(db, row, revoke_remote=False)
    assert result.state == "succeeded"

    broker.fail_ack = False
    broker.calls.clear()
    with factory() as db:
        swept = coordinator(broker).sweep_terminal_receipts(
            db, owner_id, terminal_before=datetime.now(UTC) + timedelta(seconds=1))
    assert swept == 1
    assert [name for name, _ in broker.calls] == ["inspect", "acknowledge_terminal"]


def test_ledger_and_public_result_never_contain_private_recovery_values(recovery_db):
    factory, owner_id, connection_id = recovery_db
    broker = MemoryBroker()
    marker = "synthetic-private-recovery-marker"
    with factory() as db:
        row = db.get(ProviderConnection, connection_id)
        result = coordinator(broker).disconnect(db, row, revoke_remote=False)
        serialized = repr(auth_operations.operation(db, owner_id, result.operation_id).model_dump())
    assert marker not in serialized
    for forbidden in ("secret_ref", "owner_epoch", "binding_fingerprint", "account", "scope"):
        assert forbidden not in result.result


def test_keyed_terminal_and_keyless_active_replay_are_db_only_before_owner_or_core_access(
        recovery_db):
    from app.services.connection_recovery import ConnectionRecoveryCoordinator

    factory, owner_id, connection_id = recovery_db
    broker = MemoryBroker()
    with factory() as db:
        row = db.get(ProviderConnection, connection_id)
        first = coordinator(broker).disconnect(
            db, row, revoke_remote=False, idempotency_key="stable-request")
    first_call_count = len(broker.calls)
    unavailable = ConnectionRecoveryCoordinator(
        broker=NoTouch(), owner_epochs=NoTouch(), credential_cache=NoTouch())
    with factory() as db:
        row = db.get(ProviderConnection, connection_id)
        replay = unavailable.disconnect(
            db, row, revoke_remote=False, idempotency_key="stable-request")
        with pytest.raises(auth_operations.AuthOperationError) as conflict:
            unavailable.disconnect(
                db, row, revoke_remote=True, idempotency_key="stable-request")
    assert replay.safe_dict() == first.safe_dict()
    assert conflict.value.code == "idempotency_conflict"
    assert len(broker.calls) == first_call_count

    second_broker = MemoryBroker()
    with factory() as reset:
        row = reset.get(ProviderConnection, connection_id)
        row.secret_ref = f"{owner_id}:{connection_id}:" + "7" * 32
        row.credential_state = "present"
        row.readiness_revision = 8
        row.auth_grant_revision = 7
        reset.commit()
    with factory() as db:
        row = db.get(ProviderConnection, connection_id)
        with pytest.raises(CrashPoint):
            coordinator(second_broker, operation_id="3" * 64,
                        crash=lambda point: (_ for _ in ()).throw(CrashPoint(point))
                        if point == "after_ledger_commit" else None).disconnect(
                            db, row, revoke_remote=False)
    second_broker.calls.clear()
    with factory() as db:
        row = db.get(ProviderConnection, connection_id)
        pending = unavailable.disconnect(db, row, revoke_remote=False)
    assert pending.state == "pending"
    assert second_broker.calls == []


def test_cancel_restores_before_state_but_never_relabels_committed_cleanup(recovery_db):
    factory, owner_id, connection_id = recovery_db
    broker = MemoryBroker()

    def stop(point):
        if point == "after_recovery_staged":
            raise CrashPoint(point)

    service = coordinator(broker, crash=stop)
    with factory() as db:
        row = db.get(ProviderConnection, connection_id)
        with pytest.raises(CrashPoint):
            service.disconnect(db, row, revoke_remote=False)
    with factory() as db:
        cancelled = coordinator(broker).cancel_operation(
            db, owner_id, connection_id, "4" * 64)
    assert cancelled.state == "cancelled"
    assert cancelled.result["credential_state"] == "present"


def test_db_commit_and_terminal_reply_loss_are_resolved_by_exact_reread(recovery_db):
    factory, _, connection_id = recovery_db
    broker = MemoryBroker()
    armed = {"db": True, "terminal": True}

    with factory() as db:
        real_commit = db.commit

        def arm_reply_loss(point):
            if point == "after_recovery_staged" and armed["db"]:
                armed["db"] = False

                def commit_then_raise():
                    db.commit = real_commit
                    real_commit()
                    raise RuntimeError("synthetic DB reply loss")

                db.commit = commit_then_raise
            if point == "after_cleanup" and armed["terminal"]:
                armed["terminal"] = False

                def terminal_then_raise():
                    db.commit = real_commit
                    real_commit()
                    raise RuntimeError("synthetic terminal reply loss")

                db.commit = terminal_then_raise

        row = db.get(ProviderConnection, connection_id)
        result = coordinator(broker, crash=arm_reply_loss).disconnect(
            db, row, revoke_remote=False)
    assert result.state == "succeeded"


def test_core_cleanup_reply_loss_reinspects_receipt_without_repeating_action(recovery_db):
    factory, _, connection_id = recovery_db
    broker = MemoryBroker()
    real_discard = broker.credential_discard_exact
    lost = False

    def cleanup_then_lose(binding, action, **kwargs):
        nonlocal lost
        reply = real_discard(binding, action, **kwargs)
        if action == "cleanup_committed" and not lost:
            lost = True
            raise RuntimeError("synthetic Core reply loss")
        return reply

    broker.credential_discard_exact = cleanup_then_lose
    with factory() as db:
        row = db.get(ProviderConnection, connection_id)
        result = coordinator(broker).disconnect(db, row, revoke_remote=False)
    assert result.state == "succeeded"
    assert [name for name, _ in broker.calls].count("cleanup_committed") == 1


def test_prepare_reply_loss_returns_pending_then_restores_exact_before_state(recovery_db):
    factory, owner_id, connection_id = recovery_db
    broker = MemoryBroker()
    real_prepare = broker.credential_prepare

    def prepare_then_lose(binding, exact_old_reference=None):
        real_prepare(binding, exact_old_reference)
        raise RuntimeError("synthetic prepare reply loss")

    broker.credential_prepare = prepare_then_lose
    with factory() as db:
        result = coordinator(broker).disconnect(
            db, db.get(ProviderConnection, connection_id), revoke_remote=False)
    assert result.state == "pending"
    broker.credential_prepare = real_prepare
    with factory() as db:
        terminal = coordinator(broker).operation_status(
            db, owner_id, connection_id, "4" * 64)
        row = db.get(ProviderConnection, connection_id)
        assert row.secret_ref is not None
        assert row.credential_state == "present"
    assert terminal.state == "failed"


@pytest.mark.parametrize("lost_step", ["marker", "outcome"])
def test_remote_revoke_db_reply_loss_never_blindly_repeats_provider_call(
        recovery_db, monkeypatch, lost_step):
    factory, _, connection_id = recovery_db
    broker = MemoryBroker()
    remote_calls = []
    if lost_step == "marker":
        real = auth_operations.mark_revoke_attempt_started

        def commit_then_lose(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError("synthetic marker reply loss")

        monkeypatch.setattr(auth_operations, "mark_revoke_attempt_started", commit_then_lose)
    else:
        real = auth_operations.record_revoke_outcome
        lost = False

        def commit_then_lose(*args, **kwargs):
            nonlocal lost
            reply = real(*args, **kwargs)
            if not lost:
                lost = True
                raise RuntimeError("synthetic outcome reply loss")
            return reply

        monkeypatch.setattr(auth_operations, "record_revoke_outcome", commit_then_lose)

    with factory() as db:
        result = coordinator(broker).disconnect(
            db, db.get(ProviderConnection, connection_id), revoke_remote=True,
            remote_revoker=lambda **safe: remote_calls.append(safe) or "revoked")
    assert result.state == "succeeded"
    assert len(remote_calls) == (0 if lost_step == "marker" else 1)
    assert result.result["revoke_state"] == (
        "remote_revoke_failed" if lost_step == "marker" else "revoked")


def test_grant_replacement_commits_new_winner_then_core_cleans_old_without_old_revival(
        recovery_db):
    from app.services.connection_recovery import (
        _public_config_fingerprint, connection_state_fingerprint,
    )

    factory, owner_id, connection_id = recovery_db
    broker = MemoryBroker()
    operation_id = "2" * 64
    with factory() as db:
        row = db.get(ProviderConnection, connection_id)
        before = connection_state_fingerprint(db, row)
        operation = auth_operations.begin_operation(db, AuthOperationIntent(
            operation_id=operation_id, owner_id=owner_id, connection_id=connection_id,
            kind="grant_replace", intent_fingerprint="1" * 64,
            before_state_fingerprint=before,
            current_generation_fingerprint=GENERATION, revoke_requested=False,
        ), "grant-request")
        binding = {
            "version": 1, "operation_id": operation_id, "owner_id": owner_id,
            "connection_id": connection_id, "kind": "grant_replace",
            "intent_fingerprint": operation.intent_fingerprint,
            "before_state_fingerprint": before, "revoke_requested": False,
            "expires_at": 0, "owner_epoch": OWNER_EPOCH,
            "provider_id": "openrouter", "auth_method_id": "api_key",
            "config_revision": 4, "auth_grant_revision": 3,
            "public_config_fingerprint": _public_config_fingerprint(row, "api_key"),
        }
        broker.material[operation_id] = {
            "binding": dict(binding), "recovery": True, "prepared": True,
            "receipt": None, "old_absent": False,
        }
        result = coordinator(broker).commit_prepared_grant(
            db, row, operation=operation, binding=binding,
            target_auth_method_id="api_key", transition="proven_new_grant")
    assert result.state == "succeeded"
    assert [name for name, _ in broker.calls] == [
        "inspect", "commit", "cleanup_committed", "inspect", "acknowledge_terminal"]
    with factory() as check:
        row = check.get(ProviderConnection, connection_id)
        assert row.secret_ref == f"{owner_id}:{connection_id}:" + "9" * 32
        assert (row.readiness_revision, row.auth_grant_revision) == (5, 4)


def test_grant_replacement_rejects_stale_owner_binding_before_core_inspect(recovery_db):
    from app.services.connection_recovery import connection_state_fingerprint

    factory, owner_id, connection_id = recovery_db
    broker = MemoryBroker()
    operation_id = "2" * 64
    with factory() as db:
        row = db.get(ProviderConnection, connection_id)
        before = connection_state_fingerprint(db, row)
        operation = auth_operations.begin_operation(db, AuthOperationIntent(
            operation_id=operation_id, owner_id=owner_id, connection_id=connection_id,
            kind="grant_replace", intent_fingerprint="1" * 64,
            before_state_fingerprint=before,
            current_generation_fingerprint=GENERATION, revoke_requested=False,
        ), "grant-request")
        binding = {
            "version": 1, "operation_id": operation_id, "owner_id": owner_id,
            "connection_id": connection_id, "kind": "grant_replace",
            "intent_fingerprint": operation.intent_fingerprint,
            "before_state_fingerprint": before, "revoke_requested": False,
            "expires_at": 0, "owner_epoch": OLD_GENERATION,
            "provider_id": "openrouter", "auth_method_id": "api_key",
            "config_revision": 4, "auth_grant_revision": 3,
            "public_config_fingerprint": "0" * 64,
        }
        with pytest.raises(auth_operations.AuthOperationError):
            coordinator(broker).commit_prepared_grant(
                db, row, operation=operation, binding=binding,
                target_auth_method_id="api_key", transition="proven_new_grant")
    assert broker.calls == []


def test_connections_exposes_durable_disconnect_and_grant_hooks_without_store_access():
    expected = SimpleNamespace(state="pending")
    recovery = SimpleNamespace(
        disconnect=lambda *args, **kwargs: expected,
        commit_prepared_grant=lambda *args, **kwargs: expected,
    )
    assert connections.disconnect_connection_durable(
        object(), object(), False, recovery=recovery) is expected
    assert connections.commit_prepared_credential_grant_durable(
        object(), object(), recovery=recovery, operation=object(), binding={},
        target_auth_method_id="api_key", transition="proven_new_grant") is expected


def test_disconnect_uses_explicit_nonexpiring_recovery_sentinel(recovery_db):
    factory, _, connection_id = recovery_db

    class ExpiryCheckingBroker(MemoryBroker):
        seen_expiry = None

        def credential_prepare(self, binding, exact_old_reference=None):
            self.seen_expiry = binding["expires_at"]
            assert self.seen_expiry == (1 << 63) - 1
            return super().credential_prepare(binding, exact_old_reference)

    broker = ExpiryCheckingBroker()
    with factory() as db:
        result = coordinator(broker).disconnect(
            db, db.get(ProviderConnection, connection_id), revoke_remote=False)
    assert result.state == "succeeded"
    assert broker.seen_expiry == (1 << 63) - 1


def test_reconcile_rejects_committed_shape_with_wrong_revisions_before_material_action(
        recovery_db):
    factory, owner_id, connection_id = recovery_db
    broker = MemoryBroker()

    def stop(point):
        if point == "after_recovery_staged":
            raise CrashPoint(point)

    with factory() as db:
        with pytest.raises(CrashPoint):
            coordinator(broker, crash=stop).disconnect(
                db, db.get(ProviderConnection, connection_id), revoke_remote=False)
    with factory() as db:
        row = db.get(ProviderConnection, connection_id)
        row.secret_ref = None
        row.credential_state = "missing"
        row.readiness_revision = 99
        row.auth_grant_revision = 88
        db.query(ConnectionModelCache).filter_by(
            owner_id=owner_id, connection_id=connection_id).delete()
        db.execute(__import__("sqlalchemy").text(
            "UPDATE connection_auth_operations SET phase='committed_cleanup_pending' "
            "WHERE operation_id=:operation_id"), {"operation_id": "4" * 64})
        db.commit()

    broker.calls.clear()
    with factory() as db:
        result = coordinator(broker).reconcile_owner(db, owner_id)[0]
    assert result.state == "pending"
    assert result.error_code == "configuration_changed"
    assert [name for name, _ in broker.calls] == ["inspect"]


@pytest.mark.parametrize("credential_state", [
    "authorizing", "present", "refresh_required", "rejected", "locked", "unavailable",
])
def test_ref_less_disconnect_closed_states_remain_pending_without_core_action(
        recovery_db, credential_state):
    factory, owner_id, connection_id = recovery_db
    broker = MemoryBroker()
    with factory() as db:
        row = db.get(ProviderConnection, connection_id)
        row.secret_ref = None
        row.credential_state = credential_state
        db.query(ConnectionModelCache).filter_by(
            owner_id=owner_id, connection_id=connection_id).delete()
        before = (row.readiness_revision, row.auth_grant_revision)
        db.commit()
        result = coordinator(broker).disconnect(db, row, revoke_remote=False)
        db.refresh(row)
        after = (row.readiness_revision, row.auth_grant_revision)
    assert result.state == "pending"
    assert result.error_code == "configuration_changed"
    assert before == after == (4, 3)
    assert broker.calls == []


@pytest.mark.parametrize("method,state", [
    ("api_key", "missing"),
    ("local_none", "not_required"),
])
def test_ref_less_disconnect_only_allows_exact_no_material_states(
        recovery_db, method, state):
    factory, owner_id, connection_id = recovery_db
    broker = MemoryBroker()
    with factory() as db:
        row = db.get(ProviderConnection, connection_id)
        row.secret_ref = None
        row.auth_method_id = method
        row.credential_state = state
        db.query(ConnectionModelCache).filter_by(
            owner_id=owner_id, connection_id=connection_id).delete()
        db.commit()
        result = coordinator(broker).disconnect(db, row, revoke_remote=False)
    assert result.state == "succeeded"
    assert broker.calls == []


def test_invalid_grant_target_restores_old_and_discards_prepared_without_row_change(
        recovery_db):
    from app.services.connection_recovery import connection_state_fingerprint

    factory, owner_id, connection_id = recovery_db
    broker = MemoryBroker()
    operation_id = "2" * 64
    with factory() as db:
        row = db.get(ProviderConnection, connection_id)
        before = connection_state_fingerprint(db, row)
        before_values = (
            row.secret_ref, row.auth_method_id, row.readiness_revision,
            row.auth_grant_revision, row.credential_state,
        )
        operation = auth_operations.begin_operation(db, AuthOperationIntent(
            operation_id=operation_id, owner_id=owner_id, connection_id=connection_id,
            kind="same_grant_refresh", intent_fingerprint="1" * 64,
            before_state_fingerprint=before,
            current_generation_fingerprint=GENERATION, revoke_requested=False,
        ), "invalid-grant")
        binding = {
            "version": 1, "operation_id": operation_id, "owner_id": owner_id,
            "connection_id": connection_id, "kind": "same_grant_refresh",
            "intent_fingerprint": operation.intent_fingerprint,
            "before_state_fingerprint": before, "revoke_requested": False,
            "expires_at": 2_000_000_000, "owner_epoch": OWNER_EPOCH,
            "provider_id": "openrouter", "auth_method_id": "api_key",
            "config_revision": 4, "auth_grant_revision": 3,
            "public_config_fingerprint": "0" * 64,
        }
        broker.material[operation_id] = {
            "binding": dict(binding), "recovery": True, "prepared": True,
            "receipt": None, "old_absent": False,
        }
        result = coordinator(broker).commit_prepared_grant(
            db, row, operation=operation, binding=binding,
            target_auth_method_id="oauth_browser", transition="same_grant_refresh")
        db.refresh(row)
        after_values = (
            row.secret_ref, row.auth_method_id, row.readiness_revision,
            row.auth_grant_revision, row.credential_state,
        )
        assert db.query(ConnectionModelCache).filter_by(
            owner_id=owner_id, connection_id=connection_id).one_or_none() is not None
    assert result.state == "failed"
    assert result.error_code == "configuration_changed"
    assert before_values == after_values
    assert [name for name, _ in broker.calls] == [
        "inspect", "restore_old", "discard_prepared", "inspect", "acknowledge_terminal",
    ]


def test_external_update_and_delete_are_blocked_by_nonterminal_operation(recovery_db):
    factory, owner_id, connection_id = recovery_db
    with factory() as db:
        row = db.get(ProviderConnection, connection_id)
        auth_operations.begin_operation(db, AuthOperationIntent(
            operation_id="5" * 64, owner_id=owner_id, connection_id=connection_id,
            kind="disconnect", intent_fingerprint="6" * 64,
            before_state_fingerprint="7" * 64,
            current_generation_fingerprint=GENERATION, revoke_requested=False,
        ), None)
        with pytest.raises(connections.ConnectionFailure) as update_error:
            connections.update(db, row, {"label": "blocked"}, NoTouch())
        with pytest.raises(connections.ConnectionFailure) as delete_error:
            connections.delete(db, row, NoTouch())
    assert update_error.value.code == delete_error.value.code == "credential_operation_pending"


def _prepared_grant_case(db, owner_id, connection_id, broker, *, kind):
    from app.services.connection_recovery import (
        _public_config_fingerprint, connection_state_fingerprint,
    )

    row = db.get(ProviderConnection, connection_id)
    before = connection_state_fingerprint(db, row)
    operation_id = "2" * 64
    operation = auth_operations.begin_operation(db, AuthOperationIntent(
        operation_id=operation_id, owner_id=owner_id, connection_id=connection_id,
        kind=kind, intent_fingerprint="1" * 64,
        before_state_fingerprint=before,
        current_generation_fingerprint=GENERATION, revoke_requested=False,
    ), f"{kind}-request")
    binding = {
        "version": 1, "operation_id": operation_id, "owner_id": owner_id,
        "connection_id": connection_id, "kind": kind,
        "intent_fingerprint": operation.intent_fingerprint,
        "before_state_fingerprint": before, "revoke_requested": False,
        "expires_at": 2_000_000_000, "owner_epoch": OWNER_EPOCH,
        "provider_id": "openrouter", "auth_method_id": "api_key",
        "config_revision": 4, "auth_grant_revision": 3,
        "public_config_fingerprint": _public_config_fingerprint(row, "api_key"),
    }
    broker.material[operation_id] = {
        "binding": dict(binding), "recovery": True, "prepared": True,
        "receipt": None, "old_absent": False,
    }
    return row, operation, binding


@pytest.mark.parametrize(("kind", "transition", "wrong_revisions"), [
    ("grant_replace", "proven_new_grant", (4, 3)),
    ("same_grant_refresh", "same_grant_refresh", (5, 4)),
])
def test_grant_restart_wrong_transition_revisions_are_pending_without_material_action(
        recovery_db, kind, transition, wrong_revisions):
    factory, owner_id, connection_id = recovery_db
    broker = MemoryBroker()

    def stop(point):
        if point == "after_db_commit":
            raise CrashPoint(point)

    with factory() as db:
        row, operation, binding = _prepared_grant_case(
            db, owner_id, connection_id, broker, kind=kind)
        with pytest.raises(CrashPoint):
            coordinator(broker, crash=stop).commit_prepared_grant(
                db, row, operation=operation, binding=binding,
                target_auth_method_id="api_key", transition=transition)
    with factory() as db:
        row = db.get(ProviderConnection, connection_id)
        row.readiness_revision, row.auth_grant_revision = wrong_revisions
        db.commit()
    broker.calls.clear()
    with factory() as db:
        result = coordinator(broker).reconcile_owner(db, owner_id)[0]
    assert result.state == "pending"
    assert result.error_code == "configuration_changed"
    assert [name for name, _ in broker.calls] == ["inspect"]
    assert broker.material["2" * 64]["recovery"] is True


@pytest.mark.parametrize(("kind", "transition", "exact_revisions"), [
    ("grant_replace", "proven_new_grant", (5, 4)),
    ("same_grant_refresh", "same_grant_refresh", (4, 3)),
])
def test_grant_restart_exact_transition_revisions_cleanup_and_terminalize(
        recovery_db, kind, transition, exact_revisions):
    factory, owner_id, connection_id = recovery_db
    broker = MemoryBroker()

    def stop(point):
        if point == "after_db_commit":
            raise CrashPoint(point)

    with factory() as db:
        row, operation, binding = _prepared_grant_case(
            db, owner_id, connection_id, broker, kind=kind)
        with pytest.raises(CrashPoint):
            coordinator(broker, crash=stop).commit_prepared_grant(
                db, row, operation=operation, binding=binding,
                target_auth_method_id="api_key", transition=transition)
    broker.calls.clear()
    with factory() as db:
        row = db.get(ProviderConnection, connection_id)
        assert (row.readiness_revision, row.auth_grant_revision) == exact_revisions
        result = coordinator(broker).reconcile_owner(db, owner_id)[0]
    assert result.state == "succeeded"
    assert [name for name, _ in broker.calls] == [
        "inspect", "cleanup_committed", "inspect", "acknowledge_terminal",
    ]


@pytest.mark.parametrize(("kind", "transition", "exact_revisions"), [
    ("grant_replace", "proven_new_grant", (5, 4)),
    ("same_grant_refresh", "same_grant_refresh", (4, 3)),
])
def test_grant_db_commit_reply_loss_uses_exact_transition_discriminator(
        recovery_db, kind, transition, exact_revisions):
    factory, owner_id, connection_id = recovery_db
    broker = MemoryBroker()
    with factory() as db:
        row, operation, binding = _prepared_grant_case(
            db, owner_id, connection_id, broker, kind=kind)
        real_commit = db.commit
        commit_count = 0

        def commit_then_lose():
            nonlocal commit_count
            commit_count += 1
            real_commit()
            if commit_count == 2:
                db.commit = real_commit
                raise RuntimeError("synthetic grant DB reply loss")

        db.commit = commit_then_lose
        result = coordinator(broker).commit_prepared_grant(
            db, row, operation=operation, binding=binding,
            target_auth_method_id="api_key", transition=transition)
    assert result.state == "succeeded"
    with factory() as db:
        row = db.get(ProviderConnection, connection_id)
        assert (row.readiness_revision, row.auth_grant_revision) == exact_revisions


@pytest.mark.parametrize(("kind", "transition"), [
    ("grant_replace", "same_grant_refresh"),
    ("same_grant_refresh", "proven_new_grant"),
])
def test_grant_kind_and_transition_mismatch_is_rejected_before_core_inspect(
        recovery_db, kind, transition):
    factory, owner_id, connection_id = recovery_db
    broker = MemoryBroker()
    with factory() as db:
        row, operation, binding = _prepared_grant_case(
            db, owner_id, connection_id, broker, kind=kind)
        with pytest.raises(auth_operations.AuthOperationError) as error:
            coordinator(broker).commit_prepared_grant(
                db, row, operation=operation, binding=binding,
                target_auth_method_id="api_key", transition=transition)
    assert error.value.code == "configuration_changed"
    assert broker.calls == []
