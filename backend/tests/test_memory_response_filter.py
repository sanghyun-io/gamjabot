"""A committed memory transition retires its confirmation from future model context only."""
import json
import pytest
from test_room_state import room_client,headers,create_room
from test_bot_creation import private_team,tool
from app.services.events import append_event,ActorRef
from app.models.collaboration import RoomEvent
from app.services.providers import ProviderResult
from app.services.memory_context import snapshot

@pytest.mark.parametrize('scope,operation',[('private','edit'),('private','delete'),('common','edit'),('common','delete')])
def test_transition_confirmation_is_excluded_without_dropping_later_owner_messages(room_client,scope,operation):
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner)
    bot,dm,_=private_team(client,owner,project)
    room=create_room(client,owner,project,'Synthetic memory room')
    assert client.post(f'/api/v1/conversations/{room}/members/{bot}',headers=own).status_code==201
    assert client.put(f'/api/v1/bots/{bot}/memory-policy',headers=own,json={'expected_revision':0,'mode':'owner_rooms'}).status_code==200
    if scope=='private':
        r=client.post(f'/api/v1/bots/{bot}/memories',headers={**own,'Idempotency-Key':'seed'},json={'expected_scope_revision':0,'category':'fact','content':'OLD_SENTINEL'})
        ident=r.json()['memory_id'];family='edit_bot_memory' if operation=='edit' else 'forget_bot_memory'
        args={'memory_id':ident,'content':'NEW_SENTINEL'} if operation=='edit' else {'memory_id':ident}
    else:
        r=client.post('/api/v1/bots/memory/common',headers={**own,'Idempotency-Key':'seed'},json={'expected_scope_revision':0,'kind':'name','content':'OLD_SENTINEL','origin':{'agent_id':bot,'source_kind':'manual'}})
        ident=r.json()['fact_id'];family='edit_owner_fact' if operation=='edit' else 'forget_owner_fact'
        args={'fact_id':ident,'content':'NEW_SENTINEL'} if operation=='edit' else {'fact_id':ident}
    assert r.status_code==200,r.text
    async def provider(request):
        if not request.observations:return tool(family,args,'transition')
        with client.app.state.session_factory() as db:
            append_event(db,room,ActorRef('user',owner),'message',{'content':'INTERLEAVED_NEW_USER_MESSAGE'})
        return ProviderResult(text='OLD_SENTINEL previous; NEW_SENTINEL confirmed')
    client.app.state.scheduler.provider=provider
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'@Ada update the synthetic memory'})
    assert response.status_code==201,response.text
    with client.app.state.session_factory() as db:
        confirmation=db.query(RoomEvent).filter_by(room_id=room,actor_type='agent',kind='message').one()
        assert 'OLD_SENTINEL' in confirmation.payload['content']
        confirmation_id=confirmation.event_id
        data=snapshot(db,room,bot)
        assert any(x['event_id']==confirmation.event_id for x in data['binding']['retired_persistence_responses'])
        # A later summary explicitly covering the retired confirmation cannot revive it.
        append_event(db,room,ActorRef('system',None),'summary',{'source_start_seq':confirmation.seq,'source_end_seq':confirmation.seq,
            'owner_checkpoint':True,'content':'SUMMARY_OLD_SENTINEL'})
    preview=client.post(f'/api/v1/conversations/{room}/context-preview',headers=own,json={'content':'@Ada report current context'})
    assert preview.status_code==200,preview.text
    messages=preview.json()['agents'][0]['messages'];serialized=json.dumps(messages,ensure_ascii=False)
    assert 'OLD_SENTINEL' not in serialized
    assert 'INTERLEAVED_NEW_USER_MESSAGE' in serialized
    if operation=='edit':assert 'NEW_SENTINEL' in serialized
    with client.app.state.session_factory() as db:
        assert db.get(RoomEvent,confirmation_id).payload['content'].startswith('OLD_SENTINEL')


def test_unbacked_activity_cannot_hide_a_message(room_client):
    client,(owner,_,project,_,_)=room_client
    bot,room,_=private_team(client,owner,project)
    with client.app.state.session_factory() as db:
        root=append_event(db,room,ActorRef('user',owner),'message',{'content':'Normal request'})
        append_event(db,room,ActorRef('system',None),'activity',{'category':'bot_persistence','status':'saved',
            'trigger_event_id':root.event_id,'bot_persistence':{'operation':'delete','agent_id':bot}})
        message=append_event(db,room,ActorRef('agent',bot),'message',{'trigger_event_id':root.event_id,'content':'NORMAL_RESULT'})
        result=snapshot(db,room,bot)
        assert not result['binding'].get('retired_persistence_responses')
        assert db.get(RoomEvent,message.event_id) is not None

@pytest.mark.asyncio
async def test_checkin_context_excludes_retired_memory_confirmation(room_client):
    from datetime import timedelta
    from app.models.primary_checkins import PrimaryCheckin
    from app.services import primary_checkins as checkins
    from test_primary_checkins import model_use
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner)
    bot,room,_=private_team(client,owner,project)
    item=client.post(f'/api/v1/bots/{bot}/memories',headers={**own,'Idempotency-Key':'checkin-seed'},json={
        'expected_scope_revision':0,'category':'fact','content':'OLD_CHECKIN_SENTINEL'}).json()
    async def edit_provider(request):
        if not request.observations:return tool('edit_bot_memory',{'memory_id':item['memory_id'],'content':'CURRENT_CHECKIN_SENTINEL'},'edit')
        return ProviderResult(text='OLD_CHECKIN_SENTINEL became CURRENT_CHECKIN_SENTINEL')
    scheduler=client.app.state.scheduler;scheduler.provider=edit_provider
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'@Ada update the memory'})
    assert response.status_code==201,response.text
    assert client.put('/api/v1/bots/primary',headers=own,json={'agent_id':bot,'expected_revision':0}).status_code==200
    cfg={'expected_revision':0,'expected_primary_revision':1,'enabled':True,'prompt':'Ask about the project','interval_minutes':1}
    assert client.put('/api/v1/bots/primary/check-ins',headers=own,json=cfg).status_code==200
    with client.app.state.session_factory() as db:
        setting=db.get(PrimaryCheckin,owner);setting.next_run_at=checkins.now()-timedelta(minutes=1);db.commit()
        rid=checkins.claim(db)
    seen=[]
    async def checkin_provider(request):
        seen.append(request);return ProviderResult(text='SYNTHETIC_CHECKIN',model_use=model_use(request))
    scheduler.provider=checkin_provider
    await checkins.execute(scheduler,rid)
    assert len(seen)==1 and 'OLD_CHECKIN_SENTINEL' not in json.dumps(seen[0].events)
    assert 'CURRENT_CHECKIN_SENTINEL' in seen[0].memory
