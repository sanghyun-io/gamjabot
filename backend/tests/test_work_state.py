"""Only disposable SQLite, synthetic providers, and explicit execution identities."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep
from uuid import uuid4

import pytest
from pydantic import ValidationError

from test_room_state import room_client, headers, create_room
from test_scheduler import ready_agents
from app.models.collaboration import RoomAgentRun, RoomEvent, RoomTrigger, ToolAction
from app.models.models import Conversation
from app.schemas.work_state import WorkState, WorkEventRef
from app.services.events import ActorRef, append_event
from app.services.providers import ProviderResult
from app.services.room_service import begin_turn, list_members


def make_work_room(room_client):
    client, (owner, other, project, _, _) = room_client
    room = create_room(client, owner, project, "work-state")
    agents = ready_agents(client, owner, room, ("Ada", "Ben"))
    return client, owner, other, room, agents


def trigger(db, room, owner, agents, state="running"):
    event = append_event(db, room, ActorRef("user", owner), "message", {"content": "synthetic request"})
    row = RoomTrigger(event_id=event.event_id, room_id=room, state=state,
                      selected_agents=agents, max_rounds=2, current_round=1)
    db.add(row)
    db.commit()
    return row


def run_frame(db, room, agent, request, state="idle", generation=3, round_number=1):
    run = db.get(RoomAgentRun, (room, agent))
    run.trigger_event_id, run.generation, run.round, run.state = request.event_id, generation, round_number, state
    db.commit()
    return run


def terminal(db, room, agent, request, *, kind="message", generation=3, round_number=1, **extra):
    payload = {"trigger_event_id": request.event_id, "round": round_number, "generation": generation, **extra}
    if kind == "message":
        payload["content"] = "synthetic answer"
    return append_event(db, room, ActorRef("agent", agent), kind, payload)


def states(client, owner, room):
    response = client.get(f"/api/v1/conversations/{room}/members", headers=headers(client, owner))
    assert response.status_code == 200, response.text
    return {member["agent_id"]: member["work_state"] for member in response.json()}


def test_current_request_never_uses_old_result_and_running_beats_newer_queue(room_client):
    client, owner, other, room, (ada, ben) = make_work_room(room_client)
    with client.app.state.session_factory() as db:
        old = trigger(db, room, owner, [ada, ben], "done")
        run_frame(db, room, ada, old)
        terminal(db, room, ada, old)
        old_id = old.event_id
    previous = states(client, owner, room)[ada]
    assert previous["scope"] == "previous" and previous["phase"] == "completed"
    assert previous["request"]["event_id"] == old_id
    with client.app.state.session_factory() as db:
        current = trigger(db, room, owner, [ada, ben], "pending")
        current_id = current.event_id
    queued = states(client, owner, room)[ada]
    assert queued["phase"] == "queued" and queued["scope"] == "current" and queued["result"] is None
    with client.app.state.session_factory() as db:
        db.get(RoomTrigger, current_id).state = "running"
        db.commit()
        assert begin_turn(db, room, ada, trigger_event_id=current_id, round_number=1, watermark=2) == 4
        newer = trigger(db, room, owner, [ada, ben], "pending")
        newer_id = newer.event_id
    active = states(client, owner, room)[ada]
    assert active["phase"] == "thinking" and active["trigger_event_id"] == current_id
    assert active["trigger_event_id"] != newer_id and active["result"] is None
    assert client.get(f"/api/v1/conversations/{room}/members", headers=headers(client, other)).status_code == 404
    with client.app.state.session_factory() as db:
        with pytest.raises(Exception) as error:
            list_members(db, room, other)
        assert error.value.status_code == 404


@pytest.mark.parametrize("bad", ["generation", "round", "legacy", "failure", "paused_peer", "old_peer", "single_round"])
def test_peer_wait_only_from_exact_successful_wave(room_client, bad):
    client, owner, _, room, (ada, ben) = make_work_room(room_client)
    with client.app.state.session_factory() as db:
        request = trigger(db, room, owner, [ada, ben])
        run_frame(db, room, ada, request)
        peer = run_frame(db, room, ben, request, "running")
        args = {"generation": 2} if bad == "generation" else {"round_number": 2} if bad == "round" else {}
        event = terminal(db, room, ada, request, kind="failure" if bad == "failure" else "message", **args)
        if bad == "legacy":
            payload = dict(event.payload); payload.pop("generation"); event.payload = payload
        if bad == "paused_peer": peer.paused = True
        if bad == "old_peer": peer.trigger_event_id = str(uuid4())
        if bad == "single_round": request.max_rounds = 1
        db.commit()
    result = states(client, owner, room)
    assert result[ada]["phase"] != "peer_waiting"
    if bad in {"generation", "round", "legacy"}:
        assert result[ada]["phase"] == "unknown" and result[ada]["result"] is None
    if bad == "failure": assert result[ada]["phase"] == "failed"


@pytest.mark.parametrize("action_state,phase", [("waiting", "approval_waiting"), ("pending", "tool_waiting"),
    ("approved", "tool_waiting"), ("started", "tool_running"), ("uncertain", "uncertain")])
def test_live_action_identity_wins_over_older_waiting_call(room_client, action_state, phase):
    client, owner, _, room, (ada, ben) = make_work_room(room_client)
    with client.app.state.session_factory() as db:
        request = trigger(db, room, owner, [ada, ben])
        run = run_frame(db, room, ada, request, "running")
        current_id = str(uuid4())
        for action_id, state in ((str(uuid4()), "waiting"), (current_id, action_state)):
            db.add(ToolAction(action_id=action_id, room_id=room, agent_id=ada, trigger_event_id=request.event_id,
                              call_id="same-call", family="read_file", arguments={}, state=state))
        run.active_tool = {"call_ids": ["same-call"], "action_id": current_id, "generation": 3, "round": 1}
        db.commit()
    value = states(client, owner, room)[ada]
    assert value["phase"] == phase and value["action_id"] == current_id
    with client.app.state.session_factory() as db:
        run = db.get(RoomAgentRun, (room, ada)); run.generation += 1; db.commit()
    assert states(client, owner, room)[ada]["phase"] == "unknown"


@pytest.mark.parametrize("mode,phase", [("paused", "paused"), ("stopped", "stopped"),
    ("closed", "stopped"), ("cancelled", "cancelled"), ("interrupted", "interrupted"), ("uncertain", "uncertain")])
def test_invalidation_and_uncertainty_never_become_completion(room_client, mode, phase):
    client, owner, _, room, (ada, ben) = make_work_room(room_client)
    with client.app.state.session_factory() as db:
        request = trigger(db, room, owner, [ada, ben], "done")
        run = run_frame(db, room, ada, request)
        terminal(db, room, ada, request, kind="failure" if mode == "uncertain" else "message", code="tool_effect_uncertain")
        if mode == "paused": run.paused = True; run.generation += 1
        elif mode == "stopped": run.state = "stopped"; run.generation += 1
        elif mode == "closed": db.get(Conversation, room).is_active = False
        elif mode in {"cancelled", "interrupted"}: request.state = mode; run.generation += 1
        db.commit()
    assert states(client, owner, room)[ada]["phase"] == phase


def test_queued_followup_metadata_not_old_completion_and_reads_do_not_mutate(room_client):
    client, owner, _, room, (ada, ben) = make_work_room(room_client)
    with client.app.state.session_factory() as db:
        request = trigger(db, room, owner, [ada, ben]); request.current_round = 2
        run = run_frame(db, room, ada, request)
        terminal(db, room, ada, request)
        run.pending_task = {"work_wave": {"trigger_event_id": request.event_id, "round": 2, "generation": 3}}
        db.commit()
        request_id = request.event_id
    first = states(client, owner, room)
    assert first[ada]["phase"] == "queued" and first[ada]["round"] == 2 and first[ada]["result"] is None
    assert states(client, owner, room) == first
    with client.app.state.session_factory() as db:
        assert begin_turn(db, room, ada, trigger_event_id=request_id, round_number=2, watermark=3) == 4
        run = db.get(RoomAgentRun, (room, ada))
        assert run.trigger_event_id == request_id and run.round == 2 and run.watermark_seq == 3
        assert "work_wave" not in run.pending_task


def test_actual_scheduler_wave_has_two_different_states_and_exact_final_refs(room_client, capsys):
    client, owner, _, room, (ada, ben) = make_work_room(room_client)
    entered, release = threading.Event(), threading.Event()

    async def provider(request):
        if request.agent_id == ben:
            entered.set()
            await asyncio.to_thread(release.wait)
        return ProviderResult(text="synthetic final answer")

    client.app.state.scheduler.provider = provider
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f"/api/v1/conversations/{room}/messages",
            json={"content": "same wave"}, headers=headers(client, owner))
        try:
            assert entered.wait(3)
            deadline = monotonic() + 3
            while monotonic() < deadline:
                current = states(client, owner, room)
                if current[ada]["phase"] == "peer_waiting": break
                sleep(.01)
            assert current[ada]["phase"] == "peer_waiting" and current[ada]["peer_agent_ids"] == [ben]
            assert current[ben]["phase"] == "thinking"
            assert current[ada]["scope"] == current[ben]["scope"] == "current"
        finally:
            release.set()
        assert pending.result(timeout=5).status_code == 201
    final = states(client, owner, room)
    assert states(client, owner, room) == final
    with client.app.state.session_factory() as db:
        for agent in (ada, ben):
            value = final[agent]
            assert value["scope"] == "previous" and value["phase"] == "completed"
            event = db.get(RoomEvent, value["result"]["event_id"])
            assert event.seq == value["result"]["seq"] and event.actor_id == agent
            assert event.payload["generation"] == value["generation"] and event.payload["round"] == value["round"]
    from app.owner_cli import command

    class CanonicalAPI:
        def call(self, method, path, payload, *, authenticated):
            assert (method, path, payload, authenticated) == ("GET", f"/conversations/{room}/members", None, True)
            return client.get('/api/v1' + path, headers=headers(client, owner)).json()

    capsys.readouterr()
    command(CanonicalAPI(), ["members", str(room)])
    cli_data = json.loads(capsys.readouterr().out)
    assert {member["agent_id"]: member["work_state"] for member in cli_data} == final


def test_typed_contract_rejects_noncanonical_ids_bool_generation_and_version():
    with pytest.raises(ValidationError): WorkEventRef(event_id=str(uuid4()).upper(), seq=1)
    with pytest.raises(ValidationError): WorkState(room_id=1, agent_id=2, generation=True)
    with pytest.raises(ValidationError): WorkState(room_id=1, agent_id=2, version=True)


@pytest.mark.parametrize("state", ["cancelled", "done", "interrupted"])
def test_cancel_or_terminal_race_cannot_claim_a_queue_or_reopen_the_trigger(room_client, state):
    client, owner, _, room, (ada, ben) = make_work_room(room_client)
    with client.app.state.session_factory() as db:
        request = trigger(db, room, owner, [ada, ben], state)
        request_id = request.event_id
        run = db.get(RoomAgentRun, (room, ada))
        original_generation = run.generation
        assert begin_turn(db, room, ada, trigger_event_id=request_id, round_number=1, watermark=1) is None
        assert db.get(RoomAgentRun, (room, ada)).generation == original_generation
    calls = []

    async def forbidden(request):
        calls.append(request)
        return ProviderResult(text="should not be requested")

    client.app.state.scheduler.provider = forbidden
    asyncio.run(client.app.state.scheduler._run_trigger(request_id))
    assert calls == []
    with client.app.state.session_factory() as db:
        assert db.get(RoomTrigger, request_id).state == state


def test_later_peer_reply_does_not_resolve_an_unknown_tool_effect(room_client):
    client, owner, _, room, (ada, ben) = make_work_room(room_client)
    with client.app.state.session_factory() as db:
        request = trigger(db, room, owner, [ada, ben], "done"); request.current_round = 2
        run_frame(db, room, ada, request, generation=4, round_number=2)
        terminal(db, room, ada, request, generation=4, round_number=2)
        action_id = str(uuid4())
        db.add(ToolAction(action_id=action_id, room_id=room, agent_id=ada, trigger_event_id=request.event_id,
                          call_id="earlier-turn", family="write_file", arguments={}, state="uncertain"))
        db.commit()
        cause = append_event(db, room, ActorRef("tool", None), "tool_result", {
            "trigger_event_id": request.event_id, "agent_id": ada, "action_id": action_id, "state": "uncertain"})
        cause_id, cause_seq = cause.event_id, cause.seq
    value = states(client, owner, room)[ada]
    assert value["phase"] == "uncertain" and value["round"] == 2 and value["generation"] == 4
    assert value["event"] == {"event_id": cause_id, "seq": cause_seq} and value["action_id"] == action_id
