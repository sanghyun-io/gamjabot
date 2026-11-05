"""Private auth error history survives process and backend recreation."""

import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import RuntimeConfig, create_app
from app import owner_cli
from app.services.secrets import SecretReentryRequired, SecretStoreUnavailable


def record(number=1, stage="auth_remember"):
    value = {"correlation_id": f"{number:032x}", "stage": stage,
             "code": "secret_store_unavailable", "reason": "keychain_query_failed"}
    if stage == "auth_remember":
        value.update(error_class="native_os_status", cleanup="success",
                     operation="sec_item_add", os_status=-25293)
    else:
        value.update(error_class="secret_store_unavailable", os_status=-25293)
    return value


def store(root):
    from app.services.auth_diagnostics import AuthDiagnosticStore
    return AuthDiagnosticStore(root)


class FailedSecretStore:
    def put(self, *_):
        raise SecretStoreUnavailable("keychain_query_failed", -25293,
                                     operation="sec_item_add", error_class="native_os_status")

    def get_no_ui(self, *_):
        raise SecretReentryRequired()

    def delete(self, *_):
        pass


@pytest.mark.parametrize("stage", ["auth_remember", "auth_restore"])
def test_backend_recreation_retains_diagnostic(sqlite_url, tmp_path, monkeypatch, stage):
    root = tmp_path / "appdata"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "diagnostic-fixture-session")
    headers = {"x-gamja-session": "diagnostic-fixture-session"}
    with TestClient(create_app()) as client:
        client.app.state.login_secret_store = FailedSecretStore()
        if stage == "auth_remember":
            account = {"email": "diagnostic@example.com", "username": "diagnostic",
                       "password": "fixture-password-long"}
            assert client.post("/api/v1/auth/register", headers=headers, json=account).status_code == 200
            response = client.post("/api/v1/auth/login", headers=headers,
                                   json={**account, "remember_device": True})
        else:
            response = client.post("/api/v1/auth/restore", headers=headers)
        assert response.status_code == 503
        correlation = response.json()["detail"]["correlation_id"]
        path = f"/api/v1/auth/{stage.removeprefix('auth_')}/diagnostics/{correlation}"
        expected = client.get(path, headers=headers).json()
        assert expected["stage"] == stage
    with TestClient(create_app()) as client:
        assert not getattr(client.app.state, stage + "_diagnostics", {})
        result = client.get(path, headers=headers)
        assert result.status_code == 200
        assert result.json() == expected
        output = []
        monkeypatch.setattr(owner_cli, "compact", output.append)

        class Client:
            def call(self, method, route, **_):
                res = client.request(method, "/api/v1" + route, headers=headers)
                if res.status_code != 200:
                    raise RuntimeError("fixture route absent")
                return res.json()

        owner_cli.diagnostics(correlation, Client())
        assert output == [expected]


def test_cli_accepts_restore_reentry_detail():
    value = record(stage="auth_restore")
    value.pop("os_status")
    value.update(code="credential_locked", reason="credential_reentry_required",
                 error_class="credential_reentry_required")
    assert owner_cli._valid_restore_diagnostic(value, value["correlation_id"])


def test_fresh_process_loads_committed_detail(tmp_path):
    assert store(tmp_path).put(record())
    code = "from app.services.auth_diagnostics import AuthDiagnosticStore; import json,sys; print(json.dumps(AuthDiagnosticStore(sys.argv[1]).get(sys.argv[2], 'auth_remember')))"
    env = {"PYTHONPATH": str(Path(__file__).parents[1]), "PYTHONDONTWRITEBYTECODE": "1"}
    child = subprocess.run([sys.executable, "-c", code, str(tmp_path), record()["correlation_id"]],
                           env=env, capture_output=True, text=True, timeout=10)
    assert child.returncode == 0
    assert json.loads(child.stdout) == record()
    assert (tmp_path / "auth-diagnostics.json").stat().st_mode & 0o777 == 0o600


def test_trim_per_stage_and_duplicate_moves_to_newest(tmp_path):
    for stage in ("auth_remember", "auth_restore"):
        for number in range(1, 26):
            assert store(tmp_path).put(record(number, stage))
    reader = store(tmp_path)
    assert reader.get(record(5)["correlation_id"], "auth_remember") is None
    assert reader.get(record(6)["correlation_id"], "auth_remember") == record(6)
    assert reader.get(record(25)["correlation_id"], "auth_restore") == record(25, "auth_restore")
    assert reader.put(record(6))
    assert reader.put(record(26))
    assert reader.get(record(6)["correlation_id"], "auth_remember") == record(6)
    assert reader.get(record(7)["correlation_id"], "auth_remember") is None
    assert len(json.loads((tmp_path / "auth-diagnostics.json").read_text())["records"]) == 40


@pytest.mark.parametrize("change", [
    {"secret": "sensitive-test-sentinel"}, {"correlation_id": "not-a-correlation"},
    {"correlation_id": []}, {"stage": "unknown"}, {"code": "arbitrary-text"},
    {"reason": "sensitive-test-sentinel"}, {"error_class": "unknown"},
    {"cleanup": "unknown-text"}, {"operation": "unknown"}, {"operation": []},
    {"os_status": True}, {"os_status": None}, {"os_status": float("nan")},
    {"os_status": 2 ** 31}, {"os_status": -(2 ** 31) - 1}, {"code": []},
])
def test_untrusted_fields_never_persist(tmp_path, change):
    assert not store(tmp_path).put({**record(), **change})
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("content", [b"broken", b"x" * 65537,
    b'{"version":1,"records":[],"secret":"sensitive-test-sentinel"}',
    b'{"version":1,"version":1,"records":[]}',
    b'{"version":1,"records":[{}]}'])
def test_bad_file_preserved_and_lookup_closed(tmp_path, content):
    path = tmp_path / "auth-diagnostics.json"
    path.write_bytes(content)
    path.chmod(0o600)
    assert store(tmp_path).get(record()["correlation_id"], "auth_remember") is None
    assert not store(tmp_path).put(record())
    assert path.read_bytes() == content


@pytest.mark.skipif(os.name != "posix", reason="POSIX file boundary")
@pytest.mark.parametrize("kind", ["permissions", "symlink", "hardlink", "fifo", "root_permissions", "root_symlink"])
def test_unsafe_files_never_followed_or_overwritten(tmp_path, kind):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    path = root / "auth-diagnostics.json"
    outside = tmp_path / "outside"
    outside.write_bytes(b"preserve-me")
    if kind == "permissions":
        path.write_bytes(b"preserve-me"); path.chmod(0o644)
    elif kind == "symlink":
        path.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, path); path.chmod(0o600)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "root_permissions":
        root.chmod(0o755)
    else:
        link = tmp_path / "link"; link.symlink_to(root, target_is_directory=True); root = link
    assert not store(root).put(record())
    assert store(root).get(record()["correlation_id"], "auth_remember") is None
    assert outside.read_bytes() == b"preserve-me"
    if kind == "permissions":
        assert path.stat().st_mode & 0o777 == 0o644


def test_cross_instance_threads_merge_records(tmp_path):
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(lambda number: store(tmp_path).put(record(number)), range(1, 16)))
    reader = store(tmp_path)
    assert all(reader.get(record(n)["correlation_id"], "auth_remember") == record(n) for n in range(1, 16))


def test_windows_read_branch_rejects_link_before_following(tmp_path, monkeypatch):
    from app.services import auth_diagnostics
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"version": 1, "records": [record()]}))
    outside.chmod(0o600)
    (root / "auth-diagnostics.json").symlink_to(outside)
    reader = store(root)

    class WindowsOS:
        name = "nt"
        O_NOFOLLOW = 0

        def __getattr__(self, name):
            return getattr(os, name)

    monkeypatch.setattr(auth_diagnostics, "os", WindowsOS())
    assert reader.get(record()["correlation_id"], "auth_remember") is None


def test_windows_created_temp_removed_when_descriptor_validation_fails(tmp_path, monkeypatch):
    from app.services import auth_diagnostics
    writer = store(tmp_path)

    class WindowsOS:
        name = "nt"
        O_NOFOLLOW = 0

        def fstat(self, _):
            raise OSError("fixture metadata unavailable")

        def __getattr__(self, name):
            return getattr(os, name)

    monkeypatch.setattr(auth_diagnostics, "os", WindowsOS())
    assert not writer.put(record())
    assert not list(tmp_path.iterdir())


def test_constructor_disabled_store_and_explicit_test_database_no_disk(tmp_path, monkeypatch):
    missing = tmp_path / "missing"
    store(missing)
    assert not missing.exists()
    assert not store(None).put(record())
    monkeypatch.setenv("GAMJA_APP_DATA_DIR", str(missing))
    assert RuntimeConfig.from_environment("sqlite:///:memory:").auth_diagnostic_dir is None
    assert RuntimeConfig.from_environment().auth_diagnostic_dir == str(missing)


def test_make_auth_report_diagnostic_converts_remember_record():
    from app.services.auth_diagnostics import make_auth_report_diagnostic
    value = record(stage="auth_remember")
    result = make_auth_report_diagnostic(value)
    assert result is not None
    assert result["version"] == 1
    assert result["correlation_id"] == value["correlation_id"]
    assert result["stage"] == "auth_remember"
    assert result["failure_code"] == value["code"]
    assert result["reason"] == value["reason"]
    assert result["error_class"] == value["error_class"]
    assert result["room_id"] is None
    assert result["agent_id"] is None
    assert result["connection_id"] is None
    assert result["trigger_event_id"] is None
    assert result["action_id"] is None
    assert result["exception_chain"] == []
    assert result["frames"] == []
    assert result["os_status"] == -25293
    assert result["operation"] == "sec_item_add"
    assert result["cleanup"] == "success"
    assert "occurred_at" in result
    assert result["occurred_at"].endswith("Z")
    assert "http_status" not in result
    assert "vendor_request_id" not in result


def test_make_auth_report_diagnostic_converts_restore_record():
    from app.services.auth_diagnostics import make_auth_report_diagnostic
    value = record(stage="auth_restore")
    result = make_auth_report_diagnostic(value)
    assert result is not None
    assert result["stage"] == "auth_restore"
    assert result["failure_code"] == value["code"]
    assert result["os_status"] == -25293
    assert "operation" not in result
    assert "cleanup" not in result


def test_make_auth_report_diagnostic_rejects_invalid_record():
    from app.services.auth_diagnostics import make_auth_report_diagnostic
    assert make_auth_report_diagnostic({"secret": "sensitive-test-sentinel"}) is None
    assert make_auth_report_diagnostic({**record(), "stage": "unknown"}) is None
    assert make_auth_report_diagnostic({**record(), "code": "arbitrary-text"}) is None


def test_record_auth_diagnostic_emits_via_pipe(sqlite_url, tmp_path, monkeypatch):
    import stat
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from app.main import create_app
    written = []
    monkeypatch.setattr("app.services.diagnostics.os.fstat", lambda _fd: SimpleNamespace(st_mode=stat.S_IFIFO))
    monkeypatch.setattr("app.services.diagnostics.os.write", lambda _fd, value: written.append(value))
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "diagnostic-fixture-session")
    headers = {"x-gamja-session": "diagnostic-fixture-session"}
    with TestClient(create_app()) as client:
        client.app.state.login_secret_store = FailedSecretStore()
        account = {"email": "emit@example.com", "username": "emit", "password": "fixture-password-long"}
        assert client.post("/api/v1/auth/register", headers=headers, json=account).status_code == 200
        response = client.post("/api/v1/auth/login", headers=headers, json={**account, "remember_device": True})
        assert response.status_code == 503
        correlation = response.json()["detail"]["correlation_id"]
    assert len(written) == 1
    assert written[0].startswith(b"GAMJA_DIAGNOSTIC_V2:")
    assert correlation.encode() in written[0]
    assert b"auth_remember" in written[0]
    assert b"emit@example.com" not in written[0]


def test_emission_failure_does_not_alter_auth_outcome(sqlite_url, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    def fail_write(*_):
        raise OSError("fixture pipe failure")
    monkeypatch.setattr("app.services.diagnostics.os.write", fail_write)
    import stat
    from types import SimpleNamespace
    monkeypatch.setattr("app.services.diagnostics.os.fstat", lambda _fd: SimpleNamespace(st_mode=stat.S_IFIFO))
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "diagnostic-fixture-session")
    headers = {"x-gamja-session": "diagnostic-fixture-session"}
    with TestClient(create_app()) as client:
        client.app.state.login_secret_store = FailedSecretStore()
        account = {"email": "emitfail@example.com", "username": "emitfail", "password": "fixture-password-long"}
        assert client.post("/api/v1/auth/register", headers=headers, json=account).status_code == 200
        response = client.post("/api/v1/auth/login", headers=headers, json={**account, "remember_device": True})
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "secret_store_unavailable"


def test_get_diagnostic_does_not_re_emit(sqlite_url, tmp_path, monkeypatch):
    import stat
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from app.main import create_app
    written = []
    monkeypatch.setattr("app.services.diagnostics.os.fstat", lambda _fd: SimpleNamespace(st_mode=stat.S_IFIFO))
    monkeypatch.setattr("app.services.diagnostics.os.write", lambda _fd, value: written.append(value))
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "diagnostic-fixture-session")
    headers = {"x-gamja-session": "diagnostic-fixture-session"}
    with TestClient(create_app()) as client:
        client.app.state.login_secret_store = FailedSecretStore()
        account = {"email": "noemit@example.com", "username": "noemit", "password": "fixture-password-long"}
        assert client.post("/api/v1/auth/register", headers=headers, json=account).status_code == 200
        response = client.post("/api/v1/auth/login", headers=headers, json={**account, "remember_device": True})
        assert response.status_code == 503
        correlation = response.json()["detail"]["correlation_id"]
        count_after_login = len(written)
        path = f"/api/v1/auth/remember/diagnostics/{correlation}"
        assert client.get(path, headers=headers).status_code == 200
        assert client.get(path, headers=headers).status_code == 200
        assert len(written) == count_after_login


def test_corrupt_diagnostic_file_does_not_break_login(sqlite_url, tmp_path, monkeypatch):
    root = tmp_path / "appdata"
    root.mkdir(mode=0o700)
    path = root / "auth-diagnostics.json"
    path.write_bytes(b"preserve-corrupt-file"); path.chmod(0o600)
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "diagnostic-fixture-session")
    headers = {"x-gamja-session": "diagnostic-fixture-session"}
    with TestClient(create_app()) as client:
        client.app.state.login_secret_store = FailedSecretStore()
        account = {"email": "diagnostic@example.com", "username": "diagnostic",
                   "password": "fixture-password-long"}
        assert client.post("/api/v1/auth/register", headers=headers, json=account).status_code == 200
        failed = client.post("/api/v1/auth/login", headers=headers, json={**account, "remember_device": True})
        assert failed.status_code == 503
        assert failed.json()["detail"]["code"] == "secret_store_unavailable"
        assert client.post("/api/v1/auth/login", headers=headers, json=account).status_code == 200
    assert path.read_bytes() == b"preserve-corrupt-file"
