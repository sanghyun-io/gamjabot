"""Detailed local diagnostics retain code locations without secret-bearing text."""

import httpx
from types import SimpleNamespace
import stat

from app.services.diagnostic_detail import make_diagnostic
from app.services.diagnostics import emit_detailed_diagnostic
from app.services.llm_service import LLMFailure


def test_exception_chain_and_safe_context_omit_messages_urls_paths_and_locals():
    secret = "Bearer private-api-key"
    try:
        try:
            raise httpx.ConnectError(f"{secret} https://example.test/private?token=secret")
        except httpx.ConnectError as cause:
            raise RuntimeError(f"{secret} /Users/alice/private.txt") from cause
    except RuntimeError as error:
        record = make_diagnostic(error, stage="provider_request", failure_code="provider_unavailable",
                                 room_id=3, agent_id=5, connection_id=7,
                                 trigger_event_id="a" * 32, action_id=None)

    rendered = str(record)
    assert record["version"] == 1
    assert record["error_class"] == "runtime_error"
    assert record["reason"] == "connect_error"
    assert record["room_id"] == 3 and record["agent_id"] == 5
    assert record["connection_id"] == 7
    assert record["exception_chain"] == [
        {"module": "builtins", "class": "RuntimeError"},
        {"module": "httpx", "class": "ConnectError"},
    ]
    assert len(record["correlation_id"]) == 32
    assert len(record["frames"]) <= 4
    assert secret not in rendered and "private.txt" not in rendered
    assert "private?token" not in rendered and "/Users/alice" not in rendered


def test_http_status_and_vendor_request_id_are_strictly_bounded():
    response = httpx.Response(429, headers={"x-request-id": "req-safe-12"},
                              request=httpx.Request("POST", "https://example.test/private?secret=1"))
    error = httpx.HTTPStatusError("Bearer private-api-key", request=response.request, response=response)
    record = make_diagnostic(error, stage="provider_request", failure_code="rate_limited",
                             room_id=3, agent_id=5, connection_id=7,
                             trigger_event_id="b" * 32, action_id=None)
    assert record["http_status"] == 429
    assert record["vendor_request_id"] == "req-safe-12"
    assert "private" not in str(record)


def test_untrusted_vendor_request_id_and_untrusted_exception_type_are_not_saved():
    bad_type = type("SecretToken", (Exception,), {"__module__": "untrusted.private"})
    error = bad_type("Bearer private-api-key")
    error.http_status = 2000
    error.vendor_request_id = "Bearer private-api-key"
    record = make_diagnostic(error, stage="provider_result", failure_code="provider_unavailable",
                             room_id=3, agent_id=5, connection_id=None,
                             trigger_event_id="c" * 32, action_id=None)
    assert record["exception_chain"] == [{"module": "other", "class": "Error"}]
    assert "http_status" not in record and "vendor_request_id" not in record
    assert "private-api-key" not in str(record)


def test_private_diagnostic_pipe_emits_only_a_bounded_safe_record(monkeypatch):
    written = []
    monkeypatch.setattr("app.services.diagnostics.os.fstat", lambda _fd: SimpleNamespace(st_mode=stat.S_IFIFO))
    monkeypatch.setattr("app.services.diagnostics.os.write", lambda _fd, value: written.append(value))
    record = make_diagnostic(RuntimeError("Bearer private-api-key"), stage="provider_request",
                             failure_code="provider_unavailable", room_id=3, agent_id=5,
                             connection_id=7, trigger_event_id="a" * 32, action_id=None)
    emit_detailed_diagnostic(record)
    assert len(written) == 1 and written[0].startswith(b"GAMJA_DIAGNOSTIC_V2:")
    assert len(written[0]) <= 2048
    assert b"private-api-key" not in written[0]


def test_private_pipe_accepts_maximal_bounded_detail(monkeypatch):
    written = []
    monkeypatch.setattr("app.services.diagnostics.os.fstat", lambda _fd: SimpleNamespace(st_mode=stat.S_IFIFO))
    monkeypatch.setattr("app.services.diagnostics.os.write", lambda _fd, value: written.append(value))
    record = make_diagnostic(RuntimeError("secret"), stage="tool_execution", failure_code="unexpected_error",
                             room_id=9223372036854775807, agent_id=9223372036854775807,
                             connection_id=9223372036854775807,
                             trigger_event_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                             action_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    record["failure_code"] = "screenshot_artifact_unavailable"
    record["reason"] = "screenshot_artifact_unavailable"
    record["exception_chain"] = [{"module": "app." + "a" * 76, "class": "B" * 64}] * 3
    record["frames"] = [{"file": "app/" + "a" * 117 + ".py",
                          "function": "f" * 64, "line": 999999}] * 4
    record["http_status"] = 599
    record["vendor_request_id"] = "r" * 64
    emit_detailed_diagnostic(record)
    assert len(written) == 1
    assert len(written[0]) > 2048
    assert len(written[0]) <= 2304


def test_typed_native_refusal_has_no_fabricated_exception_or_stack():
    record = make_diagnostic(None, stage="tool_execution", failure_code="screen_recording_denied",
                             room_id=1, agent_id=2, connection_id=None,
                             trigger_event_id=None, action_id="a" * 32)
    assert record["reason"] == "screen_recording_denied"
    assert record["error_class"] == "native_refusal"
    assert record["exception_chain"] == [] and record["frames"] == []


def test_typed_native_capture_refusal_retains_actionable_code():
    record = make_diagnostic(None, stage="tool_execution", failure_code="protected_surface",
                             room_id=1, agent_id=2, connection_id=None,
                             trigger_event_id=None, action_id="a" * 32)
    assert record["failure_code"] == "protected_surface"
    assert record["reason"] == "protected_surface"


def test_known_context_failure_retains_its_code():
    record = make_diagnostic(LLMFailure("provider_not_configured", "private input"),
                             stage="context_build", failure_code="provider_not_configured",
                             room_id=1, agent_id=2, connection_id=None,
                             trigger_event_id=None, action_id=None)
    assert record["failure_code"] == "provider_not_configured"
    assert record["reason"] == "provider_not_configured"
    assert "private input" not in str(record)


def test_frozen_bundle_frame_is_relative_to_app_root(monkeypatch, tmp_path):
    bundled_app = tmp_path / "_MEI123" / "app"
    monkeypatch.setattr("app.services.diagnostic_detail.APP_ROOT", bundled_app)
    namespace = {}
    script = compile("raise RuntimeError('private path')", str(bundled_app / "services" / "scheduler.py"), "exec")
    try:
        exec(script, namespace)
    except RuntimeError as error:
        record = make_diagnostic(error, stage="provider_result", failure_code="unexpected_error",
                                 room_id=1, agent_id=2, connection_id=None,
                                 trigger_event_id=None, action_id=None)
    assert {"file": "app/services/scheduler.py", "function": "<module>", "line": 1} in record["frames"]
    assert str(tmp_path) not in str(record)


def test_untrusted_identifiers_and_codes_are_not_copied_into_record():
    record = make_diagnostic(ValueError("private"), stage="secret-stage", failure_code="Bearer private-api-key",
                             room_id=1, agent_id=2, connection_id=None,
                             trigger_event_id="/Users/alice/private", action_id="secret-token")
    assert record["failure_code"] == "unexpected_error"
    assert record["stage"] == "tool_execution"
    assert record["trigger_event_id"] is None and record["action_id"] is None
    assert "private-api-key" not in str(record) and "/Users/alice" not in str(record)


def test_profile_guard_code_survives_local_and_receiver_diagnostic_projection():
    import json
    from app.desktop_diagnostics import validated_detail
    from app.services.providers.base import ProviderFailure
    from reporting.receiver.server import _validate_diagnostic
    error = ProviderFailure("unsupported_model_protocol", "PRIVATE message must never be stored")
    record = make_diagnostic(error, stage="provider_request", failure_code=error.code,
                             room_id=3, agent_id=5, connection_id=7,
                             trigger_event_id="a" * 32, action_id=None)
    assert record["failure_code"] == record["reason"] == "unsupported_model_protocol"
    assert validated_detail(json.dumps(record), 3, 5) == record
    _validate_diagnostic(record)
    assert "PRIVATE" not in json.dumps(record)
    unknown = make_diagnostic(error, stage="provider_request", failure_code="PRIVATE raw code",
                             room_id=3, agent_id=5, connection_id=7,
                             trigger_event_id="a" * 32, action_id=None)
    assert unknown["failure_code"] == "unexpected_error"
