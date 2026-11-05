"""Synthetic desktop ownership and IPC tests; never touch the real desktop."""

import asyncio
import sqlite3
import base64
import struct
import hashlib
import hmac
import json
import socket
import threading
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.tools.desktop import BackendStartupLock, DesktopLeaseManager
from app.services.tools.desktop import DesktopExecutor, NativeDesktopClient
from types import SimpleNamespace
from test_room_state import room_client, headers, create_room
from test_scheduler import ready_agents, enable_tools
from test_tool_grants import ObservableNativeFixture, _wait_for_action, inspected_approval
from app.models.collaboration import DesktopLease, RoomEvent, ToolAction, ProviderConnection
from app.models.models import Agent
from app.services.providers import ProviderResult, ToolCall
from app.main import create_app
from fastapi.testclient import TestClient


def manager(tmp_path, name):
    path = tmp_path / f"{name}.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE desktop_lease (id INTEGER PRIMARY KEY, room_id INTEGER, agent_id INTEGER, workflow_id TEXT, session_id TEXT, lease_id TEXT, acquired_at TEXT, expires_at TEXT)")
    return DesktopLeaseManager(str(path), str(tmp_path / "host.lock"))


def test_second_backend_cannot_reconcile_live_first_backend(tmp_path):
    path = str(tmp_path / "profile.sqlite")
    with BackendStartupLock(path):
        with pytest.raises(RuntimeError, match="already running"):
            with BackendStartupLock(path):
                pass
    with BackendStartupLock(path):
        pass


@pytest.mark.asyncio
async def test_host_lock_serializes_workflows_across_database_roots(tmp_path):
    first, second = manager(tmp_path, "one"), manager(tmp_path, "two")
    await first.acquire(1, 2, "workflow-a", "session-a", asyncio.Event())
    waiting = asyncio.create_task(second.acquire(3, 4, "workflow-b", "session-b", asyncio.Event()))
    await asyncio.sleep(.1)
    assert not waiting.done()
    assert first.current_owner() == {"room_id": 1, "agent_id": 2, "workflow_id": "workflow-a"}
    await first.release(1, 2, "workflow-a", "session-a")
    await asyncio.wait_for(waiting, 1)
    assert second.current_owner() == {"room_id": 3, "agent_id": 4, "workflow_id": "workflow-b"}
    await second.release(3, 4, "workflow-b", "session-b")


@pytest.mark.asyncio
async def test_wrong_owner_cannot_release_and_wait_cancels(tmp_path):
    first, second = manager(tmp_path, "one"), manager(tmp_path, "two")
    await first.acquire(1, 2, "workflow-a", "session-a", asyncio.Event())
    assert not await first.release(9, 2, "workflow-a", "session-a")
    cancel = asyncio.Event()
    waiting = asyncio.create_task(second.acquire(3, 4, "workflow-b", "session-b", cancel))
    await asyncio.sleep(.05)
    cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert first.current_owner()["room_id"] == 1
    await first.release(1, 2, "workflow-a", "session-a")


@pytest.mark.asyncio
async def test_native_refusal_never_executes_action_or_leaves_lease(tmp_path):
    class RefusingNative:
        async def call(self, body, **start):
            if start.get("preflight"):
                start["preflight"]()
            assert body["operation"] == "lease_acquire"
            return {"state": "failed", "text": "Screen Recording permission denied",
                    "code": "screen_recording_denied"}

    path = manager(tmp_path, "one").database_path
    executor = DesktopExecutor(path, native=RefusingNative(), app_data=tmp_path,
                               lock_path=str(tmp_path / "host.lock"))
    action = SimpleNamespace(room_id=1, agent_id=2, workflow_id="workflow-a", action_id="a1",
                             family="screenshot", arguments={}, payload_digest="digest")
    grant = SimpleNamespace(session_id="session-a", grant_id=5, scope="once")
    result = await executor(action, grant, asyncio.Event())
    assert result.state == "failed" and "Screen Recording" in result.text
    assert result.error_code == "screen_recording_denied"
    assert result.diagnostic["reason"] == "screen_recording_denied"
    assert result.diagnostic["exception_chain"] == []
    assert executor.lease.current_owner() is None


@pytest.mark.asyncio
async def test_synthetic_screenshot_is_room_owned_and_released_after_turn(tmp_path):
    # A synthetic 1x1 PNG; no screen or OS permission APIs are called.
    image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg==")
    class FakeNative(ObservableNativeFixture):
        def __init__(self):
            self.calls = []
        async def call(self, body, **start):
            if start.get("preflight"):
                start["preflight"]()
            self.calls.append(body)
            if body["operation"] == "action":
                return {"state": "completed", "png_base64": base64.b64encode(image).decode(),
                        "width": 1, "height": 1}
            return {"state": "completed"}
    native = FakeNative()
    path = manager(tmp_path, "one").database_path
    executor = DesktopExecutor(path, native=native, app_data=tmp_path, lock_path=str(tmp_path / "host.lock"))
    action = SimpleNamespace(room_id=1, agent_id=2, workflow_id="workflow-a", action_id="a1",
                             family="screenshot", arguments={}, payload_digest="digest")
    grant = SimpleNamespace(session_id="session-a", grant_id=5, scope="once")
    result = await executor(action, grant, asyncio.Event())
    assert result.image_bytes == image and result.image_mime == "image/png"
    assert result.image_width == result.image_height == 1
    assert (tmp_path / result.screenshot_ref).read_bytes() == image
    assert [call["operation"] for call in native.calls] == ["lease_acquire", "action"]
    await executor.release_turn(1, 2)
    assert native.calls[-1]["operation"] == "lease_release"
    assert executor.lease.current_owner() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", [lambda image: image[:24],
                                       lambda image: image[:-4] + b"FAIL"])
async def test_corrupt_native_png_never_becomes_a_room_observation(tmp_path, damage):
    image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg==")
    class FakeNative(ObservableNativeFixture):
        async def call(self, body, **start):
            if start.get("preflight"):
                start["preflight"]()
            if body["operation"] == "action":
                return {"state": "completed", "png_base64": base64.b64encode(damage(image)).decode(),
                        "width": 1, "height": 1}
            return {"state": "completed"}
    path = manager(tmp_path, "corrupt").database_path
    executor = DesktopExecutor(path, native=FakeNative(), app_data=tmp_path,
                               lock_path=str(tmp_path / "host.lock"))
    action = SimpleNamespace(room_id=1, agent_id=2, workflow_id="corrupt", action_id="bad",
                             family="screenshot", arguments={}, payload_digest="digest")
    grant = SimpleNamespace(session_id="session", grant_id=5, scope="once")
    try:
        result = await executor(action, grant, asyncio.Event())
        assert result.state == "uncertain"
        assert result.error_code == "native_screenshot_invalid"
        assert result.diagnostic["reason"] == "native_screenshot_invalid"
        assert result.diagnostic["exception_chain"]
        assert "screen image" in result.text.lower()
        assert not (tmp_path / "rooms/1/screenshots/bad.png").exists()
    finally:
        await executor.release_turn(1, 2)


@pytest.mark.asyncio
async def test_screenshot_native_transport_failure_is_typed_without_raw_error(tmp_path):
    class FakeNative(ObservableNativeFixture):
        async def call(self, body, **start):
            if start.get("preflight"):
                start["preflight"]()
            if body["operation"] == "action":
                raise TimeoutError("private endpoint /tmp/secret.sock")
            return {"state": "completed"}
    executor = DesktopExecutor(manager(tmp_path, "transport").database_path,
                               native=FakeNative(), app_data=tmp_path, lock_path=str(tmp_path / "host.lock"))
    action = SimpleNamespace(room_id=1, agent_id=2, workflow_id="transport", action_id="timeout",
                             family="screenshot", arguments={}, payload_digest="digest")
    grant = SimpleNamespace(session_id="session", grant_id=5, scope="once")
    try:
        result = await executor(action, grant, asyncio.Event())
        assert result.state == "uncertain" and result.error_code == "native_response_unconfirmed"
        assert "private endpoint" not in result.text
    finally:
        await executor.release_turn(1, 2)


@pytest.mark.asyncio
async def test_screenshot_lease_database_failure_has_safe_stage_code(tmp_path, monkeypatch):
    import app.services.tools.desktop as desktop_module

    database = manager(tmp_path, "lease-database").database_path
    original_connect = desktop_module.sqlite3.connect
    calls = 0

    def fail_first_connect(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("private database path")
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(desktop_module.sqlite3, "connect", fail_first_connect)

    class FakeNative(ObservableNativeFixture):
        async def call(self, _body):
            pytest.fail("native action must not be dispatched")

    executor = DesktopExecutor(database, native=FakeNative(), app_data=tmp_path,
                               lock_path=str(tmp_path / "host.lock"))
    action = SimpleNamespace(room_id=1, agent_id=2, workflow_id="lease-database", action_id="lease-error",
                             family="screenshot", arguments={}, payload_digest="digest")
    grant = SimpleNamespace(session_id="session", grant_id=5, scope="once")
    result = await executor(action, grant, asyncio.Event())
    assert result.state == "uncertain" and result.error_code == "native_lease_unconfirmed"
    assert "private database path" not in result.text
    assert executor.lease.current_owner() is None


@pytest.mark.asyncio
async def test_screenshot_artifact_storage_failure_is_typed_without_raw_path(tmp_path):
    image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg==")
    class FakeNative(ObservableNativeFixture):
        async def call(self, body, **start):
            if start.get("preflight"):
                start["preflight"]()
            if body["operation"] == "action":
                return {"state": "completed", "png_base64": base64.b64encode(image).decode(),
                        "width": 1, "height": 1}
            return {"state": "completed"}
    blocked = tmp_path / "private-app-data"
    blocked.write_text("cannot be a directory")
    executor = DesktopExecutor(manager(tmp_path, "storage").database_path,
                               native=FakeNative(), app_data=blocked, lock_path=str(tmp_path / "host.lock"))
    action = SimpleNamespace(room_id=1, agent_id=2, workflow_id="storage", action_id="store",
                             family="screenshot", arguments={}, payload_digest="digest")
    grant = SimpleNamespace(session_id="session", grant_id=5, scope="once")
    try:
        result = await executor(action, grant, asyncio.Event())
        assert result.state == "uncertain" and result.error_code == "screenshot_artifact_unavailable"
        assert result.diagnostic["reason"] == "os_error"
        assert result.diagnostic["exception_chain"]
        assert str(blocked) not in str(result.diagnostic)
        assert str(blocked) not in result.text
    finally:
        await executor.release_turn(1, 2)


@pytest.mark.asyncio
async def test_cancelled_long_native_effect_releases_only_after_quiescence(tmp_path, monkeypatch):
    import app.services.tools.desktop as desktop_module
    monkeypatch.setattr(desktop_module, "RELEASE_WAIT_SECONDS", .03)
    monkeypatch.setattr(desktop_module, "RELEASE_RETRY_SECONDS", .01)
    settled = asyncio.Event()
    started = asyncio.Event()
    class SlowNative:
        context_id = "b" * 32
        effect_lock = False
        release_attempts = 0
        async def call(self, body, **start):
            if start.get("preflight"):
                start["preflight"]()
            if body["operation"] == "lease_acquire":
                self.effect_lock = True
                return {"state": "completed"}
            if body["operation"] == "action":
                effect = asyncio.create_task(settled.wait())
                started.set()
                await asyncio.shield(effect)
                return {"state": "uncertain"}
            if body["operation"] == "lease_release":
                self.release_attempts += 1
                if not settled.is_set():
                    raise TimeoutError("synthetic request deadline expired behind native effect")
                self.effect_lock = False
            return {"state": "completed"}
    path = manager(tmp_path, "long").database_path
    native = SlowNative()
    first = DesktopExecutor(path, native=native, app_data=tmp_path,
                            lock_path=str(tmp_path / "host.lock"))
    other = manager(tmp_path, "other")
    action = SimpleNamespace(room_id=1, agent_id=2, workflow_id="long", action_id="long-action",
                             family="pointer_click", arguments={"x": 1, "y": 1}, payload_digest="digest")
    from dataclasses import replace
    from test_process_review import reviewed_process
    from app.services.grants import GrantRef, now
    action, grant = reviewed_process(action, GrantRef(grant_id=5, scope="once", session_id="session"),
                                     SimpleNamespace(desktop=first))
    grant = replace(grant, approved_at=now())
    running = asyncio.create_task(first(action, grant, asyncio.Event()))
    await started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    try:
        await first.release_turn(1, 2)
        waiting = asyncio.create_task(other.acquire(3, 4, "next", "next-session", asyncio.Event()))
        await asyncio.sleep(.05)
        assert not waiting.done()
        assert native.effect_lock and native.release_attempts >= 1
        assert first.lease.current_owner()["workflow_id"] == "long"
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT workflow_id FROM desktop_lease").fetchone()[0] == "long"
        settled.set()
        await asyncio.wait_for(waiting, 1)
        assert not native.effect_lock and native.release_attempts >= 2
        assert first.lease.current_owner()["workflow_id"] == "next"
        await other.release(3, 4, "next", "next-session")
    finally:
        settled.set()
        await first.close()


@pytest.mark.asyncio
async def test_authenticated_unix_ipc_framing_uses_separate_key(tmp_path):
    path = Path("/tmp") / f"gamja-test-{uuid4().hex}.sock"
    key = bytes(range(32))
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen(1)
    captured = []
    def serve():
        with listener:
            stream, _ = listener.accept()
            with stream:
                size = struct.unpack(">I", stream.recv(4))[0]
                envelope = json.loads(stream.recv(size))
                raw = base64.b64decode(envelope["body"])
                assert hmac.compare_digest(envelope["mac"], hmac.new(key, raw, hashlib.sha256).hexdigest())
                captured.append(json.loads(raw))
                data = json.dumps({"state": "completed", "text": "synthetic"}).encode()
                stream.sendall(struct.pack(">I", len(data)) + data)
    thread = threading.Thread(target=serve)
    thread.start()
    try:
        result = await NativeDesktopClient(str(path), key.hex()).call({"operation": "lease_release",
            "room_id": 1, "agent_id": 2, "workflow_id": "test", "lease_id": "a" * 32})
        assert result["state"] == "completed"
        assert captured[0]["request_id"] and captured[0]["deadline_ms"]
    finally:
        thread.join(timeout=2)
        path.unlink()


def test_scheduler_delivers_room_png_to_verified_vision_continuation(room_client, tmp_path):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "vision")
    agent = ready_agents(client, owner, room, ("Ada",))[0]
    enable_tools(client, agent)
    with client.app.state.session_factory() as db:
        connection = db.get(ProviderConnection, db.get(Agent, agent).connection_id)
        connection.capabilities = {"declared": {"tools": True, "vision": True},
            "verified": {"model_id": "exact", "base_url": connection.base_url, "tools": True, "vision": True}}
        db.commit()
    image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg==")
    class FakeNative(ObservableNativeFixture):
        async def call(self, body, **start):
            if start.get("preflight"):
                start["preflight"]()
            if body["operation"] == "action":
                return {"state": "completed", "png_base64": base64.b64encode(image).decode(),
                        "width": 1, "height": 1}
            return {"state": "completed"}
    desktop = client.app.state.local_executor.desktop
    desktop.native = desktop.lease.native = FakeNative()
    desktop.app_data = tmp_path
    desktop.lease.lock_path = tmp_path / "host.lock"
    observed = []
    async def provider(request):
        if request.observations:
            observed.extend(request.observations)
            return ProviderResult(text="I can inspect the synthetic pixel.")
        return ProviderResult(tool_calls=[ToolCall("screen-1", "screenshot", {}, "workflow-a")],
                              continuation={"call_ids": ["screen-1"]})
    client.app.state.scheduler.provider = provider
    path = f"/api/v1/tools/rooms/{room}"
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"/api/v1/conversations/{room}/messages",
                              json={"content": "inspect"}, headers=headers(client, owner))
        action_id = _wait_for_action(client, room)
        approved = client.post(f"{path}/actions/{action_id}/decision",
                               json=inspected_approval(client, owner, room, action_id, "once"), headers=headers(client, owner))
        assert approved.status_code == 200, approved.text
        assert pending.result(timeout=3).status_code == 201
    assert len(observed) == 1 and observed[0].image_bytes == image
    assert observed[0].screenshot_ref == f"rooms/{room}/screenshots/{action_id}.png"
    assert (tmp_path / observed[0].screenshot_ref).read_bytes() == image
    with client.app.state.session_factory() as db:
        event = db.query(RoomEvent).filter_by(room_id=room, kind="tool_result").one()
        assert event.payload["image_mime"] == "image/png"
        assert event.payload["image_width"] == event.payload["image_height"] == 1
        assert "image_bytes" not in event.payload
        assert db.get(DesktopLease, 1) is None


def test_recovery_records_uncertain_before_clearing_stale_lease(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "recovery")
    agent = ready_agents(client, owner, room, ("Ada",))[0]
    with client.app.state.session_factory() as db:
        db.add(ToolAction(action_id=str(uuid4()), room_id=room, agent_id=agent,
                          family="pointer_click", arguments={"x": 1, "y": 1}, state="started",
                          workflow_id="old-workflow", started_at=datetime.now(timezone.utc)))
        db.add(DesktopLease(id=1, room_id=room, agent_id=agent, workflow_id="old-workflow",
                            session_id="old-session", lease_id="a" * 32,
                            acquired_at=datetime.now(timezone.utc),
                            expires_at=datetime.now(timezone.utc) + timedelta(minutes=1)))
        db.commit()
    asyncio.run(client.app.state.scheduler.start())
    with client.app.state.session_factory() as db:
        assert db.get(DesktopLease, 1) is None
        action = db.query(ToolAction).filter_by(room_id=room).one()
        assert action.state == "uncertain"
        result = db.query(RoomEvent).filter_by(room_id=room, kind="tool_result").one()
        assert result.payload["state"] == "uncertain"


def test_authenticated_lease_read_redacts_other_users_room(room_client, tmp_path):
    client, (owner, other, project, _, _) = room_client
    room = create_room(client, owner, project, "owner")
    agent = ready_agents(client, owner, room, ("Ada",))[0]
    lease = client.app.state.local_executor.desktop.lease
    lease.lock_path = tmp_path / "host.lock"
    assert client.get("/api/v1/tools/desktop/lease", headers=headers(client, owner)).json()["active"] is False
    asyncio.run(lease.acquire(room, agent, "workflow-a", "session-a", asyncio.Event()))
    try:
        mine = client.get("/api/v1/tools/desktop/lease", headers=headers(client, owner)).json()
        other_view = client.get("/api/v1/tools/desktop/lease", headers=headers(client, other)).json()
        assert mine["owner"] == {"room_id": room, "agent_id": agent, "workflow_id": "workflow-a"}
        assert other_view == {"active": True, "owner": None, "another_profile_or_room": True}
    finally:
        asyncio.run(lease.release(room, agent, "workflow-a", "session-a"))


def test_abrupt_backend_death_reconciles_before_next_workflow(room_client, sqlite_url, tmp_path):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "crash")
    agent = ready_agents(client, owner, room, ("Ada",))[0]
    action_id = str(uuid4())
    with client.app.state.session_factory() as db:
        db.add(ToolAction(action_id=action_id, room_id=room, agent_id=agent, family="pointer_click",
                          arguments={"x": 1, "y": 1}, state="started", workflow_id="crashed",
                          started_at=datetime.now(timezone.utc)))
        db.add(DesktopLease(id=1, room_id=room, agent_id=agent, workflow_id="crashed",
                            session_id="dead-session", lease_id="a" * 32,
                            acquired_at=datetime.now(timezone.utc),
                            expires_at=datetime.now(timezone.utc) + timedelta(minutes=1)))
        db.commit()
    client.__exit__(None, None, None)
    database_path = sqlite_url.removeprefix("sqlite:///")
    workflow_lock = str(tmp_path / "host.lock")
    script = ("import fcntl,sys,time; "
              "a=open(sys.argv[1],'a+b'); b=open(sys.argv[2],'a+b'); "
              "fcntl.flock(a,fcntl.LOCK_EX); fcntl.flock(b,fcntl.LOCK_EX); "
              "print('ready',flush=True); time.sleep(30)")
    process = subprocess.Popen([sys.executable, "-c", script, database_path + ".backend.lock",
                                workflow_lock], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(RuntimeError, match="already running"):
            with BackendStartupLock(database_path):
                pass
    finally:
        process.kill()
        process.wait(timeout=2)
        process.stdout.close()
    with TestClient(create_app(database_url=sqlite_url)) as restarted:
        with restarted.app.state.session_factory() as db:
            assert db.get(ToolAction, action_id).state == "uncertain"
            assert db.get(DesktopLease, 1) is None
            assert db.query(RoomEvent).filter_by(room_id=room, kind="tool_result").one().payload["state"] == "uncertain"
        lease = DesktopLeaseManager(database_path, workflow_lock)
        asyncio.run(lease.acquire(room, agent, "new-workflow", "new-session", asyncio.Event()))
        assert lease.current_owner()["workflow_id"] == "new-workflow"
        asyncio.run(lease.release(room, agent, "new-workflow", "new-session"))
        with restarted.app.state.session_factory() as db:
            assert db.get(ToolAction, action_id).state == "uncertain"

@pytest.mark.asyncio
async def test_missing_helper_returns_typed_unavailable_without_lease(tmp_path):
    executor = DesktopExecutor(manager(tmp_path, "missing-helper").database_path,
                               app_data=tmp_path, lock_path=str(tmp_path / "host.lock"))
    action = SimpleNamespace(room_id=1, agent_id=2, workflow_id="workflow", action_id="missing",
                             family="screenshot", arguments={}, payload_digest="a" * 64)
    grant = SimpleNamespace(session_id="session", grant_id=5, scope="once")
    result = await executor(action, grant, asyncio.Event())
    assert result.state == "failed"
    assert result.error_code == "native_capability_unavailable"
    assert result.diagnostic["reason"] == "native_capability_unavailable"
    assert executor.lease.current_owner() is None
    assert not (tmp_path / "host.lock").exists()
    assert not (tmp_path / "rooms").exists()
    await executor.close()

@pytest.mark.asyncio
async def test_invalid_optional_helper_credentials_do_not_disable_file_process_work(tmp_path, monkeypatch):
    from app.services.tools import LocalExecutor
    from app.services.grants import GrantRef
    monkeypatch.setenv("GAMJA_DESKTOP_IPC_ENDPOINT", "/synthetic/missing.sock")
    monkeypatch.setenv("GAMJA_DESKTOP_IPC_KEY", "malformed-fixture")
    executor = LocalExecutor(database_path=manager(tmp_path, "optional").database_path)
    def action(family, **arguments):
        return SimpleNamespace(room_id=1, agent_id=2, workflow_id="workflow", action_id=family,
                               family=family, arguments=arguments, payload_digest="a" * 64)
    grant = GrantRef()
    try:
        screenshot = await executor(action("screenshot"), grant, asyncio.Event())
        assert screenshot.error_code == "native_capability_unavailable"
        path = str((tmp_path / "local.txt").resolve())
        from test_conditional_files import reviewed
        pending, inspected = reviewed("write_file", {"path": path, "content": "healthy"})
        assert (await executor(pending, inspected, asyncio.Event())).state == "completed"
        pending, inspected = reviewed("read_file", {"path": path})
        assert (await executor(pending, inspected, asyncio.Event())).text == "healthy"
        from test_process_review import reviewed_process
        pending, inspected = reviewed_process(action("start_process", executable=sys.executable,
                                      argv=["-c", "print('healthy')"], cwd=str(tmp_path)), grant)
        result = await executor(pending, inspected, asyncio.Event())
        assert result.state == "completed" and result.text == "healthy"
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_disappeared_helper_before_lease_is_typed_unavailable(tmp_path):
    native = NativeDesktopClient(f"/tmp/gamja-helper-missing-{uuid4().hex}.sock", "00" * 32)
    executor = DesktopExecutor(manager(tmp_path, "disappeared").database_path,
                               native=native, app_data=tmp_path, lock_path=str(tmp_path / "host.lock"))
    action = SimpleNamespace(room_id=1, agent_id=2, workflow_id="workflow", action_id="missing",
                             family="screenshot", arguments={}, payload_digest="a" * 64)
    grant = SimpleNamespace(session_id="session", grant_id=5, scope="once")
    result = await executor(action, grant, asyncio.Event())
    assert result.state == "failed" and result.error_code == "native_capability_unavailable"
    assert executor.lease.current_owner() is None
    await executor.close()
