"""Disposable SQLite checks; these do not prove native browser custody."""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from test_room_state import room_client, create_room
from test_teammates import team
from app.models.collaboration import RoomAgentRun, RoomTrigger, ToolAction
from app.services import execution_scopes as scopes, room_service, teammates
from app.services.events import ActorRef, append_event
from app.services.scheduler import _teammate_failure_readonly_park_eligible as eligible


def test_teammate_failure_park_requires_exact_live_lineage_and_readonly_result(room_client, monkeypatch):
    client,(owner,_,project,_,_)=room_client
    room=create_room(client,owner,project,'synthetic custody')
    ada,ben=team(client,owner,room)
    epoch='a'*64
    authority=SimpleNamespace(owner_id=owner,proposer=SimpleNamespace(owner_epoch=epoch))
    monkeypatch.setattr('app.services.review_snapshot.retained_review',lambda action:SimpleNamespace(authority=authority))
    monkeypatch.setattr('app.services.scheduler.observe_authority',lambda *args,**kwargs:authority)
    with client.app.state.session_factory() as db:
        root=append_event(db,room,ActorRef('user',owner),'message',{'content':'synthetic'})
        db.add(RoomTrigger(event_id=root.event_id,room_id=room,state='running',selected_agents=[ada],current_round=1,
            max_rounds=1,max_tool_calls=8,max_executor_seconds=60))
        run=db.get(RoomAgentRun,(room,ada));run.state='running';run.generation+=1;run.trigger_event_id=root.event_id
        db.commit()
        teammates.send(db,room,root.event_id,ada,run.generation,'request',{'agent_id':ben,'content':'synthetic'})
        job=teammates.next_job(db,room,root.event_id)
        generation=room_service.begin_turn(db,room,ben,trigger_event_id=root.event_id,round_number=1,watermark=1)
        # Existing scheduler creates this root for normal tool-enabled teammate turns.
        scopes.root_scope(db,root.event_id,create=True)
        proof=scopes.bind(db,room,ben,root.event_id,generation,teammate_job=job)
        action=ToolAction(action_id=str(uuid4()),room_id=room,agent_id=ben,trigger_event_id=root.event_id,
            family='browser_inspect',arguments={},state='completed',workflow_id='workflow',completed_at=datetime.now(timezone.utc),
            session_id=str(uuid4()),approval_scope='once',approval_owner_id=owner,execution_binding=proof)
        db.add(action)
        append_event(db,room,ActorRef('tool',None),'tool_result',{'trigger_event_id':root.event_id,'agent_id':ben,
            'action_id':action.action_id,'state':'completed'})
        failure=append_event(db,room,ActorRef('agent',ben),'failure',{'trigger_event_id':root.event_id,
            'generation':generation,'round':1})
        teammates.finish_job(db,job,failure)
        db.get(RoomAgentRun,(room,ben)).state='idle';db.commit()
        args=dict(room_id=room,agent_id=ben,owner_id=owner,root_id=root.event_id,generation=generation,
            round_number=1,failure_event_id=failure.event_id,calls=1,workflows={'workflow'},
            owner_epochs=SimpleNamespace(current=lambda _:epoch),proof=proof,job=job)
        assert len(proof["lineage"]) == 3
        assert scopes.require_binding(db,proof,for_start=False)
        assert eligible(db,**args)
        action.family='browser_interact';db.flush();assert not eligible(db,**args)
        action.family='browser_inspect';db.flush()
        scope=db.get(scopes.ExecutionScope,proof['scope_id']);scope.state='revoked';scope.epoch+=1;db.flush()
        assert not eligible(db,**args)
        scope.state='active';scope.epoch-=1
        parent=db.get(scopes.ExecutionScope,proof['root_scope_id']);parent.state='revoked';parent.epoch+=1;db.flush()
        assert not eligible(db,**args)


import pytest
from test_execution_scopes import seed, FakeExecutor
from app.models.execution_scope import ExecutionScope


@pytest.mark.asyncio
@pytest.mark.parametrize('ack',[True,False])
async def test_browser_only_scope_stop_requires_core_and_local_ack(room_client,ack):
    client,(owner,_,project,_,_)=room_client
    data=seed(client,owner,project);executor=FakeExecutor();calls=[]
    class Bridge:
        available=True;core_epoch='b'*64
        async def scope_revoke(self,payload):
            calls.append(payload)
            return {'state':'revoked' if ack else 'failed'}
    app=SimpleNamespace(session_factory=client.app.state.session_factory,local_executor=executor,mcp_client=Bridge(),
                        owner_epoch_provider=SimpleNamespace(current=lambda _:'c'*64))
    with app.session_factory() as db:
        action=ToolAction(action_id=str(uuid4()),room_id=data.personal[data.ben],agent_id=data.ben,
            trigger_event_id=data.root,family='browser_inspect',arguments={},state='completed',execution_binding=data.proof)
        db.add(action);scope=db.get(ExecutionScope,data.proof['scope_id']);scope.state='revoked';scope.epoch+=1;db.commit()
        stop_id=scopes._stop_receipt(db,data.room,[{'scope_id':scope.scope_id,'epoch':scope.epoch}])
    result=await scopes.confirm_stop(app,stop_id,owner)
    assert result['state']==('settled' if ack else 'unknown')
    assert len(calls)==1 and calls[0]['scopes']==result['revocations']
    assert executor.stops==result['revocations']
