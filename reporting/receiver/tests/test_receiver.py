import copy
import http.client
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from reporting.receiver.server import make_server  # noqa: E402


VALID_FIXTURES = json.loads((ROOT / "reporting/fixtures/valid.json").read_text())
VALID = VALID_FIXTURES[0]
INVALID = json.loads((ROOT / "reporting/fixtures/invalid.json").read_text())


@contextmanager
def receiver(db_path, **options):
    logs = io.StringIO()
    server = make_server(db_path, port=0, log_sink=logs, **options)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, logs
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def post(server, payload, *, headers=None, path="/reports"):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        conn.request("POST", path, body=body, headers={"Content-Type": "application/json", **(headers or {})})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def get(server, path):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def test_receipt_is_durable_private_and_survives_restart(tmp_path):
    db = tmp_path / "private" / "reports.sqlite3"
    payload = copy.deepcopy(VALID)
    with receiver(db) as (server, _):
        status, body = post(server, payload)
        assert status == 201
        receipt = json.loads(body)
        assert set(receipt) == {"report_id", "receipt_id", "accepted_at"}
        assert receipt["report_id"] == payload["report_id"]
        assert len(receipt["receipt_id"]) == 32
        assert get(server, "/reports")[0] == 405
        assert get(server, "/reports/" + receipt["receipt_id"])[0] == 405
    assert db.stat().st_mode & 0o777 == 0o600
    assert db.parent.stat().st_mode & 0o777 == 0o700
    with receiver(db) as (server, _):
        assert post(server, payload) == (201, body)
        assert server.store.count() == 1


@pytest.mark.parametrize("payload", INVALID)
def test_contract_rejects_invalid_fixture(tmp_path, payload):
    with receiver(tmp_path / "reports.sqlite3") as (server, _):
        assert post(server, payload)[0] in (400, 413)
        assert server.store.count() == 0


def test_valid_fixtures_under_wire_limit_are_accepted(tmp_path):
    for index, payload in enumerate(VALID_FIXTURES):
        with receiver(tmp_path / f"reports-{index}.sqlite3") as (server, _):
            raw_utf8 = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            assert len(raw_utf8) <= 4096
            assert post(server, raw_utf8)[0] == 201
    with receiver(tmp_path / "escaped.sqlite3") as (server, _):
        ascii_escaped = json.dumps(VALID_FIXTURES[2], separators=(",", ":")).encode()
        assert len(ascii_escaped) > 4096
        assert post(server, ascii_escaped)[0] == 413


def test_optional_sanitized_diagnostic_is_accepted_without_breaking_legacy_v1(tmp_path):
    detail = {"version": 1, "correlation_id": "a" * 32, "occurred_at": "2026-09-30T01:00:00Z",
              "stage": "tool_execution", "failure_code": "provider_unavailable", "reason": "unexpected_error", "error_class": "value_error",
              "room_id": 4, "agent_id": 7, "connection_id": 3, "trigger_event_id": None,
              "action_id": None, "exception_chain": [{"module": "builtins", "class": "ValueError"}],
              "frames": [{"file": "app/services/scheduler.py", "function": "_run", "line": 382}],
              "http_status": 503, "vendor_request_id": "req_123"}
    with receiver(tmp_path / "reports.sqlite3") as (server, _):
        assert post(server, VALID)[0] == 201
        enriched = {**VALID, "report_id": "b" * 32, "diagnostics": [detail]}
        assert post(server, enriched)[0] == 201
        assert server.store.count() == 2


def test_producer_bounded_module_and_native_code_remain_accepted(tmp_path):
    detail = {"version": 1, "correlation_id": "a" * 32, "occurred_at": "2026-09-30T01:00:00Z",
              "stage": "tool_execution", "failure_code": "accessibility_denied", "reason": "accessibility_denied",
              "error_class": "native_refusal", "room_id": 4, "agent_id": 7, "connection_id": None,
              "trigger_event_id": None, "action_id": None,
              "exception_chain": [{"module": "app." + "a" * 76, "class": "Error"}], "frames": []}
    with receiver(tmp_path / "bounded.sqlite3") as (server, _):
        assert post(server, {**VALID, "diagnostics": [detail]})[0] == 201


def test_backend_producer_fixture_is_accepted_as_report_detail(tmp_path):
    line = (ROOT / "reporting/fixtures/diagnostic-v2-line.txt").read_bytes()
    assert len(line) <= 2304 and line.startswith(b"GAMJA_DIAGNOSTIC_V2:")
    assert b"synthetic-private-value" not in line
    detail = json.loads(line.removeprefix(b"GAMJA_DIAGNOSTIC_V2:"))["diagnostic"]
    assert detail["frames"][0]["file"] == "app/services/synthetic_error.py"
    with receiver(tmp_path / "reports.sqlite3") as (server, _):
        payload = {**VALID, "diagnostics": [detail]}
        assert post(server, payload)[0] == 201
        assert server.store.count() == 1


@pytest.mark.parametrize("change", [
    {"secret": "token"}, {"frames": [{"file": "/Users/alice/private.py", "function": "run", "line": 1}]},
    {"exception_chain": [{"module": "private.module", "class": "SecretError"}]},
    {"vendor_request_id": "Authorization: secret"}, {"vendor_request_id": ".secret"},
    {"correlation_id": "not-hex"},
    {"stage": []}, {"reason": {}}, {"room_id": True},
])
def test_sanitized_diagnostic_rejects_freeform_content(tmp_path, change):
    detail = {"version": 1, "correlation_id": "a" * 32, "occurred_at": "2026-09-30T01:00:00Z",
              "stage": "tool_execution", "failure_code": "provider_unavailable", "reason": "unexpected_error", "error_class": "value_error",
              "room_id": 4, "agent_id": 7, "connection_id": 3, "trigger_event_id": None,
              "action_id": None, "exception_chain": [], "frames": []}
    detail.update(change)
    with receiver(tmp_path / "reports.sqlite3") as (server, _):
        assert post(server, {**VALID, "diagnostics": [detail]})[0] == 400


@pytest.mark.parametrize("body", [b"{", b"\xff", b'{"schema_version":1,"schema_version":1}', b"NaN"])
def test_malformed_json_or_utf8_is_rejected(tmp_path, body):
    with receiver(tmp_path / "reports.sqlite3") as (server, _):
        assert post(server, body)[0] == 400


def test_escaped_unpaired_surrogate_is_rejected(tmp_path):
    body = json.dumps({**VALID, "description": "\ud800"}).encode()
    with receiver(tmp_path / "reports.sqlite3") as (server, _):
        assert post(server, body)[0] == 400


@pytest.mark.parametrize("field,value", [
    ("os", "linux"), ("mode", "background"), ("component", "secret"),
    ("stage", "error"), ("code", "raw_error"), ("arch", "arm"),
    ("os_version", "Darwin private"), ("occurred_at", "2026-09-29T00:00:00+09:00"),
    ("occurrences", True), ("report_id", "../../private"),
])
def test_strict_field_validation(tmp_path, field, value):
    payload = {**VALID, field: value}
    with receiver(tmp_path / "reports.sqlite3") as (server, _):
        assert post(server, payload)[0] == 400


def test_accepts_safe_ascii_and_hex_forms_allowed_by_rust(tmp_path):
    payload = {**VALID, "report_id": "ABCDEF0123456789ABCDEF0123456789", "app_version": ".1", "build_id": "+build"}
    with receiver(tmp_path / "reports.sqlite3") as (server, _):
        assert post(server, payload)[0] == 201


def test_boundary_and_transfer_framing(tmp_path):
    with receiver(tmp_path / "reports.sqlite3") as (server, _):
        assert post(server, b" " * 4097)[0] == 413
        assert post(server, VALID, headers={"Content-Type": "text/plain"})[0] == 415
        sock = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        try:
            sock.sendall(b"POST /reports HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n")
            assert b" 400 " in sock.recv(1024)
        finally:
            sock.close()
        sock = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        try:
            huge = b"9" * 5000
            sock.sendall(b"POST /reports HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: " + huge + b"\r\n\r\n")
            assert b" 413 " in sock.recv(1024)
        finally:
            sock.close()
        sock = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        try:
            sock.sendall(b"POST /reports HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\n{}")
            assert b" 400 " in sock.recv(1024)
        finally:
            sock.close()
        sock = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        try:
            sock.sendall(b"POST /reports HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n\r\n")
            assert b" 411 " in sock.recv(1024)
        finally:
            sock.close()


def test_same_bytes_replay_and_changed_bytes_conflict(tmp_path):
    with receiver(tmp_path / "reports.sqlite3") as (server, _):
        body = json.dumps(VALID).encode()
        first = post(server, body)
        assert first[0] == 201
        assert json.loads(first[1])["report_id"] == VALID["report_id"]
        assert post(server, body) == first
        changed = json.dumps({**VALID, "occurrences": 2}).encode()
        assert post(server, changed)[0] == 409
        assert server.store.count() == 1


def test_failed_commit_never_yields_receipt(tmp_path):
    db = tmp_path / "reports.sqlite3"
    with receiver(db) as (server, _):
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TRIGGER fail_reports BEFORE INSERT ON reports BEGIN SELECT RAISE(ABORT, 'failed'); END")
        assert post(server, VALID)[0] == 503
        assert server.store.count() == 0
        with sqlite3.connect(db) as conn:
            conn.execute("DROP TRIGGER fail_reports")
        assert post(server, VALID)[0] == 201


def test_rate_and_capacity_refuse_without_storing(tmp_path):
    with receiver(tmp_path / "rate.sqlite3", rate_global=1, rate_per_ip=1) as (server, _):
        assert post(server, VALID)[0] == 201
        assert post(server, {**VALID, "report_id": "a" * 32})[0] == 429
        assert server.store.count() == 1
    with receiver(tmp_path / "capacity.sqlite3", max_reports=1) as (server, _):
        assert post(server, VALID)[0] == 201
        assert post(server, {**VALID, "report_id": "b" * 32})[0] == 507
        assert server.store.count() == 1


def test_startup_and_daily_prune_and_operator_delete(tmp_path):
    db = tmp_path / "reports.sqlite3"
    now = 2_000_000_000.0
    current = [now]
    with receiver(db, clock=lambda: current[0]) as (server, _):
        status, body = post(server, VALID)
        assert status == 201
        receipt = json.loads(body)["receipt_id"]
    current[0] += 31 * 86400
    with receiver(db, clock=lambda: current[0]) as (server, _):
        assert server.store.count() == 0
        next_payload = {**VALID, "report_id": "c" * 32}
        assert post(server, next_payload)[0] == 201
        current[0] += 31 * 86400
        assert post(server, {**VALID, "report_id": "d" * 32})[0] == 201
        assert server.store.count() == 1
    with receiver(db, clock=lambda: current[0]) as (server, _):
        receipt = json.loads(post(server, {**VALID, "report_id": "e" * 32})[1])["receipt_id"]
        command = [sys.executable, "-m", "reporting.receiver.operator", "--db", str(db)]
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        counts = subprocess.run(command + ["count"], capture_output=True, text=True, env=env, check=True)
        assert json.loads(counts.stdout)["total"] == 2
        inspected = subprocess.run(command + ["inspect", receipt], capture_output=True, text=True, env=env, check=True)
        assert json.loads(inspected.stdout)["report"]["report_id"] == "e" * 32
        subprocess.run(command + ["delete", receipt], capture_output=True, text=True, env=env, check=True)
        assert server.store.count() == 1


def test_operator_prune_reports_deleted_count(tmp_path):
    db = tmp_path / "reports.sqlite3"
    with receiver(db) as (server, _):
        assert post(server, VALID)[0] == 201
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE reports SET accepted_epoch = ?", (time.time() - 31 * 86400,))
    command = [sys.executable, "-m", "reporting.receiver.operator", "--db", str(db), "prune"]
    result = subprocess.run(command, capture_output=True, text=True,
                            env={**os.environ, "PYTHONPATH": str(ROOT)}, check=True)
    assert json.loads(result.stdout) == {"deleted": 1}


def test_idle_server_prunes_without_new_request(tmp_path):
    db = tmp_path / "reports.sqlite3"
    current = [2_000_000_000.0]
    with receiver(db, clock=lambda: current[0], prune_check_seconds=0.02) as (server, _):
        assert post(server, VALID)[0] == 201
        current[0] += 31 * 86400
        for _ in range(100):
            if server.store.count() == 0:
                break
            threading.Event().wait(0.02)
        assert server.store.count() == 0


def test_slow_body_has_total_read_deadline(tmp_path):
    with receiver(tmp_path / "reports.sqlite3", read_timeout=0.1) as (server, _):
        sock = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        try:
            sock.sendall(b"POST /reports HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{")
            threading.Event().wait(0.2)
            assert b" 408 " in sock.recv(1024)
        finally:
            sock.close()


def test_request_slots_refuse_excess_clients(tmp_path):
    with receiver(tmp_path / "reports.sqlite3", max_concurrent=1, read_timeout=1) as (server, _):
        held = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        try:
            held.sendall(b"POST /reports HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{")
            threading.Event().wait(0.05)
            other = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
            try:
                other.sendall(b"POST /reports HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n")
                assert b" 503 " in other.recv(1024)
            finally:
                other.close()
        finally:
            held.close()


def test_logs_exclude_report_body_id_and_manual_description(tmp_path):
    payload = {**VALID, "description": "PRIVATE DESCRIPTION"}
    with receiver(tmp_path / "reports.sqlite3") as (server, logs):
        assert post(server, payload)[0] == 201
        assert post(server, {**payload, "report_id": "a" * 32}, path="/reports?secret=private")[0] == 404
        assert payload["report_id"] not in logs.getvalue()
        assert "PRIVATE DESCRIPTION" not in logs.getvalue()
        assert "secret=private" not in logs.getvalue()


def test_missing_native_capability_producer_detail_is_accepted_without_freeform_codes(tmp_path):
    line = (ROOT / "reporting/fixtures/diagnostic-v2-native-unavailable.txt").read_bytes()
    assert len(line) <= 2304
    detail = json.loads(line.removeprefix(b"GAMJA_DIAGNOSTIC_V2:"))["diagnostic"]
    assert detail["failure_code"] == detail["reason"] == "native_capability_unavailable"
    with receiver(tmp_path / "native-unavailable.sqlite3") as (server, _):
        assert post(server, {**VALID, "diagnostics": [detail]})[0] == 201
        for field in ("failure_code", "reason"):
            invalid = {**detail, field: "arbitrary_private_exception"}
            assert post(server, {**VALID, "report_id": "b" * 32, "diagnostics": [invalid]})[0] == 400


def test_auth_remember_diagnostic_is_accepted(tmp_path):
    detail = {"version": 1, "correlation_id": "a" * 32, "occurred_at": "2026-10-10T12:00:00Z",
              "stage": "auth_remember", "failure_code": "secret_store_unavailable",
              "reason": "keychain_query_failed", "error_class": "native_os_status",
              "room_id": None, "agent_id": None, "connection_id": None,
              "trigger_event_id": None, "action_id": None,
              "exception_chain": [], "frames": [],
              "os_status": -25293, "operation": "sec_item_add", "cleanup": "success"}
    with receiver(tmp_path / "auth-remember.sqlite3") as (server, _):
        assert post(server, {**VALID, "diagnostics": [detail]})[0] == 201
        assert server.store.count() == 1


def test_auth_restore_diagnostic_is_accepted(tmp_path):
    detail = {"version": 1, "correlation_id": "b" * 32, "occurred_at": "2026-10-10T12:00:00Z",
              "stage": "auth_restore", "failure_code": "credential_locked",
              "reason": "credential_reentry_required", "error_class": "credential_reentry_required",
              "room_id": None, "agent_id": None, "connection_id": None,
              "trigger_event_id": None, "action_id": None,
              "exception_chain": [], "frames": [], "os_status": -25293}
    with receiver(tmp_path / "auth-restore.sqlite3") as (server, _):
        assert post(server, {**VALID, "diagnostics": [detail]})[0] == 201
        assert server.store.count() == 1


def test_old_turn_diagnostic_retained_with_auth_extension(tmp_path):
    detail = {"version": 1, "correlation_id": "c" * 32, "occurred_at": "2026-09-30T01:00:00Z",
              "stage": "tool_execution", "failure_code": "provider_unavailable", "reason": "unexpected_error", "error_class": "value_error",
              "room_id": 4, "agent_id": 7, "connection_id": 3, "trigger_event_id": None,
              "action_id": None, "exception_chain": [], "frames": []}
    with receiver(tmp_path / "old-turn.sqlite3") as (server, _):
        assert post(server, {**VALID, "diagnostics": [detail]})[0] == 201
        assert server.store.count() == 1


@pytest.mark.parametrize("change", [
    {"stage": "auth_remember", "cleanup": "invalid"},
    {"stage": "auth_remember", "operation": "invalid_op"},
    {"stage": "auth_restore", "cleanup": "success"},
    {"stage": "auth_restore", "operation": "sec_item_add"},
    {"stage": "auth_remember", "os_status": True},
    {"stage": "auth_remember", "os_status": None},
    {"stage": "auth_remember", "os_status": 2 ** 31},
    {"stage": "auth_remember", "os_status": -(2 ** 31) - 1},
    {"stage": "auth_remember", "room_id": 1},
    {"stage": "auth_remember", "exception_chain": [{"module": "builtins", "class": "Error"}]},
    {"stage": "auth_remember", "frames": [{"file": "app/test.py", "function": "f", "line": 1}]},
    {"stage": "auth_remember", "http_status": 503},
    {"stage": "tool_execution", "os_status": -25293},
    {"stage": "auth_remember", "failure_code": "provider_unavailable"},
    {"stage": "auth_remember", "error_class": "value_error"},
])
def test_auth_diagnostic_rejects_forbidden_combinations(tmp_path, change):
    detail = {"version": 1, "correlation_id": "d" * 32, "occurred_at": "2026-10-10T12:00:00Z",
              "stage": "auth_remember", "failure_code": "secret_store_unavailable",
              "reason": "keychain_query_failed", "error_class": "native_os_status",
              "room_id": None, "agent_id": None, "connection_id": None,
              "trigger_event_id": None, "action_id": None,
              "exception_chain": [], "frames": []}
    detail.update(change)
    with receiver(tmp_path / "auth-forbidden.sqlite3") as (server, _):
        assert post(server, {**VALID, "diagnostics": [detail]})[0] == 400


@pytest.mark.parametrize("stage", ["auth_remember", "auth_restore"])
def test_module_entrypoint_accepts_auth_diagnostics(tmp_path, stage):
    # Import-only tests cannot detect helpers defined after blocking main().
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
    log = tmp_path / "module-stderr.log"
    with log.open("wb") as stderr:
        child = subprocess.Popen(
            [sys.executable, "-m", "reporting.receiver.server", "--db",
             str(tmp_path / "private" / "reports.sqlite3"), "--port", str(port)],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=stderr,
        )
        try:
            deadline = time.monotonic() + 10
            while True:
                assert child.poll() is None, log.read_text()
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.2):
                        break
                except OSError:
                    assert time.monotonic() < deadline, "module did not listen"
                    time.sleep(.05)
            payload = copy.deepcopy(VALID)
            detail = {"version": 1, "correlation_id": "a" * 32,
                      "occurred_at": "2026-10-10T12:00:00Z", "stage": stage,
                      "failure_code": "secret_store_unavailable",
                      "reason": "keychain_store_unavailable" if stage == "auth_remember" else "keychain_query_failed",
                      "error_class": "native_os_status" if stage == "auth_remember" else "secret_store_unavailable",
                      "room_id": None, "agent_id": None, "connection_id": None,
                      "trigger_event_id": None, "action_id": None,
                      "exception_chain": [], "frames": [], "os_status": -25293}
            if stage == "auth_remember":
                detail.update(operation="sec_item_add", cleanup="success")
            payload["diagnostics"] = [detail]
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("POST", "/reports", json.dumps(payload),
                                   {"Content-Type": "application/json"})
                response = connection.getresponse()
                assert response.status == 201, response.read()
                assert json.loads(response.read())["report_id"] == payload["report_id"]
            finally:
                connection.close()
        finally:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
    assert "NameError" not in log.read_text()
