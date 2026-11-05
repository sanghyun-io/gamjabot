"""Synthetic paired gateway, isolated app-data, no real device or account."""
import asyncio,hashlib
from threading import Event
from uuid import uuid4
import httpx,pytest
from test_mobile_access import mobile,paired,ORIGIN
from test_review_preview import preview_fixture
from test_room_state import room_client

from test_room_state import headers
from app.services.library_uploads import LibraryUploads

def op(enabled,revision=0,epoch='ab'*32):return {'operation_id':str(uuid4()),'enabled':enabled,'expected_revision':revision,'expected_owner_epoch':epoch}
def device(m):return next(iter(m.state['devices']))
def allow(m):
 key=device(m);command=op(True);r=m.library_scope_command(m.state['owner'],key,command);assert r['state']=='applied';return key,command

@pytest.mark.asyncio
async def test_default_off_scope_is_per_device_and_persists(mobile):
 m,epoch,client,owner,*_=mobile
 async with paired(m) as (phone,_):
  key=device(m);assert (await phone.get('/session')).json()['library_read'] is False
  assert (await phone.get('/api/library/bots')).status_code==403
  first=op(True);result=m.library_scope_command(owner,key,first);assert result['device_id']==key and result['library_revision']==1
  assert m.library_scope_command(owner,key,first,resolve=True)==result
  assert (await phone.get('/api/library/bots')).status_code==200
  assert (await phone.post('/api/library/bots',json={})).status_code==405
  from app.services.mobile_access import MobileAccess
  reloaded=MobileAccess(m.root,client.app);assert reloaded.state['devices'][key]['library_read'] is True
  assert reloaded.state['devices'][key]['library_revision']==1
  off=op(False,1);assert m.library_scope_command(owner,key,off)['library_revision']==2
  assert (await phone.get('/api/library/bots')).status_code==403

@pytest.mark.asyncio
async def test_scope_receipt_fence_cas_and_same_owner_new_epoch(mobile):
 from app.services.core_auth_broker import OwnerBinding
 m,epoch,client,owner,*_=mobile
 async with paired(m) as (phone,_):
  key=device(m);enabled=op(True);assert m.library_scope_command(owner,key,enabled)['state']=='applied'
  stale=op(False,0);assert m.library_scope_command(owner,key,stale)['code']=='mobile_library_revision_changed'
  fenced=op(False,1);receipt=m.library_scope_command(owner,key,fenced,resolve=True);assert receipt['state']=='fenced'
  assert m.library_scope_command(owner,key,fenced)==receipt
  epoch.binding=OwnerBinding(owner,'cd'*32)
  assert m.library_scope_command(owner,key,enabled,resolve=True)['state']=='applied'
  with pytest.raises(Exception) as e:m.library_scope_command(owner,key,op(False,1))
  assert e.value.status_code==409
  newer=op(False,1,'cd'*32);assert m.library_scope_command(owner,key,newer)['state']=='applied'
  assert m.library_scope_command(owner,key,fenced,resolve=True)==receipt

@pytest.mark.asyncio
async def test_upload_list_detail_and_safe_download(mobile,tmp_path):
 m,epoch,client,owner,broker,path,proposal=mobile;action=proposal();bot=action.agent_id
 store=LibraryUploads(tmp_path);client.app.state.local_executor.library_uploads=store
 content=b'<script>synthetic</script>';sha=hashlib.sha256(content).hexdigest();upload=str(uuid4());store.put(owner,bot,upload,'unsafe.html',content,sha)
 async with paired(m) as (phone,_):
  allow(m);base=f'/api/library/bots/{bot}/uploads'
  page=await phone.get(base);assert page.status_code==200,page.text
  assert page.json()['items'][0]['sha256']==sha
  detail=await phone.get(base+'/'+upload);assert detail.status_code==200 and detail.json()['filename']=='unsafe.html'
  file=await phone.get(base+'/'+upload+'/file');assert file.status_code==200 and file.content==content
  assert file.headers['content-type']=='application/octet-stream'
  assert file.headers['content-disposition'].startswith('attachment;')
  assert file.headers['cache-control']=='no-store' and file.headers['x-content-type-options']=='nosniff'
  store.delete(owner,bot,upload);assert (await phone.get(base+'/'+upload+'/file')).status_code==410

@pytest.mark.asyncio
async def test_scope_aba_during_slow_read_never_returns_data(mobile,tmp_path):
 m,epoch,client,owner,broker,path,proposal=mobile;action=proposal();bot=action.agent_id
 store=LibraryUploads(tmp_path);client.app.state.local_executor.library_uploads=store
 content=b'synthetic';sha=hashlib.sha256(content).hexdigest();upload=str(uuid4());store.put(owner,bot,upload,'x.txt',content,sha)
 entered=Event();release=Event();detail=store.detail
 def slow(*args):entered.set();assert release.wait(5);return detail(*args)
 store.detail=slow
 async with paired(m) as (phone,_):
  key,_=allow(m);task=asyncio.create_task(phone.get(f'/api/library/bots/{bot}/uploads/{upload}'))
  try:
   assert await asyncio.to_thread(entered.wait,5)
   assert m.library_scope_command(owner,key,op(False,1))['state']=='applied'
   assert m.library_scope_command(owner,key,op(True,2))['state']=='applied'
  finally:release.set()
  response=await task;assert response.status_code==409 and b'synthetic' not in response.content

@pytest.mark.asyncio
async def test_result_exact_version_and_pin_list_owned(mobile):
 from datetime import datetime,timezone
 from app.models.collaboration import RoomEvent
 from app.models.room_result import RoomResult,RoomResultVersion
 m,epoch,client,owner,broker,path,proposal=mobile;action=proposal();bot=action.agent_id;room=action.room_id
 # Synthetic saved result in a real SQLite room; no provider or user data.
 rid=str(uuid4());eid=str(uuid4());now=datetime.now(timezone.utc)
 with client.app.state.session_factory() as db:
  from sqlalchemy import func
  seq=(db.query(func.max(RoomEvent.seq)).filter_by(room_id=room).scalar() or 0)+1
  db.add(RoomEvent(event_id=eid,room_id=room,seq=seq,actor_type='agent',actor_id=bot,actor_name_snapshot='Synthetic',kind='message',payload={'content':'Result contents'},created_at=now));db.flush()
  db.add(RoomResult(result_id=rid,owner_id=owner,room_id=room,current_version=1,created_at=now,updated_at=now));db.flush()
  db.add(RoomResultVersion(result_id=rid,version=1,room_id=room,kind='text',title='Synthetic result',source_event_id=eid,source_seq=seq,selector='text',author_agent_id=bot,author_name_snapshot='Synthetic',execution={},content='Result contents',evidence=[],state='draft',state_revision=1,link_report='unchecked',created_at=now));db.commit()
 client.app.state.owner_epoch_provider.current=lambda uid:epoch.binding.owner_epoch if epoch.binding and epoch.binding.owner_id==uid else None
 async with paired(m) as (phone,_):
  allow(m);base=f'/api/library/bots/{bot}'
  listing=await phone.get(base+'/results');assert listing.status_code==200,listing.text
  assert any(x['result_id']==rid and x['version']==1 for x in listing.json()['items'])
  detail=await phone.get(base+f'/results/{room}/{rid}/1');assert detail.status_code==200,detail.text
  assert detail.json()['content']=='Result contents' and 'file_ref' not in detail.json() and 'execution' not in detail.json()
  assert (await phone.get(base+f'/results/{room}/{rid}/2')).status_code==404
  pins=await phone.get(base+'/pins');assert pins.status_code==200,pins.text;assert pins.json()['items']==[]

@pytest.mark.asyncio
async def test_revoke_during_slow_read_prevents_return(mobile,tmp_path):
 m,epoch,client,owner,broker,path,proposal=mobile;action=proposal();bot=action.agent_id
 store=LibraryUploads(tmp_path);client.app.state.local_executor.library_uploads=store
 content=b'private synthetic';sha=hashlib.sha256(content).hexdigest();upload=str(uuid4());store.put(owner,bot,upload,'x.txt',content,sha)
 entered=Event();release=Event();detail=store.detail
 def slow(*args):entered.set();assert release.wait(5);return detail(*args)
 store.detail=slow
 async with paired(m) as (phone,_):
  key,_=allow(m);task=asyncio.create_task(phone.get(f'/api/library/bots/{bot}/uploads/{upload}'))
  try:
   assert await asyncio.to_thread(entered.wait,5);m.revoke(owner,key)
  finally:release.set()
  response=await task;assert response.status_code==401 and b'private synthetic' not in response.content

@pytest.mark.asyncio
async def test_scope_revoked_at_download_send_boundary_returns_no_file(mobile,tmp_path,monkeypatch):
 from app.services.mobile_access import MobileDownload
 m,epoch,client,owner,broker,path,proposal=mobile;action=proposal();bot=action.agent_id
 store=LibraryUploads(tmp_path);client.app.state.local_executor.library_uploads=store
 content=b'private synthetic file';sha=hashlib.sha256(content).hexdigest();upload=str(uuid4());store.put(owner,bot,upload,'x.txt',content,sha)
 original=MobileDownload.__call__
 async with paired(m) as (phone,_):
  key,_=allow(m)
  async def disable_before_send(response,scope,receive,send):
   assert m.library_scope_command(owner,key,op(False,1))['state']=='applied'
   return await original(response,scope,receive,send)
  monkeypatch.setattr(MobileDownload,'__call__',disable_before_send)
  response=await phone.get(f'/api/library/bots/{bot}/uploads/{upload}/file')
  assert response.status_code==403 and content not in response.content
  assert not m.library_download.locked()

@pytest.mark.asyncio
async def test_shared_bot_hidden_and_direct_id_denied(mobile):
 from app.models.collaboration import ProviderConnection
 from app.models.shared_text_runtime import SharedTextRoom
 m,epoch,client,owner,broker,path,proposal=mobile;action=proposal();bot=action.agent_id;room=action.room_id
 with client.app.state.session_factory() as db:
  connection=ProviderConnection(user_id=owner,kind='compatible',name='Synthetic',base_url='http://127.0.0.1:1/v1',default_model_id='test',auth_required=False,enabled=True,capabilities={})
  db.add(connection);db.flush()
  db.add(SharedTextRoom(owner_id=owner,issuer='synthetic',subject='synthetic',team_id='t',publication_id='p',room_id=room,agent_id=bot,connection_id=connection.id,model_id='test'));db.commit()
 async with paired(m) as (phone,_):
  allow(m);page=await phone.get('/api/library/bots');assert page.status_code==200 and all(x['id']!=bot for x in page.json()['items'])
  for part in ('results','uploads','pins'):
   assert (await phone.get(f'/api/library/bots/{bot}/{part}')).status_code==404

@pytest.mark.asyncio
async def test_result_room_deactivated_before_send_returns_no_bytes(mobile,monkeypatch):
 from datetime import datetime,timezone
 from app.models.collaboration import RoomEvent
 from app.models.models import Conversation
 from app.models.room_result import RoomResult,RoomResultVersion
 from app.services.mobile_access import MobileDownload
 from sqlalchemy import func
 m,epoch,client,owner,broker,path,proposal=mobile;action=proposal();bot=action.agent_id;room=action.room_id;rid=str(uuid4());eid=str(uuid4());now=datetime.now(timezone.utc)
 with client.app.state.session_factory() as db:
  seq=(db.query(func.max(RoomEvent.seq)).filter_by(room_id=room).scalar() or 0)+1
  db.add(RoomEvent(event_id=eid,room_id=room,seq=seq,actor_type='agent',actor_id=bot,actor_name_snapshot='Synthetic',kind='message',payload={'content':'synthetic'},created_at=now));db.flush()
  db.add(RoomResult(result_id=rid,owner_id=owner,room_id=room,current_version=1,created_at=now,updated_at=now));db.flush()
  db.add(RoomResultVersion(result_id=rid,version=1,room_id=room,kind='text',title='Synthetic',source_event_id=eid,source_seq=seq,selector='text',author_agent_id=bot,author_name_snapshot='Synthetic',execution={},content='synthetic',evidence=[],state='draft',state_revision=1,link_report='unchecked',created_at=now));db.commit()
 original_forward=m.library_forward;original_send=MobileDownload.__call__
 async def fake_file(*args,**kwargs):
  if kwargs.get('download'):return httpx.Response(200,content=b'private synthetic')
  return await original_forward(*args,**kwargs)
 async def change_before_send(response,scope,receive,send):
  with client.app.state.session_factory() as db:db.get(Conversation,room).is_active=False;db.commit()
  return await original_send(response,scope,receive,send)
 monkeypatch.setattr(m,'library_forward',fake_file);monkeypatch.setattr(MobileDownload,'__call__',change_before_send)
 async with paired(m) as (phone,_):
  allow(m);response=await phone.get(f'/api/library/bots/{bot}/results/{room}/{rid}/1/file')
  assert response.status_code==404 and b'private synthetic' not in response.content

@pytest.mark.asyncio
async def test_directory_fsync_failure_disables_scope_until_restart(mobile,monkeypatch):
 import os,stat
 m,epoch,client,owner,*_=mobile
 async with paired(m) as (phone,_):
  key=device(m);real=os.fsync
  def fail_directory(fd):
   if stat.S_ISDIR(os.fstat(fd).st_mode):raise OSError('synthetic fsync failure')
   return real(fd)
  monkeypatch.setattr(os,'fsync',fail_directory)
  with pytest.raises(OSError):m.library_scope_command(owner,key,op(True))
  assert m.library_scope_uncertain is True
  assert (await phone.get('/api/library/bots')).status_code==503
  with pytest.raises(Exception) as e:m.library_scope_command(owner,key,op(True,1))
  assert e.value.status_code==503

@pytest.mark.asyncio
async def test_personal_bot_shared_room_results_are_excluded(mobile):
 from datetime import datetime,timezone
 from app.models.collaboration import RoomEvent,ProviderConnection
 from app.models.models import Agent
 from app.models.room_result import RoomResult,RoomResultVersion
 from app.models.shared_text_runtime import SharedTextRoom
 from sqlalchemy import func
 m,epoch,client,owner,broker,path,proposal=mobile;action=proposal();bot=action.agent_id;room=action.room_id;rid=str(uuid4());eid=str(uuid4());now=datetime.now(timezone.utc)
 with client.app.state.session_factory() as db:
  seq=(db.query(func.max(RoomEvent.seq)).filter_by(room_id=room).scalar() or 0)+1
  db.add(RoomEvent(event_id=eid,room_id=room,seq=seq,actor_type='agent',actor_id=bot,actor_name_snapshot='Synthetic',kind='message',payload={'content':'shared synthetic'},created_at=now));db.flush()
  db.add(RoomResult(result_id=rid,owner_id=owner,room_id=room,current_version=1,created_at=now,updated_at=now));db.flush()
  db.add(RoomResultVersion(result_id=rid,version=1,room_id=room,kind='text',title='Shared synthetic',source_event_id=eid,source_seq=seq,selector='text',author_agent_id=bot,author_name_snapshot='Synthetic',execution={},content='shared synthetic',evidence=[],state='draft',state_revision=1,link_report='unchecked',created_at=now))
  shared_agent=Agent(user_id=owner,name='shared',description='x',system_prompt='x');connection=ProviderConnection(user_id=owner,kind='compatible',name='Synthetic',base_url='http://127.0.0.1:1/v1',default_model_id='test',auth_required=False,enabled=True,capabilities={});db.add_all([shared_agent,connection]);db.flush()
  db.add(SharedTextRoom(owner_id=owner,issuer='synthetic',subject='synthetic',team_id='t',publication_id='p',room_id=room,agent_id=shared_agent.id,connection_id=connection.id,model_id='test'));db.commit()
 async with paired(m) as (phone,_):
  allow(m);base=f'/api/library/bots/{bot}'
  page=await phone.get(base+'/results');assert page.status_code==200 and all(x['result_id']!=rid for x in page.json()['items'])
  assert (await phone.get(base+f'/results/{room}/{rid}/1')).status_code==404

@pytest.mark.asyncio
async def test_mac_scope_endpoint_exact_device_receipt_and_legacy_default(mobile):
 m,epoch,client,owner,*_=mobile
 async with paired(m) as (phone,_):
  key=device(m);m.state['devices'][key].pop('library_read');m.state['devices'][key].pop('library_revision');m.state['devices'][key].pop('library_ops');m._save()
  status=client.get('/api/v1/remote/mobile/status',headers=headers(client,owner));assert status.status_code==200,status.text
  row=next(d for d in status.json()['devices'] if d['id']==key);assert row['library_read'] is False and row['library_revision']==0
  body=op(True);base=f'/api/v1/remote/mobile/devices/{key}/library'
  saved=client.post(base+'/operations',json=body,headers=headers(client,owner));assert saved.status_code==200,saved.text
  result=saved.json();assert result['device_id']==key and result['operation_id']==body['operation_id'] and result['library_read'] is True and result['library_revision']==1
  assert client.post(base+'/resolve',json=body,headers=headers(client,owner)).json()==result
  assert client.post(base+'/resolve',json={**body,'enabled':False},headers=headers(client,owner)).status_code==409
