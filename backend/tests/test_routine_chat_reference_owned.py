from test_room_state import room_client,headers
from test_bot_creation import private_team
from test_owner_routines import data
from app.services.routine_persistence import metadata
from app.models.collaboration import RoomEvent

def test_structured_reference_exact_owner_room_bot_revision_and_idempotency(room_client):
 client,(owner,other,project,_,_)=room_client;bot,room,_=private_team(client,owner,project)
 api=f'/api/v1/bots/{bot}/routines';one=client.post(api,headers={**headers(client,owner),'Idempotency-Key':'create-one'},json=data(False)).json()['routine']
 otherrow=client.post(api,headers={**headers(client,owner),'Idempotency-Key':'create-two'},json={**data(False),'instruction':'Other private content'}).json()['routine']
 ref={'agent_id':bot,'routine_id':one['routine_id'],'revision':one['revision']};path=f'/api/v1/conversations/{room}/events';h={**headers(client,owner),'Idempotency-Key':'ref-one'};payload={'content':'Check the selected routine','routine_ref':ref}
 sent=client.post(path,headers=h,json=payload);assert sent.status_code==201,sent.text
 replay=client.post(path,headers=h,json=payload);assert replay.status_code==201 and replay.json()['event_id']==sent.json()['event_id']
 with client.app.state.session_factory() as db:
  root=db.get(RoomEvent,sent.json()['event_id']);rows=metadata(db,owner,bot,trigger_event_id=root.event_id,room_id=room)
  selected=next(x for x in rows if x['routine_id']==one['routine_id']);unselected=next(x for x in rows if x['routine_id']==otherrow['routine_id'])
  assert selected['selected'] is True and selected['instruction']==data(False)['instruction'] and 'instruction' not in unselected
 bad={**ref,'revision':ref['revision']+1};fail=client.post(path,headers={**h,'Idempotency-Key':'bad-rev'},json={**payload,'routine_ref':bad});assert fail.status_code==409 and fail.json()['detail']['code']=='routine_reference_changed'
 foreign=client.post(path,headers={**headers(client,other),'Idempotency-Key':'other'},json=payload);assert foreign.status_code==404
 assert client.post(path,headers={**h,'Idempotency-Key':'other-bot'},json={**payload,'routine_ref':{**ref,'agent_id':bot+999}}).status_code==404

def test_selected_reference_reaches_real_scheduler_synthetic_provider(room_client):
 import time
 from app.services.providers import ProviderResult
 client,(owner,_,project,_,_)=room_client;bot,room,_=private_team(client,owner,project)
 record=client.post(f'/api/v1/bots/{bot}/routines',headers={**headers(client,owner),'Idempotency-Key':'synthetic-create'},json=data(False)).json()['routine']
 calls=[]
 async def provider(request):
  calls.append(request);return ProviderResult(text='Synthetic response')
 client.app.state.scheduler.provider=provider
 ref={'agent_id':bot,'routine_id':record['routine_id'],'revision':record['revision']}
 sent=client.post(f'/api/v1/conversations/{room}/events',headers={**headers(client,owner),'Idempotency-Key':'synthetic-ref'},json={'content':'Describe this routine','routine_ref':ref})
 assert sent.status_code==201,sent.text
 for _ in range(200):
  if calls:break
  time.sleep(.01)
 assert calls
 assert record['routine_id'] in calls[0].system and data(False)['instruction'] in calls[0].system
 assert 'read_bot_routine' in {tool['name'] for tool in calls[0].tools}
