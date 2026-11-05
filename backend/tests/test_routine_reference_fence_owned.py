from uuid import uuid4
from test_room_state import room_client,headers
from test_bot_creation import private_team
from test_owner_routines import data
from app.models.collaboration import RoomEvent
from sqlalchemy import text

def setup_ref(room_client):
 client,(owner,other,project,_,_)=room_client;bot,room,_=private_team(client,owner,project)
 routine=client.post(f'/api/v1/bots/{bot}/routines',headers={**headers(client,owner),'Idempotency-Key':str(uuid4())},json=data(False)).json()['routine']
 payload={'content':'Synthetic request','routine_ref':{'agent_id':bot,'routine_id':routine['routine_id'],'revision':routine['revision']}}
 return client,owner,other,bot,room,routine,payload

def test_fenced_rejection_survives_restored_routine_and_payload_mismatch(room_client):
 client,owner,other,bot,room,r,payload=setup_ref(room_client); key=str(uuid4());h={**headers(client,owner),'Idempotency-Key':key}
 with client.app.state.session_factory() as db:
  db.execute(text('UPDATE owner_routines SET deleted_at=CURRENT_TIMESTAMP WHERE routine_id=:id'),{'id':r['routine_id']});db.commit()
 result=client.post(f'/api/v1/conversations/{room}/routine-reference/resolve',headers=h,json=payload)
 assert result.status_code==200 and result.json()=={'version':1,'state':'rejected','code':'routine_not_found'}
 with client.app.state.session_factory() as db:
  db.execute(text('UPDATE owner_routines SET deleted_at=NULL WHERE routine_id=:id'),{'id':r['routine_id']});db.commit()
 late=client.post(f'/api/v1/conversations/{room}/events',headers=h,json=payload)
 assert late.status_code==404 and late.json()['detail']=={'code':'routine_not_found','fenced':True}
 mismatch=client.post(f'/api/v1/conversations/{room}/events',headers=h,json={**payload,'content':'Different'})
 assert mismatch.status_code==409 and mismatch.json()['detail']['code']=='routine_reference_key_conflict'
 with client.app.state.session_factory() as db:
  assert db.query(RoomEvent).filter_by(room_id=room,idempotency_key=key).count()==0
  record=db.execute(text('SELECT request_digest,code FROM routine_reference_fences WHERE owner_id=:owner AND room_id=:room AND idempotency_key=:key'),{'owner':owner,'room':room,'key':key}).first()
  assert record.code=='routine_not_found' and len(record.request_digest)==64

def test_pending_accepted_and_other_owner_rejected(room_client):
 client,owner,other,bot,room,r,payload=setup_ref(room_client);key=str(uuid4());h={**headers(client,owner),'Idempotency-Key':key};base=f'/api/v1/conversations/{room}'
 pending=client.post(base+'/routine-reference/resolve',headers=h,json=payload)
 assert pending.json()=={'version':1,'state':'pending'}
 saved=client.post(base+'/events',headers=h,json=payload);assert saved.status_code==201,saved.text
 result=client.post(base+'/routine-reference/resolve',headers=h,json=payload)
 assert result.json()=={'version':1,'state':'accepted','event_id':saved.json()['event_id'],'seq':saved.json()['seq']}
 foreign=client.post(base+'/routine-reference/resolve',headers={**headers(client,other),'Idempotency-Key':key},json=payload)
 assert foreign.status_code==404

def test_concurrent_reject_and_resolve_share_one_durable_fence(room_client):
 from concurrent.futures import ThreadPoolExecutor
 from threading import Barrier
 client,owner,other,bot,room,r,payload=setup_ref(room_client)
 payload={**payload,'routine_ref':{**payload['routine_ref'],'revision':r['revision']+1}}
 key=str(uuid4());h={**headers(client,owner),'Idempotency-Key':key};base=f'/api/v1/conversations/{room}';barrier=Barrier(2)
 def call(route):
  barrier.wait(timeout=5)
  response=client.post(base+route,headers=h,json=payload)
  return response.status_code,response.json()
 with ThreadPoolExecutor(max_workers=2) as pool:
  send,resolve=list(pool.map(call,('/events','/routine-reference/resolve')))
 assert send[0]==409 and send[1]['detail']['fenced'] is True
 assert resolve==(200,{'version':1,'state':'rejected','code':'routine_reference_changed'})
 with client.app.state.session_factory() as db:
  assert db.execute(text('SELECT count(*) FROM routine_reference_fences WHERE owner_id=:owner AND room_id=:room AND idempotency_key=:key'),{'owner':owner,'room':room,'key':key}).scalar()==1
  db.execute(text('UPDATE owner_routines SET revision=:revision WHERE routine_id=:id'),{'revision':r['revision']+1,'id':r['routine_id']});db.commit()
 late=client.post(base+'/events',headers=h,json=payload)
 assert late.status_code==409 and late.json()['detail']['fenced'] is True

def test_fence_survives_application_restart(sqlite_url,monkeypatch):
 from fastapi.testclient import TestClient
 from app.main import create_app
 from app.models.models import User,Project
 monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN','room-test-local-token')
 with TestClient(create_app(database_url=sqlite_url)) as first:
  with first.app.state.session_factory() as db:
   owner=User(email='restart-fence@invalid.test',username='restart-fence',hashed_password='x');db.add(owner);db.flush()
   project=Project(user_id=owner.id,name='Synthetic');db.add(project);db.commit();oid=owner.id;pid=project.id
  bot,room,_=private_team(first,oid,pid)
  routine=first.post(f'/api/v1/bots/{bot}/routines',headers={**headers(first,oid),'Idempotency-Key':str(uuid4())},json=data(False)).json()['routine']
  payload={'content':'Do not duplicate','routine_ref':{'agent_id':bot,'routine_id':routine['routine_id'],'revision':routine['revision']+1}}
  key=str(uuid4());h={**headers(first,oid),'Idempotency-Key':key};base=f'/api/v1/conversations/{room}'
  first_response=first.post(base+'/events',headers=h,json=payload)
  assert first_response.status_code==409 and first_response.json()['detail']['fenced'] is True
 with TestClient(create_app(database_url=sqlite_url)) as second:
  h={**headers(second,oid),'Idempotency-Key':key}
  with second.app.state.session_factory() as db:
   db.execute(text('UPDATE owner_routines SET revision=:revision WHERE routine_id=:id'),{'revision':routine['revision']+1,'id':routine['routine_id']});db.commit()
  late=second.post(base+'/events',headers=h,json=payload)
  assert late.status_code==409 and late.json()['detail']['fenced'] is True
  resolved=second.post(base+'/routine-reference/resolve',headers=h,json=payload)
  assert resolved.json()=={'version':1,'state':'rejected','code':'routine_reference_changed'}

def test_inactive_room_fence_prevents_acceptance_after_reactivation(room_client):
 client,owner,other,bot,room,r,payload=setup_ref(room_client);key=str(uuid4());h={**headers(client,owner),'Idempotency-Key':key};base=f'/api/v1/conversations/{room}'
 with client.app.state.session_factory() as db:
  db.execute(text('UPDATE conversations SET is_active=0 WHERE id=:id'),{'id':room});db.commit()
 result=client.post(base+'/routine-reference/resolve',headers=h,json=payload)
 assert result.status_code==200 and result.json()=={'version':1,'state':'rejected','code':'routine_reference_room_changed'}
 with client.app.state.session_factory() as db:
  db.execute(text('UPDATE conversations SET is_active=1 WHERE id=:id'),{'id':room});db.commit()
 late=client.post(base+'/events',headers=h,json=payload)
 assert late.status_code==409 and late.json()['detail']=={'code':'routine_reference_room_changed','fenced':True}

def test_ordinary_event_dedup_unchanged(room_client):
 from app.services.events import stage_event,ActorRef
 client,owner,other,bot,room,r,payload=setup_ref(room_client)
 key=str(uuid4())
 with client.app.state.session_factory() as db:
  first=stage_event(db,room,ActorRef('user',owner),'message',{'content':'ordinary synthetic message'},key);db.commit();first_id=first.event_id
 with client.app.state.session_factory() as db:
  again=stage_event(db,room,ActorRef('user',owner),'message',{'content':'ordinary synthetic message'},key);db.commit();assert again.event_id==first_id
