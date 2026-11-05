from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from time import sleep
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.collaboration import (
    ProviderConnection, RequestFinalizeRecord, RoomAgentRun, RoomEvent, RoomMembership,
    RoomTrigger, ToolAction,
)
from app.models.models import Agent, Conversation, Project, User
from app.services.providers import ProviderResult
from app.services.context import provider_text_size
from app.services.request_finalize import execute_finalize


@pytest.fixture
def finalize_client(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "finalize-test-token")
    app = create_app(database_url=sqlite_url)
    captured = []

    async def provider(request):
        captured.append(request)
        return ProviderResult(text="final synthetic result")

    with TestClient(app) as client:
        app.state.scheduler.provider = provider
        with app.state.session_factory() as db:
            owner = User(email="final@test.invalid", username="owner", hashed_password="x")
            other = User(email="other-final@test.invalid", username="other", hashed_password="x")
            db.add_all([owner, other]); db.flush()
            project = Project(user_id=owner.id, name="Final project")
            other_project = Project(user_id=other.id, name="Other")
            db.add_all([project, other_project]); db.flush()
            room = Conversation(user_id=owner.id, project_id=project.id, title="Room")
            other_room = Conversation(user_id=other.id, project_id=other_project.id, title="Other")
            db.add_all([room, other_room]); db.flush()
            connection = ProviderConnection(
                user_id=owner.id, kind="compatible", name="Fake",
                base_url="http://127.0.0.1:1/v1", default_model_id="",
                auth_required=False, enabled=True, capabilities={}, readiness_revision=3,
            )
            db.add(connection); db.flush()
            proposer = Agent(user_id=owner.id, name="Proposer", description="d",
                             system_prompt="FINAL SYSTEM", connection_id=connection.id,
                             model_id="model-p", temperature="0.2", max_tokens=500)
            reviewer = Agent(user_id=owner.id, name="Reviewer", description="d",
                             system_prompt="peer", connection_id=connection.id,
                             model_id="model-r")
            db.add_all([proposer, reviewer]); db.flush()
            db.add_all([
                RoomMembership(room_id=room.id, agent_id=proposer.id),
                RoomMembership(room_id=room.id, agent_id=reviewer.id),
            ]); db.flush()
            db.add_all([
                RoomAgentRun(room_id=room.id, agent_id=proposer.id,
                             memory={"PRIVATE_MEMORY": "must not leak"}),
                RoomAgentRun(room_id=room.id, agent_id=reviewer.id),
            ])
            request_id, answer_id, failure_id, unrelated_id = (str(uuid4()) for _ in range(4))
            db.add_all([
                RoomEvent(event_id=request_id, room_id=room.id, seq=1, actor_type="user",
                          actor_id=owner.id, actor_name_snapshot="owner", kind="message",
                          payload={"content": "SOURCE_MARKER decide"}, created_at=datetime(2026, 10, 6)),
                RoomEvent(event_id=answer_id, room_id=room.id, seq=2, actor_type="agent",
                          actor_id=proposer.id, actor_name_snapshot="Proposer old", kind="message",
                          payload={"content": "PEER_RESULT_MARKER useful", "trigger_event_id": request_id,
                                   "round": 1, "model_use": {"connection_id": connection.id,
                                   "model_id": "old-p", "provider_kind": "openai", "watermark_seq": 1}},
                          created_at=datetime(2026, 10, 6, 0, 0, 1)),
                RoomEvent(event_id=failure_id, room_id=room.id, seq=3, actor_type="agent",
                          actor_id=reviewer.id, actor_name_snapshot="Reviewer old", kind="failure",
                          payload={"code": "provider_unavailable", "message": "PRIVATE_FAILURE",
                                   "trigger_event_id": request_id, "round": 1},
                          created_at=datetime(2026, 10, 6, 0, 0, 2)),
                RoomEvent(event_id=unrelated_id, room_id=room.id, seq=4, actor_type="user",
                          actor_id=owner.id, actor_name_snapshot="owner", kind="message",
                          payload={"content": "UNRELATED_SAME_ROOM_MARKER"},
                          created_at=datetime(2026, 10, 6, 0, 0, 3)),
                RoomEvent(event_id=str(uuid4()), room_id=other_room.id, seq=1, actor_type="user",
                          actor_id=other.id, actor_name_snapshot="other", kind="message",
                          payload={"content": "CROSS_ROOM_MARKER"},
                          created_at=datetime(2026, 10, 6, 0, 0, 3)),
            ])
            db.add(RoomTrigger(event_id=request_id, room_id=room.id, state="interrupted",
                               selected_agents=[proposer.id, reviewer.id], max_rounds=2,
                               max_tool_calls=8, max_executor_seconds=60, current_round=1))
            db.commit()
            ids = {"owner": owner.id, "other": other.id, "room": room.id,
                   "other_room": other_room.id, "proposer": proposer.id,
                   "reviewer": reviewer.id, "connection": connection.id,
                   "request": request_id, "answer": answer_id, "failure": failure_id,
                   "captured": captured}
        yield client, ids


def auth(client, owner):
    return {"X-Gamja-Session": client.app.state.local_session_token,
            "Authorization": f"Bearer {create_access_token({'sub': owner})}"}


def preview(client, ids, agent=None, owner=None):
    return client.post(
        f"/api/v1/conversations/{ids['room']}/requests/{ids['request']}/finalize/preview",
        json={"agent_id": agent or ids["proposer"]},
        headers=auth(client, owner or ids["owner"]),
    )


def execute(client, ids, plan, key="final-key", agent=None):
    return client.post(
        f"/api/v1/conversations/{ids['room']}/requests/{ids['request']}/finalize",
        json={"agent_id": agent or ids["proposer"], "plan_hash": plan},
        headers={**auth(client, ids["owner"]), "Idempotency-Key": key},
    )


def test_finalize_preview_execute_replay_and_source_scoped_dispatch(finalize_client):
    client, ids = finalize_client
    response = preview(client, ids)
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["eligible"] is True and len(plan["plan_hash"]) == 64
    assert plan["source_completion"]["state"] == "interrupted"
    assert plan["source_completion"]["outcome_status"] == "partial"
    assert plan["source_coverage"]["source_outcome_event_ids"] == [ids["answer"], ids["failure"]]
    assert plan["source_coverage"]["included_message_event_ids"] == [ids["answer"]]
    assert plan["excluded_agents"] == [{"agent_id": ids["reviewer"],
                                         "name_snapshot": "Reviewer old", "reason": "failed"}]
    assert "PRIVATE_FAILURE" not in str(plan)

    created = execute(client, ids, plan["plan_hash"])
    assert created.status_code == 201, created.text
    result = created.json()
    assert result["replayed"] is False and result["source_outcome_event_ids"] == [ids["answer"], ids["failure"]]
    with client.app.state.session_factory() as db:
        event = db.get(RoomEvent, result["finalize_event_id"])
        trigger = db.get(RoomTrigger, result["finalize_event_id"])
        assert event.payload["finalize"]["source_request_event_id"] == ids["request"]
        assert event.payload["finalize"]["source_status"] == {
            "outcome_status": "partial", "unanswered_turn_count": 0,
            "omitted_message_count": 0, "truncated_event_count": 0,
        }
        assert trigger.selected_agents == [ids["proposer"]]
        assert (trigger.max_rounds, trigger.max_tool_calls, trigger.max_executor_seconds) == (1, 0, 60)
        assert db.query(RequestFinalizeRecord).count() == 1

    for _ in range(200):
        if ids["captured"]:
            break
        sleep(.01)
    assert len(ids["captured"]) == 1
    request = ids["captured"][0]
    serialized = str(request.events) + str(request.memory) + str(request.tools)
    assert "SOURCE_MARKER" in serialized and "PEER_RESULT_MARKER" in serialized
    assert "UNRELATED_SAME_ROOM_MARKER" not in serialized
    assert "CROSS_ROOM_MARKER" not in serialized
    assert "PRIVATE_MEMORY" not in serialized and "PRIVATE_FAILURE" not in serialized
    assert request.agent_id == ids["proposer"] and request.tools == [] and request.memory is None
    assert provider_text_size(request) == plan["source_coverage"]["used_chars"]
    assert "1 failed turns" in str(request.events)
    assert "No independent test-execution evidence" in str(request.events)
    with client.app.state.session_factory() as db:
        assert "Source limits:" not in db.get(RoomEvent, result["finalize_event_id"]).payload["content"]

    response_event = None
    for _ in range(200):
        with client.app.state.session_factory() as db:
            response_event = db.query(RoomEvent).filter_by(
                room_id=ids["room"], actor_type="agent", kind="message",
            ).filter(RoomEvent.payload["trigger_event_id"].as_string()
                     == result["finalize_event_id"]).first()
        if response_event is not None:
            break
        sleep(.01)
    assert response_event is not None
    assert response_event.payload["model_use"] == {
        "connection_id": ids["connection"], "model_id": "model-p",
        "provider_kind": "compatible", "watermark_seq": result["finalize_seq"],
    }

    with client.app.state.session_factory() as db:
        db.get(ProviderConnection, ids["connection"]).base_url = "http://localhost:2/v1"
        db.commit()
    replay = execute(client, ids, "0" * 64)
    assert replay.status_code == 201
    assert replay.json() == {**result, "replayed": True}
    conflict = execute(client, ids, "0" * 64, agent=ids["reviewer"])
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == {"code": "finalize_idempotency_conflict"}


def test_finalize_dispatch_keeps_requested_deliverable_and_original_constraints(finalize_client):
    from copy import deepcopy
    from app.services.request_finalize import FINALIZE_CONTENT

    client, ids = finalize_client
    requirement = "Return complete label(value) code: string only, trim then NFC, at most 12 code points; empty is allowed."
    artifact = "```javascript\nfunction label(value) {\n  if (typeof value !== 'string') throw new TypeError('string only');\n  return Array.from(value.trim().normalize('NFC')).slice(0, 12).join('');\n}\n```"
    with client.app.state.session_factory() as db:
        source = db.get(RoomEvent, ids["request"])
        answer = db.get(RoomEvent, ids["answer"])
        source.payload = {**source.payload, "content": requirement}
        answer.payload = {**answer.payload, "content": artifact}
        db.commit()
        original_payloads = deepcopy([source.payload, answer.payload])

    plan = preview(client, ids).json()
    assert plan["eligible"]
    created = execute(client, ids, plan["plan_hash"]).json()
    for _ in range(200):
        if ids["captured"]:
            break
        sleep(.01)
    assert len(ids["captured"]) == 1
    request = ids["captured"][0]
    by_id = {event["event_id"]: event for event in request.events}
    assert by_id[ids["request"]]["payload"]["content"] == requirement
    assert by_id[ids["answer"]]["payload"]["content"] == artifact
    guidance = by_id[created["finalize_event_id"]]["payload"]["content"]
    assert "include the complete usable deliverable" in guidance
    assert "Preserve its constraints, bounds, and exceptions" in guidance
    assert "Do not invent missing parts of shortened or omitted deliverables" in guidance
    assert "No independent test-execution evidence" in guidance
    assert request.tools == [] and request.memory is None
    assert provider_text_size(request) == plan["source_coverage"]["used_chars"] <= 32000
    with client.app.state.session_factory() as db:
        assert [db.get(RoomEvent, key).payload for key in (ids["request"], ids["answer"])] == original_payloads
        assert db.get(RoomEvent, created["finalize_event_id"]).payload["content"] == FINALIZE_CONTENT


@pytest.mark.parametrize(("mutation", "reason"), [
    ("running", "source_not_terminal"),
    ("all_failed", "source_all_failed"),
    ("all_passed", "source_all_passed"),
    ("no_outcomes", "source_no_useful_result"),
    ("malformed", "source_outcome_unknown"),
    ("waiting_action", "action_unsettled"),
    ("uncertain_action", "action_uncertain"),
    ("mutating_action", "completed_effect"),
    ("busy", "room_busy"),
    ("paused", "agent_unavailable"),
])
def test_finalize_blocks_nonterminal_or_unsafe_sources_without_writes(finalize_client, mutation, reason):
    client, ids = finalize_client
    with client.app.state.session_factory() as db:
        if mutation == "running":
            db.get(RoomTrigger, ids["request"]).state = "running"
        elif mutation in {"all_failed", "all_passed"}:
            event = db.get(RoomEvent, ids["answer"])
            event.kind = "failure" if mutation == "all_failed" else "activity"
            event.payload = ({"code": "provider_unavailable", "trigger_event_id": ids["request"], "round": 1}
                             if mutation == "all_failed" else
                             {"status": "passed", "trigger_event_id": ids["request"], "round": 1})
            if mutation == "all_passed":
                peer = db.get(RoomEvent, ids["failure"])
                peer.kind = "activity"
                peer.payload = {"status": "passed", "trigger_event_id": ids["request"], "round": 1}
        elif mutation == "no_outcomes":
            db.delete(db.get(RoomEvent, ids["answer"]))
            db.delete(db.get(RoomEvent, ids["failure"]))
        elif mutation == "malformed":
            event = db.get(RoomEvent, ids["answer"])
            event.payload = {**event.payload, "round": "one"}
        elif mutation.endswith("action"):
            state = {"waiting_action": "waiting", "uncertain_action": "started",
                     "mutating_action": "completed"}[mutation]
            db.add(ToolAction(action_id=str(uuid4()), room_id=ids["room"],
                              agent_id=ids["proposer"], trigger_event_id=ids["request"],
                              family="write_file", arguments={}, state=state))
        elif mutation == "busy":
            event_id = str(uuid4())
            db.add(RoomEvent(event_id=event_id, room_id=ids["room"], seq=5,
                             actor_type="user", actor_id=ids["owner"], actor_name_snapshot="owner",
                             kind="message", payload={"content": "busy"}, created_at=datetime(2026, 10, 6)))
            db.add(RoomTrigger(event_id=event_id, room_id=ids["room"], state="pending",
                               selected_agents=[ids["reviewer"]], max_rounds=1,
                               max_tool_calls=0, max_executor_seconds=60))
        elif mutation == "paused":
            db.get(RoomAgentRun, (ids["room"], ids["proposer"])).paused = True
        db.commit()
    blocked = preview(client, ids)
    assert blocked.status_code == 200 and blocked.json()["blocked_reason"] == reason
    attempted = execute(client, ids, "0" * 64, key="blocked")
    assert attempted.status_code == 409
    assert attempted.json()["detail"] == {"code": "finalize_blocked", "reason": reason}
    with client.app.state.session_factory() as db:
        assert db.query(RequestFinalizeRecord).count() == 0


def test_finalize_owner_scope_and_stale_plan(finalize_client):
    client, ids = finalize_client
    assert preview(client, ids, owner=ids["other"]).status_code == 404
    plan = preview(client, ids).json()["plan_hash"]
    with client.app.state.session_factory() as db:
        db.get(ProviderConnection, ids["connection"]).readiness_revision += 1
        db.commit()
    stale = execute(client, ids, plan, key="stale")
    assert stale.status_code == 409
    assert stale.json()["detail"] == {"code": "finalize_plan_stale"}


def test_finalize_uses_registry_validation_for_typed_connection_without_reading_secret(
        finalize_client):
    client, ids = finalize_client
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, ids["connection"])
        row.auth_required = True
        row.secret_ref = "mock-secret-reference"
        row.base_url = "https://openrouter.ai/api/v1"
        row.provider_id = "openrouter"
        row.protocol_id = "openai_chat_completions"
        row.auth_method_id = "api_key"
        row.public_config_version = 1
        row.public_config = {}
        row.auth_grant_revision = 2
        row.credential_state = "present"
        db.commit()
    valid = preview(client, ids)
    assert valid.status_code == 200 and valid.json()["eligible"] is True
    assert valid.json()["current_model"]["provider_kind"] == "compatible"

    with client.app.state.session_factory() as db:
        db.get(ProviderConnection, ids["connection"]).protocol_id = "anthropic_messages"
        db.commit()
    blocked = preview(client, ids)
    assert blocked.status_code == 200
    assert blocked.json()["blocked_reason"] == "model_unavailable"
    with client.app.state.session_factory() as db:
        assert db.query(RequestFinalizeRecord).count() == 0


def test_finalize_rejects_inconsistent_local_none_without_writes_or_provider_call(
        finalize_client):
    client, ids = finalize_client
    secret_reads = []

    class ForbiddenSecrets:
        def get_no_ui(self, reference):
            secret_reads.append(reference)
            raise AssertionError('finalize preflight must not read a credential')

    client.app.state.secret_store = ForbiddenSecrets()
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, ids["connection"])
        row.auth_required = True
        row.secret_ref = "mock-secret-reference"
        row.base_url = "http://localhost:11434/v1"
        row.provider_id = "ollama"
        row.protocol_id = "openai_chat_completions"
        row.auth_method_id = "local_none"
        row.public_config_version = 1
        row.public_config = {}
        row.auth_grant_revision = 0
        row.credential_state = "not_required"
        db.commit()
        before = (db.query(RoomEvent).count(), db.query(RoomTrigger).count())

    blocked = preview(client, ids)
    assert blocked.status_code == 200
    assert blocked.json()["blocked_reason"] == "model_unavailable"
    attempted = execute(client, ids, "0" * 64, key="invalid-local-none")
    assert attempted.status_code == 409
    assert attempted.json()["detail"] == {
        "code": "finalize_blocked", "reason": "model_unavailable"}
    with client.app.state.session_factory() as db:
        assert db.query(RequestFinalizeRecord).count() == 0
        assert (db.query(RoomEvent).count(), db.query(RoomTrigger).count()) == before
    assert ids["captured"] == []
    assert secret_reads == []


def test_finalize_agent_selection_owner_membership_and_action_safety(finalize_client):
    client, ids = finalize_client
    with client.app.state.session_factory() as db:
        third = Agent(user_id=ids["owner"], name="Not selected", description="d",
                      system_prompt="x", connection_id=ids["connection"], model_id="model-x")
        foreign = Agent(user_id=ids["other"], name="Foreign", description="d",
                        system_prompt="x", model_id="model-x")
        db.add_all([third, foreign]); db.flush()
        db.add(RoomMembership(room_id=ids["room"], agent_id=third.id)); db.flush()
        db.add(RoomAgentRun(room_id=ids["room"], agent_id=third.id))
        third_id, foreign_id = third.id, foreign.id
        db.add(ToolAction(action_id=str(uuid4()), room_id=ids["room"],
                          agent_id=ids["proposer"], trigger_event_id=ids["request"],
                          family="read_file", arguments={}, state="completed"))
        db.commit()
    assert preview(client, ids, agent=third_id).json()["blocked_reason"] == "agent_not_selected"
    assert preview(client, ids, agent=foreign_id).status_code == 404
    assert preview(client, ids).json()["eligible"] is True
    with client.app.state.session_factory() as db:
        approved_id = str(uuid4())
        db.add(ToolAction(action_id=approved_id, room_id=ids["room"],
                          agent_id=ids["proposer"], trigger_event_id=ids["request"],
                          family="read_file", arguments={}, state="approved"))
        db.commit()
    assert preview(client, ids).json()["blocked_reason"] == "action_unsettled"
    with client.app.state.session_factory() as db:
        db.delete(db.get(ToolAction, approved_id))
        db.get(RoomMembership, (ids["room"], ids["proposer"])).is_active = False
        db.commit()
    assert preview(client, ids).json()["blocked_reason"] == "agent_unavailable"


@pytest.mark.parametrize("mutation", [
    "model", "disable", "endpoint", "high_water", "source", "action",
])
def test_finalize_changed_paid_intent_never_writes_from_stale_preview(finalize_client, mutation):
    client, ids = finalize_client
    plan = preview(client, ids).json()["plan_hash"]
    with client.app.state.session_factory() as db:
        if mutation == "model":
            db.get(Agent, ids["proposer"]).model_id = "model-new"
        elif mutation == "disable":
            db.get(ProviderConnection, ids["connection"]).enabled = False
        elif mutation == "endpoint":
            db.get(ProviderConnection, ids["connection"]).base_url = "http://localhost:2/v1"
        elif mutation == "high_water":
            db.add(RoomEvent(event_id=str(uuid4()), room_id=ids["room"], seq=5,
                             actor_type="system", actor_id=None, actor_name_snapshot="System",
                             kind="activity", payload={"status": "note"},
                             created_at=datetime(2026, 10, 6, 0, 0, 4)))
        elif mutation == "source":
            db.get(RoomTrigger, ids["request"]).state = "running"
        else:
            db.add(ToolAction(action_id=str(uuid4()), room_id=ids["room"],
                              agent_id=ids["proposer"], trigger_event_id=ids["request"],
                              family="read_file", arguments={}, state="completed"))
        db.commit()
    response = execute(client, ids, plan, key=f"stale-{mutation}")
    assert response.status_code == 409
    with client.app.state.session_factory() as db:
        assert db.query(RequestFinalizeRecord).count() == 0


def test_finalize_long_source_is_bounded_with_truthful_coverage(finalize_client):
    client, ids = finalize_client
    with client.app.state.session_factory() as db:
        db.get(RoomEvent, ids["request"]).payload = {"content": "S" * 13000}
        db.commit()
    response = preview(client, ids)
    assert response.status_code == 200 and response.json()["eligible"] is True
    truncation = next(item for item in response.json()["source_coverage"]["truncations"]
                      if item["event_id"] == ids["request"])
    assert truncation == {"event_id": ids["request"], "original_chars": 13000,
                          "included_chars": 12000, "omitted_chars": 1000}
    assert response.json()["source_coverage"]["used_chars"] <= 32000


@pytest.mark.parametrize("mutation", [
    "system", "model", "endpoint", "disable", "revision", "capabilities",
])
def test_finalize_postcommit_binding_change_fails_before_provider(finalize_client, mutation):
    client, ids = finalize_client
    plan = preview(client, ids).json()["plan_hash"]
    with client.app.state.session_factory() as db:
        result = execute_finalize(db, ids["owner"], ids["room"], ids["request"],
                                  ids["proposer"], plan, f"race-{mutation}")
    with client.app.state.session_factory() as db:
        if mutation == "system":
            db.get(Agent, ids["proposer"]).system_prompt = "CHANGED SYSTEM"
        elif mutation == "model":
            db.get(Agent, ids["proposer"]).model_id = "model-new"
        elif mutation == "endpoint":
            db.get(ProviderConnection, ids["connection"]).base_url = "http://localhost:2/v1"
        elif mutation == "disable":
            db.get(ProviderConnection, ids["connection"]).enabled = False
        elif mutation == "revision":
            db.get(ProviderConnection, ids["connection"]).readiness_revision += 1
        else:
            db.get(ProviderConnection, ids["connection"]).capabilities = {
                "declared": {"temperature": True}, "verified": {},
            }
        db.commit()

    async def schedule():
        await client.app.state.scheduler.schedule_user_event(
            ids["room"], result["finalize_event_id"], wait=True)

    client.portal.call(schedule)
    assert ids["captured"] == []
    with client.app.state.session_factory() as db:
        failure = db.query(RoomEvent).filter_by(room_id=ids["room"], kind="failure").filter(
            RoomEvent.payload["trigger_event_id"].as_string() == result["finalize_event_id"],
        ).one()
        assert failure.payload["code"] == "finalize_binding_changed"


def test_finalize_postcommit_source_change_fails_before_provider(finalize_client):
    client, ids = finalize_client
    plan = preview(client, ids).json()["plan_hash"]
    with client.app.state.session_factory() as db:
        result = execute_finalize(db, ids["owner"], ids["room"], ids["request"],
                                  ids["proposer"], plan, "race-source")
    with client.app.state.session_factory() as db:
        event = db.get(RoomEvent, ids["answer"])
        event.payload = {**event.payload, "content": "CHANGED AFTER COMMIT"}
        db.commit()

    async def schedule():
        await client.app.state.scheduler.schedule_user_event(
            ids["room"], result["finalize_event_id"], wait=True)

    client.portal.call(schedule)
    assert ids["captured"] == []
    with client.app.state.session_factory() as db:
        failure = db.query(RoomEvent).filter_by(room_id=ids["room"], kind="failure").filter(
            RoomEvent.payload["trigger_event_id"].as_string() == result["finalize_event_id"],
        ).one()
        assert failure.payload["code"] == "finalize_context_changed"


def test_finalize_preview_never_reads_locked_secret_or_calls_provider(finalize_client):
    client, ids = finalize_client

    class DeniedStore:
        def get_no_ui(self, _reference):
            raise AssertionError("preview opened credential store")

    client.app.state.secret_store = DeniedStore()
    with client.app.state.session_factory() as db:
        connection = db.get(ProviderConnection, ids["connection"])
        connection.auth_required = True
        connection.secret_ref = "opaque-reference"
        db.commit()
    response = preview(client, ids)
    assert response.status_code == 200 and response.json()["eligible"] is True
    assert ids["captured"] == []


def test_finalize_concurrent_execute_is_one_atomic_intent(finalize_client):
    client, ids = finalize_client
    plan = preview(client, ids).json()["plan_hash"]

    def send():
        response = execute(client, ids, plan, key="concurrent")
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _index: send(), range(2)))
    assert [status for status, _body in responses] == [201, 201]
    assert {body["finalize_event_id"] for _status, body in responses}.__len__() == 1
    assert sorted(body["replayed"] for _status, body in responses) == [False, True]
    with client.app.state.session_factory() as db:
        assert db.query(RequestFinalizeRecord).count() == 1
        event_id = responses[0][1]["finalize_event_id"]
        assert db.query(RoomTrigger).filter_by(event_id=event_id).count() == 1


def test_finalize_injected_record_failure_rolls_back_event_and_trigger(finalize_client, monkeypatch):
    client, ids = finalize_client
    plan = preview(client, ids).json()["plan_hash"]
    with client.app.state.session_factory() as db:
        before = (db.query(RoomEvent).count(), db.query(RoomTrigger).count())
        original_add = db.add

        def fail_record(instance, *args, **kwargs):
            if isinstance(instance, RequestFinalizeRecord):
                raise RuntimeError("synthetic record failure")
            return original_add(instance, *args, **kwargs)

        monkeypatch.setattr(db, "add", fail_record)
        with pytest.raises(RuntimeError, match="synthetic record failure"):
            execute_finalize(db, ids["owner"], ids["room"], ids["request"],
                             ids["proposer"], plan, "rollback")
    with client.app.state.session_factory() as db:
        assert (db.query(RoomEvent).count(), db.query(RoomTrigger).count()) == before
        assert db.query(RequestFinalizeRecord).count() == 0


@pytest.mark.parametrize("peer_kind,peer_payload,expected", [
    ("failure", {"message": "PRIVATE_FAILURE", "code": "provider_unavailable"}, "1 failed turns, 0 turns without an answer"),
    ("activity", {"status": "passed"}, "0 failed turns, 1 turns without an answer"),
])
def test_finalize_projection_preserves_peer_limits_without_inventing_test_evidence(peer_kind, peer_payload, expected):
    from copy import deepcopy
    from app.services.context_projection import project_finalize_context
    source = {"event_id": str(uuid4()), "room_id": 9, "seq": 1, "actor_type": "user",
              "actor_id": 1, "actor_name_snapshot": "Owner", "kind": "message", "payload": {"content": "Explain the code"}}
    active = {**source, "event_id": str(uuid4()), "seq": 4, "payload": {"content": "Summarize linked results"}}
    answer = {**source, "event_id": str(uuid4()), "seq": 2, "actor_type": "agent", "actor_id": 8,
              "actor_name_snapshot": "Proposer", "payload": {"content": "Tests all passed; no remaining uncertainty"}}
    peer = {**answer, "event_id": str(uuid4()), "seq": 3, "actor_id": 10,
            "actor_name_snapshot": "Reviewer", "kind": peer_kind, "payload": peer_payload}
    before = deepcopy([source, answer, peer, active])
    result = project_finalize_context(source_request=source, outcomes=[answer, peer], active_event=active, agent_id=8, system="system")
    provider_input = str(result.messages)
    assert expected in provider_input
    assert "passed means no answer, not a passed test" in provider_input
    assert "No independent test-execution evidence" in provider_input
    assert "PRIVATE_FAILURE" not in provider_input
    assert "Later retries and unrelated messages are outside" in provider_input
    assert "A recorded failure is not evidence that the Agent simply did not respond" in provider_input
    assert "Do not invent reviewer findings, actions, test results, limits" in provider_input
    assert "If the source does not establish a detail, say it is unknown" in provider_input
    assert [source, answer, peer, active] == before
    assert result.fingerprint == project_finalize_context(source_request=source, outcomes=[answer, peer], active_event=active, agent_id=8, system="system").fingerprint


def test_finalize_projection_discloses_exact_shortened_and_omitted_context_within_budget():
    from app.services.context_projection import project_finalize_context
    source = {"event_id": str(uuid4()), "room_id": 9, "seq": 1, "actor_type": "user", "actor_id": 1,
              "actor_name_snapshot": "Owner", "kind": "message", "payload": {"content": "Original code discussion"}}
    outcomes = [{**source, "event_id": str(uuid4()), "seq": index + 2, "actor_type": "agent", "actor_id": 8,
                 "actor_name_snapshot": "Proposer", "payload": {"content": "x" * 5000}} for index in range(9)]
    active = {**source, "event_id": str(uuid4()), "seq": 20, "payload": {"content": "Summarize exact results"}}
    result = project_finalize_context(source_request=source, outcomes=outcomes, active_event=active, agent_id=8, system="system")
    coverage = result.source_coverage
    assert coverage["omitted_event_ids"] and coverage["truncations"]
    assert f'{len(coverage["omitted_event_ids"])} omitted messages' in str(result.messages)
    assert f'{len(coverage["truncations"])} shortened events' in str(result.messages)
    assert coverage["used_chars"] == result.used_chars <= 32000


def test_finalize_records_declined_answer_as_unanswered_not_a_passed_test(finalize_client):
    client, ids = finalize_client
    with client.app.state.session_factory() as db:
        peer = db.get(RoomEvent, ids["failure"])
        peer.kind = "activity"
        peer.payload = {**peer.payload, "status": "passed"}
        db.commit()
    plan = preview(client, ids).json()
    assert plan["eligible"], plan
    result = execute(client, ids, plan["plan_hash"]).json()
    with client.app.state.session_factory() as db:
        facts = db.get(RoomEvent, result["finalize_event_id"]).payload["finalize"]["source_status"]
        assert facts["unanswered_turn_count"] == 1
