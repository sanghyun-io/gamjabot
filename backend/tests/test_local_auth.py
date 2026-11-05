import secrets
import hashlib

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


@pytest.fixture
def secured_client(sqlite_url, monkeypatch):
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", token)
    from app.main import create_app

    with TestClient(create_app(database_url=sqlite_url)) as client:
        yield client


@pytest.mark.parametrize("path", ["/", "/health", "/docs", "/openapi.json", "/api/v1/auth/login"])
def test_all_http_paths_require_launch_token(secured_client, path):
    client = secured_client
    token = client.app.state.local_session_token
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-Gamja-Session": "wrong"}).status_code == 401
    assert client.get(path, headers={"X-Gamja-Session": token}).status_code != 401


def test_foreign_origin_cannot_use_token_or_preflight(secured_client):
    client = secured_client
    token = client.app.state.local_session_token
    assert client.get("/health", headers={"X-Gamja-Session": token, "Origin": "https://evil.example"}).status_code == 403
    response = client.options("/health", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "x-gamja-session"})
    assert response.status_code == 403


def test_owner_cli_capability_is_transport_only_and_rejects_browser_origin(secured_client):
    client = secured_client
    capability = "a" * 64
    client.app.state.cli_capability_hash = hashlib.sha256(capability.encode()).hexdigest()
    assert client.get("/health", headers={"X-Gamja-CLI-Capability": capability}).status_code == 200
    assert client.get("/health", headers={"X-Gamja-CLI-Capability": "b" * 64}).status_code == 401
    assert client.get("/health", headers={"X-Gamja-CLI-Capability": capability,
                                           "Origin": "tauri://localhost"}).status_code == 401


def test_allowed_preflight_discloses_no_route_data(secured_client):
    client = secured_client
    headers = {"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "x-gamja-session"}
    response = client.options("/health", headers=headers)
    assert response.status_code == 200
    assert "healthy" not in response.text
    assert client.get("/health", headers={"Origin": headers["Origin"]}).status_code == 401


@pytest.mark.parametrize("headers", [
    {"origin": "http://localhost:3000"},
    {"origin": "https://evil.example", "sec-websocket-protocol": "gamja.local.wrong"},
])
def test_websocket_rejects_missing_or_wrong_local_credentials_before_accept(secured_client, headers):
    client = secured_client
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect("/api/v1/conversations/1/ws", headers=headers):
            pass
    assert rejected.value.code == 1008


def test_websocket_rejects_foreign_origin_with_valid_local_token(secured_client):
    client = secured_client
    token = client.app.state.local_session_token
    headers = {"origin": "https://evil.example", "sec-websocket-protocol": f"gamja.chat, gamja.local.{token}"}
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect("/api/v1/conversations/1/ws", headers=headers):
            pass
    assert rejected.value.code == 1008


def test_websocket_rejects_unknown_inactive_and_foreign_rooms(secured_client):
    client = secured_client
    token = client.app.state.local_session_token
    from app.api.routes.auth import create_access_token
    from app.models.models import User, Project, Conversation

    db = client.app.state.session_factory()
    try:
        owner = User(email="owner@example.test", username="owner", hashed_password="unused")
        other = User(email="other@example.test", username="other", hashed_password="unused")
        db.add_all([owner, other])
        db.flush()
        project = Project(user_id=other.id, name="project")
        db.add(project)
        db.flush()
        active = Conversation(user_id=other.id, project_id=project.id, title="foreign", is_active=True)
        inactive = Conversation(user_id=other.id, project_id=project.id, title="inactive", is_active=False)
        db.add_all([active, inactive])
        db.commit()
        owner_id, other_id, active_id, inactive_id = owner.id, other.id, active.id, inactive.id
    finally:
        db.close()

    def headers(user_id):
        return {"origin": "http://localhost:3000", "sec-websocket-protocol": f"gamja.chat, gamja.local.{token}, gamja.user.{create_access_token({'sub': user_id})}"}

    for room_id, user_id in [(99999, other_id), (inactive_id, other_id), (active_id, owner_id)]:
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(f"/api/v1/conversations/{room_id}/ws", headers=headers(user_id)):
                pass
        assert rejected.value.code == 1008

    with client.websocket_connect(f"/api/v1/conversations/{active_id}/ws", headers=headers(other_id)) as socket:
        assert socket.accepted_subprotocol == "gamja.chat"
