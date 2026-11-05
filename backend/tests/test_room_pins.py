"""Durable owner-room pin mutation contracts."""

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.collaboration import RoomPin
from app.models.models import Conversation, Project, User


def auth(client, owner_id):
    return {
        "X-Gamja-Session": client.app.state.local_session_token,
        "Authorization": f"Bearer {create_access_token({'sub': owner_id})}",
    }


def test_pin_is_idempotent_concurrent_restart_stable_and_owner_scoped(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "room-pin-token")
    app = create_app(database_url=sqlite_url)
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            owner = User(email="pin-owner@test.invalid", username="pin-owner", hashed_password="x")
            other = User(email="pin-other@test.invalid", username="pin-other", hashed_password="x")
            db.add_all([owner, other])
            db.flush()
            own_project = Project(user_id=owner.id, name="Own")
            foreign_project = Project(user_id=other.id, name="Foreign")
            db.add_all([own_project, foreign_project])
            db.flush()
            closed = Conversation(user_id=owner.id, project_id=own_project.id, title="Closed", is_active=False)
            foreign = Conversation(user_id=other.id, project_id=foreign_project.id, title="Foreign")
            db.add_all([closed, foreign])
            db.commit()
            owner_id, other_id, room_id, foreign_id = owner.id, other.id, closed.id, foreign.id

        def put_pin(_number):
            response = client.put(f"/api/v1/rooms/{room_id}/pin", headers=auth(client, owner_id))
            return response.status_code, response.json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(put_pin, range(2)))
        assert [status for status, _ in responses] == [200, 200]
        assert responses[0][1] == responses[1][1]
        assert responses[0][1]["pinned"] is True
        with app.state.session_factory() as db:
            assert db.query(RoomPin).filter_by(owner_id=owner_id, room_id=room_id).count() == 1
        assert client.put(f"/api/v1/rooms/{foreign_id}/pin", headers=auth(client, owner_id)).status_code == 404
        assert client.put(f"/api/v1/rooms/{room_id}/pin", headers=auth(client, other_id)).status_code == 404

    restarted = create_app(database_url=sqlite_url)
    with TestClient(restarted) as client:
        pinned = client.get("/api/v1/rooms/index", params={"status": "closed"}, headers=auth(client, owner_id))
        assert pinned.status_code == 200
        assert pinned.json()["items"][0]["room_id"] == room_id
        assert pinned.json()["items"][0]["pinned"] is True
        assert client.delete(f"/api/v1/rooms/{room_id}/pin", headers=auth(client, owner_id)).status_code == 204
        assert client.delete(f"/api/v1/rooms/{room_id}/pin", headers=auth(client, owner_id)).status_code == 204
        assert client.delete(f"/api/v1/rooms/{foreign_id}/pin", headers=auth(client, owner_id)).status_code == 404
        with restarted.state.session_factory() as db:
            assert db.query(RoomPin).count() == 0
