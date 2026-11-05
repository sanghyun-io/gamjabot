"""Disposable source migration/constraints only, never the installed schema26 DB."""

import sqlite3
from sqlalchemy import create_engine
import pytest

from app.core import migrate as migration
from test_migrations import legacy_db


@pytest.mark.parametrize("start",[25,26])
def test_schema27_preserves_all_old_rows_definitions_and_repeat_bytes(tmp_path,monkeypatch,start):
    path=tmp_path/"old.sqlite";legacy_db(path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR",str(tmp_path/"owned-appdata"))
    engine=create_engine(f"sqlite:///{path}")
    with monkeypatch.context() as scoped:
        scoped.setattr(migration,"REVISIONS",migration.REVISIONS[:start]);migration.migrate_database(engine)
    with sqlite3.connect(path) as db:
        tables={r[0]:r[1] for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name!='schema_version'")}
        data={t:db.execute(f'SELECT * FROM "{t}" ORDER BY rowid').fetchall() for t in tables}
        columns={t:[r[1] for r in db.execute(f'PRAGMA table_info("{t}")')] for t in tables}
    migration.migrate_database(engine)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT max(version) FROM schema_version").fetchone()[0]==27
        assert db.execute("PRAGMA foreign_key_check").fetchall()==[]
        for t,sql in tables.items():
            if start==25 and t=="tool_actions":
                assert [r[1] for r in db.execute('PRAGMA table_info("tool_actions")')][len(columns[t]):]==["review_id","review_snapshot","approved_at","approval_owner_id"]
            else:
                assert db.execute("SELECT sql FROM sqlite_master WHERE name=?",(t,)).fetchone()[0]==sql
            # Revision26 adds nullable tool columns when starting at25; compare original columns.
            assert db.execute(f'SELECT '+','.join('"'+x+'"' for x in columns[t])+f' FROM "{t}" ORDER BY rowid').fetchall()==data[t]
    engine.dispose();before=path.read_bytes();migration.migrate_database(engine);engine.dispose();assert path.read_bytes()==before


def test_schema27_requires_its_unique_source_index(tmp_path,monkeypatch):
    path=tmp_path/"new.sqlite";legacy_db(path);monkeypatch.setenv("GAMJA_APP_DATA_DIR",str(tmp_path/"owned"));engine=create_engine(f"sqlite:///{path}")
    migration.migrate_database(engine)
    with sqlite3.connect(path) as db:db.execute("DROP INDEX ux_room_result_source")
    with pytest.raises(RuntimeError,match="ux_room_result_source"):migration.migrate_database(engine)
    engine.dispose()
