"""Disposable DB plus owned source processes; no installed runtime/Chrome."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
import sys
import time

import pytest

from test_room_state import room_client
from test_review_preview import preview_fixture
from test_review_claim import inspected, locked_claim
from app.models.collaboration import BroadMode, ProviderConnection, RoomAgentRun, RoomGrant, ToolAction
from app.models.models import Agent, Conversation, User
from app.services.grants import now, set_broad
from app.services.resource_authority import resource_live
from app.services.tools.process import ProcessExecutor, resource_authorized
from app.services.tools.browser import WorkflowAuthority, origin


def resource(preview_fixture, mode, family="start_process", command=None):
    client,owner,broker,path,proposal=preview_fixture
    epochs=SimpleNamespace(value="a"*64)
    epochs.current=lambda _:epochs.value
    broker.owner_epochs=epochs
    cwd=path.parent/"process-cwd";cwd.mkdir(exist_ok=True)
    args=({"executable":sys.executable,"argv":["-c",command or "print('owned')"],
           "cwd":str(cwd),"background_seconds":10} if family=="start_process" else
          {"url":"https://example.com"})
    fake=SimpleNamespace(browser=SimpleNamespace(workflows={})) if family.startswith("browser_") else None
    action=proposal(family,args,fake,"owned-workflow")
    with broker.sessions() as db:
        run=db.get(RoomAgentRun,(action.room_id,action.agent_id))
        assert run is not None
        run.generation=7;run.trigger_event_id=action.trigger_event_id;run.state="idle";run.paused=False
        db.commit()
    if mode=="broad":
        with broker.sessions() as db:set_broad(db,owner,broker.session_id,True,None,always=True)
    else:inspected(client,owner,broker,action,mode)
    ref=locked_claim(broker,action)
    assert ref is not None and ref.run_generation==7
    with broker.sessions() as db:
        saved=db.get(ToolAction,action.action_id);saved.state="completed";saved.started_at=now();saved.completed_at=now();db.commit()
    return client,owner,broker,action,ref,epochs


@pytest.mark.parametrize("mode",["once","room_session","broad"])
@pytest.mark.parametrize("family",["start_process","browser_navigate"])
def test_original_authority_remains_live_after_canonical_completed_result(preview_fixture,mode,family):
    _,_,broker,action,ref,epochs=resource(preview_fixture,mode,family)
    assert resource_live(broker.sessions,ref,7,owner_epochs=epochs)
    if family.startswith("browser_"):
        authority=WorkflowAuthority(broker.sessions,action.room_id,action.agent_id,
            owner_epochs=epochs,workflow_id=action.workflow_id)
        authority.add(origin("https://example.com"),ref)
        assert authority.active() and authority.allows(origin("https://example.com"))
    else:
        owned=SimpleNamespace(room_id=action.room_id,agent_id=action.agent_id,
            session_id=ref.session_id,workflow_id=action.workflow_id,grant_id=ref.grant_id,
            approval_scope=ref.scope,generation=7,authority_ref=ref)
        assert resource_authorized(broker.sessions,owned,owner_epochs=epochs)
        owned.workflow_id="other"
        assert not resource_authorized(broker.sessions,owned,owner_epochs=epochs)


@pytest.mark.parametrize("mode",["once","room_session","broad"])
@pytest.mark.parametrize("change",["connection","owner","agent","room","epoch","epoch-unavailable",
    "generation","trigger","paused","stopped","failed","review","payload","session"])
def test_completed_resource_cannot_rebase_onto_changed_identity_or_run(preview_fixture,mode,change):
    _,owner,broker,action,ref,epochs=resource(preview_fixture,mode)
    if change=="epoch":epochs.value="b"*64
    elif change=="epoch-unavailable":epochs.current=lambda _: (_ for _ in ()).throw(RuntimeError("synthetic unavailable"))
    else:
        with broker.sessions() as db:
            saved=db.get(ToolAction,action.action_id);run=db.get(RoomAgentRun,(action.room_id,action.agent_id))
            if change=="connection":db.get(ProviderConnection,ref.review.authority.proposer.connection_id).readiness_revision+=1
            elif change=="owner":db.get(User,owner).is_active=False
            elif change=="agent":db.get(Agent,action.agent_id).is_active=False
            elif change=="room":db.get(Conversation,action.room_id).is_active=False
            elif change=="generation":run.generation+=1
            elif change=="trigger":run.trigger_event_id=str(uuid4())
            elif change=="paused":run.paused=True
            elif change=="stopped":run.state="stopped"
            elif change=="failed":saved.state="uncertain"
            elif change=="review":saved.review_id=str(uuid4())
            elif change=="payload":saved.payload_digest="b"*64
            elif change=="session":saved.session_id=str(uuid4())
            db.commit()
    assert not resource_live(broker.sessions,ref,7,owner_epochs=epochs)


@pytest.mark.parametrize("mode",["once","room_session"])
@pytest.mark.parametrize("change",["token","revoked","expiry","approval-time","families","targets","workflow"])
def test_resource_stops_if_original_grant_binding_changes(preview_fixture,mode,change):
    _,_,broker,action,ref,epochs=resource(preview_fixture,mode)
    with broker.sessions() as db:
        grant=db.get(RoomGrant,ref.grant_id);scope=dict(grant.scope)
        if change=="token":scope["approval_review_id"]=str(uuid4())
        elif change=="revoked":grant.decision="revoked"
        elif change=="expiry":grant.expires_at=now()-timedelta(seconds=1)
        elif change=="approval-time":scope["approved_at"]=(ref.approved_at-timedelta(seconds=1)).isoformat()+"Z"
        elif change=="families":scope["families"]=None
        elif change=="targets":scope["targets"]=None
        else:
            if mode=="once":scope["action_id"]=str(uuid4())
            else:scope["workflow_id"]="other"
        grant.scope=scope;db.commit()
    assert not resource_live(broker.sessions,ref,7,owner_epochs=epochs)


def test_broad_disable_and_reenable_does_not_resurrect_old_resource_or_foreground(preview_fixture):
    _,owner,broker,action,ref,epochs=resource(preview_fixture,"broad")
    with broker.sessions() as db:
        saved=db.get(ToolAction,action.action_id);saved.state="started";db.commit()
    assert broker._active(action.action_id,ref)
    with broker.sessions() as db:
        set_broad(db,owner,broker.session_id,False,None)
        set_broad(db,owner,broker.session_id,True,None,always=True)
    assert not resource_live(broker.sessions,ref,7,owner_epochs=epochs)
    assert not broker._active(action.action_id,ref)
    with broker.sessions() as db:assert db.get(BroadMode,owner).enabled is True


def test_generation_is_captured_at_claim_not_rebased_at_later_resource_creation(preview_fixture):
    _,_,broker,action,ref,epochs=resource(preview_fixture,"room_session","browser_navigate")
    with broker.sessions() as db:
        db.get(RoomAgentRun,(action.room_id,action.agent_id)).generation=8;db.commit()
    late=WorkflowAuthority(broker.sessions,action.room_id,action.agent_id,
        owner_epochs=epochs,workflow_id=action.workflow_id)
    late.add(origin("https://example.com"),ref)
    assert late.generation==8 and not late.active()
    assert not resource_live(broker.sessions,ref,8,owner_epochs=epochs)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode",["once","room_session","broad"])
async def test_real_returned_background_process_stops_on_original_connection_change(preview_fixture,mode):
    _,_,broker,action,ref,epochs=resource(preview_fixture,mode,command="import time;time.sleep(20)")
    action.arguments=dict(broker._previews[action.action_id].raw_arguments)
    executor=ProcessExecutor(broker.sessions,owner_epochs=epochs)
    try:
        result=await executor(action,ref,asyncio.Event())
        assert result.state=="completed" and len(executor.running)==1
        owned=next(iter(executor.running.values()))
        with broker.sessions() as db:
            db.get(ProviderConnection,ref.review.authority.proposer.connection_id).readiness_revision+=1;db.commit()
        for _ in range(80):
            if not executor.running:break
            await asyncio.sleep(.05)
        assert not executor.running and owned.process.poll() is not None
        assert owned.process.stdout.closed and owned.control_fd is None
    finally:await executor.close()


@pytest.mark.asyncio
async def test_generation_drift_before_process_launch_never_spawns_guardian(preview_fixture,monkeypatch):
    _,_,broker,action,ref,epochs=resource(preview_fixture,"once")
    action.arguments=dict(broker._previews[action.action_id].raw_arguments)
    with broker.sessions() as db:db.get(RoomAgentRun,(action.room_id,action.agent_id)).generation=8;db.commit()
    def forbidden(*args,**kwargs):raise AssertionError("No late guardian may spawn")
    monkeypatch.setattr("app.services.tools.process.subprocess.Popen",forbidden)
    result=await ProcessExecutor(broker.sessions,owner_epochs=epochs)(action,ref,asyncio.Event())
    assert result.state=="failed" and result.error_code=="review_changed"


def test_actual_app_wires_same_owner_epoch_provider_to_executor_resources(room_client):
    client,_=room_client
    executor=client.app.state.local_executor;broker=client.app.state.scheduler.broker
    assert executor.processes.owner_epochs is broker.owner_epochs
    assert executor.browser.owner_epochs is broker.owner_epochs


@pytest.mark.asyncio
async def test_cancelled_startup_reaps_and_closes_captured_stdout_before_executor_close_returns(preview_fixture,monkeypatch):
    from pathlib import Path
    from app.services.tools import process as process_tools
    _,_,broker,action,ref,epochs=resource(preview_fixture,"once")
    action.arguments=dict(broker._previews[action.action_id].raw_arguments)
    directory=Path(action.arguments['cwd']).parent
    frozen=directory/"frozen-source-fixture"
    backend=Path(__file__).resolve().parents[1]
    frozen.write_text(f"#!{sys.executable}\nimport sys,time\nsys.dont_write_bytecode=True\ntime.sleep(.4)\n"
        f"sys.path.insert(0,{str(backend)!r})\nfrom app.services.tools.guardian import main\n"
        "sys.argv=[sys.argv[0],*sys.argv[2:]]\nsys.exit(main())\n")
    frozen.chmod(0o700)
    monkeypatch.setattr(process_tools,"sys",SimpleNamespace(platform=sys.platform,frozen=True,executable=str(frozen)))
    spawn=process_tools.subprocess.Popen;captured=[]
    def capture(*args,**kwargs):
        process=spawn(*args,**kwargs);captured.append(process);return process
    monkeypatch.setattr(process_tools.subprocess,"Popen",capture)
    executor=ProcessExecutor(broker.sessions,owner_epochs=epochs)
    task=asyncio.create_task(executor(action,ref,asyncio.Event()))
    try:
        for _ in range(40):
            if captured:break
            await asyncio.sleep(.01)
        assert len(captured)==1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(task,1)
        await executor.close()
        assert captured[0].poll() is not None and captured[0].stdout.closed
        assert not executor._starting_cleanup and not executor.running
    finally:
        if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
        await executor.close()
