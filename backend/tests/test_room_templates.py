"""Owner-persistent project room template contracts use disposable SQLite only."""

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.collaboration import (
    ProjectRoomTemplate,
    ProjectRoomTemplateMember,
    RoomAgentRun,
    RoomDraft,
    RoomEvent,
    RoomGrant,
    RoomMembership,
    RoomReadState,
    RoomScheduleSettings,
    RoomTrigger,
    ToolAction,
)
from app.models.models import Agent, Conversation, Message, Project, User
from app.services import room_template_service


@pytest.fixture
def template_client(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "room-template-token")
    app = create_app(database_url=sqlite_url)
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            owner = User(email="template-owner@test.invalid", username="template-owner", hashed_password="x")
            other = User(email="template-other@test.invalid", username="template-other", hashed_password="x")
            db.add_all([owner, other])
            db.flush()
            project = Project(user_id=owner.id, name="Main")
            second_project = Project(user_id=owner.id, name="Second")
            foreign_project = Project(user_id=other.id, name="Foreign")
            inactive_project = Project(user_id=owner.id, name="Inactive", is_active=False)
            db.add_all([project, second_project, foreign_project, inactive_project])
            db.flush()
            first = Agent(user_id=owner.id, name="Ada", description="d", system_prompt="p")
            second = Agent(user_id=owner.id, name="Lin", description="d", system_prompt="p")
            inactive = Agent(user_id=owner.id, name="Old", description="d", system_prompt="p", is_active=False)
            foreign = Agent(user_id=other.id, name="Other", description="d", system_prompt="p")
            db.add_all([first, second, inactive, foreign])
            db.commit()
            ids = {
                "owner": owner.id, "other": other.id,
                "project": project.id, "second_project": second_project.id,
                "foreign_project": foreign_project.id, "inactive_project": inactive_project.id,
                "first": first.id, "second": second.id, "inactive": inactive.id, "foreign": foreign.id,
            }
        yield client, ids


def auth(client, owner_id):
    return {
        "X-Gamja-Session": client.app.state.local_session_token,
        "Authorization": f"Bearer {create_access_token({'sub': owner_id})}",
    }


def body(ids, **changes):
    value = {
        "name": "Release team",
        "room_title": "Release room",
        "agent_ids": [ids["first"]],
        "max_rounds": 3,
        "max_tool_calls": 12,
        "max_executor_seconds": 180,
    }
    value.update(changes)
    return value


def create_template(client, ids, value=None):
    response = client.post(
        f"/api/v1/projects/{ids['project']}/room-templates",
        headers=auth(client, ids["owner"]), json=value or body(ids),
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_template_crud_is_owner_scoped_ordered_and_strict(template_client):
    client, ids = template_client
    own = auth(client, ids["owner"])
    created = create_template(client, ids, body(ids, name="  Release team  ", room_title="  Release room  ",
                                                   agent_ids=[ids["second"], ids["first"]]))
    assert created["name"] == "Release team" and created["room_title"] == "Release room"
    assert created["agent_ids"] == sorted([ids["first"], ids["second"]])
    template_id = created["id"]
    base = f"/api/v1/projects/{ids['project']}/room-templates"
    assert [row["id"] for row in client.get(base, headers=own).json()] == [template_id]
    assert client.get(f"{base}/{template_id}", headers=own).json() == created

    replacement = body(ids, name="Follow-up team", room_title="Follow-up room", agent_ids=[])
    updated = client.put(f"{base}/{template_id}", headers=own, json=replacement)
    assert updated.status_code == 200
    assert updated.json()["name"] == "Follow-up team" and updated.json()["agent_ids"] == []

    foreign = auth(client, ids["other"])
    assert client.get(base, headers=foreign).status_code == 404
    assert client.get(f"{base}/{template_id}", headers=foreign).status_code == 404
    assert client.put(f"{base}/{template_id}", headers=foreign, json=replacement).status_code == 404
    assert client.delete(f"{base}/{template_id}", headers=foreign).status_code == 404
    assert client.get(f"/api/v1/projects/{ids['foreign_project']}/room-templates", headers=own).status_code == 404
    assert client.get(f"/api/v1/projects/{ids['inactive_project']}/room-templates", headers=own).status_code == 404

    assert client.delete(f"{base}/{template_id}", headers=own).status_code == 204
    assert client.get(f"{base}/{template_id}", headers=own).status_code == 404


@pytest.mark.parametrize("change", [
    {"name": " "}, {"room_title": "x" * 201}, {"agent_ids": [1, 1]},
    {"agent_ids": list(range(1, 66))}, {"max_rounds": True}, {"max_rounds": 1.5},
    {"max_tool_calls": 65}, {"max_executor_seconds": 0}, {"extra": True},
])
def test_template_input_rejects_invalid_bounds_and_shapes(template_client, change):
    client, ids = template_client
    value = body(ids)
    value.update(change)
    response = client.post(f"/api/v1/projects/{ids['project']}/room-templates",
                           headers=auth(client, ids["owner"]), json=value)
    assert response.status_code == 422


def test_template_name_and_member_preflight_fail_without_partial_rows(template_client):
    client, ids = template_client
    own = auth(client, ids["owner"])
    created = create_template(client, ids)
    base = f"/api/v1/projects/{ids['project']}/room-templates"
    duplicate = client.post(base, headers=own, json=body(ids, name=" Release team "))
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == {"code": "room_template_name_conflict"}

    invalid_ids = sorted([ids["inactive"], ids["foreign"], 999_999])
    invalid = client.post(base, headers=own, json=body(ids, name="Invalid", agent_ids=invalid_ids))
    assert invalid.status_code == 409
    assert invalid.json()["detail"] == {
        "code": "room_template_members_unavailable", "agent_ids": invalid_ids,
    }
    with client.app.state.session_factory() as db:
        assert db.query(ProjectRoomTemplate).count() == 1
        assert db.query(ProjectRoomTemplateMember).count() == 1

    update = client.put(f"{base}/{created['id']}", headers=own,
                        json=body(ids, name="Changed", agent_ids=invalid_ids))
    assert update.status_code == 409
    assert client.get(f"{base}/{created['id']}", headers=own).json()["name"] == "Release team"


def test_instantiation_is_atomic_fresh_and_template_edits_do_not_mutate_rooms(template_client):
    client, ids = template_client
    own = auth(client, ids["owner"])
    created = create_template(client, ids)
    endpoint = f"/api/v1/projects/{ids['project']}/room-templates/{created['id']}"
    first = client.post(endpoint + "/rooms", headers=own, json={})
    assert first.status_code == 201, first.text
    first_room = first.json()["id"]
    assert first.json()["title"] == "Release room"

    replacement = body(ids, name="Release team", room_title="Next release",
                       agent_ids=[ids["second"]], max_rounds=4,
                       max_tool_calls=20, max_executor_seconds=240)
    assert client.put(endpoint, headers=own, json=replacement).status_code == 200
    second = client.post(endpoint + "/rooms", headers=own, json={})
    assert second.status_code == 201
    second_room = second.json()["id"]
    assert second.json()["title"] == "Next release"

    with client.app.state.session_factory() as db:
        assert [row.agent_id for row in db.query(RoomMembership).filter_by(room_id=first_room).all()] == [ids["first"]]
        assert [row.agent_id for row in db.query(RoomMembership).filter_by(room_id=second_room).all()] == [ids["second"]]
        first_policy = db.get(RoomScheduleSettings, first_room)
        second_policy = db.get(RoomScheduleSettings, second_room)
        assert (first_policy.max_rounds, first_policy.max_tool_calls, first_policy.max_executor_seconds) == (3, 12, 180)
        assert (second_policy.max_rounds, second_policy.max_tool_calls, second_policy.max_executor_seconds) == (4, 20, 240)
        for room_id, agent_id in ((first_room, ids["first"]), (second_room, ids["second"])):
            run = db.get(RoomAgentRun, (room_id, agent_id))
            assert (run.state, run.paused, run.generation) == ("idle", False, 0)
            assert run.checkpoint == run.memory == run.pending_task == run.active_tool == {}
            assert db.query(RoomEvent).filter_by(room_id=room_id).count() == 0
            assert db.query(Message).filter_by(conversation_id=room_id).count() == 0
            assert db.query(RoomTrigger).filter_by(room_id=room_id).count() == 0
            assert db.query(ToolAction).filter_by(room_id=room_id).count() == 0
            assert db.query(RoomGrant).filter_by(room_id=room_id).count() == 0
            assert db.query(RoomDraft).filter_by(room_id=room_id).count() == 0
            assert db.query(RoomReadState).filter_by(room_id=room_id).count() == 0


def test_instantiation_revalidates_members_and_rolls_back_after_child_flush(template_client):
    client, ids = template_client
    own = auth(client, ids["owner"])
    created = create_template(client, ids)
    endpoint = f"/api/v1/projects/{ids['project']}/room-templates/{created['id']}/rooms"
    with client.app.state.session_factory() as db:
        db.get(Agent, ids["first"]).is_active = False
        db.commit()
        before = db.query(Conversation).count()
    unavailable = client.post(endpoint, headers=own, json={})
    assert unavailable.status_code == 409
    assert unavailable.json()["detail"] == {
        "code": "room_template_members_unavailable", "agent_ids": [ids["first"]],
    }
    with client.app.state.session_factory() as db:
        assert db.query(Conversation).count() == before
        db.get(Agent, ids["first"]).is_active = True
        db.commit()

    def fail_schedule(_mapper, _connection, _target):
        raise RuntimeError("synthetic template commit failure")

    event.listen(RoomScheduleSettings, "before_insert", fail_schedule)
    try:
        with client.app.state.session_factory() as db, pytest.raises(RuntimeError, match="synthetic template commit failure"):
            room_template_service.instantiate_template(
                db, ids["project"], created["id"], ids["owner"])
    finally:
        event.remove(RoomScheduleSettings, "before_insert", fail_schedule)
    with client.app.state.session_factory() as db:
        assert db.query(Conversation).count() == before
        assert not db.query(RoomMembership).join(Conversation).filter(
            Conversation.title == "Release room").first()


def test_create_room_requires_exact_empty_body_and_active_owned_project(template_client):
    client, ids = template_client
    created = create_template(client, ids)
    own = auth(client, ids["owner"])
    endpoint = f"/api/v1/projects/{ids['project']}/room-templates/{created['id']}/rooms"
    assert client.post(endpoint, headers=own, json={"title": "override"}).status_code == 422
    assert client.post(endpoint, headers=auth(client, ids["other"]), json={}).status_code == 404
    wrong_project = f"/api/v1/projects/{ids['second_project']}/room-templates/{created['id']}/rooms"
    assert client.post(wrong_project, headers=own, json={}).status_code == 404
    with client.app.state.session_factory() as db:
        db.get(Project, ids["project"]).is_active = False
        db.commit()
    assert client.post(endpoint, headers=own, json={}).status_code == 404
