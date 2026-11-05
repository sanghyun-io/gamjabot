from types import SimpleNamespace
from uuid import uuid4
import pytest
from pydantic import ValidationError
from app.schemas.tool import DecisionRequest
from app.services.tool_broker import prepare
from app.models.collaboration import ToolAction
from test_room_state import room_client
from test_review_decision import approve
from test_review_preview import preview_fixture


def test_strict_choice_and_browser_tool_shape():
    assert DecisionRequest(decision='approve').send_image_to_model is False
    with pytest.raises(ValidationError): DecisionRequest(decision='approve', send_image_to_model='true')
    action = prepare('browser_screenshot', {'url': 'https://example.com/'}, 'flow')
    assert action.scope_target == 'https://example.com'
    with pytest.raises(ValueError): prepare('browser_screenshot', {}, 'flow')


@pytest.mark.parametrize('choice', [False, True])
def test_manual_screenshot_approval_records_choice(preview_fixture, choice):
    client, owner, broker, _, proposal = preview_fixture
    executor = SimpleNamespace(desktop=SimpleNamespace(native=SimpleNamespace(context_id=uuid4().hex)))
    action = proposal('screenshot', {}, executor, 'flow')
    token = broker.preview(action, owner_id=owner)['review_id']
    response = approve(client, owner, action, token, send_image_to_model=choice)
    assert response.status_code == 200, response.text
    assert response.json()['send_image_to_model'] is choice
    with broker.sessions() as db:
        assert db.get(ToolAction, action.action_id).send_image_to_model is choice


def test_other_family_rejects_image_choice(preview_fixture):
    client, owner, broker, _, proposal = preview_fixture
    action = proposal()
    token = broker.preview(action, owner_id=owner)['review_id']
    assert approve(client, owner, action, token, send_image_to_model=True).status_code == 422


def test_model_image_boundary_requires_exact_current_manual_approval(preview_fixture, monkeypatch):
    from app.services import scheduler
    client, owner, broker, _, proposal = preview_fixture
    executor = SimpleNamespace(desktop=SimpleNamespace(native=SimpleNamespace(context_id=uuid4().hex)))
    action = proposal('screenshot', {}, executor, 'flow')
    token = broker.preview(action, owner_id=owner)['review_id']
    assert approve(client, owner, action, token, send_image_to_model=True).status_code == 200
    monkeypatch.setattr(scheduler, 'verified', lambda *_: True)
    monkeypatch.setattr(scheduler, 'frozen_proposer', lambda *a, **k: original)
    monkeypatch.setattr(scheduler, 'observe_authority', lambda *a, **k: None)
    from app.services.review_snapshot import retained_review
    with broker.sessions() as db:
        saved = db.get(ToolAction, action.action_id)
        saved.state = 'completed'; db.commit()
        original = retained_review(saved).authority.proposer
        outcome = SimpleNamespace(image_bytes=b'png')
        run = lambda: scheduler._consented_image(db, action.action_id, action.room_id, action.agent_id, object(), outcome, None)
        assert run() == b'png'
        saved.send_image_to_model = False; db.commit(); assert run() is None
        saved.send_image_to_model = None; db.commit(); assert run() is None
        saved.send_image_to_model = True; saved.approval_owner_id = owner + 1; db.commit(); assert run() is None
        saved.approval_owner_id = owner; db.commit()
        monkeypatch.setattr(scheduler, 'frozen_proposer', lambda *a, **k: original.model_copy(update={'model_id': 'stale'}))
        assert run() is None


def test_cli_explicit_image_flag():
    from app.owner_cli import command
    from test_review_cli import Client
    client = Client()
    command(client, ["decide", "3", "action", "approve", "--review-id", str(uuid4()), "--send-image-to-model"])
    assert client.calls[0][0][2]["send_image_to_model"] is True
    with pytest.raises(RuntimeError):
        command(Client(), ["decide", "3", "action", "deny", "--send-image-to-model"])


def test_browser_screenshot_owner_only_artifact(room_client):
    from test_screenshot_artifacts import stored_screenshot, PNG
    from test_room_state import create_room, headers
    client, (owner, other, project, agent, _) = room_client
    room = create_room(client, owner, project, "browser capture")
    action_id, _ = stored_screenshot(client, room, agent)
    with client.app.state.session_factory() as db:
        action = db.get(ToolAction, action_id)
        action.family = "browser_screenshot"
        db.commit()
    url = f"/api/v1/tools/rooms/{room}/actions/{action_id}/screenshot"
    assert client.get(url, headers=headers(client, owner)).content == PNG
    assert client.get(url, headers=headers(client, other)).status_code == 404


def test_schema43_preserves_unknown_and_orm_new_defaults_false(room_client):
    import sqlite3
    import importlib
    migration = importlib.import_module('app.core.migrations.0043_image_consent')
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE tool_actions (action_id TEXT PRIMARY KEY)')
    db.execute("INSERT INTO tool_actions VALUES ('legacy')")
    migration.upgrade(db)
    assert db.execute('SELECT send_image_to_model FROM tool_actions').fetchone() == (None,)
    client, (owner, _, project, agent, _) = room_client
    from test_room_state import create_room
    room = create_room(client, owner, project, 'new action')
    with client.app.state.session_factory() as session:
        row = ToolAction(action_id=str(uuid4()), room_id=room, agent_id=agent, family='screenshot', arguments={}, state='pending')
        session.add(row); session.commit()
        assert row.send_image_to_model is False


def test_session_grant_does_not_copy_image_choice(preview_fixture):
    from app.models.collaboration import RoomGrant
    from test_room_state import headers
    client, owner, broker, _, proposal = preview_fixture
    executor = SimpleNamespace(desktop=SimpleNamespace(native=SimpleNamespace(context_id=uuid4().hex)))
    first = proposal('screenshot', {}, executor, 'flow')
    token = broker.preview(first, owner_id=owner)['review_id']
    response = client.post(f'/api/v1/tools/rooms/{first.room_id}/actions/{first.action_id}/decision',
        headers=headers(client, owner), json={'decision':'approve', 'scope':'room_session',
        'review_id':token, 'send_image_to_model':True})
    assert response.status_code == 200, response.text
    second = proposal('screenshot', {}, executor, 'flow')
    with broker.sessions() as db:
        grant = db.get(RoomGrant, response.json()['grant_id'])
        assert 'send_image_to_model' not in grant.scope
        assert db.get(ToolAction, second.action_id).send_image_to_model is False
