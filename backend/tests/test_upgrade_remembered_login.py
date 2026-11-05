"""Tests for upgrade_remembered_login service.

All tests use SQLAlchemy SQLite in tmpdir fixture with no real Owner data or network access.
TMPDIR is used and cleaned up on exit, PYTHONDONTWRITEBYTECODE=1.
"""

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.services.secrets import SecretStoreUnavailable
from app.services.upgrade_remembered_login import (
    ExpectedState, UpgradeFailed, UpgradeResult, upgrade_remembered_login
)


# Test fixtures
@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary SQLite database for testing."""
    db_path = tmp_path / "upgrade.sqlite3"
    engine = create_engine(f"sqlite:///{db_path}")
    SessionLocal = sessionmaker(bind=engine)

    # Create tables
    with SessionLocal() as db:
        db.execute(text("""CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            email TEXT UNIQUE,
            username TEXT UNIQUE,
            hashed_password TEXT,
            is_active INTEGER
        )"""))
        db.execute(text("""CREATE TABLE remembered_login (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            secret_hash TEXT NOT NULL,
            expires_at DATETIME NOT NULL,
            secret_ref VARCHAR(32) NOT NULL DEFAULT 'current',
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0)
        )"""))
        db.commit()

    try:
        yield SessionLocal
    finally:
        engine.dispose()
        db_path.unlink(missing_ok=True)


class FakeStore:
    """Fake secret store for testing."""
    def __init__(self):
        self.values = {}
        self.deleted = []
        self.put_calls = 0
        self.fail_put = False
        self.fail_get = False

    def put(self, ref, value):
        if self.fail_put:
            raise RuntimeError("synthetic put failure")
        self.values[ref] = value
        self.put_calls += 1

    def get_no_ui(self, ref):
        if self.fail_get:
            raise RuntimeError("synthetic get failure")
        return self.values.get(ref)

    def delete(self, ref):
        self.deleted.append(ref)
        self.values.pop(ref, None)


def test_successful_upgrade_preserves_expiry_and_keeps_old_ref(tmp_db):
    """Successful upgrade preserves expiry exactly and never deletes old secret."""
    store = FakeStore()
    old_credential = "old-device-credential"
    old_hash = hashlib.sha256(old_credential.encode()).hexdigest()
    old_ref = "a" * 32
    owner_id = 1
    revision = 1
    expiry = datetime.utcnow() + timedelta(days=10)

    with tmp_db() as db:
        db.execute(text("INSERT INTO users VALUES (1, 'test@example.com', 'test', 'hash', 1)"))
        db.execute(text("""
            INSERT INTO remembered_login (id, owner_id, secret_hash, expires_at, secret_ref, revision)
            VALUES (1, :owner, :hash, :expiry, :ref, :rev)
        """), {"owner": owner_id, "hash": old_hash, "expiry": expiry.isoformat(), "ref": old_ref, "rev": revision})
        db.commit()

        expected = ExpectedState(
            owner_id=owner_id,
            old_secret_ref=old_ref,
            revision=revision,
            original_expires_at=expiry,
        )

        result = upgrade_remembered_login(db, store, expected)

        assert isinstance(result, UpgradeResult)
        assert len(result.new_secret_ref) == 32
        assert result.new_revision == 2
        assert result.expires_at == expiry

        # Verify DB state
        row = db.execute(text(
            "SELECT secret_ref, revision, owner_id, expires_at, secret_hash "
            "FROM remembered_login WHERE id = 1")).first()
        assert row.secret_ref == result.new_secret_ref
        assert row.revision == 2
        assert row.owner_id == owner_id
        assert datetime.fromisoformat(row.expires_at) == expiry

        # Old ref should NOT be deleted
        assert old_ref not in store.deleted
        # New ref should exist in store
        assert result.new_secret_ref in store.values


def test_rejection_before_native_write_on_drift(tmp_db):
    """Drift/revoked/inactive/expired rejections happen BEFORE any native put."""
    store = FakeStore()
    old_credential = "old-device-credential"
    old_hash = hashlib.sha256(old_credential.encode()).hexdigest()
    old_ref = "a" * 32
    owner_id = 1
    revision = 1
    expiry = datetime.utcnow() + timedelta(days=10)

    with tmp_db() as db:
        db.execute(text("INSERT INTO users VALUES (1, 'test@example.com', 'test', 'hash', 1)"))
        # Add another user for owner drift test
        db.execute(text("INSERT INTO users VALUES (99, 'other@example.com', 'other', 'hash', 1)"))
        db.execute(text("""
            INSERT INTO remembered_login (id, owner_id, secret_hash, expires_at, secret_ref, revision)
            VALUES (1, :owner, :hash, :expiry, :ref, :rev)
        """), {"owner": owner_id, "hash": old_hash, "expiry": expiry.isoformat(), "ref": old_ref, "rev": revision})
        db.commit()

        # Test revision drift
        with pytest.raises(UpgradeFailed) as exc:
            expected = ExpectedState(owner_id=owner_id, old_secret_ref=old_ref, revision=99, original_expires_at=expiry)
            upgrade_remembered_login(db, store, expected)
        assert exc.value._diagnostic["reason"] == "revision_drift"
        assert store.put_calls == 0  # No native write attempted

        # Test owner drift
        with pytest.raises(UpgradeFailed) as exc:
            expected = ExpectedState(owner_id=99, old_secret_ref=old_ref, revision=revision, original_expires_at=expiry)
            upgrade_remembered_login(db, store, expected)
        assert exc.value._diagnostic["reason"] == "owner_id_drift"
        assert store.put_calls == 0

        # Test secret_ref drift
        with pytest.raises(UpgradeFailed) as exc:
            expected = ExpectedState(owner_id=owner_id, old_secret_ref="b" * 32, revision=revision, original_expires_at=expiry)
            upgrade_remembered_login(db, store, expected)
        assert exc.value._diagnostic["reason"] == "secret_ref_drift"
        assert store.put_calls == 0

        # Test expiry drift
        with pytest.raises(UpgradeFailed) as exc:
            expected = ExpectedState(owner_id=owner_id, old_secret_ref=old_ref, revision=revision, original_expires_at=datetime.utcnow())
            upgrade_remembered_login(db, store, expected)
        assert exc.value._diagnostic["reason"] == "expiry_drift"
        assert store.put_calls == 0


def test_rejection_before_native_write_on_inactive_user(tmp_db):
    """Inactive user rejection happens BEFORE any native put."""
    store = FakeStore()
    old_credential = "old-device-credential"
    old_hash = hashlib.sha256(old_credential.encode()).hexdigest()
    old_ref = "a" * 32
    owner_id = 1
    revision = 1
    expiry = datetime.utcnow() + timedelta(days=10)

    with tmp_db() as db:
        db.execute(text("INSERT INTO users VALUES (1, 'test@example.com', 'test', 'hash', 0)"))  # is_active = 0
        db.execute(text("""
            INSERT INTO remembered_login (id, owner_id, secret_hash, expires_at, secret_ref, revision)
            VALUES (1, :owner, :hash, :expiry, :ref, :rev)
        """), {"owner": owner_id, "hash": old_hash, "expiry": expiry.isoformat(), "ref": old_ref, "rev": revision})
        db.commit()

        expected = ExpectedState(owner_id=owner_id, old_secret_ref=old_ref, revision=revision, original_expires_at=expiry)

        with pytest.raises(UpgradeFailed) as exc:
            upgrade_remembered_login(db, store, expected)
        assert exc.value._diagnostic["reason"] == "user_inactive"
        assert store.put_calls == 0


def test_rejection_before_native_write_on_expired(tmp_db):
    """Already expired rejection happens BEFORE any native put."""
    store = FakeStore()
    old_credential = "old-device-credential"
    old_hash = hashlib.sha256(old_credential.encode()).hexdigest()
    old_ref = "a" * 32
    owner_id = 1
    revision = 1
    expiry = datetime.utcnow() - timedelta(days=1)  # Already expired

    with tmp_db() as db:
        db.execute(text("INSERT INTO users VALUES (1, 'test@example.com', 'test', 'hash', 1)"))
        db.execute(text("""
            INSERT INTO remembered_login (id, owner_id, secret_hash, expires_at, secret_ref, revision)
            VALUES (1, :owner, :hash, :expiry, :ref, :rev)
        """), {"owner": owner_id, "hash": old_hash, "expiry": expiry.isoformat(), "ref": old_ref, "rev": revision})
        db.commit()

        expected = ExpectedState(owner_id=owner_id, old_secret_ref=old_ref, revision=revision, original_expires_at=expiry)

        with pytest.raises(UpgradeFailed) as exc:
            upgrade_remembered_login(db, store, expected)
        assert exc.value._diagnostic["reason"] == "already_expired"
        assert store.put_calls == 0


def test_readback_failure_cleans_new_secret_only(tmp_db):
    """Readback failure cleans up only the new uncommitted secret."""
    store = FakeStore()
    store.fail_get = True  # Simulate readback failure
    old_credential = "old-device-credential"
    old_hash = hashlib.sha256(old_credential.encode()).hexdigest()
    old_ref = "a" * 32
    owner_id = 1
    revision = 1
    expiry = datetime.utcnow() + timedelta(days=10)

    with tmp_db() as db:
        db.execute(text("INSERT INTO users VALUES (1, 'test@example.com', 'test', 'hash', 1)"))
        db.execute(text("""
            INSERT INTO remembered_login (id, owner_id, secret_hash, expires_at, secret_ref, revision)
            VALUES (1, :owner, :hash, :expiry, :ref, :rev)
        """), {"owner": owner_id, "hash": old_hash, "expiry": expiry.isoformat(), "ref": old_ref, "rev": revision})
        db.commit()

        expected = ExpectedState(owner_id=owner_id, old_secret_ref=old_ref, revision=revision, original_expires_at=expiry)

        with pytest.raises(UpgradeFailed) as exc:
            upgrade_remembered_login(db, store, expected)

        assert exc.value._diagnostic["reason"] == "readback_failed"
        # New secret should be cleaned up
        assert len(store.deleted) == 1
        assert store.deleted[0] not in store.values  # The new ref
        # Old secret should NOT be deleted
        assert old_ref not in store.deleted
        # DB should be unchanged
        row = db.execute(text("SELECT secret_ref, revision FROM remembered_login WHERE id = 1")).first()
        assert row.secret_ref == old_ref
        assert row.revision == 1


def test_native_put_failure_cleans_new_secret_only(tmp_db):
    """Native put failure cleans up only the new uncommitted secret."""
    store = FakeStore()
    store.fail_put = True  # Simulate put failure
    old_credential = "old-device-credential"
    old_hash = hashlib.sha256(old_credential.encode()).hexdigest()
    old_ref = "a" * 32
    owner_id = 1
    revision = 1
    expiry = datetime.utcnow() + timedelta(days=10)

    with tmp_db() as db:
        db.execute(text("INSERT INTO users VALUES (1, 'test@example.com', 'test', 'hash', 1)"))
        db.execute(text("""
            INSERT INTO remembered_login (id, owner_id, secret_hash, expires_at, secret_ref, revision)
            VALUES (1, :owner, :hash, :expiry, :ref, :rev)
        """), {"owner": owner_id, "hash": old_hash, "expiry": expiry.isoformat(), "ref": old_ref, "rev": revision})
        db.commit()

        expected = ExpectedState(owner_id=owner_id, old_secret_ref=old_ref, revision=revision, original_expires_at=expiry)

        with pytest.raises(UpgradeFailed) as exc:
            upgrade_remembered_login(db, store, expected)

        assert exc.value._diagnostic["reason"] == "store_unavailable"
        # Old secret should NOT be deleted
        assert old_ref not in store.deleted


def test_cas_race_reports_conflict_and_cleans_new_secret(tmp_db):
    """CAS race reports 409 conflict and cleans up new secret."""
    store = FakeStore()
    old_credential = "old-device-credential"
    old_hash = hashlib.sha256(old_credential.encode()).hexdigest()
    old_ref = "a" * 32
    winner_ref = "w" * 32
    owner_id = 1
    revision = 1
    expiry = datetime.utcnow() + timedelta(days=10)

    class RacingSession:
        """Session that simulates concurrent update during CAS."""
        def __init__(self, session, tmp_db):
            self.session = session
            self.tmp_db = tmp_db
            self._update_count = 0

        def __getattr__(self, name):
            return getattr(self.session, name)

        def execute(self, statement, parameters=None):
            # If this is the UPDATE statement, simulate concurrent modification
            if str(statement).lstrip().startswith("UPDATE remembered_login"):
                # Release the read transaction, then make a real competing commit.
                self.session.rollback()
                with self.tmp_db() as concurrent:
                    concurrent.execute(text("""
                        UPDATE remembered_login
                        SET secret_ref = :winner_ref, revision = 2
                        WHERE id = 1
                    """), {"winner_ref": winner_ref})
                    concurrent.commit()

                # Now execute the original UPDATE - it will match 0 rows
                return self.session.execute(statement, parameters or {})
            return self.session.execute(statement, parameters or {})

        def commit(self):
            # Don't commit since the race failed
            pass

    with tmp_db() as db:
        db.execute(text("INSERT INTO users VALUES (1, 'test@example.com', 'test', 'hash', 1)"))
        db.execute(text("""
            INSERT INTO remembered_login (id, owner_id, secret_hash, expires_at, secret_ref, revision)
            VALUES (1, :owner, :hash, :expiry, :ref, :rev)
        """), {"owner": owner_id, "hash": old_hash, "expiry": expiry.isoformat(), "ref": old_ref, "rev": revision})
        db.commit()

        expected = ExpectedState(owner_id=owner_id, old_secret_ref=old_ref, revision=revision, original_expires_at=expiry)

        with pytest.raises(HTTPException) as exc:
            upgrade_remembered_login(RacingSession(db, tmp_db), store, expected)

        assert exc.value.status_code == 409
        # New secret should be cleaned up
        assert len(store.deleted) == 1
        # DB should have winner's ref
        row = db.execute(text("SELECT secret_ref, revision FROM remembered_login WHERE id = 1")).first()
        assert row.secret_ref == winner_ref
        assert row.revision == 2


def test_commit_succeeded_but_raised_rechecks_db(tmp_db):
    """Commit succeeded but raised exception rechecks DB for actual state."""
    store = FakeStore()
    old_credential = "old-device-credential"
    old_hash = hashlib.sha256(old_credential.encode()).hexdigest()
    old_ref = "a" * 32
    owner_id = 1
    revision = 1
    expiry = datetime.utcnow() + timedelta(days=10)

    class CommitSuccessButRaise:
        def __init__(self, session):
            self.session = session

        def __getattr__(self, name):
            return getattr(self.session, name)

        def commit(self):
            self.session.commit()
            raise RuntimeError("synthetic commit exception")

    with tmp_db() as db:
        db.execute(text("INSERT INTO users VALUES (1, 'test@example.com', 'test', 'hash', 1)"))
        db.execute(text("""
            INSERT INTO remembered_login (id, owner_id, secret_hash, expires_at, secret_ref, revision)
            VALUES (1, :owner, :hash, :expiry, :ref, :rev)
        """), {"owner": owner_id, "hash": old_hash, "expiry": expiry.isoformat(), "ref": old_ref, "rev": revision})
        db.commit()

        expected = ExpectedState(owner_id=owner_id, old_secret_ref=old_ref, revision=revision, original_expires_at=expiry)

        result = upgrade_remembered_login(CommitSuccessButRaise(db), store, expected)

        # Should succeed after rechecking DB
        assert isinstance(result, UpgradeResult)
        assert len(result.new_secret_ref) == 32
        assert result.new_revision == 2

        # Verify DB state
        row = db.execute(text("SELECT secret_ref, revision FROM remembered_login WHERE id = 1")).first()
        assert row.secret_ref == result.new_secret_ref
        assert row.revision == 2


def test_ambiguous_commit_keeps_new_ref_and_reports_uncertain(tmp_db):
    """Ambiguous commit (can't verify) keeps new ref and reports uncertain."""
    from app.services import upgrade_remembered_login as module

    store = FakeStore()
    old_credential = "old-device-credential"
    old_hash = hashlib.sha256(old_credential.encode()).hexdigest()
    old_ref = "a" * 32
    owner_id = 1
    revision = 1
    expiry = datetime.utcnow() + timedelta(days=10)

    class AmbiguousCommit:
        def __init__(self, session):
            self.session = session

        def __getattr__(self, name):
            return getattr(self.session, name)

        def commit(self):
            self.session.commit()
            raise RuntimeError("synthetic commit exception")

    with tmp_db() as db:
        db.execute(text("INSERT INTO users VALUES (1, 'test@example.com', 'test', 'hash', 1)"))
        db.execute(text("""
            INSERT INTO remembered_login (id, owner_id, secret_hash, expires_at, secret_ref, revision)
            VALUES (1, :owner, :hash, :expiry, :ref, :rev)
        """), {"owner": owner_id, "hash": old_hash, "expiry": expiry.isoformat(), "ref": old_ref, "rev": revision})
        db.commit()

        expected = ExpectedState(owner_id=owner_id, old_secret_ref=old_ref, revision=revision, original_expires_at=expiry)

        # Make Session construction fail to simulate unreadable commit result
        original_session = module.Session
        try:
            call_count = [0]
            def failing_session(**kwargs):
                call_count[0] += 1
                if call_count[0] == 1:  # First call (for verification)
                    raise RuntimeError("synthetic unreadable commit result")
                return original_session(**kwargs)

            module.Session = failing_session

            with pytest.raises(UpgradeFailed) as exc:
                upgrade_remembered_login(AmbiguousCommit(db), store, expected)

            assert exc.value._diagnostic["reason"] == "verification_failed"
            assert exc.value._diagnostic["cleanup"] == "not_applicable"

            assert store.deleted == []
            assert len(store.values) == 1
            row = db.execute(text("SELECT secret_ref FROM remembered_login WHERE id = 1")).first()
            assert row.secret_ref in store.values
        finally:
            module.Session = original_session


def test_bool_owner_id_rejected(tmp_db):
    """Boolean owner_id is rejected (must be positive int)."""
    store = FakeStore()
    old_ref = "a" * 32
    expiry = datetime.utcnow() + timedelta(days=10)

    with tmp_db() as db:
        db.execute(text("INSERT INTO users VALUES (1, 'test@example.com', 'test', 'hash', 1)"))
        db.execute(text("""
            INSERT INTO remembered_login (id, owner_id, secret_hash, expires_at, secret_ref, revision)
            VALUES (1, 1, 'hash', :expiry, :ref, 1)
        """), {"expiry": expiry.isoformat(), "ref": old_ref})
        db.commit()

        with pytest.raises(UpgradeFailed):
            ExpectedState(True, old_ref, 1, expiry)
        assert store.put_calls == 0


@pytest.mark.parametrize("change", [
    {"owner_id": 0}, {"owner_id": "1"}, {"revision": True}, {"revision": 0},
    {"revision": 1.0}, {"old_secret_ref": "z" * 32}, {"old_secret_ref": None},
    {"original_expires_at": "tomorrow"},
    {"original_expires_at": datetime.now(timezone.utc)},
])
def test_invalid_expected_types_rejected(change):
    values = dict(owner_id=1, old_secret_ref="a"*32, revision=1,
                  original_expires_at=datetime.utcnow()+timedelta(days=10))
    values.update(change)
    with pytest.raises(UpgradeFailed) as error:
        ExpectedState(**values)
    assert error.value._diagnostic["reason"] == "invalid_expected_state"


@pytest.mark.parametrize("stored", ["not-a-date", "2026-10-11T00:00:00+00:00"])
def test_invalid_stored_expiry_rejected_before_native_write(tmp_db, stored):
    with tmp_db() as db:
        db.execute(text("INSERT INTO users VALUES (1, 'test@example.com', 'test', 'hash', 1)"))
        db.execute(text("INSERT INTO remembered_login VALUES (1, 1, 'hash', :expiry, :ref, 1)"),
                   {"expiry": stored, "ref": "a"*32})
        db.commit()
        store = FakeStore()
        expected = ExpectedState(1, "a"*32, 1, datetime.utcnow()+timedelta(days=10))
        with pytest.raises(UpgradeFailed) as error:
            upgrade_remembered_login(db, store, expected)
        assert error.value._diagnostic["reason"] == "invalid_stored_expiry"
        assert store.put_calls == 0


def test_existing_space_formatted_expiry_preserved(tmp_db):
    expiry = datetime.utcnow()+timedelta(days=10)
    stored = expiry.isoformat(sep=" ")
    with tmp_db() as db:
        db.execute(text("INSERT INTO users VALUES (1, 'test@example.com', 'test', 'hash', 1)"))
        db.execute(text("INSERT INTO remembered_login VALUES (1, 1, 'hash', :expiry, :ref, 1)"),
                   {"expiry": stored, "ref": "current"})
        db.commit()
        store = FakeStore()
        result = upgrade_remembered_login(db, store, ExpectedState(1, "current", 1, expiry))
        assert result.expires_at == expiry
        assert db.execute(text("SELECT expires_at FROM remembered_login WHERE id=1")).scalar_one() == stored
        assert "current" not in store.deleted


def test_no_remembered_login_row_rejected_before_native_write(tmp_db):
    """No remembered_login row is rejected before any native write."""
    store = FakeStore()
    owner_id = 1
    revision = 1
    expiry = datetime.utcnow() + timedelta(days=10)
    old_ref = "a" * 32

    with tmp_db() as db:
        db.execute(text("INSERT INTO users VALUES (1, 'test@example.com', 'test', 'hash', 1)"))
        db.commit()

        expected = ExpectedState(owner_id=owner_id, old_secret_ref=old_ref, revision=revision, original_expires_at=expiry)

        with pytest.raises(UpgradeFailed) as exc:
            upgrade_remembered_login(db, store, expected)

        assert exc.value._diagnostic["reason"] == "no_remembered_login"
        assert store.put_calls == 0


# Run tests
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
