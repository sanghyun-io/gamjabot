"""Canonical event log checks use synthetic SQLite and app data only."""

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID
import httpx

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.collaboration import ProviderConnection, RoomEvent
from app.models.models import Agent, Conversation, Message, Project, User


@pytest.fixture
def event_client(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "event-test-local-token")
    app = create_app(database_url=sqlite_url)
    with TestClient(app) as client:
        db = app.state.session_factory()
        try:
            owner = User(email="events-owner@test.invalid", username="events-owner", hashed_password="x")
            other = User(email="events-other@test.invalid", username="events-other", hashed_password="x")
            db.add_all((owner, other))
            db.flush()
            project = Project(user_id=owner.id, name="Group")
            db.add(project)
            db.flush()
            room = Conversation(user_id=owner.id, project_id=project.id, title="A")
            db.add(room)
            db.commit()
            ids = owner.id, other.id, room.id
        finally:
            db.close()
        yield client, ids


def headers(client, user_id, **extra):
    return {"X-Gamja-Session": client.app.state.local_session_token,
            "Authorization": f"Bearer {create_access_token({'sub': user_id})}", **extra}


def ws_headers(client, user_id):
    return {"origin": "http://localhost:3000", "sec-websocket-protocol":
            f"gamja.chat, gamja.local.{client.app.state.local_session_token}, gamja.user.{create_access_token({'sub': user_id})}"}


def join_agent(client, owner, room):
    db = client.app.state.session_factory()
    try:
        agent = Agent(user_id=owner, name="Ada", description="d", system_prompt="p", model="gpt-4")
        db.add(agent)
        db.commit()
        agent_id = agent.id
    finally:
        db.close()
    assert client.post(f"/api/v1/conversations/{room}/members/{agent_id}", headers=headers(client, owner)).status_code == 201
    return agent_id


def test_parallel_posts_have_gap_free_sequences_and_stable_device(event_client, sqlite_url):
    client, (owner, _, room) = event_client
    path = f"/api/v1/conversations/{room}/events"

    def send(index):
        response = client.post(path, json={"content": f"message {index}"}, headers=headers(client, owner, **{"Idempotency-Key": f"key-{index}"}))
        assert response.status_code == 201, response.text
        return response.json()

    with ThreadPoolExecutor(max_workers=8) as pool:
        posted = list(pool.map(send, range(20)))
    assert sorted(event["seq"] for event in posted) == list(range(1, 21))
    assert len({event["event_id"] for event in posted}) == 20
    assert all(UUID(event["event_id"]).version == 4 for event in posted)
    device_ids = {event["device_id"] for event in posted}
    assert len(device_ids) == 1
    assert UUID(next(iter(device_ids)))
    replay = client.get(f"{path}?after_seq=10&limit=5", headers=headers(client, owner)).json()
    assert [event["seq"] for event in replay] == [11, 12, 13, 14, 15]
    assert [event["event_id"] for event in replay] == [next(event["event_id"] for event in posted if event["seq"] == n) for n in range(11, 16)]
    client.__exit__(None, None, None)  # End the first backend before restart.
    with TestClient(create_app(database_url=sqlite_url)) as restarted:
        more = restarted.post(path, json={"content": "after restart"}, headers=headers(restarted, owner))
        assert more.status_code == 201
        assert more.json()["device_id"] == next(iter(device_ids))
        assert more.json()["seq"] == 21


def test_duplicate_idempotency_key_returns_same_event_and_does_not_overwrite(event_client):
    client, (owner, _, room) = event_client
    path = f"/api/v1/conversations/{room}/events"
    auth = headers(client, owner, **{"Idempotency-Key": "repeat"})
    first = client.post(path, json={"content": "first"}, headers=auth)
    second = client.post(path, json={"content": "first"}, headers=auth)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert client.post(path, json={"content": "changed"}, headers=auth).status_code == 409
    assert client.get(path, headers=headers(client, owner)).json() == [first.json()]


def test_generic_event_idempotency_requires_the_entire_original_payload(event_client):
    client, (_, _, room) = event_client
    from app.services.events import ActorRef, append_event
    from fastapi import HTTPException

    db = client.app.state.session_factory()
    try:
        actor = ActorRef("system", None)
        first = append_event(db, room, actor, "activity", {"a": 1, "b": 2}, "whole-payload")
        original_id = first.event_id
        assert append_event(db, room, actor, "activity", {"a": 1, "b": 2}, "whole-payload").event_id == original_id
        for different in ({"a": 1}, {"a": 1, "b": 2, "c": 3}, {"a": 1, "b": 3}):
            with pytest.raises(HTTPException) as rejected:
                append_event(db, room, actor, "activity", different, "whole-payload")
            assert rejected.value.status_code == 409
        assert db.query(RoomEvent).filter_by(room_id=room).count() == 1
    finally:
        db.close()


def test_missing_provider_key_is_typed_failure_on_http_and_ws(event_client, monkeypatch):
    client, (owner, _, room) = event_client
    agent_id = join_agent(client, owner, room)
    with client.app.state.session_factory() as db:
        connection = ProviderConnection(user_id=owner, kind="openai", name="Missing key", default_model_id="exact",
                                        enabled=True, auth_required=True, capabilities={"declared": {}, "verified": {}})
        db.add(connection)
        db.flush()
        agent = db.get(Agent, agent_id)
        agent.connection_id, agent.model_id = connection.id, "exact"
        db.commit()
    response = client.post(f"/api/v1/conversations/{room}/messages", json={"content": "hello"}, headers=headers(client, owner))
    assert response.status_code == 502
    assert "Credential is missing" in response.text
    events = client.get(f"/api/v1/conversations/{room}/events", headers=headers(client, owner)).json()
    assert [(event["kind"], event["actor_type"]) for event in events] == [("message", "user"), ("failure", "agent")]
    assert events[1]["payload"]["code"] == "missing_secret"
    db = client.app.state.session_factory()
    try:
        assert [(message.role, message.content) for message in db.query(Message).filter_by(conversation_id=room)] == [("user", "hello")]
    finally:
        db.close()
    with client.websocket_connect(f"/api/v1/conversations/{room}/ws?after_seq=2", headers=ws_headers(client, owner)) as socket:
        socket.send_json({"type": "message", "content": "again"})
        received = [socket.receive_json(), socket.receive_json()]
        assert [(event["kind"], event["actor_type"]) for event in received] == [("message", "user"), ("failure", "agent")]
        assert received[1]["actor_id"] == agent_id
    assert [event["seq"] for event in client.get(f"/api/v1/conversations/{room}/events", headers=headers(client, owner)).json()] == [1, 2, 3, 4]


def test_upstream_exception_is_redacted_and_success_text_is_not_prefix_parsed(event_client, monkeypatch):
    client, (owner, _, room) = event_client
    agent_id = join_agent(client, owner, room)
    with client.app.state.session_factory() as db:
        connection = ProviderConnection(user_id=owner, kind="compatible", name="Local", base_url="http://127.0.0.1:8765/v1",
                                        default_model_id="exact", enabled=True, auth_required=False,
                                        capabilities={"declared": {"max_tokens": True, "temperature": True},
                                                      "verified": {"model_id": "exact", "base_url": "http://127.0.0.1:8765/v1",
                                                                   "max_tokens": True, "temperature": True}})
        db.add(connection)
        db.flush()
        agent = db.get(Agent, agent_id)
        agent.connection_id, agent.model_id = connection.id, "exact"
        db.commit()
    def failed_provider(wire):
        raise httpx.ConnectError("secret=TOPSECRET upstream token", request=wire)
    client.app.state.connection_transport = httpx.MockTransport(failed_provider)
    response = client.post(f"/api/v1/conversations/{room}/messages", json={"content": "one"}, headers=headers(client, owner))
    assert response.status_code == 502
    events = client.get(f"/api/v1/conversations/{room}/events", headers=headers(client, owner)).json()
    assert events[-1]["kind"] == "failure"
    assert "TOPSECRET" not in response.text
    assert "TOPSECRET" not in str(events)

    from app.services.providers import ProviderResult
    async def literal_model_text(request):
        return ProviderResult(text="Error: this is the model's literal answer")

    client.app.state.scheduler.provider = literal_model_text
    response = client.post(f"/api/v1/conversations/{room}/messages", json={"content": "two"}, headers=headers(client, owner))
    assert response.status_code == 201
    assert response.json()[0]["content"] == "Error: this is the model's literal answer"
    events = client.get(f"/api/v1/conversations/{room}/events", headers=headers(client, owner)).json()
    assert events[-1]["kind"] == "message"


def test_parallel_retries_with_same_key_commit_one_event(event_client):
    client, (owner, _, room) = event_client
    path = f"/api/v1/conversations/{room}/events"
    auth = headers(client, owner, **{"Idempotency-Key": "same-key"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        pair = list(pool.map(lambda _: client.post(path, json={"content": "same"}, headers=auth), range(2)))
    assert all(response.status_code == 201 for response in pair)
    assert pair[0].json() == pair[1].json()
    assert [event["seq"] for event in client.get(path, headers=headers(client, owner)).json()] == [1]


def test_event_endpoints_deny_foreign_user_origin_and_missing_auth(event_client):
    client, (owner, other, room) = event_client
    path = f"/api/v1/conversations/{room}/events"
    assert client.post(path, json={"content": "secret"}, headers=headers(client, owner)).status_code == 201
    for auth in (headers(client, other), {"X-Gamja-Session": client.app.state.local_session_token},
                 headers(client, owner, Origin="https://evil.example")):
        for method in (client.get, lambda p, headers: client.post(p, json={"content": "forged", "actor_id": other, "seq": 900}, headers=headers)):
            response = method(path, headers=auth)
            assert response.status_code in (401, 403, 404)
            assert "secret" not in response.text
    for auth in (ws_headers(client, other), {"origin": "http://localhost:3000"},
                 {**ws_headers(client, owner), "origin": "https://evil.example"}):
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(f"/api/v1/conversations/{room}/ws?after_seq=0", headers=auth):
                pass
        assert rejected.value.code == 1008


def test_two_subscribers_receive_http_and_ws_writes_after_cursor(event_client):
    client, (owner, _, room) = event_client
    path = f"/api/v1/conversations/{room}/events"
    assert client.post(path, json={"content": "before"}, headers=headers(client, owner)).status_code == 201
    with client.websocket_connect(f"/api/v1/conversations/{room}/ws?after_seq=1", headers=ws_headers(client, owner)) as first:
        with client.websocket_connect(f"/api/v1/conversations/{room}/ws?after_seq=1", headers=ws_headers(client, owner)) as second:
            http_event = client.post(path, json={"content": "http"}, headers=headers(client, owner)).json()
            assert first.receive_json()["event_id"] == http_event["event_id"]
            assert second.receive_json()["event_id"] == http_event["event_id"]
            first.send_json({"type": "message", "content": "socket", "idempotency_key": "ws-one"})
            from_first = first.receive_json()
            from_second = second.receive_json()
            assert from_first["event_id"] == from_second["event_id"]
            assert from_first["seq"] == 3
    assert [event["seq"] for event in client.get(f"{path}?after_seq=2", headers=headers(client, owner)).json()] == [3]


def test_reconnect_and_duplicate_ws_command_replay_only_new_event(event_client):
    client, (owner, _, room) = event_client
    path = f"/api/v1/conversations/{room}/events"
    first = client.post(path, json={"content": "first"}, headers=headers(client, owner, **{"Idempotency-Key": "repeat"})).json()
    with client.websocket_connect(f"/api/v1/conversations/{room}/ws?after_seq=1", headers=ws_headers(client, owner)) as socket:
        socket.send_json({"type": "message", "content": "first", "idempotency_key": "repeat"})
        socket.send_json({"type": "message", "content": "second", "idempotency_key": "new"})
        second = socket.receive_json()
        assert second["seq"] == 2
        assert second["content"] == "second"
    with client.websocket_connect(f"/api/v1/conversations/{room}/ws?after_seq=1", headers=ws_headers(client, owner)) as socket:
        assert socket.receive_json()["event_id"] == second["event_id"]
    assert [event["event_id"] for event in client.get(path, headers=headers(client, owner)).json()] == [first["event_id"], second["event_id"]]


def test_websocket_rejects_forged_actor_and_closes_when_room_stops(event_client):
    client, (owner, _, room) = event_client
    with client.websocket_connect(f"/api/v1/conversations/{room}/ws", headers=ws_headers(client, owner)) as socket:
        socket.send_json({"type": "message", "content": "forged", "actor_type": "system"})
        with pytest.raises(WebSocketDisconnect) as rejected:
            socket.receive_json()
        assert rejected.value.code == 1008
    assert client.get(f"/api/v1/conversations/{room}/events", headers=headers(client, owner)).json() == []
    with client.websocket_connect(f"/api/v1/conversations/{room}/ws", headers=ws_headers(client, owner)) as socket:
        assert client.post(f"/api/v1/conversations/{room}/stop", headers=headers(client, owner)).status_code == 204
        with pytest.raises(WebSocketDisconnect) as rejected:
            socket.receive_json()
        assert rejected.value.code == 1008


def test_actor_name_snapshot_and_archived_room_history(event_client):
    client, (owner, _, room) = event_client
    path = f"/api/v1/conversations/{room}/events"
    first = client.post(path, json={"content": "before rename"}, headers=headers(client, owner)).json()
    db = client.app.state.session_factory()
    try:
        db.get(User, owner).username = "renamed-owner"
        db.commit()
    finally:
        db.close()
    second = client.post(path, json={"content": "after rename"}, headers=headers(client, owner)).json()
    assert first["actor_name_snapshot"] == "events-owner"
    assert second["actor_name_snapshot"] == "renamed-owner"
    assert client.post(f"/api/v1/conversations/{room}/stop", headers=headers(client, owner)).status_code == 204
    assert [event["actor_name_snapshot"] for event in client.get(path, headers=headers(client, owner)).json()] == ["events-owner", "renamed-owner"]
    assert [message["content"] for message in client.get(f"/api/v1/conversations/{room}/messages", headers=headers(client, owner)).json()] == ["before rename", "after rename"]
    assert client.post(path, json={"content": "too late"}, headers=headers(client, owner)).status_code == 404
