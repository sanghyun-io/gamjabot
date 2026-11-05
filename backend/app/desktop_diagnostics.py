#!/usr/bin/env python3
"""Read-only, content-free snapshot of the installed Gamja Bot desktop state.

The CLI is a separate process: it cannot report the running app's live TCC
decision. The in-app native permission command must perform that preflight.
"""

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit


DEFAULT_APP = Path("/Applications/Gamja Bot.app")
DEFAULT_DATABASE = Path.home() / "Library/Application Support/top.gamja.bot/gamja_bot.db"
ACTION_STATES = ("pending", "waiting", "approved", "started")
SCREEN_CODES = frozenset({"screen_recording_denied", "native_lease_unconfirmed",
    "native_response_unconfirmed", "native_screenshot_invalid", "screenshot_artifact_unavailable",
    "protected_surface", "capture_failed", "capture_too_large"})
FAILURE_CODES = frozenset({"provider_unavailable", "provider_invalid_response", "invalid_credential",
    "rate_limited", "quota_exhausted", "provider_rejected", "secret_store_unavailable", "missing_secret", "credential_locked",
    "unsupported_option", "unsupported_model_protocol", "tool_effect_uncertain"})
DIAGNOSTIC_STAGES = frozenset({"turn_setup", "context_build", "provider_request", "provider_result",
    "tool_validation", "tool_execution", "turn_persist"})
ERROR_CLASSES = frozenset({"sqlite_error", "database_error", "http_error", "timeout_error",
    "os_error", "type_error", "value_error", "key_error", "index_error", "runtime_error",
    "other_error", "llm_failure", "provider_failure", "connection_failure", "native_refusal"})
DETAIL_REASONS = FAILURE_CODES | frozenset({"connect_timeout", "read_timeout", "timeout",
    "provider_not_configured", "invalid_trigger", "context_input_too_large", "context_memory_too_large",
    "storage_unavailable", "unreachable", "provider_error", "invalid_response",
    "connect_error", "network_error", "http_status", "database_error", "permission_denied",
    "not_found", "connection_refused", "os_error", "invalid_tool_arguments", "invalid_continuation",
    "invalid_configuration", "redirect_refused", "context_too_large", "reply_context_unavailable", "tool_budget_exhausted",
    "executor_budget_exhausted", "invalid_action_result", "tool_broker_unavailable",
    "memory_context_changed", "execution_context_changed",
    "teammate_context_changed", "teammate_unavailable", "teammate_invalid_request",
    "teammate_delivery_changed", "teammate_budget_exhausted",
    "bot_message_context_changed", "bot_message_invalid_request", "bot_message_delivery_changed",
    "connection_disabled", "missing_model", "cancelled", "desktop_release_failed", "unexpected_error",
    "screen_recording_denied", "native_lease_unconfirmed", "native_response_unconfirmed",
    "native_screenshot_invalid", "screenshot_artifact_unavailable", "process_launch_unconfirmed",
    "process_guardian_launch_failed", "process_executable_not_found", "process_executable_not_permitted",
    "process_launch_failed", "accessibility_denied", "backend_stopped", "capture_failed",
    "capture_too_large", "desktop_busy", "input_failed", "invalid_chord", "invalid_coordinates",
    "invalid_scroll", "native_request_denied", "protected_surface", "text_limit",
    "unsupported_key", "unsupported_native_family"})
DETAIL_CODES = DETAIL_REASONS | frozenset({"native_screenshot_invalid", "screenshot_artifact_unavailable",
    "native_lease_unconfirmed", "native_response_unconfirmed", "process_launch_unconfirmed",
    "process_guardian_launch_failed", "process_executable_not_found", "process_executable_not_permitted",
    "process_launch_failed", "screen_recording_denied"})
HEX_ID = re.compile(r"[0-9a-f]{32}\Z")
UUID_ID = re.compile(r"(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\Z")
REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")


def safe_time(raw):
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def safe_host(raw):
    if not isinstance(raw, str):
        return None
    try:
        return urlsplit(raw).hostname
    except ValueError:
        return None


def parse_processes(listing: str, app: Path) -> dict:
    app_exe = str(app / "Contents/MacOS/gamja-bot")
    backend_exe = str(app / "Contents/MacOS/backend")
    rows = []
    for line in listing.splitlines():
        match = re.fullmatch(r"\s*(\d+)\s+(\d+)\s+(.+?)\s*", line)
        if match:
            rows.append((int(match[1]), int(match[2]), match[3]))
    app_pids = sorted(pid for pid, _, path in rows if path == app_exe)
    descendants = set(app_pids)
    changed = True
    while changed:
        prior = len(descendants)
        descendants.update(pid for pid, ppid, _ in rows if ppid in descendants)
        changed = len(descendants) != prior
    return {"app_pids": app_pids,
            "backend_pids": sorted(pid for pid, _, path in rows if pid in descendants and path == backend_exe)}


def _count(db, table: str, where: str = "1=1") -> int:
    # Every caller supplies a fixed table name and predicate from this source.
    return db.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


def database_status(path: Path) -> dict:
    if not path.is_file():
        return {"available": False, "reason": "database_absent"}
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=1) as db:
            db.execute("PRAGMA query_only=ON")
            result = {"available": True,
                      "schema_version": db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0],
                      "active_agents": _count(db, "agents", "is_active=1"),
                      "active_rooms": _count(db, "conversations", "is_active=1"),
                      "pending_triggers": _count(db, "room_triggers", "state='pending'"),
                      "running_triggers": _count(db, "room_triggers", "state='running'"),
                      "action_counts": {state: _count(db, "tool_actions", f"state='{state}'")
                                        for state in ACTION_STATES}}
            row = db.execute("SELECT state, json_extract(observation,'$.error_code'), completed_at "
                             "FROM tool_actions WHERE family='screenshot' "
                             "ORDER BY completed_at DESC LIMIT 1").fetchone()
            result["latest_screenshot"] = ({"state": row[0] if row[0] in (
                "completed", "failed", "denied", "cancelled", "uncertain") else "other",
                "error_code": row[1] if row[1] in SCREEN_CODES else None,
                "completed_at": safe_time(row[2])} if row else None)
            failures = db.execute("SELECT json_extract(payload,'$.code'), COUNT(*) FROM room_events "
                                  "WHERE kind='failure' GROUP BY json_extract(payload,'$.code')").fetchall()
            result["failure_counts_total"] = {code: sum(count for raw, count in failures if raw == code)
                                               for code in sorted(FAILURE_CODES)
                                               if any(raw == code for raw, _ in failures)}
            result["other_failure_count_total"] = sum(count for raw, count in failures
                                                       if raw not in FAILURE_CODES)
            diagnostic = db.execute(
                "SELECT json_extract(payload,'$.diagnostic.stage'), "
                "json_extract(payload,'$.diagnostic.error_class'), "
                "json_extract(payload,'$.diagnostic.correlation_id'), "
                "json_extract(payload,'$.diagnostic.http_status'), created_at FROM room_events "
                "WHERE kind='failure' AND json_type(payload,'$.diagnostic')='object' "
                "ORDER BY created_at DESC LIMIT 1").fetchone()
            result["latest_provider_diagnostic"] = None
            if diagnostic:
                stage, error_class, correlation_id, http_status, created_at = diagnostic
                if (stage in DIAGNOSTIC_STAGES and error_class in ERROR_CLASSES
                        and isinstance(correlation_id, str)
                        and re.fullmatch(r"[0-9a-f]{32}", correlation_id)):
                    result["latest_provider_diagnostic"] = {
                        "stage": stage, "error_class": error_class,
                        "correlation_id": correlation_id, "created_at": safe_time(created_at)}
                    if isinstance(http_status, int) and 100 <= http_status <= 599:
                        result["latest_provider_diagnostic"]["http_status"] = http_status
            agents = db.execute("SELECT a.id,a.connection_id,a.model_id,c.kind,c.base_url,c.enabled,"
                                "c.auth_required,c.secret_ref,c.capabilities FROM agents a "
                                "LEFT JOIN provider_connections c ON c.id=a.connection_id "
                                "WHERE a.is_active=1 ORDER BY a.id").fetchall()
            result["agents"] = []
            for agent_id, connection_id, model_id, kind, base_url, enabled, auth, ref, capabilities in agents:
                try:
                    status = json.loads(capabilities or "{}")
                    verified = status.get("verified", {}) if isinstance(status, dict) else {}
                    if not isinstance(verified, dict):
                        verified = {}
                    if "models" in verified:
                        models = verified["models"]
                        verified = models.get(model_id, {}) if isinstance(models, dict) else {}
                except (TypeError, ValueError, AttributeError):
                    verified = {}
                if not isinstance(verified, dict):
                    verified = {}
                result["agents"].append({"id": agent_id, "connection_id": connection_id,
                    "model_id": model_id, "connection_kind": kind,
                    "connection_host": safe_host(base_url),
                    "connection_enabled": bool(enabled), "auth_required": bool(auth),
                    "secret_ref_present": bool(ref), "tools_verified": verified.get("tools") is True,
                    "vision_verified": verified.get("vision") is True})
            return result
    except (sqlite3.Error, OSError):
        return {"available": False, "reason": "database_unavailable"}


def validated_detail(raw: str, room_id: int, actor_id: int) -> dict | None:
    """Project a stored diagnostic through an exact, content-free schema."""
    try:
        item = json.loads(raw)
    except (TypeError, ValueError):
        return None
    required = {"version", "correlation_id", "occurred_at", "stage", "failure_code", "reason",
                "error_class", "room_id", "agent_id", "connection_id", "trigger_event_id",
                "action_id", "exception_chain", "frames"}
    optional = {"http_status", "vendor_request_id"}
    if not isinstance(item, dict) or not required <= item.keys() or set(item) - required - optional:
        return None
    if (type(item["version"]) is not int or item["version"] != 1 or
            not isinstance(item["correlation_id"], str) or
            not HEX_ID.fullmatch(item["correlation_id"]) or
            not isinstance(item["occurred_at"], str) or safe_time(item["occurred_at"]) != item["occurred_at"] or
            any(type(item[key]) is not str for key in ("stage", "failure_code", "reason", "error_class")) or
            item["stage"] not in DIAGNOSTIC_STAGES or item["failure_code"] not in DETAIL_CODES or
            item["reason"] not in DETAIL_REASONS or item["error_class"] not in ERROR_CLASSES or
            type(item["room_id"]) is not int or item["room_id"] != room_id or
            type(item["agent_id"]) is not int or item["agent_id"] != actor_id or
            item["room_id"] < 1 or item["agent_id"] < 1 or
            (item["connection_id"] is not None and
             (type(item["connection_id"]) is not int or item["connection_id"] < 1))):
        return None
    for key in ("trigger_event_id", "action_id"):
        value = item[key]
        if value is not None and (not isinstance(value, str) or not UUID_ID.fullmatch(value)):
            return None
    chain = item["exception_chain"]
    if not isinstance(chain, list) or len(chain) > 3 or (not chain and item["error_class"] != "native_refusal"):
        return None
    for entry in chain:
        if not isinstance(entry, dict) or set(entry) != {"module", "class"}:
            return None
        module, name = entry["module"], entry["class"]
        if not isinstance(module, str) or not isinstance(name, str):
            return None
        trusted = (module in {"builtins", "sqlite3", "sqlalchemy.exc", "httpx", "httpcore", "ssl", "socket", "other"}
                   or re.fullmatch(r"(?:httpx|httpcore|app)(?:\.[A-Za-z_][A-Za-z0-9_]*)+", module))
        if not trusted or len(module) > 80 or not IDENTIFIER.fullmatch(name):
            return None
    frames = item["frames"]
    if not isinstance(frames, list) or len(frames) > 4:
        return None
    for frame in frames:
        if not isinstance(frame, dict) or set(frame) != {"file", "function", "line"}:
            return None
        path, function, line = frame["file"], frame["function"], frame["line"]
        if (not isinstance(path, str) or not path.startswith("app/") or not path.endswith(".py") or
                len(path) > 124 or not re.fullmatch(r"app/[A-Za-z0-9_./-]+\.py", path) or
                ".." in path.split("/") or not isinstance(function, str) or len(function) > 64 or
                not re.fullmatch(r"[A-Za-z0-9_<>]+", function) or
                type(line) is not int or not 0 < line < 1_000_000):
            return None
    if "http_status" in item and (type(item["http_status"]) is not int or not 100 <= item["http_status"] <= 599):
        return None
    if "vendor_request_id" in item and (not isinstance(item["vendor_request_id"], str) or
                                         not REQUEST_ID.fullmatch(item["vendor_request_id"])):
        return None
    return item


def error_details(path: Path, limit: int = 1, correlation: str | None = None) -> list[dict]:
    if not path.is_file() or not 1 <= limit <= 20 or (correlation is not None and not HEX_ID.fullmatch(correlation)):
        return []
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=1) as db:
            db.execute("PRAGMA query_only=ON")
            predicate = " AND json_extract(payload,'$.diagnostic.correlation_id')=?" if correlation else ""
            values = (correlation, min(200, limit * 5)) if correlation else (min(200, limit * 5),)
            rows = db.execute("SELECT room_id, actor_id, kind, json_extract(payload,'$.agent_id'), "
                              "json_extract(payload,'$.diagnostic') FROM room_events "
                              "WHERE kind IN ('failure','tool_result') AND json_type(payload,'$.diagnostic')='object'" +
                              predicate + " ORDER BY created_at DESC LIMIT ?", values).fetchall()
            found = []
            for room_id, actor_id, kind, tool_agent_id, raw in rows:
                item = validated_detail(raw, room_id, tool_agent_id if kind == "tool_result" else actor_id)
                if item is not None:
                    found.append(item)
                    if len(found) == limit:
                        break
            return found
    except (sqlite3.Error, OSError):
        return []


def file_hash(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def app_status(app: Path) -> dict:
    try:
        process = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,comm="],
                                 capture_output=True, text=True, check=True, timeout=5)
        running = parse_processes(process.stdout, app)
        running["process_observation"] = "observed"
    except (OSError, subprocess.SubprocessError):
        running = {"app_pids": None, "backend_pids": None,
                   "process_observation": "unavailable"}
    executable = app / "Contents/MacOS/gamja-bot"
    sidecar = app / "Contents/MacOS/backend"
    result = {"path": str(app), **running, "main_sha256": file_hash(executable),
              "backend_sha256": file_hash(sidecar)}
    if not executable.is_file():
        result["signature"] = "app_absent"
        return result
    try:
        signed = subprocess.run(["/usr/bin/codesign", "-dv", "--verbose=4", str(app)],
                                capture_output=True, text=True, timeout=5)
        detail = signed.stderr
        result["signature"] = "adhoc" if "Signature=adhoc" in detail else (
            "signed" if signed.returncode == 0 else "unavailable")
        match = re.search(r"^Identifier=([A-Za-z0-9.-]+)$", detail, re.MULTILINE)
        result["bundle_id"] = match[1] if match else None
        match = re.search(r"^CDHash=([0-9a-f]{40})$", detail, re.MULTILINE)
        result["cdhash"] = match[1] if match else None
        verification = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
                                      capture_output=True, timeout=10)
        result["signature_valid"] = verification.returncode == 0
        requirement = subprocess.run(["/usr/bin/codesign", "-dr", "-", str(app)],
                                     capture_output=True, text=True, timeout=5)
        result["designated_requirement_kind"] = (
            "cdhash" if re.search(r'designated => cdhash H"[0-9a-f]{40}"',
                                  requirement.stdout + requirement.stderr)
            else "other_or_unavailable")
    except (OSError, subprocess.SubprocessError):
        result["signature"] = "unavailable"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=DEFAULT_APP)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--errors", type=int, metavar="N", help="show 1-20 validated recent error details")
    parser.add_argument("--correlation", help="show the validated detail for one 32-hex correlation ID")
    args = parser.parse_args()
    if args.errors is not None and not 1 <= args.errors <= 20:
        parser.error("--errors must be between 1 and 20")
    if args.correlation is not None and not HEX_ID.fullmatch(args.correlation):
        parser.error("--correlation must be 32 lowercase hexadecimal characters")
    output = {"observed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "app": app_status(args.app), "database_snapshot": database_status(args.database),
              "native_screen_recording_now": "unobservable_from_external_process"}
    if args.errors is not None or args.correlation is not None:
        output["error_details"] = error_details(args.database, args.errors or 1, args.correlation)
    print(json.dumps(output,
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
