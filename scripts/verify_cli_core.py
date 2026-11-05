#!/usr/bin/env python3
"""Operator-only isolated CLI-core verification; never an installed/GUI cutover.

The package mode runs its public gamja with an empty private cwd/app-data. It proves
the frozen engine and CLI account/control lifecycle without secure-store/vendor
calls. It does not prove provider jobs, schedule recovery, native OS execution,
GUI persistence, signing or whole-branch acceptance. Source mode is a separate
integration check and cannot earn the frozen-package gate.
"""
import argparse
import ctypes
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import time
from urllib.parse import urlsplit


MAX_RESPONSE = 73728
MAX_RECEIPT = 16384
REPORT_OPERATIONS = ("status", "list", "create", "export", "delete")


class GateFailure(RuntimeError):
    """Fixed operator error code; never attach subprocess/HTTP error details."""


def require(condition, code):
    if not condition:
        raise GateFailure(code)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def loopback(value):
    try:
        require(isinstance(value, str), "endpoint_untrusted")
        parsed = urlsplit(value)
        require(parsed.port is not None and 1 <= parsed.port <= 65535
                and value == f"http://127.0.0.1:{parsed.port}", "endpoint_untrusted")
        return parsed.port
    except (TypeError, ValueError):
        raise GateFailure("endpoint_untrusted") from None


def read_endpoint(path):
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and not info.st_mode & 0o077 and info.st_size <= 4096, "endpoint_untrusted")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = None
            value = json.load(stream, object_pairs_hook=unique_object)
        require(isinstance(value, dict) and set(value) == {
            "version", "api_url", "control_url", "capability", "core_version", "engine_version"
        }, "endpoint_untrusted")
        require(all(type(value[name]) is int and value[name] == 1
                    for name in ("version", "core_version", "engine_version")), "endpoint_untrusted")
        require(isinstance(value["capability"], str)
                and re.fullmatch(r"[0-9a-f]{64}", value["capability"]), "endpoint_untrusted")
        loopback(value["api_url"])
        loopback(value["control_url"])
        return value
    except (OSError, ValueError, UnicodeError):
        raise GateFailure("endpoint_untrusted") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def local_json(url, path, capability, body=None, *, origin=None, timeout=3):
    """Reviewed control wrapper: capability may never become a browser header."""
    require(path in {"/control", "/health"} and isinstance(capability, str)
            and re.fullmatch(r"[0-9a-f]{64}", capability), "invalid_fixture_request")
    require(origin is None or (isinstance(origin, str) and not any(ord(char) <= 32 for char in origin)),
            "invalid_fixture_request")
    auth = [("X-Gamja-CLI-Capability", capability)]
    if origin is not None:
        auth.append(("Origin", origin))
    return _bounded_local_json(url, path, 'POST' if body is not None else 'GET', auth, body, timeout)


def local_api_json(url, path, session, body=None, *, jwt=None, method='GET', timeout=3):
    """The operator's finite canonical routes and fixed browser auth headers."""
    allowed = {
        ('POST', '/api/v1/auth/register'), ('POST', '/api/v1/auth/login'), ('POST', '/api/v1/auth/restore'),
        ('POST', '/api/v1/connections'), ('POST', '/api/v1/agents/'),
        ('POST', '/api/v1/projects/'), ('POST', '/api/v1/conversations/'),
    }
    dynamic = ((method == 'POST' and re.fullmatch(r'/api/v1/(?:connections/[1-9][0-9]*/test|projects/[1-9][0-9]*/agents/[1-9][0-9]*|conversations/[1-9][0-9]*/(?:members/[1-9][0-9]*|events))', path))
        or (method in {'GET', 'PUT'} and re.fullmatch(r'/api/v1/conversations/[1-9][0-9]*/schedule', path))
        or (method == 'GET' and re.fullmatch(r'/api/v1/conversations/[1-9][0-9]*/events', path))) if isinstance(path, str) else False
    require((method, path) in allowed or dynamic, 'invalid_fixture_request')
    require(isinstance(session, str) and re.fullmatch(r'[A-Za-z0-9_=-]{32,4096}', session), 'invalid_fixture_request')
    require(jwt is None or (isinstance(jwt, str) and re.fullmatch(r'[A-Za-z0-9_.=-]{1,4096}', jwt)), 'invalid_fixture_request')
    auth = [('X-Gamja-Session', session), ('Origin', 'tauri://localhost')]
    if jwt is not None:
        auth.append(('Authorization', 'Bearer ' + jwt))
    return _bounded_local_json(url, path, method, auth, body, timeout)


def _bounded_local_json(url, path, method, auth, body, timeout):
    """One absolute deadline; capped direct loopback request/headers/body."""
    port = loopback(url)
    deadline = time.monotonic() + timeout
    connection = None
    def remaining():
        value = deadline - time.monotonic()
        require(value > 0, "local_request_timeout")
        return value
    def receive(size):
        connection.settimeout(remaining())
        return connection.recv(size)
    try:
        payload = json.dumps(body).encode() if body is not None else b""
        require(len(payload) <= 16384, "invalid_fixture_request")
        headers = (f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                   f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n")
        headers += ''.join(f'{name}: {value}\r\n' for name, value in auth)
        require(len(headers.encode('ascii')) <= 8192, 'invalid_fixture_request')
        connection = socket.create_connection(("127.0.0.1", port), timeout=remaining())
        connection.settimeout(remaining())
        connection.sendall(headers.encode() + b"\r\n" + payload)
        content = bytearray()
        while not content.endswith(b"\r\n\r\n"):
            require(len(content) < 8192, "response_too_large")
            part = receive(1)
            require(part, "local_request_failed")
            content.extend(part)
        lines = content.decode("ascii").split("\r\n")
        status_line = lines[0].split(" ", 2)
        require(len(status_line) == 3 and status_line[0] in {"HTTP/1.0", "HTTP/1.1"}
                and status_line[1].isdigit(), "local_request_failed")
        fields = {}
        for line in lines[1:]:
            if line:
                name, value = line.split(":", 1)
                name = name.lower()
                require(name not in fields, "local_request_failed")
                fields[name] = value.strip()
        require("transfer-encoding" not in fields and "content-length" in fields, "local_request_failed")
        length = int(fields["content-length"])
        require(0 <= length <= MAX_RESPONSE, "response_too_large")
        content = bytearray()
        while len(content) < length:
            part = receive(min(4096, length - len(content)))
            require(part, "local_request_failed")
            content.extend(part)
        remaining()
        return int(status_line[1]), json.loads(content) if content else None
    except (OSError, ValueError, UnicodeError):
        raise GateFailure("local_request_failed") from None
    finally:
        if connection:
            connection.close()


def child_exited(child):
    """Observe without reaping; a waitable leader pins its numeric group ID."""
    require(child.returncode is None, "cleanup_ownership_unproven")
    while True:
        try:
            if hasattr(os, "waitid"):
                information = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                return information is not None and information.si_pid == child.pid
            require(platform.system() == "Darwin" and ctypes.sizeof(ctypes.c_void_p) == 8,
                    "cleanup_ownership_unproven")
            # Darwin Python omits os.waitid. This matches native siginfo_t on
            # Apple64 (six int fields, address, nine size_t padding fields).
            class SigInfo(ctypes.Structure):
                _fields_ = [("signo", ctypes.c_int), ("error", ctypes.c_int), ("code", ctypes.c_int),
                            ("pid", ctypes.c_int), ("uid", ctypes.c_uint), ("status", ctypes.c_int),
                            ("address", ctypes.c_void_p), ("padding", ctypes.c_size_t * 9)]
            library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
            waitid = library.waitid
            waitid.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(SigInfo), ctypes.c_int]
            waitid.restype = ctypes.c_int
            information = SigInfo()
            if waitid(1, child.pid, ctypes.byref(information), os.WEXITED | os.WNOHANG | os.WNOWAIT) != 0:
                error = ctypes.get_errno()
                if error == errno.EINTR:
                    continue
                raise GateFailure("cleanup_ownership_unproven")
            require(information.pid in (0, child.pid), "cleanup_ownership_unproven")
            return information.pid == child.pid
        except InterruptedError:
            continue
        except (OSError, AttributeError):
            raise GateFailure("cleanup_ownership_unproven") from None


def group_alive(group):
    states = subprocess.run(["/bin/ps", "-axo", "pgid=,stat="], capture_output=True,
                            text=True, timeout=2)
    require(states.returncode == 0, "cleanup_ownership_unproven")
    return any(len(row) == 2 and row[0] == str(group) and not row[1].startswith("Z")
               for line in states.stdout.splitlines() if (row := line.split()))


def process_identity(pid):
    # Identity is only used to OBSERVE owned engine cleanup; never kill a PID
    # taken from an HTTP response. The Popen handles are our kill authority.
    if type(pid) is not int or pid <= 1:
        return None
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart=", "-o", "stat="],
                            capture_output=True, timeout=2, text=True)
    line = result.stdout.strip()
    return line if result.returncode == 0 and line and not line.split()[-1].startswith("Z") else None


class PrivateFixture:
    def __init__(self):
        self.root = None
        self.children = []
        self.finalized = set()
        self.engines = []
        self.endpoint = None
        self.resources = {}
        self.cleanup = {"processes_exited": True, "engine_processes_exited": True,
                        "capability_removed": True, "sockets_closed": True,
                        "temporary_root_removed": True}

    def __enter__(self):
        self.root = Path(tempfile.mkdtemp(prefix="gamja-cli-core-verify-"))
        self.root.chmod(0o700)
        self.resources = {"temporary_root": self.root.name, "spawned_process_pids": [],
                          "engine_process_pids": [], "loopback_ports": []}
        for name in ("home", "tmp", "cwd", "appdata"):
            (self.root / name).mkdir(mode=0o700)
        # An allowlist prevents owner credentials, databases, proxies and launch
        # seams from being inherited. Package binaries locate their own assets.
        self.env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
                    "TMPDIR": str(self.root / "tmp"),
                    "XDG_CONFIG_HOME": str(self.root / "home"), "PYTHONDONTWRITEBYTECODE": "1"}
        if "HOME" in os.environ:
            self.env["HOME"] = os.environ["HOME"]  # Preserve OS metadata; never override it.
        return self

    def spawn(self, words, **options):
        child = subprocess.Popen(words, cwd=self.root / "cwd", env=self.env,
                                 start_new_session=True, **options)
        self.children.append(child)
        self.resources["spawned_process_pids"].append(child.pid)
        return child

    def command(self, words, *, stdin=None, timeout=20):
        # Disk-backed streams keep child output out of memory/logs on failures.
        # A bounded command duration and bounded read protect the receipt path.
        with tempfile.TemporaryFile(dir=self.root / "tmp") as output, tempfile.TemporaryFile(dir=self.root / "tmp") as error:
            child = self.spawn(words, stdin=subprocess.PIPE, stdout=output, stderr=error)
            try:
                payload = stdin.encode() if stdin is not None else b""
                require(len(payload) <= 4096, "invalid_fixture_request")
                if payload:
                    child.stdin.write(payload)
                    child.stdin.flush()
            except BrokenPipeError:
                pass
            finally:
                child.stdin.close()
            end = time.monotonic() + timeout
            while not child_exited(child):
                if time.monotonic() >= end:
                    self.reap(child)
                    raise GateFailure("command_timeout")
                time.sleep(.01)
            self.reap(child)
            require(child.returncode == 0, "command_failed")
            output.seek(0)
            content = output.read(MAX_RESPONSE + 1)
            require(len(content) <= MAX_RESPONSE, "response_too_large")
            try:
                return [json.loads(line) for line in content.decode().splitlines() if line.strip()]
            except (ValueError, UnicodeError):
                raise GateFailure("command_output_invalid") from None

    def reap(self, child, *, grace=0):
        require(child in self.children, "cleanup_ownership_unproven")
        if child.pid in self.finalized:
            return child.returncode
        end = time.monotonic() + grace
        while not child_exited(child) and time.monotonic() < end:
            time.sleep(.01)
        # The leader was spawned by this fixture. Kill the entire owned client
        # process group BEFORE reaping, so resistant descendants cannot survive.
        try:
            child_exited(child)  # ECHILD/already reaped must prohibit every signal.
            os.killpg(child.pid, signal.SIGTERM)
            time.sleep(.1)
            child_exited(child)
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            # Darwin can return EPERM for an unreaped group containing only a
            # zombie leader. Check group states before treating it as gone.
            require(not group_alive(child.pid), "cleanup_signal_failed")
        end = time.monotonic() + 5
        while not child_exited(child) or group_alive(child.pid):
            require(time.monotonic() < end, "cleanup_group_survived")
            time.sleep(.01)
        # The sole final reap occurs only AFTER group signals and live-member
        # verification. Nothing may signal this numeric group afterward.
        child.wait(timeout=1)
        self.finalized.add(child.pid)
        return child.returncode

    def __exit__(self, *_):
        # Exceptions in cleanup must never make the default empty-fixture state
        # look clean or delete recovery evidence while an owned process survives.
        for name in self.cleanup:
            self.cleanup[name] = False
        try:
            path = self.root / "appdata/owner-cli.json"
            stop_requested = False
            if path.exists():
                try:
                    self.endpoint = read_endpoint(path)
                    local_json(self.endpoint["control_url"], "/control", self.endpoint["capability"], {"operation": "stop"})
                    stop_requested = True
                except GateFailure:
                    pass
            for child in self.children:
                self.reap(child, grace=7 if stop_requested else 0)
            end = time.monotonic() + 5
            while time.monotonic() < end and any(process_identity(pid) == identity for pid, identity in self.engines):
                time.sleep(.05)
            self.cleanup["processes_exited"] = all(child.pid in self.finalized for child in self.children)
            self.cleanup["engine_processes_exited"] = all(process_identity(pid) != identity for pid, identity in self.engines)
            self.cleanup["capability_removed"] = not path.exists()
            self.cleanup["sockets_closed"] = True
            if self.endpoint:
                for key in ("api_url", "control_url"):
                    try:
                        local_json(self.endpoint[key], "/health" if key == "api_url" else "/control",
                                   self.endpoint["capability"], None if key == "api_url" else {"operation": "health"}, timeout=.3)
                        self.cleanup["sockets_closed"] = False
                    except GateFailure:
                        pass
        finally:
            if self.cleanup["processes_exited"] and self.cleanup["engine_processes_exited"]:
                shutil.rmtree(self.root)
            self.cleanup["temporary_root_removed"] = not self.root.exists()


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def artifacts(args):
    if args.package:
        try:
            spec = importlib.util.spec_from_file_location("core_recipe", Path(__file__).with_name("build_core.py"))
            recipe = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(recipe)
            target = recipe.TARGETS.get((platform.system(), platform.machine()))
            manifest = recipe.verify_package(args.package, target)
            require(manifest["core_protocol"] == manifest["engine_protocol"] == 1, "package_invalid")
            prefix = args.package.resolve()
            core = prefix / "bin/gamja"
            engine = prefix / "libexec/gamja/gamja-engine"
            require(core.is_file() and engine.is_file(), "package_invalid")
            values = {"core": sha256(core), "engine": sha256(engine),
                      "manifest": sha256(prefix / "core-manifest.json")}
            version = manifest.get("version")
            require(isinstance(version, str) and re.fullmatch(r"[0-9A-Za-z.+-]{1,64}", version), "package_invalid")
            return core, [], values, {"package": version, "core_protocol": 1, "engine_protocol": 1}
        except (OSError, ValueError, KeyError, TypeError, GateFailure):
            raise GateFailure("package_invalid") from None
    require(args.core and args.python and args.engine_entry, "source_arguments_required")
    paths = [path.resolve() for path in (args.core, args.python, args.engine_entry)]
    require(all(path.is_file() for path in paths), "source_artifact_missing")
    return paths[0], ["--engine", str(paths[1]), "--engine-arg", str(paths[2])], {
        "core": sha256(paths[0]), "engine_entry": sha256(paths[2])}, {"core_protocol": 1, "engine_protocol": 1}


def account_rows(account, register=False):
    rows = ([{"command": ["register", "--secret-stdin"], "input": account}] if register else [])
    return rows + [{"command": ["login", "--secret-stdin"], "input": {"email": account["email"]}},
                   {"command": ["me"]}, {"command": ["providers"]},
                   {"command": ["projects"]}, {"command": ["agents"]}]


def report_words(operation, report_id=None):
    require(operation in REPORT_OPERATIONS, "invalid_fixture_request")
    needs_id = operation in {"export", "delete"}
    require((needs_id and isinstance(report_id, str) and re.fullmatch(r"[0-9a-f]{32}", report_id))
            or (not needs_id and report_id is None), "invalid_fixture_request")
    return ["report", operation, *([report_id] if needs_id else [])]


def exercise(fixture, core, engine_options, receipt):
    data = fixture.root / "appdata"
    words = [str(core), "--app-data", str(data), *engine_options,
             "--helper", str(fixture.root / "missing-native-helper"), "--startup-timeout-ms", "15000"]

    def public(*arguments):
        result = fixture.command([*words, *arguments])
        require(len(result) == 1, "command_output_invalid")
        return result[0]

    def public_lifecycle(health):
        for result in (public("start", "--if-needed"), public("status")):
            require(isinstance(result, dict) and result.get("engine") == "ready"
                    and result.get("core_pid") == health["core_pid"]
                    and result.get("engine_pid") == health["engine_pid"], "public_lifecycle_gate_failed")

    def public_report(operation, report_id=None):
        return public(*report_words(operation, report_id))

    def control(operation, **extra):
        endpoint = read_endpoint(data / "owner-cli.json")
        fixture.endpoint = endpoint
        for key in ("api_url", "control_url"):
            port = loopback(endpoint[key])
            if port not in fixture.resources["loopback_ports"]:
                fixture.resources["loopback_ports"].append(port)
        status, value = local_json(endpoint["control_url"], "/control", endpoint["capability"], {"operation": operation, **extra})
        require(status == 200, "control_fixture_failed")
        return value

    def launch():
        child = fixture.spawn([*words, "serve"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end = time.monotonic() + 20
        while time.monotonic() < end:
            require(not child_exited(child), "core_exited_before_ready")
            if (data / "owner-cli.json").exists():
                health = control("health")
                if health.get("engine") == "ready":
                    require(health.get("core_pid") == child.pid, "runtime_identity_mismatch")
                    identity = process_identity(health.get("engine_pid"))
                    require(identity is not None, "runtime_identity_mismatch")
                    fixture.engines.append((health["engine_pid"], identity))
                    fixture.resources["engine_process_pids"].append(health["engine_pid"])
                    require(health.get("native_helper") == "unavailable", "missing_helper_gate_failed")
                    return child, health
            time.sleep(.05)
        raise GateFailure("core_ready_timeout")

    def stop(child):
        fixture.command([*words, "stop"])
        fixture.reap(child, grace=8)
        require(not (data / "owner-cli.json").exists(), "capability_cleanup_failed")

    password = secrets.token_urlsafe(24)
    account = {"email": "cli-core-fixture@example.com", "username": "cli-core-fixture"}
    batch = fixture.root / "account.ndjson"

    def auth(register=False):
        rows = account_rows(account, register)
        batch.write_text("".join(json.dumps(row) + "\n" for row in rows))
        batch.chmod(0o600)
        result = fixture.command([*words, "--batch", str(batch)], stdin=(password + "\n") * (2 if register else 1))
        offset = 1 if register else 0
        require(len(result) == offset + 5 and result[offset] == {"authenticated": True}, "account_fixture_failed")
        user = result[offset + 1]
        require(user.get("email") == account["email"] and user.get("username") == account["username"]
                and type(user.get("id")) is int and user["id"] > 0, "account_fixture_failed")
        require(all(value == [] for value in result[offset + 2:]), "empty_domain_fixture_failed")
        require(password not in json.dumps(result) and "access_token" not in json.dumps(result), "credential_output_detected")
        return user["id"]

    child, first = launch()
    public_lifecycle(first)
    receipt["runtime"] = [{"core_pid": first["core_pid"], "engine_pid": first["engine_pid"]}]
    original_capability = fixture.endpoint["capability"]
    endpoint = fixture.endpoint
    require(local_json(endpoint["control_url"], "/control", "00" * 32, {"operation": "health"})[0] == 401,
            "capability_denial_failed")
    require(local_json(endpoint["control_url"], "/control", endpoint["capability"], {"operation": "health"}, origin="http://tauri.localhost")[0] == 401,
            "origin_denial_failed")
    require(local_json(endpoint["api_url"], "/health", endpoint["capability"])[0] == 200, "api_health_failed")
    user_id = auth(register=True)
    # A business client exiting cannot stop the same runtime.
    require(control("health")["core_pid"] == first["core_pid"], "client_lifetime_gate_failed")
    reporting = public_report("status")
    require(reporting.get("auto_reporting_enabled") is False, "report_consent_gate_failed")
    control("report_frontend_failure")
    events = public_report("list").get("events", [])
    require(events, "report_event_gate_failed")
    draft = public_report("create")
    report_id = draft.get("envelope", {}).get("report_id")
    require(isinstance(report_id, str) and re.fullmatch(r"[0-9a-f]{32}", report_id), "report_fixture_failed")
    export = Path(public_report("export", report_id))
    require(export.resolve().is_relative_to(data.resolve()) and export.is_file() and export.stat().st_size <= MAX_RESPONSE,
            "report_export_gate_failed")
    require(report_id in export.read_text(), "report_export_gate_failed")
    stop(child)
    child, second = launch()
    public_lifecycle(second)
    receipt["runtime"].append({"core_pid": second["core_pid"], "engine_pid": second["engine_pid"]})
    require(first["core_pid"] != second["core_pid"] and original_capability != fixture.endpoint["capability"], "restart_identity_gate_failed")
    require(auth() == user_id, "account_restart_gate_failed")
    require(report_id in json.dumps(public_report("list")), "report_restart_gate_failed")
    public_report("delete", report_id)
    require(report_id not in json.dumps(public_report("list")), "report_delete_gate_failed")
    require(public_report("status").get("auto_reporting_enabled") is False, "report_consent_gate_failed")
    stop(child)
    for gate in ("public_start_if_needed_status", "api_account_cli", "authenticated_control", "missing_helper", "client_exit_persistence",
                 "reporting_no_consent", "restart_account_and_report", "explicit_stop"):
        receipt["gates"][gate] = "pass"


def write_receipt(path, value):
    payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    require(len(payload) <= MAX_RECEIPT, "receipt_too_large")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise GateFailure("receipt_exists") from None
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, help="verified independent installed prefix; no copies/builds")
    parser.add_argument("--core", type=Path, help="source-mode native gamja executable")
    parser.add_argument("--python", type=Path, help="source-mode existing shared interpreter")
    parser.add_argument("--engine-entry", type=Path, help="source-mode existing backend/run.py")
    parser.add_argument("--receipt", type=Path, required=True, help="new evidence JSON outside disposable fixtures")
    args = parser.parse_args(argv)
    receipt = {"format": 1, "scope": "frozen_account_control" if args.package else "source_account_control",
               "status": "failed", "failure": None, "artifact_hashes": {}, "versions": {}, "runtime": [],
               "gates": {name: "not_run" for name in (
                   "frozen_independent_package", "public_start_if_needed_status", "api_account_cli", "authenticated_control", "missing_helper",
                   "client_exit_persistence", "reporting_no_consent", "restart_account_and_report", "explicit_stop",
                   "canonical_provider_job_schedule", "installed_gui_persistence", "native_os_execution",
                   "windows_native", "intel_mac_native", "whole_branch_review", "cutover")},
               "cleanup": {}, "limitations": ["No vendor or OS secure-store operations", "No GUI, schedule/job or native OS gate", "No installed app replacement or cutover"]}
    fixture = None
    previous_term = signal.getsignal(signal.SIGTERM)
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        require(os.name == "posix", "native_platform_harness_pending")
        require(not (args.package and any((args.core, args.python, args.engine_entry))), "ambiguous_artifacts")
        core, options, hashes, versions = artifacts(args)
        receipt["artifact_hashes"], receipt["versions"] = hashes, versions
        fixture = PrivateFixture()
        with fixture:
            exercise(fixture, core, options, receipt)
        require(all(fixture.cleanup.values()), "cleanup_incomplete")
        if args.package:
            receipt["gates"]["frozen_independent_package"] = "pass"
        receipt["status"] = "passed_scoped_gate"
    except GateFailure as error:
        receipt["failure"] = str(error)
    except KeyboardInterrupt:
        receipt["failure"] = "harness_interrupted"
    except Exception:
        receipt["failure"] = "harness_execution_failed"
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        receipt["cleanup"] = fixture.cleanup if fixture else {"no_fixture_resources_created": True}
        receipt["resources"] = fixture.resources if fixture else {}
    try:
        write_receipt(args.receipt, receipt)
    except (OSError, GateFailure):
        print(json.dumps({"status": "failed", "failure": "receipt_write_failed"}))
        return 1
    print(json.dumps({"status": receipt["status"], "failure": receipt["failure"]}))
    return 0 if receipt["status"] == "passed_scoped_gate" else 1


if __name__ == "__main__":
    raise SystemExit(main())
