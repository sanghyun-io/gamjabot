"""Bounded, content-free details for locally persisted turn failures."""

import errno
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy.exc import SQLAlchemyError


APP_ROOT = Path(__file__).resolve().parents[1]
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
SAFE_ID = re.compile(r"(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\Z")
STAGES = frozenset({"turn_setup", "context_build", "provider_request", "provider_result",
                    "tool_validation", "tool_execution", "turn_persist"})
TRUSTED_MODULES = frozenset({"builtins", "sqlite3", "sqlalchemy.exc", "httpx", "httpcore", "ssl", "socket"})
SAFE_FAILURE_CODES = frozenset({
    "mcp_transport_unavailable", "provider_unavailable", "provider_invalid_response", "provider_output_truncated", "invalid_credential", "rate_limited", "quota_exhausted",
    "provider_rejected", "secret_store_unavailable", "missing_secret", "credential_locked", "unsupported_option", "unsupported_model_protocol",
    "provider_not_configured", "invalid_trigger", "context_input_too_large",
    "context_memory_too_large", "memory_context_changed", "storage_unavailable", "unreachable", "provider_error",
    "invalid_response",
    "invalid_tool_arguments", "invalid_continuation", "invalid_configuration", "redirect_refused",
    "context_too_large", "reply_context_unavailable", "tool_budget_exhausted", "executor_budget_exhausted", "invalid_action_result",
    "tool_effect_uncertain", "tool_broker_unavailable", "connection_disabled", "missing_model", "review_changed",
    "cancelled", "desktop_release_failed",
    "teammate_context_changed", "teammate_unavailable", "teammate_invalid_request",
    "teammate_delivery_changed", "teammate_budget_exhausted",
    "bot_message_context_changed", "bot_message_invalid_request", "bot_message_delivery_changed",
    "execution_context_changed",
    "native_capability_unavailable", "screen_recording_denied", "native_lease_unconfirmed", "native_response_unconfirmed",
    "native_screenshot_invalid", "screenshot_artifact_unavailable", "process_launch_unconfirmed",
    "process_guardian_launch_failed", "process_executable_not_found", "process_executable_not_permitted",
    "process_launch_failed",
    "accessibility_denied", "backend_stopped", "capture_failed", "capture_too_large", "desktop_busy",
    "input_failed", "invalid_chord", "invalid_coordinates", "invalid_scroll", "native_request_denied",
    "protected_surface", "text_limit", "unsupported_key", "unsupported_native_family",
    "unexpected_error",
})


def _chain(error: Exception | None) -> list[BaseException]:
    found, seen = [], set()
    current: BaseException | None = error
    while current is not None and len(found) < 3 and id(current) not in seen:
        found.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return found


def _safe_exception_type(error: BaseException) -> dict[str, str]:
    module, name = type(error).__module__, type(error).__name__
    trusted = module in TRUSTED_MODULES or module.startswith("httpx.") or module.startswith("httpcore.") or module.startswith("app.")
    if trusted and IDENTIFIER.fullmatch(name) and len(module) <= 80:
        return {"module": module, "class": name}
    return {"module": "other", "class": "Error"}


def _reason(chain: list[BaseException], failure_code: str) -> str:
    for error in chain:
        if isinstance(error, httpx.ConnectTimeout):
            return "connect_timeout"
        if isinstance(error, httpx.ReadTimeout):
            return "read_timeout"
        if isinstance(error, httpx.TimeoutException):
            return "timeout"
        if isinstance(error, httpx.ConnectError):
            return "connect_error"
        if isinstance(error, httpx.NetworkError):
            return "network_error"
        if isinstance(error, httpx.HTTPStatusError):
            return "http_status"
        if isinstance(error, (sqlite3.Error, SQLAlchemyError)):
            return "database_error"
        if isinstance(error, OSError):
            return {errno.EACCES: "permission_denied", errno.EPERM: "permission_denied",
                    errno.ENOENT: "not_found", errno.ECONNREFUSED: "connection_refused"}.get(error.errno, "os_error")
    return failure_code if failure_code in SAFE_FAILURE_CODES else "unexpected_error"


def _error_class(error: Exception | None) -> str:
    if error is None:
        return "native_refusal"
    name, module = type(error).__name__, type(error).__module__
    if module.startswith("app.services.providers") and name in {"ProviderFailure", "UnsupportedOption"}:
        return "provider_failure"
    if module == "app.services.connections" and name == "ConnectionFailure":
        return "connection_failure"
    if module == "app.services.llm_service" and name == "LLMFailure":
        return "llm_failure"
    for kind, label in ((sqlite3.Error, "sqlite_error"), (SQLAlchemyError, "database_error"),
                        (httpx.HTTPError, "http_error"), (TimeoutError, "timeout_error"),
                        (OSError, "os_error"), (TypeError, "type_error"), (ValueError, "value_error"),
                        (KeyError, "key_error"), (IndexError, "index_error"), (RuntimeError, "runtime_error")):
        if isinstance(error, kind):
            return label
    return "other_error"


def _frames(chain: list[BaseException]) -> list[dict]:
    frames = []
    for error in chain:
        frame = error.__traceback__
        while frame is not None:
            try:
                relative = Path(frame.tb_frame.f_code.co_filename).resolve().relative_to(APP_ROOT).as_posix()
            except (OSError, ValueError):
                frame = frame.tb_next
                continue
            function = frame.tb_frame.f_code.co_name
            if (relative.endswith(".py") and len(relative) <= 120 and
                    re.fullmatch(r"[A-Za-z0-9_./-]+", relative) and
                    len(function) <= 64 and re.fullmatch(r"[A-Za-z0-9_<>]+", function) and
                    0 < frame.tb_lineno < 1_000_000):
                item = {"file": "app/" + relative, "function": function, "line": frame.tb_lineno}
                if not frames or frames[-1] != item:
                    frames.append(item)
            frame = frame.tb_next
    return frames[-4:]


def _response_metadata(chain: list[BaseException]) -> tuple[int | None, str | None]:
    for error in chain:
        response = error.response if isinstance(error, httpx.HTTPStatusError) else None
        status = getattr(response, "status_code", None) if response is not None else getattr(error, "http_status", None)
        if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
            continue
        headers = response.headers if response is not None else {}
        request_id = (headers.get("x-request-id") or headers.get("x-alibaba-request-id") or
                      headers.get("request-id") or getattr(error, "vendor_request_id", None))
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            request_id = None
        return status, request_id
    return None, None


def make_diagnostic(error: Exception | None, *, stage: str, failure_code: str, room_id: int,
                    agent_id: int, connection_id: int | None, trigger_event_id: str | None,
                    action_id: str | None) -> dict:
    stage = stage if stage in STAGES else "tool_execution"
    failure_code = failure_code if failure_code in SAFE_FAILURE_CODES else "unexpected_error"
    trigger_event_id = trigger_event_id if isinstance(trigger_event_id, str) and SAFE_ID.fullmatch(trigger_event_id) else None
    action_id = action_id if isinstance(action_id, str) and SAFE_ID.fullmatch(action_id) else None
    connection_id = connection_id if type(connection_id) is int and connection_id > 0 else None
    chain = _chain(error)
    status, request_id = _response_metadata(chain)
    record = {
        "version": 1, "correlation_id": uuid4().hex,
        "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "stage": stage, "failure_code": failure_code, "reason": _reason(chain, failure_code),
        "error_class": _error_class(error),
        "room_id": room_id, "agent_id": agent_id, "connection_id": connection_id,
        "trigger_event_id": trigger_event_id, "action_id": action_id,
        "exception_chain": [_safe_exception_type(item) for item in chain],
        "frames": _frames(chain),
    }
    if failure_code == 'context_too_large':
        budget=getattr(error,'context_budget',None)
        if (type(budget) is dict and set(budget)=={'stage','wire_chars','limit','eligible_old_count','root_turn_chars'}
                and budget['stage']=='projection_compacted' and type(budget['limit']) is int and budget['limit'] in (32000,64000)
                and type(budget['wire_chars']) is int and 0<=budget['wire_chars']<=4000000
                and (budget['eligible_old_count'] is None or type(budget['eligible_old_count']) is int and 0<=budget['eligible_old_count']<=10000)
                and budget['root_turn_chars'] is None):
            record['context_budget']=dict(budget)
    if failure_code == 'review_changed':
        from app.services.action_review import ReviewChanged
        if isinstance(error, ReviewChanged):
            where=getattr(error,'review_failure_stage',None)
            family=getattr(error,'review_candidate_family',None)
            record['review_failure']={'stage':where if type(where) is str and where in {'execution_scope','review_capture'} else 'other',
                'family':family if type(family) is str and family in {'browser_interact','browser_navigate','browser_inspect'} else 'other'}
        reason=getattr(error,'vault_handoff_reason',None)
        if type(reason) is str and reason in {'authority_changed','execution_scope','review_snapshot',
                'profile_unavailable','profile_selection','profile_storage','profile_held','owner_changed',
                'item_unavailable','owner_epoch','secret_unavailable','origin_invalid','origin_mismatch','profile_busy'}:
            record['vault_handoff_review']={'reason':reason}
    from app.services.providers.base import ProviderFailure
    if isinstance(error, ProviderFailure) and error.code == "provider_invalid_response":
        call_detail = getattr(error, "tool_call_diagnostic", None)
        if (isinstance(call_detail, dict) and set(call_detail) == {"reason", "offered_plain_builtin"}
                and call_detail["reason"] in {"missing_id", "unrecognized_name", "nonobject_arguments", "workflow_type"}
                and type(call_detail["offered_plain_builtin"]) is bool):
            record["tool_call"] = dict(call_detail)
        validation = getattr(error, "tool_validation_diagnostic", None)
        if (type(validation) is dict and set(validation) == {"family", "reason"}
                and type(validation["family"]) is str and validation["family"] in {"write_page", "mcp", "other_builtin"}
                and type(validation["reason"]) is str and validation["reason"] in {"schema_shape", "schema_reference", "arguments_json", "arguments_size",
                    "meta_schema", "argument_schema", "meta_validation", "argument_validation", "schema_error",
                    "unresolved_reference", "recursion"}):
            record["tool_validation"] = dict(validation)
        detail = getattr(error, "schema_diagnostic", None)
        if isinstance(detail, dict) and set(detail) == {"validator", "field"}:
            if (isinstance(detail["validator"], str) and detail["validator"] in {"type", "required", "additionalProperties", "enum", "minLength", "maxLength"}
                    and (detail["field"] is None or isinstance(detail["field"], str) and detail["field"] in {"memory_id", "content", "category"})):
                record["tool_schema"] = dict(detail)
    detail=getattr(error,'schema_diagnostic',None)
    if (failure_code=='provider_invalid_response' and isinstance(detail,dict)
            and set(detail)=={'tool','validator','field'} and detail['tool']=='write_page'
            and isinstance(detail['validator'],str) and detail['validator'] in {'type','required','additionalProperties'}
            and (detail['field'] is None or detail['field']=='document')):
        record['tool_schema']=dict(detail)
    truncated=getattr(error,'truncation_diagnostic',None)
    if (failure_code=='provider_output_truncated' and isinstance(truncated,dict)
            and set(truncated)=={'stop_reason','output_limit','reported_output_tokens'}
            and truncated['stop_reason']=='max_tokens' and type(truncated['output_limit']) is int
            and 1<=truncated['output_limit']<=100_000
            and (truncated['reported_output_tokens'] is None or type(truncated['reported_output_tokens']) is int
                 and 0<=truncated['reported_output_tokens']<=1_000_000_000)):
        record['provider_output']=dict(truncated)
    sync = getattr(error, 'mcp_sync_diagnostic', None)
    if failure_code == 'mcp_transport_unavailable' and isinstance(sync,dict) and set(sync)=={'stage','code'}:
        from app.services.mcp_registry import MCP_SYNC_CODES
        if (isinstance(sync['stage'],str) and sync['stage'] in {'configure','bind','epoch','channel'}
                and isinstance(sync['code'],str) and sync['code'] in MCP_SYNC_CODES):
            record['mcp_sync'] = dict(sync)
    if status is not None:
        record["http_status"] = status
    if request_id is not None:
        record["vendor_request_id"] = request_id
    return record
