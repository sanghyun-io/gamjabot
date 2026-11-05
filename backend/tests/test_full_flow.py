"""End-to-end synthetic room, consent, native observation, and recovery contract."""

import base64
import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep

import httpx
import pytest

from app.models.collaboration import ProviderConnection, RoomEvent, ToolAction
from app.models.models import Agent
from app.services.providers import ProviderResult, ToolCall
from app.services.tools import LocalExecutor
from test_room_state import create_room, headers, room_client
from test_scheduler import enable_tools, ready_agents
from test_tool_grants import ObservableNativeFixture, _wait_for_action, inspected_approval


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
)


def _wire_call(call_id="wire-1", family="screenshot"):
    return {"id": call_id, "type": "function", "function": {
        "name": "gamja_" + family.encode().hex(),
        "arguments": '{"workflow_id":"wire-flow"}'}}


def _wire_room(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "wire")
    (ada,) = ready_agents(client, owner, room, ("Ada",))
    enable_tools(client, ada)
    return client, owner, room, ada


def _native_screen(client, tmp_path, *, action=None):
    class FakeNative(ObservableNativeFixture):
        def __init__(self):
            self.operations = []

        async def call(self, body):
            self.operations.append(body["operation"])
            if body["operation"] == "action":
                if action:
                    return await action(body)
                return {"state": "completed", "text": "synthetic pixel",
                        "png_base64": base64.b64encode(PNG).decode(), "width": 1, "height": 1}
            return {"state": "completed"}

    native = FakeNative()
    desktop = client.app.state.local_executor.desktop
    desktop.native = desktop.lease.native = native
    desktop.app_data = tmp_path
    desktop.lease.lock_path = tmp_path / "host.lock"
    return native


def test_missing_desktop_workflow_gets_one_host_owned_turn_id(room_client, tmp_path):
    client, owner, room, agent = _wire_room(room_client)
    _native_screen(client, tmp_path)

    async def provider(request):
        if request.observations:
            if request.observations[0].call_id == "capture-1":
                return ProviderResult(tool_calls=[ToolCall("pointer-1", "pointer_move", {"x": 1, "y": 2})],
                                      continuation={"call_ids": ["pointer-1"]})
            return ProviderResult(text="synthetic capture finished")
        return ProviderResult(tool_calls=[ToolCall("capture-1", "screenshot", {})],
                              continuation={"call_ids": ["capture-1"]})

    client.app.state.scheduler.provider = provider
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"/api/v1/conversations/{room}/messages",
                              json={"content": "synthetic request"}, headers=headers(client, owner))
        action_id = _wait_for_action(client, room)
        with client.app.state.session_factory() as db:
            action = db.get(ToolAction, action_id)
            assert action.agent_id == agent and action.workflow_id
            workflow_id = action.workflow_id
        decision = client.post(f"/api/v1/tools/rooms/{room}/actions/{action_id}/decision",
                               json=inspected_approval(client, owner, room, action_id, "once"),
                               headers=headers(client, owner))
        assert decision.status_code == 200
        deadline = monotonic() + 3
        second = None
        while monotonic() < deadline:
            with client.app.state.session_factory() as db:
                second = db.query(ToolAction).filter_by(room_id=room, state="waiting").first()
                if second and second.action_id != action_id:
                    assert second.workflow_id == workflow_id
                    second_id = second.action_id
                    break
            sleep(.02)
        else:
            raise AssertionError("Pointer action did not request approval")
        decision = client.post(f"/api/v1/tools/rooms/{room}/actions/{second_id}/decision",
                               json=inspected_approval(client, owner, room, second_id, "once"),
                               headers=headers(client, owner))
        assert decision.status_code == 200
        assert pending.result(timeout=5).status_code == 201


def test_host_workflow_is_distinct_on_next_turn(room_client, tmp_path):
    client, owner, room, _ = _wire_room(room_client)
    _native_screen(client, tmp_path)

    async def provider(request):
        if request.observations:
            return ProviderResult(text="synthetic completion")
        return ProviderResult(tool_calls=[ToolCall("capture-next", "screenshot", {})],
                              continuation={"call_ids": ["capture-next"]})

    client.app.state.scheduler.provider = provider
    workflows = []
    for content in ("first synthetic turn", "second synthetic turn"):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(client.post, f"/api/v1/conversations/{room}/messages",
                                  json={"content": content}, headers=headers(client, owner))
            deadline = monotonic() + 3
            while monotonic() < deadline:
                with client.app.state.session_factory() as db:
                    action = db.query(ToolAction).filter_by(room_id=room, state="waiting").first()
                    if action:
                        workflows.append(action.workflow_id)
                        action_id = action.action_id
                        break
                sleep(.02)
            else:
                raise AssertionError("Screenshot action did not request approval")
            decision = client.post(f"/api/v1/tools/rooms/{room}/actions/{action_id}/decision",
                                   json=inspected_approval(client, owner, room, action_id, "once"),
                                   headers=headers(client, owner))
            assert decision.status_code == 200
            assert pending.result(timeout=5).status_code == 201
    assert len(workflows) == 2 and workflows[0] != workflows[1]


def test_packaged_browser_selects_bundled_engine(monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/synthetic/installed/resources")
    assert LocalExecutor().browser.engine == "bundled"


def test_two_rooms_real_consent_broker_native_observation_and_peer(room_client, sqlite_url, tmp_path):
    client, (owner, _, project, _, _) = room_client
    room_a = create_room(client, owner, project, "A")
    room_b = create_room(client, owner, project, "B")
    ada, ben = ready_agents(client, owner, room_a, ("Ada", "Ben"))
    (cara,) = ready_agents(client, owner, room_b, ("Cara",))
    assert client.post(f"/api/v1/conversations/{room_b}/members/{ada}",
                       headers=headers(client, owner)).status_code == 201
    enable_tools(client, ada)
    with client.app.state.session_factory() as db:
        connection = db.get(ProviderConnection, db.get(Agent, ada).connection_id)
        connection.capabilities = {"declared": {"tools": True, "vision": True},
            "verified": {"model_id": "exact", "base_url": connection.base_url,
                         "tools": True, "vision": True}}
        db.commit()

    class FakeNative(ObservableNativeFixture):
        def __init__(self):
            self.operations = []

        async def call(self, body):
            self.operations.append(body["operation"])
            if body["operation"] == "action":
                assert body["family"] == "screenshot"
                return {"state": "completed", "text": "synthetic screenshot pixel",
                        "png_base64": base64.b64encode(PNG).decode(), "width": 1, "height": 1}
            return {"state": "completed"}

    native = FakeNative()
    desktop = client.app.state.local_executor.desktop
    desktop.native = desktop.lease.native = native
    desktop.app_data = tmp_path
    desktop.lease.lock_path = tmp_path / "host.lock"
    seen = []

    def provider_wire(request):
        body = json.loads(request.content)
        seen.append(body)
        messages = body["messages"]
        system = messages[0]["content"]
        context = json.dumps(messages)
        if system == "system-Ada" and "capture" in context:
            if any(message.get("role") == "tool" for message in messages):
                assert "synthetic screenshot pixel" in context
                assert "data:image/png;base64" in context
                return httpx.Response(200, json={"choices": [{"message": {"content": "Ada saw synthetic pixel"}}]})
            if "Ada saw synthetic pixel" not in context:
                return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": [{
                    "id": "capture-1", "type": "function", "function": {
                        "name": "gamja_73637265656e73686f74",
                        "arguments": '{"workflow_id":"flow-a"}'}}]}}]})
        if system == "system-Ben" and "Ada saw synthetic pixel" in context:
            assert "synthetic screenshot pixel" in context
            assert f"rooms/{room_a}/screenshots/" in context
            return httpx.Response(200, json={"choices": [{"message": {
                "content": "Ben received Ada's observation"}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": f"reply {system}"}}]})

    client.app.state.connection_transport = httpx.MockTransport(provider_wire)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending_a = pool.submit(client.post, f"/api/v1/conversations/{room_a}/messages",
                                json={"content": "capture"}, headers=headers(client, owner))
        action_id = _wait_for_action(client, room_a)
        pending_b = pool.submit(client.post, f"/api/v1/conversations/{room_b}/messages",
                                json={"content": "room B"}, headers=headers(client, owner))
        assert pending_b.result(timeout=5).status_code == 201
        assert not pending_a.done()
        decision = client.post(f"/api/v1/tools/rooms/{room_a}/actions/{action_id}/decision",
                               json=inspected_approval(client, owner, room_a, action_id, "once"),
                               headers=headers(client, owner))
        assert decision.status_code == 200, decision.text
        assert pending_a.result(timeout=5).status_code == 201

    with client.app.state.session_factory() as db:
        actions = db.query(ToolAction).filter_by(room_id=room_a).all()
        assert len(actions) == 1 and actions[0].state == "completed"
        assert actions[0].agent_id == ada and actions[0].call_id == "capture-1"
        events_a = db.query(RoomEvent).filter_by(room_id=room_a).order_by(RoomEvent.seq).all()
        events_b = db.query(RoomEvent).filter_by(room_id=room_b).order_by(RoomEvent.seq).all()
        tool_result = next(event for event in events_a if event.kind == "tool_result")
        assert tool_result.payload["screenshot_ref"] == f"rooms/{room_a}/screenshots/{action_id}.png"
        assert "synthetic screenshot pixel" in tool_result.payload["text"]
        assert any(event.kind == "message" and event.payload.get("content") ==
                   "Ben received Ada's observation" for event in events_a)
        assert all(event.room_id == room_b for event in events_b)
        assert not any(event.kind == "tool_result" for event in events_b)
        assert next(event for event in events_a if event.kind == "message" and
                    event.payload.get("content") == "Ada saw synthetic pixel").payload["model_use"]["model_id"] == "exact"
        assert (tmp_path / f"rooms/{room_a}/screenshots/{action_id}.png").read_bytes() == PNG
        cursor = events_a[1].seq
    replay = client.get(f"/api/v1/conversations/{room_a}/events?after_seq={cursor}",
                        headers=headers(client, owner)).json()
    assert replay and all(event["seq"] > cursor for event in replay)
    assert len({event["event_id"] for event in replay}) == len(replay)
    assert native.operations == ["lease_acquire", "action", "lease_release"]
    assert any(request["messages"][0]["content"] == "system-Ada" and
               "room B" in json.dumps(request["messages"]) for request in seen)
    assert any(request["messages"][0]["content"] == "system-Cara" for request in seen)
    assert any(request["messages"][0]["content"] == "system-Ben" for request in seen)
    assert not any("synthetic screenshot pixel" in json.dumps(request["messages"]) and
                   "room B" in json.dumps(request["messages"]) for request in seen)
    client.__exit__(None, None, None)
    from app.main import create_app
    from fastapi.testclient import TestClient
    with TestClient(create_app(database_url=sqlite_url)) as restarted:
        with restarted.app.state.session_factory() as db:
            assert db.query(RoomEvent).filter_by(room_id=room_a, kind="tool_result").count() == 1
            assert db.query(RoomEvent).filter_by(room_id=room_b, kind="tool_result").count() == 0
            assert db.get(ToolAction, action_id).state == "completed"


def test_saved_compatible_adapter_tool_call_reaches_real_consent_broker(room_client, tmp_path):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "wire")
    (ada,) = ready_agents(client, owner, room, ("Ada",))
    enable_tools(client, ada)
    wire_requests = []

    def provider_wire(request):
        body = json.loads(request.content)
        wire_requests.append(body)
        if any(message.get("role") == "tool" for message in body["messages"]):
            return httpx.Response(200, json={"choices": [{"message": {"content": "Observed pixel"}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": [{
            "id": "wire-screen-1", "type": "function",
            "function": {"name": "gamja_73637265656e73686f74", "arguments": "{\"workflow_id\":\"wire-flow\"}"}}]}}]})

    class FakeNative(ObservableNativeFixture):
        async def call(self, body):
            if body["operation"] == "action":
                return {"state": "completed", "png_base64": base64.b64encode(PNG).decode(),
                        "width": 1, "height": 1}
            return {"state": "completed"}

    desktop = client.app.state.local_executor.desktop
    desktop.native = desktop.lease.native = FakeNative()
    desktop.app_data = tmp_path
    desktop.lease.lock_path = tmp_path / "host.lock"
    client.app.state.connection_transport = httpx.MockTransport(provider_wire)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"/api/v1/conversations/{room}/messages",
                              json={"content": "capture"}, headers=headers(client, owner))
        action_id = _wait_for_action(client, room)
        decision = client.post(f"/api/v1/tools/rooms/{room}/actions/{action_id}/decision",
                               json=inspected_approval(client, owner, room, action_id, "once"),
                               headers=headers(client, owner))
        assert decision.status_code == 200, decision.text
        response = pending.result(timeout=5)
    assert response.status_code == 201, response.text
    assert response.json()[0]["content"] == "Observed pixel"
    assert len(wire_requests) == 2
    assert wire_requests[1]["messages"][-1]["tool_call_id"] == "wire-screen-1"
    with client.app.state.session_factory() as db:
        assert db.get(ToolAction, action_id).state == "completed"
        assert db.query(RoomEvent).filter_by(room_id=room, kind="tool_result").one().payload["call_id"] == "wire-screen-1"


def test_default_eight_call_budget_rejects_ninth_before_consent(room_client):
    client, owner, room, _ = _wire_room(room_client)
    wire_calls = []

    def provider_wire(request):
        wire_calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": [
            _wire_call(f"call-{number}") for number in range(9)]}}]})

    client.app.state.connection_transport = httpx.MockTransport(provider_wire)
    response = client.post(f"/api/v1/conversations/{room}/messages",
                           json={"content": "many captures"}, headers=headers(client, owner))
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "tool_budget_exhausted"
    with client.app.state.session_factory() as db:
        assert db.query(ToolAction).filter_by(room_id=room).count() == 0
    assert len(wire_calls) == 1


def test_unsupported_wire_tool_is_rejected_before_broker(room_client):
    client, owner, room, _ = _wire_room(room_client)
    client.app.state.connection_transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": "", "tool_calls": [_wire_call(family="nonexistent_tool")]}}]}))
    response = client.post(f"/api/v1/conversations/{room}/messages",
                           json={"content": "bad tool"}, headers=headers(client, owner))
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "provider_invalid_response"
    with client.app.state.session_factory() as db:
        assert db.query(ToolAction).filter_by(room_id=room).count() == 0
        assert db.query(RoomEvent).filter_by(room_id=room, kind="failure").one().payload["code"] == "provider_invalid_response"


@pytest.mark.parametrize("capability", ["absent", "false"])
def test_saved_model_without_tool_capability_rejects_supported_wire_call(room_client, capability):
    client, owner, room, ada = _wire_room(room_client)
    with client.app.state.session_factory() as db:
        connection = db.get(ProviderConnection, db.get(Agent, ada).connection_id)
        connection.capabilities = ({"declared": {}, "verified": {}} if capability == "absent" else
                                   {"declared": {"tools": False},
                                    "verified": {"model_id": "exact", "base_url": connection.base_url,
                                                 "tools": False}})
        db.commit()
    wire_requests = []

    def provider_wire(request):
        wire_requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "", "tool_calls": [_wire_call()]}}]})

    client.app.state.connection_transport = httpx.MockTransport(provider_wire)
    response = client.post(f"/api/v1/conversations/{room}/messages",
                           json={"content": "capture"}, headers=headers(client, owner))
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "provider_invalid_response"
    assert len(wire_requests) == 1 and "tools" not in wire_requests[0]
    with client.app.state.session_factory() as db:
        assert db.query(ToolAction).filter_by(room_id=room).count() == 0
        assert db.query(RoomEvent).filter_by(room_id=room, kind="failure").one().payload["code"] == "provider_invalid_response"


def test_unverified_vision_keeps_png_out_of_wire_continuation(room_client, tmp_path):
    client, owner, room, _ = _wire_room(room_client)
    native = _native_screen(client, tmp_path)
    wire_requests = []

    def provider_wire(request):
        body = json.loads(request.content)
        wire_requests.append(body)
        if any(message.get("role") == "tool" for message in body["messages"]):
            assert "synthetic pixel" in json.dumps(body)
            assert "data:image/png;base64" not in json.dumps(body)
            return httpx.Response(200, json={"choices": [{"message": {"content": "Text only"}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": [_wire_call()]}}]})

    client.app.state.connection_transport = httpx.MockTransport(provider_wire)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"/api/v1/conversations/{room}/messages",
                              json={"content": "capture"}, headers=headers(client, owner))
        action_id = _wait_for_action(client, room)
        assert client.post(f"/api/v1/tools/rooms/{room}/actions/{action_id}/decision",
                           json=inspected_approval(client, owner, room, action_id, "once"),
                           headers=headers(client, owner)).status_code == 200
        assert pending.result(timeout=5).status_code == 201
    assert len(wire_requests) == 2
    assert native.operations == ["lease_acquire", "action", "lease_release"]
    with client.app.state.session_factory() as db:
        observation = db.query(RoomEvent).filter_by(room_id=room, kind="tool_result").one().payload
        assert observation["screenshot_ref"] == f"rooms/{room}/screenshots/{action_id}.png"
        assert "synthetic pixel" in observation["text"]


@pytest.mark.parametrize("termination", ["cancel", "stop"])
def test_wire_tool_waiting_for_consent_terminates_without_native_effect(room_client, tmp_path, termination):
    client, owner, room, _ = _wire_room(room_client)
    native = _native_screen(client, tmp_path)
    client.app.state.connection_transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": "", "tool_calls": [_wire_call()]}}]}))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"/api/v1/conversations/{room}/messages",
                              json={"content": "capture"}, headers=headers(client, owner))
        action_id = _wait_for_action(client, room)
        assert client.post(f"/api/v1/conversations/{room}/{termination}",
                           headers=headers(client, owner)).status_code == 204
        pending.result(timeout=5)
    assert native.operations == []
    with client.app.state.session_factory() as db:
        assert db.get(ToolAction, action_id).state == "cancelled"
        assert db.query(RoomEvent).filter_by(room_id=room, kind="tool_result").one().payload["state"] == "cancelled"


@pytest.mark.parametrize("termination", ["cancel", "revoke", "stop"])
def test_wire_tool_running_native_effect_terminates_uncertain(room_client, sqlite_url, tmp_path, termination):
    client, owner, room, _ = _wire_room(room_client)
    entered, stopped = threading.Event(), threading.Event()

    async def slow_action(_body):
        entered.set()
        try:
            await asyncio.sleep(30)
        finally:
            stopped.set()

    native = _native_screen(client, tmp_path, action=slow_action)
    client.app.state.connection_transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": "", "tool_calls": [_wire_call()]}}]}))
    path = f"/api/v1/tools/rooms/{room}"
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"/api/v1/conversations/{room}/messages",
                              json={"content": "capture"}, headers=headers(client, owner))
        action_id = _wait_for_action(client, room)
        approved = client.post(f"{path}/actions/{action_id}/decision",
                               json=inspected_approval(client, owner, room, action_id, "room_session"),
                               headers=headers(client, owner))
        assert approved.status_code == 200, approved.text
        assert entered.wait(3)
        if termination == "revoke":
            response = client.post(f"{path}/grants/{approved.json()['grant_id']}/revoke",
                                   headers=headers(client, owner))
        else:
            response = client.post(f"/api/v1/conversations/{room}/{termination}",
                                   headers=headers(client, owner))
        assert response.status_code == 204
        pending.result(timeout=5)
    assert stopped.is_set()
    assert native.operations[:2] == ["lease_acquire", "action"]
    with client.app.state.session_factory() as db:
        assert db.get(ToolAction, action_id).state == "uncertain"
        assert db.query(RoomEvent).filter_by(room_id=room, kind="tool_result").one().payload["state"] == "uncertain"
    if termination == "revoke":
        client.__exit__(None, None, None)
        from app.main import create_app
        from fastapi.testclient import TestClient
        with TestClient(create_app(database_url=sqlite_url)) as restarted:
            with restarted.app.state.session_factory() as db:
                assert db.get(ToolAction, action_id).state == "uncertain"
                assert db.query(RoomEvent).filter_by(room_id=room, kind="tool_result").count() == 1


def test_wire_failure_survives_restart_and_cursor_replay(room_client, sqlite_url):
    client, owner, room, _ = _wire_room(room_client)
    client.app.state.connection_transport = httpx.MockTransport(
        lambda request: httpx.Response(503, json={"error": "synthetic upstream failure"}))
    response = client.post(f"/api/v1/conversations/{room}/messages",
                           json={"content": "fail"}, headers=headers(client, owner))
    assert response.status_code == 502
    events = client.get(f"/api/v1/conversations/{room}/events", headers=headers(client, owner)).json()
    failure = next(event for event in events if event["kind"] == "failure")
    assert failure["payload"]["code"] == response.json()["detail"]["code"]
    client.__exit__(None, None, None)
    from app.main import create_app
    from fastapi.testclient import TestClient
    with TestClient(create_app(database_url=sqlite_url)) as restarted:
        replay = restarted.get(f"/api/v1/conversations/{room}/events?after_seq={failure['seq'] - 1}",
                               headers=headers(restarted, owner)).json()
        assert replay[0]["event_id"] == failure["event_id"]
        assert len([event for event in replay if event["kind"] == "failure"]) == 1
        with restarted.app.state.session_factory() as db:
            assert db.query(ToolAction).filter_by(room_id=room).count() == 0
