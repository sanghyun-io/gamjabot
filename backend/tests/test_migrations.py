"""Migration checks use only synthetic SQLite files under pytest's temp directory."""

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.core.migrate import REVISIONS, migrate_database
from app.main import create_app
from app.models.models import Base, User, Agent, Project, ProjectAgent, Conversation, Message, RemoteDevice


LEGACY_TABLES = (
    "users", "agents", "projects", "project_agents", "conversations", "messages", "remote_devices"
)


def legacy_db(path: Path):
    """Create a pre-migration database with explicit IDs and tied message timestamps."""
    engine = create_engine(f"sqlite:///{path}")
    # A true pre-revision schema must not inherit Agent's newly mapped columns.
    with engine.begin() as connection:
        connection.exec_driver_sql("""CREATE TABLE agents (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
            name VARCHAR NOT NULL, description TEXT NOT NULL, system_prompt TEXT NOT NULL,
            model VARCHAR NOT NULL, temperature VARCHAR NOT NULL, max_tokens INTEGER NOT NULL,
            is_active BOOLEAN, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME)""")
        connection.exec_driver_sql("""CREATE TABLE projects (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
            name VARCHAR NOT NULL, description TEXT, is_active BOOLEAN,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME)""")
    Base.metadata.create_all(engine, tables=[
        User.__table__, Agent.__table__, Project.__table__, ProjectAgent.__table__,
        Conversation.__table__, Message.__table__, RemoteDevice.__table__,
    ])
    engine.dispose()
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO users (id,email,username,hashed_password) VALUES (11,'a@x','alice','hash-a'),(22,'b@x','bob','hash-b')")
        db.execute("INSERT INTO projects (id,user_id,name) VALUES (31,11,'Original')")
        db.execute("INSERT INTO agents (id,user_id,name,description,system_prompt,model,temperature,max_tokens) VALUES (41,11,'Ada','desc','prompt','gpt-4','0.7',2000)")
        db.execute("INSERT INTO project_agents (project_id,agent_id) VALUES (31,41)")
        db.execute("INSERT INTO conversations (id,user_id,project_id,title) VALUES (51,11,31,'Room'),(52,22,31,'Other')")
        db.execute("INSERT INTO messages (id,conversation_id,agent_id,role,content,created_at) VALUES (63,51,41,'assistant','third','2024-01-02 00:00:00'),(61,51,NULL,'user','first','2024-01-01 00:00:00'),(62,51,41,'assistant','second','2024-01-01 00:00:00'),(64,52,NULL,'user','other','2024-01-01 00:00:00')")
        db.execute("INSERT INTO remote_devices (id,user_id,name,access_token) VALUES (71,11,'Old phone','token')")


def snapshot(db, tables=LEGACY_TABLES):
    return {
        table: db.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
        for table in tables
    }


def project_rows(db):
    return db.execute(
        "SELECT id,user_id,name,description,is_active,created_at,updated_at FROM projects ORDER BY id"
    ).fetchall()


def test_revision_five_preserves_legacy_agent_model_and_messages_without_invented_connection(tmp_path, monkeypatch):
    from app.core import migrate as migration
    path = tmp_path / "revision-four.sqlite"
    legacy_db(path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    engine = create_engine(f"sqlite:///{path}")
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:4])
        migration.migrate_database(engine)
    with sqlite3.connect(path) as db:
        before_messages = db.execute("SELECT * FROM messages ORDER BY id").fetchall()
        before_agent = db.execute("SELECT id,user_id,model FROM agents WHERE id=41").fetchone()
    migration.migrate_database(engine)
    migration.migrate_database(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT id,user_id,model FROM agents WHERE id=41").fetchone() == before_agent
        assert db.execute("SELECT connection_id,model_id FROM agents WHERE id=41").fetchone() == (None, "gpt-4")
        assert db.execute("SELECT * FROM messages ORDER BY id").fetchall() == before_messages
        assert db.execute("SELECT version FROM schema_version ORDER BY version").fetchall() == [(n,) for n in range(1, len(REVISIONS) + 1)]
        db.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE agents SET connection_id=999 WHERE id=41")


def test_legacy_rows_survive_two_startups_with_ordered_room_events(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite"
    legacy_db(path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "test-only-token")
    with sqlite3.connect(path) as db:
        before = snapshot(db)
        before_projects = project_rows(db)
    app = create_app(database_url=f"sqlite:///{path}")
    for _ in range(2):
        with TestClient(app, headers={"X-Gamja-Session": "test-only-token"}) as client:
            assert client.get("/health").json() == {"status": "healthy"}
    with sqlite3.connect(path) as db:
        assert {table: rows for table, rows in snapshot(db).items() if table not in {"agents", "projects"}} == {table: rows for table, rows in before.items() if table not in {"agents", "projects"}}
        assert project_rows(db) == before_projects
        assert db.execute("SELECT id,user_id,name,description,system_prompt,model,temperature,max_tokens,is_active,created_at,updated_at FROM agents").fetchall() == before["agents"]
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(n,) for n in range(1, len(REVISIONS) + 1)]
        events = db.execute("SELECT room_id,seq,actor_type,actor_id,actor_name_snapshot,payload FROM room_events ORDER BY room_id,seq").fetchall()
        assert [(r, s, t, i, n) for r, s, t, i, n, _ in events] == [
            (51, 1, "user", 11, "alice"),
            (51, 2, "agent", 41, "Ada"),
            (51, 3, "agent", 41, "Ada"),
            (52, 1, "user", 22, "bob"),
        ]
        assert [json.loads(row[-1])["legacy_message_id"] for row in events] == [61, 62, 63, 64]
        assert db.execute("SELECT room_id,agent_id FROM room_memberships").fetchall() == [(51, 41), (52, 41)]
        db.execute("INSERT INTO messages (conversation_id,role,content) VALUES (51,'user','future')")
        assert db.execute("SELECT last_insert_rowid()").fetchone()[0] > 64
    backups = list((tmp_path / "appdata" / "backups").glob("*.sqlite"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert {table: rows for table, rows in snapshot(db).items() if table not in {"agents", "projects"}} == {table: rows for table, rows in before.items() if table not in {"agents", "projects"}}
        assert project_rows(db) == before_projects
        assert db.execute("SELECT id,user_id,name,description,system_prompt,model,temperature,max_tokens,is_active,created_at,updated_at FROM agents").fetchall() == before["agents"]
        assert "room_events" not in {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert backups[0].stat().st_mode & 0o077 == 0


def test_empty_database_initializes_all_tables(tmp_path, monkeypatch):
    path = tmp_path / "empty.sqlite"
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    engine = create_engine(f"sqlite:///{path}")
    migrate_database(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert set(LEGACY_TABLES) <= tables
        assert {"room_events", "room_memberships", "room_agent_runs", "room_grants", "tool_actions", "desktop_lease", "provider_connections", "room_triggers", "room_schedule_settings", "remembered_login", "room_drafts", "room_read_state", "project_room_templates", "project_room_template_members", "room_launch_requests", "room_pins", "connection_test_history", "request_retry_records", "starter_requests", "schema_version"} <= tables
        assert {"provider_id", "protocol_id", "auth_method_id", "public_config_version",
                "public_config", "auth_grant_revision", "credential_state"} <= {
            row[1] for row in db.execute("PRAGMA table_info('provider_connections')")}
        assert db.execute("SELECT version FROM schema_version ORDER BY version DESC").fetchone() == (len(REVISIONS),)


def test_revision_twenty_preserves_history_unknown_metering_and_adds_starter_storage(tmp_path, monkeypatch):
    from app.core import migrate as migration
    path = tmp_path / "revision-nineteen.sqlite"
    legacy_db(path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    engine = create_engine(f"sqlite:///{path}")
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:19])
        migration.migrate_database(engine)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO provider_connections (id,user_id,kind,name,default_model_id,enabled,auth_required,capabilities,readiness_revision) VALUES (80,11,'openai','Ready','m',1,0,'{}',1)")
        db.execute("INSERT INTO connection_test_history (owner_id,connection_id,config_revision,config_fingerprint,model_id,ok,outcome_code,credential_observation,duration_ms) VALUES (11,80,1,?, 'm',1,'connected','not_required',1)", ('a' * 64,))
        db.commit()
    migration.migrate_database(engine); engine.dispose()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT profile,actual_request_count FROM connection_test_history").fetchone() == (None, None)
        assert {row[1] for row in db.execute("PRAGMA table_info('starter_requests')")} >= {
            'owner_id', 'idempotency_key', 'project_id', 'room_id', 'proposer_agent_id', 'reviewer_agent_id'}
        assert {row[1] for row in db.execute("PRAGMA table_info('request_finalize_records')")} >= {
            'owner_id', 'idempotency_key', 'request_digest', 'plan_hash', 'room_id',
            'source_request_event_id', 'source_outcome_event_ids', 'finalize_event_id',
            'finalize_seq', 'agent_id', 'execution_binding_digest', 'projection_fingerprint'}
        assert db.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone() == (len(migration.REVISIONS),)


def test_revision_twenty_one_upgrades_exact_revision_twenty(tmp_path, monkeypatch):
    from app.core import migrate as migration
    path = tmp_path / "revision-twenty.sqlite"
    legacy_db(path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata-21"))
    engine = create_engine(f"sqlite:///{path}")
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:20])
        migration.migrate_database(engine)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT max(version) FROM schema_version").fetchone() == (20,)
        assert db.execute("SELECT name FROM sqlite_master WHERE name='request_finalize_records'").fetchone() is None
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:21])
        migration.migrate_database(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT max(version) FROM schema_version").fetchone() == (21,)
        assert {row[1] for row in db.execute("PRAGMA table_info('request_finalize_records')")} >= {
            "owner_id", "idempotency_key", "source_request_event_id", "source_outcome_event_ids",
            "finalize_event_id", "finalize_seq", "agent_id", "execution_binding_digest",
            "projection_fingerprint",
        }


def test_revision_fourteen_preserves_existing_rows_and_enforces_template_ownership(tmp_path, monkeypatch):
    from app.core import migrate as migration
    path = tmp_path / "revision-thirteen.sqlite"
    legacy_db(path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    engine = create_engine(f"sqlite:///{path}")
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:13])
        migration.migrate_database(engine)
    with sqlite3.connect(path) as db:
        before = snapshot(db)
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:14])
        migration.migrate_database(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        assert snapshot(db) == before
        db.execute("""INSERT INTO project_room_templates
            (id,owner_id,project_id,name,room_title,max_rounds,max_tool_calls,max_executor_seconds)
            VALUES (81,11,31,'Release','Release room',2,8,60)""")
        db.execute("INSERT INTO project_room_template_members (template_id,agent_id) VALUES (81,41)")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("""INSERT INTO project_room_templates
                (id,owner_id,project_id,name,room_title,max_rounds,max_tool_calls,max_executor_seconds)
                VALUES (82,22,31,'Foreign','Foreign room',2,8,60)""")
        assert db.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone() == (14,)


def test_revision_fifteen_preserves_projects_and_adds_replay_safe_archive_metadata(tmp_path, monkeypatch):
    from app.core import migrate as migration
    path = tmp_path / "revision-fourteen.sqlite"
    legacy_db(path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    engine = create_engine(f"sqlite:///{path}")
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:14])
        migration.migrate_database(engine)
    with sqlite3.connect(path) as db:
        before = db.execute("SELECT id,user_id,name,description,is_active,created_at,updated_at FROM projects").fetchall()
        assert {row[1] for row in db.execute('PRAGMA table_info("projects")')} == {
            "id", "user_id", "name", "description", "is_active", "created_at", "updated_at"}
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:15])
        migration.migrate_database(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT id,user_id,name,description,is_active,created_at,updated_at FROM projects").fetchall() == before
        assert {"archived_at", "archive_plan_hash", "archive_result"} <= {
            row[1] for row in db.execute('PRAGMA table_info("projects")')}
        db.execute("UPDATE projects SET archived_at='2026-10-05 00:00:00', archive_plan_hash=?, archive_result=? WHERE id=31",
                   ("a" * 64, '{"counts":{"active_rooms":1}}'))
        assert db.execute("SELECT archive_plan_hash FROM projects WHERE id=31").fetchone() == ("a" * 64,)
        assert db.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone() == (15,)


def test_revision_sixteen_preserves_existing_rows_and_scopes_launch_keys_to_owner(tmp_path, monkeypatch):
    from app.core import migrate as migration
    path = tmp_path / "revision-fifteen.sqlite"
    legacy_db(path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    engine = create_engine(f"sqlite:///{path}")
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:15])
        migration.migrate_database(engine)
    with sqlite3.connect(path) as db:
        before = snapshot(db)
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:16])
        migration.migrate_database(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        after = snapshot(db)
        assert {table: rows for table, rows in after.items() if table != "room_launch_requests"} == before
        db.execute("""INSERT INTO room_launch_requests
            (owner_id,idempotency_key,payload_digest,room_id)
            VALUES (11,'same-key',?,51)""", ("a" * 64,))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("""INSERT INTO room_launch_requests
                (owner_id,idempotency_key,payload_digest,room_id)
                VALUES (11,'same-key',?,51)""", ("b" * 64,))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("""INSERT INTO room_launch_requests
                (owner_id,idempotency_key,payload_digest,room_id)
                VALUES (22,'foreign-room',?,51)""", ("c" * 64,))
        assert db.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone() == (16,)


def test_revision_seventeen_preserves_rows_and_scopes_room_pins_to_owner(tmp_path, monkeypatch):
    from app.core import migrate as migration
    path = tmp_path / "revision-sixteen.sqlite"
    legacy_db(path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    engine = create_engine(f"sqlite:///{path}")
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:16])
        migration.migrate_database(engine)
    with sqlite3.connect(path) as db:
        before = snapshot(db)
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:17])
        migration.migrate_database(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        assert snapshot(db) == before
        db.execute("INSERT INTO room_pins (owner_id,room_id) VALUES (11,51)")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO room_pins (owner_id,room_id) VALUES (11,51)")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO room_pins (owner_id,room_id) VALUES (22,51)")
        assert db.execute("SELECT owner_id,room_id FROM room_pins").fetchall() == [(11, 51)]
        assert db.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone() == (17,)


def test_revision_seven_removes_trigger_wide_call_id_unique_index_without_losing_actions(tmp_path, monkeypatch):
    from app.core import migrate as migration
    path = tmp_path / "revision-six.sqlite"
    legacy_db(path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    engine = create_engine(f"sqlite:///{path}")
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:6])
        migration.migrate_database(engine)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO tool_actions (action_id,room_id,agent_id,trigger_event_id,call_id,family,arguments,state) "
                   "VALUES ('action-one',51,41,'trigger-one','call-1','read_file','{}','completed')")
    migration.migrate_database(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO tool_actions (action_id,room_id,agent_id,trigger_event_id,call_id,family,arguments,state) "
                   "VALUES ('action-two',51,41,'trigger-one','call-1','read_file','{}','completed')")
        assert db.execute("SELECT action_id FROM tool_actions ORDER BY action_id").fetchall() == [
            ("action-one",), ("action-two",)]
        assert db.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone() == (len(REVISIONS),)


def test_failure_rolls_back_ddl_dml_and_keeps_recoverable_backup(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite"
    legacy_db(path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    with sqlite3.connect(path) as db:
        before = snapshot(db)
        before_projects = project_rows(db)

    import importlib
    revision = importlib.import_module("app.core.migrations.0001_collaboration")

    def fail_halfway(db):
        db.execute("CREATE TABLE partial_upgrade (id INTEGER)")
        db.execute("INSERT INTO users (id,email,username,hashed_password) VALUES (99,'failure@x','failure','hash')")
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(revision, "upgrade", fail_halfway)
    engine = create_engine(f"sqlite:///{path}")
    with pytest.raises(RuntimeError, match="injected migration failure"):
        migrate_database(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        assert snapshot(db) == before
        assert "partial_upgrade" not in {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "schema_version" not in {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    backups = list((tmp_path / "appdata" / "backups").glob("*.sqlite"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert snapshot(db) == before


def test_constraints_keep_events_and_runs_in_their_rooms(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite"
    legacy_db(path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    engine = create_engine(f"sqlite:///{path}")
    migrate_database(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    engine.dispose()
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO room_events (event_id,room_id,seq,actor_type,actor_name_snapshot,kind,payload,created_at) VALUES ('x',999,1,'user','alice','message','{}','2024-01-01')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO room_events (event_id,room_id,seq,actor_type,actor_name_snapshot,kind,payload,created_at) VALUES ('x',51,1,'user','alice','message','{}','2024-01-01')")
        db.execute("INSERT INTO room_agent_runs (room_id,agent_id,state) VALUES (51,41,'idle')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO room_agent_runs (room_id,agent_id,state) VALUES (51,41,'idle')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO room_agent_runs (room_id,agent_id,state) VALUES (999,41,'idle')")


def test_preflight_backup_includes_committed_wal_rows(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite"
    legacy_db(path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    with sqlite3.connect(path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO users (id,email,username,hashed_password) VALUES (23,'wal@x','wal','hash')")
        writer.commit()
        engine = create_engine(f"sqlite:///{path}")
        migrate_database(engine)
        engine.dispose()
        backup = next((tmp_path / "appdata" / "backups").glob("*.sqlite"))
        with sqlite3.connect(backup) as copy:
            assert copy.execute("SELECT username FROM users WHERE id=23").fetchone() == ("wal",)


def test_revision_two_preserves_revision_one_rows_and_memberships(tmp_path, monkeypatch):
    path = tmp_path / "revision-one.sqlite"
    legacy_db(path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    from app.core import migrate as migration_module

    engine = create_engine(f"sqlite:///{path}")
    with monkeypatch.context() as scoped:
        scoped.setattr(migration_module, "REVISIONS", ("0001_collaboration",))
        migration_module.migrate_database(engine)
    with sqlite3.connect(path) as db:
        before = snapshot(db)
        before_projects = project_rows(db)
        old_events = db.execute("SELECT event_id,room_id,seq,payload FROM room_events ORDER BY room_id,seq").fetchall()
        old_members = db.execute("SELECT room_id,agent_id,is_active FROM room_memberships ORDER BY room_id").fetchall()
        db.execute("INSERT INTO room_agent_runs (room_id,agent_id,state,watermark_seq,round) VALUES (51,41,'idle',3,2)")
    migration_module.migrate_database(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        assert {table: rows for table, rows in snapshot(db).items() if table not in {"agents", "projects"}} == {table: rows for table, rows in before.items() if table not in {"agents", "projects"}}
        assert project_rows(db) == before_projects
        assert db.execute("SELECT id,user_id,name,description,system_prompt,model,temperature,max_tokens,is_active,created_at,updated_at FROM agents").fetchall() == before["agents"]
        assert db.execute("SELECT event_id,room_id,seq,payload FROM room_events ORDER BY room_id,seq").fetchall() == old_events
        assert db.execute("SELECT room_id,agent_id,is_active FROM room_memberships ORDER BY room_id").fetchall() == old_members
        assert db.execute("SELECT state,watermark_seq,round,checkpoint,memory,pending_task,paused,generation FROM room_agent_runs WHERE room_id=51 AND agent_id=41").fetchone() == ("idle", 3, 2, "{}", "{}", "{}", 0, 0)
        assert db.execute("SELECT version FROM schema_version ORDER BY version").fetchall() == [(n,) for n in range(1, len(REVISIONS) + 1)]


def test_revision_three_preserves_migrated_events_and_new_writes_follow_cursor(tmp_path, monkeypatch):
    path = tmp_path / "revision-two.sqlite"
    legacy_db(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE users SET is_active=1 WHERE id=11")
        db.execute("UPDATE projects SET is_active=1 WHERE id=31")
        db.execute("UPDATE conversations SET is_active=1 WHERE id=51")
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "test-only-token")
    from app.core import migrate as migration_module
    from app.api.routes.auth import create_access_token

    engine = create_engine(f"sqlite:///{path}")
    with monkeypatch.context() as scoped:
        scoped.setattr(migration_module, "REVISIONS", ("0001_collaboration", "0002_room_state"))
        migration_module.migrate_database(engine)
    with sqlite3.connect(path) as db:
        before = db.execute("SELECT event_id,room_id,seq,actor_name_snapshot,payload,created_at,idempotency_key FROM room_events ORDER BY room_id,seq").fetchall()
    migration_module.migrate_database(engine)
    engine.dispose()
    with TestClient(create_app(database_url=f"sqlite:///{path}")) as client:
        headers = {"X-Gamja-Session": "test-only-token", "Authorization": f"Bearer {create_access_token({'sub': 11})}"}
        result = client.post("/api/v1/conversations/51/events", json={"content": "new"}, headers=headers)
        assert result.status_code == 201, result.text
        assert result.json()["seq"] == 4
        assert result.json()["device_id"] is not None
        history = client.get("/api/v1/conversations/51/events?after_seq=3", headers=headers).json()
        assert [event["event_id"] for event in history] == [result.json()["event_id"]]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT event_id,room_id,seq,actor_name_snapshot,payload,created_at,idempotency_key FROM room_events WHERE seq<=3 ORDER BY room_id,seq").fetchall() == before
        assert db.execute("SELECT device_id FROM room_events WHERE seq<=3").fetchall() == [(None,), (None,), (None,), (None,)]
        assert db.execute("SELECT type FROM pragma_table_info('room_events') WHERE name='device_id'").fetchone() == ("VARCHAR(36)",)


def test_revision_three_rejects_nonnull_remote_device_provenance_without_erasing_it(tmp_path, monkeypatch):
    path = tmp_path / "revision-two.sqlite"
    legacy_db(path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    from app.core import migrate as migration_module

    engine = create_engine(f"sqlite:///{path}")
    with monkeypatch.context() as scoped:
        scoped.setattr(migration_module, "REVISIONS", ("0001_collaboration", "0002_room_state"))
        migration_module.migrate_database(engine)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE room_events SET device_id=71 WHERE seq=1 AND room_id=51")
    with pytest.raises(RuntimeError, match="manual provenance recovery"):
        migration_module.migrate_database(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT device_id FROM room_events WHERE seq=1 AND room_id=51").fetchone() == (71,)
        assert db.execute("SELECT version FROM schema_version ORDER BY version").fetchall() == [(1,), (2,)]


def test_revision_eighteen_preserves_connections_and_owner_scopes_test_history(tmp_path, monkeypatch):
    from app.core import migrate as migration
    path = tmp_path / "revision-seventeen.sqlite"
    legacy_db(path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    engine = create_engine(f"sqlite:///{path}")
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:17])
        migration.migrate_database(engine)
    with sqlite3.connect(path) as db:
        db.execute("""INSERT INTO provider_connections
            (id,user_id,kind,name,default_model_id,enabled,auth_required,capabilities)
            VALUES (81,11,'openai','Existing','exact',1,1,'{\"declared\":{},\"verified\":{}}')""")
        before = db.execute("SELECT id,user_id,kind,name,default_model_id,enabled,auth_required,capabilities FROM provider_connections").fetchall()
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:18])
        migration.migrate_database(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        assert db.execute("SELECT id,user_id,kind,name,default_model_id,enabled,auth_required,capabilities FROM provider_connections").fetchall() == before
        assert db.execute("SELECT readiness_revision FROM provider_connections WHERE id=81").fetchone() == (1,)
        db.execute("""INSERT INTO connection_test_history
            (owner_id,connection_id,config_revision,config_fingerprint,model_id,ok,outcome_code,
             credential_observation,verified_capabilities,duration_ms)
            VALUES (11,81,1,?,'exact',1,'connected','verified','{}',12)""", ("a" * 64,))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("""INSERT INTO connection_test_history
                (owner_id,connection_id,config_revision,config_fingerprint,model_id,ok,outcome_code,
                 credential_observation,duration_ms)
                VALUES (22,81,1,?,'exact',0,'provider_error','unknown',12)""", ("b" * 64,))
        assert db.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone() == (18,)


def test_revision_nineteen_preserves_rows_and_owner_scopes_retry_keys(tmp_path, monkeypatch):
    from app.core import migrate as migration
    path = tmp_path / "revision-eighteen.sqlite"
    legacy_db(path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    engine = create_engine(f"sqlite:///{path}")
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:18])
        migration.migrate_database(engine)
    with sqlite3.connect(path) as db:
        before_events = db.execute("SELECT event_id,room_id,seq,payload FROM room_events ORDER BY room_id,seq").fetchall()
        request, failure, retry = ("10000000-0000-4000-8000-000000000001",
                                   "10000000-0000-4000-8000-000000000002",
                                   "10000000-0000-4000-8000-000000000003")
        for seq, event_id, kind, actor in ((5, request, "message", "user"),
                                           (6, failure, "failure", "agent"),
                                           (7, retry, "message", "user")):
            db.execute("""INSERT INTO room_events
                (event_id,room_id,seq,actor_type,actor_id,actor_name_snapshot,kind,payload,created_at)
                VALUES (?,51,?,?,11,'Owner',?,'{}','2026-10-05')""",
                       (event_id, seq, actor, kind))
        db.commit()
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:19])
        migration.migrate_database(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        assert db.execute("SELECT event_id,room_id,seq,payload FROM room_events WHERE seq < 5 ORDER BY room_id,seq").fetchall() == before_events
        db.execute("""INSERT INTO request_retry_records
            (owner_id,idempotency_key,request_digest,plan_hash,room_id,
             source_failure_event_id,source_trigger_event_id,source_request_event_id,
             retry_event_id,agent_id,retry_seq)
            VALUES (11,'same-key',?,?,51,?,?,?,?,41,7)""",
                   ("a" * 64, "b" * 64, failure, request, request, retry))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("""INSERT INTO request_retry_records
                (owner_id,idempotency_key,request_digest,plan_hash,room_id,
                 source_failure_event_id,source_trigger_event_id,source_request_event_id,
                 retry_event_id,agent_id,retry_seq)
                VALUES (11,'same-key',?,?,51,?,?,?,?,41,8)""",
                       ("c" * 64, "d" * 64, failure, request, request, request))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("""INSERT INTO request_retry_records
                (owner_id,idempotency_key,request_digest,plan_hash,room_id,
                 source_failure_event_id,source_trigger_event_id,source_request_event_id,
                 retry_event_id,agent_id,retry_seq)
                VALUES (22,'foreign',?,?,51,?,?,?,?,41,8)""",
                       ("c" * 64, "d" * 64, failure, request, request, request))
        assert db.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone() == (19,)


def test_revision_twenty_two_preserves_legacy_remembered_login_as_current(tmp_path, monkeypatch):
    from app.core import migrate as migration
    path = tmp_path / "revision-twenty-one.sqlite"
    legacy_db(path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    engine = create_engine(f"sqlite:///{path}")
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:21])
        migration.migrate_database(engine)
    with sqlite3.connect(path) as db:
        db.execute("""INSERT INTO remembered_login (id,owner_id,secret_hash,expires_at)
                    VALUES (1,11,?,'2099-01-01')""", ("a" * 64,))
        db.commit()

    migration.migrate_database(engine)
    engine.dispose()

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT secret_ref,revision FROM remembered_login WHERE id=1").fetchone() == ('current', 1)
        assert db.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone() == (len(migration.REVISIONS),)


def test_revision_twenty_three_preserves_schema_twenty_two_connection_bytes_and_bindings(
        tmp_path, monkeypatch):
    from app.core import migrate as migration
    path = tmp_path / "revision-twenty-two.sqlite"
    legacy_db(path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    engine = create_engine(f"sqlite:///{path}")
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:22])
        migration.migrate_database(engine)
    with sqlite3.connect(path) as db:
        rows = (
            (81, "openai", "OpenAI", None, "synthetic-ref-openai", "gpt-4o", 1, 1,
             '{"declared":{"tools":true},"verified":{}}'),
            (82, "anthropic", "Anthropic", None, "synthetic-ref-anthropic", "claude-3", 1, 1,
             '{"declared":{},"verified":{}}'),
            (83, "gemini", "Gemini", None, "synthetic-ref-gemini", "gemini-2", 1, 1,
             '{"declared":{"vision":true},"verified":{}}'),
            (84, "xai", "xAI", None, "synthetic-ref-xai", "grok-3", 0, 1,
             '{"declared":{},"verified":{}}'),
            (85, "compatible", "Local", "http://127.0.0.1:11434/v1", None, "local-model", 1, 0,
             '{"declared":{"max_tokens":true},"verified":{}}'),
        )
        db.executemany("""INSERT INTO provider_connections
            (id,user_id,kind,name,base_url,secret_ref,default_model_id,enabled,
             auth_required,capabilities,readiness_revision)
            VALUES (?,11,?,?,?,?,?,?,?,?,7)""", rows)
        db.execute("UPDATE agents SET connection_id=81,model_id='gpt-4o' WHERE id=41")
        db.execute("""INSERT INTO connection_test_history
            (id,owner_id,connection_id,config_revision,config_fingerprint,model_id,ok,
             outcome_code,credential_observation,verified_capabilities,duration_ms,
             profile,actual_request_count,tested_at)
            VALUES (91,11,81,7,?,'gpt-4o',1,'connected','verified',?,13,'text',1,
                    '2026-10-07 00:00:00')""",
                   ("a" * 64, '{"tools":false}'))
        db.commit()
        legacy_connections = db.execute("""SELECT
            id,user_id,kind,name,base_url,secret_ref,default_model_id,enabled,
            auth_required,capabilities,readiness_revision,created_at,updated_at
            FROM provider_connections ORDER BY id""").fetchall()
        agent_binding = db.execute(
            "SELECT id,connection_id,model_id FROM agents WHERE id=41").fetchall()
        test_history = db.execute(
            "SELECT * FROM connection_test_history ORDER BY id").fetchall()

    migration.migrate_database(engine)
    engine.dispose()

    with sqlite3.connect(path) as db:
        assert db.execute("""SELECT
            id,user_id,kind,name,base_url,secret_ref,default_model_id,enabled,
            auth_required,capabilities,readiness_revision,created_at,updated_at
            FROM provider_connections ORDER BY id""").fetchall() == legacy_connections
        assert db.execute(
            "SELECT id,connection_id,model_id FROM agents WHERE id=41").fetchall() == agent_binding
        assert db.execute(
            "SELECT * FROM connection_test_history ORDER BY id").fetchall() == test_history
        assert db.execute("""SELECT provider_id,protocol_id,auth_method_id,
            public_config_version,public_config,auth_grant_revision,credential_state
            FROM provider_connections ORDER BY id""").fetchall() == [(None,) * 7] * 5
        assert db.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone() == (
                len(migration.REVISIONS),)


def test_revision_twenty_four_is_additive_owner_scoped_and_cascades_cache(tmp_path, monkeypatch):
    from app.core import migrate as migration
    path = tmp_path / "revision-twenty-three.sqlite"
    legacy_db(path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    engine = create_engine(f"sqlite:///{path}")
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:23])
        migration.migrate_database(engine)
    with sqlite3.connect(path) as db:
        db.execute("""INSERT INTO provider_connections
            (id,user_id,kind,name,base_url,secret_ref,default_model_id,enabled,
             auth_required,capabilities,readiness_revision,provider_id,protocol_id,
             auth_method_id,public_config_version,public_config,auth_grant_revision,credential_state)
            VALUES (81,11,'compatible','OpenRouter',NULL,'synthetic-ref','openai/gpt-5',1,
                    1,'{"declared":{},"verified":{}}',7,'openrouter',
                    'openai_chat_completions','api_key',1,'{}',3,'present')""")
        db.execute("UPDATE agents SET connection_id=81,model_id='openai/gpt-5' WHERE id=41")
        db.execute("""INSERT INTO connection_test_history
            (id,owner_id,connection_id,config_revision,config_fingerprint,model_id,ok,
             outcome_code,credential_observation,verified_capabilities,duration_ms,
             profile,actual_request_count,tested_at)
            VALUES (91,11,81,7,?,'openai/gpt-5',1,'connected','verified',?,13,'text',1,
                    '2026-10-07 00:00:00')""", ("b" * 64, '{"tools":false}'))
        db.commit()
        connection = db.execute("SELECT * FROM provider_connections WHERE id=81").fetchone()
        binding = db.execute("SELECT id,connection_id,model_id FROM agents WHERE id=41").fetchone()
        history = db.execute("SELECT * FROM connection_test_history WHERE id=91").fetchone()

    migration.migrate_database(engine)
    engine.dispose()

    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        assert db.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone() == (
                len(migration.REVISIONS),)
        assert db.execute("SELECT * FROM provider_connections WHERE id=81").fetchone() == connection
        assert db.execute("SELECT id,connection_id,model_id FROM agents WHERE id=41").fetchone() == binding
        assert db.execute("SELECT * FROM connection_test_history WHERE id=91").fetchone() == history
        assert db.execute("SELECT * FROM connection_model_cache").fetchall() == []
        columns = {row[1] for row in db.execute("PRAGMA table_info(connection_model_cache)")}
        assert {"owner_id", "connection_id", "config_revision", "auth_grant_revision",
                "config_fingerprint", "observed_at", "truncated", "models", "updated_at"} <= columns
        foreign_keys = db.execute("PRAGMA foreign_key_list(connection_model_cache)").fetchall()
        assert {(row[2], row[3], row[4], row[6]) for row in foreign_keys} == {
            ("provider_connections", "connection_id", "id", "CASCADE"),
            ("provider_connections", "owner_id", "user_id", "CASCADE"),
        }
        db.execute("""INSERT INTO connection_model_cache
            (owner_id,connection_id,config_revision,auth_grant_revision,config_fingerprint,
             observed_at,truncated,models) VALUES (11,81,7,3,?,NULL,0,'[]')""", ("c" * 64,))
        db.execute("UPDATE agents SET connection_id=NULL WHERE id=41")
        db.execute("DELETE FROM connection_test_history WHERE id=91")
        db.execute("DELETE FROM provider_connections WHERE id=81")
        assert db.execute("SELECT * FROM connection_model_cache").fetchall() == []


def test_revision_twenty_four_failure_is_atomic(tmp_path, monkeypatch):
    from app.core import migrate as migration
    path = tmp_path / "revision-twenty-four-atomic.sqlite"
    legacy_db(path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    engine = create_engine(f"sqlite:///{path}")
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:23])
        migration.migrate_database(engine)
    revision = __import__("app.core.migrations.0024_connection_model_cache", fromlist=["upgrade"])
    original = revision.upgrade

    def fail_after_create(db):
        original(db)
        raise RuntimeError("synthetic migration failure")

    monkeypatch.setattr(revision, "upgrade", fail_after_create)
    with pytest.raises(RuntimeError, match="synthetic migration failure"):
        migration.migrate_database(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT MAX(version) FROM schema_version").fetchone() == (23,)
        assert db.execute("SELECT name FROM sqlite_master WHERE name='connection_model_cache'").fetchone() is None


@pytest.mark.parametrize("damage", ["marker_only", "missing_room_events"])
def test_current_version_with_incomplete_schema_never_reaches_readiness(tmp_path, monkeypatch, damage):
    path = tmp_path / "incomplete.sqlite"
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "test-only-token")
    if damage == "missing_room_events":
        engine = create_engine(f"sqlite:///{path}")
        migrate_database(engine)
        engine.dispose()
        with sqlite3.connect(path) as db:
            db.execute("DROP TABLE room_events")
    else:
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
            db.execute("INSERT INTO schema_version (version) VALUES (1)")

    with pytest.raises(RuntimeError, match="Incomplete schema"):
        with TestClient(create_app(database_url=f"sqlite:///{path}"), headers={"X-Gamja-Session": "test-only-token"}):
            pass
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == ([(n,) for n in range(1, len(REVISIONS) + 1)] if damage == "missing_room_events" else [(1,)])
