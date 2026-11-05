"""Isolated SQLite contract; not evidence of live Core or browser behavior."""
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
from uuid import uuid4
import pytest
from sqlalchemy import text
from test_room_state import room_client
from test_teammate_custody_authority import make_task
from app.models.collaboration import RoomAgentRun,RoomGrant,ToolAction
from app.models.teammate import TeammateHandoff
from app.services import teammates
from app.services.action_review import ReviewChanged
from app.services.custody_settled_park import require_failed_readonly
from app.services.events import ActorRef,append_event
from app.services.teammate_custody_authority import bind


def prepare(client,owner,project,monkeypatch):
    db,args,agents=make_task(client,owner,project,nested=True)
    identity=bind(db,**args);epoch='a'*64
    authority=SimpleNamespace(owner_id=owner,proposer=SimpleNamespace(owner_epoch=epoch))
    target=SimpleNamespace(model_dump=lambda **kw:{'role':'synthetic'})
    monkeypatch.setattr('app.services.custody_settled_park.retained_review',lambda action:SimpleNamespace(authority=authority,targets=[target]))
    monkeypatch.setattr('app.services.custody_settled_park.observe_authority',lambda *a,**kw:authority)
    monkeypatch.setattr('app.services.custody_settled_park.bound_authority',lambda *a:True)
    run=db.get(RoomAgentRun,(args['room_id'],args['agent_id']))
    run.pending_task=dict(run.pending_task or {},custody_teammate_job={'handoff_id':args['job']['handoff_id'],'phase':'response'})
    approved=datetime.now(timezone.utc).replace(tzinfo=None);action_id=str(uuid4());session=str(uuid4());review_id=str(uuid4())
    scope={'kind':'once','action_id':action_id,'session_id':session,'approval_review_id':review_id,
           'payload_digest':'b'*64,'families':['browser_inspect'],'targets':['synthetic'],
           'target_pins':[{'role':'synthetic'}],'approved_at':approved.isoformat()+'Z'}
    grant=RoomGrant(room_id=args['room_id'],agent_id=args['agent_id'],decision='consumed',scope=scope,expires_at=approved+timedelta(minutes=5))
    db.add(grant);db.flush()
    action=ToolAction(action_id=action_id,room_id=args['room_id'],agent_id=args['agent_id'],trigger_event_id=args['root_id'],
        family='browser_inspect',arguments={},state='completed',workflow_id='flow',started_at=approved+timedelta(seconds=1),
        completed_at=approved+timedelta(seconds=2),session_id=session,approval_scope='once',approval_owner_id=owner,
        approved_at=approved,grant_id=grant.id,review_id=review_id,payload_digest='b'*64,target='synthetic')
    db.add(action)
    append_event(db,args['room_id'],ActorRef('tool',None),'tool_result',{'trigger_event_id':args['root_id'],
        'agent_id':args['agent_id'],'action_id':action.action_id,'state':'completed'},'tool-result:'+action.action_id)
    failure=append_event(db,args['room_id'],ActorRef('agent',args['agent_id']),'failure',{'trigger_event_id':args['root_id'],
        'generation':args['generation'],'round':1,'teammate_handoff':{'handoff_id':args['job']['handoff_id'],'phase':'response'}})
    teammates.finish_job(db,args['job'],failure);run.state='idle';db.commit()
    return db,args,identity,action,failure,SimpleNamespace(current=lambda _:epoch)


def test_failed_readonly_settlement_rechecks_generation_action_and_approval(room_client,monkeypatch):
    client,(owner,_,project,_,_)=room_client;db,args,identity,action,failure,epochs=prepare(client,owner,project,monkeypatch)
    try:
        kw=dict(failure_event_id=failure.event_id,action_ids={action.action_id},workflow_id='flow',owner_epochs=epochs)
        assert require_failed_readonly(db,identity,**kw)
        action.family='browser_interact';db.flush()
        with pytest.raises(ReviewChanged):require_failed_readonly(db,identity,**kw)
        action.family='browser_inspect';action.approval_owner_id=owner+1;db.flush()
        with pytest.raises(ReviewChanged):require_failed_readonly(db,identity,**kw)
        action.approval_owner_id=owner;db.get(RoomAgentRun,(args['room_id'],args['agent_id'])).generation+=1;db.flush()
        with pytest.raises(ReviewChanged):require_failed_readonly(db,identity,**kw)
    finally:db.close()


def test_every_workflow_action_and_original_consumed_grant_are_required(room_client,monkeypatch):
    client,(owner,_,project,_,_)=room_client;db,args,identity,action,failure,epochs=prepare(client,owner,project,monkeypatch)
    try:
        denied=ToolAction(action_id=str(uuid4()),room_id=args['room_id'],agent_id=args['agent_id'],trigger_event_id=args['root_id'],
            family='browser_navigate',arguments={},state='denied',workflow_id='flow',completed_at=datetime.now(timezone.utc),session_id=str(uuid4()))
        db.add(denied)
        append_event(db,args['room_id'],ActorRef('tool',None),'tool_result',{'trigger_event_id':args['root_id'],
            'agent_id':args['agent_id'],'action_id':denied.action_id,'state':'denied'},'tool-result:'+denied.action_id)
        db.commit()
        kw=dict(failure_event_id=failure.event_id,action_ids={action.action_id,denied.action_id},workflow_id='flow',owner_epochs=epochs)
        assert require_failed_readonly(db,identity,**kw)
        with pytest.raises(ReviewChanged):require_failed_readonly(db,identity,**{**kw,'action_ids':{action.action_id}})
        grant=db.get(RoomGrant,action.grant_id);grant.decision='revoked';db.flush()
        with pytest.raises(ReviewChanged):require_failed_readonly(db,identity,**kw)
    finally:db.close()


def test_db_cancel_before_post_park_recheck_denies_return(room_client,monkeypatch):
    client,(owner,_,project,_,_)=room_client;db,args,identity,action,failure,epochs=prepare(client,owner,project,monkeypatch)
    try:
        kw=dict(failure_event_id=failure.event_id,action_ids={action.action_id},workflow_id='flow',owner_epochs=epochs)
        assert require_failed_readonly(db,identity,**kw)
        # Simulates DB cancellation committed while the Core response is in flight.
        db.get(TeammateHandoff,args['job']['handoff_id']).state='cancelled';db.commit()
        with pytest.raises(ReviewChanged):require_failed_readonly(db,identity,**kw)
    finally:db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(('cancel_before','revoke_ok'),[(True,True),(True,False),(False,True)])
async def test_core_park_response_and_db_cancel_are_serialized(room_client,monkeypatch,cancel_before,revoke_ok):
    import asyncio,threading
    from app.services.vault_profile_handoff import park_custody
    client,(owner,_,project,_,_)=room_client;db,args,identity,action,failure,epochs=prepare(client,owner,project,monkeypatch)
    entered=threading.Event();release=threading.Event();revoked=[]
    class Proxy:
        async def abort_connections(self,**kwargs):pass
    class Provider:
        def current(self,owner_id):return 'a'*64
        def vault_custody(self,owner_id,operation,payload):
            entered.set();release.wait(2)
            return {'status':'parked','custody_id':'c'*32,'profile_id':'p'*32,'profile_revision':1,'review_context_id':'b'*32}
        def vault_profile_revoke(self,owner_id,handoff_id):
            revoked.append(handoff_id)
            return {'status':'closed' if revoke_ok else 'unavailable','handoff_id':handoff_id,'profile_id':'p'*32,'profile_revision':1}
    workflow=(args['room_id'],args['agent_id'],args['root_id'],args['generation'],'flow','d'*64)
    slot={'owner':owner,'epoch':'a'*64,'state':'sealed','cancelled':False,'workflow':workflow,
          'workflow_identity':identity,'workflow_actions':{action.action_id},'context_id':'a'*32,
          'custody_id':'c'*32,'profile_id':'p'*32,'profile_revision':1,'proxy':Proxy()}
    browser=SimpleNamespace(profiles=SimpleNamespace(custody_slots={'f'*32:slot}),owner_epochs=Provider(),
                            sessions=client.app.state.session_factory)
    try:
        task=asyncio.create_task(park_custody(browser,'f'*32,slot,workflow,settled_failure=failure.event_id))
        assert await asyncio.to_thread(entered.wait,2)
        if cancel_before:
            db.get(TeammateHandoff,args['job']['handoff_id']).state='cancelled';db.commit()
        release.set();result=await task
        if cancel_before:
            assert result is False and slot['state']=='unknown' and slot['cancelled'] and revoked==['f'*32]
        else:
            assert result is True and slot['state']=='sealed' and slot['workflow'] is None and not revoked
            db.get(TeammateHandoff,args['job']['handoff_id']).state='cancelled';db.commit()
    finally:release.set();db.close()


def test_real_retained_review_and_consumed_grant_bind_settled_action(room_client):
    import asyncio,hashlib
    import os
    from app.models.models import Agent
    from app.models.collaboration import ProviderConnection
    from app.services import grants
    from app.services.action_review import ProposerBinding,observe_authority
    from app.services.review_runtime import BrowserTarget
    from app.services.review_snapshot import ReviewSnapshot,ExternalContext,ReviewChecks,_binding,stage_initial_review,retained_review
    from app.services.scheduler import _teammate_failure_readonly_park_eligible
    from app.services.vault_profile_handoff import park_completed_custodies,canonical
    from app.services.tool_broker import prepare,stage_action_result
    client,(owner,_,project,_,_)=room_client;db,args,_=make_task(client,owner,project)
    epochs=SimpleNamespace(current=lambda _:'a'*64);session=str(uuid4())
    class Broker:
        session_id=session
        def require_review(self,db,action,*,owner_id,review_id):
            assert owner_id==owner and review_id==action.review_id
            return retained_review(action)
        def execution_review(self,db,action):return retained_review(action)
    try:
        identity=bind(db,**args);run=db.get(RoomAgentRun,(args['room_id'],args['agent_id']))
        run.pending_task=dict(run.pending_task or {},custody_teammate_job={'handoff_id':args['job']['handoff_id'],'phase':'response'})
        agent=db.get(Agent,args['agent_id']);connection=db.get(ProviderConnection,agent.connection_id)
        proposer=ProposerBinding(owner_id=owner,agent_id=agent.id,room_id=args['room_id'],connection_id=connection.id,
            model_id=agent.model_id,config_revision=connection.readiness_revision,auth_grant_revision=connection.auth_grant_revision,
            provider_id=connection.provider_id,protocol_id=connection.protocol_id,owner_epoch='a'*64)
        authority=observe_authority(db,room_id=args['room_id'],agent_id=agent.id,session_id=session,proposer=proposer,owner_epochs=epochs)
        prepared=prepare('browser_inspect',{'url':'https://example.test/'},'flow')
        action=ToolAction(action_id=str(uuid4()),room_id=args['room_id'],agent_id=agent.id,trigger_event_id=args['root_id'],
            family=prepared.family,arguments=prepared.public_arguments,target=prepared.target,scope_target=prepared.scope_target,
            expected_effect=prepared.expected_effect,payload_digest=prepared.payload_digest,workflow_id='flow',session_id=session,state='waiting')
        target=BrowserTarget(room_id=args['room_id'],agent_id=agent.id,session_id=session,workflow_id='flow',status='verified',context_id='a'*32)
        review=ReviewSnapshot(version=1,captured_at=datetime.now(timezone.utc),action=_binding(action),authority=authority,targets=(target,),
            external_context=ExternalContext(account='unsupported',target_version='unsupported'),
            checks=ReviewChecks(target='runtime_observed',owner_authentication='verified'))
        stage_initial_review(action,review);db.add(action);db.commit()
        grants.decide(db,action.action_id,args['room_id'],owner,'approve','once',session,review_id=action.review_id,broker=Broker())
        action=db.get(ToolAction,action.action_id,populate_existing=True)
        ref=grants.claim(db,action,session,action.grant_id,broker=Broker(),require_action_review=True);assert ref is not None
        action.state='started';action.started_at=grants.now();db.flush();stage_action_result(db,action.action_id,'completed','synthetic')
        db.commit()
        denied=ToolAction(action_id=str(uuid4()),room_id=args['room_id'],agent_id=agent.id,trigger_event_id=args['root_id'],
            family=prepared.family,arguments=prepared.public_arguments,target=prepared.target,scope_target=prepared.scope_target,
            expected_effect=prepared.expected_effect,payload_digest=prepared.payload_digest,workflow_id='flow',session_id=session,state='waiting')
        denied_review=ReviewSnapshot(version=1,captured_at=datetime.now(timezone.utc),action=_binding(denied),authority=authority,targets=(target,),
            external_context=ExternalContext(account='unsupported',target_version='unsupported'),
            checks=ReviewChecks(target='runtime_observed',owner_authentication='verified'))
        stage_initial_review(denied,denied_review);db.add(denied);db.commit()
        grants.decide(db,denied.action_id,args['room_id'],owner,'deny',None,session)
        stage_action_result(db,denied.action_id,'denied','synthetic denied');db.commit()
        second=ToolAction(action_id=str(uuid4()),room_id=args['room_id'],agent_id=agent.id,trigger_event_id=args['root_id'],
            family=prepared.family,arguments=prepared.public_arguments,target=prepared.target,scope_target=prepared.scope_target,
            expected_effect=prepared.expected_effect,payload_digest=prepared.payload_digest,workflow_id='flow',session_id=session,state='waiting')
        second_review=ReviewSnapshot(version=1,captured_at=datetime.now(timezone.utc),action=_binding(second),authority=authority,targets=(target,),
            external_context=ExternalContext(account='unsupported',target_version='unsupported'),
            checks=ReviewChecks(target='runtime_observed',owner_authentication='verified'))
        stage_initial_review(second,second_review);db.add(second);db.commit()
        grants.decide(db,second.action_id,args['room_id'],owner,'approve','once',session,review_id=second.review_id,broker=Broker())
        second=db.get(ToolAction,second.action_id,populate_existing=True)
        assert grants.claim(db,second,session,second.grant_id,broker=Broker(),require_action_review=True) is not None
        second.state='started';second.started_at=grants.now();db.flush();stage_action_result(db,second.action_id,'completed','synthetic two');db.commit()
        failure=append_event(db,args['room_id'],ActorRef('agent',agent.id),'failure',{'trigger_event_id':args['root_id'],
            'generation':args['generation'],'round':1,'teammate_handoff':{'handoff_id':args['job']['handoff_id'],'phase':'response'}})
        teammates.finish_job(db,args['job'],failure);db.get(RoomAgentRun,(args['room_id'],agent.id)).state='idle';db.commit()
        kw=dict(failure_event_id=failure.event_id,action_ids={action.action_id,denied.action_id,second.action_id},workflow_id='flow',owner_epochs=epochs)
        assert require_failed_readonly(db,identity,**kw)
        assert _teammate_failure_readonly_park_eligible(db,room_id=args['room_id'],agent_id=agent.id,owner_id=owner,
            root_id=args['root_id'],generation=args['generation'],round_number=1,failure_event_id=failure.event_id,
            calls=3,workflows={'flow'},owner_epochs=epochs,proof=None,job=args['job'])
        class Proxy:
            async def abort_connections(self,**kwargs):pass
        class Provider:
            def current(self,owner_id):return 'a'*64
            def vault_custody(self,owner_id,operation,payload):
                return {'status':'parked','custody_id':'c'*32,'profile_id':'p'*32,'profile_revision':1,'review_context_id':'b'*32}
        workflow=(args['room_id'],agent.id,args['root_id'],args['generation'],'flow',hashlib.sha256(canonical(None)).hexdigest())
        slot={'owner':owner,'epoch':'a'*64,'state':'sealed','cancelled':False,'workflow':workflow,
            'workflow_identity':identity,'workflow_actions':{action.action_id,second.action_id},'context_id':'a'*32,
            'custody_id':'c'*32,'profile_id':'p'*32,'profile_revision':1,'proxy':Proxy()}
        browser=SimpleNamespace(profiles=SimpleNamespace(custody_slots={'f'*32:slot}),owner_epochs=Provider(),
                                sessions=client.app.state.session_factory)
        asyncio.run(park_completed_custodies(browser,args['room_id'],agent.id,args['root_id'],args['generation'],None,{'flow'},
                                             settled_failure=failure.event_id))
        assert slot['state']=='sealed' and slot['workflow'] is None
        original_grant=action.grant_id;action.grant_id=second.grant_id;db.flush()
        with pytest.raises(ReviewChanged):require_failed_readonly(db,identity,**kw)
        action.grant_id=original_grant;db.flush()
        grant=db.get(RoomGrant,action.grant_id);scope=dict(grant.scope);scope['payload_digest']='c'*64;grant.scope=scope;db.flush()
        with pytest.raises(ReviewChanged):require_failed_readonly(db,identity,**kw)
    finally:db.close()
