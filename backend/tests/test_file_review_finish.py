"""File effects and result/pin transactions use only disposable SQLite/files."""
import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from test_room_state import room_client
from test_review_preview import preview_fixture
from test_review_claim import inspected, locked_claim
from app.models.collaboration import RoomEvent, RoomGrant, ToolAction
from app.services.scheduler import ExecutorBudget
from app.services.tools.files import execute_file


def outcome(broker, action, grant=None):
    # Scheduler hands the original volatile arguments to execute; the ORM
    # preview fixture deliberately stores only their public/redacted form.
    action._raw_arguments=broker._previews[action.action_id].raw_arguments
    return asyncio.run(broker.execute(action, grant, asyncio.Event(), ExecutorBudget(5)))


def info(broker, action, grant_id):
    with broker.sessions() as db:
        grant=db.get(RoomGrant,grant_id)
        events=db.query(RoomEvent).filter_by(room_id=action.room_id,kind="tool_result").all()
        return dict(grant.scope),[(e.payload,e.idempotency_key) for e in events]


def test_own_success_advances_room_pins_and_allows_next_real_write(preview_fixture):
    client,owner,broker,path,proposal=preview_fixture
    first=proposal("write_file",{"path":str(path),"content":"first"},execute_file)
    gid=inspected(client,owner,broker,first,"room_session")["grant_id"]
    before,_=info(broker,first,gid)
    result=outcome(broker,first)
    assert result.state=="completed",result.text
    scope,events=info(broker,first,gid)
    assert scope["target_pins"]!=before["target_pins"]
    assert scope["approval_review_id"]==before["approval_review_id"]
    assert events[0][0]["state"]=="completed" and events[0][0]["review_id"]==first.review_id
    second=proposal("write_file",{"path":str(path),"content":"second"},execute_file)
    result=outcome(broker,second,gid)
    assert result.state=="completed" and path.read_text()=="second"
    with broker.sessions() as db:
        assert db.query(RoomGrant).count()==1
        assert db.get(ToolAction,second.action_id).grant_id==gid
        proofs=db.query(RoomEvent).filter_by(room_id=first.room_id,kind="activity").all()
        verified=[e.payload for e in proofs if e.payload.get("status")=="file_effect_verified"]
        assert len(verified)==2 and all(e["pins_advanced"] for e in verified)
        assert all(e["content_CAS"]=="unsupported" for e in verified)
        assert '"content"' not in str(verified)


@pytest.mark.parametrize("family",["read_file","write_file","delete_file","move_file"])
def test_file_terminal_and_pins_describe_exact_real_effect(preview_fixture,family):
    client,owner,broker,path,proposal=preview_fixture
    args={"path":str(path)}
    if family=="write_file":args["content"]="new"
    if family=="move_file":
        other=path.with_name("destination");other.write_text("old")
        args={"source":str(path),"destination":str(other)}
    action=proposal(family,args,execute_file)
    gid=inspected(client,owner,broker,action,"room_session")["grant_id"]
    result=outcome(broker,action)
    assert result.state=="completed",result.text
    scope,events=info(broker,action,gid)
    assert scope["target_pins"]==[t.model_dump(mode="json") for t in result.file_postconditions.after_targets]
    assert len(events)==1 and events[0][0]["state"]==result.state
    if family in ("delete_file","move_file"):assert scope["target_pins"][0]["status"]=="absent"


def test_external_change_before_next_registration_never_overwrites_pins(preview_fixture):
    client,owner,broker,path,proposal=preview_fixture
    first=proposal("write_file",{"path":str(path),"content":"first"},execute_file)
    gid=inspected(client,owner,broker,first,"room_session")["grant_id"]
    assert outcome(broker,first).state=="completed"
    before,_=info(broker,first,gid)
    path.write_text("outsider")
    second=proposal("write_file",{"path":str(path),"content":"second"},execute_file)
    assert locked_claim(broker,second) is None
    after,_=info(broker,first,gid)
    assert after==before and path.read_text()=="outsider"


@pytest.mark.parametrize("corruption",["target-after-effect","wrong-review","wrong-action"])
def test_post_effect_uncertainty_matches_canonical_and_keeps_original_pins(preview_fixture,corruption):
    client,owner,broker,path,proposal=preview_fixture
    async def changed(action,ref,cancel):
        result=await execute_file(action,ref,cancel)
        assert result.state=="completed"
        if corruption=="target-after-effect":path.write_text("external-after-effect")
        else:
            key="review_id" if corruption=="wrong-review" else "action_id"
            result=replace(result,file_postconditions=result.file_postconditions.model_copy(update={key:str(uuid4())}))
        return result
    action=proposal("write_file",{"path":str(path),"content":"effect"},changed)
    gid=inspected(client,owner,broker,action,"room_session")["grant_id"]
    before,_=info(broker,action,gid)
    result=outcome(broker,action)
    assert result.state=="uncertain"
    after,events=info(broker,action,gid)
    assert after==before and len(events)==1
    assert events[0][0]["state"]==result.state and events[0][0]["text"]==result.safe_text
    assert events[0][0]["error_code"]=="file_postcondition_unconfirmed"
    with broker.sessions() as db:
        assert db.query(RoomEvent).filter_by(idempotency_key=f"file-review-result:{action.action_id}").count()==0


def test_audit_failure_rolls_back_pin_advance_and_terminal_together(preview_fixture,monkeypatch):
    import app.services.tool_broker as module
    client,owner,broker,path,proposal=preview_fixture
    action=proposal("write_file",{"path":str(path),"content":"effect"},execute_file)
    gid=inspected(client,owner,broker,action,"room_session")["grant_id"]
    before,_=info(broker,action,gid)
    stage=module.stage_event
    def fail(db,room,actor,kind,payload,key,**kwargs):
        result=stage(db,room,actor,kind,payload,key,**kwargs)
        if key==f"file-review-result:{action.action_id}":raise RuntimeError("owned fixture audit failure")
        return result
    monkeypatch.setattr(module,"stage_event",fail)
    result=outcome(broker,action)
    assert result.state=="uncertain" and path.read_text()=="effect"
    after,events=info(broker,action,gid)
    assert after==before and len(events)==1 and events[0][0]["state"]=="uncertain"
    with broker.sessions() as db:
        assert db.query(RoomEvent).filter_by(idempotency_key=f"file-review-result:{action.action_id}").count()==0


def test_second_registered_old_version_cannot_run_after_first_changes_it(preview_fixture):
    client,owner,broker,path,proposal=preview_fixture
    first=proposal("write_file",{"path":str(path),"content":"first"},execute_file)
    second=proposal("write_file",{"path":str(path),"content":"second"},execute_file)
    gid=inspected(client,owner,broker,first,"room_session")["grant_id"]
    assert outcome(broker,first).state=="completed"
    scope,_=info(broker,first,gid)
    result=outcome(broker,second,gid)
    assert result.state=="denied" and path.read_text()=="first"
    after,events=info(broker,first,gid)
    assert after==scope and len(events)==2


def test_unverified_custom_result_does_not_advance_pins(preview_fixture):
    from app.services.tool_broker import ActionResult
    client,owner,broker,path,proposal=preview_fixture
    async def legacy_fixture(*_):return ActionResult("completed","synthetic no effect")
    action=proposal("read_file",{"path":str(path)},legacy_fixture)
    gid=inspected(client,owner,broker,action,"room_session")["grant_id"]
    before,_=info(broker,action,gid)
    assert outcome(broker,action).state=="completed"
    after,_=info(broker,action,gid)
    assert after==before
    with broker.sessions() as db:
        assert db.query(RoomEvent).filter_by(idempotency_key=f"file-review-result:{action.action_id}").count()==0


def test_pin_conflict_after_effect_preserves_other_pins_and_reports_uncertain(preview_fixture):
    from app.services.review_snapshot import StoredTarget
    from app.services.action_review import observe_file_target
    client,owner,broker,path,proposal=preview_fixture
    conflicting=[]
    async def changed(action,ref,cancel):
        result=await execute_file(action,ref,cancel)
        assert result.state=="completed"
        # Represents another stored result winning before this settlement;
        # the current result must not replace those pins from fresh paths.
        with broker.sessions() as db:
            grant=db.get(RoomGrant,ref.grant_id)
            pins=[StoredTarget.model_validate_json(observe_file_target(str(path)).model_dump_json()).model_dump(mode="json")]
            grant.scope={**grant.scope,"target_pins":pins};conflicting.append(dict(grant.scope));db.commit()
        return result
    action=proposal("write_file",{"path":str(path),"content":"effect"},changed)
    gid=inspected(client,owner,broker,action,"room_session")["grant_id"]
    result=outcome(broker,action)
    after,events=info(broker,action,gid)
    assert result.state=="uncertain" and after==conflicting[0]
    assert len(events)==1 and events[0][0]["state"]=="uncertain"


@pytest.mark.parametrize("mode",["once","broad"])
def test_real_non_room_file_modes_validate_evidence_without_advancing_grant_pins(preview_fixture,mode):
    from app.services.grants import set_broad
    client,owner,broker,path,proposal=preview_fixture
    action=proposal("write_file",{"path":str(path),"content":"effect"},execute_file)
    if mode=="once":inspected(client,owner,broker,action,"once")
    else:
        with broker.sessions() as db:set_broad(db,owner,broker.session_id,True,5)
    result=outcome(broker,action)
    assert result.state=="completed" and path.read_text()=="effect"
    with broker.sessions() as db:
        proof=db.query(RoomEvent).filter_by(idempotency_key=f"file-review-result:{action.action_id}").one()
        assert proof.payload["authority_source"]==mode and proof.payload["pins_advanced"] is False
        terminal=db.query(RoomEvent).filter_by(idempotency_key=f"tool-result:{action.action_id}").one()
        assert terminal.payload["state"]==result.state
