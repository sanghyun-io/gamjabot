"""Atomic custom room launch contracts on disposable SQLite."""

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.collaboration import (
    RoomAgentRun,
    RoomEvent,
    RoomGrant,
    RoomLaunchRequest,
    RoomMembership,
    RoomScheduleSettings,
    RoomTrigger,
    ToolAction,
)
from app.models.models import Agent, Conversation, Project, User
from app.schemas.room_launch import RoomLaunchWrite
from app.services.room_service import launch_room


@pytest.fixture
def launch_client(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "launch-token")
    app = create_app(database_url=sqlite_url)
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            owner = User(email="launch-owner@test.invalid", username="launch-owner", hashed_password="x")
            other = User(email="launch-other@test.invalid", username="launch-other", hashed_password="x")
            db.add_all([owner, other])
            db.flush()
            project = Project(user_id=owner.id, name="Active project")
            archived = Project(user_id=owner.id, name="Archived project", is_active=False)
            other_project = Project(user_id=other.id, name="Other project")
            db.add_all([project, archived, other_project])
            db.flush()
            first = Agent(user_id=owner.id, name="First", description="d", system_prompt="p")
            second = Agent(user_id=owner.id, name="Second", description="d", system_prompt="p")
            inactive = Agent(user_id=owner.id, name="Inactive", description="d", system_prompt="p", is_active=False)
            foreign = Agent(user_id=other.id, name="Foreign", description="d", system_prompt="p")
            db.add_all([first, second, inactive, foreign])
            db.commit()
            ids = {
                "owner": owner.id,
                "other": other.id,
                "project": project.id,
                "archived_project": archived.id,
                "other_project": other_project.id,
                "first": first.id,
                "second": second.id,
                "inactive": inactive.id,
                "foreign": foreign.id,
            }
        yield client, ids


def auth(client, owner_id):
    return {
        "X-Gamja-Session": client.app.state.local_session_token,
        "Authorization": f"Bearer {create_access_token({'sub': owner_id})}",
    }


def body(ids, **changes):
    value = {
        "project_id": ids["project"],
        "title": "  Custom room  ",
        "agent_ids": [ids["second"], ids["first"]],
        "max_rounds": 3,
        "max_tool_calls": 12,
        "max_executor_seconds": 180,
    }
    value.update(changes)
    return value


def launch(client, ids, key, value=None, *, owner=None):
    headers = {**auth(client, owner or ids["owner"]), "Idempotency-Key": key}
    return client.post("/api/v1/conversations/launch", headers=headers, json=value or body(ids))


def test_launch_creates_exact_empty_room_and_replays_lost_response(launch_client):
    client, ids = launch_client
    created = launch(client, ids, "stable-launch")
    assert created.status_code == 201, created.text
    room = created.json()
    assert room["title"] == "Custom room"
    assert room["project_id"] == ids["project"] and room["user_id"] == ids["owner"]
    assert room["is_active"] is True

    # Equivalent canonical input replays the durable room instead of creating a second one.
    replay = launch(client, ids, "stable-launch", body(ids, title="Custom room",
                                                       agent_ids=[ids["first"], ids["second"]]))
    assert replay.status_code == 201 and replay.json()["id"] == room["id"]

    with client.app.state.session_factory() as db:
        assert db.query(Conversation).filter_by(user_id=ids["owner"]).count() == 1
        assert [row.agent_id for row in db.query(RoomMembership).filter_by(
            room_id=room["id"]).order_by(RoomMembership.agent_id)] == [ids["first"], ids["second"]]
        runs = db.query(RoomAgentRun).filter_by(room_id=room["id"]).order_by(RoomAgentRun.agent_id).all()
        assert [(row.agent_id, row.state, row.generation) for row in runs] == [
            (ids["first"], "idle", 0), (ids["second"], "idle", 0)]
        policy = db.get(RoomScheduleSettings, room["id"])
        assert (policy.max_rounds, policy.max_tool_calls, policy.max_executor_seconds) == (3, 12, 180)
        request = db.get(RoomLaunchRequest, (ids["owner"], "stable-launch"))
        assert request.room_id == room["id"] and len(request.payload_digest) == 64
        assert db.query(RoomEvent).filter_by(room_id=room["id"]).count() == 0
        assert db.query(RoomTrigger).filter_by(room_id=room["id"]).count() == 0
        assert db.query(ToolAction).filter_by(room_id=room["id"]).count() == 0
        assert db.query(RoomGrant).filter_by(room_id=room["id"]).count() == 0

    changed = launch(client, ids, "stable-launch", body(ids, max_rounds=4))
    assert changed.status_code == 409
    assert changed.json()["detail"] == {"code": "room_launch_idempotency_conflict"}

    # Keys are scoped to the authenticated owner, so another owner may use the
    # same opaque key for their own independently validated launch.
    other = launch(client, ids, "stable-launch", body(
        ids, project_id=ids["other_project"], agent_ids=[ids["foreign"]]), owner=ids["other"])
    assert other.status_code == 201 and other.json()["id"] != room["id"]


def test_launch_allows_intentional_empty_roster_without_dispatch(launch_client):
    client, ids = launch_client
    response = launch(client, ids, "empty-launch", body(ids, agent_ids=[]))
    assert response.status_code == 201, response.text
    room_id = response.json()["id"]
    with client.app.state.session_factory() as db:
        assert db.query(RoomMembership).filter_by(room_id=room_id).count() == 0
        assert db.query(RoomAgentRun).filter_by(room_id=room_id).count() == 0
        assert db.get(RoomScheduleSettings, room_id) is not None
        assert db.query(RoomEvent).filter_by(room_id=room_id).count() == 0
        assert db.query(RoomTrigger).filter_by(room_id=room_id).count() == 0


def test_launch_preflight_rejects_unavailable_agents_and_projects_without_partial_rows(launch_client):
    client, ids = launch_client
    unavailable = [ids["inactive"], ids["foreign"], 999_999]
    response = launch(client, ids, "bad-agents", body(ids, agent_ids=[ids["first"], *unavailable]))
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "room_launch_agents_unavailable", "agent_ids": sorted(unavailable)}

    archived = launch(client, ids, "archived", body(ids, project_id=ids["archived_project"]))
    foreign = launch(client, ids, "foreign-project", body(ids, project_id=ids["other_project"]))
    other_owner = launch(client, ids, "owner-scope", body(ids), owner=ids["other"])
    assert archived.status_code == foreign.status_code == other_owner.status_code == 404
    with client.app.state.session_factory() as db:
        assert db.query(Conversation).count() == 0
        assert db.query(RoomLaunchRequest).count() == 0


@pytest.mark.parametrize("change", [
    {"agent_ids": [1, 1]},
    {"agent_ids": list(range(1, 66))},
    {"max_rounds": True},
    {"max_rounds": 1.5},
    {"max_rounds": 0},
    {"max_tool_calls": 65},
    {"max_executor_seconds": 3601},
    {"title": "   "},
    {"extra": "rejected"},
])
def test_launch_schema_is_strict_and_bounded(launch_client, change):
    client, ids = launch_client
    response = launch(client, ids, "invalid", body(ids, **change))
    assert response.status_code == 422
    with client.app.state.session_factory() as db:
        assert db.query(Conversation).count() == 0


def test_concurrent_exact_key_creates_one_room(launch_client):
    client, ids = launch_client

    def send():
        response = launch(client, ids, "concurrent-launch")
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _index: send(), range(2)))
    assert [status for status, _payload in responses] == [201, 201]
    assert len({payload["id"] for _status, payload in responses}) == 1
    with client.app.state.session_factory() as db:
        assert db.query(Conversation).count() == 1
        assert db.query(RoomLaunchRequest).count() == 1


def test_concurrent_changed_body_has_one_winner_and_one_conflict(launch_client):
    client, ids = launch_client

    def send(rounds):
        response = launch(client, ids, "concurrent-conflict", body(ids, max_rounds=rounds))
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send, (2, 4)))
    assert sorted(status for status, _payload in responses) == [201, 409]
    conflict = next(payload for status, payload in responses if status == 409)
    assert conflict["detail"] == {"code": "room_launch_idempotency_conflict"}
    with client.app.state.session_factory() as db:
        assert db.query(Conversation).count() == 1
        assert db.query(RoomLaunchRequest).count() == 1


def test_launch_requires_a_bounded_idempotency_header(launch_client):
    client, ids = launch_client
    missing = client.post("/api/v1/conversations/launch", headers=auth(client, ids["owner"]), json=body(ids))
    too_long = launch(client, ids, "x" * 201)
    assert missing.status_code == too_long.status_code == 422
    with client.app.state.session_factory() as db:
        assert db.query(Conversation).count() == 0


def test_launch_service_rejects_nonprinting_unicode_idempotency_key(launch_client):
    client, ids = launch_client
    with client.app.state.session_factory() as db:
        with pytest.raises(Exception) as error:
            launch_room(db, ids["owner"], RoomLaunchWrite(**body(ids)), "retry\u0085key")
    assert getattr(error.value, "status_code", None) == 422
    assert getattr(error.value, "detail", None) == {
        "code": "room_launch_idempotency_key_invalid",
    }
    with client.app.state.session_factory() as db:
        assert db.query(Conversation).count() == 0


def test_injected_schedule_failure_rolls_back_every_launch_row(launch_client):
    client, ids = launch_client

    def fail_schedule(*_args, **_kwargs):
        raise RuntimeError("injected launch failure")

    event.listen(RoomScheduleSettings, "before_insert", fail_schedule)
    try:
        with pytest.raises(RuntimeError, match="injected launch failure"):
            launch(client, ids, "rollback-launch")
    finally:
        event.remove(RoomScheduleSettings, "before_insert", fail_schedule)

    with client.app.state.session_factory() as db:
        assert db.query(Conversation).count() == 0
        assert db.query(RoomMembership).count() == 0
        assert db.query(RoomAgentRun).count() == 0
        assert db.query(RoomScheduleSettings).count() == 0
        assert db.query(RoomLaunchRequest).count() == 0
