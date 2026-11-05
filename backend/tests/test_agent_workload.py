"""Owner-wide Agent workload read model on disposable SQLite."""

from datetime import datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event as sqlalchemy_event

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.collaboration import RoomAgentRun, RoomEvent, RoomMembership, RoomReadState, RoomTrigger
from app.models.models import Agent, Conversation, Project, User


@pytest.fixture
def workload_client(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "workload-token")
    app = create_app(database_url=sqlite_url)
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            owner = User(email="workload-owner@test.invalid", username="workload-owner", hashed_password="x")
            other = User(email="workload-other@test.invalid", username="workload-other", hashed_password="x")
            db.add_all([owner, other])
            db.commit()
            ids = {"owner": owner.id, "other": other.id}
        yield client, ids


def headers(client, owner_id):
    return {
        "X-Gamja-Session": client.app.state.local_session_token,
        "Authorization": f"Bearer {create_access_token({'sub': owner_id})}",
    }


def add_room(db, owner_id, label, *, project_active=True, room_active=True):
    project = Project(user_id=owner_id, name=f"{label} project", is_active=project_active)
    db.add(project)
    db.flush()
    room = Conversation(
        user_id=owner_id, project_id=project.id, title=f"{label} room",
        is_active=room_active, created_at=datetime(2026, 10, 5, 8, 0, 0),
    )
    db.add(room)
    db.flush()
    return project, room


def add_event(db, room, seq, actor_type, kind, when, payload, *, actor_id=None, actor_name=None):
    row = RoomEvent(
        event_id=str(uuid4()), room_id=room.id, seq=seq, actor_type=actor_type,
        actor_id=actor_id, actor_name_snapshot=actor_name or actor_type.title(),
        kind=kind, payload=payload, created_at=when,
    )
    db.add(row)
    db.flush()
    return row


def join(db, room, agent, *, state="idle", paused=False, trigger=None, private=False):
    db.add(RoomMembership(room_id=room.id, agent_id=agent.id, is_active=True))
    db.flush()
    db.add(RoomAgentRun(
        room_id=room.id, agent_id=agent.id, state=state, paused=paused,
        trigger_event_id=trigger.event_id if trigger else None,
        checkpoint={"secret": "CHECKPOINT_PRIVATE"} if private else {},
        memory={"secret": "MEMORY_PRIVATE"} if private else {},
        pending_task={"secret": "TASK_PRIVATE"} if private else {},
        active_tool={"secret": "TOOL_PRIVATE"} if private else {},
        generation=7, updated_at=datetime(2026, 10, 5, 9, 0, 0),
    ))


def trigger(db, room, request, state, selected):
    db.add(RoomTrigger(
        event_id=request.event_id, room_id=room.id, state=state,
        selected_agents=selected, max_rounds=2, max_tool_calls=8,
        max_executor_seconds=60, current_round=1,
    ))


def get_workloads(client, owner_id, **params):
    return client.get("/api/v1/agent-workloads", headers=headers(client, owner_id), params=params)


def test_workload_shows_same_agent_running_in_independent_rooms_and_excludes_archives(workload_client):
    client, ids = workload_client
    now = datetime(2026, 10, 5, 10, 0, 0)
    with client.app.state.session_factory() as db:
        worker = Agent(user_id=ids["owner"], name="Parallel", description="d", system_prompt="p", is_active=True)
        inactive = Agent(user_id=ids["owner"], name="Inactive", description="d", system_prompt="p", is_active=False)
        foreign_agent = Agent(user_id=ids["other"], name="Foreign", description="d", system_prompt="p", is_active=True)
        db.add_all([worker, inactive, foreign_agent])
        db.flush()
        expected = []
        for number in range(2):
            project, room = add_room(db, ids["owner"], f"Live {number}")
            request = add_event(db, room, 1, "user", "message", now + timedelta(seconds=number), {"content": "private"})
            trigger(db, room, request, "running", [worker.id])
            join(db, room, worker, state="running", trigger=request)
            expected.append((room.id, project.id, request.event_id))
        _project, unknown_room = add_room(db, ids["owner"], "Inactive Agent")
        join(db, unknown_room, inactive, state="idle")
        _project, archived = add_room(db, ids["owner"], "Archived", project_active=False)
        join(db, archived, worker, state="running")
        _project, closed = add_room(db, ids["owner"], "Closed", room_active=False)
        join(db, closed, worker, state="running")
        _project, foreign = add_room(db, ids["other"], "Foreign")
        join(db, foreign, foreign_agent, state="running")
        db.commit()
        worker_id, inactive_id = worker.id, inactive.id

    response = get_workloads(client, ids["owner"])
    assert response.status_code == 200, response.text
    data = response.json()
    assert [item["agent_id"] for item in data["items"]] == [worker_id, inactive_id]
    parallel = data["items"][0]
    assert parallel["overall_state"] == "running"
    assert parallel["counts"] == {
        "running": 2, "waiting": 0, "paused": 0, "failed": 0,
        "idle": 0, "unknown": 0, "total_rooms": 2,
    }
    assert {room["room_id"] for room in parallel["rooms"]} == {row[0] for row in expected}
    assert all(room["href"] == f"/chat/{room['room_id']}?seq=1" for room in parallel["rooms"])
    assert data["items"][1]["overall_state"] == "unknown"
    assert data["items"][1]["counts"]["unknown"] == 1
    rendered = str(data)
    assert "Foreign" not in rendered and "Archived room" not in rendered and "Closed room" not in rendered


def test_workload_state_authority_exact_links_and_private_run_fields(workload_client):
    client, ids = workload_client
    now = datetime(2026, 10, 5, 11, 0, 0)
    with client.app.state.session_factory() as db:
        agent = Agent(user_id=ids["owner"], name="Worker", description="PRIVATE_DESCRIPTION", system_prompt="PRIVATE_PROMPT", is_active=True)
        db.add(agent)
        db.flush()

        rooms = {}
        for label in ("paused", "waiting", "queued", "failed", "idle", "stale", "missing"):
            project, room = add_room(db, ids["owner"], label.title())
            rooms[label] = (project, room)

        for label, run_state, paused in (("paused", "running", True), ("waiting", "waiting", False), ("queued", "idle", False)):
            room = rooms[label][1]
            request = add_event(db, room, 1, "user", "message", now, {"content": "PRIVATE_REQUEST"})
            trigger(db, room, request, "pending", [agent.id])
            join(db, room, agent, state=run_state, paused=paused, trigger=request if label != "queued" else None,
                 private=label == "paused")
            if label == "queued":
                add_event(db, room, 2, "agent", "failure", now + timedelta(milliseconds=500),
                          {"code": "provider_unavailable", "message": "PRIVATE_OLD_FAILURE",
                           "trigger_event_id": request.event_id}, actor_id=agent.id, actor_name="Worker")

        failed_room = rooms["failed"][1]
        failed_request = add_event(db, failed_room, 1, "user", "message", now, {"content": "PRIVATE_REQUEST"})
        failure = add_event(
            db, failed_room, 2, "agent", "failure", now + timedelta(seconds=1),
            {"code": "provider_unavailable", "message": "PRIVATE_FAILURE", "trigger_event_id": failed_request.event_id},
            actor_id=agent.id, actor_name="Worker",
        )
        join(db, failed_room, agent, state="idle")

        join(db, rooms["idle"][1], agent, state="idle")
        stale_room = rooms["stale"][1]
        stale_request = add_event(db, stale_room, 1, "user", "message", now, {"content": "private"})
        trigger(db, stale_room, stale_request, "done", [agent.id])
        join(db, stale_room, agent, state="running", trigger=stale_request)
        db.add(RoomMembership(room_id=rooms["missing"][1].id, agent_id=agent.id, is_active=True))
        db.add(RoomReadState(owner_id=ids["owner"], room_id=failed_room.id, last_read_seq=0, updated_at=now))
        db.commit()
        agent_id, failure_id, failed_request_id = agent.id, failure.event_id, failed_request.event_id
        failed_room_id = failed_room.id

    item = get_workloads(client, ids["owner"], agent_id=agent_id).json()["items"][0]
    assert item["overall_state"] == "waiting"
    assert item["counts"] == {
        "running": 0, "waiting": 2, "paused": 1, "failed": 1,
        "idle": 1, "unknown": 2, "total_rooms": 7,
    }
    by_title = {row["room_title"]: row for row in item["rooms"]}
    assert by_title["Paused room"]["state"] == "paused"
    assert by_title["Waiting room"]["state"] == "waiting"
    assert by_title["Queued room"]["state"] == "waiting"
    assert by_title["Failed room"]["failure_event_id"] == failure_id
    assert by_title["Failed room"]["failure_seq"] == 2
    assert by_title["Failed room"]["trigger_event_id"] == failed_request_id
    assert by_title["Failed room"]["trigger_seq"] == 1
    assert by_title["Failed room"]["href"].endswith(f"?seq=2&failure={failure_id}")
    assert by_title["Stale room"]["state"] == "unknown"
    rendered = str(item)
    for private in ("PRIVATE_DESCRIPTION", "PRIVATE_PROMPT", "CHECKPOINT_PRIVATE", "MEMORY_PRIVATE",
                    "TASK_PRIVATE", "TOOL_PRIVATE", "PRIVATE_REQUEST", "PRIVATE_FAILURE", "PRIVATE_OLD_FAILURE"):
        assert private not in rendered
    with client.app.state.session_factory() as db:
        assert db.get(RoomReadState, (ids["owner"], failed_room_id)).last_read_seq == 0


def test_workload_bounds_pagination_cursor_binding_truncation_and_query_count(workload_client):
    client, ids = workload_client
    with client.app.state.session_factory() as db:
        agents = [Agent(user_id=ids["owner"], name=f"Agent {number}", description="d", system_prompt="p", is_active=True)
                  for number in range(3)]
        db.add_all(agents)
        db.flush()
        running_room_id = None
        for number in range(13):
            _project, room = add_room(db, ids["owner"], f"Room {number}")
            if number == 0:
                request = add_event(db, room, 1, "user", "message", datetime(2026, 10, 5, 7, 0, 0),
                                    {"content": "private"})
                trigger(db, room, request, "running", [agents[0].id])
                join(db, room, agents[0], state="running", trigger=request)
                running_room_id = room.id
            else:
                join(db, room, agents[0], state="idle")
        db.commit()
        agent_ids = [agent.id for agent in agents]

    page_one = get_workloads(client, ids["owner"], limit=2).json()
    page_two = get_workloads(client, ids["owner"], limit=2, cursor=page_one["next_cursor"]).json()
    assert [item["agent_id"] for item in page_one["items"] + page_two["items"]] == agent_ids
    assert page_two["next_cursor"] is None
    first = page_one["items"][0]
    assert first["counts"]["total_rooms"] == 13
    assert len(first["rooms"]) == 12 and first["rooms_truncated"] is True
    assert running_room_id in {room["room_id"] for room in first["rooms"]}
    assert get_workloads(client, ids["other"], cursor=page_one["next_cursor"]).status_code == 422
    assert get_workloads(client, ids["owner"], agent_id=agent_ids[0], cursor=page_one["next_cursor"]).status_code == 422
    for params in ({"agent_id": 0}, {"limit": 0}, {"limit": 101}, {"cursor": "bad cursor"}):
        assert get_workloads(client, ids["owner"], **params).status_code == 422

    engine = client.app.state.session_factory.kw["bind"]
    counts = []
    for limit in (1, 3):
        statements = []
        def observe(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)
        sqlalchemy_event.listen(engine, "before_cursor_execute", observe)
        try:
            assert get_workloads(client, ids["owner"], limit=limit).status_code == 200
        finally:
            sqlalchemy_event.remove(engine, "before_cursor_execute", observe)
        counts.append(len(statements))
    assert counts[0] == counts[1]
    assert counts[0] <= 6
