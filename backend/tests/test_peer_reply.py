"""Disposable canonical rooms: explicit declarations cannot invent provenance."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from test_room_state import room_client, headers, create_room
from test_scheduler import ready_agents
from app.models.collaboration import RoomEvent
from app.services.events import ActorRef, append_event
from app.services.peer_reply import parse_reply_footer, peer_inputs, validated_reply
from app.services.providers import ProviderResult


def footer(ids, answer="Visible answer"):
    return answer + '\n<gamja_reply_to>' + json.dumps({"event_ids": ids}) + '</gamja_reply_to>'


def projected(event):
    return {key: getattr(event, key) for key in (
        "event_id", "room_id", "seq", "actor_type", "actor_id", "actor_name_snapshot", "kind", "payload")}


def test_metadata_only_suffix_is_removed_and_never_inferred():
    event_id = str(uuid4())
    assert parse_reply_footer(footer([event_id])) == ("Visible answer", [event_id], "declared")
    assert parse_reply_footer("Peer Alice said something") == ("Peer Alice said something", [], "absent")
    assert parse_reply_footer(footer([])) == ("Visible answer", [], "declared")
    code = '```xml\n<gamja_reply_to>{"event_ids":[]}</gamja_reply_to>\n```'
    assert parse_reply_footer(code) == (code, [], "absent")
    thinking = '<think>private\n' + footer([event_id])
    assert parse_reply_footer(thinking) == (thinking, [], "absent")
    closed = '<think>private</think>\n' + footer([event_id])
    assert parse_reply_footer(closed) == ('<think>private</think>\nVisible answer', [event_id], "declared")


@pytest.mark.parametrize("metadata", [
    '{bad', '{"event_ids":[]}',  # missing closing footer tested below
    '{"event_ids":[true]}', '{"event_ids":["not-a-uuid"]}',
    '{"event_ids":[],"room_id":999}', '{"event_ids":[],"event_ids":[]}',
    json.dumps({"event_ids": [str(uuid4())] * 2}),
    json.dumps({"event_ids": [str(uuid4()) for _ in range(9)]}),
])
def test_malformed_or_oversized_metadata_does_not_leak_or_create_cards(metadata):
    suffix = '' if metadata in ['{bad', '{"event_ids":[]}'] else '</gamja_reply_to>'
    assert parse_reply_footer('Visible\n<gamja_reply_to>' + metadata + suffix) == ('Visible', [], 'invalid')


def test_non_peer_inputs_and_duplicate_ids_are_not_eligible():
    good = {"event_id": str(uuid4()), "room_id": 1, "seq": 2, "kind": "message",
            "actor_type": "agent", "actor_id": 8, "payload": {"content": "peer"}}
    assert peer_inputs([good], 1, 7, 2) == [good]
    for change in [{"room_id": 2}, {"seq": 3}, {"actor_id": 7}, {"actor_type": "user"},
                   {"kind": "summary"}, {"event_id": "extractive-context:2"},
                   {"payload": {"content": ""}}, {"seq": True}]:
        assert peer_inputs([{**good, **change}], 1, 7, 2) == []
    assert peer_inputs([good, good], 1, 7, 2) == []


def test_actual_projection_owner_room_watermark_and_source_integrity(room_client):
    client, (owner, _, project, _, foreign_agent) = room_client
    room = create_room(client, owner, project, "reply source")
    other_room = create_room(client, owner, project, "not supplied")
    author, responder = ready_agents(client, owner, room, ("Same name", "Same name"))
    foreign_author = ready_agents(client, owner, other_room, ("Private",))[0]
    with client.app.state.session_factory() as db:
        source = append_event(db, room, ActorRef("agent", author), "message", {"content": "x" * 4100})
        unsupplied = append_event(db, room, ActorRef("agent", author), "message", {"content": "not supplied"})
        future = append_event(db, room, ActorRef("agent", author), "message", {"content": "future"})
        other = append_event(db, other_room, ActorRef("agent", foreign_author), "message", {"content": "OTHER_ROOM_PRIVATE"})
        from app.services.context_projection import _bounded_event
        bounded, _ = _bounded_event(projected(source), None)
        request = SimpleNamespace(room_id=room, agent_id=responder, watermark_seq=2, events=[bounded])
        answer, proof = validated_reply(db, request, footer([source.event_id]))
        assert answer == "Visible answer"
        assert proof["reply_to_event_ids"] == proof["provided_peer_event_ids"] == [source.event_id]
        for invalid in (unsupplied.event_id, future.event_id, other.event_id, str(uuid4())):
            _, rejected = validated_reply(db, request, footer([source.event_id, invalid]))
            assert rejected["declaration_state"] == "invalid"
            assert rejected["reply_to_event_ids"] == []
            assert "OTHER_ROOM_PRIVATE" not in str(rejected) and other.event_id not in str(rejected)
        # An injected input with a real UUID but altered content is not a delivered canonical input.
        request.events = [{**bounded, "payload": {"content": "spoof"}}]
        assert validated_reply(db, request, footer([source.event_id]))[1]["provided_peer_event_ids"] == []
        request.events = [bounded]
        request.agent_id = foreign_agent
        assert validated_reply(db, request, footer([source.event_id]))[1]["reply_to_event_ids"] == []


def test_scheduler_api_and_cli_share_stable_validated_reply_payload(room_client, capsys):
    client, (owner, other, project, _, _) = room_client
    room = create_room(client, owner, project, "reply integration")
    ready_agents(client, owner, room, ("Ada", "Ben"))
    async def provider(request):
        peers = peer_inputs(request.events, room, request.agent_id, request.watermark_seq)
        from app.services.providers.base import attributed_messages
        assert ('<gamja_reply_to>' in json.dumps(attributed_messages(request))) is bool(peers)
        return ProviderResult(text=footer([event["event_id"] for event in peers]))
    client.app.state.scheduler.provider = provider
    response = client.post(f"/api/v1/conversations/{room}/messages", json={"content": "Discuss"}, headers=headers(client, owner))
    assert response.status_code == 201
    path = f"/api/v1/conversations/{room}/events"
    actual = client.get(path, headers=headers(client, owner)).json()
    replies = [event for event in actual if event["actor_type"] == "agent"]
    assert len(replies) == 4
    assert all(event["payload"]["content"] == "Visible answer" for event in replies)
    assert any(event["payload"]["reply_provenance"]["reply_to_event_ids"] for event in replies)
    assert all(event["payload"]["reply_provenance"]["version"] == 1 for event in replies)
    assert client.get(path, headers=headers(client, other)).status_code == 404
    assert client.get(path, headers=headers(client, owner)).json() == actual
    # Existing CLI consumes the exact authenticated event contract; no parallel inferred links.
    from app.owner_cli import command
    class CLI:
        def call(self, method, route, body=None, **kwargs):
            assert method == "GET" and route == f"/conversations/{room}/events"
            assert kwargs["authenticated"] is True
            return client.get('/api/v1' + route, headers=headers(client, owner)).json()
    command(CLI(), ['events', str(room)])
    assert json.loads(capsys.readouterr().out) == actual
