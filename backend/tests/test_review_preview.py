"""Disposable registration/preview checks, not approval or executor proof."""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from test_room_state import room_client, headers
from test_action_review import _authority_fixture, posix_metadata
from test_action_review_registration import setup_registration
from test_tool_grants import _setup, _wait_for_action
from app.models.collaboration import ToolAction
from app.models.models import Agent
from app.services.action_review import ProposerBinding, ReviewChanged, observe_authority, frozen_proposer, observed_owner_epoch
from app.services.providers import ProviderResult, ToolCall
from app.services.review_snapshot import capture_review, retained_review, snapshot_value, stage_initial_review


@pytest.fixture
def preview_fixture(room_client, tmp_path):
    client, (owner, _, project, _, _) = room_client
    room, agent, proposer = _authority_fixture(client, owner, project)
    broker = client.app.state.scheduler.broker
    path = tmp_path.resolve() / "fixture"
    path.write_bytes(b"AAAA")
    def proposal(family="read_file", args=None, executor=None, workflow=None):
        broker.executor = executor
        prepared = broker.prepare(family, {"path": str(path)} if args is None else args, workflow)
        action = ToolAction(action_id=str(uuid4()), room_id=room, agent_id=agent,
            trigger_event_id=str(uuid4()), call_id="fixture", session_id=broker.session_id,
            workflow_id=workflow, state="waiting", family=prepared.family,
            target=prepared.target, scope_target=prepared.scope_target, arguments=prepared.public_arguments,
            expected_effect=prepared.expected_effect, payload_digest=prepared.payload_digest)
        with broker.sessions() as db:
            original = ProposerBinding(**{**proposer.model_dump(),
                "owner_epoch": observed_owner_epoch(broker.owner_epochs, owner)})
            authority = observe_authority(db, room_id=room, agent_id=agent,
                session_id=broker.session_id, proposer=original, owner_epochs=broker.owner_epochs)
            stage_initial_review(action, capture_review(action, prepared, authority, executor))
            db.add(action); db.commit(); db.refresh(action); db.expunge(action)
        broker.retain(action.action_id, prepared, proposer=original)
        return action
    return client, owner, broker, path, proposal


@posix_metadata
def test_unchanged_preview_is_stable_and_target_drift_explicitly_changes_token(preview_fixture):
    client, owner, broker, path, proposal = preview_fixture
    action = proposal()
    uri = f"/api/v1/tools/rooms/{action.room_id}/actions/{action.action_id}/preview"
    first = client.get(uri, headers=headers(client, owner))
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    old = first.json()["review_id"]
    assert client.get(uri, headers=headers(client, owner)).json()["review_id"] == old
    (path.parent / "unrelated").write_text("does not change this file's review")
    assert broker.preview(action, owner_id=owner)["review_id"] == old
    path.write_bytes(b"BBBB")  # Same size; the target version, not the payload, changed.
    fresh = broker.preview(action, owner_id=owner)
    assert fresh["review_id"] != old and fresh["payload_digest"] == first.json()["payload_digest"]
    assert fresh["review"]["targets"][0]["leaf"] != first.json()["review"]["targets"][0]["leaf"]
    assert broker.preview(action, owner_id=owner)["review_id"] == fresh["review_id"]
    with broker.sessions() as db:
        saved = db.get(ToolAction, action.action_id)
        assert saved.review_id == fresh["review_id"] and saved.state == "waiting"
        assert saved.approved_at is None and saved.grant_id is None


@pytest.mark.parametrize("change", ["approved", "identity", "payload", "legacy", "session", "owner"])
@posix_metadata
def test_preview_never_rebinds_stale_identity_payload_approved_or_legacy(preview_fixture, change):
    _, owner, broker, path, proposal = preview_fixture
    action = proposal("write_file", {"path": str(path), "content": "private fixture"})
    old = action.review_id
    with broker.sessions() as db:
        row = db.get(ToolAction, action.action_id)
        if change == "approved": row.state = "approved"
        elif change == "identity": db.get(Agent, action.agent_id).model_id = "other"
        elif change == "legacy": row.review_id = row.review_snapshot = None
        elif change == "session": row.session_id = str(uuid4())
        db.commit()
    if change == "approved": path.write_text("changed")
    if change == "payload": broker._previews[action.action_id].raw_arguments["content"] = "different"
    if change in ("session", "owner"):
        assert broker.preview(action, owner_id=owner + (change == "owner")) is None
    else:
        with pytest.raises(ReviewChanged): broker.preview(action, owner_id=owner)
    with broker.sessions() as db:
        assert db.get(ToolAction, action.action_id).review_id == (None if change == "legacy" else old)


@posix_metadata
def test_two_concurrent_target_refreshes_return_one_new_review(preview_fixture):
    _, owner, broker, path, proposal = preview_fixture
    action = proposal(); old = action.review_id
    path.write_bytes(b"CCCC")
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: broker.preview(action, owner_id=owner)["review_id"], range(2)))
    assert ids[0] == ids[1] != old


@posix_metadata
def test_process_review_binds_executable_and_implicit_cwd_without_argv_env(preview_fixture):
    _, owner, broker, path, proposal = preview_fixture
    action = proposal("start_process", {"executable": str(path), "argv": ["private argv"], "env": {"FIXTURE": "private env"}})
    value = broker.preview(action, owner_id=owner)
    review = value["review"]
    assert [t["role"] for t in review["targets"]] == ["executable", "cwd"]
    assert review["targets"][1]["path"] == os.getcwd()
    assert review["external_context"] == {"account": "unsupported", "target_version": "unsupported"}
    assert "private argv" not in json.dumps(review) and "private env" not in json.dumps(review)
    assert value["arguments"]["argv"] == ["private argv"]  # Owner-only volatile preview still works.
    path.write_bytes(b"DDDD")
    assert broker.preview(action, owner_id=owner)["review_id"] != value["review_id"]


@posix_metadata
def test_process_cwd_root_is_supported_without_allowing_file_root_or_aliases(preview_fixture):
    _, owner, broker, path, proposal = preview_fixture
    action = proposal("start_process", {"executable": str(path), "cwd": "/"})
    value = broker.preview(action, owner_id=owner)["review"]
    assert value["targets"][1]["path"] == "/" and value["targets"][1]["status"] == "verified"
    from app.services.action_review import observe_file_target
    for path in ("/", "//", "///"):
        with pytest.raises(ValueError): observe_file_target(path)
    for path in ("//", "///"):
        with pytest.raises(ValueError): observe_file_target(path, "cwd")


def test_owned_process_generation_and_context_cannot_be_refreshed_to_another(preview_fixture):
    _, owner, broker, _, proposal = preview_fixture
    handle = uuid4().hex
    processes = SimpleNamespace(running={})
    executor = SimpleNamespace(processes=processes)
    # A missing owned process is unavailable, not a verified arbitrary PID.
    unavailable = proposal("cancel_process", {"process_id": handle}, executor, "flow")
    assert broker.preview(unavailable, owner_id=owner)["review"]["targets"][0]["status"] == "unavailable"
    owned = SimpleNamespace(handle=handle, room_id=unavailable.room_id, agent_id=unavailable.agent_id,
        session_id=broker.session_id, workflow_id="flow", generation=3,
        deadline=time.monotonic()+60, process=SimpleNamespace(poll=lambda: None))
    processes.running[handle] = owned
    action = proposal("cancel_process", {"process_id": handle}, executor, "flow")
    before = broker.preview(action, owner_id=owner)
    assert before["review"]["targets"][0]["generation"] == 3
    owned.generation = 4
    with pytest.raises(ReviewChanged): broker.preview(action, owner_id=owner)
    owned.agent_id += 1
    with pytest.raises(ReviewChanged): proposal("cancel_process", {"process_id": handle}, executor, "flow")


def test_browser_context_lifecycle_is_bound_without_account_cookie_or_profile_path(preview_fixture):
    _, owner, broker, _, proposal = preview_fixture
    browser = SimpleNamespace(workflows={})
    executor = SimpleNamespace(browser=browser)
    action = proposal("browser_interact", {"url": "https://example.invalid/path?q=private-query",
        "selector": "private-selector", "operation": "fill", "text": "private-text"}, executor, "flow")
    value = broker.preview(action, owner_id=owner)
    assert value["review"]["targets"][0]["status"] == "absent"
    session = SimpleNamespace(room_id=action.room_id, agent_id=action.agent_id, workflow_id="flow",
        deadline=time.monotonic()+60, page=SimpleNamespace(is_closed=lambda: False),
        authority=SimpleNamespace(active=lambda: True), context_id=uuid4().hex)
    browser.workflows[(action.room_id, action.agent_id, "flow")] = session
    with pytest.raises(ReviewChanged): broker.preview(action, owner_id=owner)
    current = proposal("browser_inspect", {"url": "https://example.invalid/path"}, executor, "flow")
    review = broker.preview(current, owner_id=owner)["review"]
    assert review["targets"][0]["context_id"] == session.context_id
    assert review["external_context"]["account"] == "unsupported"
    assert not any(s in json.dumps(review) for s in ("private-query", "private-selector", "private-text", "cookie"))
    session.context_id = uuid4().hex
    with pytest.raises(ReviewChanged): broker.preview(current, owner_id=owner)


@pytest.mark.parametrize("family,args", [("screenshot", {}), ("pointer_click", {"x": 1, "y": 2}),
    ("pointer_move", {"x": 1, "y": 2}), ("pointer_scroll", {"x": 1, "y": 2, "delta": 1}),
    ("keyboard_text", {"text": "private text"}), ("keyboard_chord", {"keys": ["META", "a"]})])
def test_native_configuration_identity_does_not_claim_successful_ipc_or_external_account(preview_fixture, family, args):
    _, owner, broker, _, proposal = preview_fixture
    native = SimpleNamespace(context_id=uuid4().hex)
    executor = SimpleNamespace(desktop=SimpleNamespace(native=native))
    action = proposal(family, args, executor, "flow")
    value = broker.preview(action, owner_id=owner)
    assert value["review"]["targets"][0]["status"] == "configured"
    assert value["review"]["external_context"]["target_version"] == "unsupported"
    assert "private text" not in json.dumps(value["review"])
    native.context_id = uuid4().hex
    with pytest.raises(ReviewChanged): broker.preview(action, owner_id=owner)


def test_owner_authentication_epoch_is_frozen_before_provider_await(room_client, tmp_path):
    client, (owner, _, project, _, _) = room_client
    room, agent, effects = setup_registration(client, owner, project)
    epochs = SimpleNamespace(value="a"*64)
    epochs.current = lambda _: epochs.value
    client.app.state.scheduler.broker.owner_epochs = epochs
    seen = []
    async def provider(request):
        seen.append(frozen_proposer(request, expected_owner_id=owner, owner_epochs=epochs).owner_epoch)
        epochs.value = "b"*64
        return ProviderResult(tool_calls=[ToolCall("fixture", "read_file", {"path": str(tmp_path/"file")})],
                              continuation={"call_ids": ["fixture"]})
    client.app.state.scheduler.provider = provider
    client.post(f"/api/v1/conversations/{room}/messages", json={"content": "fixture"}, headers=headers(client, owner))
    assert seen == ["a"*64] and effects == []
    with client.app.state.session_factory() as db:
        assert db.query(ToolAction).filter_by(room_id=room).count() == 0


def test_missing_or_changed_trusted_epoch_cannot_be_filled_from_live_owner(room_client):
    client, (owner, _, project, _, _) = room_client
    room, agent, proposer = _authority_fixture(client, owner, project)
    from app.services.action_review import ProposerBinding
    bound = ProposerBinding(**{**proposer.model_dump(), "owner_epoch": "a"*64})
    for current in (None, "b"*64, "caller-label"):
        with client.app.state.session_factory() as db, pytest.raises(ReviewChanged):
            observe_authority(db, room_id=room, agent_id=agent, session_id=str(uuid4()), proposer=bound,
                              owner_epochs=SimpleNamespace(current=lambda _: current))
    with client.app.state.session_factory() as db, pytest.raises(ReviewChanged):
        observe_authority(db, room_id=room, agent_id=agent, session_id=str(uuid4()), proposer=bound)


def test_preview_keeps_original_auth_epoch_and_cannot_refresh_after_logout(preview_fixture):
    _, owner, broker, _, proposal = preview_fixture
    epochs = SimpleNamespace(value="a"*64)
    epochs.current = lambda _: epochs.value
    broker.owner_epochs = epochs
    action = proposal()
    first = broker.preview(action, owner_id=owner)
    assert first["review"]["checks"]["owner_authentication"] == "verified"
    for value in ("b"*64, None):
        epochs.value = value
        with pytest.raises(ReviewChanged): broker.preview(action, owner_id=owner)
        with broker.sessions() as db:
            assert db.get(ToolAction, action.action_id).review_id == first["review_id"]


def test_missing_native_adapter_and_bad_runtime_ownership_are_not_marked_verified(preview_fixture):
    _, owner, broker, _, proposal = preview_fixture
    action = proposal("screenshot", {}, SimpleNamespace(desktop=SimpleNamespace(native=None)), "flow")
    first = broker.preview(action, owner_id=owner)["review"]
    assert first["targets"][0]["status"] == "unavailable"
    assert first["checks"]["owner_authentication"] == "unsupported"
    first["targets"][0]["context_id"] = uuid4().hex
    from app.services.review_snapshot import ReviewSnapshot
    with pytest.raises(ValidationError): ReviewSnapshot.model_validate_json(json.dumps(first))
    unknown = proposal("screenshot", {}, SimpleNamespace(desktop=SimpleNamespace(native=object())), "flow")
    assert broker.preview(unknown, owner_id=owner)["review"]["targets"][0]["status"] == "unsupported"


@pytest.mark.parametrize("family,args", [("start_process", {"executable": "/synthetic-not-executed"}),
    ("cancel_process", {"process_id": "a"*32}), ("browser_navigate", {"url": "https://example.invalid"}),
    ("keyboard_text", {"text": "fixture"})])
def test_scheduler_registers_non_file_typed_review_before_waiting_without_effect(room_client, tmp_path, family, args):
    client, (owner, _, project, _, _) = room_client
    if family == "start_process":
        executable = tmp_path.resolve()/"program"; executable.write_text("synthetic executable observation")
        cwd = tmp_path.resolve()/"cwd"; cwd.mkdir()
        args = {**args, "executable":str(executable), "cwd":str(cwd)}
    room, agent, effects = _setup(client, owner, project, family, args)
    broker = client.app.state.scheduler.broker
    if family.startswith("browser_"):
        broker.executor.browser = SimpleNamespace(workflows={})
    if family == "cancel_process":
        handle = args["process_id"]
        owned = SimpleNamespace(handle=handle, room_id=room, agent_id=agent, session_id=broker.session_id,
            workflow_id=None, generation=1, process=SimpleNamespace(poll=lambda:None), deadline=time.monotonic()+30)
        broker.executor.processes = SimpleNamespace(running={handle:owned})
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"/api/v1/conversations/{room}/messages",
            json={"content": "propose fixture"}, headers=headers(client, owner))
        try:
            action_id = _wait_for_action(client, room)
            with client.app.state.session_factory() as db:
                action = db.get(ToolAction, action_id)
                assert retained_review(action).action.family == family
                assert action.approved_at is None and action.approval_owner_id is None
            assert effects == []
            assert client.post(f"/api/v1/tools/rooms/{room}/actions/{action_id}/decision",
                json={"decision": "deny"}, headers=headers(client, owner)).status_code == 200
            assert pending.result(timeout=3).status_code == 201
        finally:
            if not pending.done():
                client.post(f"/api/v1/conversations/{room}/cancel", headers=headers(client, owner))
                pending.result(timeout=3)
    assert effects == []
