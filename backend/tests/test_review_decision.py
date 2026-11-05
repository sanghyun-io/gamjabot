"""Exact inspected decisions/audit in disposable SQLite, not final effects."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from test_room_state import room_client, headers
from test_review_preview import preview_fixture
from app.models.collaboration import ProviderConnection, RoomEvent, RoomGrant, ToolAction
from app.models.models import Agent
from app.services.grants import decide


def decision_url(action):
    return f"/api/v1/tools/rooms/{action.room_id}/actions/{action.action_id}/decision"


def approve(client, owner, action, review_id=None, **values):
    return client.post(decision_url(action), headers=headers(client, owner), json={
        "decision": "approve", "scope": "once", "review_id": review_id, **values})


@pytest.mark.parametrize("token", [None, "not-a-uuid", str(uuid4()), str(uuid4()).upper()])
def test_missing_or_uninspected_id_never_creates_a_grant(preview_fixture, token):
    client, owner, broker, _, proposal = preview_fixture
    action = proposal()
    assert approve(client, owner, action, token).status_code == 409
    with broker.sessions() as db:
        assert db.query(RoomGrant).count() == 0
        assert db.query(RoomEvent).filter_by(kind="activity").count() == 0
        saved = db.get(ToolAction, action.action_id)
        assert saved.state == "waiting" and saved.approved_at is None


def test_version_change_requires_explicit_new_preview_not_a_decision_side_refresh(preview_fixture):
    client, owner, broker, path, proposal = preview_fixture
    marker = "PRIVATE_FIXTURE_CONTENT"
    action = proposal("write_file", {"path": str(path), "content": marker})
    displayed = broker.preview(action, owner_id=owner)
    path.write_bytes(b"BBBB")
    assert approve(client, owner, action, displayed["review_id"]).status_code == 409
    with broker.sessions() as db:
        assert db.get(ToolAction, action.action_id).review_id == displayed["review_id"]
        assert db.query(RoomGrant).count() == 0
    current = broker.preview(action, owner_id=owner)
    assert current["review_id"] != displayed["review_id"]
    assert approve(client, owner, action, displayed["review_id"]).status_code == 409
    result = approve(client, owner, action, current["review_id"])
    assert result.status_code == 200, result.text
    with broker.sessions() as db:
        saved = db.get(ToolAction, action.action_id)
        grant = db.get(RoomGrant, result.json()["grant_id"])
        events = db.query(RoomEvent).filter_by(room_id=action.room_id, kind="activity").all()
        assert len(events) == 1 and events[0].payload["review_id"] == saved.review_id == current["review_id"]
        assert saved.approved_at is not None and saved.approval_owner_id == owner
        assert events[0].payload["grant_id"] == saved.grant_id == grant.id
        assert grant.scope["authority_binding"] == saved.review_snapshot["authority"]
        assert grant.scope["target_pins"] == saved.review_snapshot["targets"]
        assert marker not in json.dumps([saved.review_snapshot, grant.scope, events[0].payload])
    activity = client.get(f"/api/v1/tools/rooms/{action.room_id}/activity", headers=headers(client, owner)).json()
    assert activity[0]["approved_at"] and activity[0]["review_status"] == "stored"
    assert "review_snapshot" not in activity[0] and marker not in json.dumps(activity)


@pytest.mark.parametrize("change", ["model", "config", "auth", "payload", "epoch", "session"])
def test_identity_payload_and_epoch_cannot_be_relabelled_at_decision(preview_fixture, change):
    client, owner, broker, _, proposal = preview_fixture
    epochs = SimpleNamespace(value="a"*64)
    epochs.current = lambda _: epochs.value
    if change == "epoch": broker.owner_epochs = epochs
    action = proposal()
    token = broker.preview(action, owner_id=owner)["review_id"]
    with broker.sessions() as db:
        agent = db.get(Agent, action.agent_id)
        connection = db.get(ProviderConnection, agent.connection_id)
        if change == "model": agent.model_id = "changed"
        elif change == "config": connection.readiness_revision += 1
        elif change == "auth": connection.auth_grant_revision = 90
        elif change == "session": db.get(ToolAction, action.action_id).session_id = str(uuid4())
        db.commit()
    if change == "epoch": epochs.value = "b"*64
    if change == "payload": broker._previews[action.action_id].raw_arguments["path"] = "/other-fixture"
    assert approve(client, owner, action, token).status_code == 409
    with broker.sessions() as db:
        assert db.query(RoomGrant).count() == 0
        assert db.get(ToolAction, action.action_id).review_id == token


def test_two_concurrent_approvals_have_one_grant_and_one_audit(preview_fixture):
    client, owner, broker, _, proposal = preview_fixture
    action = proposal(); token = broker.preview(action, owner_id=owner)["review_id"]
    auth = headers(client, owner)
    def send(_):
        return client.post(decision_url(action), headers=auth,
            json={"decision": "approve", "scope": "once", "review_id": token}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(send, range(2))) == [200, 409]
    with broker.sessions() as db:
        assert db.query(RoomGrant).count() == 1
        assert db.query(RoomEvent).filter_by(room_id=action.room_id, kind="activity").count() == 1


def test_audit_failure_rolls_back_action_grant_and_approval_fields(preview_fixture, monkeypatch):
    client, owner, broker, _, proposal = preview_fixture
    action = proposal(); token = broker.preview(action, owner_id=owner)["review_id"]
    def fail(*args, **kwargs):
        raise RuntimeError("owned injected audit allocation failure")
    monkeypatch.setattr("app.services.grants.stage_event", fail)
    with broker.sessions() as db, pytest.raises(RuntimeError):
        decide(db, action.action_id, action.room_id, owner, "approve", "once", broker.session_id,
               review_id=token, broker=broker)
    with broker.sessions() as db:
        saved = db.get(ToolAction, action.action_id)
        assert saved.state == "waiting" and saved.grant_id is None
        assert saved.approved_at is None and saved.approval_owner_id is None
        assert db.query(RoomGrant).count() == 0
        assert db.query(RoomEvent).filter_by(room_id=action.room_id, kind="activity").count() == 0


def test_deny_without_preview_survives_lost_original_context(preview_fixture):
    client, owner, broker, _, proposal = preview_fixture
    action = proposal(); broker._previews.clear(); broker._proposers.clear()
    assert client.post(decision_url(action), headers=headers(client, owner), json={"decision": "deny"}).status_code == 200
    with broker.sessions() as db:
        assert db.get(ToolAction, action.action_id).state == "denied"
        assert db.query(RoomGrant).count() == 0
        assert db.query(RoomEvent).filter_by(room_id=action.room_id, kind="activity").one().payload["status"] == "denied"


@pytest.mark.parametrize("family,args", [("browser_interact", {"url": "https://example.invalid", "operation": "click", "selector": "button"}),
    ("pointer_click", {"x": 1, "y": 2}), ("keyboard_text", {"text": "private-fixture"}), ("keyboard_chord", {"keys": ["META", "a"]})])
def test_opaque_policy_requires_once_and_consent_without_claiming_verified_account(preview_fixture, family, args):
    client, owner, broker, _, proposal = preview_fixture
    action = proposal(family, args, workflow="flow")
    view = broker.preview(action, owner_id=owner); token = view["review_id"]
    assert view["approval_policy"] == {"scopes": ["once"], "unverified_context_required": True, "start_within_seconds": 60}
    assert approve(client, owner, action, token).status_code == 409
    assert approve(client, owner, action, token, scope="room_session", acknowledge_unverified_context=True).status_code == 409
    result = approve(client, owner, action, token, acknowledge_unverified_context=True)
    assert result.status_code == 200, result.text
    with broker.sessions() as db:
        saved = db.get(ToolAction, action.action_id); grant = db.get(RoomGrant, saved.grant_id)
        assert saved.review_snapshot["external_context"]["account"] == "unsupported"
        assert grant.scope["acknowledge_unverified_context"] is True
        assert (grant.expires_at - saved.approved_at).total_seconds() == 300
        # This fixture never invokes claim, a browser, native IPC or an effect.
        assert saved.state == "approved" and saved.started_at is None


def test_cross_room_agent_session_owner_or_token_never_approves(preview_fixture):
    client, owner, broker, _, proposal = preview_fixture
    first, second = proposal(), proposal()
    other_id = broker.preview(second, owner_id=owner)["review_id"]
    assert approve(client, owner, first, other_id).status_code == 409
    assert client.post(f"/api/v1/tools/rooms/{first.room_id+100}/actions/{first.action_id}/decision",
        headers=headers(client, owner), json={"decision": "approve", "scope": "once", "review_id": first.review_id}).status_code == 404
    with broker.sessions() as db:
        assert db.query(RoomGrant).count() == 0


def test_request_cannot_supply_a_trusted_identity_or_nonboolean_consent(preview_fixture):
    client, owner, broker, _, proposal = preview_fixture
    action = proposal(); token = broker.preview(action, owner_id=owner)["review_id"]
    assert approve(client, owner, action, token, account="caller label").status_code == 422
    assert approve(client, owner, action, token, acknowledge_unverified_context="true").status_code == 422


@pytest.mark.parametrize("change", ["cancel", "legacy", "wrong_owner"])
def test_stopped_legacy_and_foreign_owner_requests_cannot_create_approval(preview_fixture, change):
    client, owner, broker, _, proposal = preview_fixture
    action = proposal(); token = broker.preview(action, owner_id=owner)["review_id"]
    if change == "cancel":
        assert client.post(f"/api/v1/conversations/{action.room_id}/cancel", headers=headers(client, owner)).status_code == 204
    elif change == "legacy":
        with broker.sessions() as db:
            saved = db.get(ToolAction, action.action_id); saved.review_id = saved.review_snapshot = None; db.commit()
    result = approve(client, owner+1 if change == "wrong_owner" else owner, action, token)
    assert result.status_code == (404 if change == "wrong_owner" else 409)
    with broker.sessions() as db:
        assert db.query(RoomGrant).count() == 0
        assert db.get(ToolAction, action.action_id).approved_at is None
