"""Safe targeted retry contracts on disposable SQLite only."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sqlalchemy_event

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.collaboration import (
    ProviderConnection, RequestRetryRecord, RoomAgentRun, RoomEvent, RoomMembership,
    RoomTrigger, ToolAction,
)
from app.models.models import Agent, Conversation, Message, Project, User
from app.services.llm_service import LLMFailure
from app.services.providers import ProviderResult
from app.services.request_retry import execute_retry


@pytest.fixture
def retry_client(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "retry-test-token")
    app = create_app(database_url=sqlite_url)
    with TestClient(app) as client:
        provider_calls = []
        async def provider(request):
            provider_calls.append(request.agent_id)
            return ProviderResult(text="synthetic targeted retry")
        app.state.scheduler.provider = provider
        with app.state.session_factory() as db:
            owner = User(email="retry-owner@test.invalid", username="retry-owner", hashed_password="x")
            other = User(email="retry-other@test.invalid", username="retry-other", hashed_password="x")
            db.add_all([owner, other]); db.flush()
            project = Project(user_id=owner.id, name="Retry project")
            foreign_project = Project(user_id=other.id, name="Foreign")
            db.add_all([project, foreign_project]); db.flush()
            room = Conversation(user_id=owner.id, project_id=project.id, title="Retry room")
            foreign_room = Conversation(user_id=other.id, project_id=foreign_project.id, title="Foreign")
            db.add_all([room, foreign_room]); db.flush()
            connection = ProviderConnection(
                user_id=owner.id, kind="compatible", name="Synthetic",
                base_url="http://127.0.0.1:8765/v1", default_model_id="default",
                auth_required=False, enabled=True, readiness_revision=1,
                # Synthetic evidence for this injected provider, not a real model check.
                capabilities={'model_features': {'exact': {
                    'model_id': 'exact', 'base_url': 'http://127.0.0.1:8765/v1',
                    'config_revision': 1, 'tested': {'text_generation': True},
                }}},
            )
            db.add(connection); db.flush()
            target = Agent(user_id=owner.id, name="Target now", description="d", system_prompt="p",
                           connection_id=connection.id, model_id="exact")
            peer = Agent(user_id=owner.id, name="Peer", description="d", system_prompt="p",
                         connection_id=connection.id, model_id="exact")
            db.add_all([target, peer]); db.flush()
            db.add_all([
                RoomMembership(room_id=room.id, agent_id=target.id),
                RoomMembership(room_id=room.id, agent_id=peer.id),
            ])
            db.flush()
            db.add_all([
                RoomAgentRun(room_id=room.id, agent_id=target.id),
                RoomAgentRun(room_id=room.id, agent_id=peer.id),
            ])
            db.flush()
            request_id, failure_id = str(uuid4()), str(uuid4())
            request = RoomEvent(
                event_id=request_id, room_id=room.id, seq=1, actor_type="user", actor_id=owner.id,
                actor_name_snapshot="retry-owner", kind="message",
                payload={"content": "@Target now @Peer inspect safely"}, created_at=datetime(2026, 10, 5),
            )
            failure = RoomEvent(
                event_id=failure_id, room_id=room.id, seq=2, actor_type="agent", actor_id=target.id,
                actor_name_snapshot="Target historical", kind="failure",
                payload={
                    "code": "provider_unavailable", "message": "PRIVATE provider detail",
                    "trigger_event_id": request_id, "round": 1,
                    "diagnostic": {"version": 1, "room_id": room.id, "agent_id": target.id,
                                   "trigger_event_id": request_id,
                                   "failure_code": "provider_unavailable"},
                }, created_at=datetime(2026, 10, 5, 0, 0, 1),
            )
            db.add_all([request, failure]); db.flush()
            db.add(RoomTrigger(
                event_id=request_id, room_id=room.id, state="done",
                selected_agents=[target.id, peer.id], max_rounds=3,
                max_tool_calls=12, max_executor_seconds=90, current_round=1,
            ))
            db.commit()
            ids = {
                "owner": owner.id, "other": other.id, "room": room.id,
                "foreign_room": foreign_room.id, "target": target.id, "peer": peer.id,
                "connection": connection.id, "request": request_id, "failure": failure_id,
                "provider_calls": provider_calls,
            }
        yield client, ids


def auth(client, owner_id):
    return {
        "X-Gamja-Session": client.app.state.local_session_token,
        "Authorization": f"Bearer {create_access_token({'sub': owner_id})}",
    }


def path(ids, *, room=None, failure=None):
    return f"/api/v1/conversations/{room or ids['room']}/failures/{failure or ids['failure']}/retry"


def preflight(client, ids, *, owner=None):
    return client.get(path(ids), headers=auth(client, owner or ids["owner"]))


def execute(client, ids, plan_hash, key="stable-retry", *, owner=None):
    return client.post(path(ids), json={"plan_hash": plan_hash}, headers={
        **auth(client, owner or ids["owner"]), "Idempotency-Key": key,
    })


def test_preflight_and_execute_create_one_exact_agent_audited_retry(retry_client):
    client, ids = retry_client
    preview = preflight(client, ids)
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert plan == {
        "version": 1, "room_id": ids["room"], "failure_event_id": ids["failure"],
        "failure_seq": 2,
        "source_request": {"event_id": ids["request"], "seq": 1,
                           "preview": "@Target now @Peer inspect safely",
                           "href": f"/chat/{ids['room']}?seq=1"},
        "agent": {"agent_id": ids["target"], "name_snapshot": "Target historical",
                  "current_name": "Target now"},
        "current_model": {"connection_id": ids["connection"], "model_id": "exact",
                          "provider_kind": "compatible"},
        "limits": {"max_rounds": 1, "max_tool_calls": 12, "max_executor_seconds": 90},
        "eligible": True, "blocked_reason": None, "plan_hash": plan["plan_hash"], "teammate_recovery": None,
    }
    assert len(plan["plan_hash"]) == 64
    assert "PRIVATE" not in str(plan)

    created = execute(client, ids, plan["plan_hash"])
    assert created.status_code == 201, created.text
    result = created.json()
    assert result["replayed"] is False and result["agent_id"] == ids["target"]
    assert result["href"] == f"/chat/{ids['room']}?seq=3"
    with client.app.state.session_factory() as db:
        retry_event = db.get(RoomEvent, result["retry_event_id"])
        assert retry_event.actor_type == "user" and retry_event.actor_id == ids["owner"]
        assert retry_event.payload["content"] == "@Target now @Peer inspect safely"
        assert retry_event.payload["retry_of_failure_event_id"] == ids["failure"]
        assert retry_event.payload["retry_agent_id"] == ids["target"]
        trigger = db.get(RoomTrigger, result["retry_event_id"])
        assert trigger.selected_agents == [ids["target"]]
        assert (trigger.max_rounds, trigger.max_tool_calls, trigger.max_executor_seconds) == (1, 12, 90)
        assert db.query(ToolAction).filter_by(trigger_event_id=result["retry_event_id"]).count() == 0
        assert db.get(RoomTrigger, ids["request"]).selected_agents == [ids["target"], ids["peer"]]

    for _ in range(100):
        if ids["provider_calls"]:
            break
        __import__("time").sleep(.01)
    assert ids["provider_calls"] == [ids["target"]]

    replay = execute(client, ids, plan["plan_hash"])
    assert replay.status_code == 201
    assert replay.json() == {**result, "replayed": True}
    changed = execute(client, ids, "0" * 64)
    assert changed.status_code == 409
    assert changed.json()["detail"] == {"code": "request_retry_idempotency_conflict"}
    with client.app.state.session_factory() as db:
        assert db.query(RequestRetryRecord).count() == 1
        assert db.query(RoomEvent).filter_by(room_id=ids["room"], actor_type="user").count() == 2


@pytest.mark.parametrize(("mutation", "reason"), [
    ("source_running", "source_not_terminal"),
    ("superseded", "failure_superseded"),
    ("room_busy", "room_busy"),
    ("agent_paused", "agent_unavailable"),
    ("agent_busy", "agent_busy"),
    ("model_missing", "model_unavailable"),
    ("action_waiting", "action_unsettled"),
    ("action_started", "action_uncertain"),
    ("action_completed_mutating", "completed_effect"),
    ("legacy_code", "source_invalid"),
    ("archived", "room_read_only"),
])
def test_retry_blocks_unsafe_current_state_without_side_effects(retry_client, mutation, reason):
    client, ids = retry_client
    with client.app.state.session_factory() as db:
        if mutation == "source_running":
            db.get(RoomTrigger, ids["request"]).state = "running"
        elif mutation == "superseded":
            db.add(RoomEvent(event_id=str(uuid4()), room_id=ids["room"], seq=3,
                             actor_type="agent", actor_id=ids["target"], actor_name_snapshot="Target",
                             kind="message", payload={"content": "later", "trigger_event_id": ids["request"], "round": 1},
                             created_at=datetime(2026, 10, 5, 0, 0, 2)))
        elif mutation == "room_busy":
            event_id = str(uuid4())
            db.add(RoomEvent(event_id=event_id, room_id=ids["room"], seq=3, actor_type="user",
                             actor_id=ids["owner"], actor_name_snapshot="Owner", kind="message",
                             payload={"content": "other"}, created_at=datetime(2026, 10, 5, 0, 0, 2)))
            db.add(RoomTrigger(event_id=event_id, room_id=ids["room"], state="pending",
                               selected_agents=[ids["peer"]], max_rounds=1,
                               max_tool_calls=1, max_executor_seconds=1))
        elif mutation == "agent_paused":
            db.get(RoomAgentRun, (ids["room"], ids["target"])).paused = True
        elif mutation == "agent_busy":
            db.get(RoomAgentRun, (ids["room"], ids["target"])).state = "running"
        elif mutation == "model_missing":
            db.get(Agent, ids["target"]).model_id = None
        elif mutation.startswith("action_"):
            state = {"action_waiting": "waiting", "action_started": "started",
                     "action_completed_mutating": "completed"}[mutation]
            db.add(ToolAction(action_id=str(uuid4()), room_id=ids["room"], agent_id=ids["peer"],
                              trigger_event_id=ids["request"], family="write_file", arguments={}, state=state))
        elif mutation == "archived":
            db.get(Conversation, ids["room"]).is_active = False
        elif mutation == "legacy_code":
            failure = db.get(RoomEvent, ids["failure"])
            failure.payload = {**failure.payload, "code": "PRIVATE legacy raw code"}
        db.commit()
    before = None
    with client.app.state.session_factory() as db:
        before = (db.query(RoomEvent).count(), db.query(RoomTrigger).count(), db.query(Message).count())
    response = preflight(client, ids)
    assert response.status_code == 200 and response.json()["blocked_reason"] == reason
    assert response.json()["eligible"] is False and response.json()["plan_hash"] is None
    blocked = execute(client, ids, "0" * 64, key=f"blocked-{mutation}")
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == {"code": "request_retry_blocked", "reason": reason}
    with client.app.state.session_factory() as db:
        assert (db.query(RoomEvent).count(), db.query(RoomTrigger).count(), db.query(Message).count()) == before
        assert db.query(RequestRetryRecord).count() == 0


def test_retry_owner_scope_stale_plan_and_concurrent_lost_response(retry_client):
    client, ids = retry_client
    assert preflight(client, ids, owner=ids["other"]).status_code == 404
    assert client.get(path(ids, room=ids["foreign_room"]), headers=auth(client, ids["owner"])).status_code == 404
    assert client.get(path(ids, failure="not-a-uuid"), headers=auth(client, ids["owner"])).status_code == 422
    plan_hash = preflight(client, ids).json()["plan_hash"]
    with client.app.state.session_factory() as db:
        db.get(Agent, ids["target"]).model_id = "new-model"
        db.commit()
    stale = execute(client, ids, plan_hash, key="stale")
    assert stale.status_code == 409
    assert stale.json()["detail"] == {"code": "request_retry_plan_stale"}

    fresh = preflight(client, ids).json()["plan_hash"]
    with client.app.state.session_factory() as db:
        db.get(ProviderConnection, ids["connection"]).readiness_revision += 1
        db.commit()
    credential_stale = execute(client, ids, fresh, key="credential-stale")
    assert credential_stale.status_code == 409
    assert credential_stale.json()["detail"] == {"code": "request_retry_plan_stale"}
    fresh = preflight(client, ids).json()["plan_hash"]
    def send():
        response = execute(client, ids, fresh, key="concurrent")
        return response.status_code, response.json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _index: send(), range(2)))
    assert [status for status, _ in responses] == [201, 201]
    assert len({body["retry_event_id"] for _, body in responses}) == 1
    assert sorted(body["replayed"] for _, body in responses) == [False, True]


def test_retry_rejects_nonprinting_idempotency_key_without_side_effects(retry_client):
    client, ids = retry_client
    plan_hash = preflight(client, ids).json()["plan_hash"]
    with client.app.state.session_factory() as db:
        with pytest.raises(Exception) as error:
            execute_retry(db, ids["owner"], ids["room"], ids["failure"], plan_hash, "retry\u0085key")
    assert getattr(error.value, "status_code", None) == 422
    assert getattr(error.value, "detail", None) == {"code": "request_retry_idempotency_key_invalid"}
    with client.app.state.session_factory() as db:
        assert db.query(RequestRetryRecord).count() == 0
        assert db.query(RoomEvent).filter_by(room_id=ids["room"]).count() == 2


def test_injected_record_failure_rolls_back_event_message_and_trigger(retry_client):
    client, ids = retry_client
    plan_hash = preflight(client, ids).json()["plan_hash"]
    before = None
    with client.app.state.session_factory() as db:
        before = (db.query(RoomEvent).count(), db.query(RoomTrigger).count(), db.query(Message).count())

    def fail(*_args, **_kwargs):
        raise RuntimeError("injected retry record failure")

    sqlalchemy_event.listen(RequestRetryRecord, "before_insert", fail)
    try:
        with pytest.raises(RuntimeError, match="injected retry record failure"):
            execute(client, ids, plan_hash, key="rollback")
    finally:
        sqlalchemy_event.remove(RequestRetryRecord, "before_insert", fail)
    with client.app.state.session_factory() as db:
        assert (db.query(RoomEvent).count(), db.query(RoomTrigger).count(), db.query(Message).count()) == before
        assert db.query(RequestRetryRecord).count() == 0


def test_explicit_agent_model_without_connection_default_remains_dispatchable(retry_client):
    client, ids = retry_client
    with client.app.state.session_factory() as db:
        db.get(ProviderConnection, ids["connection"]).default_model_id = ""
        db.commit()
    preview = preflight(client, ids)
    assert preview.status_code == 200, preview.text
    assert preview.json()["eligible"] is True
    assert preview.json()["current_model"]["model_id"] == "exact"
    created = execute(client, ids, preview.json()["plan_hash"], key="exact-agent-model")
    assert created.status_code == 201, created.text
    for _ in range(100):
        if ids["provider_calls"]:
            break
        __import__("time").sleep(.01)
    assert ids["provider_calls"] == [ids["target"]]


def test_retry_uses_registry_validation_for_typed_connection_without_reading_secret(retry_client):
    client, ids = retry_client
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
    valid = preflight(client, ids)
    assert valid.status_code == 200 and valid.json()["eligible"] is True
    assert valid.json()["current_model"]["provider_kind"] == "compatible"

    with client.app.state.session_factory() as db:
        db.get(ProviderConnection, ids["connection"]).protocol_id = "anthropic_messages"
        db.commit()
    blocked = preflight(client, ids)
    assert blocked.status_code == 200
    assert blocked.json()["blocked_reason"] == "model_unavailable"
    with client.app.state.session_factory() as db:
        assert db.query(RequestRetryRecord).count() == 0


def test_retry_rejects_inconsistent_local_none_without_writes_or_provider_call(retry_client):
    client, ids = retry_client
    secret_reads = []

    class ForbiddenSecrets:
        def get_no_ui(self, reference):
            secret_reads.append(reference)
            raise AssertionError('retry preflight must not read a credential')

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

    blocked = preflight(client, ids)
    assert blocked.status_code == 200
    assert blocked.json()["blocked_reason"] == "model_unavailable"
    attempted = execute(client, ids, "0" * 64, key="invalid-local-none")
    assert attempted.status_code == 409
    assert attempted.json()["detail"] == {
        "code": "request_retry_blocked", "reason": "model_unavailable"}
    with client.app.state.session_factory() as db:
        assert db.query(RequestRetryRecord).count() == 0
        assert (db.query(RoomEvent).count(), db.query(RoomTrigger).count()) == before
    assert ids["provider_calls"] == []
    assert secret_reads == []


def test_retry_scheduler_persists_target_failure_without_falling_back_to_mentions(retry_client):
    client, ids = retry_client
    calls = []
    async def failing_provider(request):
        calls.append(request.agent_id)
        raise LLMFailure("provider_unavailable", "Synthetic provider unavailable.")
    client.app.state.scheduler.provider = failing_provider
    plan_hash = preflight(client, ids).json()["plan_hash"]
    created = execute(client, ids, plan_hash, key="provider-failure")
    assert created.status_code == 201, created.text
    retry_event_id = created.json()["retry_event_id"]
    saved = None
    for _ in range(100):
        with client.app.state.session_factory() as db:
            saved = db.query(RoomEvent).filter_by(
                room_id=ids["room"], actor_type="agent", actor_id=ids["target"],
                kind="failure",
            ).filter(RoomEvent.payload["trigger_event_id"].as_string() == retry_event_id).first()
        if saved is not None:
            break
        __import__("time").sleep(.01)
    assert calls == [ids["target"]]
    assert saved is not None
    assert saved.payload["code"] == "provider_unavailable"
    assert saved.payload["trigger_event_id"] == retry_event_id
    with client.app.state.session_factory() as db:
        assert db.get(RoomTrigger, retry_event_id).state == "done"


def profile_guard_failure(client, ids, *, legacy=False):
    from app.services.diagnostic_detail import make_diagnostic
    from app.services.providers.base import ProviderFailure
    message = "The exact model is not supported by this Coding Plan profile."
    with client.app.state.session_factory() as db:
        failure = db.get(RoomEvent, ids["failure"])
        detail = make_diagnostic(ProviderFailure("unsupported_model_protocol", message),
                                stage="provider_request", failure_code="unsupported_model_protocol",
                                room_id=ids["room"], agent_id=ids["target"], connection_id=1,
                                trigger_event_id=ids["request"], action_id=None)
        detail.update(failure_code="unexpected_error" if legacy else "unsupported_model_protocol",
                      reason="unexpected_error" if legacy else "unsupported_model_protocol")
        failure.payload = {**failure.payload, "code": "unsupported_model_protocol",
                           "message": message, "diagnostic": detail}
        db.commit()


@pytest.mark.parametrize("legacy", [False, True])
def test_repaired_profile_binding_retries_known_guard_without_rewriting_failure(retry_client, legacy):
    from copy import deepcopy
    client, ids = retry_client
    profile_guard_failure(client, ids, legacy=legacy)
    with client.app.state.session_factory() as db:
        original = deepcopy(db.get(RoomEvent, ids["failure"]).payload)
    plan = preflight(client, ids).json()
    assert plan["eligible"] is True, plan
    first = execute(client, ids, plan["plan_hash"])
    assert first.status_code == 201, first.text
    repeated = execute(client, ids, plan["plan_hash"])
    assert repeated.json()["replayed"] is True
    assert repeated.json()["retry_event_id"] == first.json()["retry_event_id"]
    with client.app.state.session_factory() as db:
        assert db.get(RoomEvent, ids["failure"]).payload == original
        assert db.query(RequestRetryRecord).count() == 1


@pytest.mark.parametrize("mutation", ["code", "message", "stage", "reason", "error_class", "exception", "http_status", "action_id", "correlation", "owner_binding"])
def test_legacy_profile_guard_compatibility_rejects_nonmatching_or_malformed_details(retry_client, mutation):
    client, ids = retry_client
    profile_guard_failure(client, ids, legacy=True)
    with client.app.state.session_factory() as db:
        failure = db.get(RoomEvent, ids["failure"])
        payload = {**failure.payload, "diagnostic": {**failure.payload["diagnostic"]}}
        if mutation == "code": payload["code"] = "provider_unavailable"
        elif mutation == "message": payload["message"] = "PRIVATE unrecognized message"
        elif mutation == "stage": payload["diagnostic"]["stage"] = "tool_execution"
        elif mutation == "reason": payload["diagnostic"]["reason"] = "network_error"
        elif mutation == "error_class": payload["diagnostic"]["error_class"] = "runtime_error"
        elif mutation == "exception": payload["diagnostic"]["exception_chain"] = [{"module":"builtins","class":"RuntimeError"}]
        elif mutation == "http_status": payload["diagnostic"]["http_status"] = 400
        elif mutation == "action_id": payload["diagnostic"]["action_id"] = "a" * 32
        elif mutation == "correlation": payload["diagnostic"]["correlation_id"] = "PRIVATE"
        elif mutation == "owner_binding": payload["diagnostic"]["agent_id"] = ids["peer"]
        failure.payload = payload
        db.commit()
    plan = preflight(client, ids).json()
    assert plan["eligible"] is False and plan["blocked_reason"] == "source_invalid"
