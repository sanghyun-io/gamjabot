"""Owner readiness facts and explicit provider-test history stay safe and durable."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.collaboration import ConnectionTestHistory, ProviderConnection
from app.models.models import Agent, User
from app.schemas.readiness import TestHistory as ReadinessHistory


class FakeSecrets:
    def __init__(self):
        self.values = {}
        self.reads = 0

    def put(self, reference, value):
        self.values[reference] = value

    def get_no_ui(self, reference):
        self.reads += 1
        return self.values.get(reference)

    def get(self, reference):
        self.reads += 1
        return self.values.get(reference)

    def delete(self, reference):
        self.values.pop(reference, None)


@pytest.fixture
def readiness(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "readiness-test-token")
    with TestClient(create_app(database_url=sqlite_url)) as client:
        secrets = FakeSecrets()
        client.app.state.secret_store = secrets
        with client.app.state.session_factory() as db:
            users = [User(email=f"{name}@example.test", username=name, hashed_password="unused")
                     for name in ("owner", "other")]
            db.add_all(users); db.commit()
            owner_ids = [user.id for user in users]
        def headers(index=0):
            return {"X-Gamja-Session": "readiness-test-token",
                    "Authorization": f'Bearer {create_access_token({"sub": owner_ids[index]})}'}
        yield client, secrets, headers, owner_ids


def _connection(client, headers, **changes):
    body = {"provider_kind": "compatible", "label": "Synthetic",
            "base_url": "http://127.0.0.1:8765/v1", "allow_local_http": True,
            "auth_required": False, "default_model_id": "default-model",
            "capabilities": {"tools": True}}
    body.update(changes)
    response = client.post("/api/v1/connections", headers=headers(), json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_listing_is_owner_scoped_db_only_and_keeps_agent_specific_models(readiness, monkeypatch):
    client, secrets, headers, owner_ids = readiness
    owned = _connection(client, headers)
    foreign = _connection(client, lambda: headers(1), label="Foreign")
    with client.app.state.session_factory() as db:
        db.query(ProviderConnection).filter_by(id=owned["id"]).update({
            "default_model_id": "", "base_url": "https://user:url-sentinel@example.com/v1?token=url-sentinel"})
        db.add_all([
            Agent(user_id=owner_ids[0], name="Bound", description="d", system_prompt="s", model="legacy",
                  connection_id=owned["id"], model_id="agent-exact", is_active=True),
            Agent(user_id=owner_ids[0], name="Missing", description="d", system_prompt="s", model="legacy",
                  connection_id=owned["id"], model_id=None, is_active=True),
            Agent(user_id=owner_ids[0], name="Inactive", description="d", system_prompt="s", model="legacy",
                  connection_id=owned["id"], model_id="old", is_active=False),
        ])
        db.commit()
    monkeypatch.setattr(secrets, "get", lambda *_: pytest.fail("listing opened credential store"))
    monkeypatch.setattr(secrets, "get_no_ui", lambda *_: pytest.fail("listing opened credential store"))
    response = client.get("/api/v1/readiness", headers=headers())
    assert response.status_code == 200, response.text
    text = response.text
    assert [item["connection_id"] for item in response.json()["connections"]] == [owned["id"]]
    assert "Foreign" not in text and "secret_ref" not in text and "url-sentinel" not in text
    item = response.json()["connections"][0]
    assert item["declared_state"] == "declared"
    assert item["credential_observation"] == "not_required"
    assert item["binding_counts"] == {"active_bound": 1, "active_missing_model": 1, "inactive": 1, "total": 3}
    assert [(row["agent_name"], row["status"], row["observation"]) for row in item["agent_bindings"]] == [
        ("Bound", "bound", "unknown"), ("Missing", "model_missing", "unknown"),
        ("Inactive", "inactive", "unknown")]
    assert item["public_config"]["default_model_id"] == ""
    assert item["public_config"]["base_url"] is None


def test_explicit_tests_persist_only_normalized_bounded_history(readiness):
    client, secrets, headers, _ = readiness
    row = _connection(client, headers)
    path = f'/api/v1/connections/{row["id"]}/test'
    client.app.state.connection_transport = httpx.MockTransport(
        lambda request: httpx.Response(500, json={"error": "raw-provider-sentinel", "key": "secret-sentinel"},
                                       headers={"X-Secret": "header-sentinel"}))
    failed = client.post(path, headers=headers()).json()
    assert failed["code"] == "provider_error" and failed["record"]["config_current"] is True
    assert not failed["record"]["ok"]
    client.app.state.connection_transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]}))
    succeeded = client.post(path + "?model_id=agent-exact", headers=headers()).json()
    assert succeeded["ok"] is True and succeeded["record"]["code"] == "connected"
    history = client.get(f'/api/v1/connections/{row["id"]}/test-history', headers=headers())
    assert history.status_code == 200
    payload = history.json()
    assert [item["code"] for item in payload["items"]] == ["connected", "provider_error"]
    assert payload["items"][0]["model_id"] == "agent-exact"
    assert payload["items"][0]["verified_capabilities"] == {
        "max_tokens": False, "temperature": False, "tools": False, "vision": False}
    rendered = json.dumps(payload)
    for forbidden in ("raw-provider-sentinel", "secret-sentinel", "header-sentinel", "secret_ref"):
        assert forbidden not in rendered
    assert client.get(f'/api/v1/connections/{row["id"]}/test-history', headers=headers(1)).status_code == 404
    assert client.get(f'/api/v1/connections/{row["id"]}/test-history?limit=21', headers=headers()).status_code == 422


def test_test_preflight_is_db_only_and_reports_declared_profile_bound(readiness, monkeypatch):
    client, secrets, headers, _ = readiness
    row = _connection(client, headers, capabilities={
        "max_tokens": True, "temperature": True, "tools": True, "vision": True})
    monkeypatch.setattr(secrets, "get", lambda *_: pytest.fail("preflight opened credential store"))
    monkeypatch.setattr(secrets, "get_no_ui", lambda *_: pytest.fail("preflight opened credential store"))
    client.app.state.connection_transport = httpx.MockTransport(
        lambda _request: pytest.fail("preflight called provider"))
    text = client.get(
        f'/api/v1/connections/{row["id"]}/test-preview?model_id=exact&profile=text',
        headers=headers())
    full = client.get(
        f'/api/v1/connections/{row["id"]}/test-preview?model_id=exact&profile=full',
        headers=headers())
    assert text.json() == {"profile": "text", "model_id": "exact", "max_request_count": 1}
    assert full.json() == {"profile": "full", "model_id": "exact", "max_request_count": 7}


def test_history_schema_exposes_legacy_unknown_and_populated_test_profiles(readiness):
    client, _, headers, owner_ids = readiness
    row = _connection(client, headers, capabilities={})
    with client.app.state.session_factory() as db:
        db.add(ConnectionTestHistory(
            owner_id=owner_ids[0], connection_id=row["id"], config_revision=1,
            config_fingerprint="0" * 64, model_id=None, ok=False,
            outcome_code="provider_error", credential_observation="unknown",
            verified_capabilities=None, duration_ms=0, profile=None,
            actual_request_count=None,
        ))
        db.commit()
    client.app.state.connection_transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]}))
    client.post(f'/api/v1/connections/{row["id"]}/test?profile=text', headers=headers())
    client.post(f'/api/v1/connections/{row["id"]}/test?profile=full', headers=headers())

    payload = client.get(f'/api/v1/connections/{row["id"]}/test-history', headers=headers()).json()
    validated = ReadinessHistory.model_validate(payload)
    assert [item.profile for item in validated.items] == ["full", "text", None]
    assert [item.actual_request_count for item in validated.items] == [1, 1, None]
    route = next(route for route in client.app.routes
                 if route.path == '/api/v1/connections/{connection_id}/test-history')
    assert route.response_model is ReadinessHistory


def test_text_profile_uses_one_request_and_preserves_full_capability_facts(readiness):
    client, _, headers, _ = readiness
    row = _connection(client, headers, capabilities={"tools": True, "vision": True})
    prior = {"models": {"exact": {"base_url": row["base_url"], "max_tokens": True,
                                    "temperature": True, "tools": True, "vision": True}}}
    with client.app.state.session_factory() as db:
        connection = db.get(ProviderConnection, row["id"])
        connection.capabilities = {"declared": {"tools": True, "vision": True}, "verified": prior}
        db.commit()
    requests = []
    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        assert "tools" not in body
        assert not any(isinstance(item.get("content"), list) for item in body["messages"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
    client.app.state.connection_transport = httpx.MockTransport(handler)
    tested = client.post(
        f'/api/v1/connections/{row["id"]}/test?model_id=exact&profile=text', headers=headers())
    assert tested.status_code == 200, tested.text
    payload = tested.json()
    assert payload["profile"] == "text"
    assert payload["max_request_count"] == payload["actual_request_count"] == 1
    assert payload["record"]["profile"] == "text"
    assert payload["record"]["actual_request_count"] == 1
    assert payload["record"]["verified_capabilities"] is None
    assert len(requests) == 1
    saved = client.get('/api/v1/connections', headers=headers()).json()[0]
    assert saved["capabilities"]["verified"] == prior


def test_failed_text_probe_counts_single_attempt_without_hidden_retry(readiness):
    client, _, headers, _ = readiness
    row = _connection(client, headers)
    attempts = 0
    def unavailable(request):
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("synthetic", request=request)
    client.app.state.connection_transport = httpx.MockTransport(unavailable)
    result = client.post(
        f'/api/v1/connections/{row["id"]}/test?profile=text', headers=headers()).json()
    assert result["code"] == "unreachable"
    assert result["actual_request_count"] == result["record"]["actual_request_count"] == 1
    assert result["record"]["profile"] == "text"
    assert attempts == 1


@pytest.mark.asyncio
async def test_delayed_test_records_stale_evidence_without_overwriting_new_configuration(readiness):
    client, _, headers, _ = readiness
    row = _connection(client, headers, capabilities={"temperature": True})
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        if not entered.is_set():
            entered.set(); await release.wait()
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
    client.app.state.connection_transport = httpx.MockTransport(handler)
    from app.api.routes.connections import test_connection
    with client.app.state.session_factory() as db:
        user = db.query(User).filter_by(username="owner").one()
        running = asyncio.create_task(test_connection(row["id"], SimpleNamespace(app=client.app), db=db, user=user))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            edited = await asyncio.wait_for(asyncio.to_thread(
                client.patch, f'/api/v1/connections/{row["id"]}', headers=headers(),
                json={"capabilities": {"tools": True}}), 2)
            assert edited.status_code == 200
        finally:
            release.set()
    outcome = await running
    assert outcome["code"] == "configuration_changed"
    assert outcome["record"]["profile"] == "full"
    assert outcome["record"]["actual_request_count"] == 2
    assert outcome["record"]["config_current"] is False
    listed = client.get("/api/v1/readiness", headers=headers()).json()["connections"][0]
    assert listed["credential_observation"] == "not_required"
    assert listed["latest_test"]["code"] == "configuration_changed"
    assert listed["latest_test"]["config_current"] is False
    assert listed["public_config"]["declared_capabilities"] == {"tools": True}


def test_model_observation_is_exact_and_history_retains_latest_twenty(readiness):
    client, _, headers, owner_ids = readiness
    row = _connection(client, headers)
    with client.app.state.session_factory() as db:
        db.add_all([
            Agent(user_id=owner_ids[0], name="Default", description="d", system_prompt="s", model="legacy",
                  connection_id=row["id"], model_id="default-model", is_active=True),
            Agent(user_id=owner_ids[0], name="Other model", description="d", system_prompt="s", model="legacy",
                  connection_id=row["id"], model_id="other-model", is_active=True),
        ])
        db.query(ProviderConnection).filter_by(id=row["id"]).update({"default_model_id": ""})
        db.commit()
    client.app.state.connection_transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]}))
    path = f'/api/v1/connections/{row["id"]}/test?model_id=default-model'
    for _ in range(21):
        assert client.post(path, headers=headers()).json()["ok"] is True
    readiness_row = client.get("/api/v1/readiness", headers=headers()).json()["connections"][0]
    bindings = {item["agent_name"]: item for item in readiness_row["agent_bindings"]}
    assert bindings["Default"]["observation"] == "verified"
    assert bindings["Other model"]["observation"] == "unknown"
    history = client.get(f'/api/v1/connections/{row["id"]}/test-history', headers=headers()).json()["items"]
    assert len(history) == 20
    assert history == sorted(history, key=lambda item: item["id"], reverse=True)


def test_key_rotation_invalidates_observation_without_hashing_secret_reference(readiness):
    client, _, headers, _ = readiness
    row = _connection(client, headers, provider_kind="openai", base_url=None,
                      allow_local_http=False, auth_required=True, api_key="first-key")
    client.app.state.connection_transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]}))
    tested = client.post(f'/api/v1/connections/{row["id"]}/test', headers=headers()).json()
    assert tested["record"]["credential_observation"] == "verified"
    before = tested["record"]["config_fingerprint"]
    rotated = client.patch(f'/api/v1/connections/{row["id"]}', headers=headers(), json={"api_key": "second-key"})
    assert rotated.status_code == 200
    listed = client.get("/api/v1/readiness", headers=headers()).json()["connections"][0]
    assert listed["config_fingerprint"] != before
    assert listed["credential_observation"] == "unknown"
    assert listed["latest_test"]["config_current"] is False
    rendered = json.dumps(listed)
    assert "first-key" not in rendered and "second-key" not in rendered and "secret_ref" not in rendered


def test_exact_test_fingerprint_binds_provider_protocol_config_grant_model_capabilities_and_profile():
    from app.services.readiness import test_fingerprint

    row = ProviderConnection(
        id=91, user_id=1, kind="compatible", name="Exact",
        base_url=None, secret_ref="opaque-ref", default_model_id="vendor/default",
        enabled=True, auth_required=True,
        capabilities={"declared": {"tools": True}, "verified": {}},
        readiness_revision=7, provider_id="openrouter",
        protocol_id="openai_chat_completions", auth_method_id="api_key",
        public_config_version=1, public_config={}, auth_grant_revision=3,
        credential_state="present",
    )
    baseline = test_fingerprint(row, "vendor/model", "text")
    mutations = []
    for field, value in (
        ("provider_id", "deepseek"),
        ("protocol_id", "anthropic_messages"),
        ("public_config", {"base_url": "https://example.test/v1"}),
        ("auth_grant_revision", 4),
        ("capabilities", {"declared": {"tools": False}, "verified": {}}),
    ):
        original = getattr(row, field)
        setattr(row, field, value)
        mutations.append(test_fingerprint(row, "vendor/model", "text"))
        setattr(row, field, original)
    mutations.extend([
        test_fingerprint(row, "other/model", "text"),
        test_fingerprint(row, "vendor/model", "full"),
    ])
    assert len({baseline, *mutations}) == 8
    assert "opaque-ref" not in baseline
