"""Disposable pre-start boundaries, not conditional FD/native-effect proof."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm.attributes import flag_modified

from test_room_state import headers, room_client
from test_review_preview import preview_fixture
from test_review_decision import approve
from app.models.collaboration import BroadMode, ProviderConnection, RoomEvent, RoomGrant, ToolAction
from app.models.models import Agent, Conversation, User
from app.services.action_review import ReviewChanged
from app.services.grants import ReviewRequired, claim, now, set_broad
from app.services.scheduler import ExecutorBudget


def inspected(client, owner, broker, action, scope="once"):
    view = broker.preview(action, owner_id=owner)
    result = approve(client, owner, action, view["review_id"], scope=scope,
                     acknowledge_unverified_context=view["approval_policy"]["unverified_context_required"])
    assert result.status_code == 200, result.text
    return result.json()


def locked_claim(broker, action):
    with broker.sessions() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        saved = db.get(ToolAction, action.action_id)
        ref = claim(db, saved, broker.session_id, saved.grant_id, broker=broker)
        db.commit()
        return ref


@pytest.mark.parametrize("mode", ["once", "room_session", "broad"])
@pytest.mark.parametrize("change", ["content", "replace", "absent", "permissions", "connection", "owner", "epoch"])
def test_every_mode_rechecks_original_version_and_identity(preview_fixture, mode, change):
    client, owner, broker, path, proposal = preview_fixture
    epochs = SimpleNamespace(value="a"*64)
    epochs.current = lambda _: epochs.value
    broker.owner_epochs = epochs
    action = proposal()
    if mode == "broad":
        with broker.sessions() as db: set_broad(db, owner, broker.session_id, True, None, always=True)
    else:
        inspected(client, owner, broker, action, mode)
    if change == "content": path.write_bytes(b"BBBB")
    elif change == "replace":
        replacement = path.with_name("replacement"); replacement.write_bytes(b"AAAA"); replacement.replace(path)
    elif change == "absent": path.unlink()
    elif change == "permissions": path.chmod(0o400)
    elif change == "epoch": epochs.value = "b"*64
    else:
        with broker.sessions() as db:
            if change == "connection":
                agent = db.get(Agent, action.agent_id)
                db.get(ProviderConnection, agent.connection_id).readiness_revision += 1
            else: db.get(User, owner).is_active = False
            db.commit()
    with pytest.raises(ReviewChanged): locked_claim(broker, action)
    with broker.sessions() as db:
        assert db.get(ToolAction, action.action_id).started_at is None
        assert db.query(RoomGrant).filter_by(decision="consumed").count() == 0
        assert db.query(RoomEvent).filter_by(idempotency_key=f"tool-start:{action.action_id}").count() == 0


@pytest.mark.parametrize("mode", ["once", "room_session", "broad"])
def test_supported_claim_carries_immutable_original_review(preview_fixture, mode):
    client, owner, broker, _, proposal = preview_fixture
    action = proposal()
    if mode == "broad":
        with broker.sessions() as db: set_broad(db, owner, broker.session_id, True, 5)
    else: inspected(client, owner, broker, action, mode)
    ref = locked_claim(broker, action)
    assert ref.scope == mode and ref.review_id == action.review_id
    assert ref.review.action.action_id == action.action_id and ref.review.authority.owner_id == owner


@pytest.mark.parametrize("change", ["before-registration", "corrupt-pin", "legacy-binding", "bool-version", "bool-authority", "empty-pins", "null-families", "null-targets"])
def test_reuse_never_replaces_prior_target_pins_or_blesses_legacy(preview_fixture, change):
    client, owner, broker, path, proposal = preview_fixture
    first = proposal()
    grant_id = inspected(client, owner, broker, first, "room_session")["grant_id"]
    with broker.sessions() as db: original = db.get(RoomGrant, grant_id).scope
    if change == "before-registration": path.write_bytes(b"BBBB")
    second = proposal()
    if change != "before-registration":
        with broker.sessions() as db:
            grant = db.get(RoomGrant, grant_id); value = dict(grant.scope)
            if change == "legacy-binding": value.pop("authority_binding")
            elif change == "bool-version": value["review_version"] = True
            elif change == "bool-authority":
                value["authority_binding"] = dict(value["authority_binding"])
                value["authority_binding"]["euid"] = bool(value["authority_binding"]["euid"])
            elif change == "empty-pins": value["target_pins"] = []
            elif change == "null-families": value["families"] = None
            elif change == "null-targets": value["targets"] = None
            else:
                value["target_pins"] = [{**value["target_pins"][0], "status": "absent", "leaf": None}]
            grant.scope = value; flag_modified(grant, "scope"); db.commit()
    assert locked_claim(broker, second) is None
    with broker.sessions() as db:
        grant = db.get(RoomGrant, grant_id)
        assert grant.decision == "approved" and db.get(ToolAction, second.action_id).state == "waiting"
        if change == "before-registration": assert grant.scope == original


@pytest.mark.parametrize("family,args", [("pointer_click", {"x":1,"y":2}),
    ("keyboard_text", {"text":"private synthetic"}), ("keyboard_chord", {"keys":["CTRL","A"]}),
    ("browser_interact", {"url":"https://example.com", "selector":"button", "operation":"click"})])
def test_broad_does_not_automatically_claim_opaque_and_setting_stays_enabled(preview_fixture, family, args):
    _, owner, broker, _, proposal = preview_fixture
    fake = SimpleNamespace(desktop=SimpleNamespace(native=SimpleNamespace(context_id=uuid4().hex)),
                           browser=SimpleNamespace(workflows={}))
    action = proposal(family, args, fake, "owned-workflow")
    with broker.sessions() as db: set_broad(db, owner, broker.session_id, True, None, always=True)
    assert locked_claim(broker, action) is None
    with broker.sessions() as db:
        assert db.get(BroadMode, owner).enabled is True
        assert db.get(ToolAction, action.action_id).approved_at is None


@pytest.mark.parametrize("elapsed", [61, 301])
def test_expired_opaque_never_starts_and_requires_a_different_inspected_token(preview_fixture, monkeypatch, elapsed):
    client, owner, broker, _, proposal = preview_fixture
    clock = SimpleNamespace(value=now())
    monkeypatch.setattr("app.services.grants.now", lambda: clock.value)
    fake = SimpleNamespace(desktop=SimpleNamespace(native=SimpleNamespace(context_id=uuid4().hex)))
    action = proposal("pointer_click", {"x":1,"y":2}, fake, "owned-workflow")
    old = action.review_id
    granted = inspected(client, owner, broker, action)
    clock.value += timedelta(seconds=elapsed)
    with broker.sessions() as db:
        db.execute(text("BEGIN IMMEDIATE")); saved = db.get(ToolAction, action.action_id)
        with pytest.raises(ReviewRequired): claim(db, saved, broker.session_id, saved.grant_id, broker=broker)
        db.commit()
        assert saved.state == "waiting" and saved.started_at is None
        assert saved.approved_at is None and saved.approval_owner_id is None and saved.grant_id is None
        assert saved.review_id != old
        assert db.get(RoomGrant, granted["grant_id"]).decision == "revoked"
        history = db.query(RoomEvent).filter_by(room_id=action.room_id).all()
        assert any(e.payload.get("previous_review_id") == old for e in history)
        assert any(e.payload.get("review_id") == old and e.payload.get("review_snapshot") for e in history)
    assert approve(client, owner, action, old, acknowledge_unverified_context=True).status_code == 409
    new = inspected(client, owner, broker, action)
    assert new["review_id"] != old and locked_claim(broker, action).scope == "once"


def test_opaque_start_deadline_does_not_cancel_an_already_started_action(preview_fixture, monkeypatch):
    client, owner, broker, _, proposal = preview_fixture
    clock = SimpleNamespace(value=now()); monkeypatch.setattr("app.services.grants.now", lambda: clock.value)
    monkeypatch.setattr("app.services.tool_broker.now", lambda: clock.value)
    fake = SimpleNamespace(desktop=SimpleNamespace(native=SimpleNamespace(context_id=uuid4().hex)))
    action = proposal("pointer_click", {"x":1,"y":2}, fake, "owned-workflow")
    inspected(client, owner, broker, action); ref = locked_claim(broker, action)
    with broker.sessions() as db:
        saved=db.get(ToolAction, action.action_id); saved.state="started"; saved.started_at=clock.value; db.commit()
    clock.value += timedelta(seconds=80)
    assert broker._active(action.action_id, ref) is True
    with broker.sessions() as db:
        grant = db.get(RoomGrant, ref.grant_id); grant.decision="revoked"; db.commit()
    assert broker._active(action.action_id, ref) is False


@pytest.mark.asyncio
async def test_own_write_is_not_invalidated_by_active_metadata_polling_and_audit_links_review(preview_fixture):
    _, owner, broker, path, proposal = preview_fixture
    seen=[]
    async def effect(action, ref, cancel):
        seen.append(ref); path.write_bytes(b"BBBB"); await asyncio.sleep(.12)
        return "synthetic write complete"
    action=proposal("write_file", {"path":str(path),"content":"BBBB"}, effect)
    client=preview_fixture[0]; inspected(client, owner, broker, action)
    action._raw_arguments=broker._previews[action.action_id].raw_arguments
    result=await broker.execute(action,None,asyncio.Event(),ExecutorBudget(2))
    assert result.state=="completed" and len(seen)==1 and path.read_bytes()==b"BBBB"
    with broker.sessions() as db:
        start=db.query(RoomEvent).filter_by(idempotency_key=f"tool-start:{action.action_id}").one()
        final=db.query(RoomEvent).filter_by(idempotency_key=f"tool-result:{action.action_id}").one()
        assert start.payload["review_id"]==final.payload["review_id"]==action.review_id
        assert start.payload["authority_source"]=="once"


@pytest.mark.parametrize("family,args", [("screenshot",{}), ("cancel_process",{"process_id":"a"*32}),
    ("browser_inspect",{"url":"https://example.com"})])
def test_unknown_adapter_observation_cannot_become_broad_execution_authority(preview_fixture, family, args):
    _, owner, broker, _, proposal=preview_fixture
    action=proposal(family,args,None,"owned-workflow")
    with broker.sessions() as db:set_broad(db,owner,broker.session_id,True,5)
    with pytest.raises(ReviewChanged):locked_claim(broker,action)


@pytest.mark.parametrize("mode", ["once", "room_session", "broad"])
@pytest.mark.parametrize("resource", ["executable", "cwd"])
def test_process_launch_observations_are_not_exempt_from_any_mode(preview_fixture, mode, resource):
    client,owner,broker,path,proposal=preview_fixture
    cwd=path.parent/"working";cwd.mkdir()
    action=proposal("start_process",{"executable":str(path),"cwd":str(cwd)})
    if mode=="broad":
        with broker.sessions() as db:set_broad(db,owner,broker.session_id,True,5)
    else:inspected(client,owner,broker,action,mode)
    if resource=="executable":path.write_bytes(b"BBBB")
    else:cwd.rename(path.parent/"moved-working");cwd.mkdir()
    with pytest.raises(ReviewChanged):locked_claim(broker,action)


@pytest.mark.parametrize("mode", ["once", "room_session", "broad"])
def test_inflight_authority_recheck_does_not_keep_old_connection_alive(preview_fixture, mode):
    client,owner,broker,_,proposal=preview_fixture
    action=proposal()
    if mode=="broad":
        with broker.sessions() as db:set_broad(db,owner,broker.session_id,True,5)
    else:inspected(client,owner,broker,action,mode)
    ref=locked_claim(broker,action)
    with broker.sessions() as db:
        saved=db.get(ToolAction,action.action_id);saved.state="started";saved.started_at=now();db.commit()
    assert broker._active(action.action_id,ref) is True
    with broker.sessions() as db:
        agent=db.get(Agent,action.agent_id)
        db.get(ProviderConnection,agent.connection_id).readiness_revision+=1;db.commit()
    assert broker._active(action.action_id,ref) is False


@pytest.mark.asyncio
async def test_start_audit_failure_rolls_back_once_claim_before_any_effect(preview_fixture,monkeypatch):
    client,owner,broker,_,proposal=preview_fixture
    calls=[]
    async def effect(*args):calls.append(True);return "should not run"
    action=proposal(executor=effect);grant_id=inspected(client,owner,broker,action)["grant_id"]
    import app.services.tool_broker as module
    original=module.stage_event
    def fail_start(db,room,actor,kind,payload,*args,**kwargs):
        if payload.get("status")=="started":raise RuntimeError("synthetic audit failure")
        return original(db,room,actor,kind,payload,*args,**kwargs)
    monkeypatch.setattr(module,"stage_event",fail_start)
    outcome=await broker.execute(action,None,asyncio.Event(),ExecutorBudget(2))
    assert outcome.state=="failed" and calls==[]
    with broker.sessions() as db:
        assert db.get(RoomGrant,grant_id).decision=="approved"
        assert db.get(ToolAction,action.action_id).started_at is None
        assert db.query(RoomEvent).filter_by(idempotency_key=f"tool-start:{action.action_id}").count()==0
        assert db.query(RoomEvent).filter_by(idempotency_key=f"tool-result:{action.action_id}").count()==1


def test_legacy_action_cannot_acquire_broad_authority_from_current_state(preview_fixture):
    _,owner,broker,_,proposal=preview_fixture
    action=proposal()
    with broker.sessions() as db:
        row=db.get(ToolAction,action.action_id);row.review_id=row.review_snapshot=None;db.commit()
        set_broad(db,owner,broker.session_id,True,5)
    with pytest.raises(ReviewChanged):locked_claim(broker,action)


@pytest.mark.parametrize("mode", ["once", "room_session"])
def test_inflight_grant_cannot_switch_its_approval_token(preview_fixture,mode):
    client,owner,broker,_,proposal=preview_fixture
    action=proposal();inspected(client,owner,broker,action,mode);ref=locked_claim(broker,action)
    with broker.sessions() as db:
        saved=db.get(ToolAction,action.action_id);saved.state="started";saved.started_at=now();db.commit()
    assert broker._active(action.action_id,ref) is True
    with broker.sessions() as db:
        grant=db.get(RoomGrant,ref.grant_id);grant.scope={**grant.scope,"approval_review_id":str(uuid4())};db.commit()
    assert broker._active(action.action_id,ref) is False
