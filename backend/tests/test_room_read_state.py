"""Owner room read cursors are durable, monotonic, and bounded by contiguous history."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.collaboration import RoomEvent
from app.models.models import Conversation, Project, User


@pytest.fixture
def read_state_client(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "read-state-local-token")
    app = create_app(database_url=sqlite_url)
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            owner = User(email="read-owner@test.invalid", username="read-owner", hashed_password="x")
            other = User(email="read-other@test.invalid", username="read-other", hashed_password="x")
            db.add_all([owner, other])
            db.flush()
            project = Project(user_id=owner.id, name="Read project")
            db.add(project)
            db.flush()
            room = Conversation(user_id=owner.id, project_id=project.id, title="Read room")
            db.add(room)
            db.flush()
            for seq in (1, 2):
                db.add(RoomEvent(
                    event_id=f"00000000-0000-0000-0000-{seq:012d}", room_id=room.id, seq=seq,
                    actor_type="user", actor_id=owner.id, actor_name_snapshot=owner.username,
                    kind="message", payload={"content": f"message {seq}"}, created_at=datetime.utcnow(),
                ))
            db.commit()
            ids = owner.id, other.id, room.id
        yield client, ids


def headers(client, owner_id):
    return {
        "X-Gamja-Session": client.app.state.local_session_token,
        "Authorization": f"Bearer {create_access_token({'sub': owner_id})}",
    }


def path(room_id):
    return f"/api/v1/conversations/{room_id}/read-state"


def test_read_state_survives_restart_and_archived_room_can_be_acknowledged(read_state_client, sqlite_url):
    client, (owner_id, _, room_id) = read_state_client
    own = headers(client, owner_id)
    assert client.get(path(room_id), headers=own).json() == {"last_read_seq": 0, "updated_at": None}

    first = client.put(path(room_id), headers=own, json={"last_read_seq": 1})
    assert first.status_code == 200, first.text
    assert first.json()["last_read_seq"] == 1
    assert first.json()["updated_at"] is not None

    with client.app.state.session_factory() as db:
        db.get(Conversation, room_id).is_active = False
        db.commit()
    archived = client.put(path(room_id), headers=own, json={"last_read_seq": 2})
    assert archived.status_code == 200, archived.text
    assert archived.json()["last_read_seq"] == 2
    assert client.get(path(room_id), headers=own).json() == archived.json()

    client.__exit__(None, None, None)
    with TestClient(create_app(database_url=sqlite_url)) as restarted:
        restarted_headers = {
            "X-Gamja-Session": restarted.app.state.local_session_token,
            "Authorization": f"Bearer {create_access_token({'sub': owner_id})}",
        }
        assert restarted.get(path(room_id), headers=restarted_headers).json() == archived.json()


def test_read_state_is_owner_isolated(read_state_client):
    client, (owner_id, other_id, room_id) = read_state_client
    own, stranger = headers(client, owner_id), headers(client, other_id)
    assert client.put(path(room_id), headers=own, json={"last_read_seq": 1}).status_code == 200
    assert client.get(path(room_id), headers=stranger).status_code == 404
    assert client.put(path(room_id), headers=stranger, json={"last_read_seq": 2}).status_code == 404
    assert client.get(path(room_id), headers=own).json()["last_read_seq"] == 1


def test_stale_and_reordered_acknowledgements_never_regress_state(read_state_client):
    client, (owner_id, _, room_id) = read_state_client
    own = headers(client, owner_id)
    advanced = client.put(path(room_id), headers=own, json={"last_read_seq": 2}).json()

    retry = client.put(path(room_id), headers=own, json={"last_read_seq": 2})
    stale = client.put(path(room_id), headers=own, json={"last_read_seq": 1})
    assert retry.status_code == stale.status_code == 200
    assert retry.json() == advanced
    assert stale.json() == advanced


def test_future_and_post_gap_acknowledgements_are_rejected(read_state_client):
    client, (owner_id, _, room_id) = read_state_client
    own = headers(client, owner_id)
    with client.app.state.session_factory() as db:
        db.query(RoomEvent).filter_by(room_id=room_id, seq=2).delete()
        db.add(RoomEvent(
            event_id="00000000-0000-0000-0000-000000000003", room_id=room_id, seq=3,
            actor_type="user", actor_id=owner_id, actor_name_snapshot="read-owner",
            kind="message", payload={"content": "message 3"}, created_at=datetime.utcnow(),
        ))
        db.commit()

    assert client.put(path(room_id), headers=own, json={"last_read_seq": 1}).status_code == 200
    for invalid in (2, 3, 99):
        response = client.put(path(room_id), headers=own, json={"last_read_seq": invalid})
        assert response.status_code == 422
        assert response.json()["detail"] == {
            "code": "read_seq_not_contiguous", "contiguous_high_water": 1,
        }
    assert client.get(path(room_id), headers=own).json()["last_read_seq"] == 1


def test_read_state_input_is_strict_and_zero_is_idempotent(read_state_client):
    client, (owner_id, _, room_id) = read_state_client
    own = headers(client, owner_id)
    assert client.put(path(room_id), headers=own, json={"last_read_seq": 0}).json() == {
        "last_read_seq": 0, "updated_at": None,
    }
    assert client.put(path(room_id), headers=own, json={"last_read_seq": -1}).status_code == 422
    assert client.put(path(room_id), headers=own, json={"last_read_seq": 1, "force": True}).status_code == 422
    assert client.put(path(room_id), headers=own, json={"last_read_seq": "1"}).status_code == 422
