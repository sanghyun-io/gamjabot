"""Owner room lifecycle and clone contracts use disposable SQLite only."""

import asyncio
from datetime import datetime
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.collaboration import (
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
from app.services import events
from app.services import room_service
from app.services.event_hub import EventHub
from app.services.scheduler import Scheduler


@pytest.fixture
def management_client(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "room-management-token")
    app = create_app(database_url=sqlite_url)
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            owner = User(email="manage-owner@test.invalid", username="manage-owner", hashed_password="x")
            other = User(email="manage-other@test.invalid", username="manage-other", hashed_password="x")
            db.add_all([owner, other])
            db.flush()
            project = Project(user_id=owner.id, name="Main")
            destination = Project(user_id=owner.id, name="Destination")
            foreign_project = Project(user_id=other.id, name="Foreign")
            inactive_project = Project(user_id=owner.id, name="Inactive", is_active=False)
            db.add_all([project, destination, foreign_project, inactive_project])
            db.flush()
            first = Agent(user_id=owner.id, name="Ada", description="d", system_prompt="p")
            second = Agent(user_id=owner.id, name="Lin", description="d", system_prompt="p")
            foreign = Agent(user_id=other.id, name="Other", description="d", system_prompt="p")
            db.add_all([first, second, foreign])
            db.commit()
            ids = {
                "owner": owner.id, "other": other.id,
                "project": project.id, "destination": destination.id,
                "foreign_project": foreign_project.id, "inactive_project": inactive_project.id,
                "first": first.id, "second": second.id, "foreign": foreign.id,
            }
        yield client, ids


def auth(client, owner_id):
    return {
        "X-Gamja-Session": client.app.state.local_session_token,
        "Authorization": f"Bearer {create_access_token({'sub': owner_id})}",
    }


def create_room(client, owner_id, project_id, title):
    response = client.post("/api/v1/conversations/", headers=auth(client, owner_id),
                           json={"project_id": project_id, "title": title})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def join(client, owner_id, room_id, agent_id):
    response = client.post(f"/api/v1/conversations/{room_id}/members/{agent_id}",
                           headers=auth(client, owner_id))
    assert response.status_code == 201, response.text


def test_owner_lists_active_closed_or_all_and_renames_closed_room(management_client):
    client, ids = management_client
    own = auth(client, ids["owner"])
    active = create_room(client, ids["owner"], ids["project"], "Active")
    closed = create_room(client, ids["owner"], ids["project"], "Closed")
    assert client.delete(f"/api/v1/conversations/{closed}", headers=own).status_code == 204

    base = f"/api/v1/conversations/project/{ids['project']}"
    assert [row["id"] for row in client.get(base, headers=own).json()] == [active]
    assert [row["id"] for row in client.get(base + "?status=closed", headers=own).json()] == [closed]
    assert {row["id"] for row in client.get(base + "?status=all", headers=own).json()} == {active, closed}
    assert client.get(base + "?status=invalid", headers=own).status_code == 422
    assert client.get(f"/api/v1/conversations/project/{ids['foreign_project']}?status=all", headers=own).status_code == 404

    archived = client.get(f"/api/v1/conversations/{closed}", headers=own)
    assert archived.status_code == 200 and archived.json()["is_active"] is False
    assert client.get(f"/api/v1/conversations/{closed}", headers=auth(client, ids["other"])).status_code == 404
    assert client.get(f"/api/v1/conversations/{closed}/members",
                      headers=auth(client, ids["other"])).status_code == 404
    assert client.patch(f"/api/v1/conversations/{closed}", headers=auth(client, ids["other"]),
                        json={"title": "Foreign edit"}).status_code == 404
    renamed = client.patch(f"/api/v1/conversations/{closed}", headers=own, json={"title": "  Renamed room  "})
    assert renamed.status_code == 200 and renamed.json()["title"] == "Renamed room"
    boundary = client.patch(f"/api/v1/conversations/{closed}", headers=own,
                            json={"title": "  " + "x" * 200 + "  "})
    assert boundary.status_code == 200 and boundary.json()["title"] == "x" * 200
    assert client.patch(f"/api/v1/conversations/{closed}", headers=own,
                        json={"title": "x" * 201}).status_code == 422
    assert client.patch(f"/api/v1/conversations/{closed}", headers=own, json={"title": "   "}).status_code == 422
    assert client.patch(f"/api/v1/conversations/{closed}", headers=own,
                        json={"title": "ok", "extra": True}).status_code == 422


def test_close_preserves_read_only_roster_but_stop_keeps_existing_semantics_and_mutations_stay_blocked(management_client):
    client, ids = management_client
    own = auth(client, ids["owner"])
    closed = create_room(client, ids["owner"], ids["project"], "Close")
    stopped = create_room(client, ids["owner"], ids["project"], "Stop")
    join(client, ids["owner"], closed, ids["first"])
    join(client, ids["owner"], stopped, ids["first"])
    assert client.delete(f"/api/v1/conversations/{closed}", headers=own).status_code == 204
    members = client.get(f"/api/v1/conversations/{closed}/members", headers=own)
    assert members.status_code == 200 and [row["agent_id"] for row in members.json()] == [ids["first"]]
    assert members.json()[0]["run_state"] == "stopped"
    assert client.get(f"/api/v1/conversations/{closed}/schedule", headers=own).status_code == 200
    assert client.get(f"/api/v1/conversations/{closed}/events", headers=own).status_code == 200

    blocked = [
        client.post(f"/api/v1/conversations/{closed}/events", headers=own, json={"content": "no"}),
        client.put(f"/api/v1/conversations/{closed}/schedule", headers=own,
                   json={"max_rounds": 2, "max_tool_calls": 8, "max_executor_seconds": 60}),
        client.post(f"/api/v1/conversations/{closed}/members/{ids['second']}", headers=own),
        client.delete(f"/api/v1/conversations/{closed}/members/{ids['first']}", headers=own),
        client.post(f"/api/v1/conversations/{closed}/members/{ids['first']}/pause", headers=own),
        client.post(f"/api/v1/conversations/{closed}/members/{ids['first']}/resume", headers=own),
        client.post(f"/api/v1/conversations/{closed}/continue", headers=own, json={}),
        client.post(f"/api/v1/conversations/{closed}/summaries", headers=own,
                    json={"source_start_seq": 1, "source_end_seq": 1, "content": "no"}),
    ]
    assert {response.status_code for response in blocked} == {404}

    assert client.post(f"/api/v1/conversations/{stopped}/stop", headers=own).status_code == 204
    assert client.get(f"/api/v1/conversations/{stopped}/members", headers=own).json() == []
    with client.app.state.session_factory() as db:
        assert db.get(RoomMembership, (closed, ids["first"])).is_active is True
        assert db.get(RoomMembership, (stopped, ids["first"])).is_active is False


def test_clone_copies_only_preserved_roster_and_effective_schedule_into_new_empty_room(management_client):
    client, ids = management_client
    own = auth(client, ids["owner"])
    source = create_room(client, ids["owner"], ids["project"], "Source")
    for agent_id in (ids["first"], ids["second"]):
        join(client, ids["owner"], source, agent_id)
    custom = {"max_rounds": 4, "max_tool_calls": 17, "max_executor_seconds": 321}
    assert client.put(f"/api/v1/conversations/{source}/schedule", headers=own, json=custom).status_code == 200

    with client.app.state.session_factory() as db:
        event_row = events.append_event(db, source, events.ActorRef("user", ids["owner"]),
                                        "message", {"content": "private source"}, "source-key")
        db.add(RoomTrigger(event_id=event_row.event_id, room_id=source, state="done", selected_agents=[ids["first"]]))
        grant = RoomGrant(room_id=source, agent_id=ids["first"], scope={"kind": "once"}, decision="revoked")
        db.add(grant)
        db.flush()
        db.add(ToolAction(action_id=str(uuid4()), room_id=source, agent_id=ids["first"],
                          trigger_event_id=event_row.event_id, call_id="source-call", grant_id=grant.id,
                          family="read_file", arguments={"path": "/source-only"}, state="cancelled"))
        db.add(RoomDraft(owner_id=ids["owner"], room_id=source, content="draft", version=1,
                         idempotency_key=str(uuid4()), updated_at=datetime.utcnow()))
        db.add(RoomReadState(owner_id=ids["owner"], room_id=source, last_read_seq=1,
                             updated_at=datetime.utcnow()))
        run = db.get(RoomAgentRun, (source, ids["first"]))
        run.state, run.paused, run.generation = "paused", True, 9
        run.watermark_seq, run.round, run.trigger_event_id = 1, 3, event_row.event_id
        run.checkpoint, run.memory = {"old": "checkpoint"}, {"private": "memory"}
        run.pending_task, run.active_tool = {"pending": True}, {"tool": True}
        db.commit()
    assert client.delete(f"/api/v1/conversations/{source}", headers=own).status_code == 204

    response = client.post(f"/api/v1/conversations/{source}/clone", headers=own,
                           json={"project_id": ids["destination"], "title": "  Fresh copy  "})
    assert response.status_code == 201, response.text
    target = response.json()["id"]
    assert response.json()["title"] == "Fresh copy" and response.json()["is_active"] is True
    assert [row["agent_id"] for row in client.get(f"/api/v1/conversations/{target}/members", headers=own).json()] == [ids["first"], ids["second"]]
    assert client.get(f"/api/v1/conversations/{target}/schedule", headers=own).json() == custom

    with client.app.state.session_factory() as db:
        assert db.query(RoomEvent).filter_by(room_id=target).count() == 0
        assert db.query(Message).filter_by(conversation_id=target).count() == 0
        assert db.query(RoomTrigger).filter_by(room_id=target).count() == 0
        assert db.query(ToolAction).filter_by(room_id=target).count() == 0
        assert db.query(RoomGrant).filter_by(room_id=target).count() == 0
        assert db.query(RoomDraft).filter_by(room_id=target).count() == 0
        assert db.query(RoomReadState).filter_by(room_id=target).count() == 0
        for agent_id in (ids["first"], ids["second"]):
            run = db.get(RoomAgentRun, (target, agent_id))
            assert (run.state, run.paused, run.generation) == ("idle", False, 0)
            assert run.watermark_seq is None and run.round is None and run.trigger_event_id is None
            assert run.checkpoint == run.memory == run.pending_task == run.active_tool == {}

    default_source = create_room(client, ids["owner"], ids["project"], "No settings")
    default_clone = client.post(f"/api/v1/conversations/{default_source}/clone", headers=own,
                                json={"project_id": ids["destination"], "title": "Default policy"})
    assert default_clone.status_code == 201
    default_id = default_clone.json()["id"]
    assert client.get(f"/api/v1/conversations/{default_id}/members", headers=own).json() == []
    assert client.get(f"/api/v1/conversations/{default_id}/schedule", headers=own).json() == {
        "max_rounds": 2, "max_tool_calls": 8, "max_executor_seconds": 60,
    }
    with client.app.state.session_factory() as db:
        assert db.get(RoomScheduleSettings, default_id) is not None


def test_clone_preflight_rejects_unavailable_roster_without_partial_target(management_client):
    client, ids = management_client
    own = auth(client, ids["owner"])
    source = create_room(client, ids["owner"], ids["project"], "Invalid roster")
    with client.app.state.session_factory() as db:
        db.get(Agent, ids["second"]).is_active = False
        for agent_id in (ids["second"], ids["foreign"]):
            db.add(RoomMembership(room_id=source, agent_id=agent_id))
        db.flush()
        for agent_id in (ids["second"], ids["foreign"]):
            db.add(RoomAgentRun(room_id=source, agent_id=agent_id))
        db.commit()
    before = None
    with client.app.state.session_factory() as db:
        before = db.query(Conversation).count()
    response = client.post(f"/api/v1/conversations/{source}/clone", headers=own,
                           json={"project_id": ids["destination"], "title": "Must not exist"})
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "room_clone_members_unavailable",
        "agent_ids": sorted([ids["second"], ids["foreign"]]),
    }
    with client.app.state.session_factory() as db:
        assert db.query(Conversation).count() == before
        assert db.query(Conversation).filter_by(title="Must not exist").count() == 0

    assert client.post(f"/api/v1/conversations/{source}/clone", headers=auth(client, ids["other"]),
                       json={"project_id": ids["foreign_project"], "title": "Foreign"}).status_code == 404
    assert client.post(f"/api/v1/conversations/{source}/clone", headers=own,
                       json={"project_id": ids["foreign_project"], "title": "Foreign project"}).status_code == 404
    assert client.post(f"/api/v1/conversations/{source}/clone", headers=own,
                       json={"project_id": ids["inactive_project"], "title": "Inactive project"}).status_code == 404
    assert client.post(f"/api/v1/conversations/{source}/clone", headers=own,
                       json={"project_id": ids["destination"], "title": "Extra", "target_room_id": source}).status_code == 422


def test_clone_commit_failure_rolls_back_every_target_row(management_client):
    client, ids = management_client
    source = create_room(client, ids["owner"], ids["project"], "Rollback source")
    join(client, ids["owner"], source, ids["first"])

    def fail_schedule(_mapper, _connection, _target):
        raise RuntimeError("synthetic commit failure")

    event.listen(RoomScheduleSettings, "before_insert", fail_schedule)
    try:
        with client.app.state.session_factory() as db, pytest.raises(RuntimeError, match="synthetic commit failure"):
            room_service.clone_room(db, source, ids["owner"], ids["destination"], "Rollback target")
    finally:
        event.remove(RoomScheduleSettings, "before_insert", fail_schedule)
    with client.app.state.session_factory() as db:
        assert db.query(Conversation).filter_by(title="Rollback target").count() == 0
        assert not db.query(RoomMembership).join(Conversation).filter(Conversation.title == "Rollback target").first()


def test_close_cancels_durable_work_and_restart_never_dispatches_closed_room(management_client, monkeypatch):
    client, ids = management_client
    own = auth(client, ids["owner"])
    room = create_room(client, ids["owner"], ids["project"], "Queued")
    join(client, ids["owner"], room, ids["first"])
    with client.app.state.session_factory() as db:
        row = events.append_event(db, room, events.ActorRef("user", ids["owner"]),
                                  "message", {"content": "queued"}, "queued-key")
        db.add(RoomTrigger(event_id=row.event_id, room_id=room, state="pending", selected_agents=[ids["first"]]))
        db.add(RoomGrant(room_id=room, agent_id=ids["first"], scope={"kind": "once"}, decision="approved"))
        db.flush()
        grant_id = db.query(RoomGrant.id).filter_by(room_id=room).scalar()
        db.add(ToolAction(action_id=str(uuid4()), room_id=room, agent_id=ids["first"],
                          trigger_event_id=row.event_id, call_id="call", grant_id=grant_id,
                          family="read_file", arguments={"path": "/tmp/no"}, state="approved"))
        run = db.get(RoomAgentRun, (room, ids["first"]))
        run.state, run.pending_task, run.active_tool = "waiting", {"queued": True}, {"tool": True}
        db.commit()
        trigger_id = row.event_id
    assert client.delete(f"/api/v1/conversations/{room}", headers=own).status_code == 204
    with client.app.state.session_factory() as db:
        assert db.get(Conversation, room).is_active is False
        assert db.get(RoomMembership, (room, ids["first"])).is_active is True
        assert db.get(RoomTrigger, trigger_id).state == "cancelled"
        run = db.get(RoomAgentRun, (room, ids["first"]))
        assert run.state == "stopped" and run.pending_task == run.active_tool == {}
        assert db.query(RoomGrant).filter_by(room_id=room).one().decision == "revoked"
        assert db.query(ToolAction).filter_by(room_id=room).one().state == "cancelled"

    dispatched = []
    original = Scheduler._run_trigger

    async def observe(self, event_id):
        dispatched.append(event_id)
        return await original(self, event_id)

    monkeypatch.setattr(Scheduler, "_run_trigger", observe)
    async def restart_scheduler():
        restarted = Scheduler(client.app.state.session_factory, EventHub(), client.app.state)
        await restarted.start()
        await asyncio.sleep(0.02)
        await restarted.close()
    asyncio.run(restart_scheduler())
    assert dispatched == []
