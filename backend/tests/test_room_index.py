"""Owner-wide room navigation index contracts on disposable SQLite."""

from datetime import datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event as sqlalchemy_event

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.collaboration import (
    RoomAgentRun,
    RoomEvent,
    RoomMembership,
    RoomPin,
    RoomReadState,
    RoomTrigger,
    ToolAction,
)
from app.models.models import Agent, Conversation, Project, User


@pytest.fixture
def index_client(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "room-index-token")
    app = create_app(database_url=sqlite_url)
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            owner = User(email="index-owner@test.invalid", username="index-owner", hashed_password="x")
            other = User(email="index-other@test.invalid", username="index-other", hashed_password="x")
            db.add_all([owner, other])
            db.commit()
            ids = {"owner": owner.id, "other": other.id}
        yield client, ids


def headers(client, owner_id):
    return {
        "X-Gamja-Session": client.app.state.local_session_token,
        "Authorization": f"Bearer {create_access_token({'sub': owner_id})}",
    }


def project(db, owner_id, name, *, active=True):
    row = Project(user_id=owner_id, name=name, is_active=active)
    db.add(row)
    db.flush()
    return row


def room(db, owner_id, project_id, title, when, *, active=True):
    row = Conversation(
        user_id=owner_id, project_id=project_id, title=title,
        is_active=active, created_at=when, updated_at=when,
    )
    db.add(row)
    db.flush()
    return row


def room_event(db, target, seq, actor_type, kind, when, content=None):
    row = RoomEvent(
        event_id=str(uuid4()), room_id=target.id, seq=seq,
        actor_type=actor_type, actor_id=None, actor_name_snapshot=actor_type.title(),
        kind=kind, payload={"content": content} if content is not None else {"private": "technical"},
        created_at=when,
    )
    db.add(row)
    return row


def get_index(client, owner_id, **params):
    return client.get("/api/v1/rooms/index", headers=headers(client, owner_id), params=params)


def test_index_is_owner_scoped_and_distinguishes_room_from_effective_activity(index_client):
    client, ids = index_client
    now = datetime(2026, 10, 5, 8, 0, 0)
    with client.app.state.session_factory() as db:
        active_project = project(db, ids["owner"], "Active")
        archived_project = project(db, ids["owner"], "Archived", active=False)
        foreign_project = project(db, ids["other"], "Foreign")
        active = room(db, ids["owner"], active_project.id, "Active room", now)
        closed = room(db, ids["owner"], active_project.id, "Closed room", now - timedelta(minutes=1), active=False)
        legacy = room(db, ids["owner"], archived_project.id, "Legacy active child", now + timedelta(minutes=1))
        room(db, ids["other"], foreign_project.id, "Foreign secret title", now + timedelta(days=1))
        db.commit()
        project_ids = active_project.id, archived_project.id, foreign_project.id
        room_ids = active.id, closed.id, legacy.id

    active_page = get_index(client, ids["owner"]).json()
    assert [item["room_id"] for item in active_page["items"]] == [room_ids[0]]
    closed_page = get_index(client, ids["owner"], status="closed").json()
    assert [item["room_id"] for item in closed_page["items"]] == [room_ids[2], room_ids[1]]
    legacy_item = closed_page["items"][0]
    assert legacy_item["room_active"] is True
    assert legacy_item["project_active"] is False
    assert legacy_item["effective_active"] is False
    assert legacy_item["read_only"] is True
    assert closed_page["items"][1]["room_active"] is False
    assert get_index(client, ids["owner"], status="all").json()["items"]
    assert get_index(client, ids["owner"], project_id=project_ids[2]).json() == {
        "items": [], "next_cursor": None,
    }
    assert get_index(client, ids["owner"], project_id=999_999).json() == {
        "items": [], "next_cursor": None,
    }


def test_index_orders_pins_then_activity_and_uses_filter_bound_cursor(index_client):
    client, ids = index_client
    now = datetime(2026, 10, 5, 9, 0, 0)
    with client.app.state.session_factory() as db:
        group = project(db, ids["owner"], "Navigation")
        first = room(db, ids["owner"], group.id, "100%_Ready pinned", now - timedelta(days=2), active=False)
        second = room(db, ids["owner"], group.id, "Second", now - timedelta(days=1))
        third = room(db, ids["owner"], group.id, "Third", now - timedelta(days=1))
        fourth = room(db, ids["owner"], group.id, "100xxReady wildcard decoy", now)
        room_event(db, second, 1, "agent", "message", now, "safe")
        room_event(db, third, 1, "agent", "message", now, "safe")
        db.commit()
        room_ids = first.id, second.id, third.id, fourth.id

    pinned = client.put(f"/api/v1/rooms/{room_ids[0]}/pin", headers=headers(client, ids["owner"]))
    assert pinned.status_code == 200, pinned.text
    page_one = get_index(client, ids["owner"], status="all", limit=2).json()
    assert [item["room_id"] for item in page_one["items"]] == [room_ids[0], room_ids[3]]
    assert page_one["items"][0]["pinned"] is True
    assert page_one["next_cursor"]
    page_two = get_index(client, ids["owner"], status="all", limit=2, cursor=page_one["next_cursor"]).json()
    assert [item["room_id"] for item in page_two["items"]] == [room_ids[2], room_ids[1]]
    assert page_two["next_cursor"] is None

    literal = get_index(client, ids["owner"], status="all", q="100%_Ready").json()
    assert [item["room_id"] for item in literal["items"]] == [room_ids[0]]
    mismatch = get_index(client, ids["owner"], status="active", limit=2, cursor=page_one["next_cursor"])
    assert mismatch.status_code == 422
    assert mismatch.json()["detail"] == {"code": "room_index_cursor_invalid"}
    assert get_index(client, ids["other"], status="all", limit=2,
                     cursor=page_one["next_cursor"]).status_code == 422


def test_index_reports_safe_unread_attention_and_effective_work_only(index_client):
    client, ids = index_client
    now = datetime(2026, 10, 5, 10, 0, 0)
    with client.app.state.session_factory() as db:
        group = project(db, ids["owner"], "Work")
        archived = project(db, ids["owner"], "Archived", active=False)
        active = room(db, ids["owner"], group.id, "Active work", now)
        legacy = room(db, ids["owner"], archived.id, "Read only work", now + timedelta(minutes=5))
        worker = Agent(user_id=ids["owner"], name="Worker", description="d", system_prompt="p")
        db.add(worker)
        db.flush()
        for target in (active, legacy):
            room_event(db, target, 1, "user", "message", now, "request")
            failure = room_event(db, target, 2, "agent", "failure", now + timedelta(minutes=1))
            room_event(db, target, 3, "tool", "tool_result", now + timedelta(minutes=2))
            room_event(db, target, 4, "agent", "message", now + timedelta(minutes=3), "answer")
            db.add(RoomMembership(room_id=target.id, agent_id=worker.id))
            db.flush()
            db.add(RoomAgentRun(room_id=target.id, agent_id=worker.id, state="waiting"))
            db.add(ToolAction(
                action_id=str(uuid4()), room_id=target.id, agent_id=worker.id,
                family="read_file", arguments={"secret": "must-not-leak"}, state="waiting",
            ))
            db.add(RoomTrigger(event_id=failure.event_id, room_id=target.id, state="running", selected_agents=[worker.id]))
        db.add(RoomReadState(owner_id=ids["owner"], room_id=active.id, last_read_seq=1, updated_at=now))
        db.commit()
        active_id, legacy_id = active.id, legacy.id

    data = get_index(client, ids["owner"], status="all").json()
    by_id = {item["room_id"]: item for item in data["items"]}
    assert by_id[active_id]["unread_authored"] == 1
    assert by_id[active_id]["waiting_approvals"] == 1
    assert by_id[active_id]["active_failures"] == 1
    assert by_id[active_id]["attention_count"] == 2
    assert by_id[active_id]["work_status"] == "waiting"
    assert by_id[legacy_id]["unread_authored"] == 2
    assert by_id[legacy_id]["waiting_approvals"] == 0
    assert by_id[legacy_id]["active_failures"] == 0
    assert by_id[legacy_id]["attention_count"] == 0
    assert by_id[legacy_id]["work_status"] == "idle"
    assert "must-not-leak" not in str(data) and "private" not in str(data)
    assert get_index(client, ids["owner"], status="all", q="must-not-leak").json()["items"] == []
    with client.app.state.session_factory() as db:
        assert db.get(RoomReadState, (ids["owner"], active_id)).last_read_seq == 1


def test_index_validation_and_select_count_are_bounded(index_client):
    client, ids = index_client
    now = datetime(2026, 10, 5, 11, 0, 0)
    with client.app.state.session_factory() as db:
        group = project(db, ids["owner"], "Bounded")
        for number in range(5):
            target = room(db, ids["owner"], group.id, f"Room {number}", now + timedelta(minutes=number))
            room_event(db, target, 1, "agent", "message", now + timedelta(minutes=number), "safe")
        db.commit()

    for params in ({"limit": 0}, {"limit": 201}, {"q": "x" * 201}, {"project_id": 0},
                   {"status": "running"}, {"cursor": "not-a-cursor"}):
        assert get_index(client, ids["owner"], **params).status_code == 422

    engine = client.app.state.session_factory.kw["bind"]
    counts = []
    for limit in (1, 5):
        statements = []
        def observe(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)
        sqlalchemy_event.listen(engine, "before_cursor_execute", observe)
        try:
            assert get_index(client, ids["owner"], limit=limit).status_code == 200
        finally:
            sqlalchemy_event.remove(engine, "before_cursor_execute", observe)
        counts.append(len(statements))
    assert counts[0] == counts[1]
    assert counts[0] <= 8
