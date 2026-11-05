"""Mixed-provider room collaboration uses real adapters over a fake transport."""

import json

import httpx

from app.models.collaboration import ProviderConnection, RoomEvent
from app.models.models import Agent
from test_room_state import create_room, headers, room_client


def test_mixed_vendor_agents_share_peer_messages_without_cross_room_context(room_client):
    """Fix same-Agent A/B isolation and two-round peer context in room C together."""
    client, (owner, _, project, reusable_agent, _) = room_client
    room_a = create_room(client, owner, project, "Isolated A")
    room_b = create_room(client, owner, project, "Isolated B")
    room_c = create_room(client, owner, project, "Mixed peers C")

    with client.app.state.session_factory() as db:
        openai = ProviderConnection(
            user_id=owner,
            kind="openai",
            name="OpenAI without credentials",
            default_model_id="openai-test-model",
            auth_required=False,
            enabled=True,
            capabilities={"declared": {}, "verified": {}},
        )
        anthropic = ProviderConnection(
            user_id=owner,
            kind="anthropic",
            name="Anthropic without credentials",
            default_model_id="anthropic-test-model",
            auth_required=False,
            enabled=True,
            capabilities={"declared": {}, "verified": {}},
        )
        db.add_all([openai, anthropic])
        db.flush()
        ada = db.get(Agent, reusable_agent)
        ada.name = "Ada"
        ada.system_prompt = "OpenAI agent"
        ada.connection_id = openai.id
        ada.model_id = "openai-test-model"
        ada.temperature = "auto"
        ada.max_tokens = 0
        claude = Agent(
            user_id=owner,
            name="Claude",
            description="Anthropic collaborator",
            system_prompt="Anthropic agent",
            connection_id=anthropic.id,
            model_id="anthropic-test-model",
            temperature="auto",
            max_tokens=0,
        )
        db.add(claude)
        db.commit()
        claude_id, openai_id, anthropic_id = claude.id, openai.id, anthropic.id

    own = headers(client, owner)
    for room_id, agent_id in (
        (room_a, reusable_agent),
        (room_b, reusable_agent),
        (room_c, reusable_agent),
        (room_c, claude_id),
    ):
        joined = client.post(f"/api/v1/conversations/{room_id}/members/{agent_id}", headers=own)
        assert joined.status_code == 201, joined.text

    requests = []

    def provider_transport(request):
        body = json.loads(request.content)
        serialized = json.dumps(body)
        requests.append((request.url.host, serialized))

        if "ROOM_A_ONLY_MARKER" in serialized:
            assert request.url.host == "api.openai.com"
            assert "ROOM_B_ONLY_MARKER" not in serialized
            assert "ROOM_C_PEER_MARKER" not in serialized
            return httpx.Response(200, json={"choices": [{"message": {"content": "A-only reply"}}]})

        if "ROOM_B_ONLY_MARKER" in serialized:
            assert request.url.host == "api.openai.com"
            assert "ROOM_A_ONLY_MARKER" not in serialized
            assert "ROOM_C_PEER_MARKER" not in serialized
            return httpx.Response(200, json={"choices": [{"message": {"content": "B-only reply"}}]})

        assert "ROOM_C_PEER_MARKER" in serialized
        assert "ROOM_A_ONLY_MARKER" not in serialized
        assert "ROOM_B_ONLY_MARKER" not in serialized
        if request.url.host == "api.openai.com":
            content = "Ada saw Claude" if "Claude-first" in serialized else "Ada-first"
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        if request.url.host == "api.anthropic.com":
            content = "Claude saw Ada" if "Ada-first" in serialized else "Claude-first"
            return httpx.Response(200, json={"content": [{"type": "text", "text": content}]})
        raise AssertionError(f"Unexpected provider host: {request.url.host}")

    client.app.state.connection_transport = httpx.MockTransport(provider_transport)

    isolated_a = client.post(f"/api/v1/conversations/{room_a}/messages",
                             json={"content": "ROOM_A_ONLY_MARKER"}, headers=own)
    isolated_b = client.post(f"/api/v1/conversations/{room_b}/messages",
                             json={"content": "ROOM_B_ONLY_MARKER"}, headers=own)
    assert isolated_a.status_code == isolated_b.status_code == 201
    assert [message["content"] for message in isolated_a.json()] == ["A-only reply"]
    assert [message["content"] for message in isolated_b.json()] == ["B-only reply"]

    collaborated = client.post(f"/api/v1/conversations/{room_c}/messages",
        json={"content": "ROOM_C_PEER_MARKER"}, headers=own)
    assert collaborated.status_code == 201, collaborated.text
    assert {message["content"] for message in collaborated.json()} == {
        "Ada-first",
        "Claude-first",
        "Ada saw Claude",
        "Claude saw Ada",
    }

    with client.app.state.session_factory() as db:
        events = {room_id: db.query(RoomEvent).filter_by(room_id=room_id).order_by(
            RoomEvent.seq).all() for room_id in (room_a, room_b, room_c)}

    assert all("ROOM_B_ONLY_MARKER" not in str(event.payload)
               and "ROOM_C_PEER_MARKER" not in str(event.payload) for event in events[room_a])
    assert all("ROOM_A_ONLY_MARKER" not in str(event.payload)
               and "ROOM_C_PEER_MARKER" not in str(event.payload) for event in events[room_b])
    assert all("ROOM_A_ONLY_MARKER" not in str(event.payload)
               and "ROOM_B_ONLY_MARKER" not in str(event.payload) for event in events[room_c])

    a_model_use = [event.payload["model_use"] for event in events[room_a]
                   if event.actor_type == "agent" and event.kind == "message"]
    b_model_use = [event.payload["model_use"] for event in events[room_b]
                   if event.actor_type == "agent" and event.kind == "message"]
    c_model_use = [event.payload["model_use"] for event in events[room_c]
                   if event.actor_type == "agent" and event.kind == "message"]
    assert {(item["connection_id"], item["model_id"], item["provider_kind"])
            for item in a_model_use + b_model_use} == {
                (openai_id, "openai-test-model", "openai")}
    assert {(item["connection_id"], item["model_id"], item["provider_kind"])
            for item in c_model_use} == {
                (openai_id, "openai-test-model", "openai"),
                (anthropic_id, "anthropic-test-model", "anthropic"),
            }
    assert len(a_model_use) == len(b_model_use) == 1
    assert len(c_model_use) == 4
    assert [host for host, _ in requests].count("api.openai.com") == 4
    assert [host for host, _ in requests].count("api.anthropic.com") == 2
