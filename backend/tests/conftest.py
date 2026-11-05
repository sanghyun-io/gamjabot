import pytest


@pytest.fixture
def sqlite_url(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # Do not load the user's backend/.env.
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    url = f"sqlite:///{tmp_path / 'startup.sqlite'}"
    monkeypatch.setenv("DATABASE_URL", url)
    return url


@pytest.fixture
def personal_agent_core(monkeypatch):
    """Synthetic private-channel attachment, never the installed Owner's Core."""
    import json
    import socket
    import struct
    from app.main import RuntimeConfig, _personal_agent_core
    from app.services.secrets import CoreCredentialClient, CoreSecretStore
    from app.services.core_auth_broker import CoreAuthBrokerClient

    local, peer = socket.socketpair()
    auth_local, auth_peer = socket.socketpair()
    credential = auth = None
    values, calls = {}, []
    try:
        handshake = json.dumps({'version': 1, 'nonce': 'ab' * 32,
                                'current_generation_fingerprint': 'cd' * 32}).encode()
        auth_peer.sendall(struct.pack('>I', len(handshake)) + handshake)
        credential = CoreCredentialClient(local)
        auth = CoreAuthBrokerClient(auth_local)
        config = RuntimeConfig(database_url='sqlite:///:memory:',
            credential_fd=local.fileno(), auth_broker_fd=auth_local.fileno(),
            local_session_token='synthetic-local-session-token-32chars', cli_capability_hash='ef' * 32)

        def call(operation, namespace, reference, value=None):
            calls.append(operation)
            if operation == 'put': values[reference] = value
            elif operation == 'delete': values.pop(reference, None)
            else: return values.get(reference)

        monkeypatch.setattr(credential, 'call', call)
        store = CoreSecretStore(credential, 'provider',
            personal_agent_runtime=_personal_agent_core(config, credential, auth), auth_broker=auth)
        yield store, values, calls, auth
    finally:
        if auth is not None: auth.close()
        if credential is not None: credential.close()
        local.close(); peer.close(); auth_local.close(); auth_peer.close()
