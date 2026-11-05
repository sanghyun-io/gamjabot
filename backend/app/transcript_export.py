"""Safe Markdown export of authored room messages."""

import os
import re
from pathlib import Path
from uuid import uuid4


PAGE_SIZE = 100
MAX_PAGES = 100_000
ATTACHMENT_KEYS = frozenset({"attachment", "attachments", "attachment_ref", "media", "screenshot_ref"})


def collect_room_events(client, room_id: str) -> list[dict]:
    """Collect and validate complete canonical pages before producing output."""
    cursor = 0
    events = []
    for _ in range(MAX_PAGES):
        page = client.call(
            "GET",
            f"/conversations/{room_id}/events?after_seq={cursor}&limit={PAGE_SIZE}",
            authenticated=True,
        )
        if not isinstance(page, list):
            raise RuntimeError("Running backend returned an invalid event page")
        if not page:
            return events
        sequences = []
        for event in page:
            sequence = event.get("seq") if isinstance(event, dict) else None
            if type(sequence) is not int or sequence <= cursor:
                raise RuntimeError("Running backend returned an invalid event page")
            sequences.append(sequence)
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise RuntimeError("Running backend returned an invalid event page")
        events.extend(page)
        cursor = sequences[-1]
    raise RuntimeError("Running backend returned too many event pages")


def _heading_text(value: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", value).replace("|", "\\|").strip()


def _fenced(content: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", content)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{content}\n{fence}"


def render_room_markdown(room_id: str, events: list[dict]) -> str:
    sections = [f"# Gamja Bot room {room_id} transcript", "", "Authored user and agent messages only."]
    for event in events:
        if event.get("kind") != "message" or event.get("actor_type") not in {"user", "agent"}:
            continue
        payload = event.get("payload")
        actor = event.get("actor_name_snapshot")
        created = event.get("created_at")
        sequence = event.get("seq")
        content = payload.get("content") if isinstance(payload, dict) else None
        if (type(sequence) is not int or not isinstance(actor, str) or not actor.strip()
                or not isinstance(created, str) or not created.strip() or not isinstance(content, str)):
            raise RuntimeError("Running backend returned an invalid authored event")
        sections.extend([
            "",
            f"## Seq {sequence} · {_heading_text(actor)} ({event['actor_type']}) · {_heading_text(created)}",
            "",
            _fenced(content),
        ])
        if any(payload.get(key) for key in ATTACHMENT_KEYS):
            sections.extend(["", "[Attachment omitted]"])
    return "\n".join(sections) + "\n"


def write_private_atomic(path: str, content: str, *, force: bool = False) -> None:
    target = Path(path)
    if not target.name:
        raise RuntimeError("Invalid export output path")
    if os.path.lexists(target) and not force:
        raise RuntimeError("Export output already exists; use --force to replace it")
    temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as destination:
            descriptor = None
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(temporary, 0o600)
        if force:
            os.replace(temporary, target)
        else:
            try:
                os.link(temporary, target)
            except FileExistsError:
                raise RuntimeError("Export output already exists; use --force to replace it") from None
            os.unlink(temporary)
        os.chmod(target, 0o600)
    except RuntimeError:
        raise
    except OSError:
        raise RuntimeError("Cannot write export output") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def export_room(client, room_id: str, *, output: str | None = None, force: bool = False) -> str | None:
    markdown = render_room_markdown(room_id, collect_room_events(client, room_id))
    if output is None:
        return markdown
    write_private_atomic(output, markdown, force=force)
    return None
