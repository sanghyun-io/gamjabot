"""Exercise real 30→31→32 DDL on a synthetic fixture, never the Owner database."""
import json
import os
from pathlib import Path
import sqlite3

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core import migrate as migration
from app.schemas.bot_profile import BotConversationResolve, BotPrimaryWrite
from app.schemas.bot_memory import BotMemoryWrite
from app.services import bot_memory, bot_profile
from test_migrations import legacy_db


def _snapshot(path):
    with sqlite3.connect(path) as db:
        names = [name for name, in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {
            name: {
                "ddl": db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()[0],
                "columns": db.execute(f'PRAGMA table_info("{name}")').fetchall(),
                "rows": db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall(),
            } for name in names
        }


def _preserved(before, after):
    for name, original in before.items():
        assert after[name]["ddl"] == original["ddl"], name
        assert after[name]["columns"] == original["columns"], name
        if name != "schema_version":
            assert after[name]["rows"] == original["rows"], name


def test_actual_schema30_to31_to32_preserves_existing_data(tmp_path, monkeypatch):
    # The outer caller supplies --basetemp inside a TemporaryDirectory under
    # TMPDIR. Every database and migration backup stays inside that directory.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    path = tmp_path / "synthetic-schema30.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    legacy_db(path)
    engine = create_engine(f"sqlite:///{path}")
    revisions = migration.REVISIONS
    assert revisions[30:32] == ("0031_bot_profile", "0032_bot_memory")
    report = {"state": "running", "owner_database_opened": False,
              "fixture": "test_migrations.legacy_db plus actual migration runner",
              "test_file_before": "ABSENT", "path": "30 -> 31 -> 32"}
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(migration, "REVISIONS", revisions[:30])
            migration.migrate_database(engine)
        with sqlite3.connect(path) as db:
            # Existing fixture leaves legacy activation flags NULL. Set only
            # synthetic flags before taking the schema30 preservation baseline.
            for table in ("users", "agents", "projects", "conversations"):
                db.execute(f'UPDATE "{table}" SET is_active=1')
        baseline30 = _snapshot(path)
        assert max(row[0] for row in baseline30["schema_version"]["rows"]) == 30
        with monkeypatch.context() as scoped:
            scoped.setattr(migration, "REVISIONS", revisions[:31])
            migration.migrate_database(engine)
        after31 = _snapshot(path)
        _preserved(baseline30, after31)
        assert set(after31) - set(baseline30) == {"bot_owner_profiles", "bot_conversations"}
        assert [row[0] for row in after31["schema_version"]["rows"]] == list(range(1, 32))
        report["30_to31"] = {"state": "passed", "preserved_tables": len(baseline30),
            "preserved_non_version_rows": sum(len(v["rows"]) for k, v in baseline30.items() if k != "schema_version"),
            "added_tables": sorted(set(after31) - set(baseline30))}

        sessions = sessionmaker(bind=engine)
        with sessions() as db:
            selected = bot_profile.set_primary(db, 11, BotPrimaryWrite(agent_id=41, expected_revision=0))
            assert selected["revision"] == 1
            resolved = bot_profile.resolve_conversation(db, 11, 41, BotConversationResolve())
            replayed = bot_profile.resolve_conversation(db, 11, 41, BotConversationResolve())
            assert resolved["created"] and replayed["replayed"]
            assert resolved["room_id"] == replayed["room_id"]
            assert bot_profile.is_personal_conversation(db, 11, 41, resolved["room_id"])
        baseline31 = _snapshot(path)
        with monkeypatch.context() as scoped:
            scoped.setattr(migration, "REVISIONS", revisions[:32])
            migration.migrate_database(engine)
        after32 = _snapshot(path)
        _preserved(baseline31, after32)
        assert set(after32) - set(baseline31) == {
            "bot_memory_entries", "bot_memory_state", "bot_memory_commands"}
        assert [row[0] for row in after32["schema_version"]["rows"]] == list(range(1, 33))
        report["31_to32"] = {"state": "passed", "preserved_tables": len(baseline31),
            "preserved_non_version_rows": sum(len(v["rows"]) for k, v in baseline31.items() if k != "schema_version"),
            "added_tables": sorted(set(after32) - set(baseline31))}

        with sessions() as db:
            memory = bot_memory.mutate(db, 11, 41, "add", BotMemoryWrite(
                expected_scope_revision=0, category="preference", content="SYNTHETIC_MIGRATION_MEMORY"),
                "synthetic-migration-memory")
            assert memory["revision"] == 1 and memory["entry_revision"] == 1
            listing = bot_memory.listing(db, 11, 41)
            assert any(item["memory_id"] == memory["memory_id"] for item in listing["items"])
        final32 = _snapshot(path)
        backups_before = set((tmp_path / "appdata" / "backups").glob("*.sqlite"))
        with monkeypatch.context() as scoped:
            scoped.setattr(migration, "REVISIONS", revisions[:32])
            migration.migrate_database(engine)
        assert _snapshot(path) == final32
        assert set((tmp_path / "appdata" / "backups").glob("*.sqlite")) == backups_before
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        backup_versions = []
        for backup in backups_before:
            assert backup.stat().st_mode & 0o077 == 0
            with sqlite3.connect(backup) as db:
                has_history = db.execute("SELECT 1 FROM sqlite_master WHERE name='schema_version'").fetchone()
                backup_versions.append(db.execute("SELECT max(version) FROM schema_version").fetchone()[0] if has_history else 0)
        assert sorted(backup_versions) == [0, 30, 31]
        report.update({"state": "passed", "migrated_schema": 32,
            "actual_ddl_primary_dm_memory_services": "passed", "second_startup_idempotent": True,
            "foreign_key_check": [], "integrity_check": "ok", "backup_schema_versions": sorted(backup_versions)})
    finally:
        engine.dispose()
        destination = os.environ.get("GAMJA_MIGRATION_CONTRACT_REPORT")
        if destination:
            Path(destination).write_text(json.dumps(report, indent=2) + "\n")
