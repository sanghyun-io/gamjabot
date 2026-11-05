"""Keep the sanitized frontend fixture derived from real canonical HTTP responses."""

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.models import User


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "frontend/src/test-fixtures/firstSuccessTask3Wire.json"


def test_first_success_wire_fixture_matches_canonical_routes(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "wire-fixture-token")
    app = create_app(database_url=sqlite_url)
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            owner = User(email="wire@test.invalid", username="wire", hashed_password="x")
            db.add(owner); db.commit(); db.refresh(owner)
        headers = {
            "X-Gamja-Session": client.app.state.local_session_token,
            "Authorization": f"Bearer {create_access_token({'sub': owner.id})}",
        }
        client.app.state.connection_transport = httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]}))
        connection = client.post("/api/v1/connections", headers=headers, json={
            "provider_kind": "compatible", "label": "Synthetic",
            "default_model_id": "synthetic-model", "base_url": "http://127.0.0.1:8765/v1",
            "allow_local_http": True, "auth_required": False,
        }).json()
        test_preview = client.get(
            f"/api/v1/connections/{connection['id']}/test-preview",
            params={"model_id": "synthetic-model", "profile": "text"}, headers=headers).json()
        tested = client.post(
            f"/api/v1/connections/{connection['id']}/test",
            params={"model_id": "synthetic-model", "profile": "text"}, headers=headers).json()
        intent = {"project_title": "First team", "room_title": "First decision", "agents": [
            {"role": "proposer", "name": "Proposer", "connection_id": connection["id"],
             "model_id": "synthetic-model"},
            {"role": "reviewer", "name": "Reviewer", "connection_id": connection["id"],
             "model_id": "synthetic-model"},
        ]}
        planned = client.post("/api/v1/starter/preview", headers=headers, json=intent).json()
        applied = client.post("/api/v1/starter/apply", headers={
            **headers, "Idempotency-Key": "wire-fixture"},
            json={**intent, "plan_hash": planned["plan_hash"]}).json()
        last = client.get("/api/v1/starter/last-result", headers=headers).json()

    expected = json.loads(FIXTURE.read_text())
    connection_id, record_id = connection["id"], tested["record"]["id"]
    agent_map = {applied["agents"]["proposer"]: 101, applied["agents"]["reviewer"]: 102}
    normalized_intent = {**intent, "agents": [
        {**item, "connection_id": 41} for item in intent["agents"]]}
    normalized_test = {**tested, "record": {
        **tested["record"], "id": 71, "connection_id": 41,
        "tested_at": "2026-10-06T00:00:00Z", "duration_ms": 12,
        "config_fingerprint": "b" * 64,
    }}
    normalized_test.pop("verified", None)
    normalized_plan = {
        **planned, "plan_hash": "a" * 64,
        "agents": [{**item, "connection_id": 41, "test_history_id": 71,
                    "config_fingerprint": "b" * 64} for item in planned["agents"]],
    }
    normalized_apply = {
        **applied, "project_id": 81, "room_id": 91,
        "agents": {role: agent_map[value] for role, value in applied["agents"].items()},
    }
    normalized_last = {
        **last, "project_id": 81, "room_id": 91,
        "agents": {role: agent_map[value] for role, value in last["agents"].items()},
    }
    assert connection_id > 0 and record_id > 0
    assert expected == {
        "starter_intent": normalized_intent,
        "text_test_preview": test_preview,
        "text_test_result": normalized_test,
        "starter_preview": normalized_plan,
        "starter_apply_result": normalized_apply,
        "starter_last_result": normalized_last,
    }
