"""Schema 69 migration uses only synthetic SQLite under pytest tmp_path."""
import sqlite3

import pytest
from sqlalchemy import create_engine

from app.core import migrate as migration
from app.models.teammate import TeammateDeliveryReceipt
from test_migrations import legacy_db


def test_revision_69_preserves_old_events_and_does_not_invent_receipts(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite"
    legacy_db(path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    engine = create_engine(f"sqlite:///{path}")
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:68])
        migration.migrate_database(engine)
    with sqlite3.connect(path) as db:
        before = db.execute("SELECT event_id, room_id, seq, payload FROM room_events ORDER BY room_id,seq").fetchall()
        assert before
        assert db.execute("SELECT MAX(version) FROM schema_version").fetchone() == (68,)
    migration.migrate_database(engine)
    migration.migrate_database(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT event_id, room_id, seq, payload FROM room_events ORDER BY room_id,seq").fetchall() == before
        assert db.execute("SELECT MAX(version) FROM schema_version").fetchone() == (69,)
        assert db.execute("SELECT COUNT(*) FROM teammate_delivery_receipts").fetchone() == (0,)
        db.execute("PRAGMA foreign_keys=ON")
        event_id = before[0][0]
        insert = """INSERT INTO teammate_delivery_receipts
          (receipt_id,owner_id,root_room_id,root_request_event_id,execution_room_id,agent_id,
           generation,job_id,job_kind,phase,invocation_ordinal,state,sources_json)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        args = ("r1",11,51,event_id,51,41,0,"job1","retry","response",1,"attempted","[]")
        db.execute(insert,args)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(insert,("r2",*args[1:]))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(insert,("r3",11,51,event_id,51,41,0,"job2","retry","response",1,"acknowledged","[]"))
        db.execute("UPDATE teammate_delivery_receipts SET state='acknowledged', acknowledged_at=CURRENT_TIMESTAMP WHERE receipt_id='r1'")
        assert db.execute("SELECT state,sources_json,acknowledged_at IS NOT NULL FROM teammate_delivery_receipts WHERE receipt_id='r1'").fetchone() == ("acknowledged","[]",1)
        assert {c.name for c in TeammateDeliveryReceipt.__table__.columns} == {row[1] for row in db.execute("PRAGMA table_info(teammate_delivery_receipts)")}
