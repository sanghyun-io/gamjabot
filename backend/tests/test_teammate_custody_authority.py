"""Source-only task-identity experiments; no Core or real-browser effect."""
from datetime import timedelta
import pytest
from test_room_state import room_client, create_room
from test_teammates import team
from app.models.collaboration import RoomAgentRun, RoomTrigger
from app.models.teammate import TeammateHandoff
from app.services import execution_scopes as scopes, room_service, teammates
from app.services.action_review import ReviewChanged
from app.services.events import ActorRef, append_event
from app.services.teammate_custody_authority import bind, bind_cross, require_current, TaskIdentity


def make_task(client, owner, project, *, nested=False):
    room=create_room(client,owner,project,'identity experiment')
    agents=team(client,owner,room,('Ada','Ben','Cy') if nested else ('Ada','Ben'))
    ada,ben=agents[:2]
    db=client.app.state.session_factory()
    root=append_event(db,room,ActorRef('user',owner),'message',{'content':'synthetic'})
    db.add(RoomTrigger(event_id=root.event_id,room_id=room,state='running',selected_agents=[ada],current_round=1,
        max_rounds=1,max_tool_calls=8,max_executor_seconds=60))
    run=db.get(RoomAgentRun,(room,ada));run.state='running';run.generation+=1;run.trigger_event_id=root.event_id
    db.commit();teammates.send(db,room,root.event_id,ada,run.generation,'request',{'agent_id':ben,'content':'synthetic'})
    job=teammates.next_job(db,room,root.event_id)
    generation=room_service.begin_turn(db,room,ben,trigger_event_id=root.event_id,round_number=1,watermark=1)
    return db,dict(owner_id=owner,room_id=room,agent_id=ben,root_id=root.event_id,generation=generation,job=job),agents


def test_identity_is_stable_across_approval_wait_and_invalid_after_cancel(room_client,monkeypatch):
    client,(owner,_,project,_,_)=room_client;db,args,_=make_task(client,owner,project)
    try:
        db.get(RoomTrigger,args['root_id']).max_tool_calls=0;db.flush()
        assert scopes.bind(db,args['room_id'],args['agent_id'],args['root_id'],args['generation'],teammate_job=args['job']) is None
        value=bind(db,**args);old_now=scopes.now
        monkeypatch.setattr(scopes,'now',lambda:old_now()+timedelta(minutes=10))
        assert require_current(db,value)==value and 'deadline' not in value.canonical and len(value.digest)==64
        run=db.get(RoomAgentRun,(args['room_id'],args['agent_id']));run.generation+=1;db.flush()
        with pytest.raises(ReviewChanged):require_current(db,value)
        run.generation-=1;row=db.get(TeammateHandoff,args['job']['handoff_id']);row.state='cancelled';db.flush()
        with pytest.raises(ReviewChanged):require_current(db,value)
    finally:db.close()


def test_existing_typed_scope_requires_its_exact_live_proof(room_client):
    client,(owner,_,project,_,_)=room_client;db,args,_=make_task(client,owner,project)
    try:
        scopes.root_scope(db,args['root_id'],create=True);db.flush()
        with pytest.raises(ReviewChanged):bind(db,**args)
        proof=scopes.bind(db,args['room_id'],args['agent_id'],args['root_id'],args['generation'],teammate_job=args['job'])
        value=bind(db,**args,execution_binding=proof)
        assert value.as_dict()['scope_lineage'][-1]['job_kind']=='teammate'
        assert require_current(db,value,execution_binding=proof)==value
        with pytest.raises(ReviewChanged):require_current(db,value)
        scope=db.get(scopes.ExecutionScope,proof['scope_id']);scope.state='revoked';scope.epoch+=1;db.flush()
        with pytest.raises(ReviewChanged):require_current(db,value,execution_binding=proof)
        with pytest.raises(ReviewChanged):require_current(db,TaskIdentity('{}'))
    finally:db.close()


def test_nested_parent_cancellation_invalidates_child_identity(room_client):
    client,(owner,_,project,_,_)=room_client;db,args,agents=make_task(client,owner,project,nested=True)
    try:
        teammates.send(db,args['room_id'],args['root_id'],args['agent_id'],args['generation'],'nested',
            {'agent_id':agents[2],'content':'synthetic'},parent_handoff_id=args['job']['handoff_id'])
        child=teammates.next_job(db,args['room_id'],args['root_id'])
        generation=room_service.begin_turn(db,args['room_id'],agents[2],trigger_event_id=args['root_id'],round_number=1,watermark=1)
        identity=bind(db,owner_id=owner,room_id=args['room_id'],agent_id=agents[2],root_id=args['root_id'],generation=generation,job=child)
        assert len(identity.as_dict()['lineage'])==2
        parent=db.get(TeammateHandoff,args['job']['handoff_id']);parent.state='cancelled';db.flush()
        with pytest.raises(ReviewChanged):require_current(db,identity)
    finally:db.close()


def test_cross_message_identity_requires_original_scope_and_rechecks_ancestor(room_client):
    from test_execution_scopes import seed
    client,(owner,_,project,_,_)=room_client;s=seed(client,owner,project)
    with client.app.state.session_factory() as db:
        args=dict(owner_id=owner,room_id=s.personal[s.ben],agent_id=s.ben,root_id=s.root,
                  generation=s.gen,job=s.job,execution_binding=s.proof)
        identity=bind_cross(db,**args)
        assert identity.as_dict()['lineage'][-1]['kind']=='bot_message'
        assert identity.as_dict()['scope_lineage'][-1]['job_kind']=='bot_message'
        assert require_current(db,identity,execution_binding=s.proof)==identity
        root=db.get(scopes.ExecutionScope,s.proof['root_scope_id']);root.state='revoked';root.epoch+=1;db.flush()
        with pytest.raises(ReviewChanged):require_current(db,identity,execution_binding=s.proof)


def test_nested_batch_member_identity_contains_batch_and_cross_parent(room_client):
    from test_execution_scopes import seed
    from app.services import bot_batches, bot_messages
    from app.models.bot_batch import BotBatch
    from app.models.bot_message import BotMessage
    client,(owner,_,project,_,_)=room_client;s=seed(client,owner,project)
    with client.app.state.session_factory() as db:
        batch=bot_batches.send(db,s.personal[s.ben],s.room,s.root,s.ben,s.gen,'batch',
            {'tasks':[{'agent_id':s.ada,'content':'synthetic A'},{'agent_id':s.cy,'content':'synthetic C'}]},
            parent_job_id=s.message_id,parent_kind='bot_message')
        other=db.query(BotMessage).filter_by(batch_id=batch['batch_id'],recipient_agent_id=s.ada).one()
        job=bot_messages.next_job(db,s.room,s.root,exclude_rooms=[other.recipient_room_id])
        assert job and job['agent_id']==s.cy
        generation=room_service.begin_turn(db,job['room_id'],s.cy,trigger_event_id=s.root,round_number=1,watermark=1,cross_job=job)
        proof=scopes.bind(db,job['room_id'],s.cy,s.root,generation,cross_job=job)
        identity=bind_cross(db,owner_id=owner,room_id=job['room_id'],agent_id=s.cy,root_id=s.root,
                            generation=generation,job=job,execution_binding=proof)
        lineage=identity.as_dict()['lineage']
        assert [v['kind'] for v in lineage]==['bot_message','bot_message']
        assert lineage[0]['id']==s.message_id and lineage[-1]['batch_id']==batch['batch_id']
        member=db.get(BotMessage,job['message_id']);member.parent_job_id=str(__import__('uuid').uuid4());db.flush()
        with pytest.raises(ReviewChanged):require_current(db,identity,execution_binding=proof)


def test_batch_return_identity_includes_typed_parent(room_client):
    from test_execution_scopes import seed
    from app.services import bot_batches
    from app.models.bot_batch import BotBatch
    from app.models.bot_message import BotMessage
    client,(owner,_,project,_,_)=room_client;s=seed(client,owner,project)
    with client.app.state.session_factory() as db:
        result=bot_batches.send(db,s.personal[s.ben],s.room,s.root,s.ben,s.gen,'batch-return',
            {'tasks':[{'agent_id':s.ada,'content':'synthetic A'},{'agent_id':s.cy,'content':'synthetic C'}]},
            parent_job_id=s.message_id,parent_kind='bot_message')
        batch=db.get(BotBatch,result['batch_id']);batch.state='failed';batch.reply_state='running'
        for member in db.query(BotMessage).filter_by(batch_id=batch.batch_id):member.state='failed';member.reply_state='cancelled'
        db.flush();job={'message_id':batch.batch_id,'phase':'return'}
        proof=scopes.bind(db,s.personal[s.ben],s.ben,s.root,s.gen,cross_job=job)
        identity=bind_cross(db,owner_id=owner,room_id=s.personal[s.ben],agent_id=s.ben,root_id=s.root,
                            generation=s.gen,job=job,execution_binding=proof)
        assert [v['kind'] for v in identity.as_dict()['lineage']]==['bot_message','bot_batch']
        assert require_current(db,identity,execution_binding=proof)==identity
        batch.parent_job_id=str(__import__('uuid').uuid4());db.flush()
        with pytest.raises(ReviewChanged):require_current(db,identity,execution_binding=proof)


def test_unscoped_borrow_uses_exact_scheduler_marker_and_rejects_missing(room_client):
    from types import SimpleNamespace
    from app.services.vault_profile_handoff import _unscoped_teammate_identity
    client,(owner,_,project,_,_)=room_client;db,args,agents=make_task(client,owner,project)
    try:
        run=db.get(RoomAgentRun,(args['room_id'],args['agent_id']))
        run.pending_task=dict(run.pending_task or {},custody_teammate_job={'handoff_id':args['job']['handoff_id'],'phase':'response'});db.commit()
        browser=SimpleNamespace(sessions=client.app.state.session_factory)
        action=SimpleNamespace(room_id=args['room_id'],agent_id=args['agent_id'],trigger_event_id=args['root_id'])
        ref=SimpleNamespace(run_generation=args['generation'])
        identity=_unscoped_teammate_identity(browser,action,ref,owner)
        assert identity.affected_by_stop(args['room_id'],agents[0])
        assert identity.affected_by_stop(args['room_id'],agents[1])
        assert not identity.affected_by_stop(args['room_id'],99999)
        run.pending_task={};db.commit()
        with pytest.raises(ReviewChanged):_unscoped_teammate_identity(browser,action,ref,owner)
    finally:db.close()


@pytest.mark.asyncio
async def test_unscoped_parent_stop_before_park_confirmation_stays_unknown(room_client):
    import asyncio,threading
    from types import SimpleNamespace
    from app.services.vault_profile_handoff import park_custody,stop_custodies
    client,(owner,_,project,_,_)=room_client;db,args,agents=make_task(client,owner,project)
    try:identity=bind(db,**args)
    finally:db.close()
    entered=threading.Event();release=threading.Event();closed=[]
    class Proxy:
        async def abort_connections(self,**kwargs):pass
    class Provider:
        def current(self,owner_id):return 'e'*64
        def vault_custody(self,owner_id,operation,payload):
            entered.set();release.wait(2)
            return {'status':'parked','custody_id':'c'*32,'profile_id':'p'*32,'profile_revision':1,'review_context_id':'b'*32}
        def vault_profile_revoke(self,owner_id,handoff_id):
            closed.append(handoff_id)
            return {'status':'closed','handoff_id':handoff_id,'profile_id':'p'*32,'profile_revision':1}
    workflow=(args['room_id'],args['agent_id'],args['root_id'],args['generation'],'flow','d'*64)
    slot={'owner':owner,'epoch':'e'*64,'state':'sealed','cancelled':False,'workflow':workflow,
          'workflow_identity':identity,'context_id':'a'*32,'custody_id':'c'*32,'profile_id':'p'*32,
          'profile_revision':1,'proxy':Proxy()}
    browser=SimpleNamespace(profiles=SimpleNamespace(custody_slots={'f'*32:slot}),owner_epochs=Provider())
    task=asyncio.create_task(park_custody(browser,'f'*32,slot,workflow))
    assert await asyncio.to_thread(entered.wait,2)
    await stop_custodies(browser,lambda item:item['workflow_identity'].affected_by_stop(args['room_id'],agents[0]))
    release.set();assert await task is False
    assert closed==['f'*32] and slot['state']=='unknown' and slot['cancelled']


@pytest.mark.asyncio
async def test_parent_stop_routes_unscoped_child_to_core_revoke(room_client):
    from app.services.tools.browser import BrowserExecutor
    client,(owner,_,project,_,_)=room_client;db,args,agents=make_task(client,owner,project,nested=True)
    try:
        teammates.send(db,args['room_id'],args['root_id'],args['agent_id'],args['generation'],'nested',
            {'agent_id':agents[2],'content':'synthetic'},parent_handoff_id=args['job']['handoff_id'])
        job=teammates.next_job(db,args['room_id'],args['root_id'])
        generation=room_service.begin_turn(db,args['room_id'],agents[2],trigger_event_id=args['root_id'],round_number=1,watermark=1)
        identity=bind(db,owner_id=owner,room_id=args['room_id'],agent_id=agents[2],root_id=args['root_id'],generation=generation,job=job)
    finally:db.close()
    closed=[]
    class Proxy:
        async def abort_connections(self,**kwargs):pass
    class Provider:
        def vault_profile_revoke(self,owner_id,handoff_id):
            closed.append(handoff_id)
            return {'status':'closed','handoff_id':handoff_id,'profile_id':'p'*32,'profile_revision':1}
    executor=BrowserExecutor();executor.owner_epochs=Provider()
    slot={'owner':owner,'state':'sealed','cancelled':False,'workflow':(args['room_id'],agents[2],args['root_id'],generation,'flow','d'*64),
          'workflow_identity':identity,'profile_id':'p'*32,'profile_revision':1,'proxy':Proxy()}
    executor.profiles.custody_slots={'f'*32:slot}
    await executor.stop_room(args['room_id'],agents[0])
    assert closed==['f'*32] and slot['cancelled'] and slot['state']=='unknown'
