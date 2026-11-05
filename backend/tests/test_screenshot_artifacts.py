"""Owner-only screenshot retrieval and explicit deletion use synthetic PNGs."""

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.models.collaboration import RoomEvent, ToolAction
from app.api.routes import tools as tools_routes
from app.services.tool_broker import record_action_result
from test_room_state import create_room, headers, room_client


PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"synthetic-fixture")


def stored_screenshot(client, room: int, agent: int) -> tuple[str, Path]:
    action_id = str(uuid4())
    reference = f"rooms/{room}/screenshots/{action_id}.png"
    with client.app.state.session_factory() as db:
        db.add(ToolAction(action_id=action_id, room_id=room, agent_id=agent, family="screenshot",
                          arguments={}, state="started", call_id="fixture-call",
                          requested_at=datetime.now(timezone.utc)))
        db.commit()
        record_action_result(db, action_id, "completed", "Synthetic screen captured.",
                             screenshot_ref=reference, image_mime="image/png",
                             image_width=1, image_height=1)
    path = client.app.state.local_executor.desktop.app_data / reference
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for directory in (path.parents[2], path.parents[1], path.parent):
        directory.chmod(0o700)
    path.write_bytes(PNG)
    path.chmod(0o600)
    return action_id, path


def test_owner_reads_completed_screenshot_after_room_close(room_client):
    client, (owner, _, project, agent, _) = room_client
    room = create_room(client, owner, project, "screenshots")
    action_id, _ = stored_screenshot(client, room, agent)
    url = f"/api/v1/tools/rooms/{room}/actions/{action_id}/screenshot"

    response = client.get(url, headers=headers(client, owner))
    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"

    assert client.delete(f"/api/v1/conversations/{room}", headers=headers(client, owner)).status_code == 204
    assert client.get(url, headers=headers(client, owner)).content == PNG


def test_screenshot_route_hides_auth_and_room_action_mismatches(room_client):
    client, (owner, other, project, agent, _) = room_client
    room = create_room(client, owner, project, "screenshots")
    action_id, _ = stored_screenshot(client, room, agent)
    url = f"/api/v1/tools/rooms/{room}/actions/{action_id}/screenshot"

    assert client.get(url).status_code == 401
    assert client.get(url, headers=headers(client, other)).status_code == 404
    assert client.get(f"/api/v1/tools/rooms/{room + 999}/actions/{action_id}/screenshot",
                      headers=headers(client, owner)).status_code == 404
    assert client.get(f"/api/v1/tools/rooms/{room}/actions/{uuid4()}/screenshot",
                      headers=headers(client, owner)).status_code == 404

    with client.app.state.session_factory() as db:
        action = db.get(ToolAction, action_id)
        action.observation = {**action.observation, "screenshot_ref": "rooms/999/screenshots/wrong.png"}
        db.commit()
    assert client.get(url, headers=headers(client, owner)).status_code == 404


def test_delete_tombstones_exact_artifact_and_preserves_chat_event(room_client):
    client, (owner, other, project, agent, _) = room_client
    room = create_room(client, owner, project, "screenshots")
    action_id, path = stored_screenshot(client, room, agent)
    other_action, other_path = stored_screenshot(client, room, agent)
    url = f"/api/v1/tools/rooms/{room}/actions/{action_id}/screenshot"

    assert client.delete(url, headers=headers(client, other)).status_code == 404
    assert path.exists()
    assert client.delete(url, headers=headers(client, owner)).status_code == 204
    assert not path.exists() and other_path.exists()
    assert client.get(url, headers=headers(client, owner)).status_code == 410
    assert client.delete(url, headers=headers(client, owner)).status_code == 204

    with client.app.state.session_factory() as db:
        action = db.get(ToolAction, action_id)
        assert action.observation["artifact_state"] == "deleted"
        assert action.observation["artifact_deleted_at"].endswith("+00:00")
        event = db.query(RoomEvent).filter_by(room_id=room, kind="tool_result",
                                              idempotency_key=f"tool-result:{action_id}").one()
        assert event.payload["screenshot_ref"].endswith(f"/{action_id}.png")
        assert db.get(ToolAction, other_action).observation["screenshot_ref"].endswith(f"/{other_action}.png")


def test_screenshot_route_refuses_symlink_payload(room_client, tmp_path):
    client, (owner, _, project, agent, _) = room_client
    room = create_room(client, owner, project, "screenshots")
    action_id, path = stored_screenshot(client, room, agent)
    target = tmp_path / "outside.png"
    target.write_bytes(PNG)
    path.unlink()
    path.symlink_to(target)
    url = f"/api/v1/tools/rooms/{room}/actions/{action_id}/screenshot"

    assert client.get(url, headers=headers(client, owner)).status_code == 404
    assert client.delete(url, headers=headers(client, owner)).status_code == 404
    assert target.read_bytes() == PNG


def test_delete_retry_removes_file_left_after_committed_tombstone(room_client, monkeypatch):
    client, (owner, _, project, agent, _) = room_client
    room = create_room(client, owner, project, "screenshots")
    action_id, path = stored_screenshot(client, room, agent)
    url = f"/api/v1/tools/rooms/{room}/actions/{action_id}/screenshot"
    real_unlink = tools_routes._unlink_screenshot
    def fail_after_tombstone(*_args, **_kwargs):
        raise tools_routes.HTTPException(500, "Synthetic unlink failure")
    monkeypatch.setattr(tools_routes, "_unlink_screenshot", fail_after_tombstone)

    failed = client.delete(url, headers=headers(client, owner))
    assert failed.status_code == 500, failed.text
    assert path.exists()
    with client.app.state.session_factory() as db:
        assert db.get(ToolAction, action_id).observation["artifact_state"] == "deleted"
    assert client.get(url, headers=headers(client, owner)).status_code == 410

    monkeypatch.setattr(tools_routes, "_unlink_screenshot", real_unlink)
    assert client.delete(url, headers=headers(client, owner)).status_code == 204
    assert not path.exists()


def test_screenshot_route_rejects_private_file_mode_and_hardlink(room_client, tmp_path):
    client, (owner, _, project, agent, _) = room_client
    room = create_room(client, owner, project, "screenshots")
    action_id, path = stored_screenshot(client, room, agent)
    url = f"/api/v1/tools/rooms/{room}/actions/{action_id}/screenshot"

    path.chmod(0o640)
    assert client.get(url, headers=headers(client, owner)).status_code == 404
    path.chmod(0o600)
    linked = tmp_path / "linked.png"
    linked.hardlink_to(path)
    assert client.get(url, headers=headers(client, owner)).status_code == 404


def test_screenshot_route_rejects_parent_interposed_during_open(room_client, tmp_path, monkeypatch):
    client, (owner, _, project, agent, _) = room_client
    room = create_room(client, owner, project, "screenshots")
    action_id, path = stored_screenshot(client, room, agent)
    url = f"/api/v1/tools/rooms/{room}/actions/{action_id}/screenshot"
    room_path = path.parent.parent
    displaced = room_path.with_name(f"{room_path.name}-displaced")
    outside = tmp_path / "outside-room"
    (outside / "screenshots").mkdir(parents=True)
    real_open = tools_routes.os.open
    swapped = False

    def interpose(name, *args, **kwargs):
        nonlocal swapped
        descriptor = real_open(name, *args, **kwargs)
        if not swapped and (Path(name) == path.parent or name == "screenshots"):
            room_path.rename(displaced)
            room_path.symlink_to(outside, target_is_directory=True)
            swapped = True
        return descriptor

    monkeypatch.setattr(tools_routes.os, "open", interpose)
    assert client.get(url, headers=headers(client, owner)).status_code == 404


def test_committed_delete_is_idempotent_after_artifact_directory_removed(room_client):
    client, (owner, _, project, agent, _) = room_client
    room = create_room(client, owner, project, "screenshots")
    action_id, path = stored_screenshot(client, room, agent)
    url = f"/api/v1/tools/rooms/{room}/actions/{action_id}/screenshot"
    assert client.delete(url, headers=headers(client, owner)).status_code == 204
    path.parent.rmdir()

    assert client.delete(url, headers=headers(client, owner)).status_code == 204
