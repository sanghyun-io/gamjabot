"""Atomic owner project archive/restore contracts on disposable SQLite."""

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.collaboration import RoomAgentRun, RoomEvent, RoomGrant, RoomMembership, RoomTrigger, ToolAction
from app.models.models import Agent, Conversation, Project, User
from app.services import project_archive, room_service


@pytest.fixture
def archive_client(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "archive-token")
    app = create_app(database_url=sqlite_url)
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            owner = User(email="archive-owner@test.invalid", username="archive-owner", hashed_password="x")
            other = User(email="archive-other@test.invalid", username="archive-other", hashed_password="x")
            db.add_all([owner, other]); db.flush()
            project = Project(user_id=owner.id, name="Archive me")
            foreign = Project(user_id=other.id, name="Foreign")
            db.add_all([project, foreign]); db.flush()
            agent = Agent(user_id=owner.id, name="Ada", description="d", system_prompt="p")
            db.add(agent); db.flush()
            rooms = [Conversation(user_id=owner.id, project_id=project.id, title=title)
                     for title in ("First", "Second")]
            db.add_all(rooms); db.flush()
            for room in rooms:
                db.add(RoomMembership(room_id=room.id, agent_id=agent.id))
            db.flush()
            for room in rooms:
                db.add(RoomAgentRun(room_id=room.id, agent_id=agent.id, state="waiting", generation=3,
                                    pending_task={"private": True}, active_tool={"private": True}))
            db.commit()
            ids = {"owner": owner.id, "other": other.id, "project": project.id,
                   "foreign_project": foreign.id, "agent": agent.id,
                   "first": rooms[0].id, "second": rooms[1].id}
        yield client, ids


def auth(client, owner_id):
    return {
        "X-Gamja-Session": client.app.state.local_session_token,
        "Authorization": f"Bearer {create_access_token({'sub': owner_id})}",
    }


def _event(db, room_id, owner_id, seq, *, key):
    row = RoomEvent(event_id=str(uuid4()), room_id=room_id, seq=seq, actor_type="user",
                    actor_id=owner_id, actor_name_snapshot="archive-owner", kind="message",
                    payload={"content": f"message-{seq}"}, created_at=datetime.now(timezone.utc),
                    idempotency_key=key)
    db.add(row); db.flush()
    return row


def _seed_work(client, ids):
    with client.app.state.session_factory() as db:
        first = _event(db, ids["first"], ids["owner"], 1, key="first-trigger")
        second = _event(db, ids["second"], ids["owner"], 1, key="second-trigger")
        db.add_all([
            RoomTrigger(event_id=first.event_id, room_id=ids["first"], state="pending",
                        selected_agents=[ids["agent"]]),
            RoomTrigger(event_id=second.event_id, room_id=ids["second"], state="running",
                        selected_agents=[ids["agent"]]),
        ])
        grant = RoomGrant(room_id=ids["first"], agent_id=ids["agent"], scope={"kind": "once"},
                          decision="approved")
        db.add(grant); db.flush()
        db.add_all([
            ToolAction(action_id=str(uuid4()), room_id=ids["first"], agent_id=ids["agent"],
                       trigger_event_id=first.event_id, call_id="wait", family="read_file",
                       arguments={"path": "/private"}, state="waiting"),
            ToolAction(action_id=str(uuid4()), room_id=ids["second"], agent_id=ids["agent"],
                       trigger_event_id=second.event_id, call_id="started", family="start_process",
                       arguments={"secret": "never-return"}, state="started"),
        ])
        db.commit()


def test_preview_archive_lost_response_replay_and_restore_keep_children_closed(archive_client):
    client, ids = archive_client
    _seed_work(client, ids)
    own, other = auth(client, ids["owner"]), auth(client, ids["other"])

    assert client.post(f"/api/v1/projects/{ids['project']}/archive/preview", headers=other).status_code == 404
    preview = client.post(f"/api/v1/projects/{ids['project']}/archive/preview", headers=own)
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert len(plan["plan_hash"]) == 64
    assert plan["counts"] == {
        "active_rooms": 2, "pending_triggers": 1, "running_triggers": 1,
        "running_runs": 0, "waiting_runs": 2, "waiting_approvals": 1,
        "started_actions_uncertain": 1, "open_actions_cancelled": 1, "grants_revoked": 1,
    }
    assert [row["title"] for row in plan["rooms"]] == ["First", "Second"]
    assert "private" not in preview.text and "never-return" not in preview.text

    applied = client.post(f"/api/v1/projects/{ids['project']}/archive", headers=own,
                          json={"plan_hash": plan["plan_hash"]})
    assert applied.status_code == 200, applied.text
    result = applied.json()
    assert result["is_active"] is False and result["counts"] == plan["counts"]
    assert result["cleanup_unconfirmed"] == []

    # A client that lost the first response can repeat the exact digest.
    replay = client.post(f"/api/v1/projects/{ids['project']}/archive", headers=own,
                         json={"plan_hash": plan["plan_hash"]})
    assert replay.status_code == 200 and replay.json()["counts"] == plan["counts"]
    # Exact replay releases BEGIN IMMEDIATE before returning; another writer is not locked.
    with client.app.state.session_factory() as db:
        db.get(Project, ids["foreign_project"]).name = "Writable after replay"
        db.commit()

    with client.app.state.session_factory() as db:
        project = db.get(Project, ids["project"])
        assert project.is_active is False and project.archive_plan_hash == plan["plan_hash"]
        assert all(db.get(Conversation, room_id).is_active is False
                   for room_id in (ids["first"], ids["second"]))
        assert {row.state for row in db.query(RoomTrigger).all()} == {"cancelled"}
        runs = db.query(RoomAgentRun).order_by(RoomAgentRun.room_id).all()
        assert [(row.state, row.generation, row.pending_task, row.active_tool) for row in runs] == [
            ("stopped", 4, {}, {}), ("stopped", 4, {}, {})]
        assert {row.state for row in db.query(ToolAction).all()} == {"cancelled", "uncertain"}
        assert db.query(RoomEvent).filter(RoomEvent.kind == "tool_result").count() == 2
        assert db.query(RoomGrant).one().decision == "revoked"

    restored = client.post(f"/api/v1/projects/{ids['project']}/restore", headers=own)
    assert restored.status_code == 200 and restored.json() == {
        "project_id": ids["project"], "is_active": True, "closed_rooms": 2}
    restore_replay = client.post(f"/api/v1/projects/{ids['project']}/restore", headers=own)
    assert restore_replay.status_code == 200 and restore_replay.json() == restored.json()
    with client.app.state.session_factory() as db:
        assert db.get(Project, ids["project"]).archive_plan_hash is None
        assert all(db.get(Conversation, room_id).is_active is False
                   for room_id in (ids["first"], ids["second"]))
        assert room_service.eligible_agents(db, ids["first"]) == []


def test_stale_digest_and_injected_later_room_failure_leave_everything_active(archive_client, monkeypatch):
    client, ids = archive_client
    own = auth(client, ids["owner"])
    plan = client.post(f"/api/v1/projects/{ids['project']}/archive/preview", headers=own).json()
    with client.app.state.session_factory() as db:
        _event(db, ids["second"], ids["owner"], 1, key="later-change")
        db.commit()
    stale = client.post(f"/api/v1/projects/{ids['project']}/archive", headers=own,
                        json={"plan_hash": plan["plan_hash"]})
    assert stale.status_code == 409
    assert stale.json()["detail"] == {"code": "project_archive_plan_stale"}

    with client.app.state.session_factory() as db:
        fresh = project_archive.preview(db, ids["project"], ids["owner"])
    original = project_archive.stage_close_room
    calls = 0
    def fail_second(db, room_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected later-room failure")
        return original(db, room_id)
    monkeypatch.setattr(project_archive, "stage_close_room", fail_second)
    with client.app.state.session_factory() as db, pytest.raises(RuntimeError, match="later-room"):
        project_archive.apply(db, ids["project"], ids["owner"], fresh["plan_hash"])
    with client.app.state.session_factory() as db:
        assert db.get(Project, ids["project"]).is_active is True
        assert [db.get(Conversation, room_id).is_active for room_id in (ids["first"], ids["second"])] == [True, True]
        assert [db.get(RoomAgentRun, (room_id, ids["agent"])).generation
                for room_id in (ids["first"], ids["second"])] == [3, 3]


def test_preview_is_bounded_and_counts_only_active_children(archive_client):
    client, ids = archive_client
    with client.app.state.session_factory() as db:
        # Twenty-one active children prove the response list is bounded while the
        # aggregate count remains exact. Closed legacy state is deliberately noisy
        # and must not be described as work that this archive operation will cancel.
        db.add_all([
            Conversation(user_id=ids["owner"], project_id=ids["project"], title=f"Extra {index}")
            for index in range(19)
        ])
        closed = Conversation(user_id=ids["owner"], project_id=ids["project"],
                              title="Already closed", is_active=False)
        db.add(closed)
        db.flush()
        grant = RoomGrant(room_id=closed.id, agent_id=ids["agent"],
                          scope={"kind": "legacy"}, decision="approved")
        db.add(grant)
        db.flush()
        db.add(ToolAction(action_id=str(uuid4()), room_id=closed.id, agent_id=ids["agent"],
                          grant_id=grant.id, call_id="closed", family="read_file",
                          arguments={"path": "/not-returned"}, state="waiting"))
        db.commit()

    response = client.post(f"/api/v1/projects/{ids['project']}/archive/preview",
                           headers=auth(client, ids["owner"]))
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["counts"]["active_rooms"] == 21
    assert plan["counts"]["waiting_approvals"] == 0
    assert plan["counts"]["open_actions_cancelled"] == 0
    assert plan["counts"]["grants_revoked"] == 0
    assert len(plan["rooms"]) == 20
    assert plan["rooms_truncated"] is True
    assert "Already closed" not in response.text
    assert "not-returned" not in response.text


def test_postcommit_cleanup_failure_is_reported_without_rolling_back_archive(archive_client, monkeypatch):
    client, ids = archive_client
    own = auth(client, ids["owner"])
    plan = client.post(f"/api/v1/projects/{ids['project']}/archive/preview", headers=own).json()
    original = client.app.state.local_executor.stop_room

    async def fail_one(room_id, agent_id=None):
        if room_id == ids["second"]:
            raise RuntimeError("synthetic cleanup failure")
        return await original(room_id, agent_id)

    monkeypatch.setattr(client.app.state.local_executor, "stop_room", fail_one)
    response = client.post(f"/api/v1/projects/{ids['project']}/archive", headers=own,
                           json={"plan_hash": plan["plan_hash"]})
    assert response.status_code == 200
    assert response.json()["cleanup_unconfirmed"] == [ids["second"]]
    with client.app.state.session_factory() as db:
        assert db.get(Project, ids["project"]).is_active is False
        assert [db.get(Conversation, room_id).is_active
                for room_id in (ids["first"], ids["second"])] == [False, False]


def test_legacy_inactive_project_is_visible_but_active_child_must_be_safely_archived_before_restore(archive_client):
    client, ids = archive_client
    own = auth(client, ids["owner"])
    with client.app.state.session_factory() as db:
        db.get(Project, ids["project"]).is_active = False
        db.commit()
    archived = client.get("/api/v1/projects/?status=archived", headers=own)
    assert archived.status_code == 200 and [row["id"] for row in archived.json()] == [ids["project"]]
    blocked = client.post(f"/api/v1/projects/{ids['project']}/restore", headers=own)
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == {"code": "project_restore_active_children", "active_rooms": 2}

    plan = client.post(f"/api/v1/projects/{ids['project']}/archive/preview", headers=own).json()
    assert plan["counts"]["active_rooms"] == 2
    assert client.post(f"/api/v1/projects/{ids['project']}/archive", headers=own,
                       json={"plan_hash": plan["plan_hash"]}).status_code == 200
    assert client.post(f"/api/v1/projects/{ids['project']}/restore", headers=own).status_code == 200


def test_inactive_project_and_room_block_dispatch_even_if_a_pending_trigger_survives_restart(archive_client):
    client, ids = archive_client
    with client.app.state.session_factory() as db:
        event = _event(db, ids["first"], ids["owner"], 1, key="restart-pending")
        db.add(RoomTrigger(event_id=event.event_id, room_id=ids["first"], state="pending",
                           selected_agents=[ids["agent"]]))
        event_id = event.event_id
        db.get(Project, ids["project"]).is_active = False
        db.commit()
    own = auth(client, ids["owner"])
    assert client.post(f"/api/v1/conversations/{ids['first']}/events", headers=own,
                       json={"content": "must not dispatch"}).status_code == 404
    assert client.patch(f"/api/v1/conversations/{ids['first']}", headers=own,
                        json={"title": "must not rename"}).status_code == 404
    assert client.delete(f"/api/v1/conversations/{ids['first']}", headers=own).status_code == 404
    assert client.post(f"/api/v1/conversations/{ids['first']}/members/{ids['agent']}/pause",
                       headers=own).status_code == 404
    with client.app.state.session_factory() as db:
        assert room_service.eligible_agents(db, ids["first"]) == []
        assert room_service.begin_turn(db, ids["first"], ids["agent"]) is None
    asyncio.run(client.app.state.scheduler._run_trigger(event_id))
    with client.app.state.session_factory() as db:
        assert db.get(RoomTrigger, event_id).state == "cancelled"
