"""Focused actual scheduler/queue contracts with fake models and no OS effects."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
import pytest
from test_room_state import room_client,headers
from test_bot_messages import cross_team
from test_teammates import team
from app.models.bot_batch import BotBatch
from app.models.bot_message import BotMessage
from app.models.collaboration import RoomEvent,RoomAgentRun,RoomTrigger,ToolAction
from app.services.providers import ProviderResult,ToolCall
from app.services.events import ActorRef,append_event
from app.services import bot_batches,teammates
from app.services.llm_service import LLMFailure


def batch_call(tasks,call_id='fan-out'):
    return ProviderResult(tool_calls=[ToolCall(call_id,'message_bots',{'tasks':[
        {'agent_id':aid,'content':body} for aid,body in tasks]})],continuation={'call_ids':[call_id]})


def batches(client,owner,room):
    r=client.get(f'/api/v1/bots/conversations/{room}/batches',headers=headers(client,owner))
    assert r.status_code==200,r.text
    return r.json()['batches']


async def wait_release(event):
    while not event.is_set():await asyncio.sleep(.005)


def test_parallel_real_replies_join_once_and_new_owner_request_is_not_lost(room_client):
    client,(owner,_,project,_,_)=room_client
    room,(ada,ben,cy),personal=cross_team(client,owner,project,nested=True)
    entered={aid:threading.Event() for aid in (ben,cy)};release={aid:threading.Event() for aid in (ben,cy)}
    source=[];private_called=threading.Event();root_id=[];active=0;peak=0
    async def provider(request):
        nonlocal active,peak
        body=request.system+json.dumps(request.events,ensure_ascii=False)
        if request.agent_id==ada:
            source.append(request)
            if not root_id:root_id.append(request.trigger_event_id)
            if 'B_REAL_REPLY' in body and 'C_REAL_REPLY' in body:
                assert request.continuation is None and not request.observations
                assert 'B_NEW_PRIVATE_USER' not in body and 'B_PRIVATE_ANSWER' not in body
                return ProviderResult(text='A_FINAL_WITH_B_REAL_REPLY_AND_C_REAL_REPLY')
            return batch_call([(ben,'Ben exact task'),(cy,'Cy exact task')])
        if request.agent_id==ben and 'B_NEW_PRIVATE_USER' in body:
            private_called.set();return ProviderResult(text='B_PRIVATE_ANSWER')
        assert request.memory is None
        active+=1;peak=max(peak,active);entered[request.agent_id].set()
        try:await wait_release(release[request.agent_id])
        finally:active-=1
        return ProviderResult(text='B_REAL_REPLY' if request.agent_id==ben else 'C_REAL_REPLY')
    client.app.state.scheduler.provider=provider
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(client.post,f'/api/v1/conversations/{room}/messages',headers=headers(client,owner),json={'content':'@Ada delegate both tasks and synthesize their actual replies'})
        try:
            assert entered[ben].wait(5) and entered[cy].wait(5)
            pending=batches(client,owner,room)[0]
            assert pending['reply_state']=='none' and pending['return_ref'] is None and len(source)==1
            private=pool.submit(client.post,f'/api/v1/conversations/{personal[ben]}/messages',headers=headers(client,owner),json={'content':'@Ben B_NEW_PRIVATE_USER'})
            time.sleep(.05);assert not private_called.is_set()
            release[ben].set()
            assert private.result(timeout=10).status_code==201 and private_called.is_set()
            assert not first.done() and len(source)==1
        finally:
            release[ben].set();release[cy].set()
        result=first.result(timeout=10)
    assert result.status_code==201,result.text
    row,=batches(client,owner,room)
    assert peak==2 and row['state']=='completed' and row['reply_state']=='delivered' and len(source)==2
    assert [m['index'] for m in row['members']]==[0,1]
    assert all(m['resolved_result_ref']['actor_id']==m['recipient_agent_id'] for m in row['members'])
    inspector=client.get(f'/api/v1/conversations/{room}/requests/{root_id[0]}',headers=headers(client,owner)).json()
    assert inspector['completion']['outcome_status']=='succeeded' and inspector['completion']['pending_agent_count']==0,inspector
    assert any(o['event_id']==row['return_ref']['event_id'] for o in inspector['outcomes'])


def test_one_branch_stop_is_partial_but_root_stop_never_resumes_parent(room_client):
    client,(owner,_,project,_,_)=room_client
    room,(ada,ben,cy),personal=cross_team(client,owner,project,nested=True)
    entered=threading.Event();cy_done=threading.Event();cancelled=threading.Event();source=[]
    async def provider(request):
        body=json.dumps(request.events,ensure_ascii=False)
        if request.agent_id==ada:
            source.append(request)
            if 'C_SURVIVED' in body:
                assert 'cancelled' in body
                return ProviderResult(text='C_SURVIVED; Ben did not provide a reply.')
            return batch_call([(ben,'Long Ben task'),(cy,'Cy independent task')])
        if request.agent_id==cy:cy_done.set();return ProviderResult(text='C_SURVIVED')
        entered.set()
        try:await asyncio.Event().wait()
        except asyncio.CancelledError:cancelled.set();raise
    client.app.state.scheduler.provider=provider
    with ThreadPoolExecutor(max_workers=1) as pool:
        task=pool.submit(client.post,f'/api/v1/conversations/{room}/messages',headers=headers(client,owner),json={'content':'@Ada run both independently'})
        assert entered.wait(5) and cy_done.wait(5)
        assert client.post(f'/api/v1/conversations/{personal[ben]}/cancel',headers=headers(client,owner)).status_code==204
        result=task.result(timeout=10)
    row,=batches(client,owner,room)
    assert result.status_code==201 and cancelled.is_set() and len(source)==2
    assert row['state']=='partial' and row['reply_state']=='delivered'
    failed=next(m for m in row['members'] if m['recipient_agent_id']==ben)
    assert failed['resolved_result_ref'] is None and failed['failure_code']=='cancelled'
    inspector=client.get(f"/api/v1/conversations/{room}/requests/{row['root_ref']['event_id']}",headers=headers(client,owner)).json()
    assert inspector['completion']['outcome_status']=='partial'
    room2,(a2,b2,c2),_=cross_team(client,owner,project,nested=True)
    both={b2:threading.Event(),c2:threading.Event()};a2calls=[]
    async def stopped_provider(request):
        if request.agent_id==a2:a2calls.append(True);return batch_call([(b2,'B2'),(c2,'C2')])
        both[request.agent_id].set();await asyncio.Event().wait()
    client.app.state.scheduler.provider=stopped_provider
    with ThreadPoolExecutor(max_workers=1) as pool:
        task=pool.submit(client.post,f'/api/v1/conversations/{room2}/messages',headers=headers(client,owner),json={'content':'@Ada stop this entire root'})
        assert both[b2].wait(5) and both[c2].wait(5)
        assert client.post(f'/api/v1/conversations/{room2}/cancel',headers=headers(client,owner)).status_code==204
        task.result(timeout=10)
    stopped,=batches(client,owner,room2)
    assert a2calls==[True] and stopped['reply_state']=='cancelled' and stopped['return_ref'] is None
    # The same bounded wait ends when its original root deadline expires,
    # including while both provider calls are still in flight.
    room3,(a3,b3,c3),_=cross_team(client,owner,project,nested=True)
    both3={b3:threading.Event(),c3:threading.Event()};a3calls=[]
    async def expired_provider(request):
        if request.agent_id==a3:a3calls.append(True);return batch_call([(b3,'B3'),(c3,'C3')])
        both3[request.agent_id].set();await asyncio.Event().wait()
    client.app.state.scheduler.provider=expired_provider
    with ThreadPoolExecutor(max_workers=1) as pool:
        task=pool.submit(client.post,f'/api/v1/conversations/{room3}/messages',headers=headers(client,owner),json={'content':'@Ada keep the root deadline'})
        assert both3[b3].wait(5) and both3[c3].wait(5)
        from datetime import datetime,timedelta,timezone
        from app.models.execution_scope import ExecutionScope
        with client.app.state.session_factory() as db:
            group=db.query(BotBatch).filter_by(sender_agent_id=a3).one()
            past=datetime.now(timezone.utc)-timedelta(seconds=1);group.deadline_at=past
            for member in db.query(BotMessage).filter_by(batch_id=group.batch_id):member.deadline_at=past
            for scope in db.query(ExecutionScope).filter_by(root_request_event_id=group.root_request_event_id):scope.deadline_at=past
            db.commit()
        task.result(timeout=10)
    expired,=batches(client,owner,room3)
    assert a3calls==[True] and expired['reply_state']=='failed' and expired['return_ref'] is None
    assert expired['failure_code']=='batch_deadline_exceeded'


def test_nested_batches_share_two_slots_and_quote_only_actual_final_contributions(room_client):
    client,(owner,_,project,_,_)=room_client
    room,(ada,ben,cy),personal=cross_team(client,owner,project,nested=True)
    dee,eli=team(client,owner,room,('Dee','Eli'))
    for aid in (dee,eli):
        assert client.delete(f'/api/v1/conversations/{room}/members/{aid}',headers=headers(client,owner)).status_code==204
        personal[aid]=client.post(f'/api/v1/bots/{aid}/conversation',headers=headers(client,owner),json={}).json()['room_id']
    for aid in (ben,dee):
        with client.app.state.session_factory() as db:
            append_event(db,personal[aid],ActorRef('user',owner),'message',{'content':'PRIVATE_BATCH_CANARY_NEVER_FORWARD'})
    release_c=threading.Event();b_finished=threading.Event();source_calls=[];active=0;peak=0
    async def provider(request):
        nonlocal active,peak
        body=request.system+json.dumps(request.events,ensure_ascii=False)
        if request.agent_id==ada:
            source_calls.append(request)
            if 'B_FINAL_WITH_DE' in body and 'C_REAL_NESTED' in body:return ProviderResult(text='A_FINAL_ALL_REAL')
            return batch_call([(ben,'B exact delegated task'),(cy,'C independent task')])
        active+=1;peak=max(peak,active)
        try:
            assert 'PRIVATE_BATCH_CANARY_NEVER_FORWARD' not in body and request.memory is None
            if request.agent_id==ben:
                if 'DEE_REAL' in body and 'ELI_REAL' in body:
                    assert request.continuation is None and not request.observations
                    b_finished.set();return ProviderResult(text='B_FINAL_WITH_DE')
                return batch_call([(dee,'Dee exact task'),(eli,'Eli exact task')],'nested-fan-out')
            if request.agent_id==cy:await wait_release(release_c);return ProviderResult(text='C_REAL_NESTED')
            await asyncio.sleep(.01);return ProviderResult(text='DEE_REAL' if request.agent_id==dee else 'ELI_REAL')
        finally:active-=1
    client.app.state.scheduler.provider=provider
    with ThreadPoolExecutor(max_workers=1) as pool:
        task=pool.submit(client.post,f'/api/v1/conversations/{room}/messages',headers=headers(client,owner),json={'content':'@Ada delegate, wait for all nested results, then synthesize'})
        try:
            assert b_finished.wait(5)
            assert not task.done() and len(source_calls)==1
        finally:release_c.set()
        result=task.result(timeout=10)
    assert result.status_code==201 and peak<=2 and len(source_calls)==2,result.text
    rows=batches(client,owner,room);assert len(rows)==2 and all(r['reply_state']=='delivered' for r in rows),rows
    outer=next(r for r in rows if r['sender_agent_id']==ada);inner=next(r for r in rows if r['sender_agent_id']==ben)
    b=next(m for m in outer['members'] if m['recipient_agent_id']==ben)
    assert b['result_ref']['kind']=='activity' and b['resolved_result_ref']==inner['return_ref']
    assert inner['source_room']['room_id']==personal[ben] and inner['root_ref']==outer['root_ref']
    singles=client.get(f'/api/v1/bots/conversations/{room}/messages',headers=headers(client,owner)).json()
    assert singles['messages']==[]


def test_atomic_validation_replay_budget_and_mixed_call_correction(room_client):
    client,(owner,_,project,_,_)=room_client
    from test_execution_scopes import seed
    s=seed(client,owner,project)
    args={'tasks':[{'agent_id':s.ada,'content':'A task'},{'agent_id':s.cy,'content':'C task'}]}
    with client.app.state.session_factory() as db:
        before=db.query(RoomEvent).count()
        bad={'tasks':[args['tasks'][0],args['tasks'][0]]}
        with pytest.raises(LLMFailure):bot_batches.send(db,s.personal[s.ben],s.room,s.root,s.ben,s.gen,'atomic',bad,parent_job_id=s.message_id,parent_kind='bot_message')
        assert db.query(BotBatch).count()==0 and db.query(RoomEvent).count()==before
        first=bot_batches.send(db,s.personal[s.ben],s.room,s.root,s.ben,s.gen,'atomic',args,parent_job_id=s.message_id,parent_kind='bot_message')
        count=db.query(RoomEvent).count()
        replay=bot_batches.send(db,s.personal[s.ben],s.room,s.root,s.ben,s.gen,'atomic',args,parent_job_id=s.message_id,parent_kind='bot_message')
        assert replay['batch_id']==first['batch_id'] and db.query(RoomEvent).count()==count
        bot_batches.send(db,s.personal[s.ben],s.room,s.root,s.ben,s.gen,'second',args,parent_job_id=s.message_id,parent_kind='bot_message')
        three={'tasks':[*args['tasks'],{'agent_id':s.dana,'content':'D task'}]}
        bot_batches.send(db,s.personal[s.ben],s.room,s.root,s.ben,s.gen,'third',three,parent_job_id=s.message_id,parent_kind='bot_message')
        assert db.query(BotMessage).filter_by(root_request_event_id=s.root).count()==8
        with pytest.raises(LLMFailure):bot_batches.send(db,s.personal[s.ben],s.room,s.root,s.ben,s.gen,'over',args,parent_job_id=s.message_id,parent_kind='bot_message')
    from app.models.teammate import TeammateBudget
    with client.app.state.session_factory() as db:
        db.get(TeammateBudget,s.root).provider_calls=31;db.commit()
    def provider_slot(_):
        with client.app.state.session_factory() as db:
            try:teammates.claim_provider_call(db,s.root);return True
            except LLMFailure:return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(provider_slot,[0,1]))==[False,True]
    with client.app.state.session_factory() as db:assert db.get(TeammateBudget,s.root).provider_calls==32
    room,(ada,ben,cy),_=cross_team(client,owner,project,nested=True)
    calls=[]
    async def provider(request):
        body=json.dumps(request.events,ensure_ascii=False)
        if request.agent_id!=ada:return ProviderResult(text='MIX_B_REAL' if request.agent_id==ben else 'MIX_C_REAL')
        calls.append(request)
        if request.observations:
            assert len(request.observations)==2 and all(o.state=='denied' for o in request.observations)
            with client.app.state.session_factory() as db:
                assert db.query(BotBatch).filter_by(root_request_event_id=request.trigger_event_id).count()==0
                assert db.query(ToolAction).filter_by(trigger_event_id=request.trigger_event_id).count()==0
            return batch_call([(ben,'B corrected'),(cy,'C corrected')],'corrected')
        if 'MIX_B_REAL' in body and 'MIX_C_REAL' in body:
            assert request.continuation is None
            return ProviderResult(text='MIX_FINAL_ACTUAL')
        mixed=batch_call([(ben,'B invalid mix'),(cy,'C invalid mix')],'mixed-batch')
        return ProviderResult(tool_calls=[*mixed.tool_calls,ToolCall('mixed-single','message_bot',{'agent_id':ben,'content':'Must not run'})],
            continuation={'call_ids':['mixed-batch','mixed-single']})
    client.app.state.scheduler.provider=provider
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=headers(client,owner),json={'content':'@Ada use one atomic batch'})
    assert response.status_code==201 and len(calls)==3,response.text
    row,=batches(client,owner,room);assert row['reply_state']=='delivered' and len(row['members'])==2
