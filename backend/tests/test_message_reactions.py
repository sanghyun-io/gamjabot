"""Synthetic Owner-only reaction, replay and model-context boundaries."""
from concurrent.futures import ThreadPoolExecutor
from test_room_state import room_client,headers,create_room
from app.services.events import ActorRef,append_event
from app.services.context_projection import project_turn_context


def fixture_events(client,owner,agent,room):
    with client.app.state.session_factory() as db:
        user=append_event(db,room,ActorRef('user',owner),'message',{'content':'owner synthetic'})
        user_id=user.event_id
        bot=append_event(db,room,ActorRef('agent',agent),'message',{'content':'bot synthetic'})
        bot_id=bot.event_id
        action=append_event(db,room,ActorRef('tool',None),'tool_result',{'text':'tool synthetic'})
        action_id=action.event_id
    return user_id,bot_id,action_id

def test_reaction_cas_replay_authority_and_no_turn(room_client,monkeypatch):
    client,(owner,other,project,agent,_)=room_client;own=headers(client,owner)
    room=create_room(client,owner,project,'Reactions')
    client.post(f'/api/v1/conversations/{room}/members/{agent}',headers=own)
    user,bot,tool=fixture_events(client,owner,agent,room)
    path=f'/api/v1/conversations/{room}/messages/{bot}/reaction'
    async def forbidden(*args,**kwargs):raise AssertionError('reaction scheduled an Agent')
    monkeypatch.setattr(client.app.state.scheduler,'schedule_user_event',forbidden)
    initial=client.get(path,headers=own).json()
    assert initial['current_seq']==0 and initial['current_event_id'] is None and initial['emoji'] is None
    first=client.put(path,headers={**own,'Idempotency-Key':'first'},json={'emoji':'👍','expected_seq':0})
    assert first.status_code==200,first.text
    a=first.json();assert a['emoji']=='👍' and a['current_seq']==a['applied_seq'] and not a['replayed']
    second=client.put(path,headers={**own,'Idempotency-Key':'second'},json={'emoji':'❤️','expected_seq':a['current_seq']}).json()
    assert second['current_seq']>a['current_seq'] and second['emoji']=='❤️'
    replay=client.put(path,headers={**own,'Idempotency-Key':'first'},json={'emoji':'👍','expected_seq':0}).json()
    assert replay['replayed'] and replay['applied_event_id']==a['applied_event_id'] and replay['current_seq']==second['current_seq'] and replay['emoji']=='❤️'
    receipt=client.get(path+'/commands/first',headers=own).json()
    assert receipt==replay
    conflict=client.put(path,headers={**own,'Idempotency-Key':'first'},json={'emoji':'👍','expected_seq':a['current_seq']})
    assert conflict.status_code==409 and conflict.json()['detail']['code']=='reaction_idempotency_conflict'
    stale=client.put(path,headers={**own,'Idempotency-Key':'stale'},json={'emoji':None,'expected_seq':a['current_seq']})
    assert stale.status_code==409 and stale.json()['detail']['code']=='reaction_stale'
    clear=client.put(path,headers={**own,'Idempotency-Key':'clear'},json={'emoji':None,'expected_seq':second['current_seq']}).json()
    assert clear['emoji'] is None and clear['current_seq']>second['current_seq']
    assert client.get(path+'/commands/missing',headers=own).status_code==404
    assert client.put(path,headers={**own,'Idempotency-Key':'bad'},json={'emoji':'not-emoji','expected_seq':0}).status_code==422
    assert client.put(path,headers={**own,'Idempotency-Key':'bool'},json={'emoji':'👍','expected_seq':True}).status_code==422
    assert client.get(path).status_code==401
    assert client.get(path,headers=headers(client,other)).status_code==404
    assert client.get(f'/api/v1/conversations/{room}/messages/{tool}/reaction',headers=own).status_code==404
    other_room=create_room(client,owner,project,'Other room')
    assert client.get(f'/api/v1/conversations/{other_room}/messages/{user}/reaction',headers=own).status_code==404
    messages=client.get(f'/api/v1/conversations/{room}/events',headers=own).json()
    reactions=[e for e in messages if e['kind']=='activity' and e['payload'].get('category')=='message_reaction']
    assert len(reactions)==3 and all(e['actor_type']=='user' and e['actor_id']==owner for e in reactions)
    from app.models.models import Project
    with client.app.state.session_factory() as db:
        db.get(Project,project).is_active=False;db.commit()
    assert client.get(path,headers=own).status_code==404
    with client.app.state.session_factory() as db:
        db.get(Project,project).is_active=True;db.commit()
    client.delete(f'/api/v1/conversations/{room}',headers=own)
    assert client.get(path,headers=own).status_code==404


def test_simultaneous_expected_seq_only_one_wins(room_client):
    client,(owner,_,project,agent,_)=room_client;own=headers(client,owner)
    room=create_room(client,owner,project,'Concurrent reactions')
    client.post(f'/api/v1/conversations/{room}/members/{agent}',headers=own)
    user,_,_=fixture_events(client,owner,agent,room)
    path=f'/api/v1/conversations/{room}/messages/{user}/reaction'
    def send(index):return client.put(path,headers={**own,'Idempotency-Key':f'parallel-{index}'},json={'emoji':'👀','expected_seq':0})
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(send,(1,2)))
    assert sorted(r.status_code for r in results)==[200,409]
    assert next(r for r in results if r.status_code==409).json()['detail']['code']=='reaction_stale'


def test_reaction_activity_excluded_before_recent_window_and_checkpoints():
    def message(seq):return {'event_id':f'id-{seq}','room_id':1,'seq':seq,'actor_type':'user','actor_id':1,
        'actor_name_snapshot':'Owner','kind':'message','payload':{'content':f'message {seq}'}}
    regular=[message(i) for i in range(1,41)]
    reactions=[{'event_id':f'reaction-{i}','room_id':1,'seq':i,'actor_type':'user','actor_id':1,
        'actor_name_snapshot':'Owner','kind':'activity','payload':{'category':'message_reaction','version':1,'emoji':'👍','target_event_id':'id-1','expected_seq':0}}
        for i in range(41,101)]
    active=message(101);kwargs={'extractive_checkpoints':[],'watermark_seq':101,'active_event_id':'id-101','agent_id':1,'system':'role','memory':{},'tools':[]}
    without=project_turn_context(events=[*regular,active],**kwargs)
    with_reactions=project_turn_context(events=[*regular,*reactions,active],**kwargs)
    assert without.messages==with_reactions.messages
    assert without.events==with_reactions.events
    assert without.checkpoint_plan==with_reactions.checkpoint_plan
    assert all(not str(e['event_id']).startswith('reaction-') for e in with_reactions.events)
