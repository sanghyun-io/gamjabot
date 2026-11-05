"""Four disposable DB/fake-effect contracts; no installed app or real model."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi import HTTPException
from test_room_state import room_client, headers
from test_bot_messages import cross_team
from test_teammates import team
from app.models.collaboration import RoomAgentRun, RoomEvent, RoomTrigger, ToolAction, ProviderConnection, RoomGrant
from app.models.models import Agent
from app.models.bot_message import BotMessage
from app.models.execution_scope import ExecutionScope
from app.services import execution_scopes as scopes, bot_messages, room_service, resume_boundary
from app.services.events import ActorRef, append_event, stage_event
from app.services.action_review import ProposerBinding, observe_authority, ReviewChanged
from app.services.review_snapshot import capture_review, stage_initial_review
from app.services.tool_broker import ToolBroker, ActionResult
from app.services.grants import claim, set_broad
from app.services.scheduler import ExecutorBudget


class FakeExecutor:
    scoped_execution_ready=True
    def __init__(self):self.effects=[];self.stops=[]
    async def prepare_scoped_execution(self):return True
    async def __call__(self,action,ref,cancel):
        self.effects.append((action.action_id,ref.execution_binding))
        return ActionResult('completed','Synthetic result, no file content read.')
    async def stop_room(self,*args):pass
    async def close(self):pass
    async def stop_scopes(self,refs):
        self.stops.extend(refs)
        return [dict(r,state='settled') for r in refs]


def seed(client, owner, project):
    room,(ada,ben,cy),personal=cross_team(client,owner,project,nested=True)
    dana,=team(client,owner,room,('Dana',))
    with client.app.state.session_factory() as db:
        event=append_event(db,room,ActorRef('user',owner),'message',{'content':'Explicit synthetic task'})
        root=event.event_id
        db.add(RoomTrigger(event_id=root,room_id=room,state='running',selected_agents=[ada,dana],current_round=1,
            max_rounds=1,max_tool_calls=3,max_executor_seconds=4))
        run=db.get(RoomAgentRun,(room,ada));run.state='running';run.generation+=1;run.trigger_event_id=root
        db.commit()
        receipt=bot_messages.send(db,room,room,root,ada,run.generation,'to-ben',{'agent_id':ben,'content':'Explicit delegated work'})
        job=bot_messages.next_job(db,room,root)
        gen=room_service.begin_turn(db,personal[ben],ben,trigger_event_id=root,round_number=1,watermark=1,cross_job=job)
        proof=scopes.bind(db,personal[ben],ben,root,gen,cross_job=job)
        run=db.get(RoomAgentRun,(personal[ben],ben));run.pending_task=dict(run.pending_task or {},execution_binding=proof)
        # A separate direct Bot branch must survive B/C cancellation.
        drun=db.get(RoomAgentRun,(room,dana));drun.state='running';drun.generation+=1;drun.trigger_event_id=root
        sibling=scopes.bind(db,room,dana,root,drun.generation)
        db.commit()
    return SimpleNamespace(room=room,root=root,ada=ada,ben=ben,cy=cy,dana=dana,personal=personal,
        job=job,proof=proof,sibling=sibling,message_id=receipt['message_id'],gen=gen)


def proposal(client, owner, s, path, broker, *, proof=None):
    proof=proof or s.proof
    prepared=broker.prepare('read_file',{'path':str(path)})
    with client.app.state.session_factory() as db:
        agent=db.get(Agent,proof['agent_id']);con=db.get(ProviderConnection,agent.connection_id)
        proposer=ProposerBinding(owner_id=owner,agent_id=agent.id,room_id=proof['execution_room_id'],
            connection_id=con.id,model_id=agent.model_id,config_revision=con.readiness_revision,
            auth_grant_revision=con.auth_grant_revision,provider_id=con.provider_id,protocol_id=con.protocol_id)
        authority=observe_authority(db,room_id=proof['execution_room_id'],agent_id=agent.id,
            session_id=broker.session_id,proposer=proposer)
        a=ToolAction(action_id=str(uuid4()),room_id=proof['execution_room_id'],agent_id=agent.id,
            trigger_event_id=s.root,call_id=str(uuid4()),family='read_file',arguments=prepared.public_arguments,
            target=prepared.target,scope_target=prepared.scope_target,payload_digest=prepared.payload_digest,
            expected_effect=prepared.expected_effect,session_id=broker.session_id,state='waiting',execution_binding=proof)
        scopes.claim_proposal(db,a);stage_initial_review(a,capture_review(a,prepared,authority))
        db.add(a);db.commit();db.refresh(a);db.expunge(a)
    a._raw_arguments=prepared.raw_arguments;broker.retain(a.action_id,prepared,proposer=proposer)
    return a


def approve(client,owner,action,scope='once'):
    return client.post(f'/api/v1/tools/rooms/{action.room_id}/actions/{action.action_id}/decision',
        headers=headers(client,owner),json={'decision':'approve','scope':scope,'review_id':action.review_id})


def test_delegated_review_requires_fresh_once_and_exact_scoped_effect(room_client,tmp_path):
    client,(owner,_,project,_,_)=room_client;s=seed(client,owner,project)
    fake=FakeExecutor();broker=ToolBroker(client.app.state.session_factory,fake);client.app.state.scheduler.broker=broker
    path=tmp_path/'synthetic';path.write_text('Synthetic only')
    action=proposal(client,owner,s,path,broker)
    view=broker.preview(action,owner_id=owner)
    assert view['review']['version']==2 and view['approval_policy']['scopes']==['once']
    assert view['execution_binding']==s.proof and view['root_ref']['room_id']==s.room
    assert approve(client,owner,action,'room_session').status_code==409
    with broker.sessions() as db:
        set_broad(db,owner,broker.session_id,True,None,always=True)
        assert claim(db,db.get(ToolAction,action.action_id),broker.session_id,broker=broker) is None
        db.rollback()
    assert approve(client,owner,action).status_code==200
    outcome=asyncio.run(broker.execute(action,None,asyncio.Event(),ExecutorBudget(4)))
    assert outcome.state=='completed' and len(fake.effects)==1 and fake.effects[0][1]==s.proof
    again=asyncio.run(broker.execute(action,None,asyncio.Event(),ExecutorBudget(4)))
    assert again.state=='denied' and len(fake.effects)==1
    rows=client.get(f'/api/v1/bots/messages/{s.message_id}/actions',headers=headers(client,owner)).json()
    assert rows['actions'][0]['room_id']==s.personal[s.ben] and rows['root_ref']['event_id']==s.root
    inspector=client.get(f'/api/v1/conversations/{s.room}/requests/{s.root}',headers=headers(client,owner))
    assert inspector.status_code==200 and inspector.json()['action_count']==1,inspector.text


def test_source_stop_rejects_approved_old_generation_before_fake_driver(room_client,tmp_path):
    client,(owner,_,project,_,_)=room_client;s=seed(client,owner,project)
    fake=FakeExecutor();broker=ToolBroker(client.app.state.session_factory,fake);client.app.state.scheduler.broker=broker
    path=tmp_path/'synthetic';path.write_text('Synthetic only');action=proposal(client,owner,s,path,broker)
    assert approve(client,owner,action).status_code==200
    with broker.sessions() as db:
        bot_messages.cancel(db,s.room);db.commit()
    result=asyncio.run(broker.execute(action,None,asyncio.Event(),ExecutorBudget(4)))
    assert result.state in {'denied','cancelled'} and fake.effects==[]
    with broker.sessions() as db:
        assert db.get(ToolAction,action.action_id).state=='cancelled'
        assert db.get(RoomGrant,db.get(ToolAction,action.action_id).grant_id).decision=='revoked'
        with pytest.raises(ReviewChanged):scopes.require_binding(db,s.proof,for_start=False)


def test_recipient_stop_preserves_parent_return_and_unrelated_sibling(room_client):
    client,(owner,_,project,_,_)=room_client;s=seed(client,owner,project)
    with client.app.state.session_factory() as db:
        child=bot_messages.send(db,s.personal[s.ben],s.room,s.root,s.ben,s.gen,'ben-to-cy',
            {'agent_id':s.cy,'content':'Explicit nested task'},parent_job_id=s.message_id,parent_kind='bot_message')
        e=stage_event(db,s.personal[s.ben],ActorRef('agent',s.ben),'message',{'content':'Ben sent work','trigger_event_id':s.root})
        bot_messages.finish_job(db,s.job,e);db.get(RoomAgentRun,(s.personal[s.ben],s.ben)).state='idle';db.commit()
        job=bot_messages.next_job(db,s.room,s.root)
        gen=room_service.begin_turn(db,s.personal[s.cy],s.cy,trigger_event_id=s.root,round_number=1,watermark=1,cross_job=job)
        e=stage_event(db,s.personal[s.cy],ActorRef('agent',s.cy),'message',{'content':'Actual Cy result','trigger_event_id':s.root})
        bot_messages.finish_job(db,job,e);db.get(RoomAgentRun,(s.personal[s.cy],s.cy)).state='idle';db.commit()
        bot_messages.cancel(db,s.personal[s.cy]);db.commit()
        assert db.get(BotMessage,child['message_id']).reply_state=='pending'
        # Closing the completed recipient does not invalidate exact historical result.
        from app.models.models import Conversation
        db.get(Conversation,s.personal[s.cy]).is_active=False;db.commit()
        returned=bot_messages.next_job(db,s.room,s.root)
        assert returned['phase']=='return' and returned['agent_id']==s.ben
        gen=room_service.begin_turn(db,s.personal[s.ben],s.ben,trigger_event_id=s.root,round_number=1,watermark=1,cross_job=returned)
        b_return=scopes.bind(db,s.personal[s.ben],s.ben,s.root,gen,cross_job=returned)
        assert b_return['scope_id']==s.proof['scope_id'] and b_return['scope_id']!=scopes.identity(s.root,'bot_message:'+child['message_id'])
        scopes.require_binding(db,s.sibling)
        bot_messages.cancel(db,s.personal[s.ben]);db.commit()
        scopes.require_binding(db,s.sibling)
        with pytest.raises(ReviewChanged):scopes.require_binding(db,b_return)


def test_pause_resumes_original_worker_with_new_review_and_shared_budget(room_client,tmp_path):
    client,(owner,_,project,_,_)=room_client;s=seed(client,owner,project)
    fake=FakeExecutor();broker=ToolBroker(client.app.state.session_factory,fake)
    client.app.state.scheduler.broker=broker;client.app.state.local_executor=fake
    path=tmp_path/'synthetic';path.write_text('Synthetic only');old=proposal(client,owner,s,path,broker)
    assert approve(client,owner,old).status_code==200
    async def pause_unconfirmed(refs):return [dict(r,state='unknown') for r in refs]
    fake.stop_scopes=pause_unconfirmed
    paused=client.post(f'/api/v1/conversations/{s.personal[s.ben]}/members/{s.ben}/pause',headers=headers(client,owner))
    assert paused.status_code==200,paused.text
    wakes=[];client.app.state.scheduler._wake=lambda rid:wakes.append(rid)
    blocked=client.post(f'/api/v1/conversations/{s.personal[s.ben]}/members/{s.ben}/resume',headers=headers(client,owner))
    assert blocked.status_code==409 and wakes==[],blocked.text
    pause_stop=client.get(f'/api/v1/bots/conversations/{s.personal[s.ben]}/scope-stops',headers=headers(client,owner)).json()['latest']
    pause_controls=[]
    async def pause_confirmed(refs):
        pause_controls.append(refs);return [dict(r,state='settled') for r in refs]
    fake.stop_scopes=pause_confirmed
    confirmed=client.post(f"/api/v1/bots/scope-stops/{pause_stop['stop_id']}/confirm",headers=headers(client,owner))
    assert confirmed.status_code==200 and confirmed.json()['state']=='settled'
    resumed=client.post(f'/api/v1/conversations/{s.personal[s.ben]}/members/{s.ben}/resume',headers=headers(client,owner))
    assert resumed.status_code==200 and wakes==[s.room],resumed.text
    stale_control=client.post(f"/api/v1/bots/scope-stops/{pause_stop['stop_id']}/confirm",headers=headers(client,owner))
    assert stale_control.status_code==409 and len(pause_controls)==1
    with client.app.state.session_factory() as db:
        assert db.get(ToolAction,old.action_id).state=='cancelled'
        job=bot_messages.next_job(db,s.room,s.root)
        gen=room_service.begin_turn(db,s.personal[s.ben],s.ben,trigger_event_id=s.root,round_number=1,watermark=1,cross_job=job)
        proof=scopes.bind(db,s.personal[s.ben],s.ben,s.root,gen,cross_job=job);db.commit()
        assert proof['run_generation']>s.gen and proof['lineage'][-1]['epoch']==2
        with pytest.raises(ReviewChanged):scopes.require_binding(db,s.proof)
    new=proposal(client,owner,s,path,broker,proof=proof)
    assert new.review_id!=old.review_id and approve(client,owner,old).status_code==409
    with client.app.state.session_factory() as db:
        current=db.get(ToolAction,new.action_id)
        assert scopes.reserve_executor(db,current)==4000
        scopes.settle_executor(db,current,.7);db.commit()
    third=proposal(client,owner,s,path,broker,proof=proof)
    with client.app.state.session_factory() as db:
        assert scopes.reserve_executor(db,db.get(ToolAction,third.action_id))==3300
        root=db.get(ExecutionScope,proof['root_scope_id']);assert root.native_calls==3
        from app.services.llm_service import LLMFailure
        with pytest.raises(LLMFailure):scopes.claim_proposal(db,ToolAction(execution_binding=proof))
        db.rollback()
    # An unconfirmed Stop retries only its frozen scopes. A later new root
    # in the same source room is outside this receipt's cancellation set.
    async def unconfirmed(refs):return [dict(r,state='unknown') for r in refs]
    fake.stop_scopes=unconfirmed
    stopped=client.post(f'/api/v1/conversations/{s.room}/cancel',headers=headers(client,owner))
    assert stopped.status_code==204 and stopped.headers.get('X-Gamja-Scope-Stop-Id')
    stop_id=stopped.headers['X-Gamja-Scope-Stop-Id']
    before=client.get(f'/api/v1/bots/scope-stops/{stop_id}',headers=headers(client,owner)).json()
    assert before['state']=='unknown'
    with client.app.state.session_factory() as db:
        event=append_event(db,s.room,ActorRef('user',owner),'message',{'content':'Later unrelated request'})
        db.add(RoomTrigger(event_id=event.event_id,room_id=s.room,state='pending',selected_agents=[s.ada],
            max_rounds=1,max_tool_calls=3,max_executor_seconds=4))
        db.flush();later=scopes.root_scope(db,event.event_id,create=True);later_id=later.scope_id;db.commit()
    confirmed=[]
    async def control_only(refs):
        confirmed.extend(refs);return [dict(r,state='settled') for r in refs]
    fake.stop_scopes=control_only
    result=client.post(f'/api/v1/bots/scope-stops/{stop_id}/confirm',headers=headers(client,owner))
    assert result.status_code==200 and result.json()['state']=='settled',result.text
    assert confirmed==before['revocations'] and later_id not in {r['scope_id'] for r in confirmed}
    with client.app.state.session_factory() as db:
        assert db.get(ExecutionScope,later_id).state=='active'
        # A v1 action already in flight before collaboration opened retains
        # its original review. Its eventual time is still charged to the root.
        from datetime import datetime,timedelta,timezone
        root2=db.get(ExecutionScope,later_id);trigger2=db.get(RoomTrigger,root2.root_request_event_id);trigger2.state='running'
        run=db.get(RoomAgentRun,(s.room,s.ada));run.state='running';run.trigger_event_id=trigger2.event_id;run.generation+=1
        agent=db.get(Agent,s.ada);con=db.get(ProviderConnection,agent.connection_id)
        proposer=ProposerBinding(owner_id=owner,agent_id=agent.id,room_id=s.room,connection_id=con.id,
            model_id=agent.model_id,config_revision=con.readiness_revision,auth_grant_revision=con.auth_grant_revision,
            provider_id=con.provider_id,protocol_id=con.protocol_id)
        prepared=broker.prepare('read_file',{'path':str(path)})
        authority=observe_authority(db,room_id=s.room,agent_id=s.ada,session_id=broker.session_id,proposer=proposer)
        legacy=ToolAction(action_id=str(uuid4()),room_id=s.room,agent_id=s.ada,trigger_event_id=trigger2.event_id,
            call_id='legacy-started',family='read_file',arguments=prepared.public_arguments,target=prepared.target,
            scope_target=prepared.scope_target,payload_digest=prepared.payload_digest,expected_effect=prepared.expected_effect,
            session_id=broker.session_id,state='waiting')
        stage_initial_review(legacy,capture_review(legacy,prepared,authority));legacy.state='started'
        legacy.started_at=datetime.now(timezone.utc)-timedelta(milliseconds=200)
        root2.executor_reserved_ms=4000;root2.native_calls=1;db.add(legacy);db.commit()
        broker.retain(legacy.action_id,prepared,proposer=proposer)
        original_review=broker._review_identity(db,legacy,owner)[0]
        assert original_review.version==1 and 'execution_binding' not in original_review.action.model_dump()
        direct=scopes.bind(db,s.room,s.ada,trigger2.event_id,run.generation)
        waiting=ToolAction(execution_binding=direct)
        assert scopes.reserve_executor(db,waiting)==0
        legacy.state='completed';legacy.completed_at=datetime.now(timezone.utc);db.commit()
        assert 0<scopes.reserve_executor(db,waiting)<4000


def test_actual_migration34_preserves_legacy_rows_reviews_and_grants(tmp_path,monkeypatch):
    import sqlite3
    from sqlalchemy import create_engine
    from app.core import migrate as migration
    from test_migrations import legacy_db
    path=tmp_path/'synthetic33.sqlite';legacy_db(path)
    monkeypatch.setenv('GAMJA_APP_DATA_DIR',str(tmp_path/'appdata'))
    engine=create_engine(f'sqlite:///{path}')
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(migration,'REVISIONS',migration.REVISIONS[:33]);migration.migrate_database(engine)
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO tool_actions(action_id,room_id,agent_id,family,arguments,state,review_snapshot) VALUES(?,51,41,'read_file','{}','completed',?)",(str(uuid4()),'{"version":1,"preserve":"exact bytes"}'))
            db.commit()
            tables=[t for t, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            cols={t:[r[1] for r in db.execute(f'PRAGMA table_info("{t}")')] for t in tables}
            before={t:db.execute(f'SELECT * FROM "{t}" ORDER BY rowid').fetchall() for t in tables}
        with monkeypatch.context() as scoped:
            scoped.setattr(migration,'REVISIONS',migration.REVISIONS[:34]);migration.migrate_database(engine)
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT max(version) FROM schema_version').fetchone()==(34,)
            assert db.execute('SELECT count(*) FROM execution_scopes').fetchone()==(0,)
            assert db.execute('SELECT execution_binding,executor_reservation_ms FROM tool_actions').fetchone()==(None,None)
            for t in tables:
                if t=='schema_version':continue
                fields=','.join('"'+c+'"' for c in cols[t])
                assert db.execute(f'SELECT {fields} FROM "{t}" ORDER BY rowid').fetchall()==before[t]
            assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
            assert db.execute('PRAGMA integrity_check').fetchone()==('ok',)
        with monkeypatch.context() as scoped:
            scoped.setattr(migration,'REVISIONS',migration.REVISIONS[:34]);migration.migrate_database(engine)
    finally:engine.dispose()
