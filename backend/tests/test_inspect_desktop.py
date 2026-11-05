"""The owner CLI reports only bounded metadata from a disposable database."""

import importlib.util
import json
import sqlite3
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "inspect_desktop.py"
spec = importlib.util.spec_from_file_location("inspect_desktop", SCRIPT)
inspect_desktop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inspect_desktop)


def test_readonly_snapshot_omits_content_credentials_and_tool_arguments(tmp_path):
    database = tmp_path / "app.sqlite"
    with sqlite3.connect(database) as db:
        db.executescript("""
            CREATE TABLE schema_version(version INTEGER);
            INSERT INTO schema_version VALUES(11);
            CREATE TABLE agents(id INTEGER, is_active INTEGER, connection_id INTEGER, model_id TEXT);
            INSERT INTO agents VALUES(7, 1, 3, 'synthetic-model');
            CREATE TABLE provider_connections(id INTEGER, kind TEXT, enabled INTEGER,
                auth_required INTEGER, secret_ref TEXT, base_url TEXT, capabilities TEXT);
            INSERT INTO provider_connections VALUES(3, 'compatible', 1, 1,
                'private-keychain-reference', 'https://example.test/v1/private-path?token=private-url',
                '{"declared":{"tools":true},"verified":{"models":{"synthetic-model":{
                  "tools":true,"vision":false}}}}');
            CREATE TABLE conversations(id INTEGER, is_active INTEGER, title TEXT);
            INSERT INTO conversations VALUES(4, 1, 'private-room-title');
            CREATE TABLE room_agent_runs(room_id INTEGER, agent_id INTEGER, state TEXT, paused INTEGER);
            INSERT INTO room_agent_runs VALUES(4, 7, 'idle', 0);
            CREATE TABLE room_triggers(state TEXT);
            INSERT INTO room_triggers VALUES('pending');
            CREATE TABLE tool_actions(family TEXT, state TEXT, completed_at TEXT,
                observation TEXT, arguments TEXT);
            INSERT INTO tool_actions VALUES('screenshot','failed','2026-09-29 15:00:00',
                '{"error_code":"screen_recording_denied","text":"private-prompt"}',
                '{"secret":"private-argument"}');
            CREATE TABLE room_events(kind TEXT, created_at TEXT, payload TEXT);
            INSERT INTO room_events VALUES('failure','2026-09-29 15:00:00',
                '{"code":"provider_unavailable","message":"private-provider-error"}');
            INSERT INTO room_events VALUES('failure','2026-09-29 15:01:00',
                '{"code":"provider_unavailable","message":"private-provider-error",
                  "diagnostic":{"stage":"provider_request","error_class":"runtime_error",
                    "correlation_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}');
        """)
    result = inspect_desktop.database_status(database)
    rendered = str(result)
    for secret in ("private-keychain-reference", "private-path", "private-url",
                   "private-room-title", "private-prompt", "private-argument", "private-provider-error"):
        assert secret not in rendered
    assert result["schema_version"] == 11
    assert result["active_agents"] == 1
    assert result["pending_triggers"] == 1
    assert result["latest_screenshot"] == {"state": "failed", "error_code": "screen_recording_denied",
                                            "completed_at": "2026-09-29T15:00:00Z"}
    assert result["latest_provider_diagnostic"] == {
        "stage": "provider_request", "error_class": "runtime_error",
        "correlation_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "created_at": "2026-09-29T15:01:00Z"}
    assert result["agents"] == [{"id": 7, "connection_id": 3, "model_id": "synthetic-model",
                                  "connection_kind": "compatible", "connection_host": "example.test",
                                  "connection_enabled": True, "auth_required": True,
                                  "secret_ref_present": True, "tools_verified": True, "vision_verified": False}]


def test_process_parser_requires_exact_installed_executable_path():
    listing = "21 1 /Applications/Gamja Bot.app/Contents/MacOS/gamja-bot\n" \
              "22 21 /Applications/Gamja Bot.app/Contents/MacOS/backend\n" \
              "23 22 /Applications/Gamja Bot.app/Contents/MacOS/backend\n" \
              "24 1 /tmp/Gamja Bot.app/Contents/MacOS/gamja-bot\n"
    assert inspect_desktop.parse_processes(listing, Path("/Applications/Gamja Bot.app")) == {
        "app_pids": [21], "backend_pids": [22, 23]}


def test_process_observation_failure_is_unknown_not_stopped(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("synthetic process query failure")

    monkeypatch.setattr(inspect_desktop.subprocess, "run", unavailable)
    status = inspect_desktop.app_status(tmp_path / "Absent.app")
    assert status["process_observation"] == "unavailable"
    assert status["app_pids"] is None
    assert status["backend_pids"] is None


def test_host_parser_never_exposes_url_path_query_or_userinfo():
    assert inspect_desktop.safe_host("https://name:secret@example.test/private?key=secret") == "example.test"
    assert inspect_desktop.safe_host("https://[bad-host/private") is None


def test_explicit_error_inspection_shows_validated_detail_without_raw_payload(tmp_path):
    database = tmp_path / "diagnostics.sqlite"
    good = {"version": 1, "correlation_id": "a" * 32, "occurred_at": "2026-09-30T01:00:00Z",
            "stage": "provider_request", "failure_code": "quota_exhausted", "reason": "quota_exhausted",
            "error_class": "provider_failure", "room_id": 3, "agent_id": 5, "connection_id": 7,
            "trigger_event_id": "b" * 32, "action_id": None, "http_status": 429,
            "vendor_request_id": "req-safe-12",
            "exception_chain": [{"module": "app.services.providers.base", "class": "ProviderFailure"}],
            "frames": [{"file": "app/services/providers/base.py", "function": "_post", "line": 277}]}
    unsafe = {**good, "correlation_id": "c" * 32,
              "frames": [{"file": "/Users/alice/private.txt", "function": "secret", "line": 1}]}
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE room_events(room_id INTEGER, actor_id INTEGER, kind TEXT, "
                   "created_at TEXT, payload TEXT)")
        for when, record in (("2026-09-30T01:00:00Z", good), ("2026-09-30T01:01:00Z", unsafe)):
            db.execute("INSERT INTO room_events VALUES(3,5,'failure',?,?)",
                       (when, json.dumps({"message": "Bearer private-api-key", "diagnostic": record})))
    result = inspect_desktop.error_details(database, limit=5)
    assert result == [good]
    assert inspect_desktop.error_details(database, correlation="a" * 32) == [good]
    assert "private-api-key" not in str(result) and "/Users/alice" not in str(result)
    context = {**good, "stage": "context_build", "failure_code": "provider_not_configured",
               "reason": "provider_not_configured", "error_class": "llm_failure"}
    assert inspect_desktop.validated_detail(json.dumps(context), 3, 5) == context
    assert inspect_desktop.validated_detail(json.dumps({**good, "stage": []}), 3, 5) is None
    assert inspect_desktop.validated_detail(json.dumps({**good, "version": True}), 3, 5) is None


def test_explicit_error_inspection_includes_tool_result_diagnostic(tmp_path):
    database = tmp_path / "tool-diagnostics.sqlite"
    tool_detail = {"version": 1, "correlation_id": "d" * 32, "occurred_at": "2026-09-30T01:02:00Z",
                   "stage": "tool_execution", "failure_code": "process_guardian_launch_failed",
                   "reason": "not_found", "error_class": "os_error", "room_id": 3, "agent_id": 5,
                   "connection_id": None, "trigger_event_id": "b" * 32, "action_id": "c" * 32,
                   "exception_chain": [{"module": "builtins", "class": "FileNotFoundError"}],
                   "frames": [{"file": "app/services/tools/process.py", "function": "__call__", "line": 101}]}
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE room_events(room_id INTEGER, actor_id INTEGER, kind TEXT, "
                   "created_at TEXT, payload TEXT)")
        db.execute("INSERT INTO room_events VALUES(3,NULL,'tool_result',?,?)",
                   ("2026-09-30T01:02:00Z", json.dumps({"agent_id": 5, "text": "private output",
                                                          "diagnostic": tool_detail})))
    assert inspect_desktop.error_details(database) == [tool_detail]
    assert "private output" not in str(inspect_desktop.error_details(database))
