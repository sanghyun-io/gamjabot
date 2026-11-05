"""Legacy device records remain, without claiming remote transport exists."""

from datetime import datetime

from test_room_state import room_client, headers
from app.models.models import RemoteDevice


def test_legacy_token_validation_is_not_a_connection(room_client):
    client, (owner, _, _, _, _) = room_client
    original = datetime(2020, 1, 2, 3, 4, 5)
    with client.app.state.session_factory() as db:
        device = RemoteDevice(user_id=owner, name="Old phone", access_token="fixture-secret",
                              is_active=True, last_connected_at=original)
        db.add(device)
        db.commit()
        device_id = device.id
    url = f"/api/v1/remote/devices/{device_id}/connect"
    own = headers(client, owner)
    assert client.post(url, params={"access_token": "wrong"}, headers=own).status_code == 401
    response = client.post(url, params={"access_token": "fixture-secret"}, headers=own)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "unavailable"
    assert "not available" in response.json()["message"]
    with client.app.state.session_factory() as db:
        saved = db.get(RemoteDevice, device_id)
        assert saved.is_active and saved.last_connected_at == original

    registered = client.post("/api/v1/remote/devices", params={"device_name": "Future phone"}, headers=own)
    assert registered.status_code == 201
    assert "사용할 수 없습니다" in registered.json()["message"]
    with client.app.state.session_factory() as db:
        assert db.get(RemoteDevice, registered.json()["device_id"]).last_connected_at is None
