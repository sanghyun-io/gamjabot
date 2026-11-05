"""Provider-await/registration boundaries with disposable DB and no effects."""

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from test_room_state import room_client, headers, create_room
from test_scheduler import ready_agents, enable_tools
from test_tool_grants import _wait_for_action
from app.models.collaboration import ProviderConnection, RoomEvent, ToolAction
from app.models.models import Agent, Conversation, Project
from app.services.action_review import ReviewChanged, frozen_proposer
from app.services.providers import ProviderResult, ToolCall
from app.services.review_snapshot import retained_review


def setup_registration(client, owner, project):
    room = create_room(client, owner, project, "registration")
    agent = ready_agents(client, owner, room, ("Ada",))[0]
    enable_tools(client, agent)
    effects = []
    async def executor(*_):
        effects.append("unexpected")
        raise AssertionError("Registration checks must not run an effect")
    client.app.state.scheduler.broker.executor = executor
    return room, agent, effects


@pytest.mark.parametrize("change", ["model", "assignment", "config", "auth", "provider", "protocol", "disabled", "agent_owner", "connection_owner", "all_owner_transfer"])
def test_assignment_change_during_provider_await_never_registers_an_action(room_client, tmp_path, change):
    client, (owner, other, project, _, _) = room_client
    room, agent_id, effects = setup_registration(client, owner, project)
    frozen_seen = []
    async def provider(request):
        assert request.tools and request.connection.owner_id == owner
        frozen_seen.append(request.connection.config_revision)
        # Another transaction commits while the original proposal is in flight.
        with client.app.state.session_factory() as db:
            agent = db.get(Agent, agent_id)
            connection = db.get(ProviderConnection, agent.connection_id)
            if change == "model": agent.model_id = "changed"
            elif change == "assignment": agent.connection_id = None
            elif change == "config": connection.readiness_revision += 1
            elif change == "auth": connection.auth_grant_revision = (connection.auth_grant_revision or 0) + 1
            elif change == "provider": connection.provider_id = "changed"
            elif change == "protocol": connection.protocol_id = "changed"
            elif change == "disabled": connection.enabled = False
            elif change == "agent_owner": agent.user_id = other
            elif change == "connection_owner": connection.user_id = other
            elif change == "all_owner_transfer":
                agent.user_id = connection.user_id = other
                db.get(Conversation, room).user_id = other
                db.get(Project, project).user_id = other
            db.commit()
        return ProviderResult(tool_calls=[ToolCall("original-call", "read_file", {"path": str(tmp_path / "owned")})],
                              continuation={"call_ids": ["original-call"]})
    client.app.state.scheduler.provider = provider
    client.post(f"/api/v1/conversations/{room}/messages", json={"content": "inspect fixture"},
                headers=headers(client, owner))
    assert len(frozen_seen) == 1 and effects == []
    with client.app.state.session_factory() as db:
        assert db.query(ToolAction).filter_by(room_id=room).count() == 0
        failures = db.query(RoomEvent).filter_by(room_id=room, kind="failure").all()
        if change in ("agent_owner", "all_owner_transfer"):
            from app.models.collaboration import RoomAgentRun
            assert failures == []  # The old proposer cannot speak as the transferred Agent.
            run = db.get(RoomAgentRun, (room, agent_id))
            assert run.state == "idle" and run.active_tool == {}
        else:
            assert len(failures) == 1 and failures[0].payload["code"] == "review_changed"


def test_valid_file_registration_commits_review_without_content_or_authorization(room_client, tmp_path):
    client, (owner, _, project, _, _) = room_client
    room, agent_id, effects = setup_registration(client, owner, project)
    target = tmp_path.resolve() / "owned.txt"
    marker = "OWNED_PRIVATE_CONTENT_NOT_TO_PERSIST"
    captured = []
    async def provider(request):
        if request.observations:
            return ProviderResult(text="fixture finished without effect")
        captured.append(frozen_proposer(request, expected_owner_id=owner))
        # Display-only labels are not proposer identity or target version.
        with client.app.state.session_factory() as db:
            agent = db.get(Agent, agent_id)
            agent.name = "New display label"
            db.get(ProviderConnection, agent.connection_id).name = "New connection label"
            db.commit()
        return ProviderResult(tool_calls=[ToolCall("file-call", "write_file", {"path": str(target), "content": marker})],
                              continuation={"call_ids": ["file-call"]})
    client.app.state.scheduler.provider = provider
    broker = client.app.state.scheduler.broker
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"/api/v1/conversations/{room}/messages", json={"content": "propose fixture"},
                              headers=headers(client, owner))
        try:
            action_id = _wait_for_action(client, room)
            with client.app.state.session_factory() as db:
                action = db.get(ToolAction, action_id)
                snapshot = retained_review(action)
                assert snapshot.authority.proposer == captured[0] == broker._proposers[action_id]
                assert snapshot.targets[0].status == "absent"
                assert action.approved_at is None and action.approval_owner_id is None
                assert marker not in json.dumps(action.review_snapshot)
                assert marker not in json.dumps(action.arguments)
                assert action.review_id is not None
            assert effects == [] and not target.exists()
            assert client.post(f"/api/v1/tools/rooms/{room}/actions/{action_id}/decision",
                json={"decision": "deny"}, headers=headers(client, owner)).status_code == 200
            assert pending.result(timeout=3).status_code == 201
        finally:
            if not pending.done():
                client.post(f"/api/v1/conversations/{room}/cancel", headers=headers(client, owner))
                pending.result(timeout=3)
    assert effects == [] and not target.exists()
    assert action_id not in broker._previews and action_id not in broker._proposers


def test_registered_file_snapshot_is_rolled_back_if_run_generation_loses_registration(room_client, tmp_path, monkeypatch):
    import app.services.scheduler as module
    client, (owner, _, project, _, _) = room_client
    room, agent_id, effects = setup_registration(client, owner, project)
    original = module.stage_initial_review
    seen = []
    def stop_generation(action, snapshot):
        result = original(action, snapshot)
        seen.append(result)
        from app.models.collaboration import RoomAgentRun
        with client.app.state.session_factory() as other:
            run = other.get(RoomAgentRun, (room, agent_id))
            run.generation += 1
            other.commit()
        return result
    monkeypatch.setattr(module, "stage_initial_review", stop_generation)
    async def provider(_request):
        return ProviderResult(tool_calls=[ToolCall("file-call", "read_file", {"path": str(tmp_path / "owned")})],
                              continuation={"call_ids": ["file-call"]})
    client.app.state.scheduler.provider = provider
    client.post(f"/api/v1/conversations/{room}/messages", json={"content": "propose fixture"}, headers=headers(client, owner))
    assert len(seen) == 1 and effects == []
    with client.app.state.session_factory() as db:
        assert db.query(ToolAction).filter_by(room_id=room).count() == 0
    assert client.app.state.scheduler.broker._proposers == {}


def test_retain_cannot_replace_an_original_proposer_and_finish_cleans_it(room_client):
    from test_action_review import _authority_fixture
    from app.services.action_review import ProposerBinding
    client, (owner, _, project, _, _) = room_client
    room, agent, proposer = _authority_fixture(client, owner, project)
    broker = client.app.state.scheduler.broker
    action_id = str(uuid4())
    prepared = broker.prepare("read_file", {"path": "/owned-synthetic-not-opened"})
    broker.retain(action_id, prepared, proposer=proposer)
    different = ProposerBinding(**{**proposer.model_dump(), "model_id": "changed"})
    with pytest.raises(ReviewChanged): broker.retain(action_id, prepared, proposer=different)
    assert broker._proposers[action_id] == proposer
    broker._finish(action_id, "cancelled")
    assert action_id not in broker._proposers


def test_owner_transfer_between_tool_rounds_never_resolves_new_owner_context(room_client, tmp_path, monkeypatch):
    import app.services.scheduler as module
    from app.services.tool_broker import ToolBroker, ActionResult
    from app.models.collaboration import RoomAgentRun
    client, (owner, other, project, _, _) = room_client
    room, agent, effects = setup_registration(client, owner, project)
    built, requests = [], []
    build = module.build_turn_context
    async def count_context(*args, **kwargs):
        built.append(args[1])
        return await build(*args, **kwargs)
    monkeypatch.setattr(module, "build_turn_context", count_context)
    class NoEffectBroker(ToolBroker):
        async def execute(self, action, *_):
            # Registration-only double. No grant/claim or external effect is
            # exercised; a fixture result advances the scheduler continuation.
            self._finish(action.action_id, "completed", "synthetic no effect")
            with self.sessions() as db:
                a = db.get(Agent, agent)
                a.user_id = other
                db.get(ProviderConnection, a.connection_id).user_id = other
                db.get(Conversation, room).user_id = other
                db.get(Project, project).user_id = other
                db.commit()
            return ActionResult("completed", "synthetic no effect")
    client.app.state.scheduler.broker = NoEffectBroker(client.app.state.session_factory)
    async def provider(request):
        requests.append(request.connection.owner_id)
        assert len(requests) == 1, "The transferred Owner's provider must never be called"
        return ProviderResult(tool_calls=[ToolCall("file-call", "read_file", {"path": str(tmp_path / "owned")})],
                              continuation={"call_ids": ["file-call"]})
    client.app.state.scheduler.provider = provider
    client.post(f"/api/v1/conversations/{room}/messages", json={"content": "registration fixture"}, headers=headers(client, owner))
    assert requests == [owner] and built == [room] and effects == []
    with client.app.state.session_factory() as db:
        action = db.query(ToolAction).filter_by(room_id=room).one()
        assert retained_review(action).authority.owner_id == owner
        assert db.get(RoomAgentRun, (room, agent)).state == "idle"
        assert db.query(RoomEvent).filter_by(room_id=room, kind="failure").count() == 0


def test_rejected_later_proposal_preserves_previous_result_and_reports_no_new_action(room_client, tmp_path):
    from app.services.tool_broker import ToolBroker, ActionResult
    client, (owner, _, project, _, _) = room_client
    room, agent, effects = setup_registration(client, owner, project)
    class NoEffectBroker(ToolBroker):
        async def execute(self, action, *_):
            self._finish(action.action_id, "completed", "synthetic registration-only result")
            return ActionResult("completed", "synthetic registration-only result")
    client.app.state.scheduler.broker = NoEffectBroker(client.app.state.session_factory)
    requests = []
    async def provider(request):
        requests.append(request)
        if request.observations:
            with client.app.state.session_factory() as db:
                connection = db.get(ProviderConnection, request.connection.connection_id)
                connection.readiness_revision += 1
                db.commit()
        call_id = "first" if not request.observations else "second"
        return ProviderResult(tool_calls=[ToolCall(call_id, "read_file", {"path": str(tmp_path / "owned")})],
                              continuation={"call_ids": [call_id]})
    client.app.state.scheduler.provider = provider
    client.post(f"/api/v1/conversations/{room}/messages", json={"content": "registration fixture"}, headers=headers(client, owner))
    assert len(requests) == 2 and effects == []
    with client.app.state.session_factory() as db:
        action = db.query(ToolAction).filter_by(room_id=room).one()
        assert action.call_id == "first" and action.state == "completed"
        results = db.query(RoomEvent).filter_by(room_id=room, kind="tool_result").all()
        assert len(results) == 1 and results[0].payload["state"] == "completed"
        failure = db.query(RoomEvent).filter_by(room_id=room, kind="failure").one()
        assert failure.payload["code"] == "review_changed"
        assert "no new tool action" in failure.payload["message"]
