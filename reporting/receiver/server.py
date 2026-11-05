"""Loopback-only HTTP receiver for the versioned diagnostic contract."""

import argparse
import json
import re
import socket
import sqlite3
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from reporting.receiver.storage import Conflict, Full, Store


CONTRACT = json.loads((Path(__file__).resolve().parents[1] / "contract-v1.json").read_text())
FIELDS = set(CONTRACT["fields"])
REQUIRED = set(CONTRACT["required"])
LIMITS = CONTRACT["limits"]
APP_TOKEN = re.compile(r"[A-Za-z0-9._+-]{1,64}\Z")
OS_VERSION = re.compile(r"(?:unknown|[0-9]+(?:\.[0-9]+)*)\Z")
UTC_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z\Z")
HEX_ID = re.compile(r"[0-9a-fA-F]{32}\Z")
LOWER_HEX_ID = re.compile(r"[0-9a-f]{32}\Z")
LOCAL_ID = re.compile(r"(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\Z")
SYMBOL = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
MODULE_SEGMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}\Z")
FRAME_PART = re.compile(r"[A-Za-z0-9_.-]+\Z")
FUNCTION = re.compile(r"[A-Za-z0-9_<>]{1,64}\Z")
VENDOR_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
DIAGNOSTIC_STAGES = {"turn_setup", "context_build", "provider_request", "provider_result",
                     "tool_validation", "tool_execution", "turn_persist"}
AUTH_STAGES = {"auth_remember", "auth_restore"}
DIAGNOSTIC_REASONS = {"connect_timeout", "read_timeout", "timeout", "connect_error", "network_error",
                      "http_status", "database_error", "permission_denied", "not_found",
                      "connection_refused", "os_error", "invalid_tool_arguments", "unexpected_error"}
AUTH_REMEMBER_REASONS = {"credential_channel_unavailable", "credential_response_invalid", "credential_request_invalid",
                         "keychain_operation_in_progress", "keychain_interaction_required",
                         "keychain_interaction_state_read_failed", "keychain_interaction_disable_failed",
                         "keychain_interaction_restore_failed", "keychain_no_ui_guard_failed",
                         "keychain_interaction_state_uncertain", "keychain_query_failed",
                         "keychain_query_binding_failed", "keychain_store_unavailable",
                         "keychain_backend_unsupported", "keychain_backend_unavailable",
                         "credential_reentry_required", "automatic_login_readback_failed",
                         "automatic_login_save_uncertain", "automatic_login_save_failed"}
AUTH_RESTORE_REASONS = {"keychain_operation_in_progress", "keychain_interaction_required",
                        "keychain_interaction_state_read_failed", "keychain_interaction_disable_failed",
                        "keychain_interaction_restore_failed", "keychain_no_ui_guard_failed",
                        "keychain_interaction_state_uncertain", "keychain_query_failed",
                        "keychain_query_binding_failed", "keychain_store_unavailable",
                        "keychain_backend_unsupported", "keychain_backend_unavailable",
                        "credential_reentry_required"}
ERROR_CLASSES = {"native_refusal", "llm_failure", "provider_failure", "connection_failure", "sqlite_error",
                 "database_error", "http_error", "timeout_error", "os_error", "type_error", "value_error",
                 "key_error", "index_error", "runtime_error", "other_error"}
AUTH_REMEMBER_ERROR_CLASSES = {"native_os_status", "interaction_required", "bridge_transport",
                               "bridge_protocol", "request_validation", "store_unavailable",
                               "credential_reentry_required"}
AUTH_RESTORE_ERROR_CLASSES_LOCKED = {"secret_interaction_required", "credential_reentry_required"}
AUTH_RESTORE_ERROR_CLASSES_UNAVAILABLE = {"secret_store_unavailable"}
FAILURE_CODES = {"unexpected_error", "provider_unavailable", "invalid_tool_arguments", "provider_not_configured", "invalid_trigger",
                 "context_input_too_large", "context_memory_too_large", "context_too_large", "tool_broker_unavailable",
                 "tool_budget_exhausted", "invalid_continuation", "executor_budget_exhausted", "invalid_action_result",
                 "tool_effect_uncertain", "provider_invalid_response", "invalid_configuration", "missing_model",
                 "cancelled", "redirect_refused", "invalid_credential", "rate_limited", "provider_rejected",
                 "secret_store_unavailable", "missing_secret", "connection_disabled", "storage_unavailable",
                 "unreachable", "provider_error", "invalid_response", "unsupported_option", "unsupported_model_protocol", "desktop_release_failed",
                 "native_capability_unavailable", "screen_recording_denied", "native_lease_unconfirmed", "native_response_unconfirmed",
                 "native_screenshot_invalid", "screenshot_artifact_unavailable", "process_launch_unconfirmed",
                 "process_guardian_launch_failed", "process_executable_not_found",
                 "process_executable_not_permitted", "process_launch_failed", "accessibility_denied",
                 "backend_stopped", "capture_failed", "capture_too_large", "desktop_busy", "input_failed",
                 "invalid_chord", "invalid_coordinates", "invalid_scroll", "native_request_denied",
                 "protected_surface", "text_limit", "unsupported_key", "unsupported_native_family"}
AUTH_FAILURE_CODES = {"credential_locked", "secret_store_unavailable"}
AUTH_OPERATIONS = {"sec_item_add", "sec_item_update", "sec_item_copy_matching", "sec_item_delete"}
AUTH_CLEANUP = {"success", "failed", "unknown"}


def _validate_diagnostic(detail):
    required = {"version", "correlation_id", "occurred_at", "stage", "failure_code", "reason", "error_class",
                "room_id", "agent_id", "connection_id", "trigger_event_id", "action_id",
                "exception_chain", "frames"}
    allowed_optional = {"http_status", "vendor_request_id", "os_status", "operation", "cleanup"}
    if type(detail) is not dict or not required <= detail.keys() or not detail.keys() <= required | allowed_optional:
        raise ValueError("invalid diagnostic fields")
    if type(detail["version"]) is not int or detail["version"] != 1:
        raise ValueError("invalid diagnostic version")
    if type(detail["correlation_id"]) is not str or not LOWER_HEX_ID.fullmatch(detail["correlation_id"]):
        raise ValueError("invalid diagnostic correlation")
    if type(detail["occurred_at"]) is not str or len(detail["occurred_at"]) > 32 or not UTC_TIME.fullmatch(detail["occurred_at"]):
        raise ValueError("invalid diagnostic time")
    try:
        datetime.fromisoformat(detail["occurred_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid diagnostic time") from exc
    if any(type(detail[key]) is not str for key in ("stage", "failure_code", "reason", "error_class")):
        raise ValueError("invalid diagnostic classification")
    is_auth_stage = detail["stage"] in AUTH_STAGES
    is_turn_stage = detail["stage"] in DIAGNOSTIC_STAGES
    if not is_auth_stage and not is_turn_stage:
        raise ValueError("invalid diagnostic classification")
    # Auth stages have different validation rules
    if is_auth_stage:
        return _validate_auth_diagnostic(detail)
    # Turn stages must not have auth-specific fields
    if detail.get("os_status") is not None or detail.get("operation") is not None or detail.get("cleanup") is not None:
        raise ValueError("auth fields forbidden on turn stages")
    if (detail["failure_code"] not in FAILURE_CODES or
        detail["reason"] not in DIAGNOSTIC_REASONS | FAILURE_CODES or
        detail["error_class"] not in ERROR_CLASSES):
        raise ValueError("invalid diagnostic classification")
    for key in ("room_id", "agent_id", "connection_id"):
        if detail[key] is not None and (type(detail[key]) is not int or detail[key] <= 0):
            raise ValueError("invalid diagnostic context")
    for key in ("trigger_event_id", "action_id"):
        if detail[key] is not None and (type(detail[key]) is not str or not LOCAL_ID.fullmatch(detail[key])):
            raise ValueError("invalid diagnostic context")
    chain = detail["exception_chain"]
    if type(chain) is not list or len(chain) > LIMITS["exception_chain_max"]:
        raise ValueError("invalid diagnostic chain")
    for item in chain:
        if type(item) is not dict or set(item) != {"module", "class"}:
            raise ValueError("invalid diagnostic chain")
        module = item["module"]
        if (type(module) is not str or len(module) > 80 or
            not (module in {"builtins", "sqlite3", "sqlalchemy.exc", "httpx", "httpcore", "ssl", "socket", "other"} or
                 module.startswith(("httpx.", "httpcore.", "app."))) or
            not all(MODULE_SEGMENT.fullmatch(part) for part in module.split(".")) or
            type(item["class"]) is not str or not SYMBOL.fullmatch(item["class"])):
            raise ValueError("invalid diagnostic chain")
    frames = detail["frames"]
    if type(frames) is not list or len(frames) > LIMITS["frames_max"]:
        raise ValueError("invalid diagnostic frames")
    for frame in frames:
        file = frame.get("file") if type(frame) is dict else None
        parts = file.removeprefix("app/").split("/") if type(file) is str and file.startswith("app/") else []
        safe_file = (type(file) is str and len(file) <= 160 and file.endswith(".py") and
                     bool(parts) and all(part not in (".", "..") and FRAME_PART.fullmatch(part) for part in parts))
        if (type(frame) is not dict or set(frame) != {"file", "function", "line"} or
            not safe_file or type(frame["function"]) is not str or not FUNCTION.fullmatch(frame["function"]) or
            type(frame["line"]) is not int or not 1 <= frame["line"] <= 1_000_000):
            raise ValueError("invalid diagnostic frame")
    if "http_status" in detail and (type(detail["http_status"]) is not int or not 100 <= detail["http_status"] <= 599):
        raise ValueError("invalid diagnostic HTTP status")
    if "vendor_request_id" in detail and (type(detail["vendor_request_id"]) is not str or
                                         not VENDOR_REQUEST_ID.fullmatch(detail["vendor_request_id"])):
        raise ValueError("invalid diagnostic request id")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _reject_constant(_):
    raise ValueError("non-finite number")


def validate(body):
    try:
        report = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("invalid JSON") from exc
    if not isinstance(report, dict) or not REQUIRED <= report.keys() or not report.keys() <= FIELDS:
        raise ValueError("invalid fields")
    if type(report["schema_version"]) is not int or report["schema_version"] != CONTRACT["schema_version"]:
        raise ValueError("unsupported schema")
    if not isinstance(report["report_id"], str) or not HEX_ID.fullmatch(report["report_id"]):
        raise ValueError("invalid id")
    for field in ("mode", "os", "arch", "component", "stage", "code"):
        if type(report[field]) is not str or report[field] not in CONTRACT[field]:
            raise ValueError("invalid enum")
    for field in ("app_version", "build_id"):
        if type(report[field]) is not str or not APP_TOKEN.fullmatch(report[field]):
            raise ValueError("invalid version")
    version = report["os_version"]
    if type(version) is not str or len(version) > LIMITS["os_version_chars"] or not OS_VERSION.fullmatch(version):
        raise ValueError("invalid OS version")
    occurred = report["occurred_at"]
    if type(occurred) is not str or len(occurred) > 32 or not UTC_TIME.fullmatch(occurred):
        raise ValueError("invalid time")
    try:
        datetime.fromisoformat(occurred.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid time") from exc
    count = report["occurrences"]
    if type(count) is not int or not LIMITS["occurrences_min"] <= count <= LIMITS["occurrences_max"]:
        raise ValueError("invalid count")
    transitions = report["transitions"]
    if type(transitions) is not list or len(transitions) > LIMITS["transitions_max"]:
        raise ValueError("invalid transitions")
    for transition in transitions:
        if type(transition) is not dict or set(transition) != {"stage", "code"}:
            raise ValueError("invalid transition")
        if type(transition["stage"]) is not str or transition["stage"] not in CONTRACT["stage"]:
            raise ValueError("invalid transition stage")
        if type(transition["code"]) is not str or transition["code"] not in CONTRACT["code"]:
            raise ValueError("invalid transition code")
    if "description" in report:
        description = report["description"]
        if report["mode"] != "manual" or type(description) is not str or len(description) > LIMITS["manual_description_chars_max"]:
            raise ValueError("invalid description")
        try:
            description.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("invalid description") from exc
    if "diagnostics" in report:
        details = report["diagnostics"]
        if type(details) is not list or not 1 <= len(details) <= LIMITS["diagnostics_max"]:
            raise ValueError("invalid diagnostics")
        for detail in details:
            _validate_diagnostic(detail)
    return report


class RateLimiter:
    def __init__(self, global_limit, per_ip_limit, clock):
        self.global_limit = global_limit
        self.per_ip_limit = per_ip_limit
        self.clock = clock
        self.global_hits = deque()
        self.ip_hits = {}
        self.lock = threading.Lock()

    def allow(self, ip):
        now = self.clock()
        with self.lock:
            while self.global_hits and self.global_hits[0] <= now - 60:
                self.global_hits.popleft()
            for key in list(self.ip_hits):
                hits = self.ip_hits[key]
                while hits and hits[0] <= now - 60:
                    hits.popleft()
                if not hits:
                    del self.ip_hits[key]
            if len(self.global_hits) >= self.global_limit or len(self.ip_hits.get(ip, ())) >= self.per_ip_limit:
                return False
            self.global_hits.append(now)
            self.ip_hits.setdefault(ip, deque()).append(now)
            return True


class Receiver(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store, *, clock, rate_global, rate_per_ip, max_concurrent,
                 read_timeout, prune_check_seconds, log_sink):
        super().__init__(address, Handler)
        self.store = store
        self.clock = clock
        self.rate = RateLimiter(rate_global, rate_per_ip, clock)
        self.slots = threading.BoundedSemaphore(max_concurrent)
        self.last_prune = clock()
        self.prune_lock = threading.Lock()
        self.log_sink = log_sink
        self.read_timeout = read_timeout
        self.maintenance_stop = threading.Event()
        self.maintenance = threading.Thread(target=self._maintain, daemon=True)
        self.prune_check_seconds = prune_check_seconds
        self.maintenance.start()

    def _maintain(self):
        while not self.maintenance_stop.wait(self.prune_check_seconds):
            try:
                self.prune_if_due()
            except sqlite3.DatabaseError:
                self.log_sink.write("maintenance status=error\n")

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            finally:
                request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        self.log_sink.write("request status=error\n")

    def prune_if_due(self):
        now = self.clock()
        if now - self.last_prune < 86400:
            return
        with self.prune_lock:
            if now - self.last_prune >= 86400:
                self.store.prune(now)
                self.last_prune = now

    def server_close(self):
        self.maintenance_stop.set()
        self.maintenance.join(timeout=5)
        super().server_close()
        self.store.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.expired = threading.Event()
        self.connection.settimeout(self.server.read_timeout)
        self.deadline_timer = threading.Timer(self.server.read_timeout, self._expire)
        self.deadline_timer.start()

    def _expire(self):
        self.expired.set()
        try:
            self.connection.shutdown(socket.SHUT_RD)
        except OSError:
            pass

    def finish(self):
        self.deadline_timer.cancel()
        super().finish()

    def log_message(self, _format, *_args):
        pass

    def log_error(self, _format, *_args):
        pass

    def respond(self, status, receipt=None):
        body = json.dumps(receipt, separators=(",", ":")).encode() if receipt is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.close_connection = True
        self.server.log_sink.write(f"request status={status}\n")

    def do_POST(self):
        if self.path != "/reports":
            return self.respond(404)
        if not self.server.rate.allow(self.client_address[0]):
            return self.respond(429)
        try:
            self.server.prune_if_due()
        except sqlite3.DatabaseError:
            return self.respond(503)
        if self.headers.get_all("Transfer-Encoding"):
            return self.respond(400)
        lengths = self.headers.get_all("Content-Length")
        if not lengths:
            return self.respond(411)
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
            return self.respond(400)
        if len(lengths[0]) > 4:
            return self.respond(413)
        size = int(lengths[0])
        if size > CONTRACT["wire_max_bytes"]:
            return self.respond(413)
        content_types = self.headers.get_all("Content-Type")
        if content_types is None or len(content_types) != 1:
            return self.respond(400)
        if content_types[0] != "application/json":
            return self.respond(415)
        try:
            body = self.rfile.read(size)
        except (OSError, socket.timeout):
            return self.respond(408)
        if len(body) != size:
            return self.respond(408 if self.expired.is_set() else 400)
        try:
            report = validate(body)
        except ValueError:
            return self.respond(400)
        try:
            receipt = self.server.store.accept(report["report_id"], body)
        except Conflict:
            return self.respond(409)
        except Full:
            return self.respond(507)
        except sqlite3.DatabaseError:
            return self.respond(503)
        return self.respond(201, receipt)

    def do_GET(self):
        self.respond(405)

    do_HEAD = do_GET
    do_PUT = do_GET
    do_DELETE = do_GET


def make_server(db_path, *, port=8787, clock=time.time, rate_global=120, rate_per_ip=30,
                max_reports=10_000, max_concurrent=8, read_timeout=5,
                prune_check_seconds=3600, log_sink=None):
    store = Store(db_path, max_reports=max_reports, clock=clock)
    try:
        return Receiver(("127.0.0.1", port), store, clock=clock, rate_global=rate_global,
                        rate_per_ip=rate_per_ip, max_concurrent=max_concurrent,
                        read_timeout=read_timeout, prune_check_seconds=prune_check_seconds,
                        log_sink=log_sink if log_sink is not None else sys.stderr)
    except Exception:
        store.close()
        raise


def main():
    parser = argparse.ArgumentParser(description="Loopback Gamja report receiver")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    server = make_server(args.db, port=args.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()




def _validate_auth_diagnostic(detail):
    """Validate auth-specific diagnostic fields."""
    # Auth stages must have NULL context identifiers and empty chains/frames
    if any(detail.get(key) is not None for key in ("room_id", "agent_id", "connection_id", "trigger_event_id", "action_id")):
        raise ValueError("auth diagnostic must have empty context")
    if detail["exception_chain"] != [] or detail["frames"] != []:
        raise ValueError("auth diagnostic must have empty context")
    if "http_status" in detail or "vendor_request_id" in detail:
        raise ValueError("auth diagnostic must have empty context")
    # Auth failure codes
    if detail["failure_code"] not in AUTH_FAILURE_CODES:
        raise ValueError("invalid auth failure code")
    # Validate os_status if present (strict int, exclude bool)
    if "os_status" in detail:
        if type(detail["os_status"]) is not int or isinstance(detail["os_status"], bool):
            raise ValueError("invalid os_status type")
        # i32 range check
        if not -(2 ** 31) <= detail["os_status"] < 2 ** 31:
            raise ValueError("invalid os_status range")
    if detail["stage"] == "auth_remember":
        # auth_remember: cleanup REQUIRED, operation OPTIONAL
        if detail["reason"] not in AUTH_REMEMBER_REASONS:
            raise ValueError("invalid auth_remember reason")
        if detail["error_class"] not in AUTH_REMEMBER_ERROR_CLASSES:
            raise ValueError("invalid auth_remember error_class")
        if "cleanup" not in detail or detail["cleanup"] not in AUTH_CLEANUP:
            raise ValueError("auth_remember requires valid cleanup")
        if "operation" in detail and detail["operation"] not in AUTH_OPERATIONS:
            raise ValueError("invalid auth operation")
    else:  # auth_restore
        # auth_restore: cleanup and operation forbidden
        if "cleanup" in detail or "operation" in detail:
            raise ValueError("auth_restore forbids cleanup and operation")
        if detail["reason"] not in AUTH_RESTORE_REASONS:
            raise ValueError("invalid auth_restore reason")
        if detail["failure_code"] == "credential_locked":
            valid_classes = AUTH_RESTORE_ERROR_CLASSES_LOCKED
        else:
            valid_classes = AUTH_RESTORE_ERROR_CLASSES_UNAVAILABLE
        if detail["error_class"] not in valid_classes:
            raise ValueError("invalid auth_restore error_class")


if __name__ == "__main__":
    main()
