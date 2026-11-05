"""Scheduler tests use only synthetic SQLite and an injected provider."""

import asyncio
import json
import threading
import httpx
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from test_room_state import room_client, headers, create_room
from app.models.collaboration import ProviderConnection, RoomAgentRun, RoomEvent, RoomScheduleSettings, RoomTrigger, ToolAction
from app.models.models import Agent
from app.services.events import ActorRef, append_event
from app.services.providers import ProviderResult, ToolCall


class _NativeExecution:
    request_id = 2

    def __init__(self, client, model_id):
        self.client = client
        self.model_id = model_id

    def result(self, timeout=None):
        assert timeout is None
        self.client.events.append("terminal")
        return {
            "ok": True, "response_id": "native-response", "model": self.model_id,
            "text": "native answer", "tool_calls": [],
            "physical_model_request_count": 1,
        }

    def cancel(self):
        self.client.events.append("cancel")


class _NativeCoreClient:
    """Made a CoreProviderClient subclass lazily to avoid a test collection cycle."""

    @staticmethod
    def create():
        from app.services.core_provider_transport import CoreProviderClient

        class Client(CoreProviderClient):
            def __init__(self):
                self.bindings = []
                self.calls = []
                self.events = []

            def bind(self, binding, *, timeout=None):
                assert timeout is None
                self.events.append("bind")
                self.bindings.append(binding)
                return "ab" * 32

            def begin_execute(self, handle, call):
                assert handle == "ab" * 32
                self.events.append("execute")
                self.calls.append(call)
                return _NativeExecution(self, call["model_id"])

            def release(self, handle, *, timeout=None):
                assert timeout is None and handle == "ab" * 32
                self.events.append("release")

            def close(self):
                pass

        return Client()


def ready_agents(client, owner, room, names):
    with client.app.state.session_factory() as db:
        connection = ProviderConnection(user_id=owner, kind="compatible", name="Synthetic",
                                        base_url="http://127.0.0.1:8765/v1", default_model_id="exact",
                                        auth_required=False, enabled=True, capabilities={"declared": {}, "verified": {}})
        db.add(connection)
        db.flush()
        agents = []
        for name in names:
            agent = Agent(user_id=owner, name=name, description="d", system_prompt=f"system-{name}",
                          connection_id=connection.id, model_id="exact", temperature="auto", max_tokens=0)
            db.add(agent)
            db.flush()
            agents.append(agent.id)
        db.commit()
    for agent_id in agents:
        assert client.post(f"/api/v1/conversations/{room}/members/{agent_id}", headers=headers(client, owner)).status_code == 201
    return agents


def test_scheduler_routes_native_connection_to_same_core_without_http_or_store(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "native-core-route")
    credential_ref = f"{owner}:71:" + "22" * 16
    with client.app.state.session_factory() as db:
        connection = ProviderConnection(
            user_id=owner, kind="google-vertex", name="Native Vertex",
            default_model_id="gemini-native", secret_ref=credential_ref,
            auth_required=True, enabled=True, capabilities={"declared": {}, "verified": {}},
            readiness_revision=3, provider_id="google-vertex",
            protocol_id="vertex_generate_content", auth_method_id="native_chain",
            public_config_version=1,
            public_config={"project_id": "potato-project", "location": "us-central1"},
            auth_grant_revision=4, credential_state="present",
        )
        db.add(connection)
        db.flush()
        agent = Agent(
            user_id=owner, name="Ada", description="d", system_prompt="system-Ada",
            connection_id=connection.id, model_id="gemini-native",
            temperature="auto", max_tokens=0,
        )
        db.add(agent)
        db.commit()
        agent_id = agent.id
    assert client.post(
        f"/api/v1/conversations/{room}/members/{agent_id}",
        headers=headers(client, owner),
    ).status_code == 201

    class ForbiddenStore:
        def get_no_ui(self, _reference):
            raise AssertionError("native runtime must not read the Python credential store")

    class OwnerEpochs:
        def snapshot(self):
            return SimpleNamespace(owner_id=owner, owner_epoch="11" * 32)

        def current(self, owner_id):
            assert owner_id == owner
            return "11" * 32

    core = _NativeCoreClient.create()
    http_calls = []
    client.app.state.scheduler.provider = None
    client.app.state.secret_store = ForbiddenStore()
    client.app.state.connection_transport = httpx.MockTransport(
        lambda request: http_calls.append(request) or pytest.fail("native must not use HTTPX"))
    client.app.state.owner_epoch_provider = OwnerEpochs()
    client.app.state.provider_client = core

    response = client.post(
        f"/api/v1/conversations/{room}/messages",
        json={"content": "Use the saved native connection"},
        headers=headers(client, owner),
    )

    assert response.status_code == 201, response.text
    assert response.json()[0]["content"] == "native answer"
    assert http_calls == []
    assert core.events == ["bind", "execute", "terminal", "release"]
    assert core.bindings[0] == {
        "credential_ref": credential_ref,
        "owner_id": owner,
        "owner_epoch": "11" * 32,
        "connection_id": core.bindings[0]["connection_id"],
        "config_revision": 3,
        "auth_grant_revision": 4,
        "provider_id": "google-vertex",
        "auth_method_id": "native_chain",
        "public_config_fingerprint": core.bindings[0]["public_config_fingerprint"],
        "descriptor_revision": 1,
        "protocol": "vertex_generate_content_v1",
        "model_id": "gemini-native",
        "public_fields": {"project_id": "potato-project", "location": "us-central1"},
    }
    assert len(core.bindings[0]["public_config_fingerprint"]) == 64


def enable_tools(client, agent_id):
    with client.app.state.session_factory() as db:
        connection = db.get(ProviderConnection, db.get(Agent, agent_id).connection_id)
        connection.capabilities = {"declared": {"tools": True},
                                   "verified": {"model_id": "exact", "base_url": connection.base_url,
                                                "tools": True}}
        db.commit()


def test_unexpected_provider_failure_emits_one_runtime_event(room_client, monkeypatch):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "unexpected-provider")
    ready_agents(client, owner, room, ("Ada",))
    emitted = []
    def record(detail):
        with client.app.state.session_factory() as db:
            saved = db.query(RoomEvent).filter_by(room_id=room, kind="failure").one().payload["diagnostic"]
        assert detail == saved
        emitted.append(detail)
    monkeypatch.setattr("app.services.scheduler.emit_detailed_diagnostic", record)

    async def provider(_request):
        raise RuntimeError("secret /Users/alice/private.txt")

    client.app.state.scheduler.provider = provider
    response = client.post(f"/api/v1/conversations/{room}/messages",
                           json={"content": "hello"}, headers=headers(client, owner))
    assert response.status_code == 502
    assert len(emitted) == 1
    with client.app.state.session_factory() as db:
        payload = db.query(RoomEvent).filter_by(room_id=room, kind="failure").one().payload
    assert payload["code"] == "provider_unavailable"
    assert payload["diagnostic"]["stage"] == "provider_request"
    assert payload["diagnostic"]["error_class"] == "runtime_error"
    assert payload["diagnostic"]["version"] == 1
    assert payload["diagnostic"]["reason"] == "provider_unavailable"
    assert payload["diagnostic"]["room_id"] == room
    assert payload["diagnostic"]["exception_chain"][0] == {"module": "builtins", "class": "RuntimeError"}
    assert any(frame["file"] == "app/services/scheduler.py" for frame in payload["diagnostic"]["frames"])
    assert len(payload["diagnostic"]["correlation_id"]) == 32
    assert "secret" not in str(payload)


def test_provider_http_rejection_persists_status_request_id_and_safe_failure_chain(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "provider-http-detail")
    (agent,) = ready_agents(client, owner, room, ("Ada",))
    client.app.state.scheduler.provider = None
    client.app.state.connection_transport = httpx.MockTransport(lambda _request: httpx.Response(
        429, json={"error": "Bearer private-api-key"}, headers={"x-request-id": "req-safe-12"}))
    response = client.post(f"/api/v1/conversations/{room}/messages",
                           json={"content": "synthetic"}, headers=headers(client, owner))
    assert response.status_code == 502
    with client.app.state.session_factory() as db:
        payload = db.query(RoomEvent).filter_by(room_id=room, kind="failure").one().payload
        connection_id = db.get(Agent, agent).connection_id
    assert payload["code"] == "rate_limited"
    assert payload["diagnostic"]["http_status"] == 429
    assert payload["diagnostic"]["vendor_request_id"] == "req-safe-12"
    assert payload["diagnostic"]["connection_id"] == connection_id
    assert "private-api-key" not in str(payload)


def test_invalid_bare_process_path_is_typed_before_any_action(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "invalid-process")
    (agent,) = ready_agents(client, owner, room, ("Ada",))
    enable_tools(client, agent)

    async def provider(_request):
        return ProviderResult(tool_calls=[ToolCall("start-1", "start_process",
                                                   {"executable": "screencapture"})],
                              continuation={"call_ids": ["start-1"]})

    client.app.state.scheduler.provider = provider
    response = client.post(f"/api/v1/conversations/{room}/messages",
                           json={"content": "synthetic process request"}, headers=headers(client, owner))
    assert response.status_code == 502
    with client.app.state.session_factory() as db:
        assert db.query(ToolAction).filter_by(room_id=room).count() == 0
        payload = db.query(RoomEvent).filter_by(room_id=room, kind="failure").one().payload
    assert payload["code"] == "invalid_tool_arguments"
    assert "screencapture" not in str(payload)


def test_round_two_sees_committed_peer_events_without_self_third_round(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "peer-room")
    agents = ready_agents(client, owner, room, ("Ada", "Ben"))
    requests = []

    async def fake_provider(request):
        requests.append((request.agent_id, request.watermark_seq,
                         [(event["actor_type"], event["actor_id"], event["payload"].get("content")) for event in request.events]))
        return ProviderResult(text=f"agent {request.agent_id} at {request.watermark_seq}")

    client.app.state.scheduler.provider = fake_provider
    response = client.post(f"/api/v1/conversations/{room}/messages", json={"content": "Discuss"}, headers=headers(client, owner))
    assert response.status_code == 201, response.text
    with client.app.state.session_factory() as db:
        events = db.query(RoomEvent).filter_by(room_id=room).order_by(RoomEvent.seq).all()
        assert len([event for event in events if event.actor_type == "agent" and event.kind == "message"]) == 4, [(e.kind, e.payload) for e in events]
    assert len(requests) == 4
    assert [watermark for _, watermark, _ in requests[:2]] == [1, 1]
    assert all(any(actor == "agent" and who != agent for actor, who, _ in history)
               for agent, _, history in requests[2:])


def test_event_api_preserves_distinct_request_round_and_actor_model_snapshots(room_client):
    client, (owner, other, project, _, _) = room_client
    room = create_room(client, owner, project, "comparison snapshots")
    agents = ready_agents(client, owner, room, ("Ada", "Ben"))
    calls = {}

    async def provider(request):
        key = (request.trigger_event_id, request.agent_id)
        calls[key] = calls.get(key, 0) + 1
        return ProviderResult(text=f"reply {request.agent_id}/{calls[key]}")

    client.app.state.scheduler.provider = provider
    path = f"/api/v1/conversations/{room}"
    own = headers(client, owner)
    assert client.put(f"{path}/schedule", json={"max_rounds": 2, "max_tool_calls": 8,
                                                "max_executor_seconds": 60}, headers=own).status_code == 200
    assert client.post(f"{path}/messages", json={"content": "first request"}, headers=own).status_code == 201

    with client.app.state.session_factory() as db:
        changed = ProviderConnection(user_id=owner, kind="anthropic", name="Synthetic changed",
                                     base_url="https://api.anthropic.com/v1", default_model_id="claude-new",
                                     auth_required=False, enabled=True,
                                     capabilities={"declared": {}, "verified": {}})
        db.add(changed)
        db.flush()
        for agent_id, name in zip(agents, ("Ada renamed", "Ben renamed")):
            agent = db.get(Agent, agent_id)
            agent.name, agent.connection_id, agent.model_id = name, changed.id, "claude-new"
        db.commit()
        changed_connection_id = changed.id

    assert client.post(f"{path}/messages", json={"content": "second request"}, headers=own).status_code == 201
    wire = client.get(f"{path}/events", headers=own).json()
    requests = [event for event in wire if event["actor_type"] == "user" and event["kind"] == "message"]
    replies = [event for event in wire if event["actor_type"] == "agent" and event["kind"] == "message"]
    assert [event["payload"]["content"] for event in requests] == ["first request", "second request"]
    assert len(replies) == 8

    first, second = requests
    first_replies = [event for event in replies if event["payload"]["trigger_event_id"] == first["event_id"]]
    second_replies = [event for event in replies if event["payload"]["trigger_event_id"] == second["event_id"]]
    assert len(first_replies) == len(second_replies) == 4
    assert sorted(event["payload"]["round"] for event in first_replies) == [1, 1, 2, 2]
    assert sorted(event["payload"]["round"] for event in second_replies) == [1, 1, 2, 2]
    assert {event["actor_name_snapshot"] for event in first_replies} == {"Ada", "Ben"}
    assert {event["actor_name_snapshot"] for event in second_replies} == {"Ada renamed", "Ben renamed"}
    assert {event["payload"]["model_use"]["model_id"] for event in first_replies} == {"exact"}
    assert {event["payload"]["model_use"]["provider_kind"] for event in first_replies} == {"compatible"}
    assert {event["payload"]["model_use"]["connection_id"] for event in second_replies} == {changed_connection_id}
    assert {event["payload"]["model_use"]["model_id"] for event in second_replies} == {"claude-new"}
    assert {event["payload"]["model_use"]["provider_kind"] for event in second_replies} == {"anthropic"}
    assert all(set(event["payload"]["model_use"]) == {
        "connection_id", "model_id", "provider_kind", "watermark_seq",
    } for event in replies)
    assert all("secret" not in json.dumps(event["payload"]).lower() for event in replies)

    assert client.delete(path, headers=own).status_code == 204
    assert client.get(f"{path}/events", headers=own).json() == wire
    assert client.get(f"{path}/events", headers=headers(client, other)).status_code == 404


def test_first_wave_passer_gets_peer_follow_up(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "pass-peer")
    ada, ben = ready_agents(client, owner, room, ("Ada", "Ben"))
    seen = []

    async def provider(request):
        seen.append((request.agent_id, [event["payload"].get("content") for event in request.events]))
        if request.agent_id == ada:
            return ProviderResult(text="Ada committed")
        return ProviderResult(text="Ben saw Ada" if "Ada committed" in seen[-1][1] else "")

    client.app.state.scheduler.provider = provider
    result = client.post(f"/api/v1/conversations/{room}/messages", json={"content": "Discuss"},
                         headers=headers(client, owner))
    assert result.status_code == 201, result.text
    assert any(agent_id == ben and "Ada committed" in context for agent_id, context in seen)
    assert any(message["content"] == "Ben saw Ada" for message in result.json())


def test_tool_observation_alone_gives_passing_peer_follow_up(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "tool-peer")
    ada, ben = ready_agents(client, owner, room, ("Ada", "Ben"))

    class Broker:
        tool_schemas = [{"name": "read_file", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}]

        async def execute(self, _action, _grant, _cancel, _budget):
            from app.services.tool_broker import ActionResult
            return ActionResult("completed", "Ada observation", safe_text="Ada observation")

    seen = []

    async def provider(request):
        if request.agent_id == ada:
            if request.observations:
                return ProviderResult(text="")
            return ProviderResult(tool_calls=[ToolCall("observed", "read_file", {})],
                                  continuation={"call_ids": ["observed"]})
        has_observation = any(event["kind"] == "tool_result" and
                              event["payload"].get("text") == "Ada observation" for event in request.events)
        seen.append(has_observation)
        return ProviderResult(text="Ben saw the result" if has_observation else "")

    client.app.state.scheduler.broker = Broker()
    client.app.state.scheduler.provider = provider
    result = client.post(f"/api/v1/conversations/{room}/messages", json={"content": "Inspect"},
                         headers=headers(client, owner))
    assert result.status_code == 201, result.text
    assert seen == [False, True]
    assert [message["content"] for message in result.json()] == ["Ben saw the result"]


def test_delete_agent_cancels_turn_and_inactive_restart_reconciles(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "removed")
    agent_id = ready_agents(client, owner, room, ("Ada",))[0]
    entered, release = threading.Event(), threading.Event()

    async def provider(_request):
        entered.set()
        await asyncio.to_thread(release.wait)
        return ProviderResult(text="stale reply")

    client.app.state.scheduler.provider = provider
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"/api/v1/conversations/{room}/messages",
                              json={"content": "work"}, headers=headers(client, owner))
        try:
            assert entered.wait(3)
            with patch.object(client.app.state.scheduler.broker.executor, "stop_room", new_callable=AsyncMock) as stop_owned:
                deleted = client.delete(f"/api/v1/agents/{agent_id}", headers=headers(client, owner))
                assert deleted.status_code == 204, deleted.text
                stop_owned.assert_awaited_once_with(room, agent_id)
        finally:
            release.set()
        assert pending.result(timeout=3).status_code == 201
    with client.app.state.session_factory() as db:
        run = db.get(RoomAgentRun, (room, agent_id))
        assert run.state == "stopped" and run.active_tool == {}
        assert db.query(RoomEvent).filter_by(room_id=room, actor_type="agent", kind="message").count() == 0
        # Simulate an older crashed database whose inactive actor was still running.
        run.state = "running"
        db.commit()
    from app.services.scheduler import Scheduler
    restarted = Scheduler(client.app.state.session_factory, client.app.state.event_hub, client.app.state)
    asyncio.run(restarted.start())
    with client.app.state.session_factory() as db:
        assert db.get(RoomAgentRun, (room, agent_id)).state == "idle"


@pytest.mark.asyncio
async def test_context_uses_frozen_watermark_for_mid_call_user_event(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "frozen")
    agent_id = ready_agents(client, owner, room, ("Ada",))[0]
    from app.services.events import ActorRef, append_event
    from app.services.context import build_turn_context
    with client.app.state.session_factory() as db:
        first = append_event(db, room, ActorRef("user", owner), "message", {"content": "first"})
        later = append_event(db, room, ActorRef("user", owner), "message", {"content": "later"})
        first_seq, later_seq = first.seq, later.seq
        request = await build_turn_context(db, room, agent_id, first_seq)
    assert [event["payload"]["content"] for event in request.events if event["kind"] == "message"] == ["first"]
    assert later_seq > request.watermark_seq


def test_mid_call_user_event_is_absent_from_first_turn_and_present_in_next(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "race")
    agent_id = ready_agents(client, owner, room, ("Ada",))[0]
    entered, release = threading.Event(), threading.Event()
    seen = []

    async def provider(request):
        seen.append((request.watermark_seq, [event["payload"]["content"] for event in request.events
                                             if event["actor_type"] == "user" and event["kind"] == "message"]))
        if len(seen) == 1:
            entered.set()
            await asyncio.to_thread(release.wait)
        return ProviderResult(text=f"reply {len(seen)}")

    client.app.state.scheduler.provider = provider
    path = f"/api/v1/conversations/{room}"
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, f"{path}/messages", json={"content": "first"}, headers=headers(client, owner))
        try:
            assert entered.wait(3)
            later = client.post(f"{path}/events", json={"content": "later"}, headers=headers(client, owner))
            assert later.status_code == 201
        finally:
            release.set()
        assert first.result(timeout=3).status_code == 201
    assert seen[0] == (1, ["first"])
    assert seen[1][1] == ["first", "later"]


def test_slow_room_does_not_block_same_agent_in_another_room(room_client):
    client, (owner, _, project, _, _) = room_client
    slow, fast = [create_room(client, owner, project, name) for name in ("slow", "fast")]
    agent_id = ready_agents(client, owner, slow, ("Ada",))[0]
    assert client.post(f"/api/v1/conversations/{fast}/members/{agent_id}", headers=headers(client, owner)).status_code == 201
    with client.app.state.session_factory() as db:
        db.get(RoomAgentRun, (slow, agent_id)).memory = {"note": "slow-only"}
        db.get(RoomAgentRun, (fast, agent_id)).memory = {"note": "fast-only"}
        db.commit()
    entered, release = threading.Event(), threading.Event()
    seen = {}

    async def provider(request):
        seen[request.room_id] = (request.memory, [event["room_id"] for event in request.events])
        from app.services.providers.base import attributed_messages
        assert "only" not in request.system
        assert attributed_messages(request)[0]["role"] == "user"
        if request.room_id == slow:
            entered.set()
            await asyncio.to_thread(release.wait)
        return ProviderResult(text="done")

    client.app.state.scheduler.provider = provider
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"/api/v1/conversations/{slow}/messages",
                              json={"content": "slow"}, headers=headers(client, owner))
        try:
            assert entered.wait(3)
            result = client.post(f"/api/v1/conversations/{fast}/messages",
                                 json={"content": "fast"}, headers=headers(client, owner))
            assert result.status_code == 201 and result.json()[0]["content"] == "done"
            assert not release.is_set()
        finally:
            release.set()
        assert pending.result(timeout=3).status_code == 201
    assert "slow-only" in seen[slow][0] and "fast-only" not in seen[slow][0]
    assert "fast-only" in seen[fast][0] and "slow-only" not in seen[fast][0]
    assert seen[slow][1] == [slow] and seen[fast][1] == [fast]


def test_native_call_observation_continuation_and_peer_watermark(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "tools")
    ada, ben = ready_agents(client, owner, room, ("Ada", "Ben"))
    with client.app.state.session_factory() as db:
        connection = db.get(ProviderConnection, db.get(Agent, ada).connection_id)
        connection.capabilities = {"declared": {"tools": True},
            "verified": {"model_id": "exact", "base_url": connection.base_url, "tools": True}}
        db.commit()
    observed = []

    @dataclass
    class Outcome:
        state: str = "completed"
        text: str = "file contents"
        executor_seconds: float = 0.1

    class Broker:
        tool_schemas = [{"name": "read_file", "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}}]

        async def execute(self, action, grant, cancel, budget):
            assert action.room_id == room and action.agent_id == ada and action.family == "read_file"
            return Outcome()

    async def provider(request):
        if request.agent_id == ada and not request.observations and request.watermark_seq == 1:
            return ProviderResult(tool_calls=[ToolCall("call-1", "read_file", {"path": "synthetic.txt"})],
                                  continuation={"call_ids": ["call-1"]})
        if request.observations:
            observed.extend(request.observations)
        if request.agent_id == ben and request.watermark_seq > 1:
            assert any(event["kind"] == "tool_result" and event["payload"]["call_id"] == "call-1"
                       for event in request.events)
        return ProviderResult(text=f"final {request.agent_id}")

    client.app.state.scheduler.broker = Broker()
    client.app.state.scheduler.provider = provider
    response = client.post(f"/api/v1/conversations/{room}/messages", json={"content": "read"}, headers=headers(client, owner))
    assert response.status_code == 201, response.text
    assert [(item.call_id, item.text) for item in observed] == [("call-1", "file contents")]
    with client.app.state.session_factory() as db:
        assert [(e.kind, e.payload.get("call_id")) for e in db.query(RoomEvent).filter_by(room_id=room).order_by(RoomEvent.seq)
                if e.kind == "tool_result"] == [("tool_result", "call-1")]
        assert db.query(ToolAction).filter_by(room_id=room).one().state == "completed"


def test_restart_marks_interrupted_turn_and_started_action_uncertain_without_replay(room_client, sqlite_url):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "restart")
    agent_id = ready_agents(client, owner, room, ("Ada",))[0]
    from app.services.events import ActorRef, append_event
    from app.main import create_app
    from fastapi.testclient import TestClient
    with client.app.state.session_factory() as db:
        event = append_event(db, room, ActorRef("user", owner), "message", {"content": "before crash"})
        event_id = event.event_id
        db.add(RoomTrigger(event_id=event_id, room_id=room, state="running", selected_agents=[agent_id], current_round=1))
        run = db.get(RoomAgentRun, (room, agent_id))
        run.state, run.generation, run.trigger_event_id = "running", 5, event_id
        db.add(ToolAction(action_id="synthetic-action", room_id=room, agent_id=agent_id,
                          family="read_file", arguments={}, state="started"))
        db.add(ToolAction(action_id="approved-not-started", room_id=room, agent_id=agent_id,
                          family="read_file", arguments={}, state="approved"))
        db.commit()
    client.__exit__(None, None, None)  # Simulate the first backend exiting.
    with TestClient(create_app(database_url=sqlite_url)) as restarted:
        with restarted.app.state.session_factory() as db:
            run = db.get(RoomAgentRun, (room, agent_id))
            assert run.state == "idle" and run.generation == 6
            assert db.get(ToolAction, "synthetic-action").state == "uncertain"
            assert db.get(ToolAction, "approved-not-started").state == "cancelled"
            assert db.get(RoomTrigger, event_id).state == "interrupted"
            assert [(e.kind, e.payload.get("code")) for e in db.query(RoomEvent).filter_by(room_id=room).order_by(RoomEvent.seq)] == [
                ("message", None), ("failure", "interrupted"), ("tool_result", None), ("tool_result", None)]
            assert sorted(e.payload["state"] for e in db.query(RoomEvent).filter_by(room_id=room, kind="tool_result")) == [
                "cancelled", "uncertain"]


def test_mentioned_agent_only_and_room_round_override(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "mention")
    ada, ben = ready_agents(client, owner, room, ("Ada", "Ben"))
    seen = []

    async def provider(request):
        seen.append(request.agent_id)
        return ProviderResult(text=f"reply {request.agent_id}")

    client.app.state.scheduler.provider = provider
    path = f"/api/v1/conversations/{room}"
    assert client.put(f"{path}/schedule", json={"max_rounds": 1, "max_tool_calls": 3,
                                                     "max_executor_seconds": 10}, headers=headers(client, owner)).status_code == 200
    assert client.get(f"{path}/schedule", headers=headers(client, owner)).json() == {
        "max_rounds": 1, "max_tool_calls": 3, "max_executor_seconds": 10}
    reply = client.post(f"{path}/messages", json={"content": "@Ada, answer"}, headers=headers(client, owner))
    assert reply.status_code == 201
    assert [message["agent_id"] for message in reply.json()] == [ada]
    assert seen == [ada]


@pytest.mark.parametrize("payload", [
    {"max_rounds": True, "max_tool_calls": 8, "max_executor_seconds": 60},
    {"max_rounds": 2, "max_tool_calls": 1.5, "max_executor_seconds": 60},
    {"max_rounds": 0, "max_tool_calls": 8, "max_executor_seconds": 60},
    {"max_rounds": 5, "max_tool_calls": 8, "max_executor_seconds": 60},
    {"max_rounds": 2, "max_tool_calls": -1, "max_executor_seconds": 60},
    {"max_rounds": 2, "max_tool_calls": 65, "max_executor_seconds": 60},
    {"max_rounds": 2, "max_tool_calls": 8, "max_executor_seconds": 0},
    {"max_rounds": 2, "max_tool_calls": 8, "max_executor_seconds": 3601},
    {"max_rounds": 2, "max_tool_calls": 8},
    {"max_rounds": 2, "max_tool_calls": 8, "max_executor_seconds": 60, "extra": 1},
])
def test_schedule_rejects_non_integer_range_and_shape_errors(room_client, payload):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "invalid schedule")
    path = f"/api/v1/conversations/{room}/schedule"
    response = client.put(path, json=payload, headers=headers(client, owner))
    assert response.status_code == 422
    assert client.get(path, headers=headers(client, owner)).json() == {
        "max_rounds": 2, "max_tool_calls": 8, "max_executor_seconds": 60,
    }


def test_schedule_boundaries_owner_isolation_and_archived_read(room_client):
    client, (owner, other, project, _, _) = room_client
    room = create_room(client, owner, project, "schedule ownership")
    path = f"/api/v1/conversations/{room}/schedule"
    own, stranger = headers(client, owner), headers(client, other)

    minimum = {"max_rounds": 1, "max_tool_calls": 0, "max_executor_seconds": 1}
    maximum = {"max_rounds": 4, "max_tool_calls": 64, "max_executor_seconds": 3600}
    assert client.put(path, json=minimum, headers=own).json() == minimum
    assert client.put(path, json=maximum, headers=own).json() == maximum
    assert client.get(path, headers=stranger).status_code == 404
    assert client.put(path, json=minimum, headers=stranger).status_code == 404

    assert client.delete(f"/api/v1/conversations/{room}", headers=own).status_code == 204
    assert client.get(path, headers=own).json() == maximum
    assert client.put(path, json=minimum, headers=own).status_code == 404


def test_saved_schedule_is_snapshotted_only_for_new_triggers(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "schedule snapshot")
    path = f"/api/v1/conversations/{room}/schedule"
    first_policy = {"max_rounds": 1, "max_tool_calls": 2, "max_executor_seconds": 3}
    next_policy = {"max_rounds": 4, "max_tool_calls": 5, "max_executor_seconds": 6}
    assert client.put(path, json=first_policy, headers=headers(client, owner)).status_code == 200

    with client.app.state.session_factory() as db:
        first = append_event(db, room, ActorRef("user", owner), "message", {"content": "first"})
        first_id = first.event_id
        client.app.state.scheduler._create_trigger(db, first)

    assert client.put(path, json=next_policy, headers=headers(client, owner)).status_code == 200
    with client.app.state.session_factory() as db:
        snapshotted = db.get(RoomTrigger, first_id)
        assert {key: getattr(snapshotted, key) for key in first_policy} == first_policy
        second = append_event(db, room, ActorRef("user", owner), "message", {"content": "second"})
        second_id = second.event_id
        client.app.state.scheduler._create_trigger(db, second)
        created_after_save = db.get(RoomTrigger, second_id)
        assert {key: getattr(created_after_save, key) for key in next_policy} == next_policy
        assert db.get(RoomScheduleSettings, room).max_rounds == next_policy["max_rounds"]


def test_blockquoted_mentions_do_not_route_but_remain_in_agent_context(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "quoted mention")
    ada, ben = ready_agents(client, owner, room, ("Ada", "Ben"))
    seen = []

    async def provider(request):
        trigger = next(event for event in request.events if event["event_id"] == request.trigger_event_id)
        seen.append((request.agent_id, trigger["payload"]["content"]))
        return ProviderResult(text=f"reply {request.agent_id}")

    client.app.state.scheduler.provider = provider
    path = f"/api/v1/conversations/{room}"
    assert client.put(f"{path}/schedule", json={"max_rounds": 1, "max_tool_calls": 3,
                                                "max_executor_seconds": 10}, headers=headers(client, owner)).status_code == 200

    directed = "Context:\n> @Ben suggested the earlier approach\n  > @Ben added detail\n\n@Ada, answer this"
    reply = client.post(f"{path}/messages", json={"content": directed}, headers=headers(client, owner))
    assert reply.status_code == 201, reply.text
    assert [message["agent_id"] for message in reply.json()] == [ada]
    assert seen == [(ada, directed)]

    seen.clear()
    broadcast = "> @Ben suggested the earlier approach\n\nContinue together"
    reply = client.post(f"{path}/messages", json={"content": broadcast}, headers=headers(client, owner))
    assert reply.status_code == 201, reply.text
    assert [message["agent_id"] for message in reply.json()] == [ada, ben]
    assert seen == [(ada, broadcast), (ben, broadcast)]

    with client.app.state.session_factory() as db:
        saved = [event.payload["content"] for event in db.query(RoomEvent).filter_by(
            room_id=room, actor_type="user", kind="message").order_by(RoomEvent.seq)]
    assert saved == [directed, broadcast]


def test_tool_budget_failure_is_visible_and_does_not_execute(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "budget")
    ready_agents(client, owner, room, ("Ada",))
    calls = []
    delivered_tools = []

    class Broker:
        tool_schemas = [{"name": "read_file", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}]

        async def execute(self, action, grant, cancel, budget):
            calls.append(action.action_id)
            raise AssertionError("budget must stop before execution")

    async def provider(request):
        delivered_tools.append(request.tools)
        return ProviderResult(tool_calls=[ToolCall("call-1", "read_file", {})], continuation={"call_ids": ["call-1"]})

    client.app.state.scheduler.broker = Broker()
    client.app.state.scheduler.provider = provider
    path = f"/api/v1/conversations/{room}"
    client.put(f"{path}/schedule", json={"max_rounds": 1, "max_tool_calls": 0,
                                        "max_executor_seconds": 60}, headers=headers(client, owner))
    response = client.post(f"{path}/messages", json={"content": "read"}, headers=headers(client, owner))
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "tool_budget_exhausted"
    assert delivered_tools == [[]]
    assert calls == []
    with client.app.state.session_factory() as db:
        assert db.query(ToolAction).filter_by(room_id=room).count() == 0
        assert db.query(RoomEvent).filter_by(room_id=room, kind="failure").one().payload["code"] == "tool_budget_exhausted"


def test_duplicate_user_trigger_does_not_dispatch_twice(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "duplicate")
    ready_agents(client, owner, room, ("Ada",))
    calls = []

    async def provider(request):
        calls.append(request.agent_id)
        return ProviderResult(text="once")

    client.app.state.scheduler.provider = provider
    path = f"/api/v1/conversations/{room}/messages"
    auth = {**headers(client, owner), "Idempotency-Key": "same"}
    first = client.post(path, json={"content": "hello"}, headers=auth)
    repeated = client.post(path, json={"content": "hello"}, headers=auth)
    assert first.status_code == repeated.status_code == 201
    assert repeated.json() == first.json()
    assert calls == [calls[0]]
    with client.app.state.session_factory() as db:
        assert db.query(RoomTrigger).filter_by(room_id=room).count() == 1


def test_executor_budget_records_observation_before_stopping_continuation(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "executor-budget")
    ready_agents(client, owner, room, ("Ada",))

    @dataclass
    class Outcome:
        state: str = "completed"
        text: str = "effect happened"
        executor_seconds: float = 2.0

    class Broker:
        tool_schemas = [{"name": "read_file", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}]

        async def execute(self, action, grant, cancel, budget):
            return Outcome()

    async def provider(request):
        if request.observations:
            raise AssertionError("Continuation must stop after executor budget exhaustion")
        return ProviderResult(tool_calls=[ToolCall("call-1", "read_file", {})], continuation={"call_ids": ["call-1"]})

    client.app.state.scheduler.broker = Broker()
    client.app.state.scheduler.provider = provider
    path = f"/api/v1/conversations/{room}"
    client.put(f"{path}/schedule", json={"max_rounds": 1, "max_tool_calls": 1,
                                        "max_executor_seconds": 1}, headers=headers(client, owner))
    response = client.post(f"{path}/messages", json={"content": "read"}, headers=headers(client, owner))
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "executor_budget_exhausted"
    with client.app.state.session_factory() as db:
        assert [(e.kind, e.payload.get("text"), e.payload.get("code"))
                for e in db.query(RoomEvent).filter_by(room_id=room).order_by(RoomEvent.seq)] == [
                    ("message", None, None), ("tool_result", "effect happened", None),
                    ("failure", None, "executor_budget_exhausted")]


def test_explicit_continuation_overrides_one_round_room_default(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "continue")
    ready_agents(client, owner, room, ("Ada", "Ben"))

    async def provider(request):
        return ProviderResult(text=f"answer {request.agent_id}")

    client.app.state.scheduler.provider = provider
    path = f"/api/v1/conversations/{room}"
    client.put(f"{path}/schedule", json={"max_rounds": 1, "max_tool_calls": 8,
                                        "max_executor_seconds": 60}, headers=headers(client, owner))
    assert len(client.post(f"{path}/messages", json={"content": "first"}, headers=headers(client, owner)).json()) == 2
    continuation = client.post(f"{path}/continue", json={"content": "Continue", "max_rounds": 2},
                               headers=headers(client, owner))
    assert continuation.status_code == 201, continuation.text
    deadline = monotonic() + 3
    while monotonic() < deadline:
        with client.app.state.session_factory() as db:
            trigger = db.get(RoomTrigger, continuation.json()["event_id"])
            if trigger and trigger.state == "done":
                assert trigger.max_rounds == 2
                assert db.query(RoomEvent).filter_by(room_id=room, actor_type="agent", kind="message").count() == 6
                break
        sleep(0.01)
    else:
        pytest.fail("Explicit continuation did not complete")


def test_invalid_continuation_limit_cannot_commit_user_event(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "invalid")
    path = f"/api/v1/conversations/{room}/continue"
    response = client.post(path, json={"content": "work", "max_rounds": 100}, headers=headers(client, owner))
    assert response.status_code == 422
    with client.app.state.session_factory() as db:
        assert db.query(RoomEvent).filter_by(room_id=room).count() == 0


def test_cancel_active_turn_keeps_room_available_for_explicit_next_request(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "cancel")
    ready_agents(client, owner, room, ("Ada",))
    entered, release = threading.Event(), threading.Event()
    calls = []

    async def provider(request):
        calls.append(request.watermark_seq)
        if len(calls) == 1:
            entered.set()
            await asyncio.to_thread(release.wait)
        return ProviderResult(text="ready")

    client.app.state.scheduler.provider = provider
    path = f"/api/v1/conversations/{room}"
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"{path}/messages", json={"content": "first"}, headers=headers(client, owner))
        try:
            assert entered.wait(3)
            assert client.post(f"{path}/cancel", headers=headers(client, owner)).status_code == 204
        finally:
            release.set()
        assert pending.result(timeout=3).json() == []
    assert client.get(path, headers=headers(client, owner)).json()["is_active"] is True
    next_reply = client.post(f"{path}/messages", json={"content": "again"}, headers=headers(client, owner))
    assert next_reply.status_code == 201 and next_reply.json()[0]["content"] == "ready"


def test_summary_event_has_room_local_source_range_and_preserves_full_context(room_client):
    client, (owner, _, project, _, _) = room_client
    room, other = [create_room(client, owner, project, title) for title in ("summary", "other")]
    path = f"/api/v1/conversations/{room}"
    for content in ("first", "second"):
        assert client.post(f"{path}/events", json={"content": content}, headers=headers(client, owner)).status_code == 201
    assert client.post(f"/api/v1/conversations/{other}/events", json={"content": "private"},
                       headers=headers(client, owner)).status_code == 201
    summary = client.post(f"{path}/summaries", json={"source_start_seq": 1, "source_end_seq": 2,
                                                      "content": "The owner asked two questions."},
                          headers=headers(client, owner))
    assert summary.status_code == 201, summary.text
    assert summary.json()["payload"] == {"source_start_seq": 1, "source_end_seq": 2,
                                          "owner_checkpoint": True,
                                          "content": "The owner asked two questions."}
    agent_id = ready_agents(client, owner, room, ("Ada",))[0]
    from app.services.context import build_turn_context
    with client.app.state.session_factory() as db:
        request = asyncio.run(build_turn_context(db, room, agent_id, 3))
    assert [(event["kind"], event["payload"]["content"]) for event in request.events] == [
        ("message", "first"), ("message", "second"), ("summary", "The owner asked two questions.")]
    assert client.post(f"{path}/summaries", json={"source_start_seq": 1, "source_end_seq": 99,
                                                  "content": "invalid"}, headers=headers(client, owner)).status_code == 422


def test_owner_checkpoint_idempotency_bounds_ownership_and_archive(room_client):
    client, (owner, other, project, _, _) = room_client
    room = create_room(client, owner, project, "owner checkpoint")
    path = f"/api/v1/conversations/{room}"
    own = headers(client, owner)
    for content in ("first", "second"):
        assert client.post(f"{path}/events", json={"content": content}, headers=own).status_code == 201
    body = {"source_start_seq": 1, "source_end_seq": 2, "content": "Exact owner handoff"}
    keyed = {**own, "Idempotency-Key": "checkpoint-stable-retry"}

    created = client.post(f"{path}/summaries", json=body, headers=keyed)
    replay = client.post(f"{path}/summaries", json=body, headers=keyed)
    assert created.status_code == replay.status_code == 201
    assert replay.json()["event_id"] == created.json()["event_id"]
    assert created.json()["payload"] == {**body, "owner_checkpoint": True}
    conflict = client.post(f"{path}/summaries", json={**body, "content": "Changed"}, headers=keyed)
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "Idempotency key already used for a different event"
    assert client.post(f"{path}/summaries", json=body, headers=headers(client, other)).status_code == 404
    too_long = client.post(f"{path}/summaries", json={**body, "content": "x" * 8001}, headers=own)
    assert too_long.status_code == 422

    with client.app.state.session_factory() as db:
        assert db.query(RoomEvent).filter_by(room_id=room, kind="summary").count() == 1
    assert client.delete(path, headers=own).status_code == 204
    assert client.post(f"{path}/summaries", json=body, headers=own).status_code == 404
    archived = client.get(f"{path}/events", headers=own).json()
    assert [event["event_id"] for event in archived if event["kind"] == "summary"] == [created.json()["event_id"]]


def test_owner_checkpoint_rejects_gaps_and_busy_sources_then_retries(room_client):
    client, (owner, _, project, _, _) = room_client
    own = headers(client, owner)
    gap_room = create_room(client, owner, project, "checkpoint gap")
    gap_path = f"/api/v1/conversations/{gap_room}"
    with client.app.state.session_factory() as db:
        for content in ("one", "two", "three"):
            append_event(db, gap_room, ActorRef("user", owner), "message", {"content": content})
        db.delete(db.query(RoomEvent).filter_by(room_id=gap_room, seq=2).one())
        db.commit()
    gap = client.post(f"{gap_path}/summaries", json={"source_start_seq": 1, "source_end_seq": 3,
                                                      "content": "must be contiguous"}, headers=own)
    assert gap.status_code == 422
    with client.app.state.session_factory() as db:
        assert db.query(RoomEvent).filter_by(room_id=gap_room, kind="summary").count() == 0

    busy_room = create_room(client, owner, project, "checkpoint busy")
    busy_path = f"/api/v1/conversations/{busy_room}"
    with client.app.state.session_factory() as db:
        source = append_event(db, busy_room, ActorRef("user", owner), "message", {"content": "unfinished"})
        db.add(RoomTrigger(event_id=source.event_id, room_id=busy_room, state="pending",
                           selected_agents=[], max_rounds=2, max_tool_calls=8, max_executor_seconds=60))
        db.commit()
        source_id = source.event_id
    body = {"source_start_seq": 1, "source_end_seq": 1, "content": "retry after completion"}
    keyed = {**own, "Idempotency-Key": "busy-then-complete"}
    for state in ("pending", "running"):
        with client.app.state.session_factory() as db:
            db.get(RoomTrigger, source_id).state = state
            db.commit()
        blocked = client.post(f"{busy_path}/summaries", json=body, headers=keyed)
        assert blocked.status_code == 409
        assert blocked.json()["detail"] == {"code": "checkpoint_source_busy",
                                               "message": "Source range includes work that is still pending or running."}
    with client.app.state.session_factory() as db:
        assert db.query(RoomEvent).filter_by(room_id=busy_room, kind="summary").count() == 0
        db.get(RoomTrigger, source_id).state = "done"
        db.commit()
    completed = client.post(f"{busy_path}/summaries", json=body, headers=keyed)
    assert completed.status_code == 201
    assert completed.json()["payload"]["owner_checkpoint"] is True

    related_room = create_room(client, owner, project, "checkpoint reply still running")
    related_path = f"/api/v1/conversations/{related_room}"
    (agent_id,) = ready_agents(client, owner, related_room, ("Ada",))
    with client.app.state.session_factory() as db:
        parent = append_event(db, related_room, ActorRef("user", owner), "message", {"content": "parent request"})
        reply = append_event(db, related_room, ActorRef("agent", agent_id), "message",
                             {"content": "round one reply", "trigger_event_id": parent.event_id, "round": 1})
        db.add(RoomTrigger(event_id=parent.event_id, room_id=related_room, state="running",
                           selected_agents=[agent_id], max_rounds=2, max_tool_calls=8, max_executor_seconds=60))
        db.commit()
        parent_id, reply_seq = parent.event_id, reply.seq
    reply_only = {"source_start_seq": reply_seq, "source_end_seq": reply_seq,
                  "content": "reply is not complete while round two is running"}
    blocked = client.post(f"{related_path}/summaries", json=reply_only, headers=own)
    assert blocked.status_code == 409
    with client.app.state.session_factory() as db:
        assert db.query(RoomEvent).filter_by(room_id=related_room, kind="summary").count() == 0
        db.get(RoomTrigger, parent_id).state = "done"
        db.commit()
    assert client.post(f"{related_path}/summaries", json=reply_only, headers=own).status_code == 201


def test_latest_owner_checkpoint_survives_bounded_context_with_exact_coverage(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "durable handoff")
    other = create_room(client, owner, project, "private handoff")
    own = headers(client, owner)
    path = f"/api/v1/conversations/{room}"
    other_path = f"/api/v1/conversations/{other}"
    with client.app.state.session_factory() as db:
        for index in range(3):
            append_event(db, room, ActorRef("user", owner), "message", {"content": f"opening-{index}"})
    old_marker = "OLDER_HANDOFF_MUST_BE_SUPERSEDED"
    assert client.post(f"{path}/summaries", json={"source_start_seq": 1, "source_end_seq": 3,
                                                   "content": old_marker}, headers=own).status_code == 201
    with client.app.state.session_factory() as db:
        for index in range(3, 6):
            append_event(db, room, ActorRef("user", owner), "message", {"content": f"opening-{index}"})
    marker = "LATEST_OWNER_HANDOFF_MARKER_AT_FULL_LENGTH"
    handoff = "h" * (8000 - len(marker)) + marker
    latest = client.post(f"{path}/summaries", json={"source_start_seq": 1, "source_end_seq": 7,
                                                     "content": handoff}, headers=own)
    assert latest.status_code == 201, latest.text
    with client.app.state.session_factory() as db:
        for index in range(25):
            append_event(db, room, ActorRef("user", owner), "message", {"content": f"later-{index}"})
        append_event(db, other, ActorRef("user", owner), "message", {"content": "other source"})
    foreign_marker = "OTHER_ROOM_PRIVATE_HANDOFF"
    assert client.post(f"{other_path}/summaries", json={"source_start_seq": 1, "source_end_seq": 1,
                                                         "content": foreign_marker}, headers=own).status_code == 201
    agent_id = ready_agents(client, owner, room, ("Ada",))[0]
    from app.services.context import MAX_PROVIDER_CONTEXT_CHARS, build_turn_context, provider_text_size
    with client.app.state.session_factory() as db:
        run = db.get(RoomAgentRun, (room, agent_id))
        run.memory = {"durable": "ROOM_MEMORY_RETAINED"}
        db.commit()
        first = asyncio.run(build_turn_context(db, room, agent_id, 33))
        second = asyncio.run(build_turn_context(db, room, agent_id, 33))
    carried = [event for event in first.events if event["kind"] == "summary"
               and event["payload"].get("owner_checkpoint") is True]
    assert len(carried) == 1
    assert carried[0]["event_id"] == latest.json()["event_id"]
    assert carried[0]["payload"] == {"source_start_seq": 1, "source_end_seq": 7,
                                      "owner_checkpoint": True, "content": handoff}
    assert marker in json.dumps(first.events)
    assert old_marker not in json.dumps(first.events)
    assert foreign_marker not in json.dumps(first.events)
    assert "ROOM_MEMORY_RETAINED" in first.memory
    assert first.events == second.events
    assert provider_text_size(first) <= MAX_PROVIDER_CONTEXT_CHARS

    history = client.get(f"{path}/events", headers=own).json()
    owner_checkpoints = [event for event in history if event["kind"] == "summary"
                         and event["payload"].get("owner_checkpoint") is True]
    assert [event["payload"]["content"] for event in owner_checkpoints] == [old_marker, handoff]
    assert owner_checkpoints[-1]["payload"]["source_end_seq"] == 7


def test_long_room_uses_inspectable_bounded_extract_without_cross_room_content(room_client):
    client, (owner, _, project, _, _) = room_client
    room, other = [create_room(client, owner, project, name) for name in ("long", "private")]
    path = f"/api/v1/conversations/{room}"
    for i in range(25):
        content = f"item-{i}" + ("x" * 6000 if i == 23 else "")
        assert client.post(f"{path}/events", json={"content": content},
                           headers=headers(client, owner)).status_code == 201
    assert client.post(f"/api/v1/conversations/{other}/events", json={"content": "OTHER_ROOM_PRIVATE"},
                       headers=headers(client, owner)).status_code == 201
    agent_id = ready_agents(client, owner, room, ("Ada",))[0]
    from app.services.context import build_turn_context
    with client.app.state.session_factory() as db:
        db.get(RoomAgentRun, (room, agent_id)).checkpoint = {"task": "retained"}
        db.commit()
        first = asyncio.run(build_turn_context(db, room, agent_id, 25))
        second = asyncio.run(build_turn_context(db, room, agent_id, 25))
        run = db.get(RoomAgentRun, (room, agent_id))
        summaries = db.query(RoomEvent).filter_by(room_id=room, kind="summary").all()
        assert len(summaries) == 1
        assert summaries[0].payload["source_end_seq"] == 5
        assert summaries[0].payload["extractive_checkpoint"] is True
        assert run.checkpoint["context_summary"]["summary_event_id"] == summaries[0].event_id
        assert run.checkpoint["task"] == "retained"
        assert first.events == second.events
        assert len(first.events) == 21
        assert sum(len(json.dumps(event)) for event in first.events) < 100000
        assert "omitted" in first.events[-2]["payload"]["content"]
        assert "OTHER_ROOM_PRIVATE" not in json.dumps(first.events)


def test_queued_trigger_remains_full_and_identified_outside_recent_window(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "queued")
    other = create_room(client, owner, project, "private")
    agent_id = ready_agents(client, owner, room, ("Ada",))[0]
    from app.services.events import ActorRef, append_event
    from app.services.providers.base import attributed_messages
    from app.services.context import build_turn_context
    from app.services.llm_service import LLMFailure
    marker = "ESSENTIAL_TRIGGER_MARKER"
    with client.app.state.session_factory() as db:
        first = append_event(db, room, ActorRef("user", owner), "message",
                             {"content": "Original request " + "x" * 200 + marker})
        first_id = first.event_id
        for index in range(24):
            append_event(db, room, ActorRef("user", owner), "message", {"content": f"later-{index}"})
        foreign = append_event(db, other, ActorRef("user", owner), "message", {"content": "OTHER_ROOM_PRIVATE"})
        with pytest.raises(LLMFailure, match="not a committed user event"):
            asyncio.run(build_turn_context(db, room, agent_id, 25, trigger_event_id=foreign.event_id))
    seen = []

    async def provider(request):
        messages = attributed_messages(request)
        active = next(event for event in request.events if event["event_id"] == first_id)
        seen.append((request.trigger_event_id, active["payload"]["content"], messages))
        return ProviderResult(text="answered original")

    client.app.state.scheduler.provider = provider

    async def schedule():
        return await client.app.state.scheduler.schedule_user_event(
            room, first_id, wait=True, limits={"max_rounds": 1})

    replies = client.portal.call(schedule)
    assert [reply.content for reply in replies] == ["answered original"]
    assert seen[0][0] == first_id and marker in seen[0][1]
    assert "Active request" in seen[0][2][-1]["content"]
    assert "room seq 1" in seen[0][2][-1]["content"]
    assert sum("ACTIVE USER REQUEST" in message["content"] for message in seen[0][2]) == 1
    assert "later-23" in json.dumps(seen[0][2])
    assert "OTHER_ROOM_PRIVATE" not in json.dumps(seen[0][2])


def test_aggregate_context_budget_trims_recent_events_and_fails_visibly_if_unfit(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "budget")
    agent_id = ready_agents(client, owner, room, ("Ada",))[0]
    from app.services.context import build_turn_context, MAX_PROVIDER_CONTEXT_CHARS
    from app.services.events import ActorRef, append_event
    from app.services.llm_service import LLMFailure
    from app.services.providers.base import attributed_messages
    with client.app.state.session_factory() as db:
        first = append_event(db, room, ActorRef("user", owner), "message", {"content": "active request"})
        for index in range(20):
            append_event(db, room, ActorRef("user", owner), "message",
                         {"content": f"history-{index}:" + "y" * 4000})
        request = asyncio.run(build_turn_context(db, room, agent_id, 21, trigger_event_id=first.event_id))
        assert len(request.events) < 21
        assert any(event["event_id"] == first.event_id and event["payload"]["content"] == "active request"
                   for event in request.events)
        assert any(event["kind"] == "summary" and event["payload"]["extractive_checkpoint"]
                   for event in request.events)
        assert sum(len(message["content"]) for message in attributed_messages(request)) + len(request.system) <= MAX_PROVIDER_CONTEXT_CHARS
        db.get(Agent, agent_id).system_prompt = "z" * (MAX_PROVIDER_CONTEXT_CHARS + 1)
        db.commit()
        with pytest.raises(LLMFailure, match="context budget"):
            asyncio.run(build_turn_context(db, room, agent_id, 21, trigger_event_id=first.event_id))


def test_tool_continuation_cannot_bypass_aggregate_context_budget(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "continuation-budget")
    agent_id = ready_agents(client, owner, room, ("Ada",))[0]

    class Broker:
        tool_schemas = [{"name": "read_file", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}]

        async def execute(self, _action, _grant, _cancel, _budget):
            from app.services.tool_broker import ActionResult
            return ActionResult("completed", "observed", safe_text="observed")

    calls = 0

    async def provider(_request):
        nonlocal calls
        calls += 1
        return ProviderResult(tool_calls=[ToolCall("large", "read_file", {})],
                              continuation={"call_ids": ["large"], "raw": "x" * 33000})

    client.app.state.scheduler.broker = Broker()
    client.app.state.scheduler.provider = provider
    response = client.post(f"/api/v1/conversations/{room}/messages", json={"content": "inspect"},
                           headers=headers(client, owner))
    assert response.status_code == 502, response.text
    assert response.json()["detail"]["code"] == "context_too_large"
    assert calls == 1


def test_oversized_current_user_message_fails_visibly(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "giant")
    agent_id = ready_agents(client, owner, room, ("Ada",))[0]
    from app.services.context import build_turn_context
    from app.services.llm_service import LLMFailure
    path = f"/api/v1/conversations/{room}/events"
    assert client.post(path, json={"content": "x" * 12001}, headers=headers(client, owner)).status_code == 201
    with client.app.state.session_factory() as db:
        with pytest.raises(LLMFailure, match="shorter request"):
            asyncio.run(build_turn_context(db, room, agent_id, 1))


def test_pointer_contract_accepts_negative_virtual_desktop_origin():
    from jsonschema import validate
    from app.services.tool_broker import SCHEMAS
    for family in ("pointer_click", "pointer_move", "pointer_scroll"):
        arguments = {"x": -500, "y": -200}
        if family == "pointer_scroll":
            arguments["delta"] = -120
        validate(arguments, SCHEMAS[family])


def test_agent_pass_releases_guard_for_next_user_request(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "pass")
    agent_id = ready_agents(client, owner, room, ("Ada",))[0]
    calls = []

    async def provider(request):
        calls.append(request.watermark_seq)
        return ProviderResult(text="" if len(calls) == 1 else "next reply")

    client.app.state.scheduler.provider = provider
    path = f"/api/v1/conversations/{room}/messages"
    assert client.post(path, json={"content": "first"}, headers=headers(client, owner)).json() == []
    second = client.post(path, json={"content": "second"}, headers=headers(client, owner))
    assert second.status_code == 201 and second.json()[0]["content"] == "next reply"
    with client.app.state.session_factory() as db:
        assert db.get(RoomAgentRun, (room, agent_id)).state == "idle"
        assert db.query(RoomEvent).filter_by(room_id=room, kind="activity").one().payload["status"] == "passed"


def test_restart_recovers_committed_user_event_without_trigger(room_client, sqlite_url):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "gap")
    agent_id = ready_agents(client, owner, room, ("Ada",))[0]
    from app.services.events import ActorRef, append_event
    from app.main import create_app
    from fastapi.testclient import TestClient
    with client.app.state.session_factory() as db:
        db.get(Agent, agent_id).connection_id = None
        db.commit()
        user_event = append_event(db, room, ActorRef("user", owner), "message", {"content": "committed"})
        event_id = user_event.event_id
        assert db.get(RoomTrigger, event_id) is None
    client.__exit__(None, None, None)  # End the first backend before restart.
    with TestClient(create_app(database_url=sqlite_url)) as restarted:
        deadline = monotonic() + 3
        while monotonic() < deadline:
            with restarted.app.state.session_factory() as db:
                trigger = db.get(RoomTrigger, event_id)
                if trigger and trigger.state == "done":
                    assert db.query(RoomEvent).filter_by(room_id=room, kind="failure").one().payload["code"] == "provider_not_configured"
                    break
            sleep(0.01)
        else:
            pytest.fail("Committed user event was not recovered")


def test_unsupported_fake_tool_call_never_reaches_broker(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "invalid-tool")
    ready_agents(client, owner, room, ("Ada",))
    calls = []

    class Broker:
        tool_schemas = [{"name": "read_file", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}]

        async def execute(self, action, grant, cancel, budget):
            calls.append(action)
            raise AssertionError("Unknown family must not execute")

    async def provider(request):
        return ProviderResult(tool_calls=[ToolCall("call-1", "delete_file", {})],
                              continuation={"call_ids": ["call-1"]})

    client.app.state.scheduler.broker = Broker()
    client.app.state.scheduler.provider = provider
    response = client.post(f"/api/v1/conversations/{room}/messages", json={"content": "try"}, headers=headers(client, owner))
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "provider_invalid_response"
    assert calls == []


@pytest.mark.parametrize("vision", [False, True])
def test_screenshot_bytes_only_reach_verified_vision_adapter(room_client, vision):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "screen")
    agent_id = ready_agents(client, owner, room, ("Ada",))[0]
    with client.app.state.session_factory() as db:
        connection = db.get(ProviderConnection, db.get(Agent, agent_id).connection_id)
        connection.capabilities = {"declared": {"tools": True, "vision": vision},
            "verified": {"model_id": "exact", "base_url": connection.base_url, "tools": True, "vision": vision}}
        db.commit()
    observations = []

    @dataclass
    class Outcome:
        state: str = "completed"
        text: str = "screenshot saved"
        executor_seconds: float = 0.1
        screenshot_ref: str = "room-artifact.png"
        image_bytes: bytes = b"synthetic image"
        image_mime: str = "image/png"
        image_width: int = 16
        image_height: int = 16

    class Broker:
        tool_schemas = [{"name": "screenshot", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}]

        async def execute(self, action, grant, cancel, budget):
            return Outcome()

    async def provider(request):
        if request.observations:
            observations.extend(request.observations)
            return ProviderResult(text="done")
        return ProviderResult(tool_calls=[ToolCall("screen-1", "screenshot", {})],
                              continuation={"call_ids": ["screen-1"]})

    client.app.state.scheduler.broker = Broker()
    client.app.state.scheduler.provider = provider
    response = client.post(f"/api/v1/conversations/{room}/messages", json={"content": "see"}, headers=headers(client, owner))
    assert response.status_code == 201, response.text
    assert observations[0].screenshot_ref == "room-artifact.png"
    assert observations[0].image_bytes == (b"synthetic image" if vision else None)
    with client.app.state.session_factory() as db:
        event = db.query(RoomEvent).filter_by(room_id=room, kind="tool_result").one()
        assert event.payload["screenshot_ref"] == "room-artifact.png"
        assert "image_bytes" not in event.payload


def test_approval_wait_is_cancellable_and_pending_call_identity_is_durable(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "approval-wait")
    agent_id = ready_agents(client, owner, room, ("Ada",))[0]
    with client.app.state.session_factory() as db:
        connection = db.get(ProviderConnection, db.get(Agent, agent_id).connection_id)
        connection.capabilities = {"declared": {"tools": True},
            "verified": {"model_id": "exact", "base_url": connection.base_url, "tools": True}}
        db.get(RoomAgentRun, (room, agent_id)).pending_task = {"task": "keep this room plan"}
        db.commit()
    entered = threading.Event()
    tokens = []

    class Broker:
        tool_schemas = [{"name": "read_file", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}]

        async def execute(self, action, grant, cancel, budget):
            tokens.append(cancel)
            entered.set()
            await cancel.wait()  # Synthetic approval wait; no executor seconds consumed.
            raise AssertionError("Cancelled approval must not execute")

    async def provider(request):
        if request.observations:
            raise AssertionError("Cancelled tool must not continue")
        return ProviderResult(tool_calls=[ToolCall("approval-1", "read_file", {})],
                              continuation={"call_ids": ["approval-1"], "raw": "PRIVATE_BEFORE_CONSENT"})

    client.app.state.scheduler.broker = Broker()
    client.app.state.scheduler.provider = provider
    path = f"/api/v1/conversations/{room}"
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"{path}/messages", json={"content": "read"}, headers=headers(client, owner))
        try:
            assert entered.wait(3)
            with client.app.state.session_factory() as db:
                run = db.get(RoomAgentRun, (room, agent_id))
                assert run.active_tool["call_ids"] == ["approval-1"]
                assert "PRIVATE_BEFORE_CONSENT" not in json.dumps(run.active_tool)
                action = db.query(ToolAction).filter_by(room_id=room).one()
                assert action.call_id == "approval-1" and action.trigger_event_id == run.trigger_event_id
        finally:
            assert client.post(f"{path}/members/{agent_id}/pause", headers=headers(client, owner)).status_code == 200
        assert pending.result(timeout=3).json() == []
    assert tokens[0].is_set()
    with client.app.state.session_factory() as db:
        run = db.get(RoomAgentRun, (room, agent_id))
        assert run.active_tool == {}
        assert run.pending_task == {"task": "keep this room plan"}
        assert db.query(RoomEvent).filter_by(room_id=room, kind="tool_result").count() == 0


def test_real_broker_keeps_unapproved_file_content_out_of_sqlite(room_client, tmp_path):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "private-proposal")
    agent_id = ready_agents(client, owner, room, ("Ada",))[0]
    enable_tools(client, agent_id)
    target = tmp_path / "unapproved.txt"
    marker = "PRIVATE_BEFORE_CONSENT"

    async def provider(request):
        assert not request.observations
        return ProviderResult(tool_calls=[ToolCall("private-call", "write_file",
                            {"path": str(target), "content": marker})],
                              continuation={"call_ids": ["private-call"], "raw": marker})

    client.app.state.scheduler.provider = provider
    path = f"/api/v1/conversations/{room}"
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"{path}/messages", json={"content": "write"},
                              headers=headers(client, owner))
        try:
            deadline = monotonic() + 3
            while monotonic() < deadline:
                with client.app.state.session_factory() as db:
                    action = db.query(ToolAction).filter_by(room_id=room).first()
                    if action and action.state == "waiting":
                        run = db.get(RoomAgentRun, (room, agent_id))
                        assert marker not in json.dumps(action.arguments)
                        assert marker not in json.dumps(run.active_tool)
                        break
                sleep(.02)
            else:
                pytest.fail("Unapproved action did not enter waiting state")
        finally:
            assert client.post(f"{path}/cancel", headers=headers(client, owner)).status_code == 204
        assert pending.result(timeout=3).status_code == 201
    assert not target.exists()


def test_new_trigger_during_worker_exit_is_not_lost(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "wake-race")
    ready_agents(client, owner, room, ("Ada",))
    entered, release = threading.Event(), threading.Event()
    scheduler = client.app.state.scheduler
    original_drain = scheduler._drain
    visits = 0

    async def delayed_drain(room_id):
        nonlocal visits
        visits += 1
        await original_drain(room_id)
        if visits == 1:
            entered.set()  # Old worker has decided there is no more work but is not done.
            await asyncio.to_thread(release.wait)

    async def provider(request):
        return ProviderResult(text="done")

    scheduler._drain = delayed_drain
    scheduler.provider = provider
    path = f"/api/v1/conversations/{room}/events"
    first = client.post(path, json={"content": "first"}, headers=headers(client, owner))
    assert first.status_code == 201
    try:
        assert entered.wait(3)
        second = client.post(path, json={"content": "second"}, headers=headers(client, owner))
        assert second.status_code == 201
    finally:
        release.set()
    deadline = monotonic() + 3
    while monotonic() < deadline:
        with client.app.state.session_factory() as db:
            trigger = db.get(RoomTrigger, second.json()["event_id"])
            if trigger and trigger.state == "done":
                assert db.query(RoomEvent).filter_by(room_id=room, actor_type="agent", kind="message").count() == 2
                break
        sleep(0.01)
    else:
        pytest.fail("Trigger was lost during worker exit")


def test_two_agents_can_use_same_native_call_id_in_one_trigger(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "same-native-call-id")
    agents = ready_agents(client, owner, room, ("Ada", "Ben"))
    enable_tools(client, agents[0])

    @dataclass
    class Outcome:
        state: str = "completed"
        text: str = "observed"
        executor_seconds: float = 0.1

    class Broker:
        tool_schemas = [{"name": "read_file", "parameters": {"type": "object", "properties": {}}}]

        async def execute(self, action, grant, cancel, budget=None):
            return Outcome()

    async def provider(request):
        if request.watermark_seq == 1 and not request.observations:
            return ProviderResult(tool_calls=[ToolCall("call-1", "read_file", {})],
                                  continuation={"call_ids": ["call-1"]})
        return ProviderResult(text=f"answer {request.agent_id}")

    client.app.state.scheduler.broker = Broker()
    client.app.state.scheduler.provider = provider
    response = client.post(f"/api/v1/conversations/{room}/messages", json={"content": "read"},
                           headers=headers(client, owner))
    assert response.status_code == 201, response.text
    assert len(response.json()) == 4
    with client.app.state.session_factory() as db:
        actions = db.query(ToolAction).filter_by(room_id=room).order_by(ToolAction.agent_id).all()
        assert len(actions) == 2
        assert {action.call_id for action in actions} == {"call-1"}
        assert {action.agent_id for action in actions} == set(agents)
        assert len({action.action_id for action in actions}) == 2


def test_one_agent_can_reuse_native_call_id_in_later_provider_response(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "native-call-reuse")
    agent_id = ready_agents(client, owner, room, ("Ada",))[0]
    enable_tools(client, agent_id)
    calls = 0

    @dataclass
    class Outcome:
        state: str = "completed"
        text: str = "observed"
        executor_seconds: float = 0.1

    class Broker:
        tool_schemas = [{"name": "read_file", "parameters": {"type": "object", "properties": {}}}]

        async def execute(self, action, grant, cancel, budget=None):
            return Outcome()

    async def provider(request):
        nonlocal calls
        calls += 1
        if calls < 3:
            return ProviderResult(tool_calls=[ToolCall("call-1", "read_file", {})],
                                  continuation={"call_ids": ["call-1"]})
        return ProviderResult(text="done")

    client.app.state.scheduler.broker = Broker()
    client.app.state.scheduler.provider = provider
    response = client.post(f"/api/v1/conversations/{room}/messages", json={"content": "read twice"},
                           headers=headers(client, owner))
    assert response.status_code == 201, response.text
    assert response.json()[0]["content"] == "done"
    assert calls == 3
    with client.app.state.session_factory() as db:
        actions = db.query(ToolAction).filter_by(room_id=room).all()
        assert len(actions) == 2
        assert {action.call_id for action in actions} == {"call-1"}
        assert len({action.action_id for action in actions}) == 2


def test_executor_budget_starts_after_approval_and_stops_action(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "execution-time-bound")
    agent_id = ready_agents(client, owner, room, ("Ada",))[0]
    enable_tools(client, agent_id)
    phases = []

    class Broker:
        tool_schemas = [{"name": "read_file", "parameters": {"type": "object", "properties": {}}}]

        async def execute(self, action, grant, cancel, budget):
            phases.append(("approval", budget.remaining_seconds))
            await asyncio.sleep(1.1)  # Longer than the execution budget, but uncharged.
            phases.append(("execution", budget.remaining_seconds))
            await budget.run(asyncio.sleep(10))
            raise AssertionError("Execution should be cancelled at the bound")

    async def provider(request):
        return ProviderResult(tool_calls=[ToolCall("call-1", "read_file", {})],
                              continuation={"call_ids": ["call-1"]})

    client.app.state.scheduler.broker = Broker()
    client.app.state.scheduler.provider = provider
    path = f"/api/v1/conversations/{room}"
    assert client.put(f"{path}/schedule", json={"max_rounds": 1, "max_tool_calls": 1,
                                                 "max_executor_seconds": 1}, headers=headers(client, owner)).status_code == 200
    started = monotonic()
    response = client.post(f"{path}/messages", json={"content": "run"}, headers=headers(client, owner))
    duration = monotonic() - started
    assert response.status_code == 502, response.text
    assert response.json()["detail"]["code"] == "executor_budget_exhausted"
    assert phases == [("approval", 1), ("execution", 1)]
    assert 2 <= duration < 4
    with client.app.state.session_factory() as db:
        assert db.query(ToolAction).filter_by(room_id=room).one().state == "uncertain"
        assert db.query(RoomEvent).filter_by(room_id=room, kind="tool_result").count() == 0


@pytest.mark.asyncio
async def test_executor_budget_rejects_result_that_suppressed_timeout_cancellation():
    from app.services.scheduler import ExecutorBudget
    from app.services.llm_service import LLMFailure

    async def ignores_cancellation():
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            return "fake success"

    with pytest.raises(LLMFailure) as error:
        await ExecutorBudget(0.01).run(ignores_cancellation())
    assert error.value.code == "executor_budget_exhausted"
