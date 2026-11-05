"""Verified starter planning and atomic creation on disposable SQLite."""

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.collaboration import (ConnectionTestHistory, ProviderConnection, RoomAgentRun, RoomEvent,
                                      RoomMembership, RoomScheduleSettings, RoomTrigger, StarterRequest)
from app.models.models import Agent, Conversation, Project, ProjectAgent, User
from app.services.readiness import test_fingerprint as exact_test_fingerprint


@pytest.fixture
def starter_client(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "starter-token")
    app = create_app(database_url=sqlite_url)
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            owner = User(email="starter@test.invalid", username="starter", hashed_password="x")
            other = User(email="other@test.invalid", username="other-starter", hashed_password="x")
            db.add_all([owner, other]); db.flush()
            connection = ProviderConnection(user_id=owner.id, kind="openai", name="Ready",
                                            secret_ref="opaque", enabled=True, auth_required=True,
                                            default_model_id="", capabilities={}, readiness_revision=3)
            foreign = ProviderConnection(user_id=other.id, kind="openai", name="Foreign",
                                         enabled=True, auth_required=False, capabilities={})
            inactive = ProviderConnection(user_id=owner.id, kind="openai", name="Inactive",
                                          enabled=False, auth_required=False, capabilities={})
            db.add_all([connection, foreign, inactive]); db.flush()
            evidence = ConnectionTestHistory(
                owner_id=owner.id, connection_id=connection.id, config_revision=3,
                config_fingerprint=exact_test_fingerprint(connection, "exact-model", "full"), model_id="exact-model", ok=True,
                outcome_code="connected", credential_observation="verified",
                verified_capabilities={}, duration_ms=1)
            db.add(evidence); db.commit()
            ids = {"owner": owner.id, "other": other.id, "connection": connection.id,
                   "foreign": foreign.id, "inactive": inactive.id}
        yield client, ids


def auth(client, owner):
    return {"X-Gamja-Session": client.app.state.local_session_token,
            "Authorization": f"Bearer {create_access_token({'sub': owner})}"}


def intent(ids, **changes):
    value = {"project_title": "First team", "room_title": "First decision",
             "agents": [
                 {"role": "proposer", "name": "Proposer", "connection_id": ids["connection"], "model_id": "exact-model"},
                 {"role": "reviewer", "name": "Reviewer", "connection_id": ids["connection"], "model_id": "exact-model"},
             ]}
    value.update(changes)
    return value


def preview(client, ids, body=None):
    return client.post("/api/v1/starter/preview", headers=auth(client, ids["owner"]), json=body or intent(ids))


def test_preview_apply_replay_and_last_result_are_exact(starter_client):
    client, ids = starter_client
    planned = preview(client, ids)
    assert planned.status_code == 200, planned.text
    plan = planned.json()
    assert plan["policy"] == {"max_rounds": 2, "max_tool_calls": 0, "max_executor_seconds": 60}
    body = {**intent(ids), "plan_hash": plan["plan_hash"]}
    created = client.post("/api/v1/starter/apply", headers={**auth(client, ids["owner"]), "Idempotency-Key": "stable"}, json=body)
    assert created.status_code == 200, created.text
    result = created.json(); assert result["replayed"] is False
    # A restarted client may preview the same intent again. The committed
    # names now collide, so the fresh plan hash changes, but replay identity is
    # still the unchanged normalized intent under the same owner/key.
    restarted_plan = preview(client, ids).json()
    assert restarted_plan["plan_hash"] != plan["plan_hash"]
    replay_after_preview = client.post(
        "/api/v1/starter/apply",
        headers={**auth(client, ids["owner"]), "Idempotency-Key": "stable"},
        json={**intent(ids), "agents": list(reversed(intent(ids)["agents"])),
              "plan_hash": restarted_plan["plan_hash"]},
    )
    assert replay_after_preview.json() == {**result, "replayed": True}
    # Replay is a durable lost-response lookup and therefore precedes current
    # readiness validation, which the successful creation itself may outlive.
    with client.app.state.session_factory() as db:
        db.query(ProviderConnection).filter_by(id=ids["connection"]).update({"readiness_revision": 99})
        db.commit()
    replay = client.post("/api/v1/starter/apply", headers={**auth(client, ids["owner"]), "Idempotency-Key": "stable"}, json=body)
    assert replay.json() == {**result, "replayed": True}
    last = client.get("/api/v1/starter/last-result", headers=auth(client, ids["owner"]))
    assert last.json() == {key: result[key] for key in ("project_id", "room_id", "agents")}
    assert client.get("/api/v1/starter/last-result", headers=auth(client, ids["other"])).json() is None
    with client.app.state.session_factory() as db:
        assert db.query(Agent).filter_by(user_id=ids["owner"]).count() == 2
        assert db.query(Project).filter_by(user_id=ids["owner"]).count() == 1
        assert db.query(ProjectAgent).count() == 2
        assert db.query(RoomMembership).filter_by(room_id=result["room_id"]).count() == 2
        policy = db.get(RoomScheduleSettings, result["room_id"])
        assert (policy.max_rounds, policy.max_tool_calls, policy.max_executor_seconds) == (2, 0, 60)
        assert db.query(RoomEvent).count() == db.query(RoomTrigger).count() == 0


def test_newest_exact_model_test_must_succeed(starter_client):
    client, ids = starter_client
    original_plan = preview(client, ids).json()
    with client.app.state.session_factory() as db:
        connection = db.get(ProviderConnection, ids["connection"])
        db.add(ConnectionTestHistory(
            owner_id=ids["owner"], connection_id=connection.id,
            config_revision=connection.readiness_revision,
            config_fingerprint=exact_test_fingerprint(connection, "exact-model", "full"), model_id="exact-model",
            ok=False, outcome_code="invalid_credential",
            credential_observation="rejected", verified_capabilities=None, duration_ms=2,
        ))
        db.commit()
    blocked = preview(client, ids)
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == {"code": "starter_model_unverified", "role": "proposer"}
    stale = client.post(
        "/api/v1/starter/apply",
        headers={**auth(client, ids["owner"]), "Idempotency-Key": "newest-failed"},
        json={**intent(ids), "plan_hash": original_plan["plan_hash"]},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == {"code": "starter_plan_stale"}
    with client.app.state.session_factory() as db:
        assert db.query(Project).count() == db.query(Agent).count() == 0
        assert db.query(StarterRequest).count() == 0


def test_preflight_stale_and_changed_intent_leave_zero_partial_rows(starter_client):
    client, ids = starter_client
    bad = intent(ids); bad["agents"][1]["model_id"] = "not-tested"
    assert preview(client, ids, bad).json()["detail"]["code"] == "starter_model_unverified"
    foreign = intent(ids); foreign["agents"][1]["connection_id"] = ids["foreign"]
    assert preview(client, ids, foreign).status_code == 404
    inactive = intent(ids); inactive["agents"][1]["connection_id"] = ids["inactive"]
    assert preview(client, ids, inactive).status_code == 404
    plan = preview(client, ids).json()
    with client.app.state.session_factory() as db:
        db.query(ProviderConnection).filter_by(id=ids["connection"]).update({"readiness_revision": 4}); db.commit()
    response = client.post("/api/v1/starter/apply", headers={**auth(client, ids["owner"]), "Idempotency-Key": "stale"},
                           json={**intent(ids), "plan_hash": plan["plan_hash"]})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "starter_plan_stale"
    with client.app.state.session_factory() as db:
        assert db.query(Project).count() == db.query(Conversation).count() == db.query(StarterRequest).count() == 0


def test_collision_allocates_new_names_and_concurrent_apply_creates_one(starter_client):
    client, ids = starter_client
    with client.app.state.session_factory() as db:
        db.add(Project(user_id=ids["owner"], name="First team"))
        db.add(Agent(user_id=ids["owner"], name="Proposer", description="d", system_prompt="p")); db.commit()
    plan = preview(client, ids).json()
    assert plan["project_title"] == "First team (2)"
    assert next(item for item in plan["agents"] if item["role"] == "proposer")["name"] == "Proposer (2)"
    body = {**intent(ids), "plan_hash": plan["plan_hash"]}
    def send():
        return client.post("/api/v1/starter/apply", headers={**auth(client, ids["owner"]), "Idempotency-Key": "race"}, json=body)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: send(), range(2)))
    assert [row.status_code for row in responses] == [200, 200]
    assert len({row.json()["room_id"] for row in responses}) == 1
    changed = {**body, "room_title": "Changed"}
    assert client.post("/api/v1/starter/apply", headers={**auth(client, ids["owner"]), "Idempotency-Key": "race"}, json=changed).json()["detail"]["code"] == "starter_idempotency_conflict"


@pytest.mark.parametrize("mapped", [Agent, Project, ProjectAgent, Conversation, RoomMembership,
                                     RoomAgentRun, RoomScheduleSettings, StarterRequest])
def test_every_insert_stage_failure_rolls_back_all_starter_rows(starter_client, mapped):
    client, ids = starter_client
    plan = preview(client, ids).json()
    def fail(*_args):
        raise RuntimeError("synthetic insert failure")
    event.listen(mapped, "before_insert", fail)
    try:
        with pytest.raises(RuntimeError, match="synthetic insert failure"):
            client.post("/api/v1/starter/apply", headers={**auth(client, ids["owner"]),
                        "Idempotency-Key": f"fail-{mapped.__tablename__}"},
                        json={**intent(ids), "plan_hash": plan["plan_hash"]})
    finally:
        event.remove(mapped, "before_insert", fail)
    with client.app.state.session_factory() as db:
        assert db.query(Project).count() == db.query(Conversation).count() == 0
        assert db.query(Agent).count() == db.query(StarterRequest).count() == 0


def test_last_result_survives_application_restart(starter_client, sqlite_url, monkeypatch):
    client, ids = starter_client
    plan = preview(client, ids).json()
    result = client.post("/api/v1/starter/apply", headers={**auth(client, ids["owner"]), "Idempotency-Key": "restart"},
                         json={**intent(ids), "plan_hash": plan["plan_hash"]}).json()
    from app.services.starter import last_result
    reopened = create_engine(sqlite_url, connect_args={"check_same_thread": False})
    with sessionmaker(bind=reopened)() as db:
        assert last_result(db, ids["owner"]) == {
            key: result[key] for key in ("project_id", "room_id", "agents")}
    reopened.dispose()


@pytest.mark.parametrize("role,field", [("proposer", 0), ("proposer", 1), ("reviewer", 0), ("reviewer", 1)])
def test_role_definition_change_invalidates_uncommitted_starter_plan(starter_client, monkeypatch, role, field):
    """A plan from before a default-role update cannot silently create different Agents."""
    from app.services.starter import ROLE_TEXT
    client, ids = starter_client
    planned = preview(client, ids).json()
    changed = list(ROLE_TEXT[role]); changed[field] += " Updated definition."
    monkeypatch.setitem(ROLE_TEXT, role, tuple(changed))
    rejected = client.post(
        "/api/v1/starter/apply",
        headers={**auth(client, ids["owner"]), "Idempotency-Key": "role-changed"},
        json={**intent(ids), "plan_hash": planned["plan_hash"]},
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == {"code": "starter_plan_stale"}
    with client.app.state.session_factory() as db:
        assert db.query(Agent).count() == db.query(Project).count() == 0
        assert db.query(Conversation).count() == db.query(StarterRequest).count() == 0


def test_committed_starter_replay_keeps_original_role_prompts_and_custom_agent(starter_client, monkeypatch):
    """New defaults apply only to newly created teams; existing/custom Agents are preserved."""
    from app.services.starter import ROLE_TEXT
    client, ids = starter_client
    with client.app.state.session_factory() as db:
        custom = Agent(user_id=ids["owner"], name="Existing custom", description="Custom role",
                       system_prompt="Owner's original custom instructions.")
        db.add(custom); db.commit()
    plan = preview(client, ids).json()
    body = {**intent(ids), "plan_hash": plan["plan_hash"]}
    headers = {**auth(client, ids["owner"]), "Idempotency-Key": "role-replay"}
    first = client.post("/api/v1/starter/apply", headers=headers, json=body).json()
    with client.app.state.session_factory() as db:
        before = [(a.id, a.name, a.description, a.system_prompt) for a in db.query(Agent).order_by(Agent.id)]
    monkeypatch.setitem(ROLE_TEXT, "proposer", ("New description", "New role instructions."))
    replay = client.post("/api/v1/starter/apply", headers=headers, json=body)
    assert replay.status_code == 200
    assert replay.json() == {**first, "replayed": True}
    with client.app.state.session_factory() as db:
        after = [(a.id, a.name, a.description, a.system_prompt) for a in db.query(Agent).order_by(Agent.id)]
        assert after == before and len(after) == 3
        assert db.query(StarterRequest).count() == 1
