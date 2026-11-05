"""Consent tests use disposable SQLite and synthetic executors only."""

import asyncio
import json
import sqlite3
import sys
import threading
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep
from uuid import uuid4

import pytest

from test_room_state import room_client, headers, create_room
from test_scheduler import ready_agents, enable_tools
from app.models.collaboration import RoomEvent, ToolAction
from app.services.providers import ProviderResult, ToolCall
from app.services.scheduler import ExecutorBudget
from app.services.tool_broker import ToolBroker


class ObservableNativeFixture:
    """Synthetic adapters have per-instance identity; never real IPC proof."""
    def __new__(cls, *args, **kwargs):
        value = super().__new__(cls)
        value.context_id = uuid4().hex
        return value


def _wait_for_action(client, room, timeout=3):
    until = monotonic() + timeout
    while monotonic() < until:
        with client.app.state.session_factory() as db:
            action = db.query(ToolAction).filter_by(room_id=room).first()
            if action and action.state == "waiting":
                return action.action_id
        sleep(.02)
    raise AssertionError("Action did not request approval")


def inspected_approval(client, owner, room, action_id, scope="once"):
    """Test-only explicit inspection of a synthetic pending action."""
    view = client.get(f"/api/v1/tools/rooms/{room}/actions/{action_id}/preview", headers=headers(client, owner))
    if view.status_code != 200:
        # Fail the original assertion, but first unblock this owned synthetic
        # request so ThreadPool shutdown cannot hide the test failure forever.
        client.post(f"/api/v1/conversations/{room}/cancel", headers=headers(client, owner))
    assert view.status_code == 200, view.text
    return {"decision": "approve", "scope": scope, "review_id": view.json()["review_id"],
            "acknowledge_unverified_context": view.json()["approval_policy"]["unverified_context_required"]}


def retain_test_review(db, action, prepared, broker, owner):
    from app.models.models import Agent
    from app.models.collaboration import ProviderConnection
    from app.services.action_review import ProposerBinding, observe_authority
    from app.services.review_snapshot import capture_review, stage_initial_review
    agent = db.get(Agent, action.agent_id); row = db.get(ProviderConnection, agent.connection_id)
    proposer = ProposerBinding(owner_id=owner, agent_id=agent.id, room_id=action.room_id,
        connection_id=row.id, model_id=agent.model_id, config_revision=row.readiness_revision,
        auth_grant_revision=row.auth_grant_revision, provider_id=row.provider_id, protocol_id=row.protocol_id)
    authority = observe_authority(db, room_id=action.room_id, agent_id=agent.id,
        session_id=broker.session_id, proposer=proposer)
    stage_initial_review(action, capture_review(action, prepared, authority, broker.executor))
    db.commit()
    broker.retain(action.action_id, prepared, proposer=proposer)


def _setup(client, owner, project, family="read_file", arguments=None):
    room = create_room(client, owner, project, "consent")
    agent = ready_agents(client, owner, room, ("Ada",))[0]
    enable_tools(client, agent)
    calls = []
    arguments = arguments or {"path": "synthetic.txt"}

    async def executor(action, grant, cancel):
        calls.append(action)
        return "synthetic result"

    executor.desktop = SimpleNamespace(native=ObservableNativeFixture())
    client.app.state.scheduler.broker.executor = executor

    async def provider(request):
        if not request.observations:
            workflow = "synthetic-workflow" if family == "screenshot" or family.startswith(("pointer_", "keyboard_")) else None
            return ProviderResult(tool_calls=[ToolCall("call-1", family, arguments, workflow)],
                                  continuation={"call_ids": ["call-1"]})
        return ProviderResult(text="finished")

    client.app.state.scheduler.provider = provider
    return room, agent, calls


def test_unexpected_tool_failure_emits_one_runtime_event(room_client, monkeypatch):
    client, (owner, _, project, _, _) = room_client
    room, _, _ = _setup(client, owner, project)
    emitted = []
    monkeypatch.setattr("app.services.tool_broker.emit_detailed_diagnostic",
                        lambda detail: emitted.append(detail))

    async def executor(_action, _grant, _cancel):
        raise RuntimeError("secret /Users/alice/private.txt")

    client.app.state.scheduler.broker.executor = executor
    assert client.put("/api/v1/tools/broad-mode", json={"enabled": True, "expires_minutes": 10},
                      headers=headers(client, owner)).status_code == 200
    response = client.post(f"/api/v1/conversations/{room}/messages",
                           json={"content": "read"}, headers=headers(client, owner))
    assert response.status_code == 502
    assert "secret" not in response.text
    assert len(emitted) == 1
    with client.app.state.session_factory() as db:
        action = db.query(ToolAction).filter_by(room_id=room).one()
        result = db.query(RoomEvent).filter_by(room_id=room, kind="tool_result").one().payload
    assert action.state == "uncertain"
    assert result["diagnostic"] == emitted[0]
    assert result["diagnostic"]["reason"] == "unexpected_error"
    assert result["diagnostic"]["exception_chain"][0] == {"module": "builtins", "class": "RuntimeError"}
    assert "secret" not in str(result)


def test_http_scheduler_denial_prevents_execution_and_activity_is_redacted(room_client, sqlite_url):
    client, (owner, other, project, _, _) = room_client
    room, agent, calls = _setup(client, owner, project, "keyboard_text", {"text": "test-secret-value"})
    path = f"/api/v1/tools/rooms/{room}"
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"/api/v1/conversations/{room}/messages",
                              json={"content": "type"}, headers=headers(client, owner))
        action_id = _wait_for_action(client, room)
        assert client.get(f"{path}/activity", headers=headers(client, other)).status_code == 404
        assert client.post(f"{path}/actions/{action_id}/decision", json={"decision": "deny"},
                           headers=headers(client, other)).status_code == 404
        activity = client.get(f"{path}/activity", headers=headers(client, owner)).json()
        assert activity[0]["action_id"] == action_id
        assert activity[0]["expected_effect"] == "Type 17 characters"
        assert "test-secret-value" not in json.dumps(activity)
        assert client.post(f"{path}/actions/{action_id}/decision", json={"decision": "deny"},
                           headers=headers(client, owner)).status_code == 200
        assert pending.result(timeout=3).status_code == 201
    with client.app.state.session_factory() as db:
        assert activity[0]["trigger_event_id"] == db.get(ToolAction, action_id).trigger_event_id
    assert not calls
    with sqlite3.connect(sqlite_url.removeprefix("sqlite:///")) as db:
        assert "test-secret-value" not in "\n".join(db.iterdump())


def test_once_grant_is_bound_and_consumed_before_executor(room_client):
    client, (owner, _, project, _, _) = room_client
    room, agent, calls = _setup(client, owner, project)
    path = f"/api/v1/tools/rooms/{room}"
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"/api/v1/conversations/{room}/messages",
                              json={"content": "read"}, headers=headers(client, owner))
        action_id = _wait_for_action(client, room)
        preview_path = f"{path}/actions/{action_id}/preview"
        preflight = client.options(preview_path, headers={
            "Origin": "tauri://localhost", "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,x-gamja-session"})
        assert preflight.status_code == 200, preflight.text
        preview = client.get(preview_path, headers={**headers(client, owner), "Origin": "tauri://localhost"})
        assert preview.status_code == 200 and preview.headers["cache-control"] == "no-store"
        assert preview.json()["action_id"] == action_id
        assert preview.json()["family"] == "read_file"
        decision = client.post(f"{path}/actions/{action_id}/decision", json=inspected_approval(client, owner, room, action_id, "once"),
                               headers=headers(client, owner))
        assert decision.status_code == 200, decision.text
        assert pending.result(timeout=3).status_code == 201
    assert len(calls) == 1 and calls[0].action_id == action_id
    with client.app.state.session_factory() as db:
        action = db.get(ToolAction, action_id)
        assert action.state == "completed" and action.grant_id is not None
        assert len(db.query(RoomEvent).filter_by(room_id=room, kind="tool_result").all()) == 1


@pytest.mark.asyncio
async def test_broker_rejects_scope_crossing_expiry_and_atomic_once(room_client):
    client, (owner, _, project, _, _) = room_client
    room, agent, calls = _setup(client, owner, project)
    broker = client.app.state.scheduler.broker
    # Direct broker calls exercise authorization independently of provider behavior.
    from app.models.collaboration import RoomGrant
    from datetime import datetime, timedelta, timezone
    from app.services.grants import decide
    first_id = str(uuid4())
    with client.app.state.session_factory() as db:
        prepared = broker.prepare("read_file", {"path":"synthetic.txt"})
        basis = ToolAction(action_id=first_id, room_id=room, agent_id=agent, family=prepared.family,
            arguments=prepared.public_arguments, state="waiting", target=prepared.target,
            scope_target=prepared.scope_target, payload_digest=prepared.payload_digest,
            expected_effect=prepared.expected_effect, session_id=broker.session_id)
        db.add(basis); db.commit(); retain_test_review(db, basis, prepared, broker, owner)
        grant_id = decide(db, first_id, room, owner, "approve", "once", broker.session_id,
                         review_id=basis.review_id, broker=broker)["grant_id"]
    async def try_action(action_id, family="read_file", target="synthetic.txt", room_id=room):
        descriptor = broker.prepare(family,{"path":target})
        with client.app.state.session_factory() as db:
            if action_id == "once-a": action = db.get(ToolAction,first_id)
            else:
                action = ToolAction(action_id=str(uuid4()),room_id=room_id,agent_id=agent,family=family,
                    arguments=descriptor.public_arguments,state="waiting",target=descriptor.target,
                    scope_target=descriptor.scope_target,payload_digest=descriptor.payload_digest,
                    expected_effect=descriptor.expected_effect,session_id=broker.session_id)
                db.add(action);db.commit();retain_test_review(db,action,descriptor,broker,owner)
            db.expunge(action)
        return await broker.execute(action,grant_id,asyncio.Event(),ExecutorBudget(2))
    first, second = await asyncio.gather(try_action("once-a"), try_action("once-b"))
    assert sorted([first.state, second.state]) == ["completed", "denied"]
    assert len(calls) == 1
    assert (await try_action("wrong-family", family="delete_file")).state == "denied"
    assert (await try_action("wrong-target", target="elsewhere.txt")).state == "denied"


def test_broad_mode_requires_separate_owner_enable_and_revokes(room_client):
    client, (owner, other, project, _, _) = room_client
    room, agent, calls = _setup(client, owner, project)
    mode = "/api/v1/tools/broad-mode"
    assert client.get(mode, headers=headers(client, owner)).json()["enabled"] is False
    assert client.put(mode, json={"enabled": True}, headers=headers(client, other)).status_code == 422
    assert client.put(mode, json={"enabled": True, "expires_minutes": 10}, headers=headers(client, other)).status_code == 200
    assert client.get(mode, headers=headers(client, owner)).json()["enabled"] is False
    assert client.put(mode, json={"enabled": True, "expires_minutes": 10}, headers=headers(client, owner)).status_code == 200
    response = client.post(f"/api/v1/conversations/{room}/messages", json={"content": "read"},
                           headers=headers(client, owner))
    assert response.status_code == 201 and len(calls) == 1
    assert client.put(mode, json={"enabled": False}, headers=headers(client, owner)).status_code == 200
    assert client.get(mode, headers=headers(client, owner)).json()["enabled"] is False


def test_always_approve_requires_explicit_opt_in_and_survives_restart_only_for_owner(room_client):
    client, (owner, other, project, _, _) = room_client
    room, _, calls = _setup(client, owner, project)
    mode = "/api/v1/tools/broad-mode"
    assert client.get(mode, headers=headers(client, owner)).json()["enabled"] is False
    assert client.put(mode, json={"enabled": True, "always": True, "expires_minutes": 10},
                      headers=headers(client, owner)).status_code == 422
    assert client.put(mode, json={"enabled": True}, headers=headers(client, owner)).status_code == 422
    enabled = client.put(mode, json={"enabled": True, "always": True}, headers=headers(client, owner))
    assert enabled.status_code == 200
    assert enabled.json() == {"enabled": True, "scope": "broad", "expires_at": None}
    assert client.get(mode, headers=headers(client, other)).json()["enabled"] is False
    from app.services.grants import broad_state
    new_launch = ToolBroker(client.app.state.session_factory)
    with client.app.state.session_factory() as db:
        assert broad_state(db, owner, new_launch.session_id)["enabled"] is True
        assert broad_state(db, other, new_launch.session_id)["enabled"] is False
    response = client.post(f"/api/v1/conversations/{room}/messages", json={"content": "read"},
                           headers=headers(client, owner))
    assert response.status_code == 201 and len(calls) == 1
    assert client.put(mode, json={"enabled": False}, headers=headers(client, owner)).json()["enabled"] is False
    with client.app.state.session_factory() as db:
        assert broad_state(db, owner, new_launch.session_id)["enabled"] is False


def test_cancel_while_waiting_never_starts_executor(room_client):
    client, (owner, _, project, _, _) = room_client
    room, _, calls = _setup(client, owner, project)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"/api/v1/conversations/{room}/messages",
                              json={"content": "read"}, headers=headers(client, owner))
        action_id = _wait_for_action(client, room)
        assert client.post(f"/api/v1/conversations/{room}/cancel", headers=headers(client, owner)).status_code == 204
        pending.result(timeout=3)
    with client.app.state.session_factory() as db:
        assert db.get(ToolAction, action_id).state == "cancelled"
        assert [event.payload["state"] for event in db.query(RoomEvent).filter_by(room_id=room, kind="tool_result")] == ["cancelled"]
    assert calls == []


@pytest.mark.parametrize("grant_scope", ["room_session", "broad", "always"])
def test_revoke_during_execution_marks_uncertain_and_stops_executor(room_client, grant_scope):
    client, (owner, _, project, _, _) = room_client
    room, _, _ = _setup(client, owner, project)
    entered, stopped = threading.Event(), threading.Event()

    async def executor(action, grant, cancel):
        entered.set()
        try:
            await asyncio.sleep(30)
        finally:
            stopped.set()

    client.app.state.scheduler.broker.executor = executor
    path = f"/api/v1/tools/rooms/{room}"
    if grant_scope in ("broad", "always"):
        option = {"always": True} if grant_scope == "always" else {"expires_minutes": 10}
        assert client.put("/api/v1/tools/broad-mode", json={"enabled": True, **option},
                          headers=headers(client, owner)).status_code == 200
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"/api/v1/conversations/{room}/messages",
                              json={"content": "read"}, headers=headers(client, owner))
        if grant_scope == "room_session":
            action_id = _wait_for_action(client, room)
            approved = client.post(f"{path}/actions/{action_id}/decision",
                                   json=inspected_approval(client, owner, room, action_id, "room_session"),
                                   headers=headers(client, owner)).json()
        assert entered.wait(3)
        if grant_scope in ("broad", "always"):
            with client.app.state.session_factory() as db:
                action_id = db.query(ToolAction).filter_by(room_id=room).one().action_id
            assert client.put("/api/v1/tools/broad-mode", json={"enabled": False},
                              headers=headers(client, owner)).status_code == 200
        else:
            assert client.post(f"{path}/grants/{approved['grant_id']}/revoke",
                               headers=headers(client, owner)).status_code == 204
        pending.result(timeout=3)
    assert stopped.is_set()
    with client.app.state.session_factory() as db:
        assert db.get(ToolAction, action_id).state == "uncertain"
        events = db.query(RoomEvent).filter_by(room_id=room, kind="tool_result").all()
        assert len(events) == 1 and events[0].payload["state"] == "uncertain"


def test_revoke_cancels_queued_grant_action(room_client):
    client, (owner, _, project, _, _) = room_client
    room, agent, _ = _setup(client, owner, project)
    broker = client.app.state.scheduler.broker
    from app.models.collaboration import RoomGrant
    from datetime import datetime, timedelta, timezone
    prepared = broker.prepare("read_file", {"path": "synthetic.txt"})
    with client.app.state.session_factory() as db:
        grant = RoomGrant(room_id=room, agent_id=agent, scope={"kind": "room_session", "session_id": broker.session_id,
                          "families": ["read_file"], "targets": [prepared.scope_target]}, decision="approved",
                          expires_at=datetime.now(timezone.utc) + timedelta(minutes=1))
        db.add(grant)
        db.flush()
        db.add(ToolAction(action_id="queued-revoke", room_id=room, agent_id=agent, family="read_file",
                          arguments=prepared.public_arguments, target=prepared.target,
                          scope_target=prepared.scope_target, payload_digest=prepared.payload_digest,
                          expected_effect=prepared.expected_effect, state="waiting", session_id=broker.session_id,
                          grant_id=grant.id))
        db.commit()
        grant_id = grant.id
    response = client.post(f"/api/v1/tools/rooms/{room}/grants/{grant_id}/revoke", headers=headers(client, owner))
    assert response.status_code == 204
    with client.app.state.session_factory() as db:
        assert db.get(ToolAction, "queued-revoke").state == "cancelled"


def test_broad_mode_does_not_survive_a_new_launch(room_client):
    client, (owner, _, _, _, _) = room_client
    mode = "/api/v1/tools/broad-mode"
    assert client.put(mode, json={"enabled": True, "expires_minutes": 10},
                      headers=headers(client, owner)).json()["enabled"] is True
    other_launch = ToolBroker(client.app.state.session_factory)
    from app.services.grants import broad_state
    with client.app.state.session_factory() as db:
        assert broad_state(db, owner, other_launch.session_id)["enabled"] is False


@pytest.mark.asyncio
async def test_room_session_grant_checks_room_agent_session_family_target_and_expiry(room_client):
    client, (owner, _, project, _, foreign_agent) = room_client
    room, agent, calls = _setup(client, owner, project)
    other_room = create_room(client, owner, project, "other")
    broker = client.app.state.scheduler.broker
    from app.models.collaboration import RoomGrant
    from datetime import timedelta
    from app.services.grants import now
    prepared = broker.prepare("read_file", {"path": "synthetic.txt"})
    from app.services.grants import decide
    with client.app.state.session_factory() as db:
        basis = ToolAction(action_id=str(uuid4()), room_id=room, agent_id=agent,
            family=prepared.family, arguments=prepared.public_arguments, target=prepared.target,
            scope_target=prepared.scope_target, payload_digest=prepared.payload_digest,
            expected_effect=prepared.expected_effect, state="waiting", session_id=broker.session_id)
        db.add(basis); db.commit()
        retain_test_review(db, basis, prepared, broker, owner)
        grant_id = decide(db, basis.action_id, room, owner, "approve", "room_session", broker.session_id,
                         review_id=basis.review_id, broker=broker)["grant_id"]

    async def execute(action_id, room_id=room, agent_id=agent, session_id=None,
                      family="read_file", path="synthetic.txt"):
        descriptor = broker.prepare(family, {"path": path})
        with client.app.state.session_factory() as db:
            action = ToolAction(action_id=str(uuid4()), room_id=room_id, agent_id=agent_id,
                                family=family, arguments=descriptor.public_arguments, target=descriptor.target,
                                scope_target=descriptor.scope_target, payload_digest=descriptor.payload_digest,
                                expected_effect=descriptor.expected_effect, state="pending",
                                session_id=session_id or broker.session_id)
            db.add(action)
            db.commit()
            if room_id == room and agent_id == agent and session_id is None:
                retain_test_review(db, action, descriptor, broker, owner)
            db.refresh(action)
            db.expunge(action)
        return await broker.execute(action, grant_id, asyncio.Event(), ExecutorBudget(2))

    assert (await execute("same-scope")).state == "completed"
    assert len(calls) == 1
    for action_id, kwargs in (("other-room", {"room_id": other_room}),
                              ("other-agent", {"agent_id": foreign_agent}),
                              ("other-session", {"session_id": "wrong-session"}),
                              ("other-family", {"family": "delete_file"}),
                              ("other-target", {"path": "elsewhere.txt"})):
        assert (await execute(action_id, **kwargs)).state == "denied"
    with client.app.state.session_factory() as db:
        db.get(RoomGrant, grant_id).expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert (await execute("expired")).state == "denied"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_approval_wait_does_not_consume_executor_budget(room_client):
    client, (owner, _, project, _, _) = room_client
    room, agent, calls = _setup(client, owner, project)
    broker = client.app.state.scheduler.broker
    from app.services.grants import decide
    descriptor = broker.prepare("read_file", {"path": "synthetic.txt"})
    with client.app.state.session_factory() as db:
        action = ToolAction(action_id=str(uuid4()), room_id=room, agent_id=agent,
                            family="read_file", arguments=descriptor.public_arguments,
                            scope_target=descriptor.scope_target, payload_digest=descriptor.payload_digest,
                            target=descriptor.target, expected_effect=descriptor.expected_effect,
                            state="pending", session_id=broker.session_id)
        db.add(action)
        db.commit()
        retain_test_review(db, action, descriptor, broker, owner)
        db.refresh(action)
        db.expunge(action)
    budget = ExecutorBudget(1)
    task = asyncio.create_task(broker.execute(action, None, asyncio.Event(), budget))
    await asyncio.sleep(.2)
    assert budget.consumed_seconds == 0
    with client.app.state.session_factory() as db:
        decide(db, action.action_id, room, owner, "approve", "once", broker.session_id, review_id=action.review_id, broker=broker)
    assert (await task).state == "completed"
    assert len(calls) == 1 and budget.consumed_seconds < .1


def test_registry_derives_effect_target_and_does_not_trust_model_labels(room_client):
    client, (_, _, _, _, _) = room_client
    broker = client.app.state.scheduler.broker
    one = broker.prepare("start_process", {"executable": sys.executable, "argv": ["--one"], "cwd": "."})
    two = broker.prepare("start_process", {"executable": sys.executable, "argv": ["--two"], "cwd": "."})
    assert one.target != two.target
    assert one.scope_target == two.scope_target
    assert "local user authority" in one.expected_effect
    assert one.raw_arguments["executable"].startswith("/")
    assert "--one" not in json.dumps(one.public_arguments) + one.target
    with pytest.raises(ValueError):
        broker.prepare("delete_file", {"path": "synthetic.txt", "expected_effect": "harmless read"})
    with pytest.raises(ValueError):
        broker.prepare("browser_navigate", {"url": "https://name:secret@example.test/"})
    url = broker.prepare("browser_navigate", {"url": "https://example.test/page?token=secret"})
    assert "secret" not in url.target + json.dumps(url.public_arguments)


def test_revision_eight_redacts_legacy_tool_payloads(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from app.core import migrate as migration
    from test_migrations import legacy_db
    path = tmp_path / "legacy-consent.sqlite"
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    legacy_db(path)
    engine = create_engine(f"sqlite:///{path}")
    with monkeypatch.context() as scoped:
        scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:7])
        migration.migrate_database(engine)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO tool_actions(action_id,room_id,agent_id,family,arguments,state,observation) "
                   "VALUES(?,?,?,?,?,?,?)", ("old-action", 51, 41, "keyboard_text",
                   json.dumps({"text": "legacy-secret"}), "completed", json.dumps({"text": "build complete"})))
        seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM room_events WHERE room_id=51").fetchone()[0]
        db.execute("INSERT INTO room_events(event_id,room_id,seq,actor_type,actor_name_snapshot,kind,payload,created_at) "
                   "VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)", ("old-tool-event", 51, seq, "tool", "Tool", "tool_result",
                   json.dumps({"action_id": "old-action", "text": "api_key=legacy-secret", "state": "completed"})))
        db.execute("INSERT INTO room_events(event_id,room_id,seq,actor_type,actor_name_snapshot,kind,payload,created_at) "
                   "VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)", ("old-safe-event", 51, seq + 1, "tool", "Tool", "tool_result",
                   json.dumps({"action_id": "old-safe-action", "text": "build complete", "state": "completed"})))
    migration.migrate_database(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        dump = "\n".join(db.iterdump())
        assert "legacy-secret" not in dump
        assert db.execute("SELECT state FROM tool_actions WHERE action_id='old-action'").fetchone() == ("completed",)
        assert json.loads(db.execute("SELECT payload FROM room_events WHERE event_id='old-tool-event'").fetchone()[0])["text"] == "api_key=[redacted]"
        assert json.loads(db.execute("SELECT payload FROM room_events WHERE event_id='old-safe-event'").fetchone()[0])["text"] == "build complete"
        assert json.loads(db.execute("SELECT observation FROM tool_actions WHERE action_id='old-action'").fetchone()[0])["text"] == "build complete"


@pytest.mark.asyncio
async def test_concurrent_entry_of_same_action_cannot_run_twice(room_client):
    client, (owner, _, project, _, _) = room_client
    room, agent, _ = _setup(client, owner, project)
    broker = client.app.state.scheduler.broker
    from app.models.collaboration import RoomGrant
    from datetime import timedelta
    from app.services.grants import now
    entered = asyncio.Event()
    count = 0

    async def executor(action, grant, cancel):
        nonlocal count
        count += 1
        entered.set()
        await asyncio.sleep(.1)
        return "done"

    broker.executor = executor
    descriptor = broker.prepare("read_file", {"path": "synthetic.txt"})
    from app.services.grants import decide
    with client.app.state.session_factory() as db:
        action = ToolAction(action_id=str(uuid4()), room_id=room, agent_id=agent, family="read_file",
                            arguments=descriptor.public_arguments, target=descriptor.target,
                            scope_target=descriptor.scope_target, payload_digest=descriptor.payload_digest,
                            expected_effect=descriptor.expected_effect, state="waiting", session_id=broker.session_id)
        db.add(action); db.commit()
        retain_test_review(db, action, descriptor, broker, owner)
        grant_id = decide(db, action.action_id, room, owner, "approve", "room_session", broker.session_id,
                         review_id=action.review_id, broker=broker)["grant_id"]
        db.refresh(action); db.expunge(action)
    first = asyncio.create_task(broker.execute(action, grant_id, asyncio.Event(), ExecutorBudget(2)))
    await entered.wait()
    second = await broker.execute(action, grant_id, asyncio.Event(), ExecutorBudget(2))
    assert sorted([second.state, (await first).state]) == ["completed", "denied"]
    assert (await broker.execute(action, grant_id, asyncio.Event(), ExecutorBudget(2))).state == "denied"
    assert count == 1


def test_action_digest_separates_write_payload_from_reusable_file_scope(room_client):
    client, (_, _, _, _, _) = room_client
    broker = client.app.state.scheduler.broker
    first = broker.prepare("write_file", {"path": "synthetic.txt", "content": "A"})
    second = broker.prepare("write_file", {"path": "synthetic.txt", "content": "B"})
    assert first.scope_target == second.scope_target
    assert first.payload_digest != second.payload_digest
    assert first.target != second.target


def test_broad_mode_cannot_replay_completed_action(room_client):
    client, (owner, _, project, _, _) = room_client
    room, _, calls = _setup(client, owner, project)
    assert client.put("/api/v1/tools/broad-mode", json={"enabled": True, "expires_minutes": 10},
                      headers=headers(client, owner)).status_code == 200
    assert client.post(f"/api/v1/conversations/{room}/messages", json={"content": "read"},
                       headers=headers(client, owner)).status_code == 201
    assert len(calls) == 1
    from app.services.scheduler import ExecutorBudget
    replay = asyncio.run(client.app.state.scheduler.broker.execute(
        calls[0], None, asyncio.Event(), ExecutorBudget(2)))
    assert replay.state == "denied" and len(calls) == 1


def test_safe_result_text_persists_and_credential_fields_are_redacted(room_client):
    client, (owner, _, project, _, _) = room_client
    room, _, _ = _setup(client, owner, project)
    from app.services.tool_broker import ActionResult

    async def executor(action, grant, cancel):
        return ActionResult("completed", '{"api_key":"synthetic-secret","status":"build complete"}')

    client.app.state.scheduler.broker.executor = executor
    assert client.put("/api/v1/tools/broad-mode", json={"enabled": True, "expires_minutes": 10},
                      headers=headers(client, owner)).status_code == 200
    assert client.post(f"/api/v1/conversations/{room}/messages", json={"content": "read"},
                       headers=headers(client, owner)).status_code == 201
    with client.app.state.session_factory() as db:
        event = db.query(RoomEvent).filter_by(room_id=room, kind="tool_result").one()
        assert "build complete" in event.payload["text"]
        assert "synthetic-secret" not in event.payload["text"]


def test_owner_preview_shows_exact_pending_command_without_persisting_text(room_client, sqlite_url):
    client, (owner, other, project, _, _) = room_client
    room, _, calls = _setup(client, owner, project, "write_file",
                            {"path": "synthetic.txt", "content": "preview-only-value"})
    path = f"/api/v1/tools/rooms/{room}"
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"/api/v1/conversations/{room}/messages",
                              json={"content": "write"}, headers=headers(client, owner))
        action_id = _wait_for_action(client, room)
        assert client.get(f"{path}/actions/{action_id}/preview", headers=headers(client, other)).status_code == 404
        preview = client.get(f"{path}/actions/{action_id}/preview", headers=headers(client, owner))
        assert preview.status_code == 200 and preview.headers["cache-control"] == "no-store"
        assert preview.json()["arguments"]["content"] == "preview-only-value"
        assert preview.json()["scope_target"] == preview.json()["target"].split(" [payload")[0]
        assert "preview-only-value" not in json.dumps(client.get(f"{path}/activity", headers=headers(client, owner)).json())
        assert client.post(f"{path}/actions/{action_id}/decision", json={"decision": "deny"},
                           headers=headers(client, owner)).status_code == 200
        pending.result(timeout=3)
    assert calls == []
    with sqlite3.connect(sqlite_url.removeprefix("sqlite:///")) as db:
        assert "preview-only-value" not in "\n".join(db.iterdump())


@pytest.mark.asyncio
async def test_once_decision_rejects_same_length_payload_substitution(room_client):
    client, (owner, _, project, _, _) = room_client
    room, agent, calls = _setup(client, owner, project)
    broker = client.app.state.scheduler.broker
    from app.services.grants import decide
    first = broker.prepare("write_file", {"path": "synthetic.txt", "content": "A"})
    with client.app.state.session_factory() as db:
        action = ToolAction(action_id=str(uuid4()), room_id=room, agent_id=agent, family="write_file",
                            arguments=first.public_arguments, target=first.target, scope_target=first.scope_target,
                            payload_digest=first.payload_digest, expected_effect=first.expected_effect,
                            state="waiting", session_id=broker.session_id)
        db.add(action)
        db.commit()
        retain_test_review(db, action, first, broker, owner)
        decide(db, action.action_id, room, owner, "approve", "once", broker.session_id, review_id=action.review_id, broker=broker)
        db.refresh(action)
        db.expunge(action)
    action._raw_arguments = {"path": "synthetic.txt", "content": "B"}
    result = await broker.execute(action, None, asyncio.Event(), ExecutorBudget(2))
    assert result.state == "denied" and calls == []
    with client.app.state.session_factory() as db:
        assert db.get(ToolAction, action.action_id).state == "denied"


@pytest.mark.asyncio
async def test_room_session_pointer_scope_allows_distinct_steps_only_in_workflow(room_client):
    client, (owner, _, project, _, _) = room_client
    room, agent, calls = _setup(client, owner, project)
    broker = client.app.state.scheduler.broker
    from app.services.grants import decide
    workflow = "test-desktop-workflow"
    first = broker.prepare("pointer_move", {"x": 10, "y": 20}, workflow)
    second = broker.prepare("pointer_move", {"x": 30, "y": 40}, workflow)
    assert first.scope_target == second.scope_target == "desktop:pointer"
    assert first.payload_digest != second.payload_digest

    def save(action_id, descriptor, workflow_id=workflow):
        with client.app.state.session_factory() as db:
            action = ToolAction(action_id=str(uuid4()), room_id=room, agent_id=agent, family="pointer_move",
                                arguments=descriptor.public_arguments, target=descriptor.target,
                                scope_target=descriptor.scope_target, payload_digest=descriptor.payload_digest,
                                expected_effect=descriptor.expected_effect, workflow_id=workflow_id,
                                state="waiting", session_id=broker.session_id)
            db.add(action)
            db.commit()
            retain_test_review(db, action, descriptor, broker, owner)
            db.refresh(action)
            db.expunge(action)
        return action

    a = save("pointer-step-a", first)
    with client.app.state.session_factory() as db:
        approved = decide(db, a.action_id, room, owner, "approve", "room_session", broker.session_id, review_id=a.review_id, broker=broker)
    assert (await broker.execute(a, approved["grant_id"], asyncio.Event(), ExecutorBudget(2))).state == "completed"
    b = save("pointer-step-b", second)
    assert (await broker.execute(b, approved["grant_id"], asyncio.Event(), ExecutorBudget(2))).state == "completed"
    other = broker.prepare("pointer_move", {"x": 50, "y": 60}, "other-workflow")
    c = save("pointer-other-workflow", other, "other-workflow")
    assert (await broker.execute(c, approved["grant_id"], asyncio.Event(), ExecutorBudget(2))).state == "denied"
    assert len(calls) == 2


def test_http_scheduler_room_session_runs_distinct_workflow_steps(room_client):
    client, (owner, _, project, _, _) = room_client
    room, _, calls = _setup(client, owner, project)

    async def provider(request):
        if not request.observations:
            return ProviderResult(tool_calls=[ToolCall("first", "pointer_move", {"x": 10, "y": 20}, "flow")],
                                  continuation={"call_ids": ["first"]})
        if request.observations[0].call_id == "first":
            return ProviderResult(tool_calls=[ToolCall("second", "pointer_move", {"x": 30, "y": 40}, "flow")],
                                  continuation={"call_ids": ["second"]})
        return ProviderResult(text="finished")

    client.app.state.scheduler.provider = provider
    path = f"/api/v1/tools/rooms/{room}"
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"/api/v1/conversations/{room}/messages",
                              json={"content": "perform flow"}, headers=headers(client, owner))
        action_id = _wait_for_action(client, room)
        decision = client.post(f"{path}/actions/{action_id}/decision",
                               json=inspected_approval(client, owner, room, action_id, "room_session"),
                               headers=headers(client, owner))
        assert decision.status_code == 200, decision.text
        assert pending.result(timeout=3).status_code == 201
    assert len(calls) == 2
    assert [(call._raw_arguments["x"], call._raw_arguments["y"]) for call in calls] == [(10, 20), (30, 40)]
    assert client.get("/api/v1/tools/broad-mode", headers=headers(client, owner)).json()["enabled"] is False
    with client.app.state.session_factory() as db:
        assert db.query(RoomEvent).filter_by(room_id=room, kind="tool_result").count() == 2


@pytest.mark.asyncio
async def test_useful_result_reaches_peer_context_and_restart(room_client, sqlite_url):
    client, (owner, _, project, _, _) = room_client
    room, agent, calls = _setup(client, owner, project)
    peer = ready_agents(client, owner, room, ("Ben",))[0]
    async def provider(request):
        if request.agent_id == agent and not request.observations and request.watermark_seq == 1:
            return ProviderResult(tool_calls=[ToolCall("call-1", "read_file", {"path": "synthetic.txt"})],
                                  continuation={"call_ids": ["call-1"]})
        return ProviderResult(text="finished")
    client.app.state.scheduler.provider = provider
    path = f"/api/v1/tools/rooms/{room}"
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"/api/v1/conversations/{room}/messages",
                              json={"content": "read"}, headers=headers(client, owner))
        action_id = _wait_for_action(client, room)
        assert client.post(f"{path}/actions/{action_id}/decision",
                           json=inspected_approval(client, owner, room, action_id, "once"), headers=headers(client, owner)).status_code == 200
        pending.result(timeout=3)
    with client.app.state.session_factory() as db:
        event = db.query(RoomEvent).filter_by(room_id=room, kind="tool_result").one()
        assert event.payload["text"] == "synthetic result"
        watermark = db.query(RoomEvent).filter_by(room_id=room).order_by(RoomEvent.seq.desc()).first().seq
        from app.services.context import build_turn_context
        from app.services.providers.base import attributed_messages
        request = await build_turn_context(db, room, peer, watermark)
        assert any("synthetic result" in item["content"] for item in attributed_messages(request))
    from app.main import create_app
    from fastapi.testclient import TestClient
    client.__exit__(None, None, None)  # End the first backend before restart.
    with TestClient(create_app(database_url=sqlite_url)) as restarted:
        with restarted.app.state.session_factory() as db:
            request = await build_turn_context(db, room, peer, watermark)
            assert any("synthetic result" in item["content"] for item in attributed_messages(request))
            assert db.query(RoomEvent).filter_by(room_id=room, kind="tool_result").count() == 1


def test_terminal_event_rollback_leaves_action_recoverable(room_client, monkeypatch):
    client, (owner, _, project, _, _) = room_client
    room, agent, _ = _setup(client, owner, project)
    from app.services import tool_broker
    from app.services.tool_broker import record_action_result
    with client.app.state.session_factory() as db:
        db.add(ToolAction(action_id="fault-window", room_id=room, agent_id=agent,
                          family="read_file", arguments={}, state="started"))
        db.commit()
    original = tool_broker.stage_event

    def fail_after_stage(db, *args, **kwargs):
        original(db, *args, **kwargs)
        raise RuntimeError("synthetic event commit failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(tool_broker, "stage_event", fail_after_stage)
        with pytest.raises(RuntimeError, match="synthetic event commit failure"):
            with client.app.state.session_factory() as db:
                record_action_result(db, "fault-window", "completed", "safe output")
    with client.app.state.session_factory() as db:
        assert db.get(ToolAction, "fault-window").state == "started"
        assert db.query(RoomEvent).filter_by(room_id=room, kind="tool_result").count() == 0
        record_action_result(db, "fault-window", "uncertain", "Effect unknown after interruption")
        record_action_result(db, "fault-window", "uncertain", "Effect unknown after interruption")
        assert db.get(ToolAction, "fault-window").state == "uncertain"
        assert db.query(RoomEvent).filter_by(room_id=room, kind="tool_result").count() == 1


@pytest.mark.asyncio
async def test_executor_timeout_has_one_uncertain_result_event(room_client):
    client, (owner, _, project, _, _) = room_client
    room, agent, _ = _setup(client, owner, project)
    broker = client.app.state.scheduler.broker
    prepared = broker.prepare("read_file", {"path": "synthetic.txt"})

    async def executor(action, grant, cancel):
        await asyncio.sleep(1)
        return "late result"

    broker.executor = executor
    assert client.put("/api/v1/tools/broad-mode", json={"enabled": True, "expires_minutes": 10},
                      headers=headers(client, owner)).status_code == 200
    action_id = str(uuid4())
    with client.app.state.session_factory() as db:
        action = ToolAction(action_id=action_id,room_id=room,agent_id=agent,
            family="read_file",arguments=prepared.public_arguments,target=prepared.target,
            scope_target=prepared.scope_target,payload_digest=prepared.payload_digest,
            expected_effect=prepared.expected_effect,state="pending",session_id=broker.session_id)
        db.add(action);db.commit();retain_test_review(db,action,prepared,broker,owner)
        db.expunge(action)
    from app.services.llm_service import LLMFailure
    with pytest.raises(LLMFailure):
        await broker.execute(action, None, asyncio.Event(), ExecutorBudget(.02))
    with client.app.state.session_factory() as db:
        assert db.get(ToolAction, action_id).state == "uncertain"
        events = db.query(RoomEvent).filter_by(room_id=room, kind="tool_result").all()
        assert len(events) == 1 and events[0].payload["state"] == "uncertain"
