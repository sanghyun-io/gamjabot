"""Synthetic durable-reply provenance, privacy and legacy draft boundaries."""
import importlib,json,sqlite3
import pytest
from test_room_state import room_client,headers,create_room
from app.models.models import Agent
from app.services.events import ActorRef,append_event
from app.services.context_projection import project_turn_context,ProjectionError,selected_agent_ids


def test_reply_draft_cas_receipt_and_legacy_send_guard(room_client,monkeypatch):
    client,(owner,other,project,agent,_)=room_client;own=headers(client,owner)
    room=create_room(client,owner,project,'Reply');client.post(f'/api/v1/conversations/{room}/members/{agent}',headers=own)
    with client.app.state.session_factory() as db:
        source=append_event(db,room,ActorRef('agent',agent),'message',{'content':'Synthetic answer'})
        ref={'event_id':source.event_id,'seq':source.seq}
    path=f'/api/v1/conversations/{room}/draft'
    selected=client.put(path,headers=own,json={'content':'','expected_version':0,'reply_to':ref}).json()
    assert selected['reply_to']==ref and selected['idempotency_key'] is None and selected['version']==1
    value={'content':'Follow up','expected_version':1,'reply_to':ref}
    saved=client.put(path,headers=own,json=value).json()
    assert saved['reply_to']==ref and saved['idempotency_key'] and saved['version']==2
    assert client.put(path,headers=own,json=value).json()==saved
    assert client.put(path,headers=own,json={'content':'Follow up','expected_version':2}).status_code==409
    changed=client.put(path,headers=own,json={'content':'Follow up','expected_version':2,'reply_to':None}).json()
    assert changed['reply_to'] is None and changed['idempotency_key']!=saved['idempotency_key']
    again=client.put(path,headers=own,json={'content':'Follow up','expected_version':3,'reply_to':ref}).json()
    assert again['idempotency_key']!=changed['idempotency_key']
    async def no_schedule(*args,**kwargs):return []
    monkeypatch.setattr(client.app.state.scheduler,'schedule_user_event',no_schedule)
    endpoint=f'/api/v1/conversations/{room}/events';h={**own,'Idempotency-Key':again['idempotency_key']}
    assert client.post(endpoint,headers=h,json={'content':'Follow up'}).status_code==409
    sent=client.post(endpoint,headers=h,json={'content':'Follow up','reply_to':ref})
    assert sent.status_code==201 and sent.json()['payload']['reply_to']==ref
    with client.app.state.session_factory() as db:
        db.get(Agent,agent).is_active=False;db.commit()
    replay=client.post(endpoint,headers=h,json={'content':'Follow up','reply_to':ref})
    assert replay.status_code==201 and replay.json()['event_id']==sent.json()['event_id']
    assert client.post(endpoint,headers=h,json={'content':'Follow up','reply_to':{**ref,'seq':ref['seq']+1}}).status_code==409
    assert client.get(path,headers=headers(client,other)).status_code==404


def test_reply_target_same_room_message_and_routing(room_client,monkeypatch):
    client,(owner,_,project,agent,_)=room_client;own=headers(client,owner)
    room=create_room(client,owner,project,'Reply source');other=create_room(client,owner,project,'Other')
    client.post(f'/api/v1/conversations/{room}/members/{agent}',headers=own)
    with client.app.state.session_factory() as db:
        source=append_event(db,room,ActorRef('agent',agent),'message',{'content':'Synthetic'})
        target={'event_id':source.event_id,'seq':source.seq}
        tool=append_event(db,room,ActorRef('tool',None),'tool_result',{'text':'tool'})
        bad={'event_id':tool.event_id,'seq':tool.seq}
    path=f'/api/v1/conversations/{room}/draft'
    assert client.put(path,headers=own,json={'content':'x','expected_version':0,'reply_to':bad}).status_code==404
    assert client.put(f'/api/v1/conversations/{other}/draft',headers=own,json={'content':'x','expected_version':0,'reply_to':target}).status_code==404
    class Bot:
        def __init__(self,id,name):self.id=id;self.name=name
    bots=[Bot(agent,'Ada'),Bot(agent+100,'Beta')]
    assert selected_agent_ids(bots,'ordinary',agent)==[agent]
    assert selected_agent_ids(bots,'@Beta override',agent)==[agent+100]
    with pytest.raises(ValueError):selected_agent_ids(bots,'ordinary',agent+500)


def test_exact_visible_reply_context_and_forget_boundary():
    source={'event_id':'source','room_id':1,'seq':1,'actor_type':'agent','actor_id':2,'actor_name_snapshot':'Bot','kind':'message',
        'payload':{'content':'<think>SECRET</think>\nVISIBLE\n'+'x'*5000}}
    active={'event_id':'active','room_id':1,'seq':50,'actor_type':'user','actor_id':1,'actor_name_snapshot':'Owner','kind':'message',
        'payload':{'content':'question','reply_to':{'event_id':'source','seq':1}}}
    kwargs={'extractive_checkpoints':[],'watermark_seq':50,'active_event_id':'active','agent_id':2,'system':'role','memory':{},'tools':[]}
    result=project_turn_context(events=[source,active],**kwargs)
    wire=json.dumps(result.messages,ensure_ascii=False)
    assert 'SECRET' not in wire and 'VISIBLE' in wire and 'x'*5000 in wire
    with pytest.raises(ProjectionError) as error:
        project_turn_context(events=[source,active],memory_binding={'context_after_seq':1},**kwargs)
    assert error.value.code=='reply_context_unavailable'
    large={**source,'payload':{'content':'x'*100000}}
    with pytest.raises(ProjectionError) as error:project_turn_context(events=[large,active],**kwargs)
    assert error.value.code=='context_too_large'


def test_migration_preserves_existing_draft_rows_and_keys():
    db=sqlite3.connect(':memory:');db.execute('PRAGMA foreign_keys=ON')
    db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY)')
    db.execute('CREATE TABLE conversations(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id))')
    db.execute('INSERT INTO users VALUES (1)');db.execute('INSERT INTO conversations VALUES (1,1)')
    importlib.import_module('app.core.migrations.0012_room_drafts').upgrade(db)
    db.execute("INSERT INTO room_drafts(owner_id,room_id,content,version,idempotency_key) VALUES (1,1,'original',3,'stable-key')")
    before=db.execute('SELECT owner_id,room_id,content,version,idempotency_key,updated_at FROM room_drafts').fetchall()
    importlib.import_module('app.core.migrations.0046_room_reply').upgrade(db)
    assert db.execute('SELECT owner_id,room_id,content,version,idempotency_key,updated_at FROM room_drafts').fetchall()==before
    assert db.execute('SELECT reply_to_event_id,reply_to_seq FROM room_drafts').fetchall()==[(None,None)]
    assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    db.close()


def test_direct_retry_retains_and_revalidates_reply_reference(retry_client):
    from datetime import datetime
    from uuid import uuid4
    from app.models.collaboration import RoomEvent
    from app.services.request_retry import build_retry_preflight,execute_retry
    client,ids=retry_client
    with client.app.state.session_factory() as db:
        request=db.get(RoomEvent,ids['request']);failure=db.get(RoomEvent,ids['failure'])
        failure.seq=3;db.flush();request.seq=2;db.flush()
        source=RoomEvent(event_id=str(uuid4()),room_id=ids['room'],seq=1,actor_type='user',actor_id=ids['owner'],
            actor_name_snapshot='Owner',kind='message',payload={'content':'synthetic original'},created_at=datetime(2026,10,1))
        db.add(source);db.flush();ref={'event_id':source.event_id,'seq':1}
        request.payload={**request.payload,'reply_to':ref};db.commit()
        plan=build_retry_preflight(db,ids['owner'],ids['room'],ids['failure'])
        assert plan['eligible']
        receipt=execute_retry(db,ids['owner'],ids['room'],ids['failure'],plan['plan_hash'],'reply-retry')
        retry=db.get(RoomEvent,receipt['retry_event_id'])
        assert retry.payload['reply_to']==ref and retry.payload['retry_of_trigger_event_id']==ids['request']

from test_request_retry import retry_client


def test_system_scope_failure_is_inspected_and_can_retry_after_rejoin(retry_client):
    from datetime import datetime
    from uuid import uuid4
    from app.models.collaboration import RoomEvent,RoomTrigger,RoomAgentRun
    from app.services.request_inspector import build_request_inspector
    from app.services.request_retry import build_retry_preflight,execute_retry
    client,ids=retry_client
    with client.app.state.session_factory() as db:
        request=db.get(RoomEvent,ids['request']);failure=db.get(RoomEvent,ids['failure'])
        failure.seq=3;db.flush();request.seq=2;db.flush()
        source=RoomEvent(event_id=str(uuid4()),room_id=ids['room'],seq=1,actor_type='agent',actor_id=ids['target'],
            actor_name_snapshot='Target historical',kind='message',payload={'content':'Synthetic visible'},created_at=datetime(2026,10,1))
        db.add(source);db.flush();ref={'event_id':source.event_id,'seq':1};request.payload={**request.payload,'reply_to':ref}
        trigger=db.get(RoomTrigger,ids['request']);trigger.selected_agents=[ids['target']];trigger.state='cancelled';trigger.current_round=1
        run=db.get(RoomAgentRun,(ids['room'],ids['target']))
        failure.actor_type='system';failure.actor_id=None;failure.actor_name_snapshot='System'
        failure.idempotency_key=f"reply-scope:{ids['request']}:{ids['target']}:{run.generation}"
        failure.payload={'category':'reply_target_unavailable','code':'reply_target_unavailable','version':1,
            'trigger_event_id':ids['request'],'agent_id':ids['target'],'generation':run.generation,'round':1,
            'source_event_id':source.event_id,'message':'The replied-to Bot is no longer available in this room.'}
        db.commit()
        view=build_request_inspector(db,ids['owner'],ids['room'],ids['request'])
        assert view['completion']['outcome_status']=='failed'
        assert len(view['outcomes'])==1 and view['outcomes'][0]['actor_type']=='system'
        plan=build_retry_preflight(db,ids['owner'],ids['room'],ids['failure'])
        assert plan['eligible']
        result=execute_retry(db,ids['owner'],ids['room'],ids['failure'],plan['plan_hash'],'system-retry')
        assert db.get(RoomEvent,result['retry_event_id']).payload['reply_to']==ref


@pytest.mark.asyncio
async def test_reply_scope_loss_records_system_failure_without_agent_turn(room_client,monkeypatch):
    from app.models.collaboration import RoomEvent,RoomTrigger,RoomMembership
    from app.services.request_inspector import build_request_inspector
    client,(owner,_,project,agent,_)=room_client;own=headers(client,owner)
    room=create_room(client,owner,project,'Scope loss');client.post(f'/api/v1/conversations/{room}/members/{agent}',headers=own)
    with client.app.state.session_factory() as db:
        source=append_event(db,room,ActorRef('agent',agent),'message',{'content':'visible'})
        root=append_event(db,room,ActorRef('user',owner),'message',{'content':'follow up','reply_to':{'event_id':source.event_id,'seq':source.seq}})
        root_id=root.event_id;db.add(RoomTrigger(event_id=root_id,room_id=room,selected_agents=[agent],max_rounds=1,max_tool_calls=0,max_executor_seconds=60));db.commit()
    async def lost_scope(*args,**kwargs):
        with client.app.state.session_factory() as db:
            db.get(RoomMembership,(room,agent)).is_active=False;db.commit()
        return 'pass'
    monkeypatch.setattr(client.app.state.scheduler,'_turn',lost_scope)
    await client.app.state.scheduler._run_trigger(root_id)
    with client.app.state.session_factory() as db:
        failure=db.query(RoomEvent).filter_by(room_id=room,actor_type='system',kind='failure').one()
        assert failure.payload['code']=='reply_target_unavailable' and failure.actor_id is None
        view=build_request_inspector(db,owner,room,root_id)
        assert view['completion']['outcome_status']=='failed' and view['outcomes'][0]['actor_type']=='system'


def test_reply_thread_keyset_snapshot_and_owner_scope(room_client):
    from app.models.collaboration import RoomEvent
    from app.models.models import Conversation
    client,(owner,other,project,agent,foreign)=room_client;auth=headers(client,owner)
    room=create_room(client,owner,project,'Thread');other_room=create_room(client,owner,project,'Other')
    client.post(f'/api/v1/conversations/{room}/members/{agent}',headers=auth)
    with client.app.state.session_factory() as db:
        root=append_event(db,room,ActorRef('agent',agent),'message',{'content':'ROOT'})
        root_id,root_seq=root.event_id,root.seq;ref={'event_id':root_id,'seq':root_seq}
        another=append_event(db,room,ActorRef('user',owner),'message',{'content':'OTHER ROOT'})
        expected=[]
        for i in range(5):
            who=ActorRef('user',owner) if i%2==0 else ActorRef('agent',agent)
            reply=append_event(db,room,who,'message',{'content':f'REPLY-{i}','reply_to':ref})
            expected.append((reply.event_id,reply.seq))
        append_event(db,room,ActorRef('user',owner),'message',{'content':'OTHER REPLY','reply_to':{'event_id':another.event_id,'seq':another.seq}})
        malformed=append_event(db,room,ActorRef('agent',agent),'message',{'content':'EXTRA','reply_to':ref})
        malformed.payload={'content':'EXTRA','reply_to':{**ref,'extra':True}}
        wrong=append_event(db,room,ActorRef('agent',agent),'message',{'content':'WRONG','reply_to':ref})
        wrong.payload={'content':'WRONG','reply_to':{'event_id':root_id,'seq':root_seq+1}}
        forged=append_event(db,room,ActorRef('agent',agent),'message',{'content':'FOREIGN','reply_to':ref})
        forged.actor_id=foreign
        invalid_content=append_event(db,room,ActorRef('agent',agent),'message',{'content':'INVALID','reply_to':ref})
        invalid_content.payload={'content':{'not':'text'},'reply_to':ref};db.commit()
    path=f'/api/v1/conversations/{room}/events/{root_id}/replies';params={'root_seq':root_seq,'limit':2}
    with client.app.state.session_factory() as db:before_count=db.query(RoomEvent).filter_by(room_id=room).count()
    response=client.get(path,headers=auth,params=params);assert response.status_code==200,response.text
    with client.app.state.session_factory() as db:assert db.query(RoomEvent).filter_by(room_id=room).count()==before_count
    first=response.json();assert first['root']==ref and first['has_more'] and len(first['items'])==2
    upper=first['upper_seq'];items=list(first['items']);cursor=first['next_after_seq']
    with client.app.state.session_factory() as db:
        late=append_event(db,room,ActorRef('user',owner),'message',{'content':'LATE','reply_to':ref});late_id=late.event_id
    while cursor is not None:
        response=client.get(path,headers=auth,params={**params,'upper_seq':upper,'after_seq':cursor});assert response.status_code==200,response.text
        page=response.json();assert page['upper_seq']==upper;items.extend(page['items']);cursor=page['next_after_seq']
        assert page['has_more']==(cursor is not None)
    assert [(item['event_id'],item['seq']) for item in items]==expected
    fresh=client.get(path,headers=auth,params={'root_seq':root_seq}).json()
    assert late_id in [item['event_id'] for item in fresh['items']] and fresh['upper_seq']>upper
    assert client.get(path,headers=headers(client,other),params=params).status_code==404
    assert client.get(f'/api/v1/conversations/{other_room}/events/{root_id}/replies',headers=auth,params=params).status_code==404
    assert client.get(path,headers=auth,params={'root_seq':root_seq+1}).status_code==404
    assert client.get(path,headers=auth,params={'root_seq':root_seq,'after_seq':2}).status_code==422
    assert client.get(path,headers=auth,params={'root_seq':root_seq,'upper_seq':upper,'after_seq':upper+1}).status_code==422
    assert client.get(path,headers=auth,params={'root_seq':root_seq,'limit':101}).status_code==422
    assert client.get(path,headers=auth,params={'root_seq':root_seq,'upper_seq':2**80}).status_code==422
    assert client.get(f'/api/v1/conversations/{room}/events/not-a-uuid/replies',headers=auth,params=params).status_code==422
    with client.app.state.session_factory() as db:
        db.get(Conversation,room).is_active=False;db.commit()
    assert client.get(path,headers=auth,params=params).status_code==200
    with client.app.state.session_factory() as db:
        db.delete(db.get(RoomEvent,root_id));db.commit()
    assert client.get(path,headers=auth,params=params).status_code==404
