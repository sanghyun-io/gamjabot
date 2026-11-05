"""One actual pinned34 -> 35 SQLite preservation and re-entry check."""
from collections import Counter
import json
import re
import sqlite3
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from app.core import migrate as migration


def insert(db,table,values):
    names=','.join('"'+key+'"' for key in values)
    db.execute(f'INSERT INTO "{table}" ({names}) VALUES ({",".join("?" for _ in values)})',tuple(values.values()))


def snapshot(db):
    tables=[name for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    columns={name:[row[1] for row in db.execute(f'PRAGMA table_info("{name}")')] for name in tables}
    return {'columns':columns,
        'rows':{name:db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() for name in tables},
        'ddl':{(kind,name,table):sql for kind,name,table,sql in db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master')}}


def seed34(db):
    """Synthetic opaque review/grant bytes are historical data, never approved execution."""
    stamp='2026-01-01 00:00:00';root,request,delivery,result=[str(uuid4()) for _ in range(4)]
    scope,message,stop=[str(uuid4()) for _ in range(3)]
    insert(db,'users',dict(id=11,email='fixture@example.invalid',username='Synthetic',hashed_password='synthetic-unused',is_active=1))
    insert(db,'projects',dict(id=31,user_id=11,name='Synthetic',is_active=1))
    for aid in (41,42):
        insert(db,'agents',dict(id=aid,user_id=11,name=f'Bot{aid}',description='synthetic',system_prompt='synthetic',
            model='synthetic-model',temperature='0.7',max_tokens=20,is_active=1,
            avatar=' { "version": 1, "head": 1, "hair": 2, "eyes": 3, "nose": 4, "mouth": 5, "skin": 6, "accent": 7 } '))
        insert(db,'project_agents',dict(project_id=31,agent_id=aid))
    for room,agent in ((51,41),(52,42)):
        insert(db,'conversations',dict(id=room,user_id=11,project_id=31,title=f'Room{room}',is_active=1))
        insert(db,'room_memberships',dict(room_id=room,agent_id=agent,is_active=1))
        insert(db,'room_agent_runs',dict(room_id=room,agent_id=agent,state='idle',generation=7,
            trigger_event_id=root,memory='{"legacy":"preserve"}',checkpoint='{"synthetic":true}'))
        insert(db,'bot_conversations',dict(owner_id=11,agent_id=agent,room_id=room,created_at=stamp,updated_at=stamp))
    insert(db,'bot_owner_profiles',dict(owner_id=11,primary_agent_id=41,revision=3,personal_project_id=31))
    for event,room,seq,actor,aid,kind,body in ((root,51,1,'user',11,'message','original request'),
            (request,51,2,'agent',41,'message','explicit task'),(delivery,52,1,'system',None,'activity','delivery'),
            (result,52,2,'agent',42,'message','actual result')):
        insert(db,'room_events',dict(event_id=event,room_id=room,seq=seq,actor_type=actor,actor_id=aid,
            actor_name_snapshot='Synthetic',kind=kind,payload=json.dumps({'content':body}),created_at=stamp))
    insert(db,'room_triggers',dict(event_id=root,room_id=51,state='done',selected_agents='[41]'))
    insert(db,'bot_memory_state',dict(owner_id=11,agent_id=41,revision=2))
    insert(db,'bot_memory_entries',dict(memory_id=str(uuid4()),owner_id=11,agent_id=41,category='preference',
        content='synthetic private preference',source_kind='manual',source_snapshot=' { "kind": "manual" } ',
        revision=2,created_by=11,updated_by=11,created_at=stamp,updated_at=stamp))
    insert(db,'execution_scopes',dict(scope_id=scope,root_request_event_id=root,owner_id=11,root_room_id=51,
        kind='branch',execution_room_id=51,agent_id=41,epoch=1,state='revoked',deadline_at=stamp,
        revocation_state='unknown',native_calls=2,max_native_calls=4,executor_used_ms=50,executor_reserved_ms=10,max_executor_ms=500))
    insert(db,'scope_stop_receipts',dict(stop_id=stop,owner_id=11,room_id=51,
        revocations=json.dumps([{'scope_id':scope,'epoch':1}]),
        receipts=json.dumps([{'scope_id':scope,'epoch':1,'state':'unknown','code':'native_scope_revocation_unconfirmed'}]),
        state='unknown',created_at=stamp))
    proof={'version':1,'root_scope_id':scope,'scope_id':scope,'owner_id':11,'root_room_id':51,
        'root_request_event_id':root,'execution_room_id':51,'agent_id':41,'run_generation':7,
        'job_id':None,'phase':None,'context_mode':'user_room','deadline_at':'2026-01-01T00:00:00+00:00',
        'lineage':[{'scope_id':scope,'epoch':0}]}
    for version in (1,2):
        action,review=str(uuid4()),str(uuid4())
        grant_scope={'kind':'once','action_id':action,'approval_review_id':review,'session_id':str(uuid4()),
            'families':['write_file'],'targets':['/synthetic-unused'],'acknowledge_unverified_context':True}
        insert(db,'room_grants',dict(id=version,room_id=51,agent_id=41,scope=json.dumps(grant_scope,indent=1),decision='consumed'))
        review_body={'version':version,'action':{'action_id':action},'historical_opaque_fixture':'preserve exactly'}
        if version==2:review_body['action']['execution_binding']=proof
        insert(db,'tool_actions',dict(action_id=action,room_id=51,agent_id=41,grant_id=version,
            trigger_event_id=root,family='write_file',arguments='{}',state='completed',review_id=review,
            review_snapshot=json.dumps(review_body,indent=2),approval_scope='once',approval_owner_id=11,
            approved_at=stamp,started_at=stamp,completed_at=stamp,
            execution_binding=json.dumps(proof,indent=1) if version==2 else None,executor_reservation_ms=10 if version==2 else None))
    common=dict(owner_id=11,root_request_event_id=root,sender_agent_id=41,recipient_agent_id=42,
        sender_name='Bot41',recipient_name='Bot42',content='preserved collaboration',delivery_key='synthetic',
        state='completed',reply_state='delivered',depth=1,request_event_id=request,result_event_id=result,
        created_at=stamp,updated_at=stamp)
    insert(db,'teammate_handoffs',dict(common,handoff_id=str(uuid4()),room_id=51))
    insert(db,'teammate_budgets',dict(root_request_event_id=root,provider_calls=2))
    insert(db,'bot_messages',dict(common,message_id=message,root_room_id=51,sender_room_id=51,
        recipient_room_id=52,delivery_event_id=delivery,deadline_at=stamp))
    return dict(scope=scope,root=root,request=request,message=message,stamp=stamp)


def test_actual_pinned34_to35_preserves_all_old_rows_ddl_reviews_grants_and_reentry(tmp_path,monkeypatch):
    assert migration.REVISIONS[33]=='0034_execution_scopes' and migration.REVISIONS[34]=='0035_bot_batches'
    path=tmp_path/'pinned34.sqlite';data_root=tmp_path/'synthetic-appdata'
    monkeypatch.setenv('GAMJA_APP_DATA_DIR',str(data_root))
    def migrate_to(version):
        engine=create_engine(f'sqlite:///{path}')
        try:
            with monkeypatch.context() as pinned:
                pinned.setattr(migration,'REVISIONS',migration.REVISIONS[:version])
                migration.migrate_database(engine)
        finally:engine.dispose()
    migrate_to(34)  # Actual migration runner, explicitly pinned before seeding.
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA foreign_keys=ON');ids=seed34(db);db.commit()
        assert db.execute('SELECT max(version) FROM schema_version').fetchone()==(34,)
        assert 'bot_batches' not in {name for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert 'batch_id' not in {row[1] for row in db.execute('PRAGMA table_info(bot_messages)')}
        before=snapshot(db);old_info=db.execute('PRAGMA table_info(bot_messages)').fetchall()
        old_fks=Counter(tuple(row[1:]) for row in db.execute('PRAGMA foreign_key_list(bot_messages)'))
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    backups_before=set((data_root/'backups').glob('*.sqlite'))
    migrate_to(35)
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA foreign_keys=ON')
        assert db.execute('SELECT version FROM schema_version ORDER BY version').fetchall()==[(v,) for v in range(1,36)]
        for table,cols in before['columns'].items():
            fields=','.join('"'+col+'"' for col in cols)
            suffix=' WHERE version<=34' if table=='schema_version' else ''
            assert db.execute(f'SELECT {fields} FROM "{table}"{suffix} ORDER BY rowid').fetchall()==before['rows'][table],table
        after=snapshot(db)
        for key,sql in before['ddl'].items():
            if key==('table','bot_messages','bot_messages'):
                sql35=re.sub(r',\s*batch_id VARCHAR\(36\) REFERENCES bot_batches\(batch_id\)','',after['ddl'][key],count=1)
                sql35=re.sub(r',\s*batch_index INTEGER','',sql35,count=1)
                assert sql35==sql
            else:assert after['ddl'][key]==sql,key
        assert db.execute('PRAGMA table_info(bot_messages)').fetchall()[:-2]==old_info
        assert db.execute('SELECT batch_id,batch_index FROM bot_messages').fetchall()==[(None,None)]
        assert db.execute('SELECT count(*) FROM bot_batches').fetchone()==(0,)
        new_fks=Counter(tuple(row[1:]) for row in db.execute('PRAGMA foreign_key_list(bot_messages)'))
        assert new_fks-old_fks==Counter({(0,'bot_batches','batch_id','batch_id','NO ACTION','NO ACTION','NONE'):1})
        assert not old_fks-new_fks
        batch_fks={(row[3],row[2],row[4]) for row in db.execute('PRAGMA foreign_key_list(bot_batches)')}
        assert batch_fks=={('owner_id','users','id'),('root_room_id','conversations','id'),
            ('root_request_event_id','room_events','event_id'),('sender_room_id','conversations','id'),
            ('sender_agent_id','agents','id'),('source_scope_id','execution_scopes','scope_id'),
            ('request_event_id','room_events','event_id'),('return_delivery_event_id','room_events','event_id'),
            ('return_event_id','room_events','event_id')}
        index=next(row for row in db.execute('PRAGMA index_list(bot_messages)') if row[1]=='ux_bot_batch_index')
        assert index[2:3]==(1,) and index[4]==1
        assert [row[2] for row in db.execute('PRAGMA index_info(ux_bot_batch_index)')]==['batch_id','batch_index']
        assert [row[2] for row in db.execute('PRAGMA index_info(ix_bot_batch_root)')]==['root_request_event_id','reply_state']
        assert any(row[2] and [col[2] for col in db.execute(f'PRAGMA index_info("{row[1]}")')]==['root_request_event_id','delivery_key']
                   for row in db.execute('PRAGMA index_list(bot_batches)'))
        db.execute('SAVEPOINT constraints')
        try:
            with pytest.raises(sqlite3.IntegrityError):db.execute('UPDATE bot_messages SET batch_id=?',(str(uuid4()),))
            batch=str(uuid4())
            insert(db,'bot_batches',dict(batch_id=batch,owner_id=11,root_room_id=51,root_request_event_id=ids['root'],
                sender_room_id=51,sender_agent_id=41,sender_name='Bot41',source_scope_id=ids['scope'],
                content='synthetic batch',delivery_key='new-batch',state='pending',reply_state='none',
                request_event_id=ids['request'],deadline_at=ids['stamp'],created_at=ids['stamp'],updated_at=ids['stamp']))
            db.execute('UPDATE bot_messages SET batch_id=?,batch_index=0',(batch,))
            cols=[row[1] for row in db.execute('PRAGMA table_info(bot_messages)')]
            duplicate=dict(zip(cols,db.execute('SELECT * FROM bot_messages').fetchone()))
            duplicate.update(message_id=str(uuid4()),delivery_key='another-member')
            with pytest.raises(sqlite3.IntegrityError):insert(db,'bot_messages',duplicate)
            with pytest.raises(sqlite3.IntegrityError):db.execute("UPDATE bot_batches SET parent_kind='forged'")
        finally:db.execute('ROLLBACK TO constraints');db.execute('RELEASE constraints')
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
        assert db.execute('PRAGMA integrity_check').fetchone()==('ok',)
        stable=snapshot(db)
    backups_after=set((data_root/'backups').glob('*.sqlite'))
    assert len(backups_after-backups_before)==1
    with sqlite3.connect(next(iter(backups_after-backups_before))) as db:assert snapshot(db)==before
    first_bytes=path.read_bytes();migrate_to(35)
    with sqlite3.connect(path) as db:assert snapshot(db)==stable
    assert path.read_bytes()==first_bytes and set((data_root/'backups').glob('*.sqlite'))==backups_after
