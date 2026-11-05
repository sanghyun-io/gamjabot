from fastapi.testclient import TestClient

from app.main import create_app
from app.api.routes.auth import verify_password


def test_registration_login_and_owner_token_without_username(sqlite_url, monkeypatch):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'login-contract-token')
    with TestClient(create_app(database_url=sqlite_url)) as client:
        headers = {'X-Gamja-Session': 'login-contract-token'}
        account = {'email': 'owner@example.com', 'username': 'owner', 'password': 'long-test-password'}
        assert client.post('/api/v1/auth/register', headers=headers, json=account).status_code == 200
        login = client.post('/api/v1/auth/login', headers=headers, json={'email': account['email'], 'password': account['password']})
        assert login.status_code == 200
        bearer = {'Authorization': f"Bearer {login.json()['access_token']}"}
        assert client.get('/api/v1/auth/me', headers={**headers, **bearer}).json()['username'] == 'owner'
        assert client.post('/api/v1/auth/login', headers=headers, json={'email': account['email'], 'password': 'wrong'}).status_code == 401
        assert client.post('/api/v1/auth/login', json={'email': account['email'], 'password': account['password']}).status_code == 401


def test_password_bytes_are_bounded_without_truncating_non_ascii(sqlite_url, monkeypatch):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'login-contract-token')
    headers = {'X-Gamja-Session': 'login-contract-token'}
    with TestClient(create_app(database_url=sqlite_url)) as client:
        base = {'email': 'unicode@example.com', 'username': 'unicode'}
        assert client.post('/api/v1/auth/register', headers=headers, json={**base, 'password': 'é' * 36}).status_code == 200
        assert client.post('/api/v1/auth/login', headers=headers, json={'email': base['email'], 'password': 'é' * 36}).status_code == 200
        assert client.post('/api/v1/auth/login', headers=headers, json={'email': base['email'], 'password': 'é' * 37}).status_code == 422
        assert client.post('/api/v1/auth/register', headers=headers, json={**base, 'email': 'long@example.com', 'password': 'a' * 73}).status_code == 422
    assert verify_password('prior-password', '$2b$12$SlSyyxUWgzp6GqqrMP0YPOg0Ymng7su9ZWQ64YYM2fFjTnv.Q.HjC')
