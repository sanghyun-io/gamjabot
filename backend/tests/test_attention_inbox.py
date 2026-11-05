"""Owner-wide attention inbox contracts on disposable SQLite."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event as sqlalchemy_event

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.collaboration import RoomEvent, RoomReadState, ToolAction
from app.models.models import Agent, Conversation, Project, User
from app.services.attention import _decode_cursor, _encode_cursor, _failure_summary


@pytest.fixture
def attention_client(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "attention-token")
    app = create_app(database_url=sqlite_url)
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            owner = User(email="attention-owner@test.invalid", username="attention-owner", hashed_password="x")
            other = User(email="attention-other@test.invalid", username="attention-other", hashed_password="x")
            db.add_all([owner, other])
            db.commit()
            ids = {"owner": owner.id, "other": other.id}
        yield client, ids


def headers(client, owner_id):
    return {
        "X-Gamja-Session": client.app.state.local_session_token,
        "Authorization": f"Bearer {create_access_token({'sub': owner_id})}",
    }


def setup_room(db, owner_id, name, *, project_active=True, room_active=True):
    project = Project(user_id=owner_id, name=f"{name} project", is_active=project_active)
    db.add(project)
    db.flush()
    room = Conversation(user_id=owner_id, project_id=project.id, title=f"{name} room", is_active=room_active)
    db.add(room)
    db.flush()
    return project, room


def event(db, room, seq, actor_type, kind, when, payload, *, actor_id=None, actor_name=None):
    row = RoomEvent(
        event_id=str(uuid4()), room_id=room.id, seq=seq, actor_type=actor_type,
        actor_id=actor_id, actor_name_snapshot=actor_name or actor_type.title(),
        kind=kind, payload=payload, created_at=when,
    )
    db.add(row)
    db.flush()
    return row


def get_attention(client, owner_id, **params):
    return client.get("/api/v1/attention", headers=headers(client, owner_id), params=params)


def test_quota_exhaustion_is_a_closed_connection_failure():
    assert _failure_summary({
        "code": "quota_exhausted", "message": "raw vendor detail",
    }) == "failure_connection"


def test_inbox_derives_current_safe_approval_and_latest_attempt_failures(attention_client):
    client, ids = attention_client
    now = datetime(2026, 10, 5, 12, 0, 0)
    with client.app.state.session_factory() as db:
        project, room = setup_room(db, ids["owner"], "Owned")
        _archived_project, archived = setup_room(db, ids["owner"], "Archived", project_active=False)
        _closed_project, closed = setup_room(db, ids["owner"], "Closed", room_active=False)
        _foreign_project, foreign = setup_room(db, ids["other"], "Foreign")
        worker = Agent(user_id=ids["owner"], name="Safe Agent", description="d", system_prompt="p")
        foreign_worker = Agent(user_id=ids["other"], name="Foreign Agent", description="d", system_prompt="p")
        db.add_all([worker, foreign_worker])
        db.flush()

        old_request = event(db, room, 1, "user", "message", now, {"content": "old request"})
        event(db, room, 2, "agent", "failure", now + timedelta(seconds=1),
              {"code": "provider_unavailable", "message": "OLD_PRIVATE", "trigger_event_id": old_request.event_id},
              actor_id=worker.id, actor_name="Safe Agent")
        latest_request = event(db, room, 3, "user", "message", now + timedelta(seconds=2), {"content": "latest request"})
        overlapping = event(db, room, 4, "agent", "failure", now + timedelta(seconds=4),
              {"code": "tool_effect_uncertain", "message": "PRIVATE_FAILURE_MESSAGE",
               "diagnostic": {"token": "SECRET_DIAGNOSTIC"}, "trigger_event_id": old_request.event_id},
              actor_id=worker.id, actor_name="Safe Agent")
        action_id = str(uuid4())
        db.add(ToolAction(
            action_id=action_id, room_id=room.id, agent_id=worker.id,
            trigger_event_id=latest_request.event_id, family="write_file",
            arguments={"content": "SECRET_ARGUMENT"}, target="/private/target",
            expected_effect="SECRET_EFFECT", state="waiting", requested_at=now + timedelta(seconds=5),
        ))
        for state in ("approved", "cancelled", "completed"):
            db.add(ToolAction(
                action_id=str(uuid4()), room_id=room.id, agent_id=worker.id,
                trigger_event_id=latest_request.event_id, family="start_process", arguments={},
                state=state, requested_at=now + timedelta(seconds=9),
            ))
        for target_room, target_agent in ((archived, worker), (closed, worker), (foreign, foreign_worker)):
            request = event(db, target_room, 1, "user", "message", now, {"content": "request"})
            event(db, target_room, 2, "agent", "failure", now + timedelta(seconds=8),
                  {"code": "provider_unavailable", "message": "HIDDEN", "trigger_event_id": request.event_id},
                  actor_id=target_agent.id, actor_name=target_agent.name)
            db.add(ToolAction(
                action_id=str(uuid4()), room_id=target_room.id, agent_id=target_agent.id,
                trigger_event_id=request.event_id, family="read_file", arguments={"path": "HIDDEN"},
                state="waiting", requested_at=now + timedelta(seconds=8),
            ))
        foreign_request_id = request.event_id
        cross_room_action_id = str(uuid4())
        db.add(ToolAction(
            action_id=cross_room_action_id, room_id=room.id, agent_id=worker.id,
            trigger_event_id=foreign_request_id, family="screenshot", arguments={}, state="waiting",
            requested_at=now + timedelta(seconds=3, milliseconds=500),
        ))
        db.commit()
        expected_ids = room.id, project.id, old_request.event_id, latest_request.event_id, overlapping.event_id
        worker_id = worker.id

    response = get_attention(client, ids["owner"])
    assert response.status_code == 200, response.text
    data = response.json()
    assert [item["kind"] for item in data["items"]] == ["approval", "failure", "approval"]
    approval, failure, cross_room_approval = data["items"]
    assert approval == {
        "id": f"approval:{action_id}", "kind": "approval", "occurred_at": "2026-10-05T12:00:05Z",
        "room_id": expected_ids[0], "room_title": "Owned room", "project_id": expected_ids[1],
        "project_name": "Owned project", "agent_id": worker_id, "agent_name": "Safe Agent",
        "request_event_id": expected_ids[3], "request_seq": 3, "action_id": action_id,
        "failure_event_id": None, "failure_seq": None, "summary_code": "approval_files",
        "href": f"/chat/{expected_ids[0]}?seq=3&action={action_id}",
    }
    assert failure["id"] == f"failure:{expected_ids[4]}"
    assert failure["failure_event_id"] == expected_ids[4] and failure["failure_seq"] == 4
    assert failure["request_event_id"] == expected_ids[2] and failure["request_seq"] == 1
    assert failure["summary_code"] == "failure_uncertain"
    assert failure["href"] == f"/chat/{expected_ids[0]}?seq=4&failure={expected_ids[4]}"
    assert cross_room_approval["action_id"] == cross_room_action_id
    assert cross_room_approval["request_event_id"] is None and cross_room_approval["request_seq"] is None
    assert cross_room_approval["summary_code"] == "approval_screen"
    assert cross_room_approval["href"] == f"/chat/{expected_ids[0]}?action={cross_room_action_id}"
    rendered = str(data)
    for private in ("SECRET_ARGUMENT", "PRIVATE_FAILURE_MESSAGE", "SECRET_DIAGNOSTIC", "SECRET_EFFECT", "/private/target", "OLD_PRIVATE", "HIDDEN", "Foreign Agent"):
        assert private not in rendered


def test_inbox_keeps_ambiguous_failure_link_exact_and_drops_it_after_a_new_user_turn(attention_client):
    client, ids = attention_client
    now = datetime(2026, 10, 5, 13, 0, 0)
    with client.app.state.session_factory() as db:
        _project, room = setup_room(db, ids["owner"], "Ambiguous")
        worker = Agent(user_id=ids["owner"], name="Agent", description="d", system_prompt="p")
        db.add(worker)
        db.flush()
        event(db, room, 1, "user", "message", now, {"content": "request"})
        failure = event(db, room, 2, "agent", "failure", now + timedelta(seconds=1),
                        {"code": "provider_unavailable", "message": "safe internally"},
                        actor_id=worker.id, actor_name="Agent")
        db.commit()
        room_id, failure_id = room.id, failure.event_id

    item = get_attention(client, ids["owner"]).json()["items"][0]
    assert item["request_event_id"] is None and item["request_seq"] is None
    assert item["failure_event_id"] == failure_id and item["failure_seq"] == 2
    assert item["href"] == f"/chat/{room_id}?seq=2&failure={failure_id}"
    assert item["summary_code"] == "failure_connection"

    with client.app.state.session_factory() as db:
        target = db.get(Conversation, room_id)
        event(db, target, 3, "user", "message", now + timedelta(seconds=2), {"content": "new attempt"})
        db.commit()
    assert get_attention(client, ids["owner"]).json() == {"items": [], "next_cursor": None}


def test_inbox_pagination_validation_query_count_and_read_state_are_bounded(attention_client):
    client, ids = attention_client
    now = datetime(2026, 10, 5, 14, 0, 0)
    aware_cursor = _encode_cursor(now.replace(tzinfo=timezone.utc), "approval:legacy-action", ids["owner"])
    assert _decode_cursor(aware_cursor, ids["owner"]) == (now, "approval:legacy-action")
    with client.app.state.session_factory() as db:
        _project, room = setup_room(db, ids["owner"], "Paged")
        worker = Agent(user_id=ids["owner"], name="Agent", description="d", system_prompt="p")
        db.add(worker)
        db.flush()
        request = event(db, room, 1, "user", "message", now, {"content": "request"})
        for number in range(5):
            db.add(ToolAction(
                action_id=f"00000000-0000-0000-0000-{number:012d}", room_id=room.id, agent_id=worker.id,
                trigger_event_id=request.event_id, family="browser_navigate",
                arguments={"url": f"https://private-{number}.invalid"}, state="waiting",
                requested_at=now + timedelta(seconds=number),
            ))
        db.add(ToolAction(
            action_id="legacy-action", room_id=room.id, agent_id=worker.id,
            trigger_event_id=request.event_id, family="future_family", arguments={}, state="waiting",
            requested_at=now + timedelta(seconds=5),
        ))
        db.add(RoomReadState(owner_id=ids["owner"], room_id=room.id, last_read_seq=0, updated_at=now))
        db.commit()
        room_id = room.id

    page_one = get_attention(client, ids["owner"], limit=2).json()
    page_two = get_attention(client, ids["owner"], limit=2, cursor=page_one["next_cursor"]).json()
    page_three = get_attention(client, ids["owner"], limit=2, cursor=page_two["next_cursor"]).json()
    ids_seen = [item["id"] for page in (page_one, page_two, page_three) for item in page["items"]]
    assert len(ids_seen) == len(set(ids_seen)) == 6
    assert page_three["next_cursor"] is None
    summaries = {item["id"]: item["summary_code"] for page in (page_one, page_two, page_three) for item in page["items"]}
    assert summaries["approval:legacy-action"] == "approval_other"
    assert all(code == "approval_browser" for item_id, code in summaries.items() if item_id != "approval:legacy-action")
    assert get_attention(client, ids["other"], limit=2, cursor=page_one["next_cursor"]).status_code == 422
    for params in ({"limit": 0}, {"limit": 201}, {"cursor": "not-a-cursor"}):
        assert get_attention(client, ids["owner"], **params).status_code == 422

    engine = client.app.state.session_factory.kw["bind"]
    counts = []
    for limit in (1, 5):
        statements = []
        def observe(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)
        sqlalchemy_event.listen(engine, "before_cursor_execute", observe)
        try:
            assert get_attention(client, ids["owner"], limit=limit).status_code == 200
        finally:
            sqlalchemy_event.remove(engine, "before_cursor_execute", observe)
        counts.append(len(statements))
    assert counts[0] == counts[1]
    assert counts[0] <= 4
    with client.app.state.session_factory() as db:
        assert db.get(RoomReadState, (ids["owner"], room_id)).last_read_seq == 0
