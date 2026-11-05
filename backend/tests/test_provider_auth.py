"""Synthetic source tests for the durable provider-auth operation ledger."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import importlib
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core import migrate as migration


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64


def _seed_owner_and_connection(engine, *, owner_id=11, connection_id=31):
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users (id,email,username,hashed_password) "
            "VALUES (:id,:email,:username,'hash')"
        ), {"id": owner_id, "email": f"owner-{owner_id}@example.test",
            "username": f"owner-{owner_id}"})
        connection.execute(text(
            "INSERT INTO provider_connections "
            "(id,user_id,kind,name,default_model_id,enabled,auth_required,capabilities,"
            "readiness_revision,provider_id,protocol_id,auth_method_id,public_config_version,"
            "public_config,auth_grant_revision,credential_state) "
            "VALUES (:id,:owner,'openai','Synthetic','model',1,1,'{}',3,'openai',"
            "'openai_chat_completions','api_key',1,'{}',4,'present')"
        ), {"id": connection_id, "owner": owner_id})


def _fresh_database(tmp_path, monkeypatch):
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    path = tmp_path / "provider-auth.sqlite"
    engine = create_engine(f"sqlite:///{path}")
    migration.migrate_database(engine)
    _seed_owner_and_connection(engine)
    return engine, path


def _contracts():
    schemas = importlib.import_module("app.schemas.provider_auth")
    service = importlib.import_module("app.services.auth_operations")
    return schemas, service


def _intent(schemas, *, operation_id=HASH_A, intent_fingerprint=HASH_B,
            revoke_requested=False, owner_id=11, connection_id=31,
            expires_at=None):
    return schemas.AuthOperationIntent(
        operation_id=operation_id,
        owner_id=owner_id,
        connection_id=connection_id,
        kind="disconnect",
        intent_fingerprint=intent_fingerprint,
        before_state_fingerprint=HASH_C,
        current_generation_fingerprint=HASH_D,
        revoke_requested=revoke_requested,
        expires_at=expires_at,
    )


def _binding(schemas, operation):
    return schemas.OperationBinding(
        operation_id=operation.operation_id,
        owner_id=operation.owner_id,
        connection_id=operation.connection_id,
        intent_fingerprint=operation.intent_fingerprint,
        current_generation_fingerprint=operation.current_generation_fingerprint,
    )


def test_revision_twenty_five_is_additive_and_transactionally_rollback_safe(
        tmp_path, monkeypatch):
    assert migration.REVISIONS[-1] == "0025_connection_auth_operations"
    path = tmp_path / "revision-24.sqlite"
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    engine = create_engine(f"sqlite:///{path}")
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:24])
        migration.migrate_database(engine)
    _seed_owner_and_connection(engine)
    with sqlite3.connect(path) as raw:
        before = raw.execute(
            "SELECT id,user_id,kind,name,readiness_revision,auth_grant_revision,"
            "credential_state FROM provider_connections"
        ).fetchall()
        revision = importlib.import_module(
            "app.core.migrations.0025_connection_auth_operations")
        raw.execute("BEGIN IMMEDIATE")
        revision.upgrade(raw)
        raw.rollback()
        assert raw.execute(
            "SELECT name FROM sqlite_master WHERE name='connection_auth_operations'"
        ).fetchone() is None

    migration.migrate_database(engine)
    engine.dispose()
    with sqlite3.connect(path) as raw:
        assert raw.execute(
            "SELECT id,user_id,kind,name,readiness_revision,auth_grant_revision,"
            "credential_state FROM provider_connections"
        ).fetchall() == before
        assert raw.execute("SELECT max(version) FROM schema_version").fetchone() == (25,)
        columns = {row[1] for row in raw.execute(
            "PRAGMA table_info('connection_auth_operations')")}
        assert {
            "operation_id", "owner_id", "connection_id", "kind", "state", "phase",
            "idempotency_key_hash", "server_replay_discriminator_hash",
            "intent_fingerprint", "before_state_fingerprint",
            "current_generation_fingerprint", "revoke_requested",
            "revoke_attempt_started_at", "revoke_state", "attempt_count",
            "next_attempt_at", "error_code", "created_at", "updated_at", "expires_at",
            "terminal_at",
        } <= columns
        forbidden = {"token", "secret", "ref", "account", "scope", "nonce", "verifier"}
        assert not {column for column in columns for marker in forbidden if marker in column}
        indexes = {row[1] for row in raw.execute(
            "PRAGMA index_list('connection_auth_operations')")}
        assert {
            "ux_connection_auth_operations_owner_key",
            "ux_connection_auth_operations_owner_discriminator",
            "ux_connection_auth_operations_active_connection",
        } <= indexes


def test_revision_twenty_five_enforces_owner_finite_unions_and_terminal_equivalence(
        tmp_path, monkeypatch):
    engine, path = _fresh_database(tmp_path, monkeypatch)
    engine.dispose()
    insert = """INSERT INTO connection_auth_operations (
        operation_id,owner_id,connection_id,kind,state,phase,
        server_replay_discriminator_hash,intent_fingerprint,before_state_fingerprint,
        current_generation_fingerprint,revoke_requested,revoke_state,error_code,terminal_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
    base = (HASH_A, 11, 31, "disconnect", "prepared", "none", HASH_E,
            HASH_B, HASH_C, HASH_D, 0, "skipped", None, None)
    with sqlite3.connect(path) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(insert, (*base[:1], 99, *base[2:]))
        for index, invalid in ((3, "unknown_kind"), (4, "unknown_state"),
                               (5, "unknown_phase"), (11, "unknown_revoke"),
                               (12, "unknown_error")):
            values = list(base)
            values[0] = f"{index:064x}"
            values[index] = invalid
            with pytest.raises(sqlite3.IntegrityError):
                raw.execute(insert, values)
        terminal_without_timestamp = list(base)
        terminal_without_timestamp[0] = "1" * 64
        terminal_without_timestamp[5] = "terminal"
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(insert, terminal_without_timestamp)
        timestamp_without_terminal = list(base)
        timestamp_without_terminal[0] = "2" * 64
        timestamp_without_terminal[13] = "2026-10-07T00:00:00+00:00"
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(insert, timestamp_without_terminal)
        raw.execute(insert, base)
        duplicate_active = list(base)
        duplicate_active[0] = "3" * 64
        duplicate_active[6] = "4" * 64
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(insert, duplicate_active)


def test_public_dto_is_closed_and_never_serializes_private_auth_material():
    schemas, _service = _contracts()
    state = schemas.AuthPublicState(
        version=1,
        operation_id=HASH_A,
        state="awaiting_user",
        expires_at=datetime(2026, 10, 7, tzinfo=timezone.utc),
        browser_url="https://login.example.test/authorize",
    )
    serialized = state.model_dump_json()
    assert "login.example.test" in serialized
    for marker in ("raw-token-marker", "secret-ref-marker", "account-marker",
                   "scope-marker", "nonce-marker", "verifier-marker"):
        assert marker not in serialized
    for field, value in {
        "token": "raw-token-marker",
        "prepared_ref": "secret-ref-marker",
        "account": "account-marker",
        "scope": "scope-marker",
        "backend_nonce": "nonce-marker",
        "pkce_verifier": "verifier-marker",
        "result": {"credential_state": "present"},
    }.items():
        with pytest.raises(ValidationError):
            schemas.AuthPublicState(
                version=1, operation_id=HASH_A, state="pending", **{field: value})
    assert schemas.AuthPublicState(
        version=1, operation_id=HASH_A, state="pending").state == "pending"
    assert schemas.AuthPublicState(
        version=1, operation_id=HASH_A, state="finalizing").state == "finalizing"


def test_begin_operation_replays_exact_key_and_rejects_changed_intent(tmp_path, monkeypatch):
    schemas, service = _contracts()
    engine, _path = _fresh_database(tmp_path, monkeypatch)
    with Session(engine) as db:
        first = service.begin_operation(db, _intent(schemas), "visible-client-key")
        replay = service.begin_operation(
            db, _intent(schemas, operation_id=HASH_E), "visible-client-key")
        assert replay.operation_id == first.operation_id
        assert replay.replayed is True
        with pytest.raises(service.AuthOperationError) as conflict:
            service.begin_operation(
                db, _intent(schemas, operation_id="f" * 64, intent_fingerprint=HASH_E),
                "visible-client-key")
        assert conflict.value.code == "idempotency_conflict"
        stored = db.execute(text(
            "SELECT idempotency_key_hash,server_replay_discriminator_hash "
            "FROM connection_auth_operations"
        )).one()
        assert stored.idempotency_key_hash != "visible-client-key"
        assert stored.server_replay_discriminator_hash is None
        assert "visible-client-key" not in "|".join(str(value) for value in stored)
    engine.dispose()


def test_keyless_operation_has_server_discriminator_and_active_retry_replays(
        tmp_path, monkeypatch):
    schemas, service = _contracts()
    engine, _path = _fresh_database(tmp_path, monkeypatch)
    with Session(engine) as db:
        first = service.begin_operation(db, _intent(schemas), None)
        replay = service.begin_operation(
            db, _intent(schemas, operation_id=HASH_E, intent_fingerprint=HASH_E), None)
        assert replay.operation_id == first.operation_id
        assert replay.replayed is True
        stored = db.execute(text(
            "SELECT idempotency_key_hash,server_replay_discriminator_hash "
            "FROM connection_auth_operations"
        )).one()
        assert stored.idempotency_key_hash is None
        assert len(stored.server_replay_discriminator_hash) == 64
        assert stored.server_replay_discriminator_hash == sha256(
            bytes.fromhex(first.operation_id)).hexdigest()
        with pytest.raises(service.AuthOperationError) as busy:
            service.begin_operation(
                db, _intent(schemas, operation_id="f" * 64), "another-key")
        assert busy.value.code == "auth_operation_busy"
    engine.dispose()


@pytest.mark.parametrize("workflow_state", ["failed", "cancelled", "expired", "interrupted"])
def test_blocking_and_phase_cas_keep_expired_or_failed_cleanup_active(
        tmp_path, monkeypatch, workflow_state):
    schemas, service = _contracts()
    engine, _path = _fresh_database(tmp_path, monkeypatch)
    expired = datetime.now(timezone.utc) - timedelta(minutes=5)
    with Session(engine) as db:
        operation = service.begin_operation(
            db, _intent(schemas, expires_at=expired), "cleanup-key")
        binding = _binding(schemas, operation)
        recovering = service.advance_phase(
            db, binding, expected="none", target="rollback_pending")
        assert recovering.phase == "rollback_pending"
        db.execute(text(
            "UPDATE connection_auth_operations SET state=:state WHERE operation_id=:id"
        ), {"id": operation.operation_id, "state": workflow_state})
        db.commit()
        blocked = service.blocking_operation(db, 11, 31)
        assert blocked is not None
        assert blocked.state == workflow_state
        assert blocked.terminal_at is None
        with pytest.raises(service.AuthOperationError) as stale:
            service.advance_phase(db, binding, expected="none", target="reconcile_pending")
        assert stale.value.code == "configuration_changed"

        outcome = schemas.TerminalOutcome(
            state=workflow_state, error_code="credential_recovery_required",
            revoke_state="skipped")
        terminal = service.terminalize(db, binding, outcome)
        assert terminal.phase == "terminal"
        assert terminal.terminal_at is not None
        assert service.blocking_operation(db, 11, 31) is None
    engine.dispose()


def test_service_rejects_unclosed_phase_and_revoke_outcome_with_safe_error(
        tmp_path, monkeypatch):
    schemas, service = _contracts()
    engine, _path = _fresh_database(tmp_path, monkeypatch)
    with Session(engine) as db:
        operation = service.begin_operation(
            db, _intent(schemas, revoke_requested=True), "closed-union-key")
        binding = _binding(schemas, operation)
        with pytest.raises(service.AuthOperationError) as phase_error:
            service.advance_phase(db, binding, expected="none", target="not_a_phase")
        assert phase_error.value.code == "configuration_changed"
        service.mark_revoke_attempt_started(
            db, binding, datetime(2026, 10, 7, tzinfo=timezone.utc))
        with pytest.raises(service.AuthOperationError) as revoke_error:
            service.record_revoke_outcome(db, binding, "unknown_outcome")
        assert revoke_error.value.code == "configuration_changed"
    engine.dispose()


def test_revoke_attempt_marker_and_outcome_are_monotonic_and_closed(tmp_path, monkeypatch):
    schemas, service = _contracts()
    engine, _path = _fresh_database(tmp_path, monkeypatch)
    first_time = datetime(2026, 10, 7, 1, 0, tzinfo=timezone.utc)
    later_time = first_time + timedelta(minutes=1)
    with Session(engine) as db:
        operation = service.begin_operation(
            db, _intent(schemas, revoke_requested=True), "revoke-key")
        binding = _binding(schemas, operation)
        started = service.mark_revoke_attempt_started(db, binding, first_time)
        replayed = service.mark_revoke_attempt_started(db, binding, later_time)
        assert started.revoke_attempt_started_at == replayed.revoke_attempt_started_at
        confirmed = service.record_revoke_outcome(db, binding, "revoked")
        assert confirmed.revoke_state == "revoked"
        with pytest.raises(service.AuthOperationError) as overwrite:
            service.record_revoke_outcome(db, binding, "remote_revoke_failed")
        assert overwrite.value.code == "configuration_changed"

        outcome = schemas.TerminalOutcome(
            state="succeeded", error_code=None, revoke_state="revoked")
        assert service.terminalize(db, binding, outcome).state == "succeeded"
    engine.dispose()


def test_revoke_cannot_start_when_not_requested(tmp_path, monkeypatch):
    schemas, service = _contracts()
    engine, _path = _fresh_database(tmp_path, monkeypatch)
    with Session(engine) as db:
        operation = service.begin_operation(db, _intent(schemas), "skip-key")
        with pytest.raises(service.AuthOperationError) as invalid:
            service.mark_revoke_attempt_started(
                db, _binding(schemas, operation), datetime.now(timezone.utc))
        assert invalid.value.code == "configuration_changed"
    engine.dispose()


def test_terminalize_rejects_skipped_overwrite_of_confirmed_revoke(
        tmp_path, monkeypatch):
    schemas, service = _contracts()
    engine, _path = _fresh_database(tmp_path, monkeypatch)
    with Session(engine) as db:
        operation = service.begin_operation(
            db, _intent(schemas, revoke_requested=True), "confirmed-revoke-key")
        binding = _binding(schemas, operation)
        service.mark_revoke_attempt_started(
            db, binding, datetime(2026, 10, 7, tzinfo=timezone.utc))
        service.record_revoke_outcome(db, binding, "revoked")

        with pytest.raises(service.AuthOperationError) as invalid:
            service.terminalize(db, binding, schemas.TerminalOutcome(
                state="failed", error_code="internal_error", revoke_state="skipped"))
        assert invalid.value.code == "configuration_changed"
        blocked = service.blocking_operation(db, 11, 31)
        assert blocked is not None
        assert blocked.revoke_state == "revoked"
        assert blocked.terminal_at is None

        terminal = service.terminalize(db, binding, schemas.TerminalOutcome(
            state="succeeded", error_code=None, revoke_state="revoked"))
        assert terminal.state == "succeeded"
        assert terminal.revoke_state == "revoked"
    engine.dispose()


def test_terminalize_requires_conservative_outcome_after_started_revoke(
        tmp_path, monkeypatch):
    schemas, service = _contracts()
    engine, _path = _fresh_database(tmp_path, monkeypatch)
    with Session(engine) as db:
        operation = service.begin_operation(
            db, _intent(schemas, revoke_requested=True), "unknown-revoke-key")
        binding = _binding(schemas, operation)
        service.mark_revoke_attempt_started(
            db, binding, datetime(2026, 10, 7, tzinfo=timezone.utc))

        with pytest.raises(service.AuthOperationError) as invalid:
            service.terminalize(db, binding, schemas.TerminalOutcome(
                state="failed", error_code="internal_error", revoke_state="skipped"))
        assert invalid.value.code == "configuration_changed"
        blocked = service.blocking_operation(db, 11, 31)
        assert blocked is not None
        assert blocked.revoke_state == "pending"
        assert blocked.revoke_attempt_started_at is not None

        conservative = service.record_revoke_outcome(
            db, binding, "remote_revoke_failed")
        assert conservative.revoke_state == "remote_revoke_failed"
        terminal = service.terminalize(db, binding, schemas.TerminalOutcome(
            state="succeeded", error_code=None,
            revoke_state="remote_revoke_failed"))
        assert terminal.state == "succeeded"
        assert terminal.revoke_state == "remote_revoke_failed"
    engine.dispose()


def test_terminalize_allows_local_failure_before_revoke_attempt(tmp_path, monkeypatch):
    schemas, service = _contracts()
    engine, _path = _fresh_database(tmp_path, monkeypatch)
    with Session(engine) as db:
        operation = service.begin_operation(
            db, _intent(schemas, revoke_requested=True), "local-failure-key")
        binding = _binding(schemas, operation)
        terminal = service.terminalize(db, binding, schemas.TerminalOutcome(
            state="failed", error_code="credential_recovery_required",
            revoke_state="skipped"))
        assert terminal.state == "failed"
        assert terminal.revoke_state == "skipped"
        assert terminal.revoke_attempt_started_at is None
    engine.dispose()


def test_operation_read_is_owner_exact_and_projects_the_full_safe_selector(
        tmp_path, monkeypatch):
    schemas, service = _contracts()
    engine, _path = _fresh_database(tmp_path, monkeypatch)
    with Session(engine) as db:
        created = service.begin_operation(db, _intent(schemas, revoke_requested=True), None)
        loaded = service.operation(db, owner_id=11, operation_id=created.operation_id)
        assert loaded == created
        assert service.recovery_selector(loaded) == {
            "operation_id": HASH_A,
            "owner_id": 11,
            "connection_id": 31,
            "kind": "disconnect",
            "intent_fingerprint": HASH_B,
            "before_state_fingerprint": HASH_C,
            "revoke_requested": True,
        }
        with pytest.raises(service.AuthOperationError) as foreign:
            service.operation(db, owner_id=12, operation_id=created.operation_id)
        assert foreign.value.code == "configuration_changed"
    engine.dispose()


def test_generation_adoption_is_exact_atomic_and_reply_loss_idempotent(
        tmp_path, monkeypatch):
    schemas, service = _contracts()
    engine, _path = _fresh_database(tmp_path, monkeypatch)
    with Session(engine) as first:
        created = service.begin_operation(first, _intent(schemas), None)
        selector = service.recovery_selector(created)
        adopted = service.adopt_generation(
            first, selector=selector, expected_generation=HASH_D, new_generation=HASH_E)
        assert adopted.state == "adopted"
        assert adopted.operation.current_generation_fingerprint == HASH_E
        assert adopted.operation.replayed is False

    # A lost success reply is recoverable, but the reread cannot prove which caller
    # performed D -> E. The orchestrator must inspect Core before taking an action.
    with Session(engine) as retry:
        uncertain = service.adopt_generation(
            retry, selector=selector, expected_generation=HASH_D, new_generation=HASH_E)
        assert uncertain.state == "already_current"
        assert uncertain.operation.current_generation_fingerprint == HASH_E
        assert uncertain.operation.replayed is False

    # A racing different generation and a changed safe selector both lose without overwrite.
    with Session(engine) as loser:
        with pytest.raises(service.AuthOperationError) as race:
            service.adopt_generation(
                loser, selector=selector, expected_generation=HASH_D,
                new_generation="f" * 64)
        assert race.value.code == "configuration_changed"
        changed = {**selector, "before_state_fingerprint": "0" * 64}
        with pytest.raises(service.AuthOperationError) as mismatch:
            service.adopt_generation(
                loser, selector=changed, expected_generation=HASH_E,
                new_generation="f" * 64)
        assert mismatch.value.code == "configuration_changed"
        malformed = {**selector, "kind": []}
        with pytest.raises(service.AuthOperationError) as invalid:
            service.adopt_generation(
                loser, selector=malformed, expected_generation=HASH_E,
                new_generation="f" * 64)
        assert invalid.value.code == "configuration_changed"
        assert service.operation(loser, 11, HASH_A).current_generation_fingerprint == HASH_E
    engine.dispose()


@pytest.mark.parametrize("wrong_predecessor", ["f" * 64, "0" * 64])
def test_current_generation_observation_is_not_predecessor_cas_proof(
        tmp_path, monkeypatch, wrong_predecessor):
    schemas, service = _contracts()
    engine, _path = _fresh_database(tmp_path, monkeypatch)
    with Session(engine) as db:
        created = service.begin_operation(db, _intent(schemas), None)
        selector = service.recovery_selector(created)
        first = service.adopt_generation(
            db, selector=selector, expected_generation=HASH_D, new_generation=HASH_E)
        first_operation = getattr(first, "operation", first)
        assert first_operation.current_generation_fingerprint == HASH_E

        wrong_proof = service.adopt_generation(
            db, selector=selector, expected_generation=wrong_predecessor,
            new_generation=HASH_E)
        assert wrong_proof.state == "already_current"
        assert wrong_proof.operation.replayed is False
    engine.dispose()


def test_generation_adoption_rejects_losing_target_and_terminal_row(
        tmp_path, monkeypatch):
    schemas, service = _contracts()
    engine, _path = _fresh_database(tmp_path, monkeypatch)
    with Session(engine) as db:
        created = service.begin_operation(db, _intent(schemas), None)
        selector = service.recovery_selector(created)
        winner = service.adopt_generation(
            db, selector=selector, expected_generation=HASH_D, new_generation="f" * 64)
        assert winner.state == "adopted"
        with pytest.raises(service.AuthOperationError) as losing_target:
            service.adopt_generation(
                db, selector=selector, expected_generation=HASH_D,
                new_generation=HASH_E)
        assert losing_target.value.code == "configuration_changed"

        operation = winner.operation
        service.terminalize(
            db, _binding(schemas, operation),
            schemas.TerminalOutcome(
                state="failed", error_code="internal_error", revoke_state="skipped"),
            selector=selector,
        )
        with pytest.raises(service.AuthOperationError) as terminal:
            service.adopt_generation(
                db, selector=selector, expected_generation="f" * 64,
                new_generation=HASH_E)
        assert terminal.value.code == "configuration_changed"
    engine.dispose()


def test_recovery_candidate_queries_are_owner_scoped_bounded_and_cursor_stable(
        tmp_path, monkeypatch):
    schemas, service = _contracts()
    engine, _path = _fresh_database(tmp_path, monkeypatch)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO provider_connections "
            "(id,user_id,kind,name,default_model_id,enabled,auth_required,capabilities,"
            "readiness_revision,provider_id,protocol_id,auth_method_id,public_config_version,"
            "public_config,auth_grant_revision,credential_state) "
            "SELECT 32,user_id,kind,'Synthetic 32',default_model_id,enabled,auth_required,"
            "capabilities,readiness_revision,provider_id,protocol_id,auth_method_id,"
            "public_config_version,public_config,auth_grant_revision,credential_state "
            "FROM provider_connections WHERE id=31"
        ))
    _seed_owner_and_connection(engine, owner_id=12, connection_id=41)
    with Session(engine) as db:
        first = service.begin_operation(
            db, _intent(schemas, operation_id="1" * 64, connection_id=31), None)
        second = service.begin_operation(
            db, _intent(schemas, operation_id="2" * 64, connection_id=32,
                        intent_fingerprint=HASH_E), None)
        foreign = service.begin_operation(
            db, _intent(schemas, operation_id="3" * 64, owner_id=12,
                        connection_id=41, intent_fingerprint="f" * 64), None)
        page = service.nonterminal_candidates(db, owner_id=11, limit=1)
        assert [item.operation_id for item in page] == [first.operation_id]
        assert [item.operation_id for item in service.nonterminal_candidates(
            db, owner_id=11, after_operation_id=first.operation_id, limit=2
        )] == [second.operation_id]
        assert foreign.operation_id not in {
            item.operation_id for item in service.nonterminal_candidates(db, owner_id=11)
        }

        selector = service.recovery_selector(first)
        terminal = service.terminalize(
            db, _binding(schemas, first),
            schemas.TerminalOutcome(state="failed", error_code="internal_error",
                                    revoke_state="skipped"),
            selector=selector,
        )
        db.execute(text(
            "UPDATE connection_auth_operations SET terminal_at=:at WHERE operation_id=:id"
        ), {"id": terminal.operation_id, "at": "2026-10-07 01:00:00"})
        db.commit()
        assert service.terminal_receipt_candidates(
            db, owner_id=11, terminal_before=datetime(2026, 10, 7, 0, 59,
                                                       tzinfo=timezone.utc), limit=5) == []
        candidates = service.terminal_receipt_candidates(
            db, owner_id=11, terminal_before=datetime(2026, 10, 7, 1, 1,
                                                       tzinfo=timezone.utc), limit=5)
        assert [item.operation_id for item in candidates] == [first.operation_id]
    engine.dispose()


def test_terminal_reply_loss_requires_exact_selector_binding_and_same_outcome(
        tmp_path, monkeypatch):
    schemas, service = _contracts()
    engine, _path = _fresh_database(tmp_path, monkeypatch)
    with Session(engine) as db:
        created = service.begin_operation(db, _intent(schemas), None)
        binding = _binding(schemas, created)
        selector = service.recovery_selector(created)
        outcome = schemas.TerminalOutcome(
            state="failed", error_code="credential_recovery_required", revoke_state="skipped")
        first = service.terminalize(db, binding, outcome, selector=selector)
        assert first.replayed is False
        replay = service.terminalize(db, binding, outcome, selector=selector)
        assert replay.replayed is True

        with pytest.raises(service.AuthOperationError) as changed_outcome:
            service.terminalize(
                db, binding,
                schemas.TerminalOutcome(state="failed", error_code="internal_error",
                                        revoke_state="skipped"),
                selector=selector,
            )
        assert changed_outcome.value.code == "configuration_changed"
        with pytest.raises(service.AuthOperationError) as changed_selector:
            service.terminalize(
                db, binding, outcome,
                selector={**selector, "revoke_requested": True},
            )
        assert changed_selector.value.code == "configuration_changed"
    engine.dispose()


def test_no_commit_phase_cas_rolls_back_with_the_callers_connection_change(
        tmp_path, monkeypatch):
    schemas, service = _contracts()
    engine, _path = _fresh_database(tmp_path, monkeypatch)
    with Session(engine) as db:
        created = service.begin_operation(db, _intent(schemas), None)
        binding = _binding(schemas, created)

        db.execute(text("BEGIN IMMEDIATE"))
        db.execute(text(
            "UPDATE provider_connections SET credential_state='missing' WHERE id=31"
        ))
        staged = service.advance_phase_no_commit(
            db, binding, expected="none", target="db_commit_pending")
        assert staged.phase == "db_commit_pending"
        db.rollback()
        assert db.execute(text(
            "SELECT credential_state FROM provider_connections WHERE id=31"
        )).scalar_one() == "present"
        assert service.operation(db, 11, HASH_A).phase == "none"

        db.rollback()
        db.execute(text("BEGIN IMMEDIATE"))
        db.execute(text(
            "UPDATE provider_connections SET credential_state='missing' WHERE id=31"
        ))
        service.advance_phase_no_commit(
            db, binding, expected="none", target="db_commit_pending")
        db.commit()
        assert db.execute(text(
            "SELECT credential_state FROM provider_connections WHERE id=31"
        )).scalar_one() == "missing"
        assert service.operation(db, 11, HASH_A).phase == "db_commit_pending"
    engine.dispose()


class _ApiCoordinator:
    def __init__(self):
        self.calls = []
        self.failure = None

    def available_auth_methods(self, _row):
        return ()

    def start_auth(self, db, row, method, config_revision, auth_grant_revision,
                   *, idempotency_key):
        from app.services.auth_operations import AuthOperationError
        if (config_revision, auth_grant_revision) != (
                int(row.readiness_revision or 1), int(row.auth_grant_revision or 0)):
            raise AuthOperationError("configuration_changed")
        if method != "api_key":
            raise AuthOperationError("auth_method_unavailable")
        if self.failure:
            raise self.failure
        self.calls.append((row.user_id, row.id, method, config_revision,
                           auth_grant_revision, idempotency_key))
        return SimpleNamespace(
            operation_id=HASH_A, state="awaiting_user", expires_at=None,
            browser_url="https://login.example.test/authorize",
            verification_uri=None, user_code=None, poll_interval=None,
            result=None, error_code=None,
        )

    def operation_status(self, _db, owner_id, connection_id, operation_id):
        from app.services.connection_recovery import AuthLifecycleResult
        self.calls.append(("status", owner_id, connection_id, operation_id))
        return AuthLifecycleResult(
            operation_id=operation_id, state="succeeded",
            result={"credential_state": "present"},
        )

    def cancel_operation(self, _db, owner_id, connection_id, operation_id):
        from app.services.connection_recovery import AuthLifecycleResult
        self.calls.append(("cancel", owner_id, connection_id, operation_id))
        return AuthLifecycleResult(
            operation_id=operation_id, state="cancelled",
            error_code="operation_cancelled",
        )


@pytest.fixture
def auth_api(tmp_path, monkeypatch):
    from app.api.routes.auth import create_access_token
    from app.main import create_app
    from app.models.collaboration import ProviderConnection
    from app.models.models import User

    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "task8-local")
    app = create_app(database_url=f"sqlite:///{tmp_path / 'task8-api.sqlite'}")
    coordinator = _ApiCoordinator()
    app.state.connection_recovery = coordinator
    with TestClient(app) as client:
        app.state.connection_recovery = coordinator
        with app.state.session_factory() as db:
            owner = User(email="task8-owner@example.test", username="task8-owner",
                         hashed_password="unused")
            foreign = User(email="task8-foreign@example.test", username="task8-foreign",
                           hashed_password="unused")
            db.add_all([owner, foreign]); db.commit()
            row = ProviderConnection(
                user_id=owner.id, kind="openai", name="Task8", enabled=True,
                auth_required=True, capabilities={}, readiness_revision=3,
                provider_id="openai", protocol_id="openai_chat_completions",
                auth_method_id="api_key", public_config_version=1,
                public_config={}, auth_grant_revision=4,
                credential_state="present", secret_ref="1:1:" + "ab" * 16,
            )
            db.add(row); db.commit()
            owner_id, foreign_id, connection_id = owner.id, foreign.id, row.id
        headers = lambda user_id: {
            "X-Gamja-Session": "task8-local",
            "Authorization": f"Bearer {create_access_token({'sub': user_id})}",
        }
        yield SimpleNamespace(
            client=client, coordinator=coordinator, owner_id=owner_id,
            foreign_id=foreign_id, connection_id=connection_id, headers=headers,
        )


def _start(api, *, user_id=None, method="api_key", key="task8-key",
           config_revision=3, auth_grant_revision=4):
    headers = api.headers(user_id or api.owner_id)
    if key is not None:
        headers["Idempotency-Key"] = key
    return api.client.post(
        f"/api/v1/connections/{api.connection_id}/auth/{method}/operations",
        headers=headers,
        json={"version": 1, "config_revision": config_revision,
              "auth_grant_revision": auth_grant_revision},
    )


def test_auth_start_route_returns_only_closed_public_instruction(auth_api):
    response = _start(auth_api)

    assert response.status_code == 200
    assert response.json() == {
        "version": 1, "operation_id": HASH_A, "state": "awaiting_user",
        "expires_at": None,
        "browser_url": "https://login.example.test/authorize",
    }
    assert auth_api.coordinator.calls == [
        (auth_api.owner_id, auth_api.connection_id, "api_key", 3, 4, "task8-key")]
    for marker in ("token", "secret_ref", "owner_epoch", "nonce", "verifier"):
        assert marker not in response.text


def test_auth_start_rejects_foreign_stale_gated_and_missing_key_before_coordinator(auth_api):
    foreign = _start(auth_api, user_id=auth_api.foreign_id)
    unknown = auth_api.client.post(
        "/api/v1/connections/999999/auth/api_key/operations",
        headers={**auth_api.headers(auth_api.owner_id), "Idempotency-Key": "unknown"},
        json={"version": 1, "config_revision": 3, "auth_grant_revision": 4},
    )
    stale = _start(auth_api, key="stale", config_revision=2)
    gated = _start(auth_api, key="gated", method="oauth_browser")
    missing_key = _start(auth_api, key=None)

    assert foreign.status_code == unknown.status_code == 404
    assert stale.status_code == 409
    assert stale.json()["detail"] == {"code": "configuration_changed"}
    assert gated.status_code == 422
    assert gated.json()["detail"] == {"code": "auth_method_unavailable"}
    assert missing_key.status_code == 422
    assert auth_api.coordinator.calls == []


def test_auth_start_maps_busy_without_exposing_internal_state(auth_api):
    from app.services.auth_operations import AuthOperationError

    auth_api.coordinator.failure = AuthOperationError("auth_operation_busy")
    response = _start(auth_api)

    assert response.status_code == 409
    assert response.json() == {"detail": {"code": "auth_operation_busy"}}


def test_auth_methods_is_owner_scoped_cached_and_never_probes_core(auth_api):
    path = f"/api/v1/connections/{auth_api.connection_id}/auth/methods"
    first = auth_api.client.get(path, headers=auth_api.headers(auth_api.owner_id))
    second = auth_api.client.get(path, headers=auth_api.headers(auth_api.owner_id))
    foreign = auth_api.client.get(path, headers=auth_api.headers(auth_api.foreign_id))
    unknown = auth_api.client.get(
        "/api/v1/connections/999999/auth/methods",
        headers=auth_api.headers(auth_api.owner_id),
    )

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {
        "version": 1,
        "methods": [
            {"method_id": "api_key", "available": False},
            {"method_id": "oauth_browser", "available": False},
            {"method_id": "oauth_device", "available": False},
        ],
    }
    assert foreign.status_code == unknown.status_code == 404
    assert auth_api.coordinator.calls == []


def test_existing_status_and_cancel_routes_project_auth_terminal_truth(auth_api):
    path = (f"/api/v1/connections/{auth_api.connection_id}/auth/operations/"
            f"{HASH_A}")
    status = auth_api.client.get(
        path, headers=auth_api.headers(auth_api.owner_id))
    cancelled = auth_api.client.delete(
        path, headers=auth_api.headers(auth_api.owner_id))

    assert status.status_code == cancelled.status_code == 200
    assert status.json() == {
        "version": 1, "operation_id": HASH_A, "state": "succeeded",
        "result": {"credential_state": "present"},
    }
    assert cancelled.json() == {
        "version": 1, "operation_id": HASH_A, "state": "cancelled",
        "error_code": "operation_cancelled",
    }
    assert "revoke_state" not in status.text + cancelled.text
    assert auth_api.coordinator.calls == [
        ("status", auth_api.owner_id, auth_api.connection_id, HASH_A),
        ("cancel", auth_api.owner_id, auth_api.connection_id, HASH_A),
    ]


def test_auth_routes_remain_single_registered_get_delete_and_post(auth_api):
    routes = [
        (method, route.path)
        for route in auth_api.client.app.routes
        for method in getattr(route, "methods", ())
        if route.path in {
            "/api/v1/connections/{connection_id}/auth/operations/{operation_id}",
            "/api/v1/connections/{connection_id}/auth/{method}/operations",
            "/api/v1/connections/{connection_id}/auth/methods",
        }
    ]
    assert routes.count((
        "GET", "/api/v1/connections/{connection_id}/auth/operations/{operation_id}")) == 1
    assert routes.count((
        "DELETE", "/api/v1/connections/{connection_id}/auth/operations/{operation_id}")) == 1
    assert routes.count((
        "POST", "/api/v1/connections/{connection_id}/auth/{method}/operations")) == 1
    assert routes.count((
        "GET", "/api/v1/connections/{connection_id}/auth/methods")) == 1


class _OwnerEpochs:
    def current(self, owner_id):
        return HASH_C if owner_id == 11 else None


def _azure_direct_row(db):
    row = db.get(importlib.import_module(
        "app.models.collaboration").ProviderConnection, 31)
    row.kind = "compatible"
    row.provider_id = "azure-openai"
    row.protocol_id = "azure_openai"
    row.auth_method_id = "api_key"
    row.public_config_version = 1
    row.public_config = {
        "resource_endpoint": "https://example.openai.azure.com",
        "api_version": "v1",
    }
    row.base_url = "https://example.openai.azure.com"
    row.secret_ref = None
    row.credential_state = "missing"
    row.readiness_revision = 1
    row.auth_grant_revision = 0
    db.commit()
    return row


class _DirectBroker:
    current_generation_fingerprint = HASH_D

    def __init__(self, db):
        self.db = db
        self.calls = []
        self.binding = None
        self.cleaned = False

    def operation_allocate(self, owner_id, owner_epoch):
        assert owner_id == 11 and owner_epoch == HASH_C
        return HASH_A

    def credential_prepare(self, binding, exact_old_reference=None, *, direct_secret=None):
        assert self.db.execute(text(
            "SELECT count(*) FROM connection_auth_operations WHERE operation_id=:id"
        ), {"id": binding["operation_id"]}).scalar_one() == 1
        self.calls.append(("prepare", exact_old_reference, direct_secret))
        self.binding = dict(binding)
        from app.services.core_auth_broker import AuthPrivateReply, AuthPublicState
        return AuthPrivateReply(AuthPublicState(
            version=1, operation_id=binding["operation_id"], state="prepared"),
            prepared_ref=f'{binding["owner_id"]}:{binding["connection_id"]}:' + "9" * 32)

    def credential_commit(self, binding):
        self.calls.append(("commit", self.db.get(
            importlib.import_module("app.models.collaboration").ProviderConnection,
            binding["connection_id"]).secret_ref))
        from app.services.core_auth_broker import AuthPrivateReply, AuthPublicState
        return AuthPrivateReply(AuthPublicState(
            version=1, operation_id=binding["operation_id"], state="committing"),
            prepared_ref=f'{binding["owner_id"]}:{binding["connection_id"]}:' + "9" * 32,
            commit_handle=HASH_E)

    def credential_discard_exact(self, binding, action, **private):
        self.calls.append((action, private.get("adopted_prepared_ref")))
        self.cleaned = True
        from app.services.core_auth_broker import AuthPrivateReply, AuthPublicState
        return AuthPrivateReply(AuthPublicState(
            version=1, operation_id=binding["operation_id"], state="succeeded"))

    def credential_discard_recovery(self, selector, action, **_private):
        from app.services.core_auth_broker import RecoveryBindingSummary, RecoveryInspectReply
        if action == "cleanup_committed":
            self.cleaned = True
        if self.cleaned:
            return RecoveryInspectReply(
                selector["operation_id"], "cleaned", "none", "present",
                binding_fingerprint=HASH_B)
        binding = self.binding
        return RecoveryInspectReply(
            selector["operation_id"], "prepared", "recovery_and_prepared", "none",
            binding_fingerprint=HASH_B,
            binding_summary=RecoveryBindingSummary(
                provider_id=binding["provider_id"],
                auth_method_id=binding["auth_method_id"],
                config_revision=binding["config_revision"],
                auth_grant_revision=binding["auth_grant_revision"],
                public_config_fingerprint=binding["public_config_fingerprint"],
            ))


def test_core_client_direct_prepare_is_private_closed_and_pair_checked():
    from app.services.core_auth_broker import CoreAuthBrokerClient

    calls = []
    class Capture(CoreAuthBrokerClient):
        def __init__(self):
            pass
        def _exact(self, frame_type, binding, extra=None):
            calls.append((frame_type, dict(binding), dict(extra or {})))
            return object()

    binding = {
        "version": 1, "operation_id": HASH_A, "owner_id": 11,
        "connection_id": 31, "kind": "auth", "intent_fingerprint": HASH_B,
        "before_state_fingerprint": HASH_C, "revoke_requested": False,
        "expires_at": 2_000_000_000, "owner_epoch": HASH_C,
        "provider_id": "azure-openai", "auth_method_id": "api_key",
        "config_revision": 1, "auth_grant_revision": 0,
        "public_config_fingerprint": HASH_E,
    }
    client = Capture()
    client.credential_prepare(binding, direct_secret="private-direct-marker")
    assert calls[-1][2] == {"direct_secret": "private-direct-marker"}
    with pytest.raises(ValueError):
        client.credential_prepare({**binding, "provider_id": "openai"},
                                  direct_secret="private-direct-marker")
    with pytest.raises(ValueError):
        client.credential_prepare({**binding, "kind": "grant_replace"},
                                  direct_secret="private-direct-marker")


def test_direct_api_key_coordinator_commits_opaque_ref_and_exact_revisions(
        tmp_path, monkeypatch):
    from app.services.connection_recovery import ConnectionRecoveryCoordinator

    engine, path = _fresh_database(tmp_path, monkeypatch)
    with Session(engine) as db:
        row = _azure_direct_row(db)
        broker = _DirectBroker(db)
        result = ConnectionRecoveryCoordinator(
            broker=broker, owner_epochs=_OwnerEpochs()).commit_direct_api_key(
                db, row, "private-direct-marker")
        saved = db.get(type(row), row.id)
        assert result.state == "succeeded"
        assert saved.secret_ref == f"11:31:{'9' * 32}"
        assert (saved.readiness_revision, saved.auth_grant_revision,
                saved.credential_state) == (2, 1, "present")
        assert broker.calls == [
            ("prepare", None, "private-direct-marker"),
            ("commit", None),
            ("cleanup_committed", saved.secret_ref),
        ]
    with sqlite3.connect(path) as raw:
        assert "private-direct-marker" not in "\n".join(raw.iterdump())


def test_direct_api_key_replace_preserves_old_until_db_cas_and_increments_once(
        tmp_path, monkeypatch):
    from app.services import provider_catalog
    from app.services.connection_recovery import ConnectionRecoveryCoordinator

    engine, _path = _fresh_database(tmp_path, monkeypatch)
    descriptor = provider_catalog.get_descriptor("azure-openai")
    monkeypatch.setitem(provider_catalog._BY_PROVIDER, "azure-openai", descriptor.model_copy(
        update={"methods": tuple(method.model_copy(update={
            "selectable": True, "external_eligibility": "verified",
        }) for method in descriptor.methods)}))
    with Session(engine) as db:
        row = _azure_direct_row(db)
        row.secret_ref = "11:31:" + "7" * 32
        row.credential_state = "present"
        row.readiness_revision = 3
        row.auth_grant_revision = 4
        db.commit()
        broker = _DirectBroker(db)
        result = ConnectionRecoveryCoordinator(
            broker=broker, owner_epochs=_OwnerEpochs()).commit_direct_api_key(
                db, row, "replacement-direct-marker")
        saved = db.get(type(row), row.id)
        assert result.state == "succeeded"
        assert broker.calls[:2] == [
            ("prepare", "11:31:" + "7" * 32, "replacement-direct-marker"),
            ("commit", "11:31:" + "7" * 32),
        ]
        assert (saved.secret_ref, saved.readiness_revision,
                saved.auth_grant_revision) == ("11:31:" + "9" * 32, 4, 5)


def test_direct_api_key_crash_after_db_cas_recovers_cleanup_without_secret_replay(
        tmp_path, monkeypatch):
    from app.services.connection_recovery import ConnectionRecoveryCoordinator

    engine, _path = _fresh_database(tmp_path, monkeypatch)
    with Session(engine) as db:
        row = _azure_direct_row(db)
        broker = _DirectBroker(db)
        def crash(point):
            if point == "after_db_commit":
                raise RuntimeError("synthetic-crash")
        with pytest.raises(RuntimeError, match="synthetic-crash"):
            ConnectionRecoveryCoordinator(
                broker=broker, owner_epochs=_OwnerEpochs(),
                crash_hook=crash).commit_direct_api_key(
                    db, row, "one-use-direct-marker")
        committed = db.get(type(row), row.id)
        assert committed.secret_ref == "11:31:" + "9" * 32

        recovered = ConnectionRecoveryCoordinator(
            broker=broker, owner_epochs=_OwnerEpochs()).reconcile_owner(db, 11)
        assert [item.state for item in recovered] == ["succeeded"]
        assert [call for call in broker.calls if call[0] == "prepare"] == [
            ("prepare", None, "one-use-direct-marker")]


class _StartBroker:
    current_generation_fingerprint = HASH_D

    def __init__(self, db):
        self.db = db
        self.calls = []

    def operation_allocate(self, owner_id, owner_epoch):
        self.calls.append(("allocate", owner_id, owner_epoch))
        return HASH_A

    def auth_start(self, binding):
        assert self.db.execute(text(
            "SELECT count(*) FROM connection_auth_operations WHERE operation_id=:id"
        ), {"id": binding["operation_id"]}).scalar_one() == 1
        self.calls.append(("auth_start", binding["operation_id"]))
        from app.services.core_auth_broker import AuthPublicState
        return AuthPublicState(
            version=1, operation_id=binding["operation_id"],
            state="awaiting_user",
            authorization_url="https://login.example.test/authorize",
        )


class _NativeStartBroker(_StartBroker):
    def auth_start(self, binding, native_enrollment=None):
        assert self.db.execute(text(
            "SELECT count(*) FROM connection_auth_operations WHERE operation_id=:id"
        ), {"id": binding["operation_id"]}).scalar_one() == 1
        self.calls.append(("auth_start", dict(binding), dict(native_enrollment or {})))
        from app.services.core_auth_broker import AuthPublicState
        return AuthPublicState(
            version=1, operation_id=binding["operation_id"], state="awaiting_user")


def test_native_auth_start_passes_only_closed_catalog_fields_after_ledger_commit(
        tmp_path, monkeypatch):
    from app.services import provider_catalog
    from app.services.connection_recovery import ConnectionRecoveryCoordinator

    engine, _path = _fresh_database(tmp_path, monkeypatch)
    with Session(engine) as db:
        row = db.get(importlib.import_module(
            "app.models.collaboration").ProviderConnection, 31)
        row.kind = "azure-openai"
        row.provider_id = "azure-openai"
        row.protocol_id = "azure_openai"
        row.auth_method_id = "native_chain"
        row.public_config = {
            "resource_endpoint": "https://example.openai.azure.com",
            "api_version": "2026-01-01",
        }
        row.secret_ref = None
        db.commit()
        broker = _NativeStartBroker(db)
        coordinator = ConnectionRecoveryCoordinator(
            broker=broker, owner_epochs=_OwnerEpochs(),
            auth_method_availability=lambda provider_id, method_id: (
                provider_id == "azure-openai" and method_id == "native_chain"),
        )
        monkeypatch.setattr(
            provider_catalog, "launchable_auth_method", lambda *_args: object())
        monkeypatch.setattr(
            coordinator, "available_auth_methods", lambda _row: ("native_chain",))

        result = coordinator.start_auth(
            db, row, "native_chain", 3, 4, idempotency_key="native-start")

    assert result.state == "awaiting_user"
    call = broker.calls[-1]
    assert call[0] == "auth_start"
    assert call[1]["provider_id"] == "azure-openai"
    assert call[1]["auth_method_id"] == "native_chain"
    assert call[2] == {
        "provider": "azure_cli",
        "resource_endpoint": "https://example.openai.azure.com",
        "api_version": "2026-01-01",
    }


@pytest.mark.parametrize(("provider_id", "fields", "expected"), [
    ("azure-openai",
     {"resource_endpoint": "https://example.openai.azure.com", "api_version": "v1"},
     {"provider": "azure_cli", "resource_endpoint": "https://example.openai.azure.com",
      "api_version": "v1"}),
    ("amazon-bedrock", {"profile_name": "production", "region": "us-east-1"},
     {"provider": "aws_profile", "profile_name": "production", "region": "us-east-1"}),
    ("google-vertex", {"project_id": "potato-project", "location": "us-central1"},
     {"provider": "vertex_adc", "project_id": "potato-project", "location": "us-central1"}),
])
def test_native_enrollment_projection_is_closed_for_each_supported_provider(
        provider_id, fields, expected):
    from app.services.connection_recovery import _native_enrollment_spec

    row = SimpleNamespace(
        provider_id=provider_id, kind=provider_id, public_config_version=1,
        public_config=fields,
    )
    assert _native_enrollment_spec(row, "native_chain") == expected


class _CommitBroker(_StartBroker):
    def __init__(self, db, *, cleanup_confirmed):
        super().__init__(db)
        self.cleanup_confirmed = cleanup_confirmed

    def auth_status(self, binding):
        self.calls.append(("auth_status", binding["operation_id"]))
        from app.services.core_auth_broker import AuthPublicState
        return AuthPublicState(
            version=1, operation_id=binding["operation_id"], state="prepared")

    def credential_commit(self, binding):
        self.calls.append(("credential_commit", binding["operation_id"]))
        from app.services.core_auth_broker import AuthPrivateReply, AuthPublicState
        return AuthPrivateReply(
            AuthPublicState(
                version=1, operation_id=binding["operation_id"], state="committing"),
            prepared_ref=f'{binding["owner_id"]}:{binding["connection_id"]}:' + "9" * 32,
            commit_handle="8" * 64,
        )

    def credential_discard_exact(self, binding, action, **_private):
        self.calls.append((action, binding["operation_id"]))
        if not self.cleanup_confirmed:
            raise RuntimeError("synthetic ambiguous cleanup")
        from app.services.core_auth_broker import AuthPrivateReply, AuthPublicState
        return AuthPrivateReply(AuthPublicState(
            version=1, operation_id=binding["operation_id"], state="succeeded"))

    def credential_discard_recovery(self, selector, action, **_private):
        self.calls.append((action, selector["operation_id"]))
        return SimpleNamespace(
            operation_id=selector["operation_id"], state="absent",
            material_state="none", receipt_state="none",
            binding_fingerprint=None, binding_summary=None, code=None,
        )


class _RestartCleanupBroker(_CommitBroker):
    """Models Core cleanup_committed's exact adopted prepared-ref contract."""

    def __init__(self, db):
        super().__init__(db, cleanup_confirmed=False)
        self.binding = None
        self.prepared_ref = None
        self.prepared = False
        self.failure_code = None

    def credential_commit(self, binding):
        reply = super().credential_commit(binding)
        self.binding = dict(binding)
        self.prepared_ref = reply.prepared_ref
        self.prepared = True
        return reply

    def credential_discard_recovery(
            self, selector, action, *, adopted_prepared_ref=None, **_private):
        self.calls.append((action, selector["operation_id"], adopted_prepared_ref))
        if action == "cleanup_committed":
            if adopted_prepared_ref != self.prepared_ref:
                self.failure_code = "configuration_changed"
            else:
                self.prepared = False
                self.failure_code = None
        return self._recovery_reply(selector["operation_id"])

    def _recovery_reply(self, operation_id):
        if not self.prepared:
            return SimpleNamespace(
                operation_id=operation_id, state="absent",
                material_state="none", receipt_state="none",
                binding_fingerprint=None, binding_summary=None,
                code=self.failure_code,
            )
        summary = SimpleNamespace(**{
            key: self.binding[key] for key in (
                "provider_id", "auth_method_id", "config_revision",
                "auth_grant_revision", "public_config_fingerprint",
            )
        })
        return SimpleNamespace(
            operation_id=operation_id,
            state="failed" if self.failure_code else "recovering",
            material_state="prepared", receipt_state="none",
            binding_fingerprint=HASH_E, binding_summary=summary,
            code=self.failure_code,
        )


class _FailedStartBroker(_StartBroker):
    def auth_start(self, binding):
        self.calls.append(("auth_start", binding["operation_id"]))
        from app.services.core_auth_broker import AuthPublicState
        return AuthPublicState(
            version=1, operation_id=binding["operation_id"], state="failed",
            code="callback_mismatch",
        )

    def credential_discard_recovery(self, selector, action, **_private):
        self.calls.append((action, selector["operation_id"]))
        return SimpleNamespace(
            operation_id=selector["operation_id"], state="absent",
            material_state="none", receipt_state="none",
            binding_fingerprint=None, binding_summary=None, code=None,
        )


def test_actual_start_commits_ledger_before_core_and_replays_before_stale_or_catalog(
        tmp_path, monkeypatch):
    from app.models.collaboration import ProviderConnection
    from app.schemas.provider_auth import OperationBinding, TerminalOutcome
    from app.services import auth_operations, provider_catalog
    from app.services.connection_recovery import ConnectionRecoveryCoordinator

    engine, _path = _fresh_database(tmp_path, monkeypatch)
    with Session(engine) as db:
        row = db.query(ProviderConnection).filter_by(id=31, user_id=11).one()
        broker = _StartBroker(db)
        coordinator = ConnectionRecoveryCoordinator(
            broker=broker, owner_epochs=_OwnerEpochs(),
            auth_method_availability=lambda provider, method:
                (provider, method) == ("openai", "api_key"),
            clock=lambda: datetime(2026, 10, 7, tzinfo=timezone.utc),
        )
        first = coordinator.start_auth(
            db, row, "api_key", 3, 4, idempotency_key="exact-replay")
        assert first.state == "awaiting_user"
        assert broker.calls == [("allocate", 11, HASH_C), ("auth_start", HASH_A)]
        operation = auth_operations.operation(db, 11, HASH_A)
        auth_operations.terminalize(
            db,
            OperationBinding(
                operation_id=operation.operation_id, owner_id=operation.owner_id,
                connection_id=operation.connection_id,
                intent_fingerprint=operation.intent_fingerprint,
                current_generation_fingerprint=operation.current_generation_fingerprint,
            ),
            TerminalOutcome(state="succeeded", error_code=None, revoke_state="skipped"),
            selector=auth_operations.recovery_selector(operation),
        )
        row.readiness_revision = 4
        row.auth_grant_revision = 5
        row.public_config = {"changed_after_success": True}
        db.commit()
        monkeypatch.setattr(
            provider_catalog, "launchable_auth_method",
            lambda *_args: pytest.fail("exact replay rechecked changed catalog"),
        )

        replay = coordinator.start_auth(
            db, row, "api_key", 3, 4, idempotency_key="exact-replay")

        assert replay.state == "succeeded"
        assert replay.result == {"credential_state": "present"}
        assert broker.calls == [("allocate", 11, HASH_C), ("auth_start", HASH_A)]
        with pytest.raises(auth_operations.AuthOperationError) as conflict:
            coordinator.start_auth(
                db, row, "api_key", 3, 6, idempotency_key="exact-replay")
        assert conflict.value.code == "idempotency_conflict"
    engine.dispose()


def test_actual_start_rejects_unavailable_stale_and_busy_before_core(
        tmp_path, monkeypatch):
    from app.models.collaboration import ProviderConnection
    from app.services import auth_operations
    from app.services.connection_recovery import ConnectionRecoveryCoordinator

    engine, _path = _fresh_database(tmp_path, monkeypatch)
    with Session(engine) as db:
        row = db.query(ProviderConnection).filter_by(id=31, user_id=11).one()
        broker = _StartBroker(db)
        unavailable = ConnectionRecoveryCoordinator(
            broker=broker, owner_epochs=_OwnerEpochs())
        with pytest.raises(auth_operations.AuthOperationError) as blocked:
            unavailable.start_auth(
                db, row, "api_key", 3, 4, idempotency_key="unavailable")
        assert blocked.value.code == "auth_method_unavailable"
        assert broker.calls == []

        coordinator = ConnectionRecoveryCoordinator(
            broker=broker, owner_epochs=_OwnerEpochs(),
            auth_method_availability=lambda *_args: True,
            clock=lambda: datetime(2026, 10, 7, tzinfo=timezone.utc),
        )
        with pytest.raises(auth_operations.AuthOperationError) as stale:
            coordinator.start_auth(
                db, row, "api_key", 2, 4, idempotency_key="stale")
        assert stale.value.code == "configuration_changed"
        assert broker.calls == []
        coordinator.start_auth(
            db, row, "api_key", 3, 4, idempotency_key="active")
        calls = list(broker.calls)
        with pytest.raises(auth_operations.AuthOperationError) as busy:
            coordinator.start_auth(
                db, row, "api_key", 3, 4, idempotency_key="other")
        assert busy.value.code == "auth_operation_busy"
        assert broker.calls == calls
    engine.dispose()


def test_start_failure_is_terminal_only_after_recovery_proves_no_material(
        tmp_path, monkeypatch):
    from app.models.collaboration import ProviderConnection
    from app.services import auth_operations
    from app.services.connection_recovery import ConnectionRecoveryCoordinator

    engine, _path = _fresh_database(tmp_path, monkeypatch)
    with Session(engine) as db:
        row = db.query(ProviderConnection).filter_by(id=31, user_id=11).one()
        broker = _FailedStartBroker(db)
        coordinator = ConnectionRecoveryCoordinator(
            broker=broker, owner_epochs=_OwnerEpochs(),
            auth_method_availability=lambda *_args: True,
            clock=lambda: datetime(2026, 10, 7, tzinfo=timezone.utc),
        )

        result = coordinator.start_auth(
            db, row, "api_key", 3, 4, idempotency_key="start-failed")
        operation = auth_operations.operation(db, 11, HASH_A)

        assert (result.state, result.error_code) == (
            "failed", "provider_invalid_response")
        assert (operation.phase, operation.state, operation.error_code) == (
            "terminal", "failed", "provider_invalid_response")
        assert broker.calls == [
            ("allocate", 11, HASH_C), ("auth_start", HASH_A),
            ("inspect", HASH_A),
        ]
    engine.dispose()


def test_initial_auth_commits_exact_row_then_closes_only_after_same_call_cleanup(
        tmp_path, monkeypatch):
    from app.models.collaboration import ProviderConnection
    from app.services import auth_operations
    from app.services.connection_recovery import ConnectionRecoveryCoordinator

    engine, _path = _fresh_database(tmp_path, monkeypatch)
    with Session(engine) as db:
        row = db.query(ProviderConnection).filter_by(id=31, user_id=11).one()
        broker = _CommitBroker(db, cleanup_confirmed=True)
        coordinator = ConnectionRecoveryCoordinator(
            broker=broker, owner_epochs=_OwnerEpochs(),
            auth_method_availability=lambda *_args: True,
            clock=lambda: datetime(2026, 10, 7, tzinfo=timezone.utc),
        )
        started = coordinator.start_auth(
            db, row, "api_key", 3, 4, idempotency_key="initial-success")
        result = coordinator.operation_status(db, 11, 31, started.operation_id)

        db.expire_all()
        current = db.query(ProviderConnection).filter_by(id=31, user_id=11).one()
        operation = auth_operations.operation(db, 11, HASH_A)
        assert result.state == "succeeded"
        assert result.result == {"credential_state": "present"}
        assert (current.readiness_revision, current.auth_grant_revision) == (4, 5)
        assert current.secret_ref == "11:31:" + "9" * 32
        assert (operation.phase, operation.state) == ("terminal", "succeeded")
        assert broker.calls[-3:] == [
            ("auth_status", HASH_A), ("credential_commit", HASH_A),
            ("cleanup_committed", HASH_A),
        ]
        safe_sql = repr(db.execute(text(
            "SELECT operation_id,owner_id,connection_id,kind,state,phase,"
            "intent_fingerprint,before_state_fingerprint,error_code "
            "FROM connection_auth_operations"
        )).all())
        assert "8" * 64 not in safe_sql
        assert "11:31:" + "9" * 32 not in safe_sql
    engine.dispose()


def test_restart_with_absent_initial_cleanup_receipt_stays_pending(
        tmp_path, monkeypatch):
    from app.models.collaboration import ProviderConnection
    from app.services import auth_operations
    from app.services.connection_recovery import ConnectionRecoveryCoordinator

    engine, _path = _fresh_database(tmp_path, monkeypatch)
    with Session(engine) as db:
        row = db.query(ProviderConnection).filter_by(id=31, user_id=11).one()
        broker = _CommitBroker(db, cleanup_confirmed=False)
        coordinator = ConnectionRecoveryCoordinator(
            broker=broker, owner_epochs=_OwnerEpochs(),
            auth_method_availability=lambda *_args: True,
            clock=lambda: datetime(2026, 10, 7, tzinfo=timezone.utc),
        )
        started = coordinator.start_auth(
            db, row, "api_key", 3, 4, idempotency_key="initial-ambiguous")
        first = coordinator.operation_status(db, 11, 31, started.operation_id)
        assert first.state == "finalizing"
        assert first.error_code == "credential_cleanup_unconfirmed"

        restarted = ConnectionRecoveryCoordinator(
            broker=broker, owner_epochs=_OwnerEpochs(),
            auth_method_availability=lambda *_args: True,
        )
        recovered = restarted.operation_status(db, 11, 31, started.operation_id)
        operation = auth_operations.operation(db, 11, HASH_A)

        assert recovered.state == "pending"
        assert recovered.error_code == "configuration_changed"
        assert operation.phase == "reconcile_pending"
        assert operation.terminal_at is None
        assert operation.state != "succeeded"
    engine.dispose()


def test_initial_auth_restart_cleans_prepared_material_with_adopted_db_reference(
        tmp_path, monkeypatch):
    from app.models.collaboration import ProviderConnection
    from app.services import auth_operations
    from app.services.connection_recovery import ConnectionRecoveryCoordinator

    engine, _path = _fresh_database(tmp_path, monkeypatch)
    with Session(engine) as db:
        row = db.query(ProviderConnection).filter_by(id=31, user_id=11).one()
        broker = _RestartCleanupBroker(db)

        def crash(point):
            if point == "after_db_commit":
                raise RuntimeError("synthetic restart after initial DB commit")

        coordinator = ConnectionRecoveryCoordinator(
            broker=broker, owner_epochs=_OwnerEpochs(),
            auth_method_availability=lambda *_args: True,
            clock=lambda: datetime(2026, 10, 7, tzinfo=timezone.utc),
            crash_hook=crash,
        )
        started = coordinator.start_auth(
            db, row, "api_key", 3, 4, idempotency_key="restart-cleanup")
        with pytest.raises(RuntimeError, match="synthetic restart"):
            coordinator.operation_status(db, 11, 31, started.operation_id)

        db.expire_all()
        adopted = db.query(ProviderConnection).filter_by(id=31, user_id=11).one()
        assert adopted.secret_ref == broker.prepared_ref
        restarted = ConnectionRecoveryCoordinator(
            broker=broker, owner_epochs=_OwnerEpochs(),
            auth_method_availability=lambda *_args: True,
        )
        recovered = restarted.operation_status(db, 11, 31, started.operation_id)
        operation = auth_operations.operation(db, 11, HASH_A)

        assert (recovered.state, recovered.result) == (
            "succeeded", {"credential_state": "present"})
        assert (operation.phase, operation.state) == ("terminal", "succeeded")
        assert ("cleanup_committed", HASH_A, broker.prepared_ref) in broker.calls
    engine.dispose()


def test_initial_auth_restart_wrong_adopted_reference_fails_closed(
        tmp_path, monkeypatch):
    from app.models.collaboration import ProviderConnection
    from app.services import auth_operations
    from app.services.connection_recovery import ConnectionRecoveryCoordinator

    engine, _path = _fresh_database(tmp_path, monkeypatch)
    with Session(engine) as db:
        row = db.query(ProviderConnection).filter_by(id=31, user_id=11).one()
        broker = _RestartCleanupBroker(db)

        def crash(point):
            if point == "after_db_commit":
                raise RuntimeError("synthetic restart after initial DB commit")

        coordinator = ConnectionRecoveryCoordinator(
            broker=broker, owner_epochs=_OwnerEpochs(),
            auth_method_availability=lambda *_args: True,
            clock=lambda: datetime(2026, 10, 7, tzinfo=timezone.utc),
            crash_hook=crash,
        )
        started = coordinator.start_auth(
            db, row, "api_key", 3, 4, idempotency_key="wrong-reference")
        with pytest.raises(RuntimeError, match="synthetic restart"):
            coordinator.operation_status(db, 11, 31, started.operation_id)

        db.expire_all()
        winner = db.query(ProviderConnection).filter_by(id=31, user_id=11).one()
        winner.secret_ref = "11:31:" + "7" * 32
        db.commit()
        restarted = ConnectionRecoveryCoordinator(
            broker=broker, owner_epochs=_OwnerEpochs(),
            auth_method_availability=lambda *_args: True,
        )
        recovered = restarted.operation_status(db, 11, 31, started.operation_id)
        operation = auth_operations.operation(db, 11, HASH_A)

        assert (recovered.state, recovered.error_code) == (
            "finalizing", "configuration_changed")
        assert operation.terminal_at is None
        assert broker.prepared is True
        assert ("cleanup_committed", HASH_A, winner.secret_ref) in broker.calls
    engine.dispose()
