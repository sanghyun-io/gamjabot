"""Room state tests use synthetic, disposable SQLite databases only."""

import asyncio
import threading
import json
import httpx
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.collaboration import RoomAgentRun, RoomEvent, RoomGrant, RoomMembership, ProviderConnection
from app.models.models import Agent, Conversation, Message, Project, ProjectAgent, User


@pytest.fixture
def room_client(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "room-test-local-token")
    app = create_app(database_url=sqlite_url)
    with TestClient(app) as client:
        db = app.state.session_factory()
        try:
            owner = User(email="room-owner@test.invalid", username="room-owner", hashed_password="x")
            other = User(email="room-other@test.invalid", username="room-other", hashed_password="x")
            db.add_all([owner, other])
            db.flush()
            project = Project(user_id=owner.id, name="Group")
            db.add(project)
            db.flush()
            agent = Agent(user_id=owner.id, name="Ada", description="d", system_prompt="p")
            foreign = Agent(user_id=other.id, name="Foreign", description="d", system_prompt="p")
            db.add_all([agent, foreign])
            db.flush()
            db.add(ProjectAgent(project_id=project.id, agent_id=agent.id))
            db.commit()
            ids = owner.id, other.id, project.id, agent.id, foreign.id
        finally:
            db.close()
        yield client, ids


def headers(client, user_id):
    return {
        "X-Gamja-Session": client.app.state.local_session_token,
        "Authorization": f"Bearer {create_access_token({'sub': user_id})}",
    }


def create_room(client, owner_id, project_id, title):
    response = client.post("/api/v1/conversations/", json={"project_id": project_id, "title": title}, headers=headers(client, owner_id))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_project_assignment_does_not_join_new_room_and_owner_checks(room_client):
    client, (owner, other, project, agent, foreign) = room_client
    room = create_room(client, owner, project, "A")
    own = headers(client, owner)
    stranger = headers(client, other)
    assert client.get(f"/api/v1/conversations/{room}/members", headers=own).json() == []
    assert client.post(f"/api/v1/conversations/{room}/members/{agent}", headers=stranger).status_code == 404
    assert client.post(f"/api/v1/conversations/{room}/members/{foreign}", headers=own).status_code == 404
    assert client.post(f"/api/v1/conversations/{room}/members/{agent}", headers=own).status_code == 201
    assert [member["agent_id"] for member in client.get(f"/api/v1/conversations/{room}/members", headers=own).json()] == [agent]


def test_room_member_list_exposes_durable_pause_state(room_client):
    client, (owner, _, project, agent, _) = room_client
    room = create_room(client, owner, project, 'Pause')
    own = headers(client, owner)
    client.post(f'/api/v1/conversations/{room}/members/{agent}', headers=own)
    assert client.get(f'/api/v1/conversations/{room}/members', headers=own).json()[0]['paused'] is False
    client.post(f'/api/v1/conversations/{room}/members/{agent}/pause', headers=own)
    assert client.get(f'/api/v1/conversations/{room}/members', headers=own).json()[0]['paused'] is True
    client.post(f'/api/v1/conversations/{room}/members/{agent}/resume', headers=own)
    assert client.get(f'/api/v1/conversations/{room}/members', headers=own).json()[0]['paused'] is False


def test_room_member_list_exposes_authoritative_run_state_only_to_owner(room_client):
    client, (owner, other, project, agent, _) = room_client
    room = create_room(client, owner, project, 'Status')
    own = headers(client, owner)
    client.post(f'/api/v1/conversations/{room}/members/{agent}', headers=own)
    path = f'/api/v1/conversations/{room}/members'
    assert client.get(path, headers=own).json()[0]['run_state'] == 'idle'
    assert client.get(path, headers=headers(client, other)).status_code == 404
    for state in ('running', 'waiting', 'paused', 'idle'):
        with client.app.state.session_factory() as db:
            run = db.get(RoomAgentRun, (room, agent))
            run.state = state
            db.commit()
        assert client.get(path, headers=own).json()[0]['run_state'] == state


@pytest.mark.parametrize("identity_matches", [True, False])
def test_legacy_http_uses_exact_saved_connection_once_with_model_metadata(room_client, monkeypatch, identity_matches):
    client, (owner, _, project, agent_id, _) = room_client
    room = create_room(client, owner, project, "Adapter room")
    own = headers(client, owner)
    assert client.post(f"/api/v1/conversations/{room}/members/{agent_id}", headers=own).status_code == 201
    with client.app.state.session_factory() as db:
        connection = ProviderConnection(user_id=owner, kind="compatible", name="Local", base_url="http://127.0.0.1:8765/v1",
                                        default_model_id="default", auth_required=False, enabled=True,
                                        capabilities={"declared": {"temperature": True, "max_tokens": True},
                                                      "verified": {"model_id": "claude-looking", "base_url": "http://127.0.0.1:8765/v1", "temperature": True, "max_tokens": True},
                                                      "model_features": {"claude-looking": {"model_id": "claude-looking", "base_url": "http://127.0.0.1:8765/v1", "config_revision": 1, "tested": {"text_generation": True}, "identity": "synthetic-model-identity"}}})
        db.add(connection)
        db.flush()
        agent = db.get(Agent, agent_id)
        agent.connection_id, agent.model_id = connection.id, "claude-looking"
        db.commit()
        connection_id = connection.id
    # Simulate only the local server's metadata response; the production
    # identity and feature checks still run before the MockTransport request.
    from app.services import model_features
    checked=[]
    async def synthetic_describe(config):
        checked.append((config.model_id, config.base_url, config.config_revision))
        return {"identity": "synthetic-model-identity" if identity_matches else "changed-model", "reported": {"text_generation": True}}
    monkeypatch.setattr(model_features, "describe", synthetic_describe)
    requests = []
    def handler(wire):
        requests.append(json.loads(wire.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]})
    client.app.state.connection_transport = httpx.MockTransport(handler)
    result = client.post(f"/api/v1/conversations/{room}/messages", headers=own, json={"content": "current"})
    if not identity_matches:
        assert result.status_code == 502 and result.json()["detail"]["code"] == "model_capabilities_changed"
        assert requests == []
        assert checked == [("claude-looking", "http://127.0.0.1:8765/v1", 1)]
        return
    assert result.status_code == 201, result.text
    assert result.json()[0]["content"] == "answer"
    assert len(requests) == 1
    assert sum(message.get("role") == "user" and message.get("content", "").endswith(": current") for message in requests[0]["messages"]) == 1
    assert checked == [("claude-looking", "http://127.0.0.1:8765/v1", 1)]
    events = client.get(f"/api/v1/conversations/{room}/events", headers=own).json()
    assert events[-1]["payload"]["model_use"] == {"connection_id": connection_id, "model_id": "claude-looking",
        "provider_kind": "compatible", "watermark_seq": 1}


@pytest.mark.asyncio
async def test_stalled_provider_in_one_room_does_not_block_other_room(room_client):
    client, (owner, _, project, agent_id, _) = room_client
    own = headers(client, owner)
    rooms = [create_room(client, owner, project, title) for title in ("slow", "fast")]
    for room in rooms:
        assert client.post(f"/api/v1/conversations/{room}/members/{agent_id}", headers=own).status_code == 201
    with client.app.state.session_factory() as db:
        connection = ProviderConnection(user_id=owner, kind="compatible", name="Local", base_url="http://127.0.0.1:8765/v1",
                                        default_model_id="exact", auth_required=False, enabled=True,
                                        capabilities={"declared": {"temperature": True, "max_tokens": True},
                                                      "verified": {"model_id": "exact", "base_url": "http://127.0.0.1:8765/v1", "temperature": True, "max_tokens": True}})
        db.add(connection)
        db.flush()
        agent = db.get(Agent, agent_id)
        agent.connection_id, agent.model_id = connection.id, "exact"
        db.commit()
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(wire):
        if "slow" in wire.content.decode():
            entered.set()
            await release.wait()
        return httpx.Response(200, json={"choices": [{"message": {"content": "reply"}}]})
    transport = httpx.MockTransport(handler)
    from app.services import room_service
    async def dispatch(room, content):
        with client.app.state.session_factory() as db:
            return await room_service.send_legacy_message(db, room, content, provider_transport=transport)
    slow = asyncio.create_task(dispatch(rooms[0], "slow"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        _, responses = await asyncio.wait_for(dispatch(rooms[1], "fast"), 1)
        assert [message.content for message in responses] == ["reply"]
    finally:
        release.set()
        await slow


@pytest.mark.asyncio
async def test_blocked_secret_read_in_one_room_does_not_hold_up_another(room_client):
    client, (owner, _, project, slow_agent_id, _) = room_client
    own = headers(client, owner)
    slow_room, fast_room = [create_room(client, owner, project, title) for title in ('slow-key', 'fast-key')]
    with client.app.state.session_factory() as db:
        fast_agent = Agent(user_id=owner, name='Fast', description='d', system_prompt='p')
        db.add(fast_agent)
        db.flush()
        slow_connection = ProviderConnection(user_id=owner, kind='compatible', name='Authenticated',
            base_url='http://127.0.0.1:8765/v1', default_model_id='exact', auth_required=True,
            secret_ref='synthetic-key-ref', enabled=True, capabilities={'declared': {}, 'verified': {}})
        fast_connection = ProviderConnection(user_id=owner, kind='compatible', name='Authless',
            base_url='http://127.0.0.1:8765/v1', default_model_id='exact', auth_required=False,
            enabled=True, capabilities={'declared': {}, 'verified': {}})
        db.add_all([slow_connection, fast_connection])
        db.flush()
        slow_agent = db.get(Agent, slow_agent_id)
        slow_agent.connection_id, slow_agent.model_id = slow_connection.id, 'exact'
        slow_agent.temperature, slow_agent.max_tokens = 'auto', 0
        fast_agent.connection_id, fast_agent.model_id = fast_connection.id, 'exact'
        fast_agent.temperature, fast_agent.max_tokens = 'auto', 0
        fast_agent_id = fast_agent.id
        db.commit()
    assert client.post(f'/api/v1/conversations/{slow_room}/members/{slow_agent_id}', headers=own).status_code == 201
    assert client.post(f'/api/v1/conversations/{fast_room}/members/{fast_agent_id}', headers=own).status_code == 201

    entered, release = threading.Event(), threading.Event()
    class SlowSecrets:
        def get_no_ui(self, ref):
            assert ref == 'synthetic-key-ref'
            entered.set()
            release.wait(3)
            return 'synthetic-key'

    transport = httpx.MockTransport(lambda wire: httpx.Response(200, json={
        'choices': [{'message': {'content': 'reply'}}]}))
    from app.services import room_service
    async def dispatch(room, content):
            with client.app.state.session_factory() as db:
                return await room_service.send_legacy_message(db, room, content, provider_store=SlowSecrets(),
                                                          provider_transport=transport, provider_cache=client.app.state.credential_cache)
    guard = threading.Timer(2, release.set)
    guard.start()
    slow = asyncio.create_task(dispatch(slow_room, 'slow'))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        _, replies = await dispatch(fast_room, 'fast')
        assert [reply.content for reply in replies] == ['reply']
        assert not release.is_set(), 'Fast room waited for the slow room credential read'
    finally:
        release.set()
        guard.cancel()
        guard.join()
        await slow


def test_same_agent_room_state_survives_concurrent_updates_and_restart(room_client, sqlite_url):
    client, (owner, _, project, agent, _) = room_client
    a = create_room(client, owner, project, "A")
    b = create_room(client, owner, project, "B")
    own = headers(client, owner)
    for room in (a, b):
        assert client.post(f"/api/v1/conversations/{room}/members/{agent}", headers=own).status_code == 201

    from app.services.room_service import get_run

    def write_state(room, memory, pending, checkpoint):
        db = client.app.state.session_factory()
        try:
            run = get_run(db, room, agent)
            run.memory = {"note": memory}
            run.pending_task = {"task": pending}
            run.checkpoint = {"position": checkpoint}
            db.add(RoomGrant(room_id=room, agent_id=agent, scope={"folder": memory}, decision="allow"))
            db.commit()
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda args: write_state(*args), [(a, "alpha", "one", 1), (b, "beta", "two", 2)]))
    assert client.post(f"/api/v1/conversations/{a}/members/{agent}/pause", headers=own).status_code == 200

    client.__exit__(None, None, None)  # End the first backend before restart.
    with TestClient(create_app(database_url=sqlite_url)) as restarted:
        db = restarted.app.state.session_factory()
        try:
            run_a, run_b = get_run(db, a, agent), get_run(db, b, agent)
            assert (run_a.memory, run_a.pending_task, run_a.checkpoint, run_a.paused) == ({"note": "alpha"}, {"task": "one"}, {"position": 1}, True)
            assert (run_b.memory, run_b.pending_task, run_b.checkpoint, run_b.paused) == ({"note": "beta"}, {"task": "two"}, {"position": 2}, False)
            assert [(g.room_id, g.scope) for g in db.query(RoomGrant).order_by(RoomGrant.room_id)] == [(a, {"folder": "alpha"}), (b, {"folder": "beta"})]
        finally:
            db.close()


def test_removal_invalidates_inflight_result_and_deactivation_keeps_history(room_client):
    client, (owner, _, project, agent, _) = room_client
    a = create_room(client, owner, project, "A")
    b = create_room(client, owner, project, "B")
    own = headers(client, owner)
    for room in (a, b):
        client.post(f"/api/v1/conversations/{room}/members/{agent}", headers=own)

    from app.services.room_service import begin_turn, finish_turn, eligible_agents

    db = client.app.state.session_factory()
    try:
        generation = begin_turn(db, a, agent)
    finally:
        db.close()
    assert client.delete(f"/api/v1/conversations/{a}/members/{agent}", headers=own).status_code == 204
    db = client.app.state.session_factory()
    try:
        assert finish_turn(db, a, agent, generation, "late answer") is None
        assert eligible_agents(db, a) == []
        assert [entry.id for entry in eligible_agents(db, b)] == [agent]
        assert db.query(Message).filter(Message.conversation_id.in_([a, b])).count() == 0
        db.query(Agent).filter(Agent.id == agent).update({"name": "Renamed", "is_active": False})
        db.add(Message(conversation_id=b, agent_id=agent, role="assistant", content="old answer"))
        db.commit()
        assert db.query(Message).filter_by(conversation_id=b, agent_id=agent).one().content == "old answer"
        assert eligible_agents(db, b) == []
    finally:
        db.close()


def test_pause_resume_and_stop_are_room_local(room_client):
    client, (owner, _, project, agent, _) = room_client
    a = create_room(client, owner, project, "A")
    b = create_room(client, owner, project, "B")
    own = headers(client, owner)
    for room in (a, b):
        client.post(f"/api/v1/conversations/{room}/members/{agent}", headers=own)
    from app.services.room_service import eligible_agents

    assert client.post(f"/api/v1/conversations/{a}/members/{agent}/pause", headers=own).status_code == 200
    db = client.app.state.session_factory()
    try:
        assert eligible_agents(db, a) == []
        assert [entry.id for entry in eligible_agents(db, b)] == [agent]
    finally:
        db.close()
    assert client.post(f"/api/v1/conversations/{a}/members/{agent}/resume", headers=own).status_code == 200
    assert client.post(f"/api/v1/conversations/{a}/stop", headers=own).status_code == 204
    db = client.app.state.session_factory()
    try:
        assert eligible_agents(db, a) == []
        assert [entry.id for entry in eligible_agents(db, b)] == [agent]
    finally:
        db.close()


@pytest.mark.parametrize("intervention", ["remove", "pause", "stop"])
def test_http_inflight_provider_result_is_discarded_after_room_intervention(room_client, monkeypatch, intervention):
    client, (owner, _, project, agent, _) = room_client
    room = create_room(client, owner, project, "A")
    own = headers(client, owner)
    assert client.post(f"/api/v1/conversations/{room}/members/{agent}", headers=own).status_code == 201
    with client.app.state.session_factory() as db:
        connection = ProviderConnection(user_id=owner, kind="compatible", name="Synthetic",
            base_url="http://127.0.0.1:8765/v1", default_model_id="exact", auth_required=False,
            enabled=True, capabilities={"declared": {}, "verified": {}})
        db.add(connection)
        db.flush()
        row = db.get(Agent, agent)
        row.connection_id, row.model_id, row.temperature, row.max_tokens = connection.id, "exact", "auto", 0
        db.commit()
    entered = threading.Event()
    release = threading.Event()

    async def delayed_provider(request):
        entered.set()
        await asyncio.to_thread(release.wait)
        from app.services.providers import ProviderResult
        return ProviderResult(text="stale provider result")

    client.app.state.scheduler.provider = delayed_provider
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, f"/api/v1/conversations/{room}/messages", json={"content": "begin"}, headers=own)
        assert entered.wait(5)
        if intervention == "remove":
            changed = client.delete(f"/api/v1/conversations/{room}/members/{agent}", headers=own)
        elif intervention == "pause":
            changed = client.post(f"/api/v1/conversations/{room}/members/{agent}/pause", headers=own)
        else:
            changed = client.post(f"/api/v1/conversations/{room}/stop", headers=own)
        assert changed.status_code in (200, 204)
        release.set()
        result = future.result(timeout=5)
    assert result.status_code == 201, result.text
    assert result.json() == []
    db = client.app.state.session_factory()
    try:
        assert [(m.role, m.content) for m in db.query(Message).filter_by(conversation_id=room)] == [("user", "begin")]
        assert [(e.actor_type, e.payload["content"]) for e in db.query(RoomEvent).filter_by(room_id=room)] == [("user", "begin")]
    finally:
        db.close()


def test_websocket_project_assignment_alone_does_not_dispatch(room_client, monkeypatch):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "A")

    async def unexpected_provider(**kwargs):
        raise AssertionError("A project assignment must not dispatch")

    from app.services.room_service import llm_service
    monkeypatch.setattr(llm_service, "generate_response", unexpected_provider)
    protocols = f"gamja.chat, gamja.local.{client.app.state.local_session_token}, gamja.user.{create_access_token({'sub': owner})}"
    with client.websocket_connect(f"/api/v1/conversations/{room}/ws", headers={"origin": "http://localhost:3000", "sec-websocket-protocol": protocols}) as socket:
        socket.send_json({"type": "message", "content": "hello"})
        assert socket.receive_json()["content"] == "hello"


def test_overlapping_turns_return_their_own_committed_reply(room_client, monkeypatch):
    client, (owner, _, project, agent, _) = room_client
    room = create_room(client, owner, project, "A")
    assert client.post(f"/api/v1/conversations/{room}/members/{agent}", headers=headers(client, owner)).status_code == 201
    from app.services import room_service

    async def provider(*, agent, messages, user_message):
        return f"reply {user_message}"

    monkeypatch.setattr(room_service.llm_service, "generate_response", provider)
    original_finish = room_service.finish_turn
    first_committed = threading.Event()
    second_committed = threading.Event()

    def interleaved_finish(db, room_id, agent_id, generation, content):
        saved = original_finish(db, room_id, agent_id, generation, content)
        if content == "reply first":
            first_committed.set()
            assert second_committed.wait(5), "second turn did not commit"
        else:
            second_committed.set()
        return saved

    monkeypatch.setattr(room_service, "finish_turn", interleaved_finish)

    def send(content):
        db = client.app.state.session_factory()
        try:
            _, responses = asyncio.run(room_service.send_legacy_message(db, room, content))
            return [message.content for message in responses]
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(send, "first")
        assert first_committed.wait(5)
        second = pool.submit(send, "second")
        assert second.result(timeout=5) == ["reply second"]
        assert first.result(timeout=5) == ["reply first"]
