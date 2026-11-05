"""Explicit, bounded model discovery stays separate from readiness evidence."""

import asyncio
import json
import socket
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.collaboration import ProviderConnection
from app.models.models import User


class FakeSecrets:
    def __init__(self):
        self.values = {}
        self.reads = 0

    def get_no_ui(self, reference):
        self.reads += 1
        return self.values.get(reference)


@pytest.fixture
def models_setup(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "models-test-token")
    secrets = FakeSecrets()
    with TestClient(create_app(database_url=sqlite_url)) as client:
        client.app.state.secret_store = secrets
        with client.app.state.session_factory() as db:
            owners = [User(email=f"{name}@example.test", username=name, hashed_password="unused")
                      for name in ("owner", "other")]
            db.add_all(owners)
            db.commit()
            owner_ids = [owner.id for owner in owners]

        def headers(index=0):
            token = create_access_token({"sub": owner_ids[index]})
            return {"X-Gamja-Session": "models-test-token", "Authorization": f"Bearer {token}"}

        def connection(provider_id, *, base_url=None, default_model_id="", grant=3,
                       readiness_revision=7, auth_required=True):
            with client.app.state.session_factory() as db:
                fields = {"base_url": base_url} if base_url else {}
                row = ProviderConnection(
                    user_id=owner_ids[0], kind="compatible", name=provider_id,
                    base_url=base_url, secret_ref=(f"ref-{provider_id}" if auth_required else None),
                    default_model_id=default_model_id, enabled=True,
                    auth_required=auth_required, capabilities={"declared": {}, "verified": {}},
                    readiness_revision=readiness_revision, provider_id=provider_id,
                    protocol_id="openai_chat_completions",
                    auth_method_id="api_key" if auth_required else "local_none",
                    public_config_version=1, public_config=fields,
                    auth_grant_revision=grant,
                    credential_state="present" if auth_required else "not_required",
                )
                db.add(row)
                db.commit()
                db.refresh(row)
                if auth_required:
                    secrets.values[row.secret_ref] = "synthetic-key"
                return row.id

        yield client, secrets, headers, connection, owner_ids, sqlite_url


def test_cached_get_is_owner_scoped_static_aware_and_has_zero_external_calls(models_setup, monkeypatch):
    client, secrets, headers, connection, _, _ = models_setup
    connection_id = connection("zai", default_model_id="vendor/custom-model")
    calls = []
    client.app.state.connection_transport = httpx.MockTransport(
        lambda request: calls.append(request) or pytest.fail("cached GET must not use transport"))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: pytest.fail("cached GET must not use DNS"))

    response = client.get(f"/api/v1/connections/{connection_id}/models", headers=headers())

    assert response.status_code == 200, response.text
    assert response.json() == {
        "observed_at": None,
        "stale": False,
        "truncated": False,
        "models": [
            {"id": "glm-5.1", "label": "glm-5.1", "source": "configured"},
            {"id": "glm-5.2", "label": "glm-5.2", "source": "configured"},
            {"id": "glm-5.3", "label": "glm-5.3", "source": "configured"},
        ],
    }
    assert secrets.reads == 0 and calls == []
    assert client.get(f"/api/v1/connections/{connection_id}/models", headers=headers(1)).status_code == 404


@pytest.mark.parametrize(("provider_id", "base_url", "expected_url", "payload", "expected"), [
    ("openrouter", None, "https://openrouter.ai/api/v1/models",
     {"data": [{"id": "openai/gpt-5"}, {"id": "openai/gpt-5"}, {"id": "anthropic/claude"}]},
     ["anthropic/claude", "openai/gpt-5"]),
    ("dashscope-cn", "https://workspace-1.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
     "https://workspace-1.cn-beijing.maas.aliyuncs.com/api/v1/models",
     {"data": [{"id": "qwen-plus"}]}, ["qwen-plus"]),
    ("ollama", "http://127.0.0.1:11434/v1", "http://127.0.0.1:11434/api/tags",
     {"models": [{"name": "llama3.2:latest"}, {"model": "qwen2.5:7b"}]},
     ["llama3.2:latest", "qwen2.5:7b"]),
    ("lm-studio", "http://127.0.0.1:1234/v1", "http://127.0.0.1:1234/v1/models",
     {"data": [{"id": "publisher/model"}]}, ["publisher/model"]),
])
def test_explicit_refresh_uses_reviewed_endpoint_and_bounded_exact_ids(
        models_setup, provider_id, base_url, expected_url, payload, expected):
    client, secrets, headers, connection, _, _ = models_setup
    local = provider_id in {"ollama", "lm-studio"}
    connection_id = connection(provider_id, base_url=base_url, auth_required=not local)
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == expected_url
        if local:
            assert "authorization" not in request.headers
        else:
            assert request.headers["authorization"] == "Bearer synthetic-key"
        return httpx.Response(200, json=payload)

    client.app.state.connection_transport = httpx.MockTransport(handler)
    response = client.post(
        f"/api/v1/connections/{connection_id}/models/refresh", headers=headers(),
        json={"version": 1, "config_revision": 7, "auth_grant_revision": 3})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["observed_at"].endswith("Z")
    assert body["stale"] is False and body["truncated"] is False
    assert [item["id"] for item in body["models"]] == expected
    assert all(item["source"] == ("native_catalog" if local else "discovery") for item in body["models"])
    assert len(requests) == 1
    assert secrets.reads == (0 if local else 1)


def test_refresh_body_is_closed_and_revisions_are_compare_and_swap(models_setup):
    client, _, headers, connection, _, _ = models_setup
    connection_id = connection("openrouter")
    path = f"/api/v1/connections/{connection_id}/models/refresh"
    for body in (
        {"version": 2, "config_revision": 7, "auth_grant_revision": 3},
        {"version": 1, "config_revision": 6, "auth_grant_revision": 3},
        {"version": 1, "config_revision": 7, "auth_grant_revision": 2},
        {"version": 1, "config_revision": 7, "auth_grant_revision": 3, "secret": "no"},
    ):
        response = client.post(path, headers=headers(), json=body)
        assert response.status_code in {409, 422}


def test_refresh_failure_is_finite_redacted_and_preserves_previous_cache(models_setup):
    client, _, headers, connection, owner_ids, _ = models_setup
    connection_id = connection("openrouter")
    with client.app.state.session_factory() as db:
        db.execute(text("""INSERT INTO connection_model_cache
            (owner_id,connection_id,config_revision,auth_grant_revision,config_fingerprint,
             observed_at,truncated,models) VALUES
            (:owner,:connection,7,3,:fingerprint,:observed,0,:models)"""), {
                "owner": owner_ids[0], "connection": connection_id, "fingerprint": "a" * 64,
                "observed": datetime.now(UTC) - timedelta(hours=2),
                "models": json.dumps([{"id": "kept/model", "label": "kept/model", "source": "discovery"}]),
            })
        db.commit()
    client.app.state.connection_transport = httpx.MockTransport(
        lambda request: httpx.Response(500, content=b"synthetic-key workspace-secret raw-upstream"))

    failed = client.post(
        f"/api/v1/connections/{connection_id}/models/refresh", headers=headers(),
        json={"version": 1, "config_revision": 7, "auth_grant_revision": 3})
    cached = client.get(f"/api/v1/connections/{connection_id}/models", headers=headers())

    assert failed.status_code == 502
    assert failed.json()["detail"] == {"code": "provider_error", "message": "Model refresh failed."}
    assert "synthetic-key" not in failed.text and "workspace-secret" not in failed.text
    assert [item["id"] for item in cached.json()["models"]] == ["kept/model"]
    assert cached.json()["stale"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), [
    ("readiness_revision", 8),
    ("auth_grant_revision", 4),
])
async def test_refresh_losing_revision_race_returns_conflict_without_overwriting_cache(
        models_setup, field, value):
    client, _, _, connection, owner_ids, _ = models_setup
    connection_id = connection("openrouter")
    entered, release = asyncio.Event(), asyncio.Event()

    async def discovery(snapshot):
        entered.set()
        await release.wait()
        return ([{"id": "late/model", "label": "late/model", "source": "discovery"}], False, datetime.now(UTC))

    from app.services.model_catalog import ModelRefreshFailure, refresh_models
    with client.app.state.session_factory() as db:
        row = db.query(ProviderConnection).filter_by(id=connection_id).one()
        running = asyncio.create_task(refresh_models(db, row, owner_ids[0], discovery))
        await asyncio.wait_for(entered.wait(), 2)
        with client.app.state.session_factory() as edit:
            edit.query(ProviderConnection).filter_by(id=connection_id).update({field: value})
            edit.commit()
        release.set()
        with pytest.raises(ModelRefreshFailure, match="configuration_changed"):
            await running
    with client.app.state.session_factory() as db:
        assert db.execute(text(
            "SELECT COUNT(*) FROM connection_model_cache WHERE connection_id=:connection"),
            {"connection": connection_id}).scalar_one() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ("readiness_revision", "auth_grant_revision", "delete"))
async def test_failed_refresh_prioritizes_lost_snapshot_and_preserves_cache(
        models_setup, mutation):
    client, _, _, connection, owner_ids, sqlite_url = models_setup
    connection_id = connection("openrouter")
    kept_models = json.dumps([
        {"id": "kept/model", "label": "kept/model", "source": "discovery"},
    ])
    with client.app.state.session_factory() as db:
        db.execute(text("""INSERT INTO connection_model_cache
            (owner_id,connection_id,config_revision,auth_grant_revision,config_fingerprint,
             observed_at,truncated,models) VALUES
            (:owner,:connection,7,3,:fingerprint,:observed,0,:models)"""), {
                "owner": owner_ids[0], "connection": connection_id,
                "fingerprint": "b" * 64, "observed": datetime.now(UTC),
                "models": kept_models,
            })
        db.commit()

    entered, release = asyncio.Event(), asyncio.Event()

    async def failing_discovery(snapshot):
        entered.set()
        await release.wait()
        from app.services.model_catalog import ModelRefreshFailure
        raise ModelRefreshFailure("provider_error", "Model refresh failed.", 502)

    from app.services.model_catalog import ModelRefreshFailure, refresh_models
    with client.app.state.session_factory() as db:
        row = db.query(ProviderConnection).filter_by(id=connection_id).one()
        running = asyncio.create_task(refresh_models(db, row, owner_ids[0], failing_discovery))
        await asyncio.wait_for(entered.wait(), 2)
        if mutation == "delete":
            with sqlite3.connect(sqlite_url.removeprefix("sqlite:///")) as edit:
                edit.execute("PRAGMA foreign_keys=OFF")
                edit.execute("DELETE FROM provider_connections WHERE id=?", (connection_id,))
                edit.commit()
        else:
            with client.app.state.session_factory() as edit:
                edit.query(ProviderConnection).filter_by(id=connection_id).update({
                    mutation: 8 if mutation == "readiness_revision" else 4,
                })
                edit.commit()
        release.set()
        with pytest.raises(ModelRefreshFailure) as changed:
            await running

    assert changed.value.code == "configuration_changed"
    assert changed.value.status_code == 409
    with client.app.state.session_factory() as db:
        cached = db.execute(text(
            "SELECT config_revision,auth_grant_revision,config_fingerprint,models "
            "FROM connection_model_cache WHERE owner_id=:owner AND connection_id=:connection"), {
                "owner": owner_ids[0], "connection": connection_id,
            }).one()
    assert tuple(cached[:3]) == (7, 3, "b" * 64)
    assert json.loads(cached.models) == json.loads(kept_models)


def test_refresh_bounds_item_count_and_rejects_oversize_or_malformed_payload(models_setup):
    client, _, headers, connection, _, _ = models_setup
    connection_id = connection("openrouter")
    path = f"/api/v1/connections/{connection_id}/models/refresh"
    body = {"version": 1, "config_revision": 7, "auth_grant_revision": 3}

    client.app.state.connection_transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": [{"id": f"vendor/model-{n}"} for n in range(205)]}))
    bounded = client.post(path, headers=headers(), json=body)
    assert bounded.status_code == 200
    assert bounded.json()["truncated"] is True and len(bounded.json()["models"]) == 200

    for payload in (b"x" * 1_000_001, json.dumps({"unexpected": []}).encode()):
        client.app.state.connection_transport = httpx.MockTransport(
            lambda request, payload=payload: httpx.Response(200, content=payload))
        failed = client.post(path, headers=headers(), json=body)
        assert failed.status_code == 502
        assert failed.json()["detail"]["code"] == "invalid_response"


def test_configured_and_generic_manual_fallback_never_invent_remote_discovery(models_setup):
    client, secrets, headers, connection, _, _ = models_setup
    for provider_id, expected in (("zai", ["glm-5.1", "glm-5.2", "glm-5.3"]), ("compatible", [])):
        connection_id = connection(provider_id, base_url="https://example.test/v1" if provider_id == "compatible" else None)
        client.app.state.connection_transport = httpx.MockTransport(
            lambda request: pytest.fail("configured strategy must not call a provider"))
        response = client.post(
            f"/api/v1/connections/{connection_id}/models/refresh", headers=headers(),
            json={"version": 1, "config_revision": 7, "auth_grant_revision": 3})
        assert response.status_code == 200, response.text
        assert response.json()["observed_at"] is None
        assert [item["id"] for item in response.json()["models"]] == expected
    assert secrets.reads == 0


def test_discovery_does_not_create_readiness_or_test_history(models_setup):
    client, _, headers, connection, _, _ = models_setup
    connection_id = connection("openrouter", default_model_id="openai/gpt-5")
    before_readiness = client.get("/api/v1/readiness", headers=headers()).json()
    before_history = client.get(
        f"/api/v1/connections/{connection_id}/test-history", headers=headers()).json()
    client.app.state.connection_transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": [{"id": "openai/gpt-5"}]}))

    refreshed = client.post(
        f"/api/v1/connections/{connection_id}/models/refresh", headers=headers(),
        json={"version": 1, "config_revision": 7, "auth_grant_revision": 3})

    assert refreshed.status_code == 200, refreshed.text
    assert client.get("/api/v1/readiness", headers=headers()).json() == before_readiness
    assert client.get(
        f"/api/v1/connections/{connection_id}/test-history", headers=headers()).json() == before_history
