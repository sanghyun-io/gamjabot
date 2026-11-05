"""Exact per-request completion is independent from the bounded inspector display."""

from datetime import datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event as sqlalchemy_event

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.collaboration import RoomEvent, RoomTrigger, ToolAction
from app.models.models import Agent, Conversation, Project, User


@pytest.fixture
def completion_client(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "completion-token")
    app = create_app(database_url=sqlite_url)
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            owner = User(email="completion@test.invalid", username="owner", hashed_password="x")
            db.add(owner)
            db.commit()
            owner_id = owner.id
        yield client, owner_id


def auth(client, owner_id):
    return {
        "X-Gamja-Session": client.app.state.local_session_token,
        "Authorization": f"Bearer {create_access_token({'sub': owner_id})}",
    }


def make_room(db, owner_id):
    project = Project(user_id=owner_id, name="Completion", is_active=True)
    db.add(project)
    db.flush()
    room = Conversation(user_id=owner_id, project_id=project.id, title="Exact work", is_active=True)
    db.add(room)
    db.flush()
    return room


def add_event(db, room, seq, actor_type, kind, payload, *, actor_id=None, name=None):
    value = RoomEvent(
        event_id=str(uuid4()), room_id=room.id, seq=seq, actor_type=actor_type,
        actor_id=room.user_id if actor_type == "user" and actor_id is None else actor_id,
        actor_name_snapshot=name or actor_type.title(), kind=kind, payload=payload,
        created_at=datetime(2026, 10, 6, 9, 0, 0) + timedelta(seconds=seq),
    )
    db.add(value)
    db.flush()
    return value


def inspect(client, owner_id, room_id, request_id):
    response = client.get(
        f"/api/v1/conversations/{room_id}/requests/{request_id}",
        headers=auth(client, owner_id),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_completion_stays_running_until_exact_two_by_two_terminal_outcomes(completion_client):
    client, owner_id = completion_client
    with client.app.state.session_factory() as db:
        room = make_room(db, owner_id)
        proposer = Agent(user_id=owner_id, name="Proposer", description="d", system_prompt="p")
        reviewer = Agent(user_id=owner_id, name="Reviewer", description="d", system_prompt="p")
        db.add_all([proposer, reviewer])
        db.flush()
        request = add_event(db, room, 1, "user", "message", {"content": "Decide"})
        trigger = RoomTrigger(
            event_id=request.event_id, room_id=room.id, state="running",
            selected_agents=[proposer.id, reviewer.id], max_rounds=2,
            max_tool_calls=0, max_executor_seconds=60, current_round=1,
        )
        db.add(trigger)
        db.commit()
        values = {"room": room.id, "request": request.event_id,
                  "agents": (proposer.id, reviewer.id)}

    first = inspect(client, owner_id, values["room"], values["request"])
    assert first["completion"] == {
        "state": "running", "outcome_status": "pending", "result_available": False,
        "completed_rounds": 0, "pending_agent_count": 2,
    }

    with client.app.state.session_factory() as db:
        room = db.get(Conversation, values["room"])
        seq = 2
        for round_number in (1, 2):
            for agent_id, label in zip(values["agents"], ("Proposer", "Reviewer")):
                add_event(db, room, seq, "agent", "message", {
                    "content": f"round {round_number}", "trigger_event_id": values["request"],
                    "round": round_number,
                    "model_use": {"connection_id": 1, "model_id": "exact",
                                  "provider_kind": "compatible", "watermark_seq": seq - 1},
                }, actor_id=agent_id, name=label)
                seq += 1
        trigger = db.get(RoomTrigger, values["request"])
        trigger.current_round = 2
        trigger.state = "done"
        db.commit()

    done = inspect(client, owner_id, values["room"], values["request"])
    assert done["completion"] == {
        "state": "completed", "outcome_status": "succeeded", "result_available": True,
        "completed_rounds": 2, "pending_agent_count": 0,
    }
    assert done["execution"]["policy"]["max_tool_calls"] == 0


def test_completion_truth_table_waiting_partial_all_pass_and_legacy_unknown(completion_client):
    client, owner_id = completion_client
    cases = {}
    with client.app.state.session_factory() as db:
        room = make_room(db, owner_id)
        first = Agent(user_id=owner_id, name="First", description="d", system_prompt="p")
        second = Agent(user_id=owner_id, name="Second", description="d", system_prompt="p")
        db.add_all([first, second])
        db.flush()

        waiting = add_event(db, room, 1, "user", "message", {"content": "waiting"})
        db.add(RoomTrigger(event_id=waiting.event_id, room_id=room.id, state="running",
                           selected_agents=[first.id], max_rounds=1, max_tool_calls=1,
                           max_executor_seconds=60, current_round=1))
        db.add(ToolAction(action_id=str(uuid4()), room_id=room.id, agent_id=first.id,
                          trigger_event_id=waiting.event_id, family="read_file", arguments={}, state="waiting"))

        partial = add_event(db, room, 2, "user", "message", {"content": "partial"})
        db.add(RoomTrigger(event_id=partial.event_id, room_id=room.id, state="done",
                           selected_agents=[first.id, second.id], max_rounds=1, max_tool_calls=0,
                           max_executor_seconds=60, current_round=1))
        add_event(db, room, 3, "agent", "message", {
            "content": "useful", "trigger_event_id": partial.event_id, "round": 1,
        }, actor_id=first.id, name="First snapshot")
        add_event(db, room, 4, "agent", "failure", {
            "code": "provider_error", "trigger_event_id": partial.event_id, "round": 1,
        }, actor_id=second.id, name="Second snapshot")

        all_pass = add_event(db, room, 5, "user", "message", {"content": "pass"})
        db.add(RoomTrigger(event_id=all_pass.event_id, room_id=room.id, state="done",
                           selected_agents=[first.id, second.id], max_rounds=2, max_tool_calls=0,
                           max_executor_seconds=60, current_round=1))
        for seq, agent_id in ((6, first.id), (7, second.id)):
            add_event(db, room, seq, "agent", "activity", {
                "status": "passed", "trigger_event_id": all_pass.event_id, "round": 1,
            }, actor_id=agent_id, name="Pass snapshot")

        malformed = add_event(db, room, 8, "user", "message", {"content": "legacy"})
        db.add(RoomTrigger(event_id=malformed.event_id, room_id=room.id, state="done",
                           selected_agents=[first.id], max_rounds=1, max_tool_calls=0,
                           max_executor_seconds=60, current_round=1))
        add_event(db, room, 9, "agent", "message", {
            "content": "ambiguous", "trigger_event_id": malformed.event_id, "round": "one",
        }, actor_id=first.id, name="Legacy snapshot")

        missing_prior = add_event(db, room, 10, "user", "message", {"content": "missing prior"})
        db.add(RoomTrigger(event_id=missing_prior.event_id, room_id=room.id, state="done",
                           selected_agents=[first.id, second.id], max_rounds=2, max_tool_calls=0,
                           max_executor_seconds=60, current_round=2))
        add_event(db, room, 11, "agent", "message", {
            "content": "first only", "trigger_event_id": missing_prior.event_id, "round": 1,
        }, actor_id=first.id, name="First snapshot")
        add_event(db, room, 12, "agent", "message", {
            "content": "second follow-up", "trigger_event_id": missing_prior.event_id, "round": 2,
        }, actor_id=second.id, name="Second snapshot")

        cancelled = add_event(db, room, 13, "user", "message", {"content": "cancelled"})
        db.add(RoomTrigger(event_id=cancelled.event_id, room_id=room.id, state="cancelled",
                           selected_agents=[first.id], max_rounds=1, max_tool_calls=0,
                           max_executor_seconds=60, current_round=1))

        interrupted = add_event(db, room, 14, "user", "message", {"content": "restart"})
        db.add(RoomTrigger(event_id=interrupted.event_id, room_id=room.id, state="interrupted",
                           selected_agents=[first.id], max_rounds=1, max_tool_calls=0,
                           max_executor_seconds=60, current_round=1))
        add_event(db, room, 15, "agent", "message", {
            "content": "partial before restart", "trigger_event_id": interrupted.event_id, "round": 1,
        }, actor_id=first.id, name="First snapshot")
        db.commit()
        cases = {"room": room.id, "waiting": waiting.event_id, "partial": partial.event_id,
                 "pass": all_pass.event_id, "legacy": malformed.event_id,
                 "missing_prior": missing_prior.event_id, "cancelled": cancelled.event_id,
                 "interrupted": interrupted.event_id}

    waiting_data = inspect(client, owner_id, cases["room"], cases["waiting"])
    assert waiting_data["completion"] == {
        "state": "waiting_approval", "outcome_status": "pending", "result_available": False,
        "completed_rounds": 0, "pending_agent_count": 1,
    }
    partial_data = inspect(client, owner_id, cases["room"], cases["partial"])
    assert partial_data["completion"] == {
        "state": "completed", "outcome_status": "partial", "result_available": True,
        "completed_rounds": 1, "pending_agent_count": 0,
    }
    pass_data = inspect(client, owner_id, cases["room"], cases["pass"])
    assert pass_data["completion"] == {
        "state": "completed", "outcome_status": "succeeded", "result_available": False,
        "completed_rounds": 1, "pending_agent_count": 0,
    }
    legacy_data = inspect(client, owner_id, cases["room"], cases["legacy"])
    assert legacy_data["completion"] == {
        "state": "completed", "outcome_status": "unknown", "result_available": False,
        "completed_rounds": 0, "pending_agent_count": 1,
    }
    missing_prior_data = inspect(client, owner_id, cases["room"], cases["missing_prior"])
    assert missing_prior_data["completion"] == {
        "state": "completed", "outcome_status": "unknown", "result_available": True,
        "completed_rounds": 0, "pending_agent_count": 0,
    }
    cancelled_data = inspect(client, owner_id, cases["room"], cases["cancelled"])
    assert cancelled_data["completion"] == {
        "state": "cancelled", "outcome_status": "failed", "result_available": False,
        "completed_rounds": 0, "pending_agent_count": 1,
    }
    interrupted_data = inspect(client, owner_id, cases["room"], cases["interrupted"])
    assert interrupted_data["completion"] == {
        "state": "interrupted", "outcome_status": "partial", "result_available": True,
        "completed_rounds": 1, "pending_agent_count": 0,
    }


def test_completion_uses_all_exact_outcomes_without_mutating_or_waking(completion_client):
    client, owner_id = completion_client
    with client.app.state.session_factory() as db:
        room = make_room(db, owner_id)
        worker = Agent(user_id=owner_id, name="Worker", description="d", system_prompt="p")
        db.add(worker)
        db.flush()
        request = add_event(db, room, 1, "user", "message", {"content": "bounded"})
        db.add(RoomTrigger(event_id=request.event_id, room_id=room.id, state="done",
                           selected_agents=[worker.id], max_rounds=1, max_tool_calls=0,
                           max_executor_seconds=60, current_round=1))
        for number in range(513):
            add_event(db, room, number + 2, "agent", "failure", {
                "code": "provider_error", "trigger_event_id": request.event_id, "round": 1,
            }, actor_id=worker.id, name="Worker snapshot")
        add_event(db, room, 515, "agent", "message", {
            "content": "private useful result", "trigger_event_id": request.event_id, "round": 1,
        }, actor_id=worker.id, name="Worker snapshot")
        db.commit()
        room_id, request_id = room.id, request.event_id

    statements = []
    engine = client.app.state.session_factory.kw["bind"]
    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lstrip().split(None, 1)[0].upper())
    sqlalchemy_event.listen(engine, "before_cursor_execute", observe)
    try:
        data = inspect(client, owner_id, room_id, request_id)
    finally:
        sqlalchemy_event.remove(engine, "before_cursor_execute", observe)
    assert data["outcome_count"] == 514 and len(data["outcomes"]) == 512
    assert data["completion"]["outcome_status"] == "unknown"
    assert data["completion"]["result_available"] is True
    assert data["completion"]["completed_rounds"] == 0
    assert set(statements) == {"SELECT"}
