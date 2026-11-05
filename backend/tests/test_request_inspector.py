"""Canonical per-request execution inspector contracts on disposable SQLite."""

from datetime import datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event as sqlalchemy_event

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.collaboration import (
    RoomAgentRun, RoomEvent, RoomMembership, RoomReadState, RoomTrigger, ToolAction,
)
from app.models.models import Agent, Conversation, Project, User
from app.services.request_inspector import _failure_summary


@pytest.fixture
def inspector_client(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "inspector-token")
    app = create_app(database_url=sqlite_url)
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            owner = User(email="inspector-owner@test.invalid", username="owner", hashed_password="x")
            other = User(email="inspector-other@test.invalid", username="other", hashed_password="x")
            db.add_all([owner, other])
            db.commit()
            ids = {"owner": owner.id, "other": other.id}
        yield client, ids


def headers(client, owner_id):
    return {
        "X-Gamja-Session": client.app.state.local_session_token,
        "Authorization": f"Bearer {create_access_token({'sub': owner_id})}",
    }


def room(db, owner_id, label, *, active=True, project_active=True):
    project = Project(user_id=owner_id, name=f"{label} project", is_active=project_active)
    db.add(project)
    db.flush()
    value = Conversation(
        user_id=owner_id, project_id=project.id, title=f"{label} room", is_active=active,
        created_at=datetime(2026, 10, 5, 10, 0, 0),
    )
    db.add(value)
    db.flush()
    return value


def event(db, target, seq, actor_type, kind, payload, *, actor_id=None, name=None):
    if actor_type == "user" and actor_id is None:
        actor_id = target.user_id
    value = RoomEvent(
        event_id=str(uuid4()), room_id=target.id, seq=seq, actor_type=actor_type,
        actor_id=actor_id, actor_name_snapshot=name or actor_type.title(), kind=kind,
        payload=payload, created_at=datetime(2026, 10, 5, 10, 0, 0) + timedelta(seconds=seq),
    )
    db.add(value)
    db.flush()
    return value


def get(client, owner_id, room_id, request_id):
    return client.get(
        f"/api/v1/conversations/{room_id}/requests/{request_id}",
        headers=headers(client, owner_id),
    )


def test_quota_exhaustion_is_a_closed_connection_failure():
    assert _failure_summary({
        "code": "quota_exhausted", "message": "raw vendor detail",
    }) == "failure_connection"


def test_inspector_uses_exact_request_snapshots_and_safe_associations(inspector_client):
    client, ids = inspector_client
    with client.app.state.session_factory() as db:
        target = room(db, ids["owner"], "Archived", active=False, project_active=False)
        foreign_room = room(db, ids["other"], "Foreign")
        first = Agent(user_id=ids["owner"], name="CURRENT NAME MUST NOT REPLACE SNAPSHOT",
                      description="PRIVATE DESCRIPTION", system_prompt="PRIVATE PROMPT")
        second = Agent(user_id=ids["owner"], name="Second current", description="d", system_prompt="p")
        foreign = Agent(user_id=ids["other"], name="FOREIGN AGENT", description="d", system_prompt="p")
        db.add_all([first, second, foreign])
        db.flush()
        request = event(db, target, 1, "user", "message",
                        {"content": "  Analyze   this request with private long text  "}, name="Owner snapshot")
        overlap = event(db, target, 2, "user", "message", {"content": "overlapping request"})
        db.add_all([
            RoomTrigger(
                event_id=request.event_id, room_id=target.id, state="done",
                selected_agents=[first.id, second.id], max_rounds=3, max_tool_calls=12,
                max_executor_seconds=90, current_round=2,
            ),
            RoomTrigger(
                event_id=overlap.event_id, room_id=target.id, state="running",
                selected_agents=[first.id], max_rounds=1, max_tool_calls=1,
                max_executor_seconds=10, current_round=1,
            ),
        ])
        first_reply = event(
            db, target, 3, "agent", "message",
            {"content": "PRIVATE RESULT", "trigger_event_id": request.event_id, "round": 1,
             "model_use": {"connection_id": 7, "model_id": "historical-model",
                           "provider_kind": "compatible", "watermark_seq": 2,
                           "api_key": "SECRET MODEL EXTRA"}},
            actor_id=first.id, name="First historical",
        )
        failure = event(
            db, target, 4, "agent", "failure",
            {"code": "tool_effect_uncertain", "message": "PRIVATE FAILURE",
             "diagnostic": {"provider_body": "SECRET BODY"},
             "trigger_event_id": request.event_id, "round": 2},
            actor_id=second.id, name="Second historical",
        )
        event(db, target, 5, "agent", "message",
              {"content": "OVERLAP PRIVATE", "trigger_event_id": overlap.event_id, "round": 1,
               "model_use": {"connection_id": 99, "model_id": "wrong-current",
                             "provider_kind": "other", "watermark_seq": 5}},
              actor_id=first.id, name="Overlap snapshot")
        # Same request ID but a foreign Agent is never an owned association.
        event(db, target, 6, "agent", "message",
              {"content": "FOREIGN PRIVATE", "trigger_event_id": request.event_id, "round": 1},
              actor_id=foreign.id, name="FOREIGN SNAPSHOT")

        waiting = str(uuid4())
        completed = str(uuid4())
        db.add_all([
            ToolAction(
                action_id=waiting, room_id=target.id, agent_id=second.id,
                trigger_event_id=request.event_id, family="write_file",
                arguments={"content": "SECRET ARGUMENT"}, target="/private/path",
                expected_effect="SECRET EFFECT", state="waiting",
                requested_at=datetime(2026, 10, 5, 10, 0, 7),
            ),
            ToolAction(
                action_id=completed, room_id=target.id, agent_id=first.id,
                trigger_event_id=request.event_id, family="browser_navigate",
                arguments={"url": "https://secret.invalid"}, observation={"private": "SECRET"},
                state="completed", requested_at=datetime(2026, 10, 5, 10, 0, 8),
                started_at=datetime(2026, 10, 5, 10, 0, 9),
                completed_at=datetime(2026, 10, 5, 10, 0, 10),
            ),
            ToolAction(
                action_id=str(uuid4()), room_id=target.id, agent_id=first.id,
                trigger_event_id=overlap.event_id, family="read_file", arguments={}, state="completed",
            ),
            ToolAction(
                action_id=str(uuid4()), room_id=foreign_room.id, agent_id=foreign.id,
                trigger_event_id=request.event_id, family="screenshot", arguments={}, state="waiting",
            ),
            ToolAction(
                action_id="not-a-canonical-id", room_id=target.id, agent_id=first.id,
                trigger_event_id=request.event_id, family="read_file",
                arguments={"secret": "MALFORMED ACTION"}, state="waiting",
            ),
        ])
        result = event(db, target, 7, "tool", "tool_result",
                       {"state": "completed", "text": "PRIVATE TOOL RESULT",
                        "action_id": completed, "trigger_event_id": request.event_id})
        # The same action ID on another request must not make the exact association ambiguous.
        event(db, target, 8, "tool", "tool_result",
              {"state": "completed", "text": "OVERLAP TOOL RESULT",
               "action_id": completed, "trigger_event_id": overlap.event_id})
        db.add(RoomEvent(
            event_id="not-a-canonical-event", room_id=target.id, seq=9,
            actor_type="agent", actor_id=first.id, actor_name_snapshot="Malformed snapshot",
            kind="message", payload={"content": "MALFORMED OUTCOME",
                                     "trigger_event_id": request.event_id, "round": 1},
            created_at=datetime(2026, 10, 5, 10, 0, 9),
        ))
        db.add_all([
            RoomMembership(room_id=target.id, agent_id=first.id, is_active=True),
            RoomMembership(room_id=target.id, agent_id=second.id, is_active=True),
        ])
        db.flush()
        db.add_all([
            RoomAgentRun(room_id=target.id, agent_id=first.id, state="running", paused=False,
                         trigger_event_id=overlap.event_id, generation=3, checkpoint={}, memory={},
                         pending_task={"private": "HIDDEN"}, active_tool={"private": "HIDDEN"}),
            RoomAgentRun(room_id=target.id, agent_id=second.id, state="running", paused=True,
                         trigger_event_id=request.event_id, generation=4, checkpoint={}, memory={},
                         pending_task={}, active_tool={}),
            RoomReadState(owner_id=ids["owner"], room_id=target.id, last_read_seq=0,
                          updated_at=datetime(2026, 10, 5, 10, 0, 0)),
        ])
        db.commit()
        expected = {
            "room": target.id, "request": request.event_id, "overlap": overlap.event_id,
            "first": first.id, "second": second.id, "reply": first_reply.event_id,
            "failure": failure.event_id, "waiting": waiting, "completed": completed,
            "result": result.event_id,
        }

    response = get(client, ids["owner"], expected["room"], expected["request"])
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["version"] == 1 and data["read_only"] is True
    assert data["request"]["preview"] == "Analyze this request with private long text"
    assert data["request"]["href"] == f"/chat/{expected['room']}?seq=1"
    assert data["execution"] == {
        "phase": "completed", "selected_agents": [
            {"agent_id": expected["first"], "name_snapshot": "First historical", "live_state": None},
            {"agent_id": expected["second"], "name_snapshot": "Second historical", "live_state": "paused"},
        ],
        "selected_agent_count": 2,
        "policy": {"max_rounds": 3, "max_tool_calls": 12, "max_executor_seconds": 90},
        "current_round": 2,
    }
    assert [(item["event_id"], item["kind"], item["round"]) for item in data["outcomes"]] == [
        (expected["reply"], "message", 1), (expected["failure"], "failure", 2),
    ]
    assert data["outcomes"][0]["model_use"] == {
        "connection_id": 7, "model_id": "historical-model",
        "provider_kind": "compatible", "watermark_seq": 2,
    }
    assert data["outcomes"][1]["summary_code"] == "failure_uncertain"
    assert data["outcomes"][1]["href"] == f"/chat/{expected['room']}?seq=4"
    assert [item["action_id"] for item in data["actions"]] == [expected["waiting"], expected["completed"]]
    assert data["actions"][0]["phase"] == "approval"
    assert data["actions"][0]["href"] == f"/chat/{expected['room']}?action={expected['waiting']}"
    assert data["actions"][1]["result_event_id"] == expected["result"]
    assert data["actions"][1]["href"] == f"/chat/{expected['room']}?seq=7"
    assert data["outcome_count"] == 2 and data["action_count"] == 2
    assert not data["outcomes_truncated"] and not data["actions_truncated"]
    rendered = str(data)
    for secret in (
        "PRIVATE RESULT", "PRIVATE FAILURE", "SECRET BODY", "OVERLAP PRIVATE",
        "FOREIGN", "SECRET ARGUMENT", "/private/path", "SECRET EFFECT",
        "secret.invalid", "PRIVATE TOOL RESULT", "HIDDEN", "wrong-current",
        "OVERLAP TOOL RESULT", "MALFORMED ACTION", "MALFORMED OUTCOME",
        "CURRENT NAME MUST NOT REPLACE SNAPSHOT",
    ):
        assert secret not in rendered
    with client.app.state.session_factory() as db:
        assert db.get(RoomReadState, (ids["owner"], expected["room"])).last_read_seq == 0


def test_waiting_action_link_targets_its_owned_execution_room(inspector_client):
    client, ids = inspector_client
    with client.app.state.session_factory() as db:
        root_room = room(db, ids["owner"], "Root")
        execution_room = room(db, ids["owner"], "Execution")
        foreign_room = room(db, ids["other"], "Foreign")
        agent = Agent(user_id=ids["owner"], name="Worker", description="d", system_prompt="p")
        db.add(agent)
        db.flush()
        request = event(db, root_room, 1, "user", "message", {"content": "synthetic"})
        db.add(RoomTrigger(
            event_id=request.event_id, room_id=root_room.id, state="done", selected_agents=[agent.id],
            max_rounds=1, max_tool_calls=1, max_executor_seconds=60, current_round=1,
        ))
        owned_id, foreign_id = str(uuid4()), str(uuid4())
        for action_id, action_room in ((owned_id, execution_room), (foreign_id, foreign_room)):
            db.add(ToolAction(
                action_id=action_id, room_id=action_room.id, agent_id=agent.id,
                trigger_event_id=request.event_id, family="browser_navigate", arguments={},
                execution_binding={"root_request_event_id": request.event_id}, state="waiting",
            ))
        db.commit()
        root_id, request_id, execution_id = root_room.id, request.event_id, execution_room.id
    response = get(client, ids["owner"], root_id, request_id)
    assert response.status_code == 200, response.text
    actions = response.json()["actions"]
    assert [action["action_id"] for action in actions] == [owned_id]
    assert actions[0]["href"] == f"/chat/{execution_id}?action={owned_id}"
    assert get(client, ids["other"], root_id, request_id).status_code == 404


def test_inspector_legacy_unknown_owner_scope_bounds_and_fixed_query_count(inspector_client):
    client, ids = inspector_client
    with client.app.state.session_factory() as db:
        target = room(db, ids["owner"], "Legacy")
        other_room = room(db, ids["other"], "Other")
        worker = Agent(user_id=ids["owner"], name="Worker", description="d", system_prompt="p")
        db.add(worker)
        db.flush()
        legacy = event(db, target, 1, "user", "message", {"content": "legacy"})
        bounded = event(db, target, 3, "user", "message", {"content": "bounded"})
        small = event(db, target, 5, "user", "message", {"content": "small"})
        foreign = event(db, other_room, 1, "user", "message", {"content": "foreign"})
        malformed_owner = event(
            db, target, 4, "user", "message", {"content": "wrong actor"},
            actor_id=ids["other"], name="Other",
        )
        # Exact ID with malformed/unowned associations remains unknown and does not leak.
        event(db, target, 2, "agent", "failure",
              {"code": "provider_unavailable", "message": "PRIVATE", "trigger_event_id": legacy.event_id,
               "round": "not-an-integer"}, actor_id=worker.id, name="Worker")
        db.add(RoomTrigger(
            event_id=bounded.event_id, room_id=target.id, state="done", selected_agents=[worker.id],
            max_rounds=1, max_tool_calls=64, max_executor_seconds=3600, current_round=1,
        ))
        db.add(RoomTrigger(
            event_id=small.event_id, room_id=target.id, state="done", selected_agents=[worker.id],
            max_rounds=1, max_tool_calls=64, max_executor_seconds=3600, current_round=1,
        ))
        db.add(ToolAction(
            action_id=str(uuid4()), room_id=target.id, agent_id=worker.id,
            trigger_event_id=small.event_id, family="future_private_family",
            arguments={"secret": "PRIVATE-small"}, state="completed",
        ))
        event(db, target, 6, "agent", "message",
              {"content": "PRIVATE OUTCOME small", "trigger_event_id": small.event_id, "round": 1},
              actor_id=worker.id, name="Worker at request")
        for number in range(513):
            db.add(ToolAction(
                action_id=str(uuid4()), room_id=target.id, agent_id=worker.id,
                trigger_event_id=bounded.event_id, family="future_private_family",
                arguments={"secret": f"PRIVATE-{number}"}, state="completed",
                requested_at=datetime(2026, 10, 5, 11, 0, 0) + timedelta(microseconds=number),
            ))
            db.add(RoomEvent(
                event_id=str(uuid4()), room_id=target.id, seq=1000 + number,
                actor_type="agent", actor_id=worker.id,
                actor_name_snapshot="Worker at request", kind="message",
                payload={"content": f"PRIVATE OUTCOME {number}",
                         "trigger_event_id": bounded.event_id, "round": 1},
                created_at=datetime(2026, 10, 5, 11, 1, 0) + timedelta(microseconds=number),
            ))
        db.commit()
        room_id, legacy_id, bounded_id, small_id, worker_id = target.id, legacy.event_id, bounded.event_id, small.event_id, worker.id
        foreign_id, other_room_id = foreign.event_id, other_room.id
        malformed_owner_id = malformed_owner.event_id

    engine = client.app.state.session_factory.kw["bind"]
    statements = []
    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)
    sqlalchemy_event.listen(engine, "before_cursor_execute", observe)
    try:
        small_response = get(client, ids["owner"], room_id, small_id)
        small_selects = len(statements)
        statements.clear()
        response = get(client, ids["owner"], room_id, bounded_id)
    finally:
        sqlalchemy_event.remove(engine, "before_cursor_execute", observe)
    assert response.status_code == 200, response.text
    assert small_response.status_code == 200, small_response.text
    small_data = small_response.json()
    assert len(small_data["outcomes"]) == small_data["outcome_count"] == 1
    assert len(small_data["actions"]) == small_data["action_count"] == 1
    assert not small_data["outcomes_truncated"] and not small_data["actions_truncated"]
    assert "PRIVATE-small" not in str(small_data) and "PRIVATE OUTCOME" not in str(small_data)
    data = response.json()
    assert data["execution"] == {
        "phase": "completed", "selected_agents": [
            {"agent_id": worker_id, "name_snapshot": "Worker at request", "live_state": None},
        ], "selected_agent_count": 1,
        "policy": {"max_rounds": 1, "max_tool_calls": 64, "max_executor_seconds": 3600},
        "current_round": 1,
    }
    assert len(data["outcomes"]) == 512 and data["outcome_count"] == 513
    assert data["outcomes_truncated"] is True
    assert len(data["actions"]) == 512 and data["action_count"] == 513 and data["actions_truncated"] is True
    assert all(item["summary_code"] == "action_other" for item in data["actions"])
    assert small_selects == len(statements) <= 14

    legacy_data = get(client, ids["owner"], room_id, legacy_id).json()
    assert legacy_data["execution"] == {
        "phase": "unknown", "selected_agents": [], "selected_agent_count": 0,
        "policy": None, "current_round": None,
    }
    assert legacy_data["outcomes"] == [] and legacy_data["actions"] == []
    assert get(client, ids["other"], room_id, legacy_id).status_code == 404
    assert get(client, ids["owner"], other_room_id, foreign_id).status_code == 404
    assert get(client, ids["owner"], room_id, foreign_id).status_code == 404
    assert get(client, ids["owner"], room_id, malformed_owner_id).status_code == 404
    assert get(client, ids["owner"], room_id, "NOT-A-UUID").status_code == 422
    assert "PRIVATE-" not in str(data) and "PRIVATE OUTCOME" not in str(data)
