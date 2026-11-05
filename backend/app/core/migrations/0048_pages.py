"""Add declarative Page versions while retaining every existing result/review."""
import re

VERSION = 48
REQUIRED_COLUMNS = {'room_result_versions': {'result_id','version','room_id','kind','title','source_event_id','source_seq','content'},
                    'room_result_reviews': {'review_id','result_id','version','room_id','source_event_id'}}
REQUIRED_UNIQUE_INDEXES = {'ux_room_result_source': ('room_result_versions', ('room_id','source_event_id','kind','selector'))}


def upgrade(db):
    tables = {}
    objects = []
    for name in ('room_result_versions', 'room_result_reviews'):
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        if not row or not row[0]: raise RuntimeError('page migration missing result table')
        tables[name] = row[0]
        objects.extend(row[0] for row in db.execute("SELECT sql FROM sqlite_master WHERE type IN ('index','trigger') AND tbl_name=? AND sql IS NOT NULL ORDER BY type,name", (name,)).fetchall())
        db.execute(f'CREATE TEMP TABLE saved_{name}_48 AS SELECT * FROM {name}')
    version_sql, count = re.subn(r"kind\s+IN\s*\(\s*'text'\s*,\s*'link'\s*,\s*'file'\s*\)",
        "kind IN ('text','link','file','page')", tables['room_result_versions'], flags=re.IGNORECASE)
    if count != 1: raise RuntimeError('page migration unexpected result kind constraint')
    db.execute('DROP TABLE room_result_reviews')
    db.execute('DROP TABLE room_result_versions')
    db.execute(version_sql)
    db.execute('INSERT INTO room_result_versions SELECT * FROM saved_room_result_versions_48')
    db.execute(tables['room_result_reviews'])
    db.execute('INSERT INTO room_result_reviews SELECT * FROM saved_room_result_reviews_48')
    for sql in objects: db.execute(sql)
    for name in ('room_result_versions', 'room_result_reviews'):
        db.execute(f'DROP TABLE saved_{name}_48')
    if db.execute('PRAGMA foreign_key_check').fetchone(): raise RuntimeError('page migration foreign key check failed')
