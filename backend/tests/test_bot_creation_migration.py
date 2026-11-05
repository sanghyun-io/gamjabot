"""One actual pinned35 -> 36 preservation/constraint/re-entry check."""
import sqlite3
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from app.core import migrate as migration
from test_bot_batch_migration import insert,seed34,snapshot


def seed35(db):
    ids=seed34(db)
    columns=[row[1] for row in db.execute('PRAGMA table_info(agents)')]
    third=dict(zip(columns,db.execute('SELECT * FROM agents WHERE id=42').fetchone()))
    third.update(id=43,name='Bot43');insert(db,'agents',third)
    insert(db,'project_agents',dict(project_id=31,agent_id=43))
    insert(db,'conversations',dict(id=53,user_id=11,project_id=31,title='Third private room',is_active=1))
    insert(db,'room_memberships',dict(room_id=53,agent_id=43,is_active=1))
    insert(db,'room_agent_runs',dict(room_id=53,agent_id=43,state='idle',generation=7,trigger_event_id=ids['root']))
    insert(db,'bot_conversations',dict(owner_id=11,agent_id=43,room_id=53,created_at=ids['stamp'],updated_at=ids['stamp']))
    def event(room,seq,actor,agent,kind,content):
        identity=str(uuid4())
        insert(db,'room_events',dict(event_id=identity,room_id=room,seq=seq,actor_type=actor,actor_id=agent,
            actor_name_snapshot='Synthetic',kind=kind,payload='{"content":"'+content+'"}',created_at=ids['stamp']))
        return identity
    batch=str(uuid4());request=event(51,3,'agent',41,'message','batch request references')
    returned=event(51,6,'agent',41,'message','actual joined result')
    insert(db,'bot_batches',dict(batch_id=batch,owner_id=11,root_room_id=51,root_request_event_id=ids['root'],
        sender_room_id=51,sender_agent_id=41,sender_name='Bot41',source_scope_id=ids['scope'],
        content='synthetic joined collaboration',delivery_key='historical-batch',state='completed',reply_state='delivered',
        request_event_id=request,return_event_id=returned,deadline_at=ids['stamp'],created_at=ids['stamp'],updated_at=ids['stamp']))
    columns=[row[1] for row in db.execute('PRAGMA table_info(bot_messages)')]
    original=dict(zip(columns,db.execute('SELECT * FROM bot_messages WHERE message_id=?',(ids['message'],)).fetchone()))
    for index,(agent,room) in enumerate(((42,52),(43,53))):
        member=dict(original,message_id=str(uuid4()),delivery_key=f'historical-member-{index}',batch_id=batch,batch_index=index,
            request_event_id=event(51,4+index,'agent',41,'message',f'explicit member task {index}'),
            recipient_agent_id=agent,recipient_room_id=room,recipient_name=f'Bot{agent}')
        if room==53:
            member.update(delivery_event_id=event(53,1,'system',None,'activity','delivery'),
                result_event_id=event(53,2,'agent',43,'message','actual third Bot result'))
        insert(db,'bot_messages',member)
    return ids


def test_actual_pinned35_to36_preserves_old_data_authority_backup_and_reentry(tmp_path,monkeypatch):
    assert migration.REVISIONS[34:36]==('0035_bot_batches','0036_bot_creation')
    path=tmp_path/'pinned35.sqlite';data_root=tmp_path/'synthetic-appdata'
    monkeypatch.setenv('GAMJA_APP_DATA_DIR',str(data_root))
    def migrate_to(version):
        engine=create_engine(f'sqlite:///{path}')
        try:
            with monkeypatch.context() as pinned:
                pinned.setattr(migration,'REVISIONS',migration.REVISIONS[:version])
                migration.migrate_database(engine)
        finally:engine.dispose()
    migrate_to(35)  # Never construct the old fixture from the latest ORM/schema.
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA foreign_keys=ON');ids=seed35(db);db.commit()
        assert db.execute('SELECT max(version) FROM schema_version').fetchone()==(35,)
        assert 'bot_creations' not in {name for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert db.execute('SELECT count(*) FROM bot_messages WHERE batch_id IS NULL').fetchone()==(1,)
        assert db.execute('SELECT count(*) FROM bot_messages WHERE batch_id IS NOT NULL').fetchone()==(2,)
        before=snapshot(db);assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    backups_before=set((data_root/'backups').glob('*.sqlite'))
    migrate_to(36)
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA foreign_keys=ON')
        assert db.execute('SELECT version FROM schema_version ORDER BY version').fetchall()==[(v,) for v in range(1,37)]
        for table,columns in before['columns'].items():
            fields=','.join('"'+column+'"' for column in columns)
            suffix=' WHERE version<=35' if table=='schema_version' else ''
            assert db.execute(f'SELECT {fields} FROM "{table}"{suffix} ORDER BY rowid').fetchall()==before['rows'][table],table
        after=snapshot(db)
        assert {key:after['ddl'][key] for key in before['ddl']}==before['ddl']
        assert set(after['columns'])-set(before['columns'])=={'bot_creations'}
        assert db.execute('SELECT count(*) FROM bot_creations').fetchone()==(0,)
        info=db.execute('PRAGMA table_info(bot_creations)').fetchall()
        assert {row[1] for row in info}=={'creation_id','owner_id','root_room_id','root_request_event_id',
            'source_agent_id','source_generation','call_id','payload_digest','source_profile_fingerprint',
            'created_agent_id','created_room_id','request_event_id','result_event_id','response','created_at'}
        assert all(row[3] for row in info)
        foreign={(row[3],row[2],row[4]) for row in db.execute('PRAGMA foreign_key_list(bot_creations)')}
        assert foreign=={('owner_id','users','id'),('root_room_id','conversations','id'),
            ('root_request_event_id','room_events','event_id'),('source_agent_id','agents','id'),
            ('created_agent_id','agents','id'),('created_room_id','conversations','id'),
            ('request_event_id','room_events','event_id'),('result_event_id','room_events','event_id')}
        unique={tuple(col[2] for col in db.execute(f'PRAGMA index_info("{row[1]}")'))
            for row in db.execute('PRAGMA index_list(bot_creations)') if row[2]}
        assert {('creation_id',),('created_agent_id',),('created_room_id',),('root_request_event_id','source_agent_id','call_id')}<=unique
        assert [row[2] for row in db.execute('PRAGMA index_info(ix_bot_creation_root)')]==['root_request_event_id']
        db.execute('SAVEPOINT constraints')
        try:
            created=dict(creation_id=str(uuid4()),owner_id=11,root_room_id=51,root_request_event_id=ids['root'],
                source_agent_id=41,source_generation=7,call_id='synthetic-create',payload_digest='a'*64,
                source_profile_fingerprint='b'*64,created_agent_id=42,created_room_id=52,
                request_event_id=ids['request'],result_event_id=ids['request'],
                response=' { "created_agent_id": 42, "created_room_id": 52 } ',created_at=ids['stamp'])
            insert(db,'bot_creations',created)
            assert db.execute('SELECT response FROM bot_creations').fetchone()==(created['response'],)
            for column,_,target in foreign:
                missing=str(uuid4()) if target=='event_id' else 999999
                with pytest.raises(sqlite3.IntegrityError):db.execute(f'UPDATE bot_creations SET "{column}"=?',(missing,))
            with pytest.raises(sqlite3.IntegrityError):db.execute('UPDATE bot_creations SET source_generation=-1')
            with pytest.raises(sqlite3.IntegrityError):db.execute('UPDATE bot_creations SET creation_id=NULL')
            # Isolate the root/source/call replay key from the two target uniques.
            with pytest.raises(sqlite3.IntegrityError):
                insert(db,'bot_creations',dict(created,creation_id=str(uuid4()),created_agent_id=43,created_room_id=53))
            with pytest.raises(sqlite3.IntegrityError):
                insert(db,'bot_creations',dict(created,creation_id=str(uuid4()),call_id='other-call',created_room_id=53))
            with pytest.raises(sqlite3.IntegrityError):
                insert(db,'bot_creations',dict(created,creation_id=str(uuid4()),call_id='other-call',created_agent_id=43))
        finally:db.execute('ROLLBACK TO constraints');db.execute('RELEASE constraints')
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
        assert db.execute('PRAGMA integrity_check').fetchone()==('ok',)
        stable=snapshot(db)
    backups_after=set((data_root/'backups').glob('*.sqlite'))
    assert len(backups_after-backups_before)==1
    with sqlite3.connect(next(iter(backups_after-backups_before))) as db:assert snapshot(db)==before
    first_bytes=path.read_bytes();migrate_to(36)
    with sqlite3.connect(path) as db:assert snapshot(db)==stable
    assert path.read_bytes()==first_bytes and set((data_root/'backups').glob('*.sqlite'))==backups_after
