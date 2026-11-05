"""The desktop runner must keep the renderer credential separate from user JWTs."""
import os
from pathlib import Path
import hashlib
import json
import secrets
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

import pytest


def test_runner_separates_launch_and_user_credentials():
    # A fresh interpreter matters: Settings is constructed when app modules import.
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _probe_runner() -> None:
    from datetime import datetime, timedelta, timezone

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import uvicorn
    from fastapi.testclient import TestClient
    import jwt
    from starlette.websockets import WebSocketDisconnect

    launch_token = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix="gamja-runner-auth-") as app_data:
        os.environ["GAMJA_LOCAL_SESSION_TOKEN"] = launch_token
        os.environ["GAMJA_DESKTOP_IPC_KEY"] = "ab" * 32
        os.environ["GAMJA_DESKTOP_IPC_ENDPOINT"] = "/tmp/synthetic-native.sock"
        os.environ["GAMJA_APP_DATA_DIR"] = app_data
        os.environ["GAMJA_PORT"] = "8765"
        os.environ["DEBUG"] = "true"

        def check_app(app, **_kwargs):
            from app.api.routes.auth import create_access_token
            from app.core.config import settings
            from app.models.models import Conversation, Project, User

            assert settings.SECRET_KEY != launch_token
            assert app.state.native_key == "ab" * 32
            assert all(name not in os.environ for name in ("SECRET_KEY", "GAMJA_LOCAL_SESSION_TOKEN",
                "GAMJA_DESKTOP_IPC_KEY", "GAMJA_DESKTOP_IPC_ENDPOINT"))
            with TestClient(app) as client:
                db = app.state.session_factory()
                try:
                    owner = User(email="owner@example.com", username="owner", hashed_password="unused")
                    other = User(email="other@example.com", username="other", hashed_password="unused")
                    db.add_all([owner, other])
                    db.flush()
                    owner_project = Project(user_id=owner.id, name="owner project")
                    other_project = Project(user_id=other.id, name="other project")
                    db.add_all([owner_project, other_project])
                    db.flush()
                    owner_room = Conversation(user_id=owner.id, project_id=owner_project.id, title="owner room")
                    other_room = Conversation(user_id=other.id, project_id=other_project.id, title="other room")
                    db.add_all([owner_room, other_room])
                    db.commit()
                    owner_id, owner_room_id, other_room_id = owner.id, owner_room.id, other_room.id
                finally:
                    db.close()

                forged = jwt.encode(
                    {"sub": str(owner_id), "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
                    launch_token,
                    algorithm=settings.ALGORITHM,
                )
                valid = create_access_token({"sub": owner_id})
                local_headers = {"X-Gamja-Session": launch_token}
                assert client.get("/api/v1/auth/me", headers={**local_headers, "Authorization": f"Bearer {forged}"}).status_code == 401
                assert client.get("/api/v1/auth/me", headers={**local_headers, "Authorization": f"Bearer {valid}"}).status_code == 200

                def ws_headers(user_token):
                    return {
                        "Origin": "http://localhost:3000",
                        "Sec-WebSocket-Protocol": f"gamja.chat, gamja.local.{launch_token}, gamja.user.{user_token}",
                    }

                for room_id, user_token in [(owner_room_id, forged), (other_room_id, valid)]:
                    try:
                        with client.websocket_connect(f"/api/v1/conversations/{room_id}/ws", headers=ws_headers(user_token)):
                            raise AssertionError("unauthorized room was accepted")
                    except WebSocketDisconnect as rejected:
                        assert rejected.code == 1008

                with client.websocket_connect(f"/api/v1/conversations/{owner_room_id}/ws", headers=ws_headers(valid)) as socket:
                    assert socket.accepted_subprotocol == "gamja.chat"

        uvicorn.run = check_app
        import run

        run.main()


if __name__ == "__main__":
    _probe_runner()


def test_typed_runtime_config_overrides_environment(tmp_path, monkeypatch):
    """A typed launch config must be authoritative even with stale legacy env."""
    from app.main import RuntimeConfig, create_app
    from fastapi.testclient import TestClient
    import hashlib

    token = secrets.token_urlsafe(32)
    capability = secrets.token_hex(32)
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "stale-environment-credential")
    config = RuntimeConfig(
        database_url=f"sqlite:///{tmp_path / 'typed.db'}",
        local_session_token=token,
        cli_capability_hash=hashlib.sha256(capability.encode()).hexdigest(),
    )
    app = create_app(runtime_config=config)
    with TestClient(app) as client:
        assert client.get("/health", headers={"X-Gamja-Session": token}).status_code == 200
        assert client.get("/health", headers={"X-Gamja-Session": "stale-environment-credential"}).status_code == 401
        assert client.get("/health", headers={"X-Gamja-CLI-Capability": capability}).status_code == 200
        assert client.get("/health", headers={"X-Gamja-CLI-Capability": capability, "Origin": "http://localhost:3000"}).status_code == 401
        assert app.state.native_endpoint is None
        assert app.state.native_key is None
        assert not hasattr(app.state, "provider_client")
    assert (tmp_path / "typed.db").is_file()


def test_typed_runtime_config_installs_distinct_core_credential_namespaces(tmp_path):
    import socket
    from app.main import RuntimeConfig, create_app
    from app.services.secrets import CoreSecretStore

    engine, core = socket.socketpair()
    descriptor = engine.detach()
    app = create_app(runtime_config=RuntimeConfig(
        database_url=f"sqlite:///{tmp_path / 'typed-credentials.db'}",
        credential_fd=descriptor,
    ))
    try:
        assert isinstance(app.state.secret_store, CoreSecretStore)
        assert isinstance(app.state.login_secret_store, CoreSecretStore)
        assert app.state.secret_store.namespace == 'provider'
        assert app.state.login_secret_store.namespace == 'remembered_login'
        assert app.state.secret_store.core is app.state.login_secret_store.core
    finally:
        app.state.credential_client.close()
        core.close()


def test_importing_main_never_consumes_the_inherited_credential_descriptor(monkeypatch):
    import importlib
    import app.main as main_module
    from app.services import secrets as secret_module

    consumed = []
    monkeypatch.setenv('GAMJA_CREDENTIAL_FD', '42')
    monkeypatch.setattr(secret_module.CoreCredentialClient, 'from_fd',
                        lambda descriptor: consumed.append(descriptor) or object())

    importlib.reload(main_module)

    assert consumed == []


def test_container_uses_the_side_effect_free_application_factory():
    dockerfile = Path(__file__).resolve().parents[1] / 'Dockerfile'
    text = dockerfile.read_text()
    assert 'app.main:create_app' in text
    assert '"--factory"' in text
    assert 'app.main:app' not in text


def test_runner_selects_managed_recovery_only_for_trusted_core_launches():
    import run

    assert run.provider_recovery_mode(
        {"GAMJA_DESKTOP_OWNER_STDIN": "1"},
        platform_name="darwin", machine="arm64",
    ) == "core_managed_recovery_required"
    assert run.provider_recovery_mode(
        {"GAMJA_AUTH_BROKER_FD": "invalid"},
        platform_name="linux", machine="x86_64",
    ) == "core_managed_recovery_required"
    assert run.provider_recovery_mode(
        {"GAMJA_PROVIDER_FD": "invalid"},
        platform_name="linux", machine="x86_64",
    ) == "core_managed_recovery_required"
    assert run.provider_recovery_mode(
        {"GAMJA_DESKTOP_OWNER_STDIN": "1"},
        platform_name="darwin", machine="x86_64",
    ) == "legacy"
    assert run.provider_recovery_mode(
        {"GAMJA_DESKTOP_OWNER_STDIN": "1"},
        platform_name="win32", machine="AMD64",
    ) == "legacy"


def test_runner_retains_managed_recovery_mode_after_environment_scrub(
        tmp_path, monkeypatch):
    import run
    import uvicorn

    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", token)
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GAMJA_PORT", "8765")
    monkeypatch.setenv("GAMJA_DESKTOP_OWNER_STDIN", "1")
    monkeypatch.setattr(run, "start_owner_watchdog", lambda: None)
    monkeypatch.setattr(
        run, "provider_recovery_mode",
        lambda *_args, **_kwargs: "core_managed_recovery_required",
    )
    captured = {}

    def capture_app(app, **_kwargs):
        captured["mode"] = app.state.provider_recovery_mode
        captured["owner_env"] = os.environ.get("GAMJA_DESKTOP_OWNER_STDIN")
        captured["broker_env"] = os.environ.get("GAMJA_AUTH_BROKER_FD")

    monkeypatch.setattr(uvicorn, "run", capture_app)

    run.main()

    assert captured == {
        "mode": "core_managed_recovery_required",
        "owner_env": None,
        "broker_env": None,
    }


_GENERATION = "ab" * 32


def _send_frame(channel, payload):
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    channel.sendall(struct.pack(">I", len(encoded)) + encoded)


def _read_exact(channel, size):
    result = bytearray()
    while len(result) < size:
        chunk = channel.recv(size - len(result))
        if not chunk:
            raise OSError("closed")
        result.extend(chunk)
    return bytes(result)


def _read_frame(channel):
    size = struct.unpack(">I", _read_exact(channel, 4))[0]
    return json.loads(_read_exact(channel, size))


def _provider_handshake(generation=_GENERATION):
    return {
        "version": 1,
        "current_generation_fingerprint": generation,
        "max_frame_bytes": 14_000_000,
        "max_in_flight": 4,
    }


def _binding():
    return {
        "credential_ref": "1:2:" + "cd" * 16,
        "owner_id": 1,
        "owner_epoch": "ef" * 32,
        "connection_id": 2,
        "config_revision": 3,
        "auth_grant_revision": 4,
        "provider_id": "azure-openai",
        "auth_method_id": "native_chain",
        "public_config_fingerprint": "01" * 32,
        "descriptor_revision": 1,
        "protocol": "azure_openai_responses_v1",
        "model_id": "deployment-1",
        "public_fields": {},
    }


def _call():
    return {
        "descriptor_revision": 1,
        "protocol": "azure_openai_responses_v1",
        "model_id": "deployment-1",
        "public_fields": {},
        "physical_model_request_budget": 1,
        "request": {"scope": {"connection_id": 2}, "messages": []},
    }


def test_runtime_config_strictly_parses_three_distinct_private_descriptors(monkeypatch):
    from app.main import RuntimeConfig

    monkeypatch.setenv("GAMJA_CREDENTIAL_FD", "41")
    monkeypatch.setenv("GAMJA_AUTH_BROKER_FD", "42")
    monkeypatch.setenv("GAMJA_PROVIDER_FD", "43")
    config = RuntimeConfig.from_environment("sqlite:///:memory:")
    assert (config.credential_fd, config.auth_broker_fd, config.provider_fd) == (41, 42, 43)

    for invalid in ("", "2", "+43", " 43", "43.0", "٤٣", "1000000"):
        monkeypatch.setenv("GAMJA_PROVIDER_FD", invalid)
        with pytest.raises(ValueError, match="provider descriptor"):
            RuntimeConfig.from_environment("sqlite:///:memory:")

    with pytest.raises(ValueError, match="distinct"):
        RuntimeConfig(database_url="sqlite:///:memory:", credential_fd=41,
                      auth_broker_fd=42, provider_fd=42)


def test_provider_fd_marks_managed_launch_and_is_scrubbed_after_consume(tmp_path, monkeypatch):
    import run
    import uvicorn

    auth_engine, auth_core = socket.socketpair()
    provider_engine, provider_core = socket.socketpair()
    _send_frame(auth_core, {"version": 1, "nonce": "12" * 32,
                            "current_generation_fingerprint": _GENERATION})
    _send_frame(provider_core, _provider_handshake())
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", secrets.token_urlsafe(32))
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GAMJA_PORT", "8765")
    monkeypatch.setenv("GAMJA_AUTH_BROKER_FD", str(auth_engine.detach()))
    monkeypatch.setenv("GAMJA_PROVIDER_FD", str(provider_engine.detach()))
    captured = {}

    def capture_app(app, **_kwargs):
        captured["mode"] = app.state.provider_recovery_mode
        captured["provider_env"] = os.environ.get("GAMJA_PROVIDER_FD")
        captured["generation"] = app.state.provider_client.current_generation_fingerprint
        app.state.provider_client.close()
        app.state.auth_broker_client.close()

    monkeypatch.setattr(uvicorn, "run", capture_app)
    try:
        run.main()
    finally:
        auth_core.close()
        provider_core.close()
    assert captured == {
        "mode": "core_managed_recovery_required",
        "provider_env": None,
        "generation": _GENERATION,
    }


def test_provider_client_exact_bind_execute_cancel_release_flow():
    from app.services.core_provider_transport import CoreProviderClient, CoreProviderUnavailable

    # Public synthetic wire vectors, independent of any private operator receipt.
    fixture_path = Path(__file__).parent / "fixtures/provider-wire-synthetic-v1.json"
    vectors = json.loads(fixture_path.read_text())
    engine, core = socket.socketpair()
    _send_frame(core, vectors[0])
    observed = []

    def server():
        bind = _read_frame(core)
        assert bind == vectors[1]
        observed.append(bind)
        handle = vectors[5]["handle"]
        _send_frame(core, vectors[5])
        execute = _read_frame(core)
        assert execute == vectors[2]
        cancel = _read_frame(core)
        assert cancel == vectors[3]
        observed.extend((execute, cancel))
        _send_frame(core, vectors[6])
        _send_frame(core, vectors[7])
        release = _read_frame(core)
        assert release == vectors[4]
        observed.append(release)
        _send_frame(core, vectors[8])
        failed_bind = _read_frame(core)
        assert failed_bind["type"] == "provider_bind_v1"
        assert failed_bind["request_id"] == vectors[9]["request_id"]
        _send_frame(core, vectors[9])

    worker = threading.Thread(target=server)
    worker.start()
    client = CoreProviderClient.from_fd(
        engine.detach(), expected_generation_fingerprint=(
            vectors[0]["current_generation_fingerprint"]),
    )
    try:
        handle = client.bind(vectors[1]["binding"])
        execution = client.begin_execute(handle, vectors[2]["call"])
        client.cancel(handle, execution.request_id)
        assert execution.result(timeout=1) == vectors[6]["result"]
        client.release(handle)
        with pytest.raises(CoreProviderUnavailable, match="configuration_changed"):
            client.bind(vectors[1]["binding"])
        assert client.available
        worker.join(1)
        assert not worker.is_alive()
        assert [item["type"] for item in observed] == [
            "provider_bind_v1", "provider_execute_v1",
            "provider_cancel_v1", "provider_release_v1",
        ]
        assert observed[0]["current_generation_fingerprint"] == (
            vectors[0]["current_generation_fingerprint"])
        assert observed[2]["target_request_id"] == observed[1]["request_id"]
    finally:
        client.close()
        core.close()


def test_provider_client_rejects_generation_mismatch_and_malformed_frames():
    from app.services.core_provider_transport import CoreProviderClient, CoreProviderUnavailable

    engine, core = socket.socketpair()
    _send_frame(core, _provider_handshake("cd" * 32))
    with pytest.raises(CoreProviderUnavailable, match="provider_handshake_invalid"):
        CoreProviderClient.from_fd(
            engine.detach(), expected_generation_fingerprint=_GENERATION)
    assert core.recv(1) == b""
    core.close()

    engine, core = socket.socketpair()
    duplicate = (
        b'{"version":1,"version":1,'
        b'"current_generation_fingerprint":"' + _GENERATION.encode() + b'",'
        b'"max_frame_bytes":14000000,"max_in_flight":4}'
    )
    core.sendall(struct.pack(">I", len(duplicate)) + duplicate)
    with pytest.raises(CoreProviderUnavailable, match="provider_handshake_invalid"):
        CoreProviderClient.from_fd(
            engine.detach(), expected_generation_fingerprint=_GENERATION)
    assert core.recv(1) == b""
    core.close()

    engine, core = socket.socketpair()
    _send_frame(core, _provider_handshake())
    client = CoreProviderClient.from_fd(
        engine.detach(), expected_generation_fingerprint=_GENERATION)
    core.sendall(struct.pack(">I", 0))
    deadline = time.monotonic() + 1
    while client.available and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not client.available
    client.close()
    core.close()


def test_provider_client_poisoned_when_cancel_ack_precedes_execute_terminal():
    from app.services.core_provider_transport import CoreProviderClient, CoreProviderUnavailable

    engine, core = socket.socketpair()
    _send_frame(core, _provider_handshake())

    def server():
        bind = _read_frame(core)
        handle = "56" * 32
        _send_frame(core, {"type": "provider_bound_v1", "version": 1,
                           "request_id": bind["request_id"], "handle": handle})
        execute = _read_frame(core)
        cancel = _read_frame(core)
        _send_frame(core, {"type": "provider_cancelled_v1", "version": 1,
                           "request_id": cancel["request_id"], "handle": handle,
                           "target_request_id": execute["request_id"]})

    worker = threading.Thread(target=server)
    worker.start()
    client = CoreProviderClient.from_fd(
        engine.detach(), expected_generation_fingerprint=_GENERATION)
    try:
        handle = client.bind(_binding())
        execution = client.begin_execute(handle, _call())
        with pytest.raises(CoreProviderUnavailable, match="provider_channel_unavailable"):
            client.cancel(handle, execution.request_id)
        with pytest.raises(CoreProviderUnavailable, match="provider_channel_unavailable"):
            execution.result(timeout=1)
        assert not client.available
    finally:
        client.close()
        core.close()
        worker.join(1)


def test_provider_client_bounds_final_outer_frame_and_tombstones():
    from app.services.core_provider_transport import CoreProviderClient, CoreProviderUnavailable

    engine, core = socket.socketpair()
    _send_frame(core, _provider_handshake())
    client = CoreProviderClient.from_fd(
        engine.detach(), expected_generation_fingerprint=_GENERATION)
    try:
        huge = _binding()
        huge["public_fields"] = {"value": "x" * 14_000_000}
        with pytest.raises(CoreProviderUnavailable, match="provider_request_invalid"):
            client.bind(huge)
        assert client.available
    finally:
        client.close()
        core.close()


def test_provider_client_accepts_13999999_outer_bytes_and_rejects_14000001():
    from app.services.core_provider_transport import CoreProviderClient, CoreProviderUnavailable

    engine, core = socket.socketpair()
    _send_frame(core, _provider_handshake())
    handled = threading.Event()

    def binding_for_size(target, request_id):
        binding = _binding()
        binding["public_fields"] = {"value": ""}
        message = {
            "type": "provider_bind_v1",
            "current_generation_fingerprint": _GENERATION,
            "binding": binding,
            "version": 1,
            "request_id": request_id,
        }
        base = len(json.dumps(message, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")).encode())
        binding["public_fields"]["value"] = "x" * (target - base)
        assert len(json.dumps(message, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")).encode()) == target
        return binding

    def server():
        assert struct.unpack(">I", _read_exact(core, 4))[0] == 13_999_999
        _read_exact(core, 13_999_999)
        _send_frame(core, {"type": "provider_bound_v1", "version": 1,
                           "request_id": 1, "handle": "78" * 32})
        handled.set()

    worker = threading.Thread(target=server)
    worker.start()
    client = CoreProviderClient.from_fd(
        engine.detach(), expected_generation_fingerprint=_GENERATION)
    try:
        assert client.bind(binding_for_size(13_999_999, 1)) == "78" * 32
        assert handled.wait(1)
        with pytest.raises(CoreProviderUnavailable, match="provider_request_invalid"):
            client.bind(binding_for_size(14_000_001, 2))
        assert client.available
        core.settimeout(0.05)
        with pytest.raises(socket.timeout):
            core.recv(1)
    finally:
        client.close()
        core.close()
        worker.join(1)


def test_provider_client_closes_when_four_retired_executions_cannot_drain():
    from app.services.core_provider_transport import CoreProviderClient, CoreProviderUnavailable

    engine, core = socket.socketpair()
    _send_frame(core, _provider_handshake())
    four_read = threading.Event()

    def server():
        bind = _read_frame(core)
        handle = "89" * 32
        _send_frame(core, {"type": "provider_bound_v1", "version": 1,
                           "request_id": bind["request_id"], "handle": handle})
        for _ in range(4):
            _read_frame(core)
        four_read.set()
        while core.recv(1):
            pass

    worker = threading.Thread(target=server)
    worker.start()
    client = CoreProviderClient.from_fd(
        engine.detach(), expected_generation_fingerprint=_GENERATION)
    try:
        handle = client.bind(_binding())
        for _ in range(4):
            execution = client.begin_execute(handle, _call())
            with pytest.raises(CoreProviderUnavailable, match="provider_timeout"):
                execution.result(timeout=0)
        assert four_read.wait(1)
        with pytest.raises(CoreProviderUnavailable, match="provider_channel_unavailable"):
            client.begin_execute(handle, _call())
        assert not client.available
    finally:
        client.close()
        core.close()
        worker.join(1)


def test_provider_client_discards_one_late_retired_result_without_reuse():
    from app.services.core_provider_transport import CoreProviderClient, CoreProviderUnavailable

    engine, core = socket.socketpair()
    _send_frame(core, _provider_handshake())
    release_late = threading.Event()

    def server():
        bind = _read_frame(core)
        handle = "9a" * 32
        _send_frame(core, {"type": "provider_bound_v1", "version": 1,
                           "request_id": bind["request_id"], "handle": handle})
        first = _read_frame(core)
        assert release_late.wait(1)
        _send_frame(core, {"type": "provider_result_v1", "version": 1,
                           "request_id": first["request_id"], "handle": handle,
                           "result": {"ok": True, "response_id": "late-1",
                                      "model": "deployment-1", "text": "late",
                                      "tool_calls": [],
                                      "physical_model_request_count": 1}})
        second = _read_frame(core)
        assert second["request_id"] != first["request_id"]
        _send_frame(core, {"type": "provider_result_v1", "version": 1,
                           "request_id": second["request_id"], "handle": handle,
                           "result": {"ok": True, "response_id": "response-2",
                                      "model": "deployment-1", "text": "ok",
                                      "tool_calls": [],
                                      "physical_model_request_count": 1}})

    worker = threading.Thread(target=server)
    worker.start()
    client = CoreProviderClient.from_fd(
        engine.detach(), expected_generation_fingerprint=_GENERATION)
    try:
        handle = client.bind(_binding())
        first = client.begin_execute(handle, _call())
        with pytest.raises(CoreProviderUnavailable, match="provider_timeout"):
            first.result(timeout=0)
        release_late.set()
        second = client.begin_execute(handle, _call())
        assert second.result(timeout=1)["response_id"] == "response-2"
        assert second.request_id != first.request_id
        assert client.available
    finally:
        client.close()
        core.close()
        worker.join(1)


@pytest.mark.parametrize("bad_frame", (
    struct.pack(">I", 0),
    struct.pack(">I", 14_000_001),
    struct.pack(">I", 1) + b"\xff",
    struct.pack(">I", 1) + b"{",
))
def test_provider_client_poison_on_closed_frame_failures(bad_frame):
    from app.services.core_provider_transport import CoreProviderClient

    engine, core = socket.socketpair()
    _send_frame(core, _provider_handshake())
    client = CoreProviderClient.from_fd(
        engine.detach(), expected_generation_fingerprint=_GENERATION)
    core.sendall(bad_frame)
    deadline = time.monotonic() + 1
    while client.available and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not client.available
    client.close()
    core.close()


def test_create_app_closes_partial_private_clients_in_reverse_order(tmp_path, monkeypatch):
    import app.main as main_module
    from app.main import RuntimeConfig
    from app.services.core_provider_transport import CoreProviderUnavailable

    closed = []

    class Client:
        available = True
        current_generation_fingerprint = _GENERATION
        def __init__(self, name):
            self.name = name
        def close(self):
            closed.append(self.name)
        def set_poison_callback(self, _callback):
            pass

    credential = Client("credential")
    auth = Client("auth")
    monkeypatch.setattr(main_module.CoreCredentialClient, "from_fd", lambda _fd: credential)
    monkeypatch.setattr(main_module.CoreAuthBrokerClient, "from_fd", lambda _fd: auth)
    monkeypatch.setattr(main_module.CoreProviderClient, "from_fd",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            CoreProviderUnavailable("provider_handshake_invalid")))

    with pytest.raises(CoreProviderUnavailable, match="provider_handshake_invalid"):
        main_module.create_app(runtime_config=RuntimeConfig(
            database_url=f"sqlite:///{tmp_path / 'partial.db'}",
            credential_fd=41, auth_broker_fd=42, provider_fd=43,
            provider_recovery_mode="core_managed_recovery_required",
        ))
    assert closed == ["auth", "credential"]


def test_create_app_requires_accepted_auth_attachment_before_provider(tmp_path, monkeypatch):
    import app.main as main_module
    from app.main import RuntimeConfig
    from app.services.core_provider_transport import CoreProviderUnavailable

    closed = []

    class Credential:
        def close(self):
            closed.append("credential")

    monkeypatch.setattr(main_module.CoreCredentialClient, "from_fd",
                        lambda _fd: Credential())
    monkeypatch.setattr(main_module.CoreProviderClient, "from_fd",
                        lambda *_args, **_kwargs: pytest.fail(
                            "provider FD consumed without auth attachment"))
    with pytest.raises(CoreProviderUnavailable, match="provider_auth_attachment_required"):
        main_module.create_app(runtime_config=RuntimeConfig(
            database_url=f"sqlite:///{tmp_path / 'missing-auth.db'}",
            credential_fd=41, provider_fd=43,
            provider_recovery_mode="core_managed_recovery_required",
        ))
    assert closed == ["credential"]


def test_lifespan_closes_scheduler_before_provider_auth_and_credential(tmp_path, monkeypatch):
    import app.main as main_module
    from app.main import RuntimeConfig
    from fastapi.testclient import TestClient

    closed = []

    class Client:
        available = True
        current_generation_fingerprint = _GENERATION
        def __init__(self, name):
            self.name = name
        def close(self):
            closed.append(self.name)
        def set_poison_callback(self, _callback):
            pass

    clients = {name: Client(name) for name in ("credential", "auth", "provider")}
    monkeypatch.setattr(main_module.CoreCredentialClient, "from_fd",
                        lambda _fd: clients["credential"])
    monkeypatch.setattr(main_module.CoreAuthBrokerClient, "from_fd",
                        lambda _fd: clients["auth"])
    monkeypatch.setattr(main_module.CoreProviderClient, "from_fd",
                        lambda *_args, **_kwargs: clients["provider"])

    class OwnerEpochs:
        def __init__(self, *_args):
            pass
        def set_credential_cache(self, _cache):
            pass

    class Cache:
        def close(self):
            closed.append("cache")

    class FakeScheduler:
        def __init__(self, session_factory, *_args):
            self.sessions = session_factory
            self.broker = None
        async def start(self):
            pass
        async def close(self):
            closed.append("scheduler")

    class FakeExecutor:
        def __init__(self, *_args, **_kwargs):
            pass
        async def close(self):
            closed.append("executor")

    monkeypatch.setattr(main_module, "TrustedOwnerEpochProvider", OwnerEpochs)
    monkeypatch.setattr(main_module, "SessionCredentialCache", Cache)
    monkeypatch.setattr(main_module, "Scheduler", FakeScheduler)
    monkeypatch.setattr(main_module, "LocalExecutor", FakeExecutor)
    app = main_module.create_app(runtime_config=RuntimeConfig(
        database_url=f"sqlite:///{tmp_path / 'shutdown.db'}",
        credential_fd=41, auth_broker_fd=42, provider_fd=43,
    ))
    with TestClient(app):
        pass
    assert closed == ["scheduler", "executor", "cache", "provider", "auth", "credential"]


@pytest.mark.parametrize("provider_attached", (False, True))
def test_lifespan_freezes_exact_runtime_auth_methods_without_read_time_probes(
        tmp_path, monkeypatch, provider_attached):
    import app.main as main_module
    from app.main import RuntimeConfig
    from fastapi.testclient import TestClient

    generation_reads = []
    provider_expectations = []
    captured = []

    class AuthClient:
        @property
        def current_generation_fingerprint(self):
            generation_reads.append("auth")
            return _GENERATION
        @property
        def available(self):
            pytest.fail("cached availability must not inspect auth client state")
        def close(self):
            pass

    class ProviderClient:
        @property
        def available(self):
            pytest.fail("cached availability must not inspect provider client state")
        def close(self):
            pass

    auth = AuthClient()
    provider = ProviderClient()
    monkeypatch.setattr(main_module.CoreAuthBrokerClient, "from_fd", lambda _fd: auth)

    def provider_from_fd(_fd, *, expected_generation_fingerprint):
        provider_expectations.append(expected_generation_fingerprint)
        return provider

    monkeypatch.setattr(main_module.CoreProviderClient, "from_fd", provider_from_fd)

    class Coordinator:
        def __init__(self, **kwargs):
            captured.append(kwargs["auth_method_availability"])

    class OwnerEpochs:
        def __init__(self, *_args):
            pass
        def set_credential_cache(self, _cache):
            pass

    class Cache:
        def close(self):
            pass

    class Scheduler:
        def __init__(self, session_factory, *_args):
            self.sessions = session_factory
            self.broker = None
        async def start(self):
            pass
        async def close(self):
            pass

    class Executor:
        def __init__(self, *_args, **_kwargs):
            pass
        async def close(self):
            pass

    monkeypatch.setattr(main_module, "ConnectionRecoveryCoordinator", Coordinator)
    monkeypatch.setattr(main_module, "TrustedOwnerEpochProvider", OwnerEpochs)
    monkeypatch.setattr(main_module, "SessionCredentialCache", Cache)
    monkeypatch.setattr(main_module, "Scheduler", Scheduler)
    monkeypatch.setattr(main_module, "LocalExecutor", Executor)
    config = RuntimeConfig(
        database_url=f"sqlite:///{tmp_path / f'catalog-{provider_attached}.db'}",
        auth_broker_fd=42, provider_fd=43 if provider_attached else None,
    )
    app = main_module.create_app(runtime_config=config)
    with TestClient(app):
        assert len(captured) == 1
        available = captured[0]
        expected = {
            ("azure-openai", "native_chain"),
            ("azure-openai", "api_key"),
            ("amazon-bedrock", "native_chain"),
            ("google-vertex", "native_chain"),
        } if provider_attached else set()
        observed = {
            pair for pair in (
                ("azure-openai", "native_chain"),
                ("azure-openai", "api_key"),
                ("amazon-bedrock", "native_chain"),
                ("google-vertex", "native_chain"),
                ("openai", "api_key"),
                ("unknown", "native_chain"),
            ) if available(*pair)
        }
        assert observed == expected
        assert generation_reads == (["auth"] if provider_attached else [])
        assert provider_expectations == ([_GENERATION] if provider_attached else [])
