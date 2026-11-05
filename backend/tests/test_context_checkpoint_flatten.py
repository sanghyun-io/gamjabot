from test_room_state import room_client
import json
from types import SimpleNamespace
from app.services.context_projection import _extractive_checkpoint, project_turn_context, EVENT_CHARS
from app.services.execution_origin import EventReference


def event(seq, content, *, kind='message', actor='user'):
    return {'event_id':f'event-{seq}', 'room_id':1, 'seq':seq, 'actor_type':actor,
            'actor_id':1 if actor != 'tool' else None, 'actor_name_snapshot':'Synthetic',
            'kind':kind, 'payload':{'content':content}}


def test_managed_checkpoint_does_not_inherit_or_reuse_old_nested_content():
    older = [event(i, f'PHYSICAL_{i}_' + 'x'*220) for i in range(1,41)]
    old = {'event_id':'old','room_id':1,'seq':41,'actor_type':'system','actor_id':None,
           'actor_name_snapshot':'System','kind':'summary',
           'payload':{'extractive_checkpoint':True,'source_start_seq':1,'source_end_seq':40,
                      'content':'NESTED_OLD_SUMMARY_DO_NOT_CARRY'}}
    checkpoint, plan = _extractive_checkpoint(older, [old], 50)
    assert plan.idempotency_key == 'extractive-context-v2:40'
    assert checkpoint['payload']['content'].count('Extractive room checkpoint') == 1
    assert 'NESTED_OLD_SUMMARY_DO_NOT_CARRY' not in plan.content
    assert 'PHYSICAL_40_' in plan.content and len(plan.content) <= EVENT_CHARS
    assert 'Only selected recent' in plan.content
    again, second = _extractive_checkpoint(older, [old, checkpoint], 50)
    assert second.idempotency_key == plan.idempotency_key and again['payload']['content'] == plan.content
    assert plan.idempotency_key != 'extractive-context:40'


def test_scoped_floor_retired_root_tool_and_verified_peer_are_preserved():
    events = [event(i, f'OLD_PHYSICAL_{i}') for i in range(1,45)]
    events[9] = event(10, 'PRIVATE_FLOOR_SECRET')
    events[24] = event(25, 'RETIRED_SECRET', actor='agent')
    events.append(event(45, 'CURRENT_TOOL_RESULT', kind='tool_result', actor='tool'))
    events.append(event(46, 'CURRENT_OWNER_ROOT'))
    old = {'event_id':'old-summary','room_id':1,'seq':44,'actor_type':'system','actor_id':None,
           'actor_name_snapshot':'System','kind':'summary', 'payload':{'extractive_checkpoint':True,
           'source_start_seq':1,'source_end_seq':44,'content':'NESTED_PRIVATE_SECRET'}}
    events.append(old)
    binding = {'context_after_seq':10,'retired_persistence_responses':[{'event_id':'event-25','seq':25}]}
    userref = EventReference(1,'root-reference',46,'user',1,'message')
    taskref = EventReference(1,'task-reference',44,'agent',1,'message')
    peerref = EventReference(2,'peer-reference',7,'agent',2,'message')
    origin = SimpleNamespace(fingerprint='synthetic', context_mode='delegated', root_ref=userref, task_content='SAFE_TASK',
        source_task_ref=taskref, task_ref=taskref, peer_result_ref=peerref, peer_result_content='VERIFIED_PEER_RESULT',
        peer_result_omitted_chars=0, peer_result_original_chars=20, peer_result_included_chars=20,
        failure_code=None, batch_id=None, execution_room_id=1, agent_id=1, root_request_event_id='event-46')
    projection = project_turn_context(events=events,extractive_checkpoints=[old],watermark_seq=46,
        active_event_id='event-46',agent_id=1,system='role',memory={},tools=[],memory_binding=binding,
        size_for_candidate=lambda system,messages: 100 if len(messages)<=7 else 40000, execution_origin=origin)
    text=json.dumps(projection.messages,ensure_ascii=False)
    assert 'CURRENT_OWNER_ROOT' in text and 'CURRENT_TOOL_RESULT' in text and 'VERIFIED_PEER_RESULT' in text
    assert 'PRIVATE_FLOOR_SECRET' not in text and 'RETIRED_SECRET' not in text and 'NESTED_PRIVATE_SECRET' not in text
    assert projection.checkpoint_plan and projection.checkpoint_plan.source_start_seq == 11
    assert len(projection.checkpoint_plan.content) <= EVENT_CHARS


def test_old_and_new_checkpoint_keys_do_not_collide(room_client):
    from test_bot_creation import private_team
    from app.services.events import append_event, ActorRef
    client,(owner,_,project,_,_)=room_client
    _,room,_=private_team(client,owner,project)
    older=[event(i, f'SYNTHETIC_{i}') for i in range(1,4)]
    _,plan=_extractive_checkpoint(older,[],3)
    with client.app.state.session_factory() as db:
        old=append_event(db,room,ActorRef('system',None),'summary',
            {'extractive_checkpoint':True,'source_start_seq':1,'source_end_seq':3,'content':'old'},
            idempotency_key='extractive-context:3')
        payload={'extractive_checkpoint':True,'source_start_seq':1,'source_end_seq':3,'content':plan.content}
        first=append_event(db,room,ActorRef('system',None),'summary',payload,idempotency_key=plan.idempotency_key)
        again=append_event(db,room,ActorRef('system',None),'summary',payload,idempotency_key=plan.idempotency_key)
        assert old.event_id != first.event_id and first.event_id == again.event_id
