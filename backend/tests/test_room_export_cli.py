import json
import stat
from urllib.parse import parse_qs, urlsplit

import pytest

from app import owner_cli


def _event(seq, *, actor_type="user", actor="Owner", kind="message", payload=None):
    return {
        "event_id": f"private-{seq}",
        "room_id": 7,
        "seq": seq,
        "actor_type": actor_type,
        "actor_id": seq,
        "actor_name_snapshot": actor,
        "kind": kind,
        "payload": payload if payload is not None else {"content": f"message {seq}"},
        "created_at": f"2026-10-03T10:{seq % 60:02d}:00Z",
    }


class EventClient:
    token = "memory-only"

    def __init__(self, events, failure_after=None):
        self.events = events
        self.failure_after = failure_after
        self.calls = []

    def call(self, method, path, body=None, *, authenticated=False, **_kwargs):
        self.calls.append((method, path, body, authenticated))
        query = parse_qs(urlsplit(path).query)
        cursor = int(query.get("after_seq", ["0"])[0])
        limit = int(query.get("limit", ["100"])[0])
        if self.failure_after is not None and cursor >= self.failure_after:
            raise RuntimeError("synthetic pagination failure")
        return [event for event in self.events if event["seq"] > cursor][:limit]


def test_room_export_paginates_past_100_and_excludes_technical_payloads(capsys):
    events = [_event(seq) for seq in range(1, 106)]
    events[1] = _event(2, actor_type="tool", actor="Tool", kind="tool_result", payload={
        "text": "private tool output", "arguments": {"api_key": "never-export"}})
    events[2] = _event(3, actor_type="system", actor="System", kind="failure", payload={
        "message": "private diagnostic", "diagnostic": {"correlation_id": "never-export"}})
    events[3] = _event(4, actor_type="agent", actor="연구원", payload={
        "content": "유니코드 그대로 ✅\n`technical-looking user text`",
        "attachments": [{"ref": "private/binary.png", "bytes": "never-export"}],
    })
    client = EventClient(events)

    owner_cli.command(client, ["room-export", "7"])

    output = capsys.readouterr().out
    assert "유니코드 그대로 ✅\n`technical-looking user text`" in output
    assert "연구원" in output and "Seq 4" in output and "2026-10-03T10:04:00Z" in output
    assert "[Attachment omitted]" in output
    assert "message 105" in output
    assert all(secret not in output for secret in (
        "private tool output", "never-export", "private diagnostic", "private-105"))
    assert [urlsplit(path).query for _, path, _, _ in client.calls] == [
        "after_seq=0&limit=100",
        "after_seq=100&limit=100",
        "after_seq=105&limit=100",
    ]
    assert all(authenticated for _, _, _, authenticated in client.calls)


def test_room_export_collects_every_page_before_stdout_or_file(tmp_path, capsys):
    target = tmp_path / "room.md"
    client = EventClient([_event(seq) for seq in range(1, 102)], failure_after=100)

    with pytest.raises(RuntimeError, match="synthetic pagination failure"):
        owner_cli.command(client, ["room-export", "7", "--output", str(target)])

    assert capsys.readouterr().out == ""
    assert not target.exists()


@pytest.mark.parametrize("pages", [
    [[_event(1)], [_event(1)]],
    [[_event(2), _event(1)]],
])
def test_room_export_rejects_non_progressing_or_unordered_pages(pages, capsys):
    class ScriptedClient:
        token = "memory-only"

        def __init__(self):
            self.index = 0

        def call(self, *_args, **_kwargs):
            page = pages[min(self.index, len(pages) - 1)]
            self.index += 1
            return page

    with pytest.raises(RuntimeError, match="invalid event page"):
        owner_cli.command(ScriptedClient(), ["room-export", "7"])
    assert capsys.readouterr().out == ""


def test_room_export_writes_private_atomic_file_and_requires_force(tmp_path, capsys):
    target = tmp_path / "room.md"
    owner_cli.command(EventClient([_event(1)]), ["room-export", "7", "--output", str(target)])

    assert capsys.readouterr().out == ""
    assert target.read_text(encoding="utf-8").startswith("# Gamja Bot room 7 transcript")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [target]

    original = target.read_text(encoding="utf-8")
    with pytest.raises(RuntimeError, match="already exists"):
        owner_cli.command(EventClient([_event(2)]), ["room-export", "7", "--output", str(target)])
    assert target.read_text(encoding="utf-8") == original

    owner_cli.command(EventClient([_event(2)]), [
        "room-export", "7", "--output", str(target), "--force",
    ])
    assert "message 2" in target.read_text(encoding="utf-8")
    assert "message 1" not in target.read_text(encoding="utf-8")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_room_export_rejects_malformed_authored_event_without_partial_output(capsys):
    malformed = _event(1, payload={"content": 42})
    with pytest.raises(RuntimeError, match="invalid authored event"):
        owner_cli.command(EventClient([malformed]), ["room-export", "7"])
    assert capsys.readouterr().out == ""
