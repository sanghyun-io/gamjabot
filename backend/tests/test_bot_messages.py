"""Focused disposable contracts for explicit cross-room text collaboration."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading
import time
import pytest

from sqlalchemy import create_engine

from test_room_state import room_client, headers, create_room
from test_teammates import team
from app.models.bot_message import BotMessage
from app.models.collaboration import RoomAgentRun, RoomEvent
from app.services.events import ActorRef, append_event
from app.services.providers import ProviderResult, ToolCall


def cross_team(client, owner, project, *, nested=False):
    room = create_room(client, owner, project, "Cross-room source")
    agents = team(client, owner, room, ("Ada", "Ben", "Cy") if nested else ("Ada", "Ben"))
    personal = {}
    for agent in agents[1:]:
        assert client.delete(f"/api/v1/conversations/{room}/members/{agent}", headers=headers(client, owner)).status_code == 204
        result = client.post(f"/api/v1/bots/{agent}/conversation", headers=headers(client, owner), json={})
        assert result.status_code == 200, result.text
        personal[agent] = result.json()["room_id"]
    return room, agents, personal


def call_bot(agent, content, call_id="send-bot"):
    return ProviderResult(tool_calls=[ToolCall(call_id, "message_bot", {"agent_id": agent, "content": content})],
                          continuation={"call_ids": [call_id]})


def list_messages(client, owner, room):
    response = client.get(f"/api/v1/bots/conversations/{room}/messages", headers=headers(client, owner))
    assert response.status_code == 200, response.text
    return response.json()["messages"]


def test_cross_room_nested_models_return_real_result_without_private_history(room_client):
    client, (owner, other, project, _, _) = room_client
    room, (ada, ben, cy), personal = cross_team(client, owner, project, nested=True)
    with client.app.state.session_factory() as db:
        append_event(db, personal[ben], ActorRef("user", owner), "message", {"content": "PRIVATE_DM_NEVER_FORWARD_918"})
        run = db.get(RoomAgentRun, (personal[ben], ben))
        run.memory = {"private": "PRIVATE_MEMORY_NEVER_FORWARD_772"}
        db.commit()
    seen = []
    async def provider(request):
        body = request.system + json.dumps(request.events, ensure_ascii=False)
        seen.append((request.agent_id, request.room_id, body))
        if request.agent_id in {ben, cy}:
            assert "PRIVATE_DM_NEVER_FORWARD_918" not in body
            assert "PRIVATE_MEMORY_NEVER_FORWARD_772" not in body
            assert {tool["name"] for tool in request.tools} <= {"list_bots", "message_bot", "message_bots", "write_page"}
            assert request.memory is None
        if request.agent_id == cy:
            return ProviderResult(text="CY_REAL_REVIEW_48")
        if request.agent_id == ben:
            if "CY_REAL_REVIEW_48" in body:
                return ProviderResult(text="BEN_RESOLVED_WITH_CY_REAL_REVIEW_48")
            return ProviderResult(text="Ben sent the subtask; result is pending.") if request.observations else call_bot(cy, "Review exactly this delegated text", "ben-to-cy")
        if "BEN_RESOLVED_WITH_CY_REAL_REVIEW_48" in body:
            return ProviderResult(text="ADA_FINAL_WITH_BEN_RESOLVED_WITH_CY_REAL_REVIEW_48")
        return ProviderResult(text="Ada sent the task; result is pending.") if request.observations else call_bot(ben, "Prepare the actual reviewed conclusion", "ada-to-ben")
    client.app.state.scheduler.provider = provider
    response = client.post(f"/api/v1/conversations/{room}/messages", headers=headers(client, owner), json={"content": "@Ada finish using another Bot's actual work"})
    assert response.status_code == 201, response.text
    messages = list_messages(client, owner, room)
    assert len(messages) == 2 and all(m["state"] == "completed" and m["reply_state"] == "delivered" for m in messages), messages
    parent = next(m for m in messages if m["sender_agent_id"] == ada)
    child = next(m for m in messages if m["sender_agent_id"] == ben)
    assert child["parent_kind"] == "bot_message" and child["parent_job_id"] == parent["message_id"]
    assert child["root_ref"] == parent["root_ref"] and child["root_ref"]["actor_type"] == "user"
    assert child["source_room"]["room_id"] == personal[ben] and child["target_room"]["room_id"] == personal[cy]
    assert child["request_ref"]["actor_id"] == ben and child["result_ref"]["actor_id"] == cy
    assert child["return_ref"]["actor_id"] == ben
    assert parent["source_room"]["room_id"] == room and parent["target_room"]["room_id"] == personal[ben]
    assert parent["root_ref"]["room_id"] == room
    assert parent["request_ref"]["actor_id"] == ada and parent["delivery_ref"]["actor_type"] == "system"
    assert parent["result_ref"]["room_id"] == personal[ben] and parent["result_ref"]["actor_id"] == ben
    assert parent["resolved_result_ref"] == child["return_ref"]
    assert parent["result_ref"]["event_id"] != parent["resolved_result_ref"]["event_id"]
    assert parent["return_ref"]["room_id"] == room and parent["return_ref"]["actor_id"] == ada
    with client.app.state.session_factory() as db:
        result = db.get(RoomEvent, parent["resolved_result_ref"]["event_id"])
        original = db.get(RoomEvent, parent["result_ref"]["event_id"])
        delivery = db.get(RoomEvent, parent["return_delivery_ref"]["event_id"])
        assert result.payload["content"] == "BEN_RESOLVED_WITH_CY_REAL_REVIEW_48"
        assert original.payload["content"] == "Ben sent the subtask; result is pending."
        assert delivery.payload["result_ref"] == parent["result_ref"]
    assert len(client.get(f"/api/v1/conversations/{room}/members", headers=headers(client, owner)).json()) == 1
    assert client.get(f"/api/v1/bots/conversations/{room}/messages", headers=headers(client, other)).status_code == 404
    inspector = client.get(f"/api/v1/conversations/{room}/requests/{parent['root_request_event_id']}", headers=headers(client, owner))
    assert inspector.status_code == 200 and inspector.json()["completion"]["outcome_status"] == "succeeded", inspector.text
    assert inspector.json()["completion"]["pending_agent_count"] == 0
    assert any("ADA_FINAL_WITH_BEN_RESOLVED" in row["content"] for row in response.json())
    calls = iter(who for who, _, _ in seen)
    assert all(any(who == expected for who in calls) for expected in (ada, ben, cy, ben, ada))
    from app.services.bot_messages import effective_result, receipt
    from app.services.llm_service import LLMFailure
    with client.app.state.session_factory() as db:
        parent_row = db.get(BotMessage, parent["message_id"])
        child_row = db.get(BotMessage, child["message_id"])
        child_row.owner_id = other
        db.flush()
        with pytest.raises(LLMFailure):
            effective_result(db, parent_row)
        db.rollback()
    with client.app.state.session_factory() as db:
        parent_row = db.get(BotMessage, parent["message_id"])
        child_row = db.get(BotMessage, child["message_id"])
        for reply_state in ("failed", "cancelled", "pending"):
            child_row.reply_state = reply_state
            assert receipt(db, parent_row)["resolved_result_ref"] is None
        child_row.reply_state = "delivered"
        child_row.state = "running"
        assert receipt(db, parent_row)["resolved_result_ref"] is None
        child_row.state = "completed"
        child_event = db.get(RoomEvent, child_row.return_event_id)
        child_event.kind = "activity"
        assert receipt(db, parent_row)["resolved_result_ref"] is None
        db.rollback()


def test_sequential_nested_batches_resolve_to_the_last_actual_answer(room_client):
    from test_bot_batches import batch_call, batches
    client, (owner, _, project, _, _) = room_client
    room, (ada, ben, cy), personal = cross_team(client, owner, project, nested=True)
    dee, eli, fay = team(client, owner, room, ("Dee", "Eli", "Fay"))
    for agent in (dee, eli, fay):
        assert client.delete(f"/api/v1/conversations/{room}/members/{agent}", headers=headers(client, owner)).status_code == 204
        personal[agent] = client.post(f"/api/v1/bots/{agent}/conversation", headers=headers(client, owner), json={}).json()["room_id"]

    async def provider(request):
        body = request.system + json.dumps(request.events, ensure_ascii=False)
        if request.agent_id == ada:
            if "BEN_FINAL_SECOND_BATCH" in body:
                return ProviderResult(text="ADA_FINAL_SECOND_BATCH")
            return ProviderResult(text="Ada waiting.") if request.observations else call_bot(ben, "Get two rounds of work", "ada-ben")
        if request.agent_id == ben:
            if "ELI_SECOND" in body and "FAY_SECOND" in body:
                return ProviderResult(text="BEN_FINAL_SECOND_BATCH")
            if "CY_FIRST" in body and "DEE_FIRST" in body:
                return batch_call([(eli, "Second one"), (fay, "Second two")], "second-batch")
            return batch_call([(cy, "First one"), (dee, "First two")], "first-batch")
        return ProviderResult(text={cy: "CY_FIRST", dee: "DEE_FIRST", eli: "ELI_SECOND", fay: "FAY_SECOND"}[request.agent_id])

    client.app.state.scheduler.provider = provider
    response = client.post(f"/api/v1/conversations/{room}/messages", headers=headers(client, owner), json={"content": "@Ada complete two rounds of delegated work"})
    assert response.status_code == 201, response.text
    parent = next(m for m in list_messages(client, owner, room) if m["sender_agent_id"] == ada)
    groups = batches(client, owner, room)
    assert len(groups) == 2 and all(g["reply_state"] == "delivered" for g in groups), groups
    first = next(g for g in groups if g["return_ref"]["kind"] == "activity")
    second = next(g for g in groups if g["return_ref"]["kind"] == "message")
    assert first["parent_job_id"] == second["parent_job_id"] == parent["message_id"]
    assert parent["resolved_result_ref"] == second["return_ref"]
    from app.models.bot_batch import BotBatch
    from app.services.bot_messages import receipt
    with client.app.state.session_factory() as db:
        parent_row = db.get(BotMessage, parent["message_id"])
        first_event = db.get(RoomEvent, first["return_ref"]["event_id"])
        second_row = db.get(BotBatch, second["batch_id"])
        original_payload = first_event.payload
        first_event.payload = {**original_payload, "batch_waiting": [{"batch_id": "missing"}]}
        db.flush()
        assert receipt(db, parent_row)["resolved_result_ref"] is None
        first_event.payload = {**original_payload, "batch_waiting": [{"batch_id": first["batch_id"]}]}
        db.flush()
        assert receipt(db, parent_row)["resolved_result_ref"] is None
        first_event.payload = original_payload
        second_row.reply_state = "pending"
        db.flush()
        assert receipt(db, parent_row)["resolved_result_ref"] is None
        db.rollback()


def test_busy_target_is_not_preempted_and_runs_after_it_becomes_idle(room_client):
    client, (owner, _, project, _, _) = room_client
    room, (ada, ben), personal = cross_team(client, owner, project)
    with client.app.state.session_factory() as db:
        run = db.get(RoomAgentRun, (personal[ben], ben))
        run.state, run.trigger_event_id = "running", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        before_generation = run.generation
        db.commit()
    target_called = threading.Event()
    async def provider(request):
        if request.agent_id == ben:
            target_called.set()
            return ProviderResult(text="BEN_AFTER_USER_FINISHED")
        if "BEN_AFTER_USER_FINISHED" in json.dumps(request.events): return ProviderResult(text="ADA_FINISHED")
        return ProviderResult(text="Waiting for Ben.") if request.observations else call_bot(ben, "Handle after the current user turn")
    client.app.state.scheduler.provider = provider
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, f"/api/v1/conversations/{room}/messages", headers=headers(client, owner), json={"content": "@Ada ask Ben"})
        for _ in range(100):
            rows = list_messages(client, owner, room)
            if rows: break
            time.sleep(0.02)
        assert rows and rows[0]["state"] == "pending" and not target_called.is_set()
        with client.app.state.session_factory() as db:
            run = db.get(RoomAgentRun, (personal[ben], ben))
            assert run.state == "running" and run.generation == before_generation
            assert run.trigger_event_id == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            run.state = "idle"
            run.generation += 1
            db.commit()
        response = future.result(timeout=10)
    assert response.status_code == 201, response.text
    assert target_called.is_set()
    assert list_messages(client, owner, room)[0]["reply_state"] == "delivered"


def test_source_stop_cancels_foreign_model_turn_and_prevents_return(room_client):
    client, (owner, _, project, _, _) = room_client
    room, (ada, ben), personal = cross_team(client, owner, project)
    entered, cancelled = threading.Event(), threading.Event()
    returns = []
    async def provider(request):
        if request.agent_id == ben:
            entered.set()
            try: await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        if request.observations: return ProviderResult(text="Waiting for actual peer result.")
        if "peer_result" in json.dumps(request.events): returns.append(True)
        return call_bot(ben, "Stop must cancel this cross-room call")
    client.app.state.scheduler.provider = provider
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, f"/api/v1/conversations/{room}/messages", headers=headers(client, owner), json={"content": "@Ada ask Ben"})
        assert entered.wait(5)
        stopped = client.post(f"/api/v1/conversations/{room}/cancel", headers=headers(client, owner))
        assert stopped.status_code == 204, stopped.text
        future.result(timeout=10)
    assert cancelled.wait(2) and not returns
    row, = list_messages(client, owner, room)
    assert row["state"] == row["reply_state"] == "cancelled"
    assert row["result_ref"] is None and row["return_ref"] is None
    with client.app.state.session_factory() as db:
        assert db.get(RoomAgentRun, (personal[ben], ben)).state == "idle"
    preview = client.get(f"/api/v1/bots/messages/{row['message_id']}/retry", headers=headers(client, owner))
    assert preview.status_code == 200 and preview.json()["eligible"], preview.text
    assert preview.json()["root_ref"] == row["root_ref"]
    assert preview.json()["source_agent_id"] == ada
    from app.schemas.bot_message import BotMessageRetryWrite
    from app.services.bot_messages import retry
    from app.models.collaboration import RoomTrigger
    from fastapi import HTTPException
    import pytest
    dana, = team(client, owner, room, ("Dana",))
    with client.app.state.session_factory() as db:
        root_trigger = db.get(RoomTrigger, row["root_request_event_id"])
        root_trigger.selected_agents = [ada, dana]
        db.commit()
    plan = client.get(f"/api/v1/bots/messages/{row['message_id']}/retry", headers=headers(client, owner)).json()
    with client.app.state.session_factory() as db:
        value = BotMessageRetryWrite(expected_updated_at=plan["expected_updated_at"], plan_hash=plan["plan_hash"])
        run = db.get(RoomAgentRun, (room, ada))
        run.state = "running"
        db.commit()
        events_before = db.query(RoomEvent).filter_by(room_id=room).count()
        with pytest.raises(HTTPException) as changed:
            retry(db, owner, row["message_id"], value, "explicit-cross-room-retry")
        assert changed.value.status_code == 409 and changed.value.detail["code"] == "bot_message_retry_changed"
        assert db.query(RoomEvent).filter_by(room_id=room).count() == events_before
        run = db.get(RoomAgentRun, (room, ada))
        run.state = "idle"
        db.commit()
        first = retry(db, owner, row["message_id"], value, "explicit-cross-room-retry")
        second = retry(db, owner, row["message_id"], value, "explicit-cross-room-retry")
        assert not first["replayed"] and second == first | {"replayed": True}
        assert first["root_room_id"] == room and first["source_agent_id"] == ada
        event = db.get(RoomEvent, first["retry_event_id"])
        assert event.room_id == room and event.actor_type == "user" and event.actor_id == owner
        assert event.payload["bot_message_retry_of"]["original_root_ref"] == row["root_ref"]
        new_trigger = db.get(RoomTrigger, first["retry_event_id"])
        assert new_trigger.state == "pending" and new_trigger.selected_agents == [ada]
        assert first["plan_hash"] == event.payload["bot_message_retry_of"]["plan_hash"] == value.plan_hash
        assert db.get(BotMessage, row["message_id"]).state == "cancelled"


def test_actual_migration33_adds_mailbox_without_changing_schema32_rows(tmp_path, monkeypatch):
    from app.core import migrate as migration
    from test_migrations import legacy_db
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    path = tmp_path / "synthetic32.sqlite"
    legacy_db(path)
    engine = create_engine(f"sqlite:///{path}")
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:32])
            migration.migrate_database(engine)
        with sqlite3.connect(path) as db:
            tables = [name for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            before = {t: (db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()[0],
                db.execute(f'SELECT * FROM "{t}" ORDER BY rowid').fetchall()) for t in tables}
        with monkeypatch.context() as scoped:
            scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:33])
            migration.migrate_database(engine)
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT max(version) FROM schema_version").fetchone() == (33,)
            assert db.execute("SELECT count(*) FROM bot_messages").fetchone() == (0,)
            assert db.execute("SELECT count(*) FROM bot_message_retries").fetchone() == (0,)
            for t, (ddl, rows) in before.items():
                assert db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()[0] == ddl
                if t != "schema_version": assert db.execute(f'SELECT * FROM "{t}" ORDER BY rowid').fetchall() == rows
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        with monkeypatch.context() as scoped:
            scoped.setattr(migration, "REVISIONS", migration.REVISIONS[:33])
            migration.migrate_database(engine)
    finally:
        engine.dispose()
