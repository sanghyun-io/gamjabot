"""Safety tests for the operator-only harness, never shipped authentication hooks."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/verify_cli_core.py"


@pytest.fixture
def harness():
    assert SCRIPT.is_file(), "verification harness must exist"
    spec = importlib.util.spec_from_file_location("verify_cli_core", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def endpoint(root, **updates):
    value = {"version": 1, "core_version": 1, "engine_version": 1,
             "api_url": "http://127.0.0.1:41231", "control_url": "http://127.0.0.1:41232",
             "capability": "ab" * 32}
    value.update(updates)
    path = root / "owner-cli.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def test_endpoint_rejects_nonprivate_symlink_and_nonloopback(harness, tmp_path):
    path = endpoint(tmp_path)
    assert harness.read_endpoint(path)["version"] == 1
    path.chmod(0o644)
    with pytest.raises(harness.GateFailure, match="endpoint_untrusted"):
        harness.read_endpoint(path)
    path.unlink()
    source = tmp_path / "real.json"
    source.write_text("{}")
    path.symlink_to(source)
    with pytest.raises(harness.GateFailure, match="endpoint_untrusted"):
        harness.read_endpoint(path)
    path.unlink()
    endpoint(tmp_path, control_url="http://example.com:80")
    with pytest.raises(harness.GateFailure, match="endpoint_untrusted"):
        harness.read_endpoint(path)


def test_endpoint_rejects_duplicate_fields(harness, tmp_path):
    path = endpoint(tmp_path)
    path.write_text(path.read_text().replace('"version": 1', '"version": 1, "version": 1'))
    with pytest.raises(harness.GateFailure, match="endpoint_untrusted"):
        harness.read_endpoint(path)


def test_endpoint_fifo_is_rejected_without_waiting_for_a_writer(harness, tmp_path):
    path = tmp_path / "owner-cli.json"
    os.mkfifo(path, mode=0o600)
    with pytest.raises(harness.GateFailure, match="endpoint_untrusted"):
        harness.read_endpoint(path)


def test_private_fixture_does_not_inherit_credentials_and_cleans_on_exception(harness, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "must-not-inherit")
    monkeypatch.setenv("SECRET_KEY", "must-not-inherit")
    monkeypatch.setenv("HTTP_PROXY", "http://must-not-inherit")
    fixture = harness.PrivateFixture()
    with pytest.raises(RuntimeError, match="fixture failure"):
        with fixture:
            root = fixture.root
            assert root.stat().st_mode & 0o777 == 0o700
            home_preserved = fixture.env.get("HOME") == os.getenv("HOME")
            assert home_preserved
            assert not set(fixture.env) & {"CODEX_HOME", "GAMJA_LOCAL_SESSION_TOKEN", "SECRET_KEY", "HTTP_PROXY"}
            child = fixture.spawn([sys.executable, "-c", "import time; time.sleep(120)"])
            raise RuntimeError("fixture failure")
    assert child.poll() is not None
    assert not root.exists()
    assert fixture.cleanup["processes_exited"] is True
    assert fixture.cleanup["temporary_root_removed"] is True


def test_command_timeout_reaps_owned_process(harness):
    with harness.PrivateFixture() as fixture:
        with pytest.raises(harness.GateFailure, match="command_timeout"):
            fixture.command([sys.executable, "-c", "import time; time.sleep(120)"], timeout=.05)
        assert all(child.poll() is not None for child in fixture.children)


def test_cleanup_kills_term_resistant_owned_descendant_before_reaping_leader(harness):
    fixture = harness.PrivateFixture()
    with fixture:
        marker = fixture.root / "descendant.pid"
        child_code = "import os,pathlib,signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(120)"
        parent_code = "import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', sys.argv[1],sys.argv[2]]); time.sleep(120)"
        child = fixture.spawn([sys.executable, "-c", parent_code, child_code, str(marker)])
        end = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < end:
            time.sleep(.01)
        assert marker.exists()
        pid = int(marker.read_text())
        identity = harness.process_identity(pid)
        assert identity is not None
    assert child.poll() is not None
    assert harness.process_identity(pid) != identity
    assert not fixture.root.exists()


def test_cleanup_keeps_exited_leader_owned_until_resistant_descendant_is_gone(harness):
    fixture = harness.PrivateFixture()
    pid = None
    try:
        with fixture:
            marker = fixture.root / "descendant.pid"
            descendant = "import os,pathlib,signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(8)"
            leader = "import pathlib,subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); p=pathlib.Path(sys.argv[2]);\nwhile not p.exists(): time.sleep(.01)"
            child = fixture.spawn([sys.executable, "-c", leader, descendant, str(marker)])
            end = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < end:
                time.sleep(.01)
            assert marker.exists()
            pid = int(marker.read_text())
            time.sleep(.1)  # Leader exits on its own; deliberately do not poll/reap it.
        end = time.monotonic() + 2
        while harness.process_identity(pid) is not None and time.monotonic() < end:
            time.sleep(.01)
        assert harness.process_identity(pid) is None
        assert fixture.cleanup["processes_exited"] is True
        assert not fixture.root.exists()
        assert child.returncode is not None
    finally:
        # Bounded self-exit prevents a failing regression leaving a live fixture.
        end = time.monotonic() + 9
        while pid and harness.process_identity(pid) is not None and time.monotonic() < end:
            time.sleep(.02)


@pytest.mark.parametrize("external_wait", ["popen", "waitpid"])
def test_reaped_leader_never_authorizes_numeric_group_signal(harness, monkeypatch, external_wait):
    fixture = harness.PrivateFixture()
    fixture.__enter__()
    child = fixture.spawn([sys.executable, "-c", "pass"])
    # Exercise both Popen's known returncode and ECHILD from an external reaper.
    if external_wait == "popen":
        child.wait(timeout=3)
    else:
        _, status = os.waitpid(child.pid, 0)
    signals = []
    monkeypatch.setattr(harness.os, "killpg", lambda *args: signals.append(args))
    try:
        with pytest.raises(harness.GateFailure, match="cleanup_ownership_unproven"):
            fixture.__exit__(None, None, None)
        assert signals == []
        assert fixture.cleanup["processes_exited"] is False
        assert fixture.root.exists()
    finally:
        import shutil
        if external_wait == "waitpid":
            child.returncode = os.waitstatus_to_exitcode(status)
        if fixture.root.exists():
            shutil.rmtree(fixture.root)


def test_completed_command_cleans_descendants_before_final_reap(harness, monkeypatch):
    fixture = harness.PrivateFixture()
    with fixture:
        marker = fixture.root / "descendant.pid"
        descendant = "import os,pathlib,signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(8)"
        leader = "import pathlib,subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); p=pathlib.Path(sys.argv[2]);\nwhile not p.exists(): time.sleep(.01)\nprint('{}')"
        original_signal = harness.os.killpg
        signals = []
        def signal_owned(group, number):
            assert group not in fixture.finalized
            signals.append((group, number))
            return original_signal(group, number)
        monkeypatch.setattr(harness.os, "killpg", signal_owned)
        assert fixture.command([sys.executable, "-c", leader, descendant, str(marker)]) == [{}]
        pid = int(marker.read_text())
        assert harness.process_identity(pid) is None
        count = len(signals)
    assert len(signals) == count  # Finalized numeric IDs are never signaled again.


def test_command_error_never_exposes_child_output(harness):
    with harness.PrivateFixture() as fixture:
        with pytest.raises(harness.GateFailure) as error:
            fixture.command([sys.executable, "-c", "import sys; print('sentinel-password'); sys.exit(4)"])
    assert str(error.value) == "command_failed"
    assert "sentinel-password" not in str(error.value)


def test_http_is_local_bounded_and_never_follows_redirect(harness):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", "https://example.com/never")
            self.send_header("Content-Length", "0")
            self.end_headers()
        def log_message(self, *_):
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        status, body = harness.local_json(f"http://127.0.0.1:{server.server_port}", "/control", "ab" * 32, {"operation": "health"})
        assert status == 302
        assert body is None
        with pytest.raises(harness.GateFailure, match="endpoint_untrusted"):
            harness.local_json("http://example.com:80", "/control", "ab" * 32, {})
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_http_rejects_oversized_response(harness):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            content = b"x" * 73729
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        def log_message(self, *_):
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with pytest.raises(harness.GateFailure, match="response_too_large"):
            harness.local_json(f"http://127.0.0.1:{server.server_port}", "/health", "ab" * 32)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("slow_headers", [True, False])
def test_http_deadline_bounds_peer_making_continuous_progress(harness, slow_headers):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    def peer():
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(1)
                connection.recv(4096)
                headers = b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\n"
                pieces = [bytes([byte]) for byte in headers] if slow_headers else [headers]
                for piece in pieces:
                    connection.sendall(piece)
                    if slow_headers:
                        time.sleep(.02)
                for byte in b'"okay"':
                    connection.sendall(bytes([byte]))
                    time.sleep(.05)
        except OSError:
            pass
    thread = threading.Thread(target=peer)
    thread.start()
    try:
        started = time.monotonic()
        with pytest.raises(harness.GateFailure):
            harness.local_json(f"http://127.0.0.1:{port}", "/health", "ab" * 32, timeout=.1)
        assert time.monotonic() - started < .22
    finally:
        listener.close()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.parametrize('reply', ['slow_headers', 'slow_body', 'oversize'])
def test_api_reader_bounds_deadline_and_body_before_buffering(harness, reply):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    request = []
    def peer():
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(1)
                request.append(connection.recv(4096))
                header = (b'HTTP/1.1 200 OK\r\nContent-Length: 73729\r\n\r\n' if reply == 'oversize'
                    else b'HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\n')
                for piece in ([bytes([byte]) for byte in header] if reply == 'slow_headers' else [header]):
                    connection.sendall(piece)
                    if reply == 'slow_headers': time.sleep(.02)
                if reply == 'oversize': return  # Reject length before waiting for body.
                for byte in b'"okay"':
                    connection.sendall(bytes([byte]))
                    time.sleep(.05)
        except OSError:
            pass
    thread = threading.Thread(target=peer)
    thread.start()
    try:
        started = time.monotonic()
        with pytest.raises(harness.GateFailure):
            harness.local_api_json(f'http://127.0.0.1:{listener.getsockname()[1]}', '/api/v1/auth/restore',
                'synthetic-session-token-' * 2, jwt='eyJfixture.eyJfixture.signature', method='POST', timeout=.1)
        assert time.monotonic() - started < .22
        assert b'Origin: tauri://localhost' in request[0]
        assert b'Authorization: Bearer eyJfixture.eyJfixture.signature' in request[0]
    finally:
        listener.close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_api_reader_rejects_header_injection_and_unplanned_routes(harness):
    for path, session, jwt in [('/api/v1/auth/restore', 'safe\r\nInjected: value', None),
            ('/api/v1/auth/restore', 'safe-' * 10, 'value\r\nInjected: value'),
            ('/api/v1/auth/restore\r\nInjected', 'safe-' * 10, None),
            ('/api/v1/unplanned', 'safe-' * 10, None)]:
        with pytest.raises(harness.GateFailure, match='invalid_fixture_request'):
            harness.local_api_json('http://127.0.0.1:41234', path, session, jwt=jwt)


def test_receipt_is_bounded_private_and_refuses_existing_file(harness, tmp_path):
    path = tmp_path / "receipt.json"
    value = {"format": 1, "status": "failed", "failure": "command_failed"}
    harness.write_receipt(path, value)
    assert json.loads(path.read_text()) == value
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(harness.GateFailure, match="receipt_exists"):
        harness.write_receipt(path, value)
    with pytest.raises(harness.GateFailure, match="receipt_too_large"):
        harness.write_receipt(tmp_path / "large.json", {"value": "x" * 16384})
    assert not (tmp_path / "large.json").exists()


def test_cli_failure_still_emits_cleanup_receipt(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), "--package", str(tmp_path / "missing"),
                             "--receipt", str(tmp_path / "receipt.json")], capture_output=True, text=True)
    assert result.returncode == 1
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["status"] == "failed"
    assert receipt["failure"] == "package_invalid"
    assert receipt["gates"]["installed_gui_persistence"] == "not_run"
    assert str(tmp_path) not in json.dumps(receipt)


def test_unexpected_fixture_error_is_sanitized_and_disposes_root(harness, monkeypatch, tmp_path):
    entry = tmp_path / "entry.py"
    entry.write_text("# never executed")
    roots = []
    def fail(fixture, *_):
        roots.append(fixture.root)
        raise RuntimeError("sentinel-secret-and-owner-path")
    monkeypatch.setattr(harness, "exercise", fail)
    receipt_path = tmp_path / "receipt.json"
    assert harness.main(["--core", sys.executable, "--python", sys.executable,
                         "--engine-entry", str(entry), "--receipt", str(receipt_path)]) == 1
    receipt = json.loads(receipt_path.read_text())
    assert receipt["failure"] == "harness_execution_failed"
    assert "sentinel" not in json.dumps(receipt)
    assert receipt["cleanup"]["temporary_root_removed"] is True
    assert roots and all(not root.exists() for root in roots)


def test_sigterm_runs_finally_and_writes_cleanup_receipt(harness, monkeypatch, tmp_path):
    entry = tmp_path / "entry.py"
    entry.write_text("# never executed")
    children = []
    previous = signal.getsignal(signal.SIGTERM)
    def interrupt(fixture, *_):
        children.append(fixture.spawn([sys.executable, "-c", "import time; time.sleep(120)"]))
        signal.raise_signal(signal.SIGTERM)
    monkeypatch.setattr(harness, "exercise", interrupt)
    path = tmp_path / "receipt.json"
    assert harness.main(["--core", sys.executable, "--python", sys.executable,
                         "--engine-entry", str(entry), "--receipt", str(path)]) == 1
    receipt = json.loads(path.read_text())
    assert receipt["failure"] == "harness_interrupted"
    assert all(receipt["cleanup"].values())
    assert children[0].poll() is not None
    assert signal.getsignal(signal.SIGTERM) == previous


def test_scripted_account_rows_use_real_canonical_command_parser(harness, monkeypatch, tmp_path, capsys):
    monkeypatch.syspath_prepend(str(SCRIPT.parents[1] / "backend"))
    from app import owner_cli
    from io import StringIO
    account = {"email": "contract-fixture@example.com", "username": "contract-fixture"}
    user = {"id": 1, **account}
    class API:
        token = None
        def call(self, method, path, payload=None, **options):
            if (method, path) == ("POST", "/auth/register"):
                return user
            if (method, path) == ("POST", "/auth/login"):
                assert payload["remember_device"] is False
                assert payload["preserve_remembered_login"] is True
                return {"access_token": "fixture-memory-token"}
            assert self.token == "fixture-memory-token" and options.get("authenticated") is True
            if (method, path) == ("GET", "/auth/me"):
                return user
            assert (method, path) in {("GET", "/connections"), ("GET", "/projects/"), ("GET", "/agents/")}
            return []
    for register in (True, False):
        rows = harness.account_rows(account, register)
        source = tmp_path / "account.ndjson"
        source.write_text("".join(json.dumps(row) + "\n" for row in rows))
        monkeypatch.setattr(owner_cli.sys, "stdin", StringIO("synthetic-only-password\n" * (2 if register else 1)))
        owner_cli.batch(API(), str(source))
        output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert len(output) == 5 + int(register)
        assert output[int(register)] == {"authenticated": True}
        assert output[int(register) + 1] == user
        assert output[int(register) + 2:] == [[], [], []]


def test_all_scripted_report_words_use_real_native_cli_adapter(harness):
    core = SCRIPT.parents[1] / "dist/core/aarch64-apple-darwin/gamja-core/bin/gamja"
    if not core.is_file():
        pytest.skip("native core adapter must be built for this contract check")
    report_id = "cd" * 16
    received = []
    replies = {
        "report_status": {"auto_reporting_enabled": False, "consent_generation": 0, "submission_available": False},
        "report_list": {"events": [], "reports": [], "auto_reporting_enabled": False, "consent_generation": 0},
        "report_create": {"envelope": {"schema_version": 1, "report_id": report_id, "mode": "manual"}, "body": "{}"},
        "report_export": "fixture-export.json",
        "report_delete": None,
    }
    class Control(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(request)
            recognized = request["operation"] in replies
            body = json.dumps(replies[request["operation"]] if recognized else {"code": "unexpected_fixture_operation"}).encode()
            self.send_response(200 if recognized else 503)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_):
            pass
    server = HTTPServer(("127.0.0.1", 0), Control)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with harness.PrivateFixture() as fixture:
            path = endpoint(fixture.root / "appdata", control_url=f"http://127.0.0.1:{server.server_port}")
            for operation in harness.REPORT_OPERATIONS:
                words = harness.report_words(operation, report_id if operation in {"export", "delete"} else None)
                assert fixture.command([str(core), "--app-data", str(fixture.root / "appdata"), *words]) == [replies[f"report_{operation}"]]
            path.unlink()  # The test owns this HTTP fixture, not a running core.
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert received == [
        {"operation": "report_status"}, {"operation": "report_list"},
        {"operation": "report_create", "event_ids": [], "description": None},
        {"operation": "report_export", "report_id": report_id},
        {"operation": "report_delete", "report_id": report_id},
    ]
