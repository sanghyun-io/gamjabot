"""The owner dashboard is one bounded, canonical aggregate request."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.collaboration import ProviderConnection, RoomAgentRun, RoomEvent, RoomMembership, RoomReadState, ToolAction
from app.models.models import Agent, Conversation, Project, ProjectAgent, User


@pytest.fixture
def dashboard_client(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "dashboard-local-token")
    app = create_app(database_url=sqlite_url)
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            owner = User(email="dashboard-owner@test.invalid", username="dashboard-owner", hashed_password="x")
            other = User(email="dashboard-other@test.invalid", username="dashboard-other", hashed_password="x")
            db.add_all([owner, other])
            db.commit()
            ids = owner.id, other.id
        yield client, ids


def headers(client, owner_id):
    return {
        "X-Gamja-Session": client.app.state.local_session_token,
        "Authorization": f"Bearer {create_access_token({'sub': owner_id})}",
    }


def get_dashboard(client, owner_id, limit=6):
    return client.get(f"/api/v1/dashboard?limit={limit}", headers=headers(client, owner_id))


def connection(db, owner_id, *, enabled=True):
    row = ProviderConnection(
        user_id=owner_id, kind="compatible", name=f"Connection {owner_id}",
        base_url="http://127.0.0.1:8765/v1", default_model_id="exact",
        enabled=enabled, auth_required=False, capabilities={},
    )
    db.add(row)
    db.flush()
    return row


def agent(db, owner_id, connection_id, *, active=True):
    row = Agent(
        user_id=owner_id, name=f"Agent {owner_id}", description="d", system_prompt="p",
        model="exact", model_id="exact", connection_id=connection_id, is_active=active,
    )
    db.add(row)
    db.flush()
    return row


def project(db, owner_id, agent_id=None, *, name="Project"):
    row = Project(user_id=owner_id, name=name)
    db.add(row)
    db.flush()
    if agent_id is not None:
        db.add(ProjectAgent(project_id=row.id, agent_id=agent_id))
    return row


def room(db, owner_id, project_id, *, title="Room", created_at=None):
    row = Conversation(user_id=owner_id, project_id=project_id, title=title, created_at=created_at)
    db.add(row)
    db.flush()
    return row


def event(db, room_id, seq, actor_type, kind, created_at, content=None, actor_id=None):
    payload = {"content": content} if content is not None else {"status": "technical"}
    row = RoomEvent(
        event_id=f"{room_id:08d}-0000-0000-0000-{seq:012d}", room_id=room_id, seq=seq,
        actor_type=actor_type, actor_id=actor_id,
        actor_name_snapshot="Owner" if actor_type == "user" else "Agent" if actor_type == "agent" else "System",
        kind=kind, payload=payload, created_at=created_at,
    )
    db.add(row)
    return row


def test_setup_progresses_connection_agent_project_room_in_order(dashboard_client):
    client, (owner_id, _) = dashboard_client
    own = headers(client, owner_id)

    empty = client.get("/api/v1/dashboard", headers=own)
    assert empty.status_code == 200, empty.text
    assert empty.json() == {
        "setup": {"next_action": "connection"},
        "counts": {"enabled_connections": 0, "ready_agents": 0, "ready_projects": 0,
                   "active_rooms": 0, "unread_authored": 0, "waiting_approvals": 0, "active_failures": 0},
        "rooms": [],
    }

    with client.app.state.session_factory() as db:
        saved = connection(db, owner_id)
        db.commit()
        connection_id = saved.id
    assert client.get("/api/v1/dashboard", headers=own).json()["setup"] == {"next_action": "agent"}

    with client.app.state.session_factory() as db:
        ready_agent = agent(db, owner_id, connection_id)
        db.commit()
        agent_id = ready_agent.id
    assert client.get("/api/v1/dashboard", headers=own).json()["setup"] == {"next_action": "project"}

    with client.app.state.session_factory() as db:
        ready_project = project(db, owner_id, agent_id)
        db.commit()
        project_id = ready_project.id
    assert client.get("/api/v1/dashboard", headers=own).json()["setup"] == {"next_action": "room"}

    with client.app.state.session_factory() as db:
        room(db, owner_id, project_id)
        db.commit()
    complete = client.get("/api/v1/dashboard", headers=own).json()
    assert complete["setup"] == {"next_action": None}
    assert complete["counts"] == {
        "enabled_connections": 1, "ready_agents": 1, "ready_projects": 1, "active_rooms": 1,
        "unread_authored": 0, "waiting_approvals": 0, "active_failures": 0,
    }


def test_setup_does_not_treat_missing_required_credential_as_ready(dashboard_client):
    client, (owner_id, _) = dashboard_client
    with client.app.state.session_factory() as db:
        unusable = ProviderConnection(
            user_id=owner_id, kind="openai", name="Missing credential",
            default_model_id="exact", enabled=True, auth_required=True,
            secret_ref=None, capabilities={},
        )
        db.add(unusable)
        db.flush()
        agent(db, owner_id, unusable.id)
        db.commit()

    data = get_dashboard(client, owner_id).json()
    assert data["counts"]["enabled_connections"] == 1
    assert data["counts"]["ready_agents"] == 0
    assert data["setup"] == {"next_action": "connection"}


def test_setup_allows_agent_specific_model_without_connection_default(dashboard_client):
    client, (owner_id, _) = dashboard_client
    with client.app.state.session_factory() as db:
        custom = ProviderConnection(
            user_id=owner_id, kind="compatible", name="Custom model connection",
            base_url="http://127.0.0.1:8765/v1", default_model_id="",
            enabled=True, auth_required=False, secret_ref=None, capabilities={},
        )
        db.add(custom)
        db.flush()
        agent(db, owner_id, custom.id)
        db.commit()

    data = get_dashboard(client, owner_id).json()
    assert data["counts"]["ready_agents"] == 1
    assert data["setup"] == {"next_action": "project"}


def test_recent_rooms_use_canonical_event_order_and_safe_authored_preview(dashboard_client):
    client, (owner_id, _) = dashboard_client
    now = datetime(2026, 10, 3, 12, 0, 0)
    with client.app.state.session_factory() as db:
        saved = connection(db, owner_id)
        ready_agent = agent(db, owner_id, saved.id)
        group = project(db, owner_id, ready_agent.id, name="Launch")
        older = room(db, owner_id, group.id, title="Older ID, newest event", created_at=now - timedelta(days=2))
        newer = room(db, owner_id, group.id, title="Newer ID, older event", created_at=now - timedelta(days=1))
        event(db, older.id, 1, "user", "message", now, "Owner request", owner_id)
        event(db, older.id, 2, "tool", "tool_result", now + timedelta(minutes=2))
        event(db, older.id, 3, "agent", "message", now + timedelta(minutes=3), "A" * 200, ready_agent.id)
        event(db, newer.id, 1, "agent", "message", now - timedelta(hours=1), "Older answer", ready_agent.id)
        db.add(RoomReadState(owner_id=owner_id, room_id=older.id, last_read_seq=1, updated_at=now))
        db.commit()

    response = get_dashboard(client, owner_id)
    assert response.status_code == 200, response.text
    data = response.json()
    assert [item["room_title"] for item in data["rooms"]] == ["Older ID, newest event", "Newer ID, older event"]
    first = data["rooms"][0]
    assert first["last_event_seq"] == 3
    assert first["last_authored_seq"] == 3
    assert first["last_authored_preview"] == "A" * 160
    assert first["unread_authored"] == 1
    assert data["counts"]["unread_authored"] == 2
    assert "technical" not in str(data)


def test_work_queue_counts_are_owner_scoped_and_new_user_message_clears_stale_failure(dashboard_client):
    client, (owner_id, other_id) = dashboard_client
    now = datetime(2026, 10, 3, 13, 0, 0)
    with client.app.state.session_factory() as db:
        saved = connection(db, owner_id)
        ready_agent = agent(db, owner_id, saved.id)
        group = project(db, owner_id, ready_agent.id, name="Owner project")
        owner_room = room(db, owner_id, group.id, title="Needs attention", created_at=now)
        event(db, owner_room.id, 1, "user", "message", now, "First", owner_id)
        event(db, owner_room.id, 2, "agent", "failure", now + timedelta(minutes=1))
        db.add(RoomMembership(room_id=owner_room.id, agent_id=ready_agent.id))
        db.flush()
        db.add(RoomAgentRun(room_id=owner_room.id, agent_id=ready_agent.id, state="waiting"))
        db.add(ToolAction(action_id="owner-wait", room_id=owner_room.id, agent_id=ready_agent.id,
                          family="read_file", arguments={}, state="waiting"))

        foreign_connection = connection(db, other_id)
        foreign_agent = agent(db, other_id, foreign_connection.id)
        foreign_project = project(db, other_id, foreign_agent.id, name="Foreign project")
        foreign_room = room(db, other_id, foreign_project.id, title="Foreign room", created_at=now + timedelta(days=1))
        event(db, foreign_room.id, 1, "agent", "failure", now + timedelta(days=1), actor_id=foreign_agent.id)
        db.add(ToolAction(action_id="foreign-wait", room_id=foreign_room.id, agent_id=foreign_agent.id,
                          family="read_file", arguments={}, state="waiting"))
        db.commit()
        owner_room_id = owner_room.id

    before = get_dashboard(client, owner_id).json()
    assert [item["room_title"] for item in before["rooms"]] == ["Needs attention"]
    assert before["counts"]["waiting_approvals"] == 1
    assert before["counts"]["active_failures"] == 1
    assert before["rooms"][0]["work_status"] == "waiting"

    with client.app.state.session_factory() as db:
        event(db, owner_room_id, 3, "user", "message", now + timedelta(minutes=2), "Retry", owner_id)
        db.commit()
    after = get_dashboard(client, owner_id).json()
    assert after["counts"]["active_failures"] == 0
    assert after["rooms"][0]["active_failures"] == 0


def test_limit_bounds_page_only_while_counts_cover_all_owner_rooms(dashboard_client):
    client, (owner_id, _) = dashboard_client
    now = datetime(2026, 10, 3, 14, 0, 0)
    with client.app.state.session_factory() as db:
        saved = connection(db, owner_id)
        ready_agent = agent(db, owner_id, saved.id)
        group = project(db, owner_id, ready_agent.id)
        for index in range(3):
            current = room(db, owner_id, group.id, title=f"Room {index}", created_at=now + timedelta(minutes=index))
            event(db, current.id, 1, "agent", "message", now + timedelta(minutes=index), f"Answer {index}", ready_agent.id)
        db.commit()

    bounded = get_dashboard(client, owner_id, limit=1)
    assert bounded.status_code == 200
    assert len(bounded.json()["rooms"]) == 1
    assert bounded.json()["counts"]["active_rooms"] == 3
    assert bounded.json()["counts"]["unread_authored"] == 3
    for invalid in (0, 13):
        assert get_dashboard(client, owner_id, limit=invalid).status_code == 422
