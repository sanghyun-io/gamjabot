"""Provider-free context preview uses the dispatch projection without side effects."""

import asyncio

from app.models.collaboration import RoomAgentRun, RoomDraft, RoomEvent, RoomReadState, RoomTrigger
from app.models.models import Agent
from app.services.context import append_room_summary, provider_text_size
from app.services.events import ActorRef, append_event
from app.services.providers import ProviderResult
from app.services.providers.base import attributed_messages
from test_room_state import create_room, headers, room_client
from test_scheduler import enable_tools, ready_agents


def preview(client, owner, room, content):
    return client.post(f"/api/v1/conversations/{room}/context-preview",
                       json={"content": content}, headers=headers(client, owner))


def test_preview_uses_canonical_quote_safe_recipients_and_has_no_side_effects(room_client, monkeypatch):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "preview")
    ada, ben = ready_agents(client, owner, room, ("Ada", "Ben"))
    with client.app.state.session_factory() as db:
        append_event(db, room, ActorRef("user", owner), "message", {"content": "history"})
        before = {
            "events": db.query(RoomEvent).filter_by(room_id=room).count(),
            "triggers": db.query(RoomTrigger).filter_by(room_id=room).count(),
            "drafts": db.query(RoomDraft).filter_by(room_id=room).count(),
            "reads": db.query(RoomReadState).filter_by(room_id=room).count(),
        }

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("preview resolved a credential")
    monkeypatch.setattr("app.services.context.resolve_connection_async", forbidden)
    response = preview(client, owner, room, "> @Ben said this\n\n@Ada answer exactly")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["selected_agent_ids"] == [ada]
    assert [item["agent_id"] for item in data["agents"]] == [ada]
    assert data["projected_seq"] == 2
    agent = data["agents"][0]
    assert agent["projection_status"] == "ready" and agent["model_id"] == "exact"
    assert any("@Ada answer exactly" in message["content"] for message in agent["messages"])
    assert agent["source_coverage"]["committed_high_water"] == 1
    assert agent["source_coverage"]["draft_included"] is True
    assert agent["budget"]["unit"] == "characters"
    assert agent["budget"]["used_chars"] <= agent["budget"]["limit_chars"] == 32000
    assert len(data["fingerprint"]) == 64
    rendered = str(data)
    for private in ("127.0.0.1", "capabilities", "secret_ref", "api_key", "arguments"):
        assert private not in rendered
    with client.app.state.session_factory() as db:
        after = {
            "events": db.query(RoomEvent).filter_by(room_id=room).count(),
            "triggers": db.query(RoomTrigger).filter_by(room_id=room).count(),
            "drafts": db.query(RoomDraft).filter_by(room_id=room).count(),
            "reads": db.query(RoomReadState).filter_by(room_id=room).count(),
        }
    assert after == before
    assert ben not in data["selected_agent_ids"]


def test_preview_messages_and_character_budget_match_real_dispatch_projection(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "parity")
    (ada,) = ready_agents(client, owner, room, ("Ada",))
    enable_tools(client, ada)
    draft = "Please compare this @Ada"
    projected = preview(client, owner, room, draft)
    assert projected.status_code == 200, projected.text
    expected = projected.json()["agents"][0]
    captured = []

    async def provider(request):
        captured.append(request)
        return ProviderResult(text="done")

    client.app.state.scheduler.provider = provider
    sent = client.post(f"/api/v1/conversations/{room}/messages", json={"content": draft},
                       headers=headers(client, owner))
    assert sent.status_code == 201, sent.text
    assert len(captured) == 1
    actual = captured[0]
    assert expected["system_text"] == actual.system
    assert expected["messages"] == attributed_messages(actual)
    assert expected["budget"]["used_chars"] == provider_text_size(actual)
    assert actual.trigger_event_id and actual.watermark_seq == projected.json()["projected_seq"]


def test_preview_is_pure_across_extracts_handoff_truncation_and_safe_nontext(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "bounded")
    (ada,) = ready_agents(client, owner, room, ("Ada",))
    with client.app.state.session_factory() as db:
        for number in range(3):
            append_event(db, room, ActorRef("user", owner), "message", {"content": f"opening-{number}"})
        append_room_summary(db, room, 1, 3, "OWNER_HANDOFF")
        for number in range(21):
            content = "LONG_MARKER" + "x" * 5000 if number == 20 else f"later-{number}"
            append_event(db, room, ActorRef("user", owner), "message", {"content": content})
        append_event(db, room, ActorRef("tool", None), "tool_result", {
            "state": "completed", "text": "screen captured",
            "screenshot_ref": f"rooms/{room}/screenshots/11111111-1111-1111-1111-111111111111.png",
            "image_mime": "image/png", "image_width": 1200, "image_height": 800,
        })
        before_summaries = db.query(RoomEvent).filter_by(room_id=room, kind="summary").count()
        before_checkpoint = dict(db.get(RoomAgentRun, (room, ada)).checkpoint or {})

    response = preview(client, owner, room, "new request @Ada")
    assert response.status_code == 200, response.text
    agent = response.json()["agents"][0]
    ranges = agent["source_coverage"]["summarized_ranges"]
    assert {item["kind"] for item in ranges} == {"extractive_checkpoint", "owner_checkpoint"}
    assert agent["truncations"] and agent["truncations"][0]["omitted_chars"] > 0
    assert agent["non_text"] == [{
        "event_id": agent["non_text"][0]["event_id"], "seq": 26,
        "kind": "screenshot_reference", "supplied": False,
        "image_mime": "image/png", "width": 1200, "height": 800,
    }]
    rendered = str(response.json())
    assert f"rooms/{room}/screenshots/11111111-1111-1111-1111-111111111111.png" in rendered
    assert "image content was not supplied" in rendered
    with client.app.state.session_factory() as db:
        assert db.query(RoomEvent).filter_by(room_id=room, kind="summary").count() == before_summaries
        assert dict(db.get(RoomAgentRun, (room, ada)).checkpoint or {}) == before_checkpoint

    captured = []
    async def provider(request):
        captured.append(request)
        return ProviderResult(text="bounded done")
    client.app.state.scheduler.provider = provider
    sent = client.post(f"/api/v1/conversations/{room}/messages", json={"content": "new request @Ada"},
                       headers=headers(client, owner))
    assert sent.status_code == 201, sent.text
    assert agent["messages"] == attributed_messages(captured[0])
    with client.app.state.session_factory() as db:
        assert db.query(RoomEvent).filter_by(room_id=room, kind="summary").count() == before_summaries + 1
        assert db.get(RoomAgentRun, (room, ada)).checkpoint["context_summary"]["source_end_seq"] == 7


def test_preview_fingerprint_tracks_recipients_public_model_and_tools(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "fingerprint")
    ada, ben = ready_agents(client, owner, room, ("Ada", "Ben"))
    first = preview(client, owner, room, "@Ada inspect").json()
    broadcast = preview(client, owner, room, "inspect together").json()
    assert first["fingerprint"] != broadcast["fingerprint"]
    assert first["selected_agent_ids"] == [ada]
    assert broadcast["selected_agent_ids"] == [ada, ben]

    with client.app.state.session_factory() as db:
        db.get(Agent, ada).model_id = "changed-model"
        db.commit()
    model_changed = preview(client, owner, room, "@Ada inspect").json()
    assert model_changed["fingerprint"] != first["fingerprint"]
    assert model_changed["agents"][0]["model_id"] == "changed-model"

    with client.app.state.session_factory() as db:
        append_event(db, room, ActorRef("user", owner), "message", {"content": "new committed source"})
    history_changed = preview(client, owner, room, "@Ada inspect").json()
    assert history_changed["fingerprint"] != model_changed["fingerprint"]

    broker = client.app.state.scheduler.broker
    original = list(broker.tool_schemas)
    broker.tool_schemas = [*original, {"name": "synthetic_preview_tool", "parameters": {"type": "object"}}]
    try:
        tools_changed = preview(client, owner, room, "@Ada inspect").json()
    finally:
        broker.tool_schemas = original
    assert tools_changed["fingerprint"] != history_changed["fingerprint"]


def test_zero_tool_policy_removes_broker_schemas_from_preview(room_client):
    client, (owner, _, project, _, _) = room_client
    room = create_room(client, owner, project, "zero-tools")
    ready_agents(client, owner, room, ("Ada",))
    schedule = {"max_rounds": 2, "max_tool_calls": 0, "max_executor_seconds": 60}
    assert client.put(f"/api/v1/conversations/{room}/schedule", json=schedule,
                      headers=headers(client, owner)).status_code == 200
    broker = client.app.state.scheduler.broker
    original = list(broker.tool_schemas)
    broker.tool_schemas = [{"name": "first", "parameters": {"type": "object"}}]
    try:
        first = preview(client, owner, room, "@Ada inspect").json()
        broker.tool_schemas = [{"name": "different", "parameters": {"type": "object"}}]
        second = preview(client, owner, room, "@Ada inspect").json()
    finally:
        broker.tool_schemas = original
    assert first["fingerprint"] == second["fingerprint"]
    assert first["agents"][0]["budget"] == second["agents"][0]["budget"]


def test_preview_owner_closed_validation_and_mixed_unavailable_agents(room_client):
    client, (owner, other, project, _, _) = room_client
    room = create_room(client, owner, project, "validation")
    (ada,) = ready_agents(client, owner, room, ("Ada",))
    with client.app.state.session_factory() as db:
        missing = Agent(user_id=owner, name="Missing", description="d", system_prompt="private",
                        connection_id=None, model_id=None, is_active=True)
        db.add(missing)
        db.commit()
        missing_id = missing.id
    assert client.post(f"/api/v1/conversations/{room}/members/{missing_id}",
                       headers=headers(client, owner)).status_code == 201

    response = preview(client, owner, room, "everyone")
    assert response.status_code == 200
    by_id = {item["agent_id"]: item for item in response.json()["agents"]}
    assert by_id[ada]["projection_status"] == "ready"
    assert by_id[missing_id]["error_code"] == "model_binding_missing"
    assert by_id[missing_id]["messages"] == [] and by_id[missing_id]["system_text"] == ""
    assert preview(client, other, room, "private").status_code == 404
    assert preview(client, owner, room, " ").status_code == 422
    oversized = preview(client, owner, room, "x" * 12001)
    assert oversized.status_code == 200
    assert {item["error_code"] for item in oversized.json()["agents"]} == {
        "context_input_too_large", "model_binding_missing",
    }

    assert client.delete(f"/api/v1/conversations/{room}", headers=headers(client, owner)).status_code == 204
    assert preview(client, owner, room, "closed").status_code == 404
