"""Versioned non-secret workspace blueprints use disposable SQLite only."""

import json
from datetime import datetime

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.collaboration import (
    ProjectRoomTemplate,
    ProjectRoomTemplateMember,
    ProviderConnection,
    RoomDraft,
    RoomEvent,
    RoomMembership,
)
from app.models.models import Agent, Conversation, Project, ProjectAgent, User


@pytest.fixture
def workspace_client(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "workspace-token")
    app = create_app(database_url=sqlite_url)
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            owner = User(email="workspace-owner@test.invalid", username="workspace-owner",
                         hashed_password="sentinel-login-secret")
            other = User(email="workspace-other@test.invalid", username="workspace-other",
                         hashed_password="other-secret")
            db.add_all([owner, other])
            db.flush()
            connection = ProviderConnection(
                user_id=owner.id, kind="openai", name="Primary", default_model_id="gpt-test",
                secret_ref="sentinel-secret-reference", enabled=True, auth_required=True,
                capabilities={
                    "declared": {"tools": True, "vision": False},
                    "verified": {"models": {"secret-model-snapshot": {"headers": "sentinel"}}},
                },
            )
            db.add(connection)
            db.flush()
            agent = Agent(
                user_id=owner.id, name="Planner", description="Plans work",
                system_prompt="Preserve this owner-authored prompt", model="gpt-test",
                connection_id=connection.id, model_id="gpt-test", temperature="0.4",
                max_tokens=4096,
            )
            inactive_agent = Agent(
                user_id=owner.id, name="Deleted", description="old", system_prompt="old",
                model="gpt-test", connection_id=connection.id, model_id="gpt-test",
                is_active=False,
            )
            db.add_all([agent, inactive_agent])
            db.flush()
            project = Project(user_id=owner.id, name="Launch", description="Ship it")
            inactive_project = Project(user_id=owner.id, name="Old", is_active=False)
            db.add_all([project, inactive_project])
            db.flush()
            db.add(ProjectAgent(project_id=project.id, agent_id=agent.id))
            template = ProjectRoomTemplate(
                owner_id=owner.id, project_id=project.id, name="Release",
                room_title="Release room", max_rounds=3, max_tool_calls=12,
                max_executor_seconds=180,
            )
            db.add(template)
            db.flush()
            db.add(ProjectRoomTemplateMember(template_id=template.id, agent_id=agent.id))
            room = Conversation(user_id=owner.id, project_id=project.id, title="Never export")
            db.add(room)
            db.flush()
            db.add(RoomMembership(room_id=room.id, agent_id=agent.id))
            db.add(RoomEvent(event_id="00000000-0000-0000-0000-000000000001",
                             room_id=room.id, seq=1, actor_type="user", actor_id=owner.id,
                             actor_name_snapshot="Owner", kind="message",
                             payload={"content": "sentinel conversation history"},
                             created_at=datetime(2026, 10, 4)))
            db.add(RoomDraft(owner_id=owner.id, room_id=room.id,
                             content="sentinel draft", version=1,
                             idempotency_key="00000000-0000-0000-0000-000000000002"))
            db.commit()
            ids = {"owner": owner.id, "other": other.id, "connection": connection.id,
                   "agent": agent.id, "inactive_agent": inactive_agent.id,
                   "project": project.id, "template": template.id}
        yield client, ids


def auth(client, owner_id):
    return {
        "X-Gamja-Session": client.app.state.local_session_token,
        "Authorization": f"Bearer {create_access_token({'sub': owner_id})}",
    }


def exported(client, ids):
    response = client.get("/api/v1/workspace/blueprint",
                          headers=auth(client, ids["owner"]))
    assert response.status_code == 200, response.text
    return response.json()


def test_export_is_deterministic_strict_and_excludes_secrets_and_runtime(workspace_client):
    client, ids = workspace_client
    first = exported(client, ids)
    second = exported(client, ids)
    assert first == second
    assert first == {
        "version": 1,
        "connections": [{
            "ref": "connection-1", "provider_kind": "openai", "label": "Primary",
            "default_model_id": "gpt-test", "base_url": None, "allow_local_http": False,
            "auth_required": True, "enabled": True,
            "capabilities": {"tools": True, "vision": False},
        }],
        "agents": [{
            "ref": "agent-1", "name": "Planner", "description": "Plans work",
            "system_prompt": "Preserve this owner-authored prompt",
            "connection_ref": "connection-1", "model_id": "gpt-test",
            "temperature": "0.4", "max_tokens": 4096,
        }],
        "projects": [{
            "ref": "project-1", "name": "Launch", "description": "Ship it",
            "agent_refs": ["agent-1"],
            "room_templates": [{
                "ref": "template-1", "name": "Release", "room_title": "Release room",
                "agent_refs": ["agent-1"], "max_rounds": 3,
                "max_tool_calls": 12, "max_executor_seconds": 180,
            }],
        }],
    }
    rendered = json.dumps(first, sort_keys=True)
    for forbidden in ("sentinel-secret-reference", "sentinel-login-secret",
                      "secret-model-snapshot", "sentinel conversation history",
                      "sentinel draft", "secret_ref", "has_secret", "verified"):
        assert forbidden not in rendered


def test_mixed_typed_export_v2_round_trip_is_non_secret_and_credentials_unset(workspace_client):
    client, ids = workspace_client
    with client.app.state.session_factory() as db:
        db.add_all([
            ProviderConnection(
                user_id=ids["owner"], kind="compatible", name="OpenRouter typed",
                base_url="https://openrouter.ai/api/v1", default_model_id="openrouter/test",
                secret_ref="sentinel-openrouter-reference", enabled=True, auth_required=True,
                capabilities={"declared": {"tools": True},
                              "verified": {"models": {"sentinel-cache-model": {}}},
                              "account": "sentinel-account"},
                readiness_revision=7, provider_id="openrouter",
                protocol_id="openai_chat_completions", auth_method_id="api_key",
                public_config_version=1, public_config={}, auth_grant_revision=4,
                credential_state="present",
            ),
            ProviderConnection(
                user_id=ids["owner"], kind="compatible", name="Ollama typed",
                base_url="http://localhost:11434/v1", default_model_id="llama-test",
                enabled=False, auth_required=False,
                capabilities={"declared": {"temperature": True}, "verified": {}},
                readiness_revision=3, provider_id="ollama",
                protocol_id="openai_chat_completions", auth_method_id="local_none",
                public_config_version=1, public_config={}, auth_grant_revision=2,
                credential_state="not_required",
            ),
        ])
        db.commit()

    blueprint = exported(client, ids)
    assert blueprint["version"] == 2
    assert blueprint["connections"] == [
        {
            "ref": "connection-1", "label": "Primary", "default_model_id": "gpt-test",
            "enabled": True, "capabilities": {"tools": True, "vision": False},
            "public_config": {"version": 1, "provider_id": "openai",
                              "protocol_id": "openai_chat_completions",
                              "auth_method_id": "api_key", "fields": {}},
        },
        {
            "ref": "connection-2", "label": "OpenRouter typed",
            "default_model_id": "openrouter/test", "enabled": True,
            "capabilities": {"tools": True},
            "public_config": {"version": 1, "provider_id": "openrouter",
                              "protocol_id": "openai_chat_completions",
                              "auth_method_id": "api_key", "fields": {}},
        },
        {
            "ref": "connection-3", "label": "Ollama typed",
            "default_model_id": "llama-test", "enabled": False,
            "capabilities": {"temperature": True},
            "public_config": {"version": 1, "provider_id": "ollama",
                              "protocol_id": "openai_chat_completions",
                              "auth_method_id": "local_none", "fields": {}},
        },
    ]
    rendered = json.dumps(blueprint, sort_keys=True)
    for forbidden in ("sentinel-openrouter-reference", "sentinel-cache-model",
                      "sentinel-account", "secret_ref", "auth_grant_revision",
                      "credential_state", "readiness_revision", "verified"):
        assert forbidden not in rendered

    other = auth(client, ids["other"])
    preview = client.post("/api/v1/workspace/blueprint/preview", headers=other,
                          json=blueprint)
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert plan["warnings"] == [{"code": "connections_need_credentials", "count": 2}]
    applied = client.post("/api/v1/workspace/blueprint/apply", headers=other,
                          json={"blueprint": blueprint, "plan_hash": plan["plan_hash"]})
    assert applied.status_code == 201, applied.text
    assert applied.json()["credentials_unset"] == 2
    with client.app.state.session_factory() as db:
        rows = db.query(ProviderConnection).filter_by(user_id=ids["other"]).order_by(
            ProviderConnection.id).all()
        assert [(row.provider_id, row.protocol_id, row.auth_method_id,
                 row.auth_required, row.secret_ref, row.auth_grant_revision,
                 row.readiness_revision, row.credential_state) for row in rows] == [
            ("openai", "openai_chat_completions", "api_key", True, None, 0, 1, "missing"),
            ("openrouter", "openai_chat_completions", "api_key", True, None, 0, 1, "missing"),
            ("ollama", "openai_chat_completions", "local_none", False, None, 0, 1,
             "not_required"),
        ]
        assert rows[1].capabilities == {"declared": {"tools": True}, "verified": {}}


def test_mixed_v2_export_rejects_unrepresentable_legacy_auth_semantics(workspace_client):
    client, ids = workspace_client
    with client.app.state.session_factory() as db:
        legacy = db.get(ProviderConnection, ids["connection"])
        legacy.auth_required = False
        legacy.secret_ref = None
        db.add(ProviderConnection(
            user_id=ids["owner"], kind="compatible", name="Typed trigger",
            base_url="https://openrouter.ai/api/v1", default_model_id="openrouter/test",
            enabled=True, auth_required=True, capabilities={}, readiness_revision=1,
            provider_id="openrouter", protocol_id="openai_chat_completions",
            auth_method_id="api_key", public_config_version=1, public_config={},
            auth_grant_revision=0, credential_state="missing",
        ))
        db.commit()
    response = client.get("/api/v1/workspace/blueprint",
                          headers=auth(client, ids["owner"]))
    assert response.status_code == 409
    assert response.json() == {"detail": {"code": "workspace_source_invalid"}}


def test_preview_is_owner_bound_and_resolves_create_only_collisions(workspace_client):
    client, ids = workspace_client
    blueprint = exported(client, ids)
    own = client.post("/api/v1/workspace/blueprint/preview",
                      headers=auth(client, ids["owner"]), json=blueprint)
    assert own.status_code == 200, own.text
    plan = own.json()
    assert plan["counts"] == {
        "connections": 1, "agents": 1, "projects": 1,
        "project_assignments": 1, "room_templates": 1, "template_members": 1,
    }
    assert plan["connections"] == [{
        "ref": "connection-1", "source_name": "Primary",
        "target_name": "Primary (imported 2)",
    }]
    assert plan["agents"][0]["target_name"] == "Planner (imported 2)"
    assert plan["projects"][0]["target_name"] == "Launch (imported 2)"
    assert plan["room_templates"][0]["target_name"] == "Release"
    assert plan["warnings"] == [{"code": "connections_need_credentials", "count": 1}]
    assert len(plan["plan_hash"]) == 64

    foreign = client.post("/api/v1/workspace/blueprint/preview",
                          headers=auth(client, ids["other"]), json=blueprint)
    assert foreign.status_code == 200
    assert foreign.json()["connections"][0]["target_name"] == "Primary"
    assert foreign.json()["plan_hash"] != plan["plan_hash"]


def test_apply_uses_exact_plan_creates_fresh_rows_and_leaves_credentials_unset(workspace_client):
    client, ids = workspace_client
    blueprint = exported(client, ids)
    headers = auth(client, ids["other"])
    plan = client.post("/api/v1/workspace/blueprint/preview",
                       headers=headers, json=blueprint).json()
    applied = client.post("/api/v1/workspace/blueprint/apply", headers=headers,
                          json={"blueprint": blueprint, "plan_hash": plan["plan_hash"]})
    assert applied.status_code == 201, applied.text
    result = applied.json()
    assert result["plan_hash"] == plan["plan_hash"]
    assert result["counts"] == plan["counts"]
    assert result["credentials_unset"] == 1

    with client.app.state.session_factory() as db:
        connection = db.get(ProviderConnection, result["connections"][0]["id"])
        agent = db.get(Agent, result["agents"][0]["id"])
        project = db.get(Project, result["projects"][0]["id"])
        template = db.get(ProjectRoomTemplate, result["room_templates"][0]["id"])
        assert connection.user_id == ids["other"] and connection.secret_ref is None
        assert connection.capabilities == {"declared": {"tools": True, "vision": False},
                                           "verified": {}}
        assert agent.user_id == ids["other"] and agent.connection_id == connection.id
        assert project.user_id == ids["other"]
        assert db.query(ProjectAgent).filter_by(project_id=project.id, agent_id=agent.id).count() == 1
        assert template.owner_id == ids["other"] and template.project_id == project.id
        assert db.query(ProjectRoomTemplateMember).filter_by(
            template_id=template.id, agent_id=agent.id).count() == 1
        assert db.query(Conversation).filter_by(user_id=ids["other"]).count() == 0


def test_legacy_v1_round_trip_keeps_agent_exact_model_when_connection_default_is_empty(
        workspace_client):
    client, ids = workspace_client
    with client.app.state.session_factory() as db:
        db.get(ProviderConnection, ids["connection"]).default_model_id = ""
        db.commit()
    blueprint = exported(client, ids)
    assert blueprint["version"] == 1
    assert blueprint["connections"][0]["default_model_id"] == ""
    assert blueprint["agents"][0]["model_id"] == "gpt-test"

    other = auth(client, ids["other"])
    preview = client.post("/api/v1/workspace/blueprint/preview", headers=other,
                          json=blueprint)
    assert preview.status_code == 200, preview.text
    applied = client.post("/api/v1/workspace/blueprint/apply", headers=other, json={
        "blueprint": blueprint, "plan_hash": preview.json()["plan_hash"],
    })
    assert applied.status_code == 201, applied.text
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, applied.json()["connections"][0]["id"])
        agent = db.get(Agent, applied.json()["agents"][0]["id"])
        assert row.default_model_id == ""
        assert agent.connection_id == row.id and agent.model_id == "gpt-test"


def test_apply_rejects_stale_or_other_owner_plan_without_partial_rows(workspace_client):
    client, ids = workspace_client
    blueprint = exported(client, ids)
    other = auth(client, ids["other"])
    plan = client.post("/api/v1/workspace/blueprint/preview", headers=other,
                       json=blueprint).json()
    with client.app.state.session_factory() as db:
        db.add(ProviderConnection(user_id=ids["other"], kind="openai", name="Primary",
                                  default_model_id="gpt-other", enabled=True,
                                  auth_required=True, capabilities={}))
        db.commit()
        before = (db.query(ProviderConnection).filter_by(user_id=ids["other"]).count(),
                  db.query(Agent).filter_by(user_id=ids["other"]).count(),
                  db.query(Project).filter_by(user_id=ids["other"]).count())
    stale = client.post("/api/v1/workspace/blueprint/apply", headers=other,
                        json={"blueprint": blueprint, "plan_hash": plan["plan_hash"]})
    assert stale.status_code == 409
    assert stale.json()["detail"] == {"code": "workspace_plan_stale"}

    own_plan = client.post("/api/v1/workspace/blueprint/preview",
                           headers=auth(client, ids["owner"]), json=blueprint).json()
    wrong_owner = client.post("/api/v1/workspace/blueprint/apply", headers=other,
                              json={"blueprint": blueprint, "plan_hash": own_plan["plan_hash"]})
    assert wrong_owner.status_code == 409
    with client.app.state.session_factory() as db:
        assert (db.query(ProviderConnection).filter_by(user_id=ids["other"]).count(),
                db.query(Agent).filter_by(user_id=ids["other"]).count(),
                db.query(Project).filter_by(user_id=ids["other"]).count()) == before


@pytest.mark.parametrize("mutate", ["version", "secret", "secret_url", "missing_ref", "duplicate_ref"])
def test_preview_rejects_unsupported_secret_and_invalid_references_without_echo(
        workspace_client, mutate):
    client, ids = workspace_client
    blueprint = exported(client, ids)
    if mutate == "version":
        blueprint["version"] = 3
    elif mutate == "secret":
        blueprint["connections"][0]["api_key"] = "sentinel-import-secret"
    elif mutate == "secret_url":
        blueprint["connections"][0]["provider_kind"] = "compatible"
        blueprint["connections"][0]["base_url"] = "https://example.invalid/v1?token=sentinel-import-secret"
    elif mutate == "missing_ref":
        blueprint["agents"][0]["connection_ref"] = "connection-missing"
    else:
        blueprint["agents"].append(dict(blueprint["agents"][0]))
    response = client.post("/api/v1/workspace/blueprint/preview",
                           headers=auth(client, ids["other"]), json=blueprint)
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "workspace_invalid_blueprint"}}
    assert "sentinel-import-secret" not in response.text


@pytest.mark.parametrize("provider_kind", ["openai", "anthropic", "gemini", "xai", "compatible"])
def test_legacy_v1_import_preserves_each_provider_kind(workspace_client, provider_kind):
    client, ids = workspace_client
    blueprint = exported(client, ids)
    item = blueprint["connections"][0]
    item["provider_kind"] = provider_kind
    item["base_url"] = "https://example.invalid/v1" if provider_kind == "compatible" else None
    other = auth(client, ids["other"])
    preview = client.post("/api/v1/workspace/blueprint/preview", headers=other,
                          json=blueprint)
    assert preview.status_code == 200, preview.text
    applied = client.post("/api/v1/workspace/blueprint/apply", headers=other, json={
        "blueprint": blueprint, "plan_hash": preview.json()["plan_hash"],
    })
    assert applied.status_code == 201, applied.text
    with client.app.state.session_factory() as db:
        row = db.get(ProviderConnection, applied.json()["connections"][0]["id"])
        assert row.kind == provider_kind
        assert row.provider_id is None and row.secret_ref is None


def test_v2_rejects_nested_sensitive_or_malformed_public_config_without_echo(workspace_client):
    client, ids = workspace_client
    blueprint = {
        "version": 2,
        "connections": [{
            "ref": "connection-1", "label": "Sensitive", "default_model_id": "model",
            "enabled": True, "capabilities": {},
            "public_config": {"version": 1, "provider_id": "openrouter",
                              "protocol_id": "openai_chat_completions",
                              "auth_method_id": "api_key", "fields": {},
                              "secret_ref": "sentinel-nested-secret"},
        }],
        "agents": [], "projects": [],
    }
    response = client.post("/api/v1/workspace/blueprint/preview",
                           headers=auth(client, ids["other"]), json=blueprint)
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "workspace_invalid_blueprint"}}
    assert "sentinel-nested-secret" not in response.text


def test_export_rejects_unavailable_active_dependencies_instead_of_omitting_them(workspace_client):
    client, ids = workspace_client
    with client.app.state.session_factory() as db:
        db.get(Agent, ids["agent"]).is_active = False
        db.commit()
    response = client.get("/api/v1/workspace/blueprint",
                          headers=auth(client, ids["owner"]))
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "workspace_source_invalid"}


def test_export_rejects_unsafe_legacy_public_url_without_echo(workspace_client):
    client, ids = workspace_client
    with client.app.state.session_factory() as db:
        connection = db.get(ProviderConnection, ids["connection"])
        connection.kind = "compatible"
        connection.base_url = "https://example.invalid/v1?token=sentinel-legacy-secret"
        db.commit()
    response = client.get("/api/v1/workspace/blueprint",
                          headers=auth(client, ids["owner"]))
    assert response.status_code == 409
    assert response.json() == {"detail": {"code": "workspace_source_invalid"}}
    assert "sentinel-legacy-secret" not in response.text


def test_apply_rolls_back_every_created_row_on_child_failure(workspace_client):
    client, ids = workspace_client
    blueprint = exported(client, ids)
    headers = auth(client, ids["other"])
    plan = client.post("/api/v1/workspace/blueprint/preview", headers=headers,
                       json=blueprint).json()

    def fail_template(_mapper, _connection, _target):
        raise RuntimeError("synthetic workspace apply failure")

    event.listen(ProjectRoomTemplate, "before_insert", fail_template)
    try:
        with pytest.raises(RuntimeError, match="synthetic workspace apply failure"):
            client.post("/api/v1/workspace/blueprint/apply", headers=headers,
                        json={"blueprint": blueprint, "plan_hash": plan["plan_hash"]})
    finally:
        event.remove(ProjectRoomTemplate, "before_insert", fail_template)
    with client.app.state.session_factory() as db:
        assert db.query(ProviderConnection).filter_by(user_id=ids["other"]).count() == 0
        assert db.query(Agent).filter_by(user_id=ids["other"]).count() == 0
        assert db.query(Project).filter_by(user_id=ids["other"]).count() == 0
