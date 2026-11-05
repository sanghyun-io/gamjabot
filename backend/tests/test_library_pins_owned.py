"""Synthetic SQLite and isolated upload store; no installed data or model."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
import hashlib
import pytest
from types import SimpleNamespace

@pytest.fixture(autouse=True)
def owner_epoch(results, monkeypatch):
 c,ids=results
 monkeypatch.setattr(c.app.state.owner_epoch_provider,"current",lambda owner: "a"*64 if owner==ids["owner"] else None)
from test_room_results import results,headers,save,base
from app.services.library_uploads import LibraryUploads
from app.models.models import Conversation

def command(source,revision=0,pinned=True):
 return {'operation_id':str(uuid4()),'source':source,'expected_revision':revision,'pinned':pinned,'expected_owner_epoch':'a'*64}

def test_exact_result_pin_cas_fence_and_scope(results):
 c,ids=results; first=save(c,ids).json(); source={'kind':'result','room_id':ids['room'],'result_id':first['result_id'],'version':1}
 path=f"/api/v1/bots/{ids['author']}/library/pins"; auth=headers(c,ids)
 assert c.post(path+'/status',json={'source':source},headers=auth).json()['revision']==0
 op=command(source); applied=c.post(path+'/operations',json=op,headers=auth);assert applied.json()=={'state':'applied','revision':1,'pinned':True,'code':None,'operation_id':op['operation_id'],'source':source,'expected_revision':0,'requested_pinned':True,'expected_owner_epoch':'a'*64}
 assert c.post(path+'/resolve',json=op,headers=auth).json()==applied.json()
 assert c.post(path+'/operations',json={**op,'pinned':False},headers=auth).status_code==409
 assert c.post(path+'/operations',json=command(source,0,False),headers=auth).json()['code']=='pin_revision_changed'
 unpin=command(source,1,False);assert c.post(path+'/operations',json=unpin,headers=auth).json()['revision']==2
 assert c.post(path+'/status',json={'source':source},headers=auth).json()['revision']==2
 blocked=command(source,2,True);fenced=c.post(path+'/resolve',json=blocked,headers=auth).json();assert fenced['state']=='fenced'
 assert c.post(path+'/operations',json=blocked,headers=auth).json()==fenced
 assert c.get(path,headers=auth).json()['items']==[]
 assert c.post(path+'/operations',json=command(source,2),headers=auth).json()['revision']==3
 assert c.post(path+'/status',json={'source':source},headers=headers(c,ids,owner=ids['other'])).status_code in (404,503)
 with c.app.state.session_factory() as db:
  db.get(Conversation,ids['room']).is_active=False;db.commit()
 item=c.get(path,headers=auth).json()['items'][0];assert item['source']==source and item['availability']=='unavailable'
 assert c.post(path+'/operations',json=command(source,3,False),headers=auth).json()['pinned'] is False

def test_upload_tombstone_is_not_deleted_or_revived_by_pin(results,tmp_path):
 c,ids=results;tmp_path.chmod(0o700);store=LibraryUploads(tmp_path);c.app.state.local_executor.library_uploads=store
 content=b'synthetic upload';digest=hashlib.sha256(content).hexdigest();upload=str(uuid4())
 store.put(ids['owner'],ids['author'],upload,'synthetic.txt',content,digest)
 source={'kind':'upload','upload_id':upload,'sha256':digest};path=f"/api/v1/bots/{ids['author']}/library/pins";auth=headers(c,ids)
 assert c.post(path+'/operations',json=command(source),headers=auth).json()['state']=='applied'
 assert store.read(ids['owner'],ids['author'],upload)==content
 store.delete(ids['owner'],ids['author'],upload)
 assert c.get(path,headers=auth).json()['items'][0]['availability']=='removed'
 assert c.post(path+'/operations',json=command(source,1,False),headers=auth).json()['state']=='applied'
 assert c.post(path+'/operations',json=command(source,2,True),headers=auth).json()['code']=='pin_source_unavailable'

def test_resolve_races_original_without_late_write(results):
 c,ids=results; first=save(c,ids).json();source={'kind':'result','room_id':ids['room'],'result_id':first['result_id'],'version':1};op=command(source)
 path=f"/api/v1/bots/{ids['author']}/library/pins";auth=headers(c,ids);barrier=Barrier(2)
 def call(endpoint):
  barrier.wait();r=c.post(path+endpoint,json=op,headers=auth);return r.status_code,r.json()
 with ThreadPoolExecutor(max_workers=2) as pool: responses=list(pool.map(call,['/resolve','/operations']))
 assert responses[0][0]==responses[1][0]==200 and responses[0][1]==responses[1][1]
 final=c.post(path+'/status',json={'source':source},headers=auth).json()
 assert final['pinned']==(responses[0][1]['state']=='applied')

def test_missing_upload_differs_from_unavailable_store(results,tmp_path):
 c,ids=results;tmp_path.chmod(0o700);store=LibraryUploads(tmp_path);c.app.state.local_executor.library_uploads=store
 source={'kind':'upload','upload_id':str(uuid4()),'sha256':'a'*64};path=f"/api/v1/bots/{ids['author']}/library/pins/status";auth=headers(c,ids)
 assert c.post(path,json={'source':source},headers=auth).json()['availability']=='unavailable'
 c.app.state.local_executor.library_uploads=None
 assert c.post(path,json={'source':source},headers=auth).json()['availability']=='unknown'

def test_revision_survives_session_and_wrong_source_owner(results):
 c,ids=results;first=save(c,ids).json();source={'kind':'result','room_id':ids['room'],'result_id':first['result_id'],'version':1}
 auth=headers(c,ids);path=f"/api/v1/bots/{ids['author']}/library/pins";op=command(source)
 result=c.post(path+'/operations',json=op,headers=auth).json()
 c.app.state.session_factory.close_all()
 assert c.post(path+'/resolve',json=op,headers=auth).json()==result
 wrong=f"/api/v1/bots/{ids['reviewer']}/library/pins/operations"
 assert c.post(wrong,json=command(source),headers=auth).json()['code']=='pin_source_unavailable'

def test_exact_old_result_version_survives_new_page_version(results):
 c,ids=results;first=save(c,ids).json();source={'kind':'result','room_id':ids['room'],'result_id':first['result_id'],'version':1}
 path=f"/api/v1/bots/{ids['author']}/library/pins";auth=headers(c,ids)
 assert c.post(path+'/operations',json=command(source),headers=auth).json()['state']=='applied'
 response=c.post(f"{base(ids)}/{first['result_id']}/versions",json={'source_event_id':ids['next'],'title':'Revision','kind':'text','expected_current_version':1},headers=headers(c,ids,'new-version'))
 assert response.status_code==201,response.text
 item=c.get(path,headers=auth).json()['items'][0];assert item['source']==source and item['availability']=='available'

def test_new_session_recovers_fence_without_running_original(results,sqlite_url):
 from sqlalchemy import create_engine
 from sqlalchemy.orm import Session
 from app.services import library_pins
 c,ids=results;first=save(c,ids).json();source={'kind':'result','room_id':ids['room'],'result_id':first['result_id'],'version':1};op=command(source)
 auth=headers(c,ids);path=f"/api/v1/bots/{ids['author']}/library/pins"
 assert c.post(path+'/resolve',json=op,headers=auth).json()['state']=='fenced'
 engine=create_engine(sqlite_url)
 try:
  with Session(engine) as db:assert library_pins.operate(db,None,ids['owner'],ids['author'],op,c.app.state.owner_epoch_provider,c.app.state.owner_recovery_lock,'a'*64)['state']=='fenced'
 finally:engine.dispose()
 assert c.post(path+'/status',json={'source':source},headers=auth).json()['revision']==0

def test_slow_upload_does_not_hold_sqlite_write_lock(results,tmp_path,sqlite_url):
 from threading import Event
 from sqlalchemy import create_engine,text
 from app.models.models import Project
 c,ids=results;tmp_path.chmod(0o700);store=LibraryUploads(tmp_path);c.app.state.local_executor.library_uploads=store
 content=b'pin synthetic';digest=hashlib.sha256(content).hexdigest();upload=str(uuid4());store.put(ids['owner'],ids['author'],upload,'x.txt',content,digest)
 source={'kind':'upload','upload_id':upload,'sha256':digest};op=command(source);auth=headers(c,ids);path=f"/api/v1/bots/{ids['author']}/library/pins"
 entered=Event();release=Event();detail=store.detail
 def slow(*args):
  entered.set();assert release.wait(5);return detail(*args)
 store.detail=slow
 engine=create_engine(sqlite_url,connect_args={'timeout':1})
 try:
  with ThreadPoolExecutor(max_workers=1) as pool:
   future=pool.submit(c.post,path+'/operations',json=op,headers=auth);assert entered.wait(5)
   try:
    with engine.connect() as db:
     db.execute(text('BEGIN IMMEDIATE'));db.execute(text('UPDATE projects SET name=name WHERE id=:id'),{'id':ids['project'] if 'project' in ids else 1});db.commit()
   finally:release.set()
   assert future.result(timeout=5).json()['state']=='applied'
 finally:release.set();engine.dispose()

def test_upload_delete_after_precheck_leaves_honest_removed_pin(results,tmp_path):
 from threading import Event
 c,ids=results;tmp_path.chmod(0o700);store=LibraryUploads(tmp_path);c.app.state.local_executor.library_uploads=store
 content=b'pin synthetic';digest=hashlib.sha256(content).hexdigest();upload=str(uuid4());store.put(ids['owner'],ids['author'],upload,'x.txt',content,digest)
 source={'kind':'upload','upload_id':upload,'sha256':digest};op=command(source);auth=headers(c,ids);path=f"/api/v1/bots/{ids['author']}/library/pins"
 observed=Event();release=Event();detail=store.detail
 def snapshot(*args):
  data=detail(*args);observed.set();assert release.wait(5);return data
 store.detail=snapshot
 with ThreadPoolExecutor(max_workers=1) as pool:
  future=pool.submit(c.post,path+'/operations',json=op,headers=auth);assert observed.wait(5)
  store.delete(ids['owner'],ids['author'],upload);release.set();assert future.result(timeout=5).json()['state']=='applied'
 store.detail=detail
 assert c.get(path,headers=auth).json()['items'][0]['availability']=='removed'

def test_owner_change_during_upload_check_rejects_before_ledger(results,tmp_path,monkeypatch):
 from threading import Event
 c,ids=results;tmp_path.chmod(0o700);store=LibraryUploads(tmp_path);c.app.state.local_executor.library_uploads=store
 content=b'epoch test';digest=hashlib.sha256(content).hexdigest();upload=str(uuid4());store.put(ids['owner'],ids['author'],upload,'x.txt',content,digest)
 source={'kind':'upload','upload_id':upload,'sha256':digest};op=command(source);auth=headers(c,ids);path=f"/api/v1/bots/{ids['author']}/library/pins"
 entered=Event();release=Event();detail=store.detail;epoch={'value':'a'*64}
 monkeypatch.setattr(c.app.state.owner_epoch_provider,'current',lambda owner:epoch['value'] if owner==ids['owner'] else None)
 def slow(*args):entered.set();assert release.wait(5);return detail(*args)
 store.detail=slow
 with ThreadPoolExecutor(max_workers=1) as pool:
  future=pool.submit(c.post,path+'/operations',json=op,headers=auth);assert entered.wait(5)
  with c.app.state.owner_recovery_lock:epoch['value']='b'*64
  release.set();response=future.result(timeout=5)
 assert response.status_code==409 and response.json()['detail']['code']=='pin_owner_changed'
 from sqlalchemy import text
 with c.app.state.session_factory() as db:
  assert db.execute(text('SELECT count(*) FROM library_pin_operations')).scalar_one()==0
  assert db.execute(text('SELECT count(*) FROM library_pins')).scalar_one()==0

def test_busy_sqlite_does_not_wait_while_holding_owner_lock(results,sqlite_url):
 from sqlalchemy import create_engine,text
 c,ids=results;first=save(c,ids).json();source={'kind':'result','room_id':ids['room'],'result_id':first['result_id'],'version':1};op=command(source)
 path=f"/api/v1/bots/{ids['author']}/library/pins";engine=create_engine(sqlite_url)
 try:
  with engine.connect() as db:
   db.execute(text('BEGIN IMMEDIATE'))
   r=c.post(path+'/resolve',json=op,headers=headers(c,ids));assert r.status_code==503 and r.json()['detail']['code']=='pin_busy'
   db.rollback()
  assert c.post(path+'/resolve',json=op,headers=headers(c,ids)).json()['state']=='fenced'
 finally:engine.dispose()

def test_stale_epoch_arrives_after_owner_rebind_cannot_write(results,monkeypatch):
 from sqlalchemy import text
 c,ids=results;first=save(c,ids).json();source={'kind':'result','room_id':ids['room'],'result_id':first['result_id'],'version':1};op=command(source)
 monkeypatch.setattr(c.app.state.owner_epoch_provider,'current',lambda owner:'b'*64)
 path=f"/api/v1/bots/{ids['author']}/library/pins"
 r=c.post(path+'/operations',json=op,headers=headers(c,ids));assert r.status_code==409 and r.json()['detail']['code']=='pin_owner_changed'
 with c.app.state.session_factory() as db:assert db.execute(text('SELECT count(*) FROM library_pin_operations')).scalar_one()==0

def test_owner_change_during_list_rejects_old_response(results,tmp_path,monkeypatch):
 from threading import Event
 c,ids=results;tmp_path.chmod(0o700);store=LibraryUploads(tmp_path);c.app.state.local_executor.library_uploads=store
 content=b'read race';digest=hashlib.sha256(content).hexdigest();upload=str(uuid4());store.put(ids['owner'],ids['author'],upload,'x.txt',content,digest)
 source={'kind':'upload','upload_id':upload,'sha256':digest};auth=headers(c,ids);path=f"/api/v1/bots/{ids['author']}/library/pins"
 assert c.post(path+'/operations',json=command(source),headers=auth).json()['state']=='applied'
 entered=Event();release=Event();detail=store.detail;epoch={'value':'a'*64}
 monkeypatch.setattr(c.app.state.owner_epoch_provider,'current',lambda owner:epoch['value'])
 def slow(*args):entered.set();assert release.wait(5);return detail(*args)
 store.detail=slow
 with ThreadPoolExecutor(max_workers=1) as pool:
  future=pool.submit(c.get,path,headers=auth);assert entered.wait(5)
  with c.app.state.owner_recovery_lock:epoch['value']='b'*64
  release.set();r=future.result(timeout=5)
 assert r.status_code==409 and r.json()['detail']['code']=='pin_owner_changed'

def test_pool_never_receives_zero_busy_timeout(results):
 from sqlalchemy import event
 c,ids=results;first=save(c,ids).json();source={'kind':'result','room_id':ids['room'],'result_id':first['result_id'],'version':1};engine=c.app.state.session_factory.kw['bind'];seen=[]
 def checkin(conn,record):
  if conn is not None:seen.append(conn.execute('PRAGMA busy_timeout').fetchone()[0])
 event.listen(engine,'checkin',checkin)
 try:
  path=f"/api/v1/bots/{ids['author']}/library/pins";assert c.post(path+'/operations',json=command(source),headers=headers(c,ids)).json()['state']=='applied'
 finally:event.remove(engine,'checkin',checkin)
 assert seen and all(timeout>0 for timeout in seen)

def test_relogin_can_resolve_exact_old_receipt_or_fence_missing(results,monkeypatch):
 c,ids=results;first=save(c,ids).json();source={'kind':'result','room_id':ids['room'],'result_id':first['result_id'],'version':1};auth=headers(c,ids);path=f"/api/v1/bots/{ids['author']}/library/pins"
 done=command(source);assert c.post(path+'/operations',json=done,headers=auth).json()['state']=='applied'
 missing=command(source,1,False);monkeypatch.setattr(c.app.state.owner_epoch_provider,'current',lambda owner:'b'*64 if owner==ids['owner'] else None)
 assert c.post(path+'/resolve',json=done,headers=auth).json()['state']=='applied'
 settled=c.post(path+'/resolve',json=missing,headers=auth).json();assert settled['state']=='fenced' and settled['expected_owner_epoch']=='a'*64
 late=c.post(path+'/operations',json=missing,headers=auth);assert late.status_code==409 and late.json()['detail']['code']=='pin_owner_changed'
 current=command(source,1,False);current['expected_owner_epoch']='b'*64
 assert c.post(path+'/operations',json=current,headers=auth).json()['state']=='applied'
 assert c.post(path+'/resolve',json=missing,headers=auth).json()==settled
 altered={**missing,'pinned':True};assert c.post(path+'/resolve',json=altered,headers=auth).status_code==409

def test_migration_67_to_68_preserves_existing_data(tmp_path,monkeypatch):
 from sqlalchemy import create_engine,text
 from app.core import migrate
 monkeypatch.setenv('GAMJA_APP_DATA_DIR',str(tmp_path/'app-data'))
 engine=create_engine('sqlite:///'+str(tmp_path/'old.sqlite'))
 revisions=migrate.REVISIONS
 try:
  monkeypatch.setattr(migrate,'REVISIONS',revisions[:-1]);migrate.migrate_database(engine)
  with engine.begin() as db:
   assert db.execute(text('SELECT max(version) FROM schema_version')).scalar_one()==67
   db.execute(text("INSERT INTO users(id,email,username,hashed_password,is_active) VALUES(71,'pin-test@invalid','pin-test','synthetic',1)"))
  monkeypatch.setattr(migrate,'REVISIONS',revisions);migrate.migrate_database(engine)
  with engine.connect() as db:
   assert db.execute(text('SELECT max(version) FROM schema_version')).scalar_one()==68
   assert db.execute(text('SELECT username FROM users WHERE id=71')).scalar_one()=='pin-test'
   assert db.execute(text('SELECT count(*) FROM library_pins')).scalar_one()==0
   assert db.execute(text('PRAGMA foreign_key_check')).all()==[]
  assert list((tmp_path/'app-data'/'backups').glob('*.sqlite'))
 finally:engine.dispose()
