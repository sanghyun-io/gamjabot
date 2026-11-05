"""Explicit current-session consent in disposable DB/owned adapter fixtures.

No installed app, real native input, external account, or renderer deadline
proof. Existing once expiry tests remain independent and unchanged.
"""
from datetime import timedelta
from types import SimpleNamespace
import time

import pytest
from sqlalchemy.orm.attributes import flag_modified

from test_room_state import room_client
from test_review_preview import preview_fixture
from test_review_claim import inspected, locked_claim
from test_review_decision import approve
from app.models.collaboration import ProviderConnection, RoomAgentRun, RoomGrant, ToolAction
from app.services.action_review import ReviewChanged
from app.services.grants import now, revoke
from app.services.resource_authority import resource_live
from app.services.review_execution import grant_lifetime_valid
from app.services.review_operation import operation_start
from app.services.tools.browser_start import BrowserStart


def adapter(family):
    if family == "browser_interact":
        session = SimpleNamespace(context_id="c"*32, deadline=time.monotonic()+300,
            page=SimpleNamespace(is_closed=lambda:False), context=object(),
            authority=SimpleNamespace(active=lambda:True, _live=lambda _:True))
        return SimpleNamespace(browser=SimpleNamespace(workflows={})), session
    return SimpleNamespace(desktop=SimpleNamespace(native=SimpleNamespace(context_id="c"*32))), None


def proposal_with_adapter(fixture, family="pointer_click"):
    _, _, broker, _, proposal = fixture
    fake, session = adapter(family)
    args = {"pointer_click":{"x":1,"y":2}, "keyboard_text":{"text":"owned fixture"},
            "keyboard_chord":{"keys":["META","a"]},
            "browser_interact":{"url":"https://example.invalid/","operation":"click","selector":"button"}}[family]
    if session is not None:
        initial=proposal("browser_navigate",{"url":args["url"]},fake,"owned-workflow")
        session.room_id,session.agent_id,session.workflow_id=initial.room_id,initial.agent_id,initial.workflow_id
        fake.browser.workflows[(initial.room_id,initial.agent_id,initial.workflow_id)]=session
        fake.browser.sessions, fake.browser.owner_epochs=broker.sessions,broker.owner_epochs
    action=proposal(family,args,fake,"owned-workflow")
    return action,fake,session,args


def start_row(broker, action):
    with broker.sessions() as db:
        run=db.get(RoomAgentRun,(action.room_id,action.agent_id));run.generation=9
        run.trigger_event_id=action.trigger_event_id;run.state="idle";run.paused=False;db.commit()
    ref=locked_claim(broker,action)
    assert ref is not None
    with broker.sessions() as db:
        row=db.get(ToolAction,action.action_id);row.state="started";row.started_at=now();db.commit()
    action.arguments=broker._previews[action.action_id].raw_arguments
    return ref


@pytest.mark.parametrize("family",["pointer_click","keyboard_text","keyboard_chord","browser_interact"])
@pytest.mark.parametrize("seconds",[61,2100])
def test_explicit_session_approval_reuses_without_60_second_or_old_30_minute_limit(preview_fixture,monkeypatch,family,seconds):
    client,owner,broker,_,proposal=preview_fixture
    action,fake,session,args=proposal_with_adapter(preview_fixture,family)
    view=broker.preview(action,owner_id=owner)
    assert view["approval_policy"]["scopes"]==["once","room_session"]
    assert approve(client,owner,action,view["review_id"],scope="room_session").status_code==409
    granted=inspected(client,owner,broker,action,"room_session")
    assert granted["expires_at"] is None
    with broker.sessions() as db:
        grant=db.get(RoomGrant,granted["grant_id"])
        from app.services.review_execution import approval_time
        later=approval_time(grant.scope)+timedelta(seconds=seconds)
        assert grant.scope["lifetime"]=="session"
    for module in ["grants","tool_broker","resource_authority","review_operation","tools.browser_start"]:
        monkeypatch.setattr("app.services."+module+".now",lambda:later)
    first_ref=start_row(broker,action)
    assert first_ref.scope=="room_session" and broker._active(action.action_id,first_ref)
    assert resource_live(broker.sessions,first_ref,9,allow_completed=False)
    if family=="browser_interact":
        key=(action.room_id,action.agent_id,action.workflow_id)
        BrowserStart(fake.browser,key,action,first_ref,session).check(session)
    else:
        assert operation_start(action,first_ref,sessions=broker.sessions,owner_epochs=None,context_id="c"*32) is None
    with broker.sessions() as db:
        row=db.get(ToolAction,action.action_id);row.state="completed";row.completed_at=later;db.commit()
    second=proposal(family,args,fake,"owned-workflow")
    second_ref=start_row(broker,second)
    assert second_ref.grant_id==first_ref.grant_id and second_ref.review_id!=first_ref.review_id
    assert second_ref.approval_review_id==first_ref.approval_review_id
    with broker.sessions() as db:
        assert db.query(RoomGrant).count()==1
        assert db.get(ToolAction,second.action_id).approval_scope=="room_session"


@pytest.mark.parametrize("change",["workflow","family","target","session","connection","context","revoked","consent","lifetime"])
def test_session_approval_does_not_expand_to_a_changed_scope(preview_fixture,change):
    client,owner,broker,_,proposal=preview_fixture
    first,fake,_,args=proposal_with_adapter(preview_fixture)
    grant_id=inspected(client,owner,broker,first,"room_session")["grant_id"]
    family="pointer_scroll" if change=="family" else "pointer_click"
    values={"x":1,"y":2,"delta":3} if family=="pointer_scroll" else args
    workflow="other-workflow" if change=="workflow" else "owned-workflow"
    if change=="context":fake.desktop.native.context_id="d"*32
    if change=="connection":
        with broker.sessions() as db:
            conn=db.get(ProviderConnection,broker._proposers[first.action_id].connection_id)
            conn.readiness_revision+=1;db.commit()
        # The old action is never re-observed under the new identity.
        with pytest.raises(ReviewChanged):locked_claim(broker,first)
        return
    if change=="revoked":
        with broker.sessions() as db:revoke(db,grant_id,first.room_id,owner)
    if change in ("target","session","consent","lifetime"):
        with broker.sessions() as db:
            grant=db.get(RoomGrant,grant_id);value=dict(grant.scope)
            if change=="target":value["targets"]=["desktop:keyboard"]
            elif change=="session":value["session_id"]="other-session"
            elif change=="consent":value["acknowledge_unverified_context"]=False
            else:value.pop("lifetime")
            grant.scope=value;flag_modified(grant,"scope");db.commit()
    second=proposal(family,values,fake,workflow)
    assert locked_claim(broker,second) is None


def test_no_room_session_consent_for_an_unobserved_browser_context(preview_fixture):
    client,owner,broker,_,proposal=preview_fixture
    fake=SimpleNamespace(browser=SimpleNamespace(workflows={}))
    action=proposal("browser_interact",{"url":"https://example.invalid/","selector":"button","operation":"click"},fake,"owned-workflow")
    view=broker.preview(action,owner_id=owner)
    assert view["approval_policy"]["scopes"]==["once"]
    assert approve(client,owner,action,view["review_id"],scope="room_session",acknowledge_unverified_context=True).status_code==409


def test_null_expiry_is_not_a_legacy_or_wrong_session_permission():
    clock=now();scope={"kind":"room_session","session_id":"current","lifetime":"session"}
    assert grant_lifetime_valid(scope,None,session_id="current",clock=clock)
    assert not grant_lifetime_valid(scope,None,session_id="other",clock=clock)
    assert not grant_lifetime_valid({k:v for k,v in scope.items() if k!="lifetime"},None,session_id="current",clock=clock)
    assert not grant_lifetime_valid({**scope,"kind":"once"},None,session_id="current",clock=clock)
    legacy={"kind":"room_session","session_id":"current"}
    assert grant_lifetime_valid(legacy,clock+timedelta(seconds=1),session_id="current",clock=clock)
    assert not grant_lifetime_valid(legacy,clock-timedelta(seconds=1),session_id="current",clock=clock)


def test_browser_reusable_grant_does_not_follow_a_replacement_context(preview_fixture):
    client,owner,broker,_,proposal=preview_fixture
    first,fake,session,args=proposal_with_adapter(preview_fixture,"browser_interact")
    inspected(client,owner,broker,first,"room_session")
    session.context_id="d"*32
    second=proposal("browser_interact",args,fake,"owned-workflow")
    assert locked_claim(broker,second) is None
