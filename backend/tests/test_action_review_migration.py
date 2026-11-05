"""Schema 25/26 preservation and rollback rehearsals on disposable SQLite."""

import importlib
import sqlite3
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from test_migrations import legacy_db
from app.core import migrate as migration

NEW_COLUMNS = {"review_id", "review_snapshot", "approved_at", "approval_owner_id"}


def rows(db):
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {name: (tuple(r[1] for r in db.execute(f'PRAGMA table_info("{name}")')),
                   db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()) for name in tables}


def pre26(tmp_path, monkeypatch):
    path = tmp_path / "before26.sqlite"
    legacy_db(path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    engine = create_engine(f"sqlite:///{path}")
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:25])
        migration.migrate_database(engine)
    with sqlite3.connect(path) as db:
        for state in ("pending", "waiting", "approved", "started", "completed", "uncertain"):
            db.execute("INSERT INTO tool_actions (action_id,room_id,agent_id,family,arguments,state) VALUES (?,51,41,'read_file','{}',?)", (str(uuid4()), state))
        db.execute("INSERT INTO room_grants (room_id,agent_id,scope,decision) VALUES (51,41,?,'approve')",
                   ('{"kind":"room_session"}',))
        db.commit()
        before = rows(db)
    return path, engine, before


def assert_old_rows(db, before):
    for table, (columns, expected) in before.items():
        if table == "schema_version": continue
        select = ",".join(f'"{c}"' for c in columns)
        assert db.execute(f'SELECT {select} FROM "{table}" ORDER BY rowid').fetchall() == expected


def test_schema26_is_additive_preserves_all_old_rows_and_leaves_reviews_unknown(tmp_path, monkeypatch):
    path, engine, before = pre26(tmp_path, monkeypatch)
    migration.migrate_database(engine)
    migration.migrate_database(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        assert_old_rows(db, before)
        assert db.execute("SELECT max(version) FROM schema_version").fetchone() == (26,)
        assert db.execute("SELECT review_id,review_snapshot,approved_at,approval_owner_id FROM tool_actions").fetchall() == [(None,) * 4] * 6
        columns = {r[1]: r for r in db.execute("PRAGMA table_info(tool_actions)")}
        assert NEW_COLUMNS <= columns.keys()
        assert all(columns[c][3] == 0 and columns[c][4] is None for c in NEW_COLUMNS)
        db.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE tool_actions SET approval_owner_id=99999")
        review_id = str(uuid4())
        db.execute("UPDATE tool_actions SET review_id=? WHERE rowid=1", (review_id,))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE tool_actions SET review_id=? WHERE rowid=2", (review_id,))
    # One legacy initialization and one pre26 backup; no new backup on replay.
    backups = list((tmp_path / "appdata/backups").glob("*.sqlite"))
    assert len(backups) == 2
    for p in backups: assert p.stat().st_mode & 0o077 == 0
    saved25 = []
    for p in backups:
        with sqlite3.connect(p) as db:
            if db.execute("SELECT name FROM sqlite_master WHERE name='schema_version'").fetchone() and db.execute("SELECT max(version) FROM schema_version").fetchone() == (25,):
                assert rows(db) == before
                saved25.append(p)
    assert len(saved25) == 1


def test_failed26_rolls_back_columns_index_and_history_without_losing_rows(tmp_path, monkeypatch):
    path, engine, before = pre26(tmp_path, monkeypatch)
    revision = importlib.import_module("app.core.migrations.0026_action_reviews")
    original = revision.upgrade
    def failing(db):
        original(db)
        raise RuntimeError("owned schema26 failure")
    monkeypatch.setattr(revision, "upgrade", failing)
    with pytest.raises(RuntimeError, match="owned schema26 failure"):
        migration.migrate_database(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        assert rows(db) == before
        assert not NEW_COLUMNS & {r[1] for r in db.execute("PRAGMA table_info(tool_actions)")}
        assert db.execute("SELECT name FROM sqlite_master WHERE name='ux_tool_actions_review_id'").fetchone() is None


@pytest.mark.parametrize("damage", ["missing", "non_unique", "wrong_column", "partial"])
def test_current26_does_not_accept_missing_or_weakened_unique_review_index(tmp_path, monkeypatch, damage):
    path, engine, _ = pre26(tmp_path, monkeypatch)
    migration.migrate_database(engine)
    with sqlite3.connect(path) as db:
        db.execute("DROP INDEX ux_tool_actions_review_id")
        if damage == "non_unique": db.execute("CREATE INDEX ux_tool_actions_review_id ON tool_actions(review_id)")
        elif damage == "wrong_column": db.execute("CREATE UNIQUE INDEX ux_tool_actions_review_id ON tool_actions(action_id)")
        elif damage == "partial": db.execute("CREATE UNIQUE INDEX ux_tool_actions_review_id ON tool_actions(review_id) WHERE state='waiting'")
    with pytest.raises(RuntimeError, match="unique index ux_tool_actions_review_id"):
        migration.migrate_database(engine)
    engine.dispose()


def test_old25_refuses_new26_then_restores_pre26_after_preserving_post26(tmp_path, monkeypatch):
    path, engine, before = pre26(tmp_path, monkeypatch)
    migration.migrate_database(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        new_id, review_id = str(uuid4()), str(uuid4())
        db.execute("INSERT INTO tool_actions (action_id,room_id,agent_id,family,arguments,state,review_id,review_snapshot) VALUES (?,51,41,'read_file','{}','waiting',?,'{}')", (new_id, review_id))
        db.commit()
        after26 = rows(db)
        post26 = tmp_path / "post26-recovery.sqlite"
        with sqlite3.connect(post26) as copy: db.backup(copy)
        post26.chmod(0o600)
    old_engine = create_engine(f"sqlite:///{path}")
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:25])
        with pytest.raises(RuntimeError, match="26 is newer"):
            migration.migrate_database(old_engine)
    old_engine.dispose()
    saved25 = None
    for p in (tmp_path / "appdata/backups").glob("*.sqlite"):
        with sqlite3.connect(p) as db:
            if db.execute("SELECT name FROM sqlite_master WHERE name='schema_version'").fetchone() and db.execute("SELECT max(version) FROM schema_version").fetchone() == (25,): saved25 = p
    assert saved25 is not None
    # Rehearsal uses SQLite backup API, never DROP COLUMN or deletes post26 rows.
    with sqlite3.connect(saved25) as old, sqlite3.connect(path) as restored: old.backup(restored)
    restored_engine = create_engine(f"sqlite:///{path}")
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:25])
        migration.migrate_database(restored_engine)
    restored_engine.dispose()
    with sqlite3.connect(path) as db: assert rows(db) == before
    with sqlite3.connect(post26) as db:
        assert rows(db) == after26
        assert db.execute("SELECT review_id FROM tool_actions WHERE action_id=?", (new_id,)).fetchone() == (review_id,)
    assert post26.stat().st_mode & 0o077 == 0
