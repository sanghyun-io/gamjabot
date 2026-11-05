#!/usr/bin/env python3
"""Isolated packaged-Core first-success smoke; never an installed cutover.

Uses one disposable app-data directory, a bounded loopback fake provider and
synthetic in-memory authentication. Receipts contain IDs, hashes and counts but
never credentials, prompts, provider bodies or response text.
"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler
from uuid import UUID

from verify_cli_core import (GateFailure, PrivateFixture, read_endpoint,
                             require, write_receipt)

MAX_BODY = 65536
SCHEMA_HEAD = 22
SMOKE_EMAIL = "first-success-smoke@example.com"


def canonical_uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value and value == value.lower()
    except ValueError:
        return False


class FakeProvider:
    def __init__(self):
        self.count = 0
        self.hashes = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length", "-1"))
                except ValueError:
                    length = -1
                if self.path != "/v1/chat/completions" or not 1 <= length <= MAX_BODY:
                    self.send_error(404); return
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeError):
                    self.send_error(400); return
                if (not isinstance(body, dict) or body.get("model") != "exact-smoke"
                        or body.get("tools") not in (None, [])
                        or any(key in body for key in ("images", "image", "vision"))):
                    self.send_error(422); return
                owner.count += 1
                owner.hashes.append(hashlib.sha256(raw).hexdigest())
                payload = json.dumps({"id": "synthetic", "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant",
                    "content": "synthetic result"}, "finish_reason": "stop"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers(); self.wfile.write(payload)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def __enter__(self):
        self.thread.start(); return self

    def __exit__(self, *_):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)
        require(not self.thread.is_alive(), "fake_provider_cleanup_failed")


def api(endpoint, method, path, body=None, *, token=None, key=None, timeout=70):
    static = {("POST", "/api/v1/auth/register"), ("POST", "/api/v1/auth/login"),
              ("POST", "/api/v1/connections"), ("POST", "/api/v1/starter/preview"),
              ("POST", "/api/v1/starter/apply"), ("GET", "/api/v1/starter/last-result")}
    dynamic = bool(isinstance(path, str) and (
        re.fullmatch(r"/api/v1/connections/[1-9][0-9]*/test(?:-preview)?\?model_id=exact-smoke&profile=text", path)
        or re.fullmatch(r"/api/v1/conversations/[1-9][0-9]*/events", path)
        or re.fullmatch(r"/api/v1/conversations/[1-9][0-9]*/requests/[0-9a-f-]{36}", path)
        or re.fullmatch(r"/api/v1/conversations/[1-9][0-9]*/requests/[0-9a-f-]{36}/finalize(?:/preview)?", path)))
    require((method, path) in static or dynamic, "smoke_route_invalid")
    headers = {"X-Gamja-CLI-Capability": endpoint["capability"], "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if key:
        headers["Idempotency-Key"] = key
    payload = json.dumps(body).encode() if body is not None else None
    require(payload is None or len(payload) <= 32768, "smoke_request_too_large")
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = Request(endpoint["api_url"] + path, data=payload, headers=headers, method=method)
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
            raw = response.read(131073)
            require(len(raw) <= 131072, "smoke_response_too_large")
            return response.status, json.loads(raw) if raw else None
    except HTTPError as error:
        raw = error.read(4097)
        require(len(raw) <= 4096, "smoke_response_too_large")
        return error.code, json.loads(raw) if raw else None


def wait_request(endpoint, token, room, request_id, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, result = api(endpoint, "GET", f"/api/v1/conversations/{room}/requests/{request_id}", token=token)
        require(status == 200 and isinstance(result, dict), "request_inspector_failed")
        state = result.get("completion", {}).get("state")
        if state not in {"queued", "running"}:
            require(state in {"completed", "cancelled", "interrupted"}, "request_not_terminal")
            return result
        time.sleep(.2)
    raise GateFailure("request_wait_timeout")


def safe_completion(result):
    completion = result.get("completion") if isinstance(result, dict) else None
    require(isinstance(completion, dict) and set(completion) == {
        "state", "outcome_status", "result_available", "completed_rounds",
        "pending_agent_count",
    }, "completion_shape_invalid")
    return {key: completion[key] for key in (
        "state", "outcome_status", "result_available", "completed_rounds",
        "pending_agent_count",
    )}


def require_first_success_shape(source, final, agent_ids, provider_count, provider_hashes):
    source_expected = {"state": "completed", "outcome_status": "succeeded",
                       "result_available": True, "completed_rounds": 2,
                       "pending_agent_count": 0}
    final_expected = {"state": "completed", "outcome_status": "succeeded",
                      "result_available": True, "completed_rounds": 1,
                      "pending_agent_count": 0}
    require(safe_completion(source) == source_expected, "source_execution_incomplete")
    require(safe_completion(final) == final_expected, "finalize_execution_incomplete")
    source_execution = source.get("execution") if isinstance(source, dict) else None
    final_execution = final.get("execution") if isinstance(final, dict) else None
    source_outcomes = source.get("outcomes") if isinstance(source, dict) else None
    final_outcomes = final.get("outcomes") if isinstance(final, dict) else None
    source_policy = {"max_rounds": 2, "max_tool_calls": 0, "max_executor_seconds": 60}
    final_policy = {"max_rounds": 1, "max_tool_calls": 0, "max_executor_seconds": 60}
    require(isinstance(agent_ids, list) and len(agent_ids) == 2
            and len(set(agent_ids)) == 2 and all(type(value) is int and value > 0 for value in agent_ids),
            "source_agent_shape_invalid")
    require(isinstance(source_execution, dict)
            and source_execution.get("selected_agent_count") == 2
            and source_execution.get("policy") == source_policy
            and {item.get("agent_id") for item in source_execution.get("selected_agents", [])
                 if isinstance(item, dict)} == set(agent_ids),
            "source_agent_shape_invalid")
    require(isinstance(final_execution, dict)
            and final_execution.get("selected_agent_count") == 1
            and final_execution.get("policy") == final_policy
            and [item.get("agent_id") for item in final_execution.get("selected_agents", [])
                 if isinstance(item, dict)] == [agent_ids[0]],
            "finalize_agent_shape_invalid")
    require(isinstance(source_outcomes, list) and len(source_outcomes) == 4
            and source.get("outcome_count") == 4
            and source.get("outcomes_truncated") is False
            and {(item.get("agent_id"), item.get("round")) for item in source_outcomes
                 if isinstance(item, dict) and item.get("kind") == "message"
                 and item.get("phase") == "succeeded"}
            == {(agent, round_number) for agent in agent_ids for round_number in (1, 2)},
            "source_outcome_shape_invalid")
    require(isinstance(final_outcomes, list) and final.get("outcome_count") == 1
            and final.get("outcomes_truncated") is False
            and len(final_outcomes) == 1 and isinstance(final_outcomes[0], dict)
            and final_outcomes[0].get("agent_id") == agent_ids[0]
            and final_outcomes[0].get("round") == 1
            and final_outcomes[0].get("kind") == "message"
            and final_outcomes[0].get("phase") == "succeeded",
            "finalize_outcome_shape_invalid")
    require(source.get("action_count") == 0 and source.get("actions") == []
            and source.get("actions_truncated") is False
            and final.get("action_count") == 0 and final.get("actions") == []
            and final.get("actions_truncated") is False,
            "zero_tools_shape_invalid")
    require(provider_count == len(provider_hashes) == 6
            and all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                    for value in provider_hashes), "provider_request_count_invalid")


def require_schema_head(value):
    require(value == SCHEMA_HEAD, "schema_head_invalid")


def launch(fixture, core, data):
    child = fixture.spawn([str(core), "--app-data", str(data), "serve"],
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 20
    endpoint_path = data / "owner-cli.json"
    while time.monotonic() < deadline:
        if endpoint_path.exists():
            endpoint = read_endpoint(endpoint_path)
            fixture.endpoint = endpoint
            return child, endpoint
        time.sleep(.05)
    raise GateFailure("core_ready_timeout")


def stop(fixture, core, data, child):
    fixture.command([str(core), "--app-data", str(data), "stop"])
    fixture.reap(child, grace=8)
    require(not (data / "owner-cli.json").exists(), "capability_cleanup_failed")


def run_smoke(package, receipt):
    recipe = importlib.util.spec_from_file_location("build_core", Path(__file__).with_name("build_core.py"))
    module = importlib.util.module_from_spec(recipe); recipe.loader.exec_module(module)
    manifest = module.verify_package(package, "aarch64-apple-darwin")
    core = package / "bin/gamja"
    result = {"format": 1, "scope": "packaged_first_success", "status": "failed",
              "artifact_hashes": {"manifest": module.sha256(package / "core-manifest.json"),
                                  "core": module.sha256(core),
                                  "engine": module.sha256(package / "libexec/gamja/gamja-engine")},
              "schema_head": None, "ids": {}, "states": {}, "provider": {},
              "idempotency": {}, "cleanup": {}, "limitations": [
                  "Synthetic owner and loopback fake provider only", "No GUI, owner data, credential store, native tools or cutover"]}
    fixture = PrivateFixture()
    try:
        with fixture, FakeProvider() as provider:
            data = fixture.root / "appdata"
            child, endpoint = launch(fixture, core, data)
            password = secrets.token_urlsafe(24)
            email = SMOKE_EMAIL
            status, _ = api(endpoint, "POST", "/api/v1/auth/register",
                            {"email": email, "username": "smoke", "password": password})
            require(status == 200, "register_failed")
            status, login = api(endpoint, "POST", "/api/v1/auth/login",
                                {"email": email, "password": password,
                                 "remember_device": False, "preserve_remembered_login": True})
            require(status == 200 and isinstance(login.get("access_token"), str), "login_failed")
            token = login["access_token"]
            status, connection = api(endpoint, "POST", "/api/v1/connections", {
                "provider_kind": "compatible", "label": "Synthetic", "base_url": provider.base_url,
                "default_model_id": "exact-smoke", "allow_local_http": True,
                "auth_required": False, "enabled": True, "capabilities": {}}, token=token)
            require(status == 201, "connection_create_failed")
            connection_id = connection["id"]
            status, preflight = api(endpoint, "GET", f"/api/v1/connections/{connection_id}/test-preview?model_id=exact-smoke&profile=text", token=token)
            require(status == 200 and preflight["max_request_count"] == 1, "text_preflight_failed")
            status, tested = api(endpoint, "POST", f"/api/v1/connections/{connection_id}/test?model_id=exact-smoke&profile=text", {}, token=token)
            require(status == 200 and tested["ok"] is True and tested["actual_request_count"] == 1
                    and provider.count == 1, "text_test_failed")
            intent = {"project_title": "Synthetic team", "room_title": "Synthetic room", "agents": [
                {"role": "proposer", "name": "Proposer", "connection_id": connection_id, "model_id": "exact-smoke"},
                {"role": "reviewer", "name": "Reviewer", "connection_id": connection_id, "model_id": "exact-smoke"}]}
            status, preview = api(endpoint, "POST", "/api/v1/starter/preview", intent, token=token)
            require(status == 200 and preview["policy"] == {"max_rounds": 2, "max_tool_calls": 0, "max_executor_seconds": 60}, "starter_preview_failed")
            status, created = api(endpoint, "POST", "/api/v1/starter/apply",
                                  {**intent, "plan_hash": preview["plan_hash"]}, token=token, key="smoke-starter")
            require(status == 200 and created["replayed"] is False, "starter_apply_failed")
            require(provider.count == 1, "starter_called_provider")
            room, proposer = created["room_id"], created["agents"]["proposer"]
            reviewer = created["agents"]["reviewer"]
            status, sent = api(endpoint, "POST", f"/api/v1/conversations/{room}/events",
                               {"content": "synthetic first decision"}, token=token, key="smoke-run")
            require(status == 201 and canonical_uuid(sent.get("event_id")), "run_send_failed")
            source = wait_request(endpoint, token, room, sent["event_id"])
            require(provider.count == len(provider.hashes) == 5, "source_provider_count_invalid")
            status, final_preview = api(endpoint, "POST", f"/api/v1/conversations/{room}/requests/{sent['event_id']}/finalize/preview",
                                        {"agent_id": proposer}, token=token)
            require(status == 200 and final_preview["eligible"] is True, "finalize_preview_failed")
            finalize_path = f"/api/v1/conversations/{room}/requests/{sent['event_id']}/finalize"
            status, finalized = api(endpoint, "POST", finalize_path,
                                    {"agent_id": proposer, "plan_hash": final_preview["plan_hash"]},
                                    token=token, key="smoke-finalize")
            require(status == 201 and canonical_uuid(finalized.get("finalize_event_id"))
                    and finalized.get("replayed") is False, "finalize_failed")
            final_state = wait_request(endpoint, token, room, finalized["finalize_event_id"])
            require_first_success_shape(source, final_state, [proposer, reviewer],
                                        provider.count, provider.hashes)
            status, replay = api(endpoint, "POST", finalize_path,
                                 {"agent_id": proposer, "plan_hash": "0" * 64}, token=token, key="smoke-finalize")
            require(status == 201 and replay["replayed"] is True
                    and replay["finalize_event_id"] == finalized["finalize_event_id"], "finalize_replay_failed")
            require(provider.count == len(provider.hashes) == 6, "finalize_replay_called_provider")
            stop(fixture, core, data, child)
            child, endpoint = launch(fixture, core, data)
            status, login = api(endpoint, "POST", "/api/v1/auth/login",
                                {"email": email, "password": password,
                                 "remember_device": False, "preserve_remembered_login": True})
            token = login.get("access_token") if status == 200 else None
            status, last = api(endpoint, "GET", "/api/v1/starter/last-result", token=token)
            require(status == 200 and last["room_id"] == room, "restart_last_result_failed")
            require(wait_request(endpoint, token, room, sent["event_id"])["completion"] == source["completion"], "restart_source_failed")
            require(wait_request(endpoint, token, room, finalized["finalize_event_id"])["completion"] == final_state["completion"], "restart_finalize_failed")
            stop(fixture, core, data, child)
            with sqlite3.connect(data / "gamja_bot.db") as database:
                schema_head = database.execute(
                    "SELECT MAX(version) FROM schema_version").fetchone()[0]
            require_schema_head(schema_head)
            result.update(status="passed", schema_head=manifest.get("schema_head"),
                          ids={"connection_id": connection_id, "room_id": room,
                               "source_request_id": sent["event_id"],
                               "finalize_request_id": finalized["finalize_event_id"]},
                          states={"source": safe_completion(source),
                                  "finalize": safe_completion(final_state)},
                          provider={"request_count": provider.count, "request_hashes": provider.hashes},
                          idempotency={"starter_replayed": False, "finalize_replayed": True})
            result["schema_head"] = schema_head
        result["cleanup"] = fixture.cleanup
        require(all(fixture.cleanup.values()), "cleanup_incomplete")
    except Exception as error:
        result["failure"] = str(error) if isinstance(error, GateFailure) else "smoke_execution_failed"
        result["cleanup"] = fixture.cleanup
    write_receipt(receipt, result)
    return 0 if result["status"] == "passed" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    return run_smoke(args.package.resolve(), args.receipt.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
