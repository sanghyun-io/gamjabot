import importlib.util
from io import BytesIO
import json
import os
import plistlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from urllib.error import HTTPError


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gamja_cli.py"
spec = importlib.util.spec_from_file_location("gamja_cli", SCRIPT)
gamja_cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gamja_cli)
from app import owner_cli


def test_cli_capability_requires_private_regular_file(tmp_path):
    path = tmp_path / "owner-cli.json"
    data = {"base_url": "http://127.0.0.1:8100", "capability": "a" * 64}
    path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(path, 0o600)
    assert gamja_cli.capability(path) == data
    os.chmod(path, 0o640)
    with pytest.raises(RuntimeError, match="unavailable"):
        gamja_cli.capability(path)


def test_cli_never_accepts_non_loopback_or_extra_capability_fields(tmp_path):
    path = tmp_path / "owner-cli.json"
    path.write_text(json.dumps({"base_url": "http://example.test:8100", "capability": "a" * 64}), encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(RuntimeError, match="unavailable"):
        gamja_cli.capability(path)
    path.write_text(json.dumps({"base_url": "http://owner@127.0.0.1:8100", "capability": "a" * 64}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="unavailable"):
        gamja_cli.capability(path)


def test_cli_never_follows_redirects():
    assert gamja_cli.NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://example.test") is None


@pytest.mark.parametrize("detail", [
    "sentinel_password",
    {"message": "sentinel_password"},
    [{"loc": ["body", "password"], "input": "sentinel_password"}],
])
def test_cli_http_errors_never_echo_server_detail(monkeypatch, detail):
    body = json.dumps({"detail": detail}).encode()

    class FailingOpener:
        def open(self, *_args, **_kwargs):
            raise HTTPError("http://127.0.0.1:8100", 422, "Unprocessable", {}, BytesIO(body))

    monkeypatch.setattr(owner_cli, "build_opener", lambda *_: FailingOpener())
    client = owner_cli.Client({"base_url": "http://127.0.0.1:8100", "capability": "a" * 64})
    with pytest.raises(RuntimeError) as error:
        client.call("POST", "/auth/login", {"password": "sentinel_password"})
    assert str(error.value) == "Request failed (422)"
    assert "sentinel_password" not in str(error.value)


def test_client_internal_timeout_preserves_default_and_bounds_poll_response(monkeypatch):
    opened = []
    reads = []
    socket_timeouts = []
    clock = [0.0]

    class Socket:
        def settimeout(self, timeout):
            socket_timeouts.append(timeout)

    class Response:
        fp = SimpleNamespace(raw=SimpleNamespace(_sock=Socket()))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, amount=None):
            reads.append(amount)
            if amount is not None:
                clock[0] += 1
            return b"{}"

    class Opener:
        def open(self, _request, timeout):
            opened.append(timeout)
            if timeout != 65:
                clock[0] += 1
            return Response()

    monkeypatch.setattr(owner_cli, "build_opener", lambda *_args: Opener())
    monkeypatch.setattr(owner_cli.time, "monotonic", lambda: clock[0])
    client = owner_cli.Client({"base_url": "http://127.0.0.1:8100", "capability": "a" * 64})

    assert client.call("GET", "/health") == {}
    assert client.call("GET", "/connections/7/auth/operations/" + "a" * 64,
                       request_timeout=5) == {}

    assert opened == [65, 5]
    assert reads == [None, owner_cli.BOUNDED_RESPONSE_BYTES + 1]
    assert socket_timeouts == [4]


def test_cli_restore_failure_accepts_only_fixed_code_and_correlation(monkeypatch):
    correlation = "a" * 32

    class FailingOpener:
        def open(self, *_args, **_kwargs):
            body = json.dumps({'detail': {'code': 'credential_locked', 'correlation_id': correlation,
                                          'message': 'sentinel_password'}}).encode()
            raise HTTPError("http://127.0.0.1:8100", 503, "Unavailable", {}, BytesIO(body))

    monkeypatch.setattr(owner_cli, "build_opener", lambda *_: FailingOpener())
    client = owner_cli.Client({"base_url": "http://127.0.0.1:8100", "capability": "a" * 64})
    with pytest.raises(owner_cli.RestoreFailure) as error:
        client.call("POST", "/auth/restore")
    assert error.value.code == "credential_locked"
    assert error.value.correlation_id == correlation
    assert "sentinel_password" not in str(error.value)


def test_cli_provider_failure_accepts_only_fixed_code_and_correlation(monkeypatch):
    correlation = "b" * 32

    class FailingOpener:
        def open(self, *_args, **_kwargs):
            body = json.dumps({'detail': {
                'code': 'secret_store_unavailable', 'correlation_id': correlation,
                'message': 'sentinel-provider-secret'}}).encode()
            raise HTTPError("http://127.0.0.1:8100", 503, "Unavailable", {}, BytesIO(body))

    monkeypatch.setattr(owner_cli, "build_opener", lambda *_: FailingOpener())
    client = owner_cli.Client({"base_url": "http://127.0.0.1:8100", "capability": "a" * 64})
    with pytest.raises(owner_cli.CredentialStoreFailure) as error:
        client.call("POST", "/connections", {"api_key": "sentinel-provider-secret"})
    assert error.value.correlation_id == correlation
    assert "sentinel-provider-secret" not in str(error.value)


def test_cli_clone_failure_reports_only_validated_unavailable_agent_ids(monkeypatch):
    class FailingOpener:
        def open(self, *_args, **_kwargs):
            body = json.dumps({"detail": {"code": "room_clone_members_unavailable",
                                           "agent_ids": [2, 7]}}).encode()
            raise HTTPError("http://127.0.0.1:8100", 409, "Conflict", {}, BytesIO(body))

    monkeypatch.setattr(owner_cli, "build_opener", lambda *_: FailingOpener())
    client = owner_cli.Client({"base_url": "http://127.0.0.1:8100", "capability": "a" * 64})
    with pytest.raises(RuntimeError, match="Room clone blocked; unavailable Agent IDs: 2, 7"):
        client.call("POST", "/conversations/5/clone", {"project_id": 4, "title": "Fresh"})


def test_cli_clone_failure_rejects_malformed_server_detail_without_echoing_it(monkeypatch):
    class FailingOpener:
        def open(self, *_args, **_kwargs):
            body = json.dumps({"detail": {"code": "room_clone_members_unavailable",
                                           "agent_ids": [2, "sentinel-secret"]}}).encode()
            raise HTTPError("http://127.0.0.1:8100", 409, "Conflict", {}, BytesIO(body))

    monkeypatch.setattr(owner_cli, "build_opener", lambda *_: FailingOpener())
    client = owner_cli.Client({"base_url": "http://127.0.0.1:8100", "capability": "a" * 64})
    with pytest.raises(RuntimeError) as error:
        client.call("POST", "/conversations/5/clone", {"project_id": 4, "title": "Fresh"})
    assert str(error.value) == "Request failed (409)"
    assert "sentinel-secret" not in str(error.value)


@pytest.mark.parametrize(("detail", "message"), [
    ({"code": "room_launch_idempotency_conflict"},
     "Room launch key was already used for different input"),
    ({"code": "room_launch_agents_unavailable", "agent_ids": [3, 8]},
     "Room launch blocked; unavailable Agent IDs: 3, 8"),
])
def test_cli_room_launch_failure_uses_only_validated_safe_detail(monkeypatch, detail, message):
    class FailingOpener:
        def open(self, *_args, **_kwargs):
            raise HTTPError("http://127.0.0.1:8100", 409, "Conflict", {},
                            BytesIO(json.dumps({"detail": detail}).encode()))

    monkeypatch.setattr(owner_cli, "build_opener", lambda *_: FailingOpener())
    client = owner_cli.Client({"base_url": "http://127.0.0.1:8100", "capability": "a" * 64})
    with pytest.raises(RuntimeError, match=re.escape(message)):
        client.call("POST", "/conversations/launch", {"title": "safe"})


def test_cli_room_launch_failure_does_not_echo_malformed_detail(monkeypatch):
    class FailingOpener:
        def open(self, *_args, **_kwargs):
            body = json.dumps({"detail": {"code": "room_launch_agents_unavailable",
                                           "agent_ids": [3, "sentinel-secret"]}}).encode()
            raise HTTPError("http://127.0.0.1:8100", 409, "Conflict", {}, BytesIO(body))

    monkeypatch.setattr(owner_cli, "build_opener", lambda *_: FailingOpener())
    client = owner_cli.Client({"base_url": "http://127.0.0.1:8100", "capability": "a" * 64})
    with pytest.raises(RuntimeError) as error:
        client.call("POST", "/conversations/launch", {"title": "safe"})
    assert str(error.value) == "Request failed (409)"
    assert "sentinel-secret" not in str(error.value)


@pytest.mark.parametrize(("detail", "message"), [
    ({"code": "project_archive_plan_stale"}, "Project archive plan is stale; preview it again"),
    ({"code": "project_restore_active_children", "active_rooms": 3},
     "Archived project still has 3 active room(s); preview and apply archive cleanup first"),
])
def test_cli_project_archive_failure_uses_only_validated_safe_detail(monkeypatch, detail, message):
    class FailingOpener:
        def open(self, *_args, **_kwargs):
            raise HTTPError("http://127.0.0.1:8100", 409, "Conflict", {},
                            BytesIO(json.dumps({"detail": detail}).encode()))
    monkeypatch.setattr(owner_cli, "build_opener", lambda *_: FailingOpener())
    client = owner_cli.Client({"base_url": "http://127.0.0.1:8100", "capability": "a" * 64})
    with pytest.raises(RuntimeError, match=re.escape(message)):
        client.call("POST", "/projects/7/archive", {"plan_hash": "a" * 64})


def test_cli_template_failure_reports_only_validated_unavailable_agent_ids(monkeypatch):
    class FailingOpener:
        def open(self, *_args, **_kwargs):
            body = json.dumps({"detail": {"code": "room_template_members_unavailable",
                                           "agent_ids": [3, 8]}}).encode()
            raise HTTPError("http://127.0.0.1:8100", 409, "Conflict", {}, BytesIO(body))

    monkeypatch.setattr(owner_cli, "build_opener", lambda *_: FailingOpener())
    client = owner_cli.Client({"base_url": "http://127.0.0.1:8100", "capability": "a" * 64})
    with pytest.raises(RuntimeError, match="Room template blocked; unavailable Agent IDs: 3, 8"):
        client.call("POST", "/projects/4/room-templates/7/rooms", {})


@pytest.mark.parametrize("detail", [
    {
        "correlation_id": "a" * 32,
        "stage": "auth_restore",
        "code": "credential_locked",
        "reason": "keychain_interaction_required",
        "error_class": "secret_interaction_required",
        "os_status": -25308,
    },
    {
        "correlation_id": "a" * 32,
        "stage": "auth_restore",
        "code": "secret_store_unavailable",
        "reason": "keychain_query_failed",
        "error_class": "secret_store_unavailable",
    },
])
def test_cli_prints_only_validated_restore_diagnostic(monkeypatch, capsys, detail):
    correlation = "a" * 32
    client = SimpleNamespace(call=lambda *_args, **_kwargs: detail)
    monkeypatch.setattr(owner_cli.desktop_diagnostics, "app_status", lambda *_: pytest.fail("must not fall back"))

    owner_cli.diagnostics(correlation, client)

    assert json.loads(capsys.readouterr().out) == detail


@pytest.mark.parametrize("reason", ["keychain_query_failed", "credential_channel_unavailable",
                                    "credential_response_invalid", "credential_request_invalid"])
def test_cli_prints_only_validated_remember_diagnostic(monkeypatch, capsys, reason):
    correlation = "c" * 32
    detail = {
        "correlation_id": correlation,
        "stage": "auth_remember",
        "code": "secret_store_unavailable",
        "reason": reason,
        "error_class": "native_os_status",
        "operation": "sec_item_add",
        "os_status": -25293,
        "cleanup": "success",
    }

    class Client:
        def call(self, _method, path, **kwargs):
            if path.startswith('/auth/restore/'):
                raise RuntimeError('Request failed (404)')
            if path.startswith('/connections/'):
                raise RuntimeError('Request failed (404)')
            return detail

    monkeypatch.setattr(owner_cli.desktop_diagnostics, "app_status", lambda *_: pytest.fail("must not fall back"))
    owner_cli.diagnostics(correlation, Client())
    assert json.loads(capsys.readouterr().out) == detail


@pytest.mark.parametrize(("mutate", "value"), [
    ("missing_cleanup", None),
    ("invalid_cleanup", "maybe"),
    ("extra", "sentinel_secret"),
    ("correlation_id", "sentinel_secret"),
    ("stage", "auth_restore"),
    ("code", "sentinel_secret"),
    ("reason", "sentinel_secret"),
    ("error_class", "sentinel_secret"),
    ("operation", "invalid_operation"),
    ("os_status", True),
    ("os_status", 2 ** 31),
    ("os_status", "sentinel_secret"),
])
def test_cli_rejects_malformed_remember_diagnostic_without_leaking_server_values(
        monkeypatch, capsys, mutate, value):
    correlation = "c" * 32
    detail = {
        "correlation_id": correlation,
        "stage": "auth_remember",
        "code": "secret_store_unavailable",
        "reason": "keychain_query_failed",
        "error_class": "native_os_status",
        "operation": "sec_item_add",
        "os_status": -25293,
        "cleanup": "success",
    }
    if mutate == "missing_cleanup":
        detail.pop("cleanup")
    elif mutate == "extra":
        detail["extra"] = value
    elif mutate == "stage":
        detail["stage"] = value
    else:
        detail[mutate] = value
    client = SimpleNamespace(call=lambda *_args, **_kwargs: detail)
    monkeypatch.setattr(owner_cli.desktop_diagnostics, "app_status", lambda *_: {"state": "fallback"})
    monkeypatch.setattr(owner_cli.desktop_diagnostics, "database_status", lambda *_: {"state": "fallback"})
    monkeypatch.setattr(owner_cli.desktop_diagnostics, "error_details", lambda *_: [])

    owner_cli.diagnostics(correlation, client)

    output = capsys.readouterr().out
    assert json.loads(output)["app"] == {"state": "fallback"}
    assert "sentinel_secret" not in output


def test_cli_prints_only_validated_provider_credential_diagnostic(monkeypatch, capsys):
    correlation = "b" * 32
    detail = {
        "correlation_id": correlation,
        "stage": "provider_credential",
        "code": "secret_store_unavailable",
        "reason": "keychain_query_failed",
        "error_class": "native_os_status",
        "operation": "sec_item_add",
        "os_status": -25293,
    }

    class Client:
        def call(self, _method, path, **kwargs):
            if path.startswith('/auth/'):
                raise RuntimeError('Request failed (404)')
            assert kwargs == {'authenticated': True}
            return detail

    monkeypatch.setattr(owner_cli.desktop_diagnostics, "app_status", lambda *_: pytest.fail("must not fall back"))
    owner_cli.diagnostics(correlation, Client())
    assert json.loads(capsys.readouterr().out) == detail


@pytest.mark.parametrize(("mutate", "value"), [
    ("missing_reason", None),
    ("extra", "sentinel_secret"),
    ("correlation_id", "sentinel_secret"),
    ("stage", "sentinel_secret"),
    ("code", "sentinel_secret"),
    ("reason", "sentinel_secret"),
    ("error_class", "sentinel_secret"),
    ("mismatched_class", "secret_store_unavailable"),
    ("os_status", True),
    ("os_status", 2 ** 31),
    ("os_status", "sentinel_secret"),
])
def test_cli_rejects_malformed_restore_diagnostic_without_leaking_server_values(
        monkeypatch, capsys, mutate, value):
    correlation = "a" * 32
    detail = {
        "correlation_id": correlation,
        "stage": "auth_restore",
        "code": "credential_locked",
        "reason": "keychain_interaction_required",
        "error_class": "secret_interaction_required",
    }
    if mutate == "missing_reason":
        detail.pop("reason")
    elif mutate == "extra":
        detail["extra"] = value
    elif mutate == "mismatched_class":
        detail["error_class"] = value
    else:
        detail[mutate] = value
    client = SimpleNamespace(call=lambda *_args, **_kwargs: detail)
    monkeypatch.setattr(owner_cli.desktop_diagnostics, "app_status", lambda *_: {"state": "fallback"})
    monkeypatch.setattr(owner_cli.desktop_diagnostics, "database_status", lambda *_: {"state": "fallback"})
    monkeypatch.setattr(owner_cli.desktop_diagnostics, "error_details", lambda *_: [])

    owner_cli.diagnostics(correlation, client)

    output = capsys.readouterr().out
    assert json.loads(output)["app"] == {"state": "fallback"}
    assert "sentinel_secret" not in output


def test_cli_login_preserves_desktop_remembered_login(monkeypatch):
    client = gamja_cli.Client({"base_url": "http://127.0.0.1:8100", "capability": "a" * 64})
    sent = {}
    monkeypatch.setattr(owner_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "owner@example.com")
    monkeypatch.setattr(gamja_cli.getpass, "getpass", lambda _: "not-recorded")
    monkeypatch.setattr(client, "call", lambda method, path, body: sent.update(
        method=method, path=path, body=body) or {"access_token": "memory-only"})
    client.login()
    assert client.token == "memory-only"
    assert sent == {"method": "POST", "path": "/auth/login", "body": {
        "email": "owner@example.com", "password": "not-recorded", "remember_device": False,
        "preserve_remembered_login": True,
    }}


def test_cli_login_remember_device_is_explicit_and_keeps_password_private(monkeypatch, capsys):
    from io import StringIO

    monkeypatch.setattr(owner_cli.sys, "stdin", StringIO("not-recorded\n"))
    sent = []
    client = SimpleNamespace(token=None, call=lambda method, path, body: sent.append(
        (method, path, body)) or {"access_token": "memory-only"})

    owner_cli.command(client, ["login", "--remember-device", "--secret-stdin"],
                      input_body={"email": "owner@example.com"})

    assert sent == [("POST", "/auth/login", {
        "email": "owner@example.com", "password": "not-recorded", "remember_device": True,
        "preserve_remembered_login": False,
    })]
    assert client.token == "memory-only"
    output = capsys.readouterr().out
    assert json.loads(output) == {"authenticated": True}
    assert "not-recorded" not in output and "memory-only" not in output


def test_remember_device_is_rejected_outside_login_before_transport():
    client = SimpleNamespace(call=lambda *_args, **_kwargs: pytest.fail("transport must not run"))
    with pytest.raises(RuntimeError, match="Remember-device"):
        owner_cli.command(client, ["me", "--remember-device"])


def test_serve_refuses_legacy_backend_lock_without_starting(monkeypatch, tmp_path):
    monkeypatch.setattr(owner_cli, "legacy_serve_is_supported", lambda: True)
    monkeypatch.setattr(owner_cli, "capability", lambda _: (_ for _ in ()).throw(RuntimeError("missing")))
    monkeypatch.setattr(owner_cli, "_backend_lock_held", lambda _: True)
    monkeypatch.setattr(owner_cli.subprocess, "Popen", lambda *_: pytest.fail("must not start a second engine"))
    with pytest.raises(RuntimeError, match="does not expose"):
        owner_cli.serve(tmp_path, tmp_path / "Gamja Bot")


def test_frozen_cli_uses_host_from_its_own_bundle(monkeypatch, tmp_path):
    macos = tmp_path / "Candidate.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    backend = macos / "backend"
    host = macos / "Renamed Gamja"
    backend.touch()
    host.touch()
    with (macos.parent / "Info.plist").open("wb") as destination:
        plistlib.dump({"CFBundleExecutable": host.name}, destination)
    monkeypatch.setattr(owner_cli.sys, "frozen", True, raising=False)
    monkeypatch.setattr(owner_cli.sys, "executable", str(backend))
    assert owner_cli.default_headless_binary() == host


def test_stop_owned_process_terminates_actual_child():
    process = owner_cli.subprocess.Popen([owner_cli.sys.executable, "-c", "import time; time.sleep(30)"])
    owner_cli._stop_owned_process(process)
    assert process.poll() is not None


def test_serve_isolates_child_and_publishes_parent_lifeline(monkeypatch, tmp_path):
    binary = tmp_path / "Gamja Bot"
    binary.touch()
    launched = {}
    monkeypatch.setattr(owner_cli, "legacy_serve_is_supported", lambda: True)

    class Process:
        def poll(self):
            return None

        def wait(self, **_kwargs):
            return 0

    class ReadyClient:
        def __init__(self, _data):
            pass

        def call(self, method, path):
            assert (method, path) == ("GET", "/health")
            return {"status": "healthy"}

    attempts = iter([RuntimeError("unavailable"), {"base_url": "http://127.0.0.1:8100", "capability": "a" * 64}])

    def next_capability(_):
        value = next(attempts)
        if isinstance(value, RuntimeError):
            raise value
        return value

    monkeypatch.setattr(owner_cli, "capability", next_capability)
    monkeypatch.setattr(owner_cli, "Client", ReadyClient)
    monkeypatch.setattr(owner_cli, "_backend_lock_held", lambda _: False)
    monkeypatch.setattr(owner_cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(owner_cli.subprocess, "Popen", lambda *args, **kwargs: launched.update(
        args=args, kwargs=kwargs) or Process())
    owner_cli.serve(tmp_path, binary)
    assert launched["kwargs"]["start_new_session"] is True
    assert launched["kwargs"]["env"]["GAMJA_HEADLESS_OWNER_PID"] == str(os.getpid())


@pytest.mark.parametrize(
    ("system", "machine", "supported"),
    [("darwin", "arm64", False), ("darwin", "x86_64", True), ("win32", "AMD64", True)],
)
def test_private_legacy_serve_platform_gate(monkeypatch, system, machine, supported):
    monkeypatch.setattr(owner_cli.sys, "platform", system)
    monkeypatch.setattr(owner_cli.platform, "machine", lambda: machine)
    assert owner_cli.legacy_serve_is_supported() is supported


def test_private_legacy_serve_on_mac_arm_uses_public_cli_instruction(monkeypatch, tmp_path):
    monkeypatch.setattr(owner_cli, "legacy_serve_is_supported", lambda: False)
    monkeypatch.setattr(owner_cli, "capability", lambda _: pytest.fail("guard must run before endpoint access"))
    monkeypatch.setattr(owner_cli.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("guard must not launch"))

    with pytest.raises(RuntimeError, match=r"use public gamja"):
        owner_cli.serve(tmp_path, None)


def test_provider_catalog_and_cached_models_use_only_canonical_reads(capsys):
    calls = []
    responses = iter((
        {"version": 1, "providers": []},
        {"observed_at": None, "stale": True, "truncated": False, "models": []},
        [{"id": 7}, {"id": 9}],
        {"observed_at": None, "stale": True, "truncated": False, "models": []},
        {"observed_at": None, "stale": False, "truncated": False,
         "models": [{"id": "configured", "label": "Configured", "source": "configured"}]},
    ))

    def dispatch(method, path, body=None, **kwargs):
        calls.append((method, path, body, kwargs))
        return next(responses)

    client = SimpleNamespace(call=dispatch)
    owner_cli.command(client, ["provider", "catalog"])
    owner_cli.command(client, ["provider", "models", "7"])
    owner_cli.command(client, ["provider", "models"])

    assert calls == [
        ("GET", "/connections/catalog", None, {"authenticated": True}),
        ("GET", "/connections/7/models", None, {"authenticated": True}),
        ("GET", "/connections", None, {"authenticated": True}),
        ("GET", "/connections/7/models", None, {"authenticated": True}),
        ("GET", "/connections/9/models", None, {"authenticated": True}),
    ]
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output[0] == {"version": 1, "providers": []}
    assert output[1]["stale"] is True
    assert output[2] == {"connections": [
        {"connection_id": 7, "observed_at": None, "stale": True,
         "truncated": False, "models": []},
        {"connection_id": 9, "observed_at": None, "stale": False,
         "truncated": False,
         "models": [{"id": "configured", "label": "Configured", "source": "configured"}]},
    ]}


def test_provider_models_refresh_uses_current_revision_snapshot(capsys):
    calls = []

    def dispatch(method, path, body=None, **kwargs):
        calls.append((method, path, body, kwargs))
        if path.endswith("/revisions"):
            return {"version": 1, "config_revision": 4, "auth_grant_revision": 2}
        return {"observed_at": "2026-10-07T00:00:00Z", "stale": False,
                "truncated": False, "models": []}

    owner_cli.command(SimpleNamespace(call=dispatch),
                      ["provider", "models", "7", "--refresh"])

    assert calls == [
        ("GET", "/connections/7/revisions", None, {"authenticated": True}),
        ("POST", "/connections/7/models/refresh",
         {"version": 1, "config_revision": 4, "auth_grant_revision": 2},
         {"authenticated": True}),
    ]
    assert json.loads(capsys.readouterr().out)["stale"] is False


def test_provider_disconnect_uses_versioned_lifecycle_contract(monkeypatch, capsys):
    calls = []
    keys = iter(("1" * 32, "2" * 32))
    monkeypatch.setattr(owner_cli, "uuid4", lambda: SimpleNamespace(hex=next(keys)))
    client = SimpleNamespace(call=lambda method, path, body=None, **kwargs:
                             calls.append((method, path, body, kwargs)) or {
                                 "credential_state": "missing", "revoke_state": "skipped"})

    owner_cli.command(client, ["provider", "disconnect", "7"])
    owner_cli.command(client, ["provider", "disconnect", "8", "--revoke"])

    assert calls == [
        ("POST", "/connections/7/disconnect",
         {"version": 1, "revoke_remote": False},
         {"authenticated": True, "idempotency_key": "1" * 32}),
        ("POST", "/connections/8/disconnect",
         {"version": 1, "revoke_remote": True},
         {"authenticated": True, "idempotency_key": "2" * 32}),
    ]
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [
        {"credential_state": "missing", "revoke_state": "skipped"},
        {"credential_state": "missing", "revoke_state": "skipped"},
    ]


def test_provider_disconnect_polls_same_closed_operation_to_terminal(monkeypatch, capsys):
    operation_id = "a" * 64
    responses = iter((
        {"version": 1, "operation_id": operation_id, "state": "pending",
         "expires_at": None, "poll_interval": 2},
        {"version": 1, "operation_id": operation_id, "state": "finalizing",
         "expires_at": None, "poll_interval": 1},
        {"version": 1, "operation_id": operation_id, "state": "succeeded",
         "result": {"credential_state": "missing", "revoke_state": "revoked"}},
    ))
    calls = []
    client = SimpleNamespace(call=lambda method, path, body=None, **kwargs:
                             calls.append((method, path, body, kwargs)) or next(responses))
    sleeps = []
    monkeypatch.setattr(owner_cli.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(owner_cli.time, "sleep", sleeps.append)
    monkeypatch.setattr(owner_cli, "uuid4", lambda: SimpleNamespace(hex="3" * 32))

    owner_cli.command(client, ["provider", "disconnect", "7", "--revoke"])

    path = f"/connections/7/auth/operations/{operation_id}"
    assert calls == [
        ("POST", "/connections/7/disconnect",
         {"version": 1, "revoke_remote": True},
         {"authenticated": True, "idempotency_key": "3" * 32}),
        ("GET", path, None, {"authenticated": True, "request_timeout": 30.0}),
        ("GET", path, None, {"authenticated": True, "request_timeout": 30.0}),
    ]
    assert sleeps == [2, 1]
    assert json.loads(capsys.readouterr().out) == {
        "version": 1, "operation_id": operation_id, "state": "succeeded",
        "result": {"credential_state": "missing", "revoke_state": "revoked"},
    }


@pytest.mark.parametrize("response", [
    {"version": 1, "operation_id": "9" * 64, "state": "succeeded",
     "result": {"credential_state": "missing", "revoke_state": "skipped"}},
    {"version": 1, "operation_id": "9" * 64, "state": "failed",
     "result": {"credential_state": "present", "revoke_state": "skipped"},
     "error_code": "credential_recovery_required"},
])
def test_provider_disconnect_post_rejects_versioned_terminal_without_stdout(response, capsys):
    client = SimpleNamespace(call=lambda *_args, **_kwargs: response)

    with pytest.raises(RuntimeError, match="Canonical provider operation response is invalid"):
        owner_cli.command(client, ["provider", "disconnect", "7"])

    assert capsys.readouterr().out == ""


def test_provider_disconnect_poll_passes_remaining_budget_and_ignores_late_result(
        monkeypatch, capsys):
    operation_id = "8" * 64
    pending = {"version": 1, "operation_id": operation_id, "state": "pending",
               "expires_at": None, "poll_interval": 1}
    clock = [0.0]
    calls = []

    def dispatch(method, path, body=None, **kwargs):
        calls.append((method, path, body, kwargs))
        if method == "POST":
            return dict(pending)
        timeout = kwargs["request_timeout"]
        assert 0 < timeout <= owner_cli.DISCONNECT_WAIT_SECONDS - 1
        clock[0] += timeout + 1
        return {"version": 1, "operation_id": operation_id, "state": "succeeded",
                "result": {"credential_state": "missing", "revoke_state": "skipped"}}

    monkeypatch.setattr(owner_cli.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(owner_cli.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    client = SimpleNamespace(call=dispatch)

    owner_cli.command(client, ["provider", "disconnect", "7"])

    assert json.loads(capsys.readouterr().out) == pending
    assert [method for method, *_rest in calls] == ["POST", "GET"]
    assert all(method != "DELETE" for method, *_rest in calls)


def test_provider_disconnect_poll_timeout_is_fixed_and_never_auto_cancels(monkeypatch, capsys):
    operation_id = "7" * 64
    pending = {"version": 1, "operation_id": operation_id, "state": "pending",
               "expires_at": None, "poll_interval": 1}
    calls = []

    def dispatch(method, path, body=None, **kwargs):
        calls.append((method, path, body, kwargs))
        if method == "POST":
            return dict(pending)
        raise RuntimeError("Running backend is unavailable")

    monkeypatch.setattr(owner_cli.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(owner_cli.time, "sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match="^Running backend is unavailable$") as failure:
        owner_cli.command(SimpleNamespace(call=dispatch), ["provider", "disconnect", "7"])

    assert "sentinel" not in str(failure.value)
    assert capsys.readouterr().out == ""
    assert all(method != "DELETE" for method, *_rest in calls)


def test_provider_disconnect_bounded_wait_and_ctrl_c_leave_last_pending_without_cancel(
        monkeypatch, capsys):
    operation_id = "b" * 64
    pending = {"version": 1, "operation_id": operation_id, "state": "pending",
               "expires_at": None, "poll_interval": 300}
    calls = []
    client = SimpleNamespace(call=lambda method, path, body=None, **kwargs:
                             calls.append((method, path, body, kwargs)) or dict(pending))
    clock = [0.0]
    monkeypatch.setattr(owner_cli.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(owner_cli.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    owner_cli.command(client, ["provider", "disconnect", "7"])
    assert json.loads(capsys.readouterr().out) == pending
    assert all(method != "DELETE" for method, *_rest in calls)
    assert clock[0] <= owner_cli.DISCONNECT_WAIT_SECONDS

    calls.clear()
    clock[0] = 0.0
    monkeypatch.setattr(
        owner_cli.time, "sleep",
        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()))
    owner_cli.command(client, ["provider", "disconnect", "7"])
    assert json.loads(capsys.readouterr().out) == pending
    assert all(method != "DELETE" for method, *_rest in calls)


def test_provider_auth_status_and_cancel_require_confirmed_closed_truth(monkeypatch, capsys):
    operation_id = "c" * 64
    cancelled = {
        "version": 1, "operation_id": operation_id, "state": "cancelled",
        "result": {"credential_state": "refresh_required", "revoke_state": "skipped"},
        "error_code": "operation_cancelled",
    }
    responses = iter((
        {"version": 1, "operation_id": operation_id, "state": "finalizing",
         "expires_at": None, "poll_interval": 1},
        {"version": 1, "operation_id": operation_id, "state": "pending",
         "expires_at": None, "poll_interval": 1},
        cancelled,
    ))
    calls = []
    client = SimpleNamespace(call=lambda method, path, body=None, **kwargs:
                             calls.append((method, path, body, kwargs)) or next(responses))
    monkeypatch.setattr(owner_cli.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(owner_cli.time, "sleep", lambda _seconds: None)

    owner_cli.command(client, ["provider", "auth", "status", "7", operation_id])
    owner_cli.command(client, ["provider", "auth", "cancel", "7", operation_id])

    path = f"/connections/7/auth/operations/{operation_id}"
    assert calls == [
        ("GET", path, None, {"authenticated": True}),
        ("DELETE", path, None, {"authenticated": True}),
        ("GET", path, None, {"authenticated": True, "request_timeout": 30.0}),
    ]
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [
        {"version": 1, "operation_id": operation_id, "state": "finalizing",
         "expires_at": None, "poll_interval": 1},
        cancelled,
    ]


def test_provider_auth_methods_and_start_use_canonical_routes_and_safe_output(capsys):
    operation_id = "f" * 64
    responses = iter((
        {"version": 1, "methods": [
            {"method_id": "oauth_browser", "available": True},
            {"method_id": "native_chain", "available": False},
        ]},
        {"version": 1, "config_revision": 3, "auth_grant_revision": 2},
        {"version": 1, "operation_id": operation_id, "state": "awaiting_user",
         "expires_at": None, "verification_uri": "https://login.example.test/device",
         "user_code": "ABCD-EFGH", "poll_interval": 2},
    ))
    calls = []
    client = SimpleNamespace(call=lambda method, path, body=None, **kwargs:
                             calls.append((method, path, body, kwargs)) or next(responses))

    owner_cli.command(client, ["provider", "auth", "methods", "7"])
    owner_cli.command(client, ["provider", "auth", "start", "7", "oauth_device",
                               "--idempotency-key", "auth-request-1"])

    assert calls == [
        ("GET", "/connections/7/auth/methods", None, {"authenticated": True}),
        ("GET", "/connections/7/revisions", None, {"authenticated": True}),
        ("POST", "/connections/7/auth/oauth_device/operations",
         {"version": 1, "config_revision": 3, "auth_grant_revision": 2},
         {"authenticated": True, "idempotency_key": "auth-request-1"}),
    ]
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output[0] == {"version": 1, "methods": [
        {"method_id": "oauth_browser", "available": True},
        {"method_id": "native_chain", "available": False},
    ]}
    assert output[1]["verification_uri"] == "https://login.example.test/device"
    assert output[1]["user_code"] == "ABCD-EFGH"
    assert "token" not in json.dumps(output).lower()
    assert "verifier" not in json.dumps(output).lower()
    assert "ref" not in json.dumps(output).lower()


@pytest.mark.parametrize("response", [
    {"version": 1, "methods": [{"method_id": "oauth_browser", "available": True,
                                  "token": "sentinel"}]},
    {"version": 1, "operation_id": "f" * 64, "state": "awaiting_user",
     "expires_at": None, "browser_url": "https://owner:secret@login.example.test/authorize"},
    {"version": 1, "operation_id": "f" * 64, "state": "succeeded",
     "result": {"credential_state": "present"}, "prepared_ref": "sentinel"},
])
def test_provider_auth_cli_rejects_private_or_unsafe_output(response, capsys):
    client = SimpleNamespace(call=lambda *_args, **_kwargs: response)
    words = (["provider", "auth", "methods", "7"] if "methods" in response
             else ["provider", "auth", "status", "7", "f" * 64])
    with pytest.raises(RuntimeError, match="Canonical provider"):
        owner_cli.command(client, words)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("response", [
    {"credential_state": "missing", "revoke_state": "skipped",
     "token": "sentinel-raw-token", "prepared_ref": "sentinel-raw-ref"},
    {"version": 1, "operation_id": "d" * 64, "state": "pending",
     "expires_at": None, "poll_interval": 1, "nonce": "sentinel-nonce"},
    {"version": 1, "operation_id": "d" * 64, "state": "succeeded",
     "result": {"credential_state": "missing", "revoke_state": "skipped",
                "provider_detail": "sentinel-provider-os-text"}},
    {"version": 1, "operation_id": "d" * 64, "state": "failed",
     "result": {"credential_state": "missing", "revoke_state": "skipped"},
     "error_code": "sentinel-provider-os-text"},
    {"version": 1, "operation_id": "d" * 64,
     "state": {"provider_text": "sentinel-provider-os-text"},
     "expires_at": None, "poll_interval": 1},
])
def test_provider_disconnect_rejects_open_or_unconfirmed_success_without_stdout(
        response, capsys):
    client = SimpleNamespace(call=lambda *_args, **_kwargs: response)
    with pytest.raises(RuntimeError, match="Canonical provider operation response is invalid") as failure:
        owner_cli.command(client, ["provider", "disconnect", "7"])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "sentinel" not in str(failure.value)


@pytest.mark.parametrize("words", [
    ["provider", "models", "--refresh"],
    ["provider", "models", "0"],
    ["provider", "models", "7", "8"],
    ["provider", "catalog", "--refresh"],
    ["provider", "disconnect", "0"],
    ["provider", "disconnect", "7", "extra"],
    ["provider", "auth", "status", "7", "A" * 64],
    ["provider", "auth", "cancel", "7", "a" * 63],
    ["provider", "auth", "status", "0", "a" * 64],
    ["provider", "auth", "delete", "7", "a" * 64],
    ["provider", "auth", "methods", "0"],
    ["provider", "auth", "methods", "7", "extra"],
    ["provider", "auth", "start", "7", "api_key"],
    ["provider", "auth", "start", "7", "unknown_method"],
])
def test_provider_catalog_models_and_disconnect_reject_invalid_shapes(words):
    client = SimpleNamespace(call=lambda *_args, **_kwargs:
                             pytest.fail("invalid provider command dispatched"))
    with pytest.raises(RuntimeError):
        owner_cli.command(client, words)


def test_typed_d1_provider_add_preserves_top_level_body_and_secret_channel(
        monkeypatch, tmp_path, capsys):
    from io import StringIO

    payload = {
        "version": 1, "provider_id": "openrouter",
        "protocol_id": "openai_chat_completions", "auth_method_id": "api_key",
        "fields": {}, "label": "OpenRouter", "default_model_id": "vendor/exact",
        "enabled": True, "capabilities": {"tools": True},
    }
    source = tmp_path / "typed-provider.json"
    source.write_text(json.dumps(payload))
    monkeypatch.setattr(owner_cli.sys, "stdin", StringIO("sentinel-provider-secret\n"))
    calls = []
    client = SimpleNamespace(call=lambda method, path, body=None, **kwargs:
                             calls.append((method, path, body, kwargs)) or {
                                 "id": 7, "api_key": "sentinel-provider-secret"})

    owner_cli.command(client, ["provider", "add", "--json", str(source), "--secret-stdin"])

    assert calls == [("POST", "/connections",
                      {**payload, "api_key": "sentinel-provider-secret"},
                      {"authenticated": True})]
    assert "sentinel-provider-secret" not in capsys.readouterr().out


def test_typed_local_none_provider_add_does_not_read_or_invent_a_secret(
        monkeypatch, tmp_path, capsys):
    payload = {
        "version": 1, "provider_id": "ollama",
        "protocol_id": "openai_chat_completions", "auth_method_id": "local_none",
        "fields": {}, "label": "Local", "default_model_id": "exact",
    }
    source = tmp_path / "local-provider.json"
    source.write_text(json.dumps(payload))
    monkeypatch.setattr(owner_cli, "read_secret", lambda *_args, **_kwargs:
                        pytest.fail("local_none must not request a credential"))
    calls = []
    client = SimpleNamespace(call=lambda method, path, body=None, **kwargs:
                             calls.append((method, path, body, kwargs)) or {"id": 8})

    owner_cli.command(client, ["provider", "add", "--json", str(source)])

    assert calls == [("POST", "/connections", payload, {"authenticated": True})]
    assert json.loads(capsys.readouterr().out) == {"id": 8}


# Literal contracts catch wrong route/method/body forwarding independently.
DISPATCH_CASES = [
    (['me'], 'GET', '/auth/me', None),
    (['logout'], 'POST', '/auth/logout', None),
    (['providers'], 'GET', '/connections', None),
    (['provider', 'list'], 'GET', '/connections', None),
    (['provider', 'add'], 'POST', '/connections', {'provider_kind': 'compatible', 'label': 'Local', 'default_model_id': 'exact', 'auth_required': False}),
    (['provider', 'set', '2'], 'PATCH', '/connections/2', {'label': 'Updated'}),
    (['provider', 'delete', '2'], 'DELETE', '/connections/2', None),
    (['provider', 'test', '2'], 'POST', '/connections/2/test', None),
    (['provider', 'test', '2', 'exact:1'], 'POST', '/connections/2/test?model_id=exact%3A1', None),
    (['agents'], 'GET', '/agents/', None),
    (['agent', 'list'], 'GET', '/agents/', None),
    (['agent', 'show', '3'], 'GET', '/agents/3', None),
    (['agent', 'workload'], 'GET', '/agent-workloads', None),
    (['agent', 'add'], 'POST', '/agents/', {'name': 'Ada', 'description': 'work', 'system_prompt': 'line 1\nline 2', 'connection_id': 2, 'model_id': 'exact'}),
    (['agent', 'set', '3'], 'PUT', '/agents/3', {'temperature': 'auto', 'max_tokens': 0}),
    (['agent', 'delete', '3'], 'DELETE', '/agents/3', None),
    (['projects'], 'GET', '/projects/', None),
    (['project', 'list'], 'GET', '/projects/', None),
    (['project', 'show', '4'], 'GET', '/projects/4', None),
    (['project', 'add'], 'POST', '/projects/', {'name': 'Research', 'description': 'Context'}),
    (['project', 'set', '4'], 'PUT', '/projects/4', {'description': None}),
    (['project', 'delete', '4'], 'DELETE', '/projects/4', None),
    (['project-agent', 'add', '4', '3'], 'POST', '/projects/4/agents/3', None),
    (['project-agent', 'remove', '4', '3'], 'DELETE', '/projects/4/agents/3', None),
    (['template', 'list', '4'], 'GET', '/projects/4/room-templates', None),
    (['template', 'show', '4', '7'], 'GET', '/projects/4/room-templates/7', None),
    (['template', 'add', '4'], 'POST', '/projects/4/room-templates', {'name': 'Release', 'room_title': 'Release room', 'agent_ids': [3], 'max_rounds': 2, 'max_tool_calls': 8, 'max_executor_seconds': 60}),
    (['template', 'set', '4', '7'], 'PUT', '/projects/4/room-templates/7', {'name': 'Updated', 'room_title': 'Updated room', 'agent_ids': [], 'max_rounds': 1, 'max_tool_calls': 0, 'max_executor_seconds': 30}),
    (['template', 'delete', '4', '7'], 'DELETE', '/projects/4/room-templates/7', None),
    (['template', 'create-room', '4', '7'], 'POST', '/projects/4/room-templates/7/rooms', {}),
    (['rooms'], 'GET', '/rooms/index', None),
    (['rooms', '4'], 'GET', '/conversations/project/4', None),
    (['room', 'list', '4'], 'GET', '/conversations/project/4', None),
    (['room', 'list', '4', 'closed'], 'GET', '/conversations/project/4?status=closed', None),
    (['room', 'show', '5'], 'GET', '/conversations/5', None),
    (['room', 'add'], 'POST', '/conversations/', {'project_id': 4, 'title': 'Plan'}),
    (['room', 'set', '5'], 'PATCH', '/conversations/5', {'title': 'Renamed'}),
    (['room', 'clone', '5'], 'POST', '/conversations/5/clone', {'project_id': 4, 'title': 'Fresh room'}),
    (['room', 'context-preview', '5'], 'POST', '/conversations/5/context-preview', {'content': 'Unsent request'}),
    (['room', 'delete', '5'], 'DELETE', '/conversations/5', None),
    (['room', 'stop', '5'], 'POST', '/conversations/5/stop', None),
    (['room', 'cancel', '5'], 'POST', '/conversations/5/cancel', None),
    (['room', 'pin', '5'], 'PUT', '/rooms/5/pin', None),
    (['room', 'unpin', '5'], 'DELETE', '/rooms/5/pin', None),
    (['attention', 'list'], 'GET', '/attention', None),
    (['request', 'inspect', '5', '550e8400-e29b-41d4-a716-446655440000'], 'GET', '/conversations/5/requests/550e8400-e29b-41d4-a716-446655440000', None),
    (['continue', '5'], 'POST', '/conversations/5/continue', {'content': 'Next', 'max_rounds': 1, 'max_tool_calls': 2, 'max_executor_seconds': 3}),
    (['room-settings', 'get', '5'], 'GET', '/conversations/5/schedule', None),
    (['room-settings', 'set', '5'], 'PUT', '/conversations/5/schedule', {'max_rounds': 1, 'max_tool_calls': 2, 'max_executor_seconds': 3}),
    (['summary', 'add', '5'], 'POST', '/conversations/5/summaries', {'source_start_seq': 1, 'source_end_seq': 2, 'content': 'Summary'}),
    (['members', '5'], 'GET', '/conversations/5/members', None),
    (['member-add', '5', '3'], 'POST', '/conversations/5/members/3', None),
    (['member-remove', '5', '3'], 'DELETE', '/conversations/5/members/3', None),
    (['member-pause', '5', '3'], 'POST', '/conversations/5/members/3/pause', None),
    (['member-resume', '5', '3'], 'POST', '/conversations/5/members/3/resume', None),
    (['events', '5', '2'], 'GET', '/conversations/5/events?after_seq=2', None),
    (['activity', '5'], 'GET', '/tools/rooms/5/activity', None),
    (['preview', '5', 'action-id'], 'GET', '/tools/rooms/5/actions/action-id/preview', None),
    (['decide', '5', 'action-id', 'approve', 'room_session', '--review-id', '550e8400-e29b-41d4-a716-446655440000'], 'POST', '/tools/rooms/5/actions/action-id/decision', {'decision': 'approve', 'scope': 'room_session', 'review_id': '550e8400-e29b-41d4-a716-446655440000', 'acknowledge_unverified_context': False}),
    (['grants', '5'], 'GET', '/tools/rooms/5/grants', None),
    (['grant-revoke', '5', '6'], 'POST', '/tools/rooms/5/grants/6/revoke', None),
    (['approval-mode'], 'GET', '/tools/broad-mode', None),
    (['approval-mode', 'manual'], 'PUT', '/tools/broad-mode', {'enabled': False}),
    (['approval-mode', 'broad', '--minutes', '10'], 'PUT', '/tools/broad-mode', {'enabled': True, 'expires_minutes': 10, 'always': False}),
    (['approval-mode', 'broad', '--always'], 'PUT', '/tools/broad-mode', {'enabled': True, 'always': True}),
    (['tool-lease'], 'GET', '/tools/desktop/lease', None),
    (['unlock', '2'], 'POST', '/connections/2/unlock', None),
    (['unlock-status', '2'], 'GET', '/connections/2/unlock', None),
    (['send', '5'], 'POST', '/conversations/5/events', {'content': 'multiline\nmessage'}),
]


@pytest.mark.parametrize('words,method,path,body', DISPATCH_CASES)
def test_business_dispatch_uses_canonical_contract(tmp_path, capsys, words, method, path, body):
    calls = []
    client = SimpleNamespace(token='memory-only')
    def call(actual_method, actual_path, actual_body=None, **kwargs):
        calls.append((actual_method, actual_path, actual_body, kwargs))
        return None if actual_method == 'DELETE' or path.endswith(('/stop', '/cancel', '/revoke', '/logout')) else {'ok': True}
    client.call = call
    if body is not None and words[0] not in {'decide', 'approval-mode'} and words[:2] != ['template', 'create-room']:
        source = tmp_path / 'input.json'
        source.write_text(json.dumps(body))
        words = [*words, '--json', str(source)]
    owner_cli.command(client, words)
    assert len(calls) == 1
    assert calls[0][:3] == (method, path, body)
    kwargs = calls[0][3]
    assert kwargs.get('authenticated', False) is (path != '/auth/logout')
    if words[0] == 'send':
        assert len(kwargs['idempotency_key']) == 32
    else:
        assert 'idempotency_key' not in kwargs
    assert json.loads(capsys.readouterr().out) in (None, {'ok': True})
    if path == '/auth/logout':
        assert client.token is None


@pytest.mark.parametrize("model_id,encoded", [
    ("vendor/exact-model", "vendor%2Fexact-model"),
    (
        "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.amazon.nova-pro-v1:0",
        "arn%3Aaws%3Abedrock%3Aus-east-1%3A123456789012%3Ainference-profile%2Fus.amazon.nova-pro-v1%3A0",
    ),
])
def test_provider_test_cli_forwards_canonical_slash_model_ids(model_id, encoded, capsys):
    from app.schemas.provider_models import valid_model_id

    assert valid_model_id(model_id)
    calls = []
    client = SimpleNamespace(call=lambda method, path, body=None, **kwargs:
                             calls.append((method, path, body, kwargs)) or {"ok": True})

    owner_cli.command(client, ["provider", "test", "2", model_id])

    assert calls == [("POST", f"/connections/2/test?model_id={encoded}", None,
                      {"authenticated": True})]
    assert json.loads(capsys.readouterr().out) == {"ok": True}


@pytest.mark.parametrize("model_id", [
    "vendor//model", "vendor/../model", "vendor/model?key=x",
    "vendor\\model", "vendor/model\n", "x" * 201,
])
@pytest.mark.parametrize("words", [["provider", "test", "2"], ["setup", "test", "2"]])
def test_cli_model_consumers_reject_noncanonical_model_ids_before_transport(model_id, words):
    client = SimpleNamespace(call=lambda *_args, **_kwargs:
                             pytest.fail("invalid model ID reached canonical HTTP"))
    with pytest.raises(RuntimeError):
        owner_cli.command(client, [*words, model_id])


def test_attention_cli_forwards_bounded_cursor_without_input(capsys):
    calls = []
    client = SimpleNamespace(call=lambda method, path, body=None, **kwargs:
                             calls.append((method, path, body, kwargs)) or {"items": [], "next_cursor": None})
    owner_cli.command(client, ["attention", "list", "--limit", "12", "--cursor", "opaque_cursor"])
    assert calls == [("GET", "/attention?limit=12&cursor=opaque_cursor", None, {"authenticated": True})]
    assert json.loads(capsys.readouterr().out) == {"items": [], "next_cursor": None}


def test_agent_workload_cli_forwards_exact_agent_and_bounded_cursor(capsys):
    calls = []
    client = SimpleNamespace(call=lambda method, path, body=None, **kwargs:
                             calls.append((method, path, body, kwargs)) or {"items": [], "next_cursor": None})
    owner_cli.command(client, ["agent", "workload", "7", "--limit", "12", "--cursor", "opaque_cursor"])
    assert calls == [("GET", "/agent-workloads?agent_id=7&limit=12&cursor=opaque_cursor", None,
                      {"authenticated": True})]
    assert json.loads(capsys.readouterr().out) == {"items": [], "next_cursor": None}


@pytest.mark.parametrize("words", [
    ["agent", "workload", "0"], ["agent", "workload", "1", "2"],
    ["agent", "workload", "--limit", "0"], ["agent", "workload", "--limit", "101"],
    ["agent", "workload", "--cursor", "bad cursor"],
])
def test_agent_workload_cli_rejects_invalid_contract_before_transport(words):
    client = SimpleNamespace(call=lambda *_a, **_k: pytest.fail("invalid workload request dispatched"))
    with pytest.raises(RuntimeError):
        owner_cli.command(client, words)


@pytest.mark.parametrize("words", [
    ["attention"], ["attention", "list", "extra"], ["attention", "list", "--limit", "0"],
    ["attention", "list", "--limit", "201"], ["attention", "list", "--cursor", "bad cursor"],
])
def test_attention_cli_rejects_invalid_contract_before_transport(words):
    client = SimpleNamespace(call=lambda *_a, **_k: pytest.fail("invalid attention request dispatched"))
    with pytest.raises(RuntimeError):
        owner_cli.command(client, words)


@pytest.mark.parametrize("words", [
    ["request", "inspect", "0", "550e8400-e29b-41d4-a716-446655440000"],
    ["request", "inspect", "1", "NOT-A-UUID"],
    ["request", "inspect", "1", "550E8400-E29B-41D4-A716-446655440000"],
    ["request", "inspect", "1", "550e8400-e29b-41d4-a716-446655440000", "extra"],
])
def test_request_inspector_cli_rejects_invalid_identity_before_transport(words):
    client = SimpleNamespace(call=lambda *_a, **_k: pytest.fail("invalid inspector request dispatched"))
    with pytest.raises(RuntimeError):
        owner_cli.command(client, words)


def test_request_retry_cli_preflights_and_executes_exact_plan(capsys):
    calls = []
    client = SimpleNamespace(call=lambda method, path, body=None, **kwargs:
                             calls.append((method, path, body, kwargs)) or {"eligible": True})
    failure = "550e8400-e29b-41d4-a716-446655440000"
    digest = "a" * 64
    owner_cli.command(client, ["request", "retry-preview", "7", failure])
    owner_cli.command(client, ["request", "retry", "7", failure, digest,
                               "--idempotency-key", "stable-retry"])
    assert calls == [
        ("GET", f"/conversations/7/failures/{failure}/retry", None, {"authenticated": True}),
        ("POST", f"/conversations/7/failures/{failure}/retry", {"plan_hash": digest},
         {"authenticated": True, "idempotency_key": "stable-retry"}),
    ]
    assert len(capsys.readouterr().out.splitlines()) == 2


@pytest.mark.parametrize("words", [
    ["request", "retry-preview", "0", "550e8400-e29b-41d4-a716-446655440000"],
    ["request", "retry", "1", "not-a-uuid", "a" * 64],
    ["request", "retry", "1", "550e8400-e29b-41d4-a716-446655440000", "bad-hash"],
    ["request", "retry", "1", "550e8400-e29b-41d4-a716-446655440000", "a" * 64,
     "--idempotency-key", "bad\u0085key"],
])
def test_request_retry_cli_rejects_invalid_contract_before_transport(words):
    client = SimpleNamespace(call=lambda *_a, **_k: pytest.fail("invalid retry request dispatched"))
    with pytest.raises(RuntimeError):
        owner_cli.command(client, words)


def test_project_archive_cli_uses_preview_hash_apply_restore_and_status(capsys):
    calls = []
    client = SimpleNamespace(call=lambda method, path, body=None, **kwargs:
                             calls.append((method, path, body, kwargs)) or {"ok": True})
    digest = "a" * 64
    for words in (["projects", "archived"], ["project", "get", "7"],
                  ["project", "archive-preview", "7"],
                  ["project", "archive", "7", digest],
                  ["project", "restore", "7"]):
        owner_cli.command(client, list(words))
    assert [(method, path, body) for method, path, body, _ in calls] == [
        ("GET", "/projects/?status=archived", None),
        ("GET", "/projects/7", None),
        ("POST", "/projects/7/archive/preview", {}),
        ("POST", "/projects/7/archive", {"plan_hash": digest}),
        ("POST", "/projects/7/restore", {}),
    ]
    assert all(kwargs == {"authenticated": True} for *_, kwargs in calls)
    assert len(capsys.readouterr().out.splitlines()) == 5


def test_rooms_cli_forwards_bounded_canonical_index_filters(capsys):
    calls = []
    client = SimpleNamespace(call=lambda method, path, body=None, **kwargs:
                             calls.append((method, path, body, kwargs)) or {"items": [], "next_cursor": None})
    owner_cli.command(client, ["rooms", "--status", "all", "--project", "4", "--query", "Release %_",
                               "--limit", "25", "--cursor", "opaque_Cursor-1"])
    assert calls == [("GET", "/rooms/index?status=all&project_id=4&q=Release+%25_&limit=25&cursor=opaque_Cursor-1",
                      None, {"authenticated": True})]
    assert json.loads(capsys.readouterr().out) == {"items": [], "next_cursor": None}


@pytest.mark.parametrize("words", [
    ["rooms", "--status", "invalid"], ["rooms", "--project", "0"],
    ["rooms", "--limit", "201"], ["rooms", "--cursor", "bad="],
    ["agents", "--query", "room"],
])
def test_rooms_cli_rejects_invalid_filters_before_transport(words):
    client = SimpleNamespace(call=lambda *_args, **_kwargs: pytest.fail("transport must not run"))
    with pytest.raises(RuntimeError):
        owner_cli.command(client, words)


def test_room_launch_cli_forwards_one_body_with_a_retry_stable_key(tmp_path, capsys):
    payload = {
        "project_id": 4, "title": "Custom room", "agent_ids": [3],
        "max_rounds": 2, "max_tool_calls": 8, "max_executor_seconds": 60,
    }
    source = tmp_path / "launch.json"
    source.write_text(json.dumps(payload))
    calls = []
    client = SimpleNamespace(call=lambda method, path, body=None, **kwargs:
                             calls.append((method, path, body, kwargs)) or {"id": 9})

    owner_cli.command(client, ["room", "launch", "--json", str(source),
                               "--idempotency-key", "stable-launch"])
    owner_cli.command(client, ["room", "launch", "--json", str(source)])

    assert [(method, path, body) for method, path, body, _kwargs in calls] == [
        ("POST", "/conversations/launch", payload),
        ("POST", "/conversations/launch", payload),
    ]
    assert calls[0][3] == {"authenticated": True, "idempotency_key": "stable-launch"}
    assert len(calls[1][3]["idempotency_key"]) == 32
    assert len(capsys.readouterr().out.splitlines()) == 2


def test_readiness_and_test_history_cli_forward_only_canonical_reads(capsys):
    calls = []
    client = SimpleNamespace(call=lambda method, path, body=None, **kwargs:
                             calls.append((method, path, body, kwargs)) or {"connections": []})
    owner_cli.command(client, ["readiness", "list"])
    owner_cli.command(client, ["provider", "test-history", "7", "--limit", "12"])
    assert [(method, path, body) for method, path, body, _ in calls] == [
        ("GET", "/readiness", None), ("GET", "/connections/7/test-history?limit=12", None)]
    assert all(call[3] == {"authenticated": True} for call in calls)
    assert len(capsys.readouterr().out.splitlines()) == 2


@pytest.mark.parametrize("words", [
    ["provider", "test-history", "0"], ["provider", "test-history", "1", "--limit", "0"],
    ["provider", "test-history", "1", "--limit", "21"], ["readiness", "list", "extra"],
])
def test_readiness_cli_rejects_invalid_shapes_before_transport(words):
    client = SimpleNamespace(call=lambda *_args, **_kwargs: pytest.fail("transport must not run"))
    with pytest.raises(RuntimeError):
        owner_cli.command(client, words)


@pytest.mark.parametrize("invalid_key", ["x" * 201, "retry\u0085key"])
def test_room_launch_cli_rejects_invalid_key_before_transport(tmp_path, invalid_key):
    source = tmp_path / "launch.json"
    source.write_text(json.dumps({
        "project_id": 4, "title": "Custom room", "agent_ids": [],
        "max_rounds": 2, "max_tool_calls": 8, "max_executor_seconds": 60,
    }))
    client = SimpleNamespace(call=lambda *_args, **_kwargs: pytest.fail("transport must not run"))
    with pytest.raises(RuntimeError, match="Invalid idempotency key"):
        owner_cli.command(client, ["room", "launch", "--json", str(source),
                                   "--idempotency-key", invalid_key])


@pytest.mark.parametrize("words", [
    ["projects", "invalid"], ["project", "archive", "7", "not-a-hash"],
    ["project", "archive-preview"], ["project", "restore", "7", "8"],
])
def test_project_archive_cli_rejects_invalid_contract(words):
    client = SimpleNamespace(call=lambda *_a, **_k: pytest.fail("invalid project request dispatched"))
    with pytest.raises(RuntimeError):
        owner_cli.command(client, words)


@pytest.mark.parametrize('words', [
    ['decide', '5', 'id', 'approve', 'forever'], ['preview', '5', '../other'],
    ['provider', 'test', '2', 'model?other=1'], ['room', 'set', '5'], ['room', 'clone', '5'],
    ['room', 'list', '4', 'invalid'], ['template', 'list'], ['template', 'show', '4'],
    ['template', 'add', '4'], ['template', 'set', '4', '7'], ['template', 'create-room', '4'],
    ['provider', 'add', 'argv-secret'], ['register', 'argv-password'],
    ['approval-mode', 'broad'], ['approval-mode', 'broad', '--minutes', '0'],
    ['approval-mode', 'broad', '--minutes', '61'],
    ['approval-mode', 'broad', '--minutes', '10', '--always'],
])
def test_invalid_commands_do_not_mutate_or_echo_arguments(words, capsys):
    client = SimpleNamespace(call=lambda *_a, **_k: pytest.fail('invalid command dispatched'))
    with pytest.raises(RuntimeError) as failure:
        owner_cli.command(client, words)
    assert 'argv-secret' not in str(failure.value) and 'argv-password' not in str(failure.value)
    assert not capsys.readouterr().out


@pytest.mark.parametrize('operation', [['register'], ['login'], ['provider', 'add'], ['provider', 'set', '2']])
def test_secret_is_read_from_dedicated_stdin_and_never_printed(operation, monkeypatch, tmp_path, capsys):
    from io import StringIO
    body = {'email': 'owner@example.com', 'username': 'Owner'} if operation[0] in {'register', 'login'} else {
        'provider_kind': 'compatible', 'label': 'Fake', 'default_model_id': 'exact'} if operation[1] == 'add' else {'label': 'New'}
    if operation[0] == 'login':
        body.pop('username')
    source = tmp_path / 'input.json'
    source.write_text(json.dumps(body))
    words = [*operation, '--json', str(source), '--secret-stdin']
    monkeypatch.setattr(owner_cli.sys, 'stdin', StringIO('sentinel-secret\n'))
    calls = []
    client = owner_cli.Client({'base_url': 'http://127.0.0.1:8100', 'capability': 'a' * 64})
    def call(method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return {'access_token': 'jwt-sentinel', 'api_key': 'sentinel-secret', 'detail': 'sentinel-secret'}
    monkeypatch.setattr(client, 'call', call)
    owner_cli.command(client, words)
    assert calls[0][2]['password' if operation[0] in {'register', 'login'} else 'api_key'] == 'sentinel-secret'
    output = capsys.readouterr().out
    assert 'sentinel-secret' not in output and 'jwt-sentinel' not in output
    assert 'sentinel-secret' not in words
    if operation[0] == 'login':
        assert client.token == 'jwt-sentinel'


@pytest.mark.parametrize('body', [{'api_key': 'secret'}, {'password': 'secret'}, {'nested': {'access_token': 'secret'}}])
def test_json_never_accepts_credentials(tmp_path, body):
    source = tmp_path / 'input.json'
    source.write_text(json.dumps(body))
    client = SimpleNamespace(call=lambda *_a, **_k: pytest.fail('must reject secrets'))
    with pytest.raises(RuntimeError, match='secret'):
        owner_cli.command(client, ['provider', 'add', '--json', str(source)])


def test_json_and_secret_cannot_both_consume_stdin(monkeypatch):
    from io import StringIO
    monkeypatch.setattr(owner_cli.sys, 'stdin', StringIO('{"email":"owner@example.com"}'))
    with pytest.raises(RuntimeError):
        owner_cli.command(SimpleNamespace(), ['login', '--json', '-', '--secret-stdin'])


def test_hidden_secret_refuses_non_tty_fallback(monkeypatch):
    from io import StringIO
    monkeypatch.setattr(owner_cli.sys, 'stdin', StringIO('not-a-tty'))
    monkeypatch.setattr(owner_cli.getpass, 'getpass', lambda _: pytest.fail('no insecure fallback'))
    with pytest.raises(RuntimeError, match='TTY'):
        owner_cli.command(SimpleNamespace(), ['register'])


def test_wait_returns_waiting_approval_without_timing_out(monkeypatch, capsys):
    client = SimpleNamespace(call=lambda method, path, **_: [] if '/events' in path else [{'action_id': 'id', 'state': 'waiting'}])
    monkeypatch.setattr(owner_cli.time, 'sleep', lambda _: pytest.fail('approval should return immediately'))
    owner_cli.command(client, ['wait', '5', '1'])
    assert json.loads(capsys.readouterr().out)['activity'][0]['state'] == 'waiting'


@pytest.mark.parametrize('url', ['\nhttp://127.0.0.1:8100', 'http://127.0.0.1:81\t00', 'http://127.0.0.1:8100\n'])
def test_capability_rejects_raw_control_whitespace(tmp_path, url):
    path = tmp_path / 'owner-cli.json'
    path.write_text(json.dumps({'base_url': url, 'capability': 'a' * 64}))
    path.chmod(0o600)
    with pytest.raises(RuntimeError):
        owner_cli.capability(path)


def test_bootstrap_through_canonical_api_with_fake_secrets_and_provider(sqlite_url, monkeypatch, tmp_path, capsys):
    import hashlib
    import httpx
    from io import StringIO
    from time import monotonic, sleep
    from urllib.parse import urlsplit
    from fastapi.testclient import TestClient
    from app.main import create_app, RuntimeConfig
    from app.services.providers import ProviderResult, ToolCall
    from test_connections import FakeSecrets

    credential = 'a' * 64
    app = create_app(runtime_config=RuntimeConfig(database_url=sqlite_url,
        cli_capability_hash=hashlib.sha256(credential.encode()).hexdigest()))
    with TestClient(app) as api:
        secrets = FakeSecrets()
        app.state.secret_store = secrets
        probed = []
        def provider_wire(request):
            body = json.loads(request.content)
            probed.append(body)
            assert request.headers['authorization'] == 'Bearer synthetic-provider-secret'
            message = {'content': 'OK'}
            if body.get('tools') and not any(m.get('role') == 'tool' for m in body['messages']):
                message = {'content': None, 'tool_calls': [{'id': 'probe', 'type': 'function',
                    'function': {'name': body['tools'][0]['function']['name'], 'arguments': '{}'}}]}
            return httpx.Response(200, json={'choices': [{'message': message}]})
        app.state.connection_transport = httpx.MockTransport(provider_wire)

        # Keep the production Client serialization and headers; replace network
        # delivery only, dispatching every request into the canonical ASGI API.
        class LocalOpener:
            def open(self, request, **_):
                response = api.request(request.method, urlsplit(request.full_url).path +
                    ('?' + urlsplit(request.full_url).query if urlsplit(request.full_url).query else ''),
                    content=request.data, headers=dict(request.header_items()))
                if response.status_code >= 400:
                    raise HTTPError(request.full_url, response.status_code, 'fixture failure', {}, BytesIO(response.content))
                return BytesIO(response.content)
        monkeypatch.setattr(owner_cli, 'build_opener', lambda *_: LocalOpener())
        endpoint = tmp_path / 'owner-cli.json'
        endpoint.write_text(json.dumps({'version': 1, 'api_url': 'http://127.0.0.1:8100',
            'control_url': 'http://127.0.0.1:8101', 'capability': credential, 'core_version': 1, 'engine_version': 1}))
        endpoint.chmod(0o600)
        client = owner_cli.Client(owner_cli.capability(endpoint))
        def run(words, body=None, secret=None):
            if body is not None:
                source = tmp_path / 'body.json'
                source.write_text(json.dumps(body))
                words = [*words, '--json', str(source)]
            if secret:
                monkeypatch.setattr(owner_cli.sys, 'stdin', StringIO(secret + '\n'))
                words = [*words, '--secret-stdin']
            owner_cli.command(client, words)
            output = capsys.readouterr().out
            assert 'synthetic-provider-secret' not in output and 'synthetic-password' not in output
            assert not client.token or client.token not in output
            return json.loads(output.splitlines()[-1])

        run(['register'], {'email': 'cli@example.com', 'username': 'cli'}, 'synthetic-password')
        run(['login'], {'email': 'cli@example.com'}, 'synthetic-password')
        assert run(['me'])['username'] == 'cli'
        connection = run(['provider', 'add'], {'provider_kind': 'compatible', 'label': 'Synthetic',
            'base_url': 'http://127.0.0.1:8765/v1', 'allow_local_http': True,
            'default_model_id': 'exact', 'capabilities': {'tools': True}}, 'synthetic-provider-secret')['id']
        assert 'synthetic-provider-secret' in secrets.values.values()
        assert run(['provider', 'test', str(connection), 'exact'])['verified']['tools'] is True
        assert probed and all(request['model'] == 'exact' for request in probed)
        agent = run(['agent', 'add'], {'name': 'Ada', 'description': 'Synthetic', 'system_prompt': 'Work safely',
            'connection_id': connection, 'model_id': 'exact'})['id']
        project = run(['project', 'add'], {'name': 'Synthetic project'})['id']
        run(['project-agent', 'add', str(project), str(agent)])
        room = run(['room', 'add'], {'project_id': project, 'title': 'Synthetic room'})['id']
        run(['member-add', str(room), str(agent)])
        run(['room-settings', 'set', str(room)], {'max_rounds': 1, 'max_tool_calls': 2, 'max_executor_seconds': 3})
        executed = []
        async def executor(action, grant, cancel):
            executed.append(action.action_id)
            return 'synthetic observation'
        app.state.scheduler.broker.executor = executor
        async def provider(request):
            if not request.observations:
                return ProviderResult(tool_calls=[ToolCall('call-1', 'read_file', {'path': 'synthetic.txt'})],
                    continuation={'call_ids': ['call-1']})
            return ProviderResult(text='synthetic complete')
        app.state.scheduler.provider = provider
        event = run(['send', str(room)], {'content': 'Inspect synthetic data'})
        assert isinstance(event['seq'], int)
        waiting = run(['wait', str(room), str(event['seq'])])
        action = next(item for item in waiting['activity'] if item['state'] == 'waiting')
        assert not executed
        preview = run(['preview', str(room), action['action_id']])
        run(['decide', str(room), action['action_id'], 'approve', '--review-id', preview['review_id']])
        result = run(['wait', str(room), str(event['seq'])])
        # The same action can still be waiting briefly after the decision commits.
        until = monotonic() + 3
        while not any(e.get('actor_type') == 'agent' and e['kind'] == 'message' for e in result['events']):
            assert monotonic() < until
            sleep(.02)
            result = run(['wait', str(room), str(event['seq'])])
        assert executed == [action['action_id']]
        assert run(['room', 'stop', str(room)]) is None
        assert run(['rooms', str(project)]) == []
        assert run(['room', 'show', str(room)])['is_active'] is False
        assert run(['events', str(room)])
        assert run(['members', str(room)]) == []
        run(['logout'])
        assert client.token is None


def test_batch_keeps_login_token_only_for_its_process_and_stops_on_error(monkeypatch, tmp_path, capsys):
    from io import StringIO
    source = tmp_path / 'commands.ndjson'
    source.write_text('\n'.join(json.dumps(row) for row in [
        {'command': ['login', '--secret-stdin'], 'input': {'email': 'owner@example.com'}},
        {'command': ['me']}, {'command': ['logout']}, {'command': ['me']},
        {'command': ['project', 'delete', '4']},
    ]))
    monkeypatch.setattr(owner_cli.sys, 'stdin', StringIO('secret-sentinel\n'))
    client = owner_cli.Client({'base_url': 'http://127.0.0.1:8100', 'capability': 'a' * 64})
    requests = []
    class Opener:
        def open(self, request, **_):
            requests.append(request)
            path = request.full_url.rsplit('/', 1)[-1]
            if path == 'login':
                assert json.loads(request.data)['password'] == 'secret-sentinel'
                return BytesIO(b'{"access_token":"memory-jwt"}')
            assert request.get_header('Authorization') == (None if path == 'logout' else 'Bearer memory-jwt')
            return BytesIO(b'' if path == 'logout' else b'{"id":1}')
    monkeypatch.setattr(owner_cli, 'build_opener', lambda *_: Opener())
    with pytest.raises(RuntimeError, match='login or restore'):
        owner_cli.batch(client, str(source))
    assert len(requests) == 3
    assert client.token is None
    output = capsys.readouterr().out
    assert [json.loads(line) for line in output.splitlines()] == [{'authenticated': True}, {'id': 1}, None]
    assert 'secret-sentinel' not in output and 'memory-jwt' not in output


@pytest.mark.parametrize('row', [
    {'command': ['me'], 'password': 'secret'}, {'command': 'me'}, {'command': []},
    {'command': ['login', '--secret-stdin'], 'input': {'email': 'a@example.com'}},
    {'command': ['send', '5', '--stdin']},
    {'command': ['project', 'add'], 'input': {'password': 'secret'}},
])
def test_stdin_batch_rejects_invalid_or_shared_secret_stream(monkeypatch, row):
    from io import StringIO
    monkeypatch.setattr(owner_cli.sys, 'stdin', StringIO(json.dumps(row) + '\n'))
    client = SimpleNamespace(call=lambda *_a, **_k: pytest.fail('invalid batch dispatched'))
    with pytest.raises(RuntimeError):
        owner_cli.batch(client, '-')


def test_structured_send_uses_explicit_retry_key_and_preserves_multiline_file(tmp_path, capsys):
    source = tmp_path / 'text.txt'
    source.write_text('first line\nsecond line\n')
    requests = []
    class Opener:
        def open(self, request, **_):
            requests.append(request)
            return BytesIO(b'{"seq":1}')
    client = owner_cli.Client({'base_url': 'http://127.0.0.1:8100', 'capability': 'a' * 64})
    client.token = 'memory-jwt'
    from unittest.mock import patch
    with patch.object(owner_cli, 'build_opener', return_value=Opener()):
        owner_cli.command(client, ['send', '5', '--file', str(source), '--idempotency-key', 'retry-key'])
    assert requests[0].get_header('Idempotency-key') == 'retry-key'
    assert json.loads(requests[0].data) == {'content': 'first line\nsecond line\n'}
    assert json.loads(capsys.readouterr().out) == {'seq': 1}


def test_interactive_register_reads_password_hidden_and_returns_no_token(monkeypatch, capsys):
    prompts = iter(['owner@example.com', 'Owner'])
    monkeypatch.setattr(owner_cli.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda _: next(prompts))
    monkeypatch.setattr(owner_cli.getpass, 'getpass', lambda _: 'hidden-password')
    client = owner_cli.Client({'base_url': 'http://127.0.0.1:8100', 'capability': 'a' * 64})
    sent = []
    monkeypatch.setattr(client, 'call', lambda method, path, body: sent.append((method, path, body)) or {'id': 1})
    owner_cli.command(client, ['register'])
    assert sent == [('POST', '/auth/register', {'email': 'owner@example.com', 'username': 'Owner', 'password': 'hidden-password'})]
    assert client.token is None
    assert json.loads(capsys.readouterr().out) == {'id': 1}


def test_only_options_is_a_sanitized_command_error(tmp_path):
    source = tmp_path / 'input.json'
    source.write_text('{}')
    with pytest.raises(RuntimeError):
        owner_cli.command(SimpleNamespace(), ['--json', str(source)])


def test_getpass_echo_warning_is_blocked(monkeypatch):
    import warnings
    monkeypatch.setattr(owner_cli.sys.stdin, 'isatty', lambda: True)
    def fallback(_):
        warnings.warn('Password may be echoed', owner_cli.getpass.GetPassWarning)
        return 'secret'
    monkeypatch.setattr(owner_cli.getpass, 'getpass', fallback)
    with pytest.raises(RuntimeError, match='Hidden'):
        owner_cli.read_secret('Password: ')


def test_main_batch_can_restore_then_run_business_commands(monkeypatch, tmp_path, capsys):
    source = tmp_path / 'batch.ndjson'
    source.write_text('{"command":["restore"]}\n{"command":["me"]}\n')
    client = owner_cli.Client({'base_url': 'http://127.0.0.1:8100', 'capability': 'a' * 64})
    requests = []
    def call(method, path, body=None, **kwargs):
        requests.append((method, path))
        return {'access_token': 'memory-only'} if path == '/auth/restore' else {'id': 1}
    monkeypatch.setattr(client, 'call', call)
    monkeypatch.setattr(owner_cli, 'Client', lambda _: client)
    monkeypatch.setattr(owner_cli, 'capability', lambda _: {})
    monkeypatch.setattr(owner_cli.sys, 'argv', ['gamja', '--batch', str(source)])
    owner_cli.main()
    assert requests == [('POST', '/auth/restore'), ('GET', '/auth/me')]
    output = capsys.readouterr().out
    assert [json.loads(line) for line in output.splitlines()] == [{'authenticated': True}, {'id': 1}]
    assert 'memory-only' not in output


def test_main_batch_restore_failure_emits_closed_diagnostic_and_skips_me(
        monkeypatch, tmp_path, capsys):
    correlation = "a" * 32
    source = tmp_path / 'batch.ndjson'
    source.write_text('{"command":["restore"]}\n{"command":["me"]}\n')
    requests = []

    class FailingOpener:
        def open(self, request, **_kwargs):
            requests.append(request.full_url)
            body = json.dumps({'detail': {
                'code': 'credential_locked', 'correlation_id': correlation,
                'message': 'sentinel_password',
            }}).encode()
            raise HTTPError(request.full_url, 503, "Unavailable", {}, BytesIO(body))

    monkeypatch.setattr(owner_cli, 'build_opener', lambda *_: FailingOpener())
    monkeypatch.setattr(owner_cli, 'capability', lambda _: {
        'base_url': 'http://127.0.0.1:8100', 'capability': 'a' * 64,
    })
    monkeypatch.setattr(owner_cli.sys, 'argv', ['gamja', '--batch', str(source)])

    with pytest.raises(SystemExit) as failure:
        owner_cli.main()

    assert failure.value.code == 1
    assert requests == ['http://127.0.0.1:8100/api/v1/auth/restore']
    output = capsys.readouterr()
    assert not output.out and 'sentinel_password' not in output.err
    assert json.loads(output.err) == {'error': {
        'version': 1,
        'phase': 'auth_restore',
        'code': 'credential_locked',
        'correlation_id': correlation,
    }}


def test_main_restore_http_failure_emits_only_finite_status(monkeypatch, capsys):
    class FailingOpener:
        def open(self, request, **_kwargs):
            body = json.dumps({'detail': 'sentinel_password'}).encode()
            raise HTTPError(request.full_url, 401, "Unauthorized", {}, BytesIO(body))

    monkeypatch.setattr(owner_cli, 'build_opener', lambda *_: FailingOpener())
    monkeypatch.setattr(owner_cli, 'capability', lambda _: {
        'base_url': 'http://127.0.0.1:8100', 'capability': 'a' * 64,
    })
    monkeypatch.setattr(owner_cli.sys, 'argv', ['gamja', 'restore'])

    with pytest.raises(SystemExit) as failure:
        owner_cli.main()

    assert failure.value.code == 1
    output = capsys.readouterr()
    assert not output.out and 'sentinel_password' not in output.err
    assert json.loads(output.err) == {'error': {
        'version': 1,
        'phase': 'auth_restore',
        'http_status': 401,
    }}


def test_logout_drops_local_token_even_when_server_is_unavailable():
    client = owner_cli.Client({'base_url': 'http://127.0.0.1:8100', 'capability': 'a' * 64})
    client.token = 'memory-jwt'
    from unittest.mock import patch
    with patch.object(client, 'call', side_effect=RuntimeError('Running backend is unavailable')):
        with pytest.raises(RuntimeError):
            owner_cli.command(client, ['logout'])
    assert client.token is None


def test_noninteractive_errors_are_structured_and_have_failure_exit(monkeypatch, capsys):
    client = SimpleNamespace(call=lambda *_a, **_k: None)
    monkeypatch.setattr(owner_cli, 'Client', lambda _: client)
    monkeypatch.setattr(owner_cli, 'capability', lambda _: {})
    monkeypatch.setattr(owner_cli.sys, 'argv', ['gamja', 'unknown', 'sentinel-secret'])
    with pytest.raises(SystemExit) as failure:
        owner_cli.main()
    assert failure.value.code == 1
    output = capsys.readouterr()
    assert not output.out and 'sentinel-secret' not in output.err
    assert isinstance(json.loads(output.err)['error'], str)


def test_non_json_success_is_a_sanitized_transport_error(monkeypatch):
    class Opener:
        def open(self, *_a, **_k):
            return BytesIO(b'sentinel-secret')
    monkeypatch.setattr(owner_cli, 'build_opener', lambda *_: Opener())
    client = owner_cli.Client({'base_url': 'http://127.0.0.1:8100', 'capability': 'a' * 64})
    with pytest.raises(RuntimeError) as failure:
        client.call('GET', '/health')
    assert 'sentinel-secret' not in str(failure.value)


def test_global_argument_error_does_not_echo_attempted_secret(monkeypatch, capsys):
    monkeypatch.setattr(owner_cli.sys, 'argv', ['gamja', '--api-key=sentinel-secret'])
    with pytest.raises(SystemExit) as failure:
        owner_cli.main()
    assert failure.value.code == 2
    output = capsys.readouterr()
    assert not output.out and 'sentinel-secret' not in output.err
    assert 'error' in json.loads(output.err)


def test_logout_can_clear_saved_login_without_an_active_jwt(monkeypatch, capsys):
    requests = []
    class Opener:
        def open(self, request, **_):
            requests.append(request)
            return BytesIO(b'')
    monkeypatch.setattr(owner_cli, 'build_opener', lambda *_: Opener())
    client = owner_cli.Client({'base_url': 'http://127.0.0.1:8100', 'capability': 'a' * 64})
    owner_cli.command(client, ['logout'])
    assert requests[0].full_url.endswith('/api/v1/auth/logout')
    assert requests[0].get_header('Authorization') is None
    assert json.loads(capsys.readouterr().out) is None


@pytest.mark.parametrize('reader', [
    ['send', '5', '--stdin'], ['send', '5', '--file', '-'],
    ['send', '5', '--json', '-'],
])
@pytest.mark.parametrize('secret_first', [True, False])
def test_file_batch_rejects_cross_row_secret_and_public_stdin_before_execution(
        monkeypatch, tmp_path, capsys, reader, secret_first):
    from io import StringIO
    secret_row = {'command': ['login', '--secret-stdin'], 'input': {'email': 'fixture@example.com'}}
    provider_row = {'command': ['provider', 'add', '--secret-stdin'], 'input': {
        'provider_kind': 'compatible', 'label': 'Fixture', 'default_model_id': 'exact'}}
    rows = [secret_row, {'command': reader}, provider_row] if secret_first else [
        {'command': reader}, secret_row, provider_row]
    source = tmp_path / 'batch.ndjson'
    source.write_text('\n'.join(map(json.dumps, rows)))
    class ObservedStdin(StringIO):
        reads = 0
        def read(self, *args):
            self.reads += 1
            return super().read(*args)
        def readline(self, *args):
            self.reads += 1
            return super().readline(*args)
    stream = ObservedStdin('synthetic-password\nsynthetic-message\nsynthetic-provider-key\n')
    monkeypatch.setattr(owner_cli.sys, 'stdin', stream)
    requests = []
    def call(method, path, body=None, **kwargs):
        requests.append((path, body))
        return {'access_token': 'synthetic-jwt'} if path == '/auth/login' else {
            'seq': 1, 'content': body.get('content', '')}
    client = SimpleNamespace(token=None, call=call)
    with pytest.raises(RuntimeError):
        owner_cli.batch(client, str(source))
    assert requests == []
    assert stream.reads == 0 and stream.tell() == 0
    output = capsys.readouterr()
    assert not output.out and not output.err
    assert client.token is None


@pytest.mark.parametrize('readers', [
    [['send', '5', '--json', '-'], ['send', '5', '--stdin']],
    [['send', '5', '--stdin'], ['send', '5', '--json', '-']],
    [['send', '5', '--file', '-'], ['send', '5', '--stdin']],
    [['send', '5', '--json', '-'], ['send', '5', '--json', '-']],
])
def test_file_batch_rejects_multiple_bulk_stdin_readers_before_any_mutation(
        monkeypatch, tmp_path, capsys, readers):
    from io import StringIO
    source = tmp_path / 'batch.ndjson'
    source.write_text('\n'.join(map(json.dumps, [
        {'command': ['project', 'add'], 'input': {'name': 'Do not create'}},
        *[{'command': words} for words in readers],
    ])))
    stream = StringIO('{"content":"public JSON"}\npublic text\n')
    monkeypatch.setattr(owner_cli.sys, 'stdin', stream)
    requests = []
    client = SimpleNamespace(call=lambda *_a, **_k: requests.append(True) or {'id': 1})
    with pytest.raises(RuntimeError):
        owner_cli.batch(client, str(source))
    assert requests == [] and stream.tell() == 0
    output = capsys.readouterr()
    assert not output.out and not output.err


def test_file_batch_allows_multiple_secret_lines_with_public_file_inputs(monkeypatch, tmp_path, capsys):
    from io import StringIO
    message = tmp_path / 'message.txt'
    message.write_text('public multiline\ntext\n')
    source = tmp_path / 'batch.ndjson'
    source.write_text('\n'.join(map(json.dumps, [
        {'command': ['login', '--secret-stdin'], 'input': {'email': 'fixture@example.com'}},
        {'command': ['send', '5', '--file', str(message), '--idempotency-key', '--stdin']},
        {'command': ['provider', 'add', '--secret-stdin'], 'input': {
            'provider_kind': 'compatible', 'label': 'Fixture', 'default_model_id': 'exact'}},
    ])))
    stream = StringIO('synthetic-password\nsynthetic-provider-key\n')
    monkeypatch.setattr(owner_cli.sys, 'stdin', stream)
    requests = []
    def call(method, path, body=None, **kwargs):
        requests.append((path, body, kwargs))
        return {'access_token': 'synthetic-jwt'} if path == '/auth/login' else {
            'seq': 1, 'content': body.get('content', '')}
    client = SimpleNamespace(token=None, call=call)
    owner_cli.batch(client, str(source))
    assert [path for path, _, _ in requests] == ['/auth/login', '/conversations/5/events', '/connections']
    assert requests[0][1]['password'] == 'synthetic-password'
    assert requests[1][1] == {'content': 'public multiline\ntext\n'}
    assert requests[1][2]['idempotency_key'] == '--stdin'
    assert requests[2][1]['api_key'] == 'synthetic-provider-key'
    assert stream.read() == '' and client.token == 'synthetic-jwt'
    output = capsys.readouterr().out
    assert 'synthetic-password' not in output and 'synthetic-provider-key' not in output
    assert 'synthetic-jwt' not in output
    assert json.loads(output.splitlines()[1])['content'] == 'public multiline\ntext\n'


@pytest.mark.parametrize('reader,raw', [
    (['send', '5', '--stdin'], 'public multiline\ntext\n'),
    (['send', '5', '--file', '-'], 'public multiline\ntext\n'),
    (['send', '5', '--json', '-'], '{"content":"public multiline\\ntext\\n"}'),
])
def test_file_batch_allows_one_public_stdin_reader_without_secret_stream(
        monkeypatch, tmp_path, capsys, reader, raw):
    from io import StringIO
    source = tmp_path / 'batch.ndjson'
    source.write_text(json.dumps({'command': reader}) + '\n' + json.dumps({'command': ['projects']}))
    monkeypatch.setattr(owner_cli.sys, 'stdin', StringIO(raw))
    requests = []
    def call(method, path, body=None, **kwargs):
        requests.append((path, body))
        return {'seq': 1, 'content': body['content']} if body else []
    owner_cli.batch(SimpleNamespace(token='synthetic-jwt', call=call), str(source))
    assert requests == [('/conversations/5/events', {'content': 'public multiline\ntext\n'}), ('/projects/', None)]
    assert json.loads(capsys.readouterr().out.splitlines()[0])['content'] == 'public multiline\ntext\n'


def test_file_batch_retains_tty_hidden_credentials(monkeypatch, tmp_path, capsys):
    from io import StringIO
    source = tmp_path / 'batch.ndjson'
    source.write_text(json.dumps({'command': ['login'], 'input': {'email': 'fixture@example.com'}}))
    stream = StringIO()
    monkeypatch.setattr(stream, 'isatty', lambda: True)
    monkeypatch.setattr(owner_cli.sys, 'stdin', stream)
    monkeypatch.setattr(owner_cli.getpass, 'getpass', lambda _: 'synthetic-password')
    sent = []
    client = SimpleNamespace(token=None, call=lambda method, path, body: sent.append(body) or {'access_token': 'synthetic-jwt'})
    owner_cli.batch(client, str(source))
    assert sent[0]['password'] == 'synthetic-password' and client.token == 'synthetic-jwt'
    assert json.loads(capsys.readouterr().out) == {'authenticated': True}


def test_workspace_cli_exports_private_file_and_previews_and_applies_exact_file(
        tmp_path, capsys):
    blueprint = {"version": 1, "connections": [], "agents": [], "projects": []}
    source = tmp_path / "workspace.json"
    source.write_text(json.dumps(blueprint), encoding="utf-8")
    output = tmp_path / "exported.json"
    calls = []

    def call(method, path, body=None, **kwargs):
        calls.append((method, path, body, kwargs))
        if method == "GET":
            return blueprint
        if path.endswith("/preview"):
            return {"version": 1, "plan_hash": "a" * 64}
        return {"plan_hash": "a" * 64, "credentials_unset": 0}

    client = SimpleNamespace(call=call)
    owner_cli.command(client, ["workspace", "export", "--output", str(output)])
    assert json.loads(output.read_text(encoding="utf-8")) == blueprint
    assert output.stat().st_mode & 0o777 == 0o600
    owner_cli.command(client, ["workspace", "preview", "--file", str(source)])
    owner_cli.command(client, ["workspace", "apply", "a" * 64, "--file", str(source)])
    assert [(method, path, body) for method, path, body, _ in calls] == [
        ("GET", "/workspace/blueprint", None),
        ("POST", "/workspace/blueprint/preview", blueprint),
        ("POST", "/workspace/blueprint/apply", {
            "blueprint": blueprint, "plan_hash": "a" * 64,
        }),
    ]
    assert all(kwargs == {"authenticated": True} for *_, kwargs in calls)
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [
        {"plan_hash": "a" * 64, "version": 1},
        {"credentials_unset": 0, "plan_hash": "a" * 64},
    ]


def test_workspace_cli_rejects_secret_fields_and_existing_export_without_echo(
        tmp_path, capsys):
    source = tmp_path / "workspace.json"
    source.write_text(json.dumps({
        "version": 1, "connections": [{"api_key": "sentinel-workspace-secret"}],
        "agents": [], "projects": [],
    }), encoding="utf-8")
    client = SimpleNamespace(call=lambda *_a, **_k: pytest.fail("invalid workspace request"))
    with pytest.raises(RuntimeError) as failure:
        owner_cli.command(client, ["workspace", "preview", "--file", str(source)])
    assert "sentinel-workspace-secret" not in str(failure.value)
    assert not capsys.readouterr().out

    output = tmp_path / "existing.json"
    output.write_text("keep", encoding="utf-8")
    client = SimpleNamespace(call=lambda *_a, **_k: {
        "version": 1, "connections": [], "agents": [], "projects": []})
    with pytest.raises(RuntimeError, match="already exists"):
        owner_cli.command(client, ["workspace", "export", "--output", str(output)])
    assert output.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(("path", "detail", "message"), [
    ("/workspace/blueprint", {"code": "workspace_source_invalid"},
     "Workspace export has an unavailable dependency"),
    ("/workspace/blueprint/apply", {"code": "workspace_plan_stale"},
     "Workspace plan is stale; preview the file again"),
])
def test_workspace_cli_reports_only_validated_fixed_errors_without_server_text(
        monkeypatch, path, detail, message):
    class FailingOpener:
        def open(self, *_args, **_kwargs):
            body = json.dumps({"detail": detail}).encode()
            raise HTTPError("http://127.0.0.1:8100", 409, "Conflict", {}, BytesIO(body))

    monkeypatch.setattr(owner_cli, "build_opener", lambda *_: FailingOpener())
    client = owner_cli.Client({"base_url": "http://127.0.0.1:8100", "capability": "a" * 64})
    client.token = "memory-jwt"
    with pytest.raises(RuntimeError, match=message):
        client.call("POST" if path.endswith("/apply") else "GET", path,
                    {} if path.endswith("/apply") else None, authenticated=True)


def test_workspace_cli_rejects_malformed_error_without_echo(monkeypatch):
    class FailingOpener:
        def open(self, *_args, **_kwargs):
            body = json.dumps({"detail": {"code": "workspace_plan_stale",
                                           "message": "sentinel-server-secret"}}).encode()
            raise HTTPError("http://127.0.0.1:8100", 409, "Conflict", {}, BytesIO(body))

    monkeypatch.setattr(owner_cli, "build_opener", lambda *_: FailingOpener())
    client = owner_cli.Client({"base_url": "http://127.0.0.1:8100", "capability": "a" * 64})
    client.token = "memory-jwt"
    with pytest.raises(RuntimeError) as error:
        client.call("POST", "/workspace/blueprint/apply", {}, authenticated=True)
    assert str(error.value) == "Request failed (409)"
    assert "sentinel-server-secret" not in str(error.value)
