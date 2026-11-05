import hashlib
import json
import logging
import os
import socket
import struct
import subprocess
import threading
from datetime import datetime, timedelta

import pytest
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient
from sqlalchemy import text


NONCE = "11" * 32
GENERATION = "22" * 32
EPOCH_A = "33" * 32
EPOCH_B = "44" * 32
EPOCH_C = "aa" * 32
OPERATION = "55" * 32
INTENT = "66" * 32
BEFORE = "77" * 32
PUBLIC_CONFIG = "88" * 32
BINDING_FINGERPRINT = "99" * 32


class FramedPeer:
    def __init__(self, channel):
        self.channel = channel
        self.channel.settimeout(2)

    def send(self, value):
        encoded = json.dumps(value, separators=(",", ":")).encode()
        self.channel.sendall(struct.pack(">I", len(encoded)) + encoded)

    def receive(self):
        header = self._read_exact(4)
        size = struct.unpack(">I", header)[0]
        return json.loads(self._read_exact(size))

    def _read_exact(self, size):
        body = bytearray()
        while len(body) < size:
            chunk = self.channel.recv(size - len(body))
            if not chunk:
                raise EOFError
            body.extend(chunk)
        return bytes(body)


def handshake(peer):
    peer.send({"version": 1, "nonce": NONCE,
               "current_generation_fingerprint": GENERATION})


def stable_binding(owner_id=7, owner_epoch=EPOCH_A):
    return {
        "version": 1,
        "operation_id": OPERATION,
        "owner_id": owner_id,
        "connection_id": 9,
        "kind": "auth",
        "intent_fingerprint": INTENT,
        "before_state_fingerprint": BEFORE,
        "revoke_requested": False,
        "expires_at": 2_000_000_000,
        "owner_epoch": owner_epoch,
        "provider_id": "openai",
        "auth_method_id": "device_code",
        "config_revision": 3,
        "auth_grant_revision": 4,
        "public_config_fingerprint": PUBLIC_CONFIG,
    }


def recovery_selector(owner_id=7):
    return {
        "operation_id": OPERATION,
        "owner_id": owner_id,
        "connection_id": 9,
        "kind": "auth",
        "intent_fingerprint": INTENT,
        "before_state_fingerprint": BEFORE,
        "revoke_requested": False,
    }


def make_client(core_loop):
    from app.services.core_auth_broker import CoreAuthBrokerClient

    backend, core = socket.socketpair()
    descriptor = backend.detach()
    worker = threading.Thread(target=core_loop, args=(FramedPeer(core),), daemon=True)
    worker.start()
    return CoreAuthBrokerClient.from_fd(descriptor), core, worker


def test_operation_allocate_uses_exact_bound_owner_and_returns_core_rng_id():
    allocated = "ab" * 32

    def core(peer):
        handshake(peer)
        assert peer.receive() == {
            "type": "operation_allocate", "version": 1,
            "owner_id": 7, "current_owner_epoch": EPOCH_A,
        }
        peer.send({"version": 1, "state": "allocated", "operation_id": allocated})

    client, core_socket, core_thread = make_client(core)
    try:
        assert client.operation_allocate(7, EPOCH_A) == allocated
    finally:
        close_fixture(client, core_socket, core_thread)


@pytest.mark.parametrize("reply", [
    {"version": 1, "state": "allocated", "operation_id": "not-lower-hex"},
    {"version": 1, "state": "failed", "code": "owner_epoch_changed"},
    {"version": 1, "state": "allocated", "operation_id": OPERATION, "unknown": True},
])
def test_operation_allocate_rejects_non_success_or_non_closed_reply(reply):
    from app.services.core_auth_broker import CoreAuthBrokerUnavailable

    def core(peer):
        handshake(peer)
        peer.receive()
        peer.send(reply)
        with pytest.raises((EOFError, OSError)):
            peer.receive()

    client, core_socket, core_thread = make_client(core)
    try:
        with pytest.raises(CoreAuthBrokerUnavailable):
            client.operation_allocate(7, EPOCH_A)
        assert client.available is False
    finally:
        close_fixture(client, core_socket, core_thread)


def close_fixture(client, core, worker):
    client.close()
    core.close()
    worker.join(2)
    assert not worker.is_alive()


def test_owner_epoch_is_unpublished_until_matching_bind_ack_and_cache_is_rotated():
    from app.services.core_auth_broker import TrustedOwnerEpochProvider
    from app.services.secrets import SessionCredentialCache

    request_seen = threading.Event()
    release_reply = threading.Event()
    observed = {}

    def core(peer):
        handshake(peer)
        observed.update(peer.receive())
        request_seen.set()
        assert release_reply.wait(2)
        peer.send({"version": 1, "state": "bound", "owner_id": 7,
                   "current_owner_epoch": EPOCH_A})

    client, core_socket, core_thread = make_client(core)
    cache = SessionCredentialCache()
    cache.put((7, 9), "cached-secret")
    provider = TrustedOwnerEpochProvider(client, cache)
    binding = {}
    binder = threading.Thread(target=lambda: binding.setdefault("value", provider.bind(7)))
    binder.start()
    try:
        assert request_seen.wait(2)
        assert provider.current(7) is None
        assert cache.get((7, 9)) is None
        release_reply.set()
        binder.join(2)
        assert binding["value"].owner_id == 7
        assert binding["value"].owner_epoch == EPOCH_A
        assert provider.current(7) == EPOCH_A
        assert provider.current(8) is None
        assert observed == {"type": "owner_bind", "version": 1, "owner_id": 7}
    finally:
        release_reply.set()
        binder.join(2)
        cache.close()
        close_fixture(client, core_socket, core_thread)


@pytest.mark.parametrize("reply", [
    {"version": 1, "state": "bound", "owner_id": 8, "current_owner_epoch": EPOCH_A},
    {"version": True, "state": "bound", "owner_id": 7, "current_owner_epoch": EPOCH_A},
    {"version": 1, "state": "bound", "owner_id": 7, "current_owner_epoch": "not-hex"},
    {"version": 1, "state": "bound", "owner_id": 7,
     "current_owner_epoch": EPOCH_A, "unexpected": True},
])
def test_malformed_or_mismatched_bind_reply_poisons_channel_and_clears_authority(reply):
    from app.services.core_auth_broker import TrustedOwnerEpochProvider
    from app.services.secrets import SessionCredentialCache

    def core(peer):
        handshake(peer)
        peer.receive()
        peer.send(reply)
        with pytest.raises((EOFError, OSError)):
            peer.receive()

    client, core_socket, core_thread = make_client(core)
    cache = SessionCredentialCache()
    cache.put((7, 9), "cached-secret")
    provider = TrustedOwnerEpochProvider(client, cache)
    try:
        assert provider.bind(7) is None
        assert provider.current(7) is None
        assert cache.get((7, 9)) is None
        assert client.available is False
    finally:
        cache.close()
        close_fixture(client, core_socket, core_thread)


def test_reply_loss_after_core_applies_bind_closes_fd_and_never_publishes_epoch():
    from app.services.core_auth_broker import TrustedOwnerEpochProvider

    def core(peer):
        handshake(peer)
        assert peer.receive() == {"type": "owner_bind", "version": 1, "owner_id": 7}
        peer.channel.close()

    client, core_socket, core_thread = make_client(core)
    provider = TrustedOwnerEpochProvider(client)
    try:
        assert provider.bind(7) is None
        assert provider.current(7) is None
        assert client.available is False
    finally:
        client.close()
        core_thread.join(2)


def test_stale_logout_compare_and_unbind_preserves_new_login_winner():
    from app.services.core_auth_broker import TrustedOwnerEpochProvider

    seen = []

    def core(peer):
        handshake(peer)
        first = peer.receive(); seen.append(first)
        peer.send({"version": 1, "state": "bound", "owner_id": 7,
                   "current_owner_epoch": EPOCH_A})
        second = peer.receive(); seen.append(second)
        peer.send({"version": 1, "state": "bound", "owner_id": 8,
                   "current_owner_epoch": EPOCH_B})
        stale = peer.receive(); seen.append(stale)
        peer.send({"version": 1, "state": "mismatch_preserved"})

    client, core_socket, core_thread = make_client(core)
    provider = TrustedOwnerEpochProvider(client)
    try:
        provider.bind(7)
        stale = provider.snapshot()
        provider.bind(8)
        assert provider.compare_and_unbind(stale) == "mismatch_preserved"
        assert provider.current(8) == EPOCH_B
        assert provider.current(7) is None
        assert seen[-1] == {
            "type": "owner_unbind", "version": 1,
            "expected_owner_id": 7, "expected_owner_epoch": EPOCH_A,
        }
    finally:
        close_fixture(client, core_socket, core_thread)


@pytest.mark.parametrize("core_applies_unbind", [False, True])
def test_ambiguous_unbind_never_returns_a_confirmed_core_state(core_applies_unbind):
    from app.services.core_auth_broker import CoreAuthBrokerUnavailable, TrustedOwnerEpochProvider
    from app.services.secrets import SessionCredentialCache

    def core(peer):
        handshake(peer)
        assert peer.receive() == {"type": "owner_bind", "version": 1, "owner_id": 7}
        peer.send({"version": 1, "state": "bound", "owner_id": 7,
                   "current_owner_epoch": EPOCH_A})
        if core_applies_unbind:
            assert peer.receive() == {
                "type": "owner_unbind", "version": 1,
                "expected_owner_id": 7, "expected_owner_epoch": EPOCH_A,
            }
        peer.channel.close()

    client, core_socket, core_thread = make_client(core)
    cache = SessionCredentialCache()
    provider = TrustedOwnerEpochProvider(client, cache)
    try:
        expected = provider.bind(7)
        cache.put((7, 9), "cached-secret")
        with pytest.raises(CoreAuthBrokerUnavailable, match="auth_broker_channel_unavailable"):
            provider.compare_and_unbind(expected)
        assert provider.current(7) is None
        assert client.available is False
        assert cache.get((7, 9)) is None
    finally:
        cache.close()
        client.close()
        core_thread.join(2)
        assert not core_thread.is_alive()


def test_exact_auth_frame_uses_handshake_generation_and_keeps_private_reply_separate():
    observed = {}

    def core(peer):
        handshake(peer)
        observed.update(peer.receive())
        peer.send({
            "version": 1, "operation_id": OPERATION, "state": "prepared",
            "authorization_url": "https://provider.example/authorize",
            "prepared_ref": "7:9:" + "ab" * 16,
            "commit_handle": "cd" * 32,
        })

    client, core_socket, core_thread = make_client(core)
    try:
        reply = client.credential_prepare(stable_binding(), exact_old_reference="7:9:" + "ef" * 16)
        assert reply.public.safe_dict() == {
            "version": 1, "operation_id": OPERATION, "state": "prepared",
            "authorization_url": "https://provider.example/authorize",
        }
        assert reply.prepared_ref == "7:9:" + "ab" * 16
        assert reply.commit_handle == "cd" * 32
        assert "prepared_ref" not in reply.public.safe_dict()
        assert "commit_handle" not in reply.public.safe_dict()
        assert observed == {
            "type": "credential_prepare",
            "binding": stable_binding(),
            "generation": {"nonce": NONCE, "current_generation_fingerprint": GENERATION},
            "exact_old_reference": "7:9:" + "ef" * 16,
        }
    finally:
        close_fixture(client, core_socket, core_thread)


@pytest.mark.parametrize(("provider_id", "native_enrollment"), [
    ("azure-openai", {"provider": "azure_cli", "resource_endpoint": "https://example.openai.azure.com",
                      "api_version": "v1"}),
    ("amazon-bedrock", {"provider": "aws_profile", "profile_name": "production",
                        "region": "us-east-1"}),
    ("google-vertex", {"provider": "vertex_adc", "project_id": "potato-project",
                       "location": "us-central1"}),
])
def test_auth_start_sends_only_the_closed_native_enrollment_spec(provider_id, native_enrollment):
    observed = {}

    def core(peer):
        handshake(peer)
        observed.update(peer.receive())
        peer.send({"version": 1, "operation_id": OPERATION, "state": "prepared"})

    client, core_socket, core_thread = make_client(core)
    binding = stable_binding()
    binding.update(provider_id=provider_id, auth_method_id="native_chain")
    try:
        assert client.auth_start(binding, native_enrollment).state == "prepared"
        assert observed == {
            "type": "auth_start",
            "binding": binding,
            "generation": {"nonce": NONCE, "current_generation_fingerprint": GENERATION},
            "native_enrollment": native_enrollment,
        }
    finally:
        close_fixture(client, core_socket, core_thread)


@pytest.mark.parametrize("binding_changes,native_enrollment", [
    ({"provider_id": "azure-openai", "auth_method_id": "native_chain"}, None),
    ({}, {"provider": "azure_cli", "resource_endpoint": "https://example.openai.azure.com",
          "api_version": "v1"}),
    ({"provider_id": "azure-openai", "auth_method_id": "native_chain"},
     {"provider": "vertex_adc", "project_id": "potato-project", "location": "us-central1"}),
    ({"provider_id": "azure-openai", "auth_method_id": "native_chain"},
     {"provider": "azure_cli", "resource_endpoint": "https://example.openai.azure.com",
      "api_version": "v1", "executable_path": "/tmp/forbidden"}),
    ({"provider_id": "amazon-bedrock", "auth_method_id": "native_chain"},
     {"provider": "aws_profile", "profile_name": True, "region": "us-east-1"}),
])
def test_auth_start_rejects_missing_mismatched_or_nonclosed_native_spec_before_core(
        binding_changes, native_enrollment):
    received = threading.Event()
    inspect_channel = threading.Event()
    inspected = threading.Event()

    def core(peer):
        handshake(peer)
        assert inspect_channel.wait(2)
        peer.channel.settimeout(0.1)
        try:
            peer.receive()
            received.set()
        except (EOFError, OSError, TimeoutError):
            pass
        finally:
            inspected.set()

    client, core_socket, core_thread = make_client(core)
    binding = stable_binding()
    binding.update(binding_changes)
    try:
        with pytest.raises(ValueError, match="native enrollment"):
            client.auth_start(binding, native_enrollment)
        inspect_channel.set()
        assert inspected.wait(1)
        assert received.is_set() is False
    finally:
        close_fixture(client, core_socket, core_thread)


def test_all_seven_auth_frame_variants_are_closed_and_generation_bound():
    from app.services.core_auth_broker import AuthPrivateReply, AuthPublicState, OwnerBinding

    requests = []

    def core(peer):
        handshake(peer)
        for _ in range(7):
            request = peer.receive()
            requests.append(request)
            peer.send({"version": 1, "operation_id": OPERATION, "state": "succeeded"})

    client, core_socket, core_thread = make_client(core)
    try:
        with pytest.raises(ValueError, match="expected owner binding"):
            client.owner_unbind(OwnerBinding(0, EPOCH_A))
        with pytest.raises(ValueError, match="expected owner binding"):
            client.owner_unbind(OwnerBinding(7, "invalid"))
        binding = stable_binding()
        public_replies = [client.auth_start(binding), client.auth_status(binding),
                          client.auth_cancel(binding)]
        private_replies = [client.credential_prepare(binding), client.credential_commit(binding),
                           client.credential_discard_exact(binding, "inspect"),
                           client.credential_refresh(binding)]
        assert all(isinstance(reply, AuthPublicState) for reply in public_replies)
        assert all(isinstance(reply, AuthPrivateReply) for reply in private_replies)
        assert [request["type"] for request in requests] == [
            "auth_start", "auth_status", "auth_cancel", "credential_prepare",
            "credential_commit", "credential_discard", "credential_refresh",
        ]
        assert all(request["generation"] == {
            "nonce": NONCE, "current_generation_fingerprint": GENERATION,
        } for request in requests)
        assert "native_enrollment" not in requests[0]
        assert requests[5]["target"] == {"exact_binding": binding}
        assert requests[5]["action"] == "inspect"
        invalid = dict(binding)
        invalid["version"] = True
        with pytest.raises(ValueError, match="stable binding"):
            client.auth_start(invalid)
    finally:
        close_fixture(client, core_socket, core_thread)


def test_private_authority_envelopes_are_not_jsonable_or_loggable(caplog):
    from app.services.core_auth_broker import TrustedOwnerEpochProvider

    prepared_ref = "7:9:" + "ab" * 16
    commit_handle = "cd" * 32

    def core(peer):
        handshake(peer)
        assert peer.receive()["type"] == "owner_bind"
        peer.send({"version": 1, "state": "bound", "owner_id": 7,
                   "current_owner_epoch": EPOCH_A})
        assert peer.receive()["type"] == "credential_prepare"
        peer.send({
            "version": 1, "operation_id": OPERATION, "state": "prepared",
            "prepared_ref": prepared_ref, "commit_handle": commit_handle,
        })
        assert peer.receive()["type"] == "credential_discard"
        peer.send({
            "operation_id": OPERATION, "state": "recovering",
            "material_state": "recovery", "receipt_state": "none",
            "binding_fingerprint": BINDING_FINGERPRINT,
        })

    client, core_socket, core_thread = make_client(core)
    provider = TrustedOwnerEpochProvider(client)
    try:
        owner = provider.bind(7)
        private = client.credential_prepare(stable_binding())
        recovery = client.credential_discard_recovery(recovery_selector(), "inspect")
        assert owner.owner_epoch == EPOCH_A
        assert private.prepared_ref == prepared_ref
        assert private.commit_handle == commit_handle
        assert recovery.binding_fingerprint == BINDING_FINGERPRINT
        assert client.current_generation_fingerprint == GENERATION
        assert not hasattr(client, "nonce")

        forbidden = (EPOCH_A, prepared_ref, commit_handle, BINDING_FINGERPRINT, NONCE, GENERATION)
        representations = " ".join(repr(value) for value in (owner, private, recovery, client))
        assert all(value not in representations for value in forbidden)
        with caplog.at_level(logging.INFO):
            logging.getLogger("core-auth-private-test").info(
                "private envelopes: %r %r %r", owner, private, recovery)
        assert all(value not in caplog.text for value in forbidden)

        for private_value in (owner, private, recovery):
            with pytest.raises(ValueError):
                jsonable_encoder(private_value)
        assert jsonable_encoder(private.safe_projection(), exclude_none=True) == {
            "version": 1, "operation_id": OPERATION, "state": "prepared",
        }
        assert jsonable_encoder(recovery.safe_projection(), exclude_none=True) == {
            "operation_id": OPERATION, "state": "recovering",
            "material_state": "recovery", "receipt_state": "none",
        }
    finally:
        close_fixture(client, core_socket, core_thread)


def test_recovery_selector_inspect_and_action_are_closed_and_private():
    requests = []
    summary = {
        "provider_id": "openrouter", "auth_method_id": "api_key",
        "config_revision": 4, "auth_grant_revision": 3,
        "public_config_fingerprint": PUBLIC_CONFIG,
    }

    def core(peer):
        handshake(peer)
        requests.append(peer.receive())
        peer.send({
            "operation_id": OPERATION, "state": "recovering",
            "material_state": "recovery_and_prepared", "receipt_state": "none",
            "binding_fingerprint": BINDING_FINGERPRINT,
            "binding_summary": summary,
        })
        requests.append(peer.receive())
        peer.send({
            "operation_id": OPERATION, "state": "restored",
            "material_state": "none", "receipt_state": "present",
            "binding_fingerprint": BINDING_FINGERPRINT,
            "binding_summary": summary,
        })

    client, core_socket, core_thread = make_client(core)
    try:
        inspected = client.credential_discard_recovery(recovery_selector(), "inspect")
        restored = client.credential_discard_recovery(
            recovery_selector(), "restore_old", binding_fingerprint=BINDING_FINGERPRINT)
        assert inspected.binding_fingerprint == BINDING_FINGERPRINT
        assert inspected.binding_summary.provider_id == "openrouter"
        assert inspected.binding_summary.auth_method_id == "api_key"
        assert inspected.safe_dict() == {
            "operation_id": OPERATION, "state": "recovering",
            "material_state": "recovery_and_prepared", "receipt_state": "none",
        }
        assert "binding_summary" not in inspected.safe_dict()
        assert restored.state == "restored"
        assert requests == [
            {
                "type": "credential_discard",
                "target": {"recovery_selector": recovery_selector()},
                "generation": {"nonce": NONCE, "current_generation_fingerprint": GENERATION},
                "action": "inspect",
            },
            {
                "type": "credential_discard",
                "target": {"recovery_selector": recovery_selector(),
                           "binding_fingerprint": BINDING_FINGERPRINT},
                "generation": {"nonce": NONCE, "current_generation_fingerprint": GENERATION},
                "action": "restore_old",
            },
        ]
        with pytest.raises(ValueError, match="binding_fingerprint"):
            client.credential_discard_recovery(recovery_selector(), "cleanup_committed")
        with pytest.raises(ValueError, match="inspect"):
            client.credential_discard_recovery(
                recovery_selector(), "inspect", binding_fingerprint=BINDING_FINGERPRINT)
    finally:
        close_fixture(client, core_socket, core_thread)


@pytest.mark.parametrize("summary", [
    {"provider_id": "openrouter", "auth_method_id": "api_key",
     "config_revision": 4, "auth_grant_revision": 3,
     "public_config_fingerprint": PUBLIC_CONFIG, "unknown": True},
    {"provider_id": "openrouter", "auth_method_id": "api_key",
     "config_revision": True, "auth_grant_revision": 3,
     "public_config_fingerprint": PUBLIC_CONFIG},
])
def test_recovery_binding_summary_parser_rejects_nonclosed_authority(summary):
    from app.services.core_auth_broker import CoreAuthBrokerUnavailable

    def core(peer):
        handshake(peer)
        peer.receive()
        peer.send({
            "operation_id": OPERATION, "state": "recovering",
            "material_state": "recovery", "receipt_state": "none",
            "binding_fingerprint": BINDING_FINGERPRINT,
            "binding_summary": summary,
        })

    client, core_socket, core_thread = make_client(core)
    try:
        with pytest.raises(CoreAuthBrokerUnavailable, match="auth_broker_response_invalid"):
            client.credential_discard_recovery(recovery_selector(), "inspect")
    finally:
        close_fixture(client, core_socket, core_thread)


@pytest.mark.parametrize("payload", [
    {"version": 1, "nonce": NONCE,
     "current_generation_fingerprint": GENERATION, "extra": "unsafe"},
    {"version": True, "nonce": NONCE,
     "current_generation_fingerprint": GENERATION},
])
def test_invalid_handshake_closes_consumed_descriptor(payload):
    from app.services.core_auth_broker import CoreAuthBrokerClient, CoreAuthBrokerUnavailable

    backend, core = socket.socketpair()
    descriptor = backend.detach()
    peer = FramedPeer(core)
    peer.send(payload)
    with pytest.raises(CoreAuthBrokerUnavailable, match="auth_broker_handshake_invalid"):
        CoreAuthBrokerClient.from_fd(descriptor)
    with pytest.raises(OSError):
        os.fstat(descriptor)
    core.close()


def test_login_restore_logout_and_inactive_user_use_only_canonical_db_owner(sqlite_url, monkeypatch):
    from app.main import RuntimeConfig, create_app

    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "owner-authority-test-session")
    requests = []
    handshake_sent = threading.Event()
    backend, core_socket = socket.socketpair()
    descriptor = backend.detach()

    def core():
        peer = FramedPeer(core_socket)
        handshake(peer)
        handshake_sent.set()
        epochs = iter((EPOCH_A, EPOCH_B, EPOCH_C))
        try:
            while True:
                request = peer.receive()
                requests.append(request)
                if request["type"] == "owner_bind":
                    peer.send({"version": 1, "state": "bound",
                               "owner_id": request["owner_id"],
                               "current_owner_epoch": next(epochs)})
                else:
                    peer.send({"version": 1, "state": "unbound"})
        except (EOFError, OSError):
            pass

    worker = threading.Thread(target=core, daemon=True)
    worker.start()
    assert handshake_sent.wait(2)
    config = RuntimeConfig(database_url=sqlite_url,
                           local_session_token="owner-authority-test-session",
                           auth_broker_fd=descriptor)
    headers = {"X-Gamja-Session": "owner-authority-test-session"}
    account = {"email": "owner@example.com", "username": "owner",
               "password": "long-test-password"}
    with TestClient(create_app(runtime_config=config)) as client:
        assert client.post("/api/v1/auth/register", headers=headers, json=account).status_code == 200
        bad = client.post("/api/v1/auth/login", headers=headers,
                          json={**account, "password": "wrong-password",
                                "owner_id": 999, "owner_epoch": "aa" * 32})
        assert bad.status_code == 401
        login = client.post("/api/v1/auth/login", headers=headers,
                            json={**account, "owner_id": 999, "owner_epoch": "aa" * 32})
        assert login.status_code == 200
        assert set(login.json()) == {"access_token", "token_type"}
        token = login.json()["access_token"]
        with client.app.state.session_factory() as db:
            owner_id = db.execute(text("SELECT id FROM users WHERE email=:email"),
                                  {"email": account["email"]}).scalar_one()
            db.execute(text("UPDATE users SET is_active=0 WHERE id=:owner"), {"owner": owner_id})
            db.commit()
        assert client.get("/api/v1/auth/me", headers={**headers,
            "Authorization": f"Bearer {token}"}).status_code == 401
        inactive = client.post("/api/v1/auth/login", headers=headers, json=account)
        assert inactive.status_code == 401
        with client.app.state.session_factory() as db:
            db.execute(text("UPDATE users SET is_active=1 WHERE id=:owner"), {"owner": owner_id})
            credential = "remembered-device-credential"
            db.execute(text("""INSERT INTO remembered_login
                (id,owner_id,secret_hash,expires_at,secret_ref,revision)
                VALUES (1,:owner,:hash,:expiry,'current',1)"""), {
                    "owner": owner_id,
                    "hash": hashlib.sha256(credential.encode()).hexdigest(),
                    "expiry": datetime.utcnow() + timedelta(days=1),
                })
            db.commit()

        class LoginStore:
            def get_no_ui(self, reference):
                assert reference == "current"
                return credential

            def delete(self, _reference):
                pass

        client.app.state.login_secret_store = LoginStore()
        with client.app.state.session_factory() as db:
            db.execute(text("UPDATE users SET is_active=0 WHERE id=:owner"), {"owner": owner_id})
            db.commit()
        assert client.post("/api/v1/auth/restore", headers=headers).status_code == 401
        assert len(requests) == 1
        with client.app.state.session_factory() as db:
            db.execute(text("UPDATE users SET is_active=1 WHERE id=:owner"), {"owner": owner_id})
            db.commit()
        assert client.post("/api/v1/auth/restore", headers=headers,
                           json={"owner_id": 999, "owner_epoch": "aa" * 32}).status_code == 200
        unlock_id = "interactive-restore-request"
        client.app.state.login_unlock_id = unlock_id
        client.app.state.credential_cache.put(("remembered-login", unlock_id), credential)
        interactive = client.get("/api/v1/auth/restore/interactive", headers=headers,
                                 params={"unlock_id": unlock_id})
        assert interactive.status_code == 200
        assert interactive.json()["state"] == "ready"
        from fastapi import HTTPException
        from app.api.routes import auth as auth_routes
        with monkeypatch.context() as failure_patch:
            failure_patch.setattr(
                auth_routes, "revoke",
                lambda *_args: (_ for _ in ()).throw(HTTPException(409, "synthetic revoke conflict")),
            )
            failed_logout = client.post(
                "/api/v1/auth/logout", headers=headers,
                json={"owner_id": 999, "owner_epoch": "aa" * 32},
            )
        assert failed_logout.status_code == 409
        assert client.app.state.owner_epoch_provider.current(owner_id) is None
        assert client.post("/api/v1/auth/logout", headers=headers).status_code == 204

    worker.join(2)
    assert not worker.is_alive()
    assert requests == [
        {"type": "owner_bind", "version": 1, "owner_id": owner_id},
        {"type": "owner_bind", "version": 1, "owner_id": owner_id},
        {"type": "owner_bind", "version": 1, "owner_id": owner_id},
        {"type": "owner_unbind", "version": 1,
         "expected_owner_id": owner_id, "expected_owner_epoch": EPOCH_C},
    ]


def test_logout_keeps_204_when_unbind_result_is_ambiguous(sqlite_url, monkeypatch):
    from app.main import RuntimeConfig, create_app

    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "ambiguous-logout-session")
    handshake_sent = threading.Event()
    backend, core_socket = socket.socketpair()
    descriptor = backend.detach()

    def core():
        peer = FramedPeer(core_socket)
        handshake(peer)
        handshake_sent.set()
        request = peer.receive()
        assert request["type"] == "owner_bind"
        peer.send({"version": 1, "state": "bound", "owner_id": request["owner_id"],
                   "current_owner_epoch": EPOCH_A})
        assert peer.receive()["type"] == "owner_unbind"
        peer.channel.close()

    worker = threading.Thread(target=core, daemon=True)
    worker.start()
    assert handshake_sent.wait(2)
    config = RuntimeConfig(database_url=sqlite_url,
                           local_session_token="ambiguous-logout-session",
                           auth_broker_fd=descriptor)
    headers = {"X-Gamja-Session": "ambiguous-logout-session"}
    account = {"email": "ambiguous@example.com", "username": "ambiguous",
               "password": "long-test-password"}
    with TestClient(create_app(runtime_config=config)) as client:
        assert client.post("/api/v1/auth/register", headers=headers, json=account).status_code == 200
        assert client.post("/api/v1/auth/login", headers=headers, json=account).status_code == 200
        assert client.post("/api/v1/auth/logout", headers=headers).status_code == 204
        assert client.app.state.owner_epoch_provider.current(1) is None
        assert client.app.state.auth_broker_client.available is False

    worker.join(2)
    assert not worker.is_alive()


def test_runtime_config_reads_auth_fd_without_import_side_effect(monkeypatch):
    import importlib
    import app.main as main_module
    from app.services import core_auth_broker

    consumed = []
    monkeypatch.setenv("GAMJA_AUTH_BROKER_FD", "42")
    monkeypatch.setattr(core_auth_broker.CoreAuthBrokerClient, "from_fd",
                        lambda descriptor: consumed.append(descriptor))
    importlib.reload(main_module)
    assert consumed == []
    config = main_module.RuntimeConfig.from_environment("sqlite://")
    assert config.auth_broker_fd == 42
    assert consumed == []


def test_desktop_runner_consumes_and_scrubs_auth_fd(monkeypatch, tmp_path):
    import run
    import uvicorn
    from fastapi.testclient import TestClient

    backend, core_socket = socket.socketpair()
    descriptor = backend.detach()
    worker_done = threading.Event()

    def core():
        peer = FramedPeer(core_socket)
        handshake(peer)
        try:
            peer.receive()
        except (EOFError, OSError):
            pass
        finally:
            worker_done.set()

    worker = threading.Thread(target=core, daemon=True)
    worker.start()
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "runner-owner-authority-session-token")
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("GAMJA_PORT", "8765")
    monkeypatch.setenv("GAMJA_AUTH_BROKER_FD", str(descriptor))

    def inspect_app(app, **_kwargs):
        assert "GAMJA_AUTH_BROKER_FD" not in os.environ
        assert app.state.auth_broker_client.available is True
        with TestClient(app) as client:
            assert client.get("/health", headers={
                "X-Gamja-Session": "runner-owner-authority-session-token",
            }).status_code == 200

    monkeypatch.setattr(uvicorn, "run", inspect_app)
    run.main()
    worker.join(2)
    assert worker_done.is_set()


def test_python_client_interoperates_with_real_rust_core_fixture():
    from pathlib import Path
    from app.services.core_auth_broker import CoreAuthBrokerClient, TrustedOwnerEpochProvider

    repository = Path(__file__).resolve().parents[2]
    binary = repository / "core-host/target/debug/examples/auth_broker_fixture"
    if not binary.is_file():
        built = subprocess.run(
            ["cargo", "build", "--manifest-path", str(repository / "core-host/Cargo.toml"),
             "--example", "auth_broker_fixture"],
            cwd=repository, capture_output=True, timeout=120, check=False,
        )
        assert built.returncode == 0, built.stderr.decode(errors="replace")

    backend, core = socket.socketpair()
    environment = os.environ.copy()
    environment["GAMJA_AUTH_BROKER_FIXTURE_FD"] = str(core.fileno())
    process = subprocess.Popen(
        [str(binary)], cwd=repository, env=environment, pass_fds=(core.fileno(),),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    core.close()
    client = None
    try:
        client = CoreAuthBrokerClient.from_fd(backend.detach())
        provider = TrustedOwnerEpochProvider(client)
        first = provider.bind(7)
        assert first is not None and provider.current(7) == first.owner_epoch
        exact = client.auth_start(stable_binding(owner_id=7, owner_epoch=first.owner_epoch))
        assert exact.safe_dict() == {
            "version": 1, "operation_id": OPERATION, "state": "failed",
            "code": "auth_method_unavailable",
        }
        selected = client.credential_discard_recovery(recovery_selector(owner_id=7), "inspect")
        assert selected.safe_dict() == {
            "operation_id": OPERATION, "state": "absent",
            "material_state": "none", "receipt_state": "none",
        }
        assert selected.binding_fingerprint is None
        second = provider.bind(8)
        assert second is not None and second.owner_epoch != first.owner_epoch
        assert provider.compare_and_unbind(first) == "mismatch_preserved"
        assert provider.current(8) == second.owner_epoch
        assert provider.compare_and_unbind(second) == "unbound"
        assert provider.current(8) is None
        client.close()
        output, error = process.communicate(timeout=5)
        assert process.returncode == 0
        assert output == b"" and error == b""
    finally:
        if client is not None:
            client.close()
        try:
            backend.close()
        except OSError:
            pass
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
