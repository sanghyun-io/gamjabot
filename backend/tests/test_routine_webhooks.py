"""Synthetic webhook authority, idempotency and schema migration coverage."""
import importlib,sqlite3
from test_room_state import room_client,headers
from test_bot_creation import private_team
from app.models.owner_routines import OwnerRoutineWebhookKey,OwnerRoutineRun
from app.services import owner_routines as routines


def test_webhook_key_claim_and_replay(room_client,monkeypatch):
    client,(owner,other,project,_,_)=room_client
    bot,room,_=private_team(client,owner,project);own=headers(client,owner)
    base=f'/api/v1/bots/{bot}/routines'
    definition={'name':'Synthetic webhook','instruction':'Summarize the event','enabled':True,'schedule':{'kind':'webhook'}}
    row=client.post(base,headers={**own,'Idempotency-Key':'create-webhook'},json=definition).json()['routine']
    rid=row['routine_id'];path=f'{base}/{rid}';key_path=path+'/webhook-key'
    assert row['next_run_at'] is None and row['webhook']=={'configured':False,'generation':0}
    assert client.post(key_path,headers=headers(client,other),json={'expected_revision':1,'expected_generation':0}).status_code==404
    issued=client.post(key_path,headers=own,json={'expected_revision':1,'expected_generation':0}).json()
    assert issued['generation']==1 and issued['revision']==1 and len(issued['key'])==46
    assert client.post(key_path,headers=own,json={'expected_revision':1,'expected_generation':0}).status_code==409
    assert client.get(path,headers=own).json()['webhook']=={'configured':True,'generation':1}
    with client.app.state.session_factory() as db:
        assert db.get(OwnerRoutineWebhookKey,rid).key_hash!=issued['key']
    starts=[];monkeypatch.setattr(client.app.state.scheduler,'start_routine',lambda run_id:starts.append(run_id))
    sender={'Authorization':'Bearer '+issued['key'],'Idempotency-Key':'event-1'}
    assert client.post(issued['url_path'],headers={'Idempotency-Key':'event-1'},json={}).status_code==401
    assert client.get(base,headers=sender).status_code==401
    assert client.post(issued['url_path'],headers={**sender,'Origin':'https://untrusted.example'},json={}).status_code==403
    assert client.post(issued['url_path'],headers=sender,content=b'{' ).status_code==422
    assert client.post(issued['url_path'],headers=sender,content=b'x'*16385).status_code==413
    assert client.post(issued['url_path'],headers=sender,json={'secret':issued['key']}).status_code==422
    result=client.post(issued['url_path'],headers=sender,json={'example':'value'}).json()
    assert result['accepted'] and result['state']=='running' and not result['replayed']
    assert client.post(issued['url_path'],headers=sender,json={'example':'value'}).json()['run_id']==result['run_id']
    assert starts==[result['run_id']]
    assert client.post(issued['url_path'],headers=sender,json={'example':'changed'}).status_code==409
    assert client.get(path+'/webhook-events/event-1',headers=own).json()['run_id']==result['run_id']
    with client.app.state.session_factory() as db:
        from app.models.collaboration import RoomEvent
        run=db.get(OwnerRoutineRun,result['run_id']);assert run.origin=='webhook' and routines.current(db,run)
        root=db.get(RoomEvent,run.root_event_id)
        assert root.payload['content']==definition['instruction'] and root.payload['webhook']['body']=={'example':'value'}
    revoked=client.request('DELETE',key_path,headers=own,json={'expected_revision':1,'expected_generation':1}).json()
    assert revoked['generation']==2 and revoked['revision']==1 and not revoked['configured']
    assert client.post(issued['url_path'],headers=sender,json={'example':'value'}).status_code==401
    with client.app.state.session_factory() as db:assert routines.current(db,db.get(OwnerRoutineRun,result['run_id'])) is None


def test_migration_preserves_rows_and_references():
    db=sqlite3.connect(':memory:');db.execute('PRAGMA foreign_keys=ON')
    for name in ('users','agents','conversations'):db.execute(f'CREATE TABLE {name}(id INTEGER PRIMARY KEY)')
    db.execute('CREATE TABLE room_events(event_id VARCHAR(36) PRIMARY KEY)')
    for name in ('users','agents','conversations'):db.execute(f'INSERT INTO {name} VALUES (1)')
    db.execute("INSERT INTO room_events VALUES ('root')")
    importlib.import_module('app.core.migrations.0040_owner_routines').upgrade(db)
    db.execute("INSERT INTO owner_routines VALUES ('r',1,1,1,1,'n','i','{}',1,'f',NULL,'t','t',NULL)")
    for name,origin in [('a','scheduled'),('b','test')]:
        db.execute("INSERT INTO owner_routine_runs VALUES (?, 'r',1,1,1,1,1,?,'t','t',NULL,'running','root',NULL,NULL,NULL,NULL,8,60)",(name,origin))
    db.execute("INSERT INTO owner_routine_pending_tests VALUES ('p',1,1,'r',1,1,'root',1,'root','done','b',NULL,'t')")
    before=db.execute('SELECT * FROM owner_routine_runs ORDER BY run_id').fetchall()
    pending=db.execute('SELECT * FROM owner_routine_pending_tests').fetchall()
    fk=db.execute('PRAGMA foreign_key_list(owner_routine_pending_tests)').fetchall()
    importlib.import_module('app.core.migrations.0045_routine_webhooks').upgrade(db)
    assert db.execute('SELECT * FROM owner_routine_runs ORDER BY run_id').fetchall()==before
    assert db.execute('SELECT * FROM owner_routine_pending_tests').fetchall()==pending
    assert db.execute('PRAGMA foreign_key_list(owner_routine_pending_tests)').fetchall()==fk
    assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    db.execute("INSERT INTO owner_routine_runs VALUES ('c','r',1,1,1,1,1,'webhook','u','t',NULL,'running','root',NULL,NULL,NULL,NULL,8,60)")
    db.close()
