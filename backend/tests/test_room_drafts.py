"""Durable composer drafts are isolated by owner and cleared by exact version."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.models import Conversation, Project, User


@pytest.fixture
def draft_client(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "draft-test-local-token")
    app = create_app(database_url=sqlite_url)
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            owner = User(email="draft-owner@test.invalid", username="draft-owner", hashed_password="x")
            other = User(email="draft-other@test.invalid", username="draft-other", hashed_password="x")
            db.add_all([owner, other])
            db.flush()
            owner_project = Project(user_id=owner.id, name="Owner project")
            other_project = Project(user_id=other.id, name="Other project")
            db.add_all([owner_project, other_project])
            db.flush()
            room = Conversation(user_id=owner.id, project_id=owner_project.id, title="Draft room")
            db.add(room)
            db.commit()
            ids = owner.id, other.id, room.id
        yield client, ids


def headers(client, owner_id):
    return {
        "X-Gamja-Session": client.app.state.local_session_token,
        "Authorization": f"Bearer {create_access_token({'sub': owner_id})}",
    }


def draft_path(room_id):
    return f"/api/v1/conversations/{room_id}/draft"


def test_draft_survives_restart_and_archived_room_is_read_only(draft_client, sqlite_url):
    client, (owner_id, _, room_id) = draft_client
    own = headers(client, owner_id)
    path = draft_path(room_id)

    assert client.get(path, headers=own).json() == {
        "content": "", "version": 0, "idempotency_key": None, "updated_at": None, "reply_to": None,
    }
    saved = client.put(path, headers=own, json={"content": "unfinished \ud55c\uae00", "expected_version": 0})
    assert saved.status_code == 200, saved.text
    assert saved.json()["content"] == "unfinished \ud55c\uae00"
    assert saved.json()["version"] == 1
    assert len(saved.json()["idempotency_key"]) == 36
    assert saved.json()["updated_at"] is not None

    with client.app.state.session_factory() as db:
        db.get(Conversation, room_id).is_active = False
        db.commit()

    assert client.get(path, headers=own).json() == saved.json()
    assert client.put(path, headers=own, json={"content": "blocked", "expected_version": 1}).status_code == 404
    assert client.delete(f"{path}?expected_version=1", headers=own).status_code == 404

    client.__exit__(None, None, None)
    with TestClient(create_app(database_url=sqlite_url)) as restarted:
        restarted_headers = {
            "X-Gamja-Session": restarted.app.state.local_session_token,
            "Authorization": f"Bearer {create_access_token({'sub': owner_id})}",
        }
        assert restarted.get(path, headers=restarted_headers).json() == saved.json()


def test_draft_is_invisible_to_another_owner(draft_client):
    client, (owner_id, other_id, room_id) = draft_client
    path = draft_path(room_id)
    saved = client.put(path, headers=headers(client, owner_id), json={"content": "private", "expected_version": 0})
    assert saved.status_code == 200

    stranger = headers(client, other_id)
    assert client.get(path, headers=stranger).status_code == 404
    assert client.put(path, headers=stranger, json={"content": "overwrite", "expected_version": 1}).status_code == 404
    assert client.delete(f"{path}?expected_version=1", headers=stranger).status_code == 404
    assert client.get(path, headers=headers(client, owner_id)).json() == saved.json()


def test_exact_version_clear_preserves_a_newer_edit(draft_client):
    client, (owner_id, _, room_id) = draft_client
    own = headers(client, owner_id)
    path = draft_path(room_id)

    sent = client.put(path, headers=own, json={"content": "send this", "expected_version": 0}).json()
    newer = client.put(path, headers=own, json={"content": "keep this newer edit", "expected_version": sent["version"]}).json()
    stale_clear = client.delete(f"{path}?expected_version={sent['version']}", headers=own)

    assert stale_clear.status_code == 409
    assert stale_clear.json()["detail"] == {"code": "draft_version_conflict", "current_version": newer["version"]}
    assert client.get(path, headers=own).json() == newer

    cleared = client.delete(f"{path}?expected_version={newer['version']}", headers=own)
    assert cleared.status_code == 204
    empty = client.get(path, headers=own).json()
    assert empty["content"] == ""
    assert empty["version"] == newer["version"] + 1
    assert empty["idempotency_key"] is None
    assert client.delete(f"{path}?expected_version={newer['version']}", headers=own).status_code == 204


def test_failed_save_retry_returns_the_same_message_key_and_stale_write_conflicts(draft_client):
    client, (owner_id, _, room_id) = draft_client
    own = headers(client, owner_id)
    path = draft_path(room_id)
    payload = {"content": "retry exactly", "expected_version": 0}

    first = client.put(path, headers=own, json=payload)
    retry = client.put(path, headers=own, json=payload)
    assert first.status_code == retry.status_code == 200
    assert retry.json() == first.json()

    changed = client.put(path, headers=own, json={"content": "new text", "expected_version": 1})
    assert changed.status_code == 200
    assert changed.json()["version"] == 2
    assert changed.json()["idempotency_key"] != first.json()["idempotency_key"]
    stale = client.put(path, headers=own, json={"content": "lost edit", "expected_version": 0})
    assert stale.status_code == 409
    assert stale.json()["detail"] == {"code": "draft_version_conflict", "current_version": 2}


def test_draft_validation_and_database_bounds(draft_client, sqlite_url):
    client, (owner_id, other_id, room_id) = draft_client
    own = headers(client, owner_id)
    path = draft_path(room_id)

    assert client.put(path, headers=own, json={"content": "x" * 65_536, "expected_version": 0}).status_code == 200
    assert client.put(path, headers=own, json={"content": "x" * 65_537, "expected_version": 1}).status_code == 422
    assert client.put(path, headers=own, json={"content": "x", "expected_version": -1}).status_code == 422
    assert client.put(path, headers=own, json={"content": "x", "expected_version": 1, "key": "client-controlled"}).status_code == 422
    assert client.delete(path, headers=own).status_code == 422
    assert client.delete(f"{path}?expected_version=-1", headers=own).status_code == 422

    database_path = sqlite_url.removeprefix("sqlite:///")
    with sqlite3.connect(database_path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE room_drafts SET content=? WHERE owner_id=? AND room_id=?",
                ("x" * 65_537, owner_id, room_id),
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO room_drafts (owner_id,room_id,content,version,idempotency_key) VALUES (?,?,?,?,?)",
                (other_id, room_id, "wrong owner", 1, "123e4567-e89b-42d3-a456-426614174000"),
            )
