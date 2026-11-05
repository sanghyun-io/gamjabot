#!/usr/bin/env python3
"""Owner CLI for an already-running Gamja Bot backend.

The per-launch capability file is created by the local host.  It is a
local transport credential, not a user login; this client keeps any JWT only in
memory and never prints credentials.
"""

import argparse
import base64
import math
import threading
import getpass
import json
import os
import platform
import plistlib
import re
import shlex
import signal
import stat
import subprocess
import sys
import time
import warnings
from uuid import UUID, uuid4
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from app import desktop_diagnostics
from app.services.auth_diagnostics import valid_auth_diagnostic
from app.schemas.provider_models import valid_model_id
from app.transcript_export import export_room, write_private_atomic


def _default_capability() -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        root = home / "Library/Application Support"
    else:
        name, fallback = ("APPDATA", home / "AppData/Roaming") if sys.platform == "win32" else ("XDG_DATA_HOME", home / ".local/share")
        value = os.environ.get(name, "")
        candidate = Path(value)
        root = candidate if candidate.is_absolute() and not any(ord(c) < 32 or ord(c) == 127 for c in value) else fallback
    return root / "top.gamja.bot/owner-cli.json"


DEFAULT_CAPABILITY = _default_capability()
ENDPOINT_FILE_VERSION = 1
CORE_PROTOCOL_VERSION = 1
ENGINE_PROTOCOL_VERSION = 1
BOUNDED_RESPONSE_BYTES = 64 * 1024
RESTORE_FAILURE_CODES = frozenset({"credential_locked", "secret_store_unavailable"})
RESTORE_DIAGNOSTIC_REASONS = frozenset({
    "keychain_operation_in_progress", "keychain_interaction_required",
    "keychain_interaction_state_read_failed", "keychain_interaction_disable_failed",
    "keychain_interaction_restore_failed", "keychain_no_ui_guard_failed",
    "keychain_interaction_state_uncertain", "keychain_query_failed",
    "keychain_query_binding_failed", "keychain_store_unavailable",
    "keychain_backend_unsupported", "keychain_backend_unavailable",
})
RESTORE_DIAGNOSTIC_CLASSES = {
    "credential_locked": "secret_interaction_required",
    "secret_store_unavailable": "secret_store_unavailable",
}
REMEMBER_FAILURE_CODES = frozenset({"credential_locked", "secret_store_unavailable"})
REMEMBER_DIAGNOSTIC_REASONS = frozenset({
    "credential_channel_unavailable", "credential_response_invalid", "credential_request_invalid",
    "keychain_operation_in_progress", "keychain_interaction_required",
    "keychain_interaction_state_read_failed", "keychain_interaction_disable_failed",
    "keychain_interaction_restore_failed", "keychain_no_ui_guard_failed",
    "keychain_interaction_state_uncertain", "keychain_query_failed",
    "keychain_query_binding_failed", "keychain_store_unavailable",
    "keychain_backend_unsupported", "keychain_backend_unavailable",
    "credential_reentry_required", "automatic_login_readback_failed",
    "automatic_login_save_uncertain", "automatic_login_save_failed",
})
REMEMBER_DIAGNOSTIC_CLASSES = frozenset({
    "native_os_status", "interaction_required", "bridge_transport",
    "bridge_protocol", "request_validation", "store_unavailable",
    "credential_reentry_required",
})
PROVIDER_DIAGNOSTIC_REASONS = frozenset({
    "keychain_query_failed", "keychain_interaction_required", "credential_channel_unavailable",
    "credential_response_invalid", "keychain_store_unavailable",
})
PROVIDER_DIAGNOSTIC_CLASSES = frozenset({
    "native_os_status", "interaction_required", "bridge_transport",
    "bridge_protocol", "store_unavailable",
})
PROVIDER_DIAGNOSTIC_OPERATIONS = frozenset({
    "sec_item_add", "sec_item_update", "sec_item_copy_matching", "sec_item_delete",
})


def default_headless_binary() -> Path:
    """Use the CLI's own installed bundle, never a guessed system location."""
    if getattr(sys, "frozen", False):
        macos = Path(sys.executable).resolve().parent
        info = macos.parent / "Info.plist"
        try:
            with info.open("rb") as source:
                executable = plistlib.load(source).get("CFBundleExecutable")
        except (OSError, ValueError, plistlib.InvalidFileException):
            raise RuntimeError("Cannot locate the installed Gamja Bot host") from None
        if not isinstance(executable, str) or not executable or "/" in executable or "\\" in executable:
            raise RuntimeError("Cannot locate the installed Gamja Bot host")
        candidate = macos / executable
        if not candidate.is_file():
            raise RuntimeError("Cannot locate the installed Gamja Bot host")
        return candidate
    # Source development has no frozen sibling; its explicit convenience
    # fallback never affects a packaged CLI.
    return Path("/Applications/Gamja Bot.app/Contents/MacOS/Gamja Bot")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, location):
        return None


class RestoreFailure(RuntimeError):
    def __init__(self, code: str, correlation_id: str):
        self.code, self.correlation_id = code, correlation_id
        super().__init__(f"Saved login unavailable ({code}; correlation {correlation_id})")


class RememberFailure(RuntimeError):
    def __init__(self, code: str, correlation_id: str):
        self.code, self.correlation_id = code, correlation_id
        super().__init__(f"Automatic login save failed ({code}; correlation {correlation_id})")


class RestoreHttpFailure(RuntimeError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"Saved login request failed ({status})")


class CredentialStoreFailure(RuntimeError):
    def __init__(self, correlation_id: str):
        self.correlation_id = correlation_id
        super().__init__(f"Credential store unavailable (correlation {correlation_id})")


def _loopback_url(value) -> bool:
    if (not isinstance(value, str) or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and port is not None
        and parsed.netloc == f"127.0.0.1:{port}"
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
    )


def _private_capability(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def capability(path: Path) -> dict:
    if sys.platform == "win32":
        return _windows_capability(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise RuntimeError("Owner CLI capability is unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)):
            raise RuntimeError("Owner CLI capability is unavailable")
        with os.fdopen(descriptor, encoding="utf-8") as source:
            descriptor = None
            data = json.load(source)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return _validate_endpoint(data)


def _validate_endpoint(data, *, allow_legacy=True):
    if not isinstance(data, dict):
        raise RuntimeError("Owner CLI capability is unavailable")
    if allow_legacy and set(data) == {"base_url", "capability"}:
        if not _loopback_url(data["base_url"]) or not _private_capability(data["capability"]):
            raise RuntimeError("Owner CLI capability is unavailable")
        return data
    required = {"version", "api_url", "control_url", "capability", "core_version", "engine_version"}
    if (set(data) != required
            or type(data["version"]) is not int or data["version"] != ENDPOINT_FILE_VERSION
            or type(data["core_version"]) is not int or data["core_version"] != CORE_PROTOCOL_VERSION
            or type(data["engine_version"]) is not int or data["engine_version"] != ENGINE_PROTOCOL_VERSION
            or not _loopback_url(data["api_url"]) or not _loopback_url(data["control_url"])
            or not _private_capability(data["capability"])):
        raise RuntimeError("Owner CLI capability is unavailable")
    return {**data, "base_url": data["api_url"]}


class EndpointReadFailure(RuntimeError):
    """Allowlisted Windows endpoint-reader result; no native detail or paths."""
    def __init__(self, code):
        self.code = code
        super().__init__(f"Owner CLI capability is unavailable ({code})")


def _unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate endpoint field")
        value[key] = item
    return value


def _windows_capability(path, *, _fixture_dll=None):
    """Use the Rust-verified endpoint in memory; never read a Windows file here.

    The private fixture injection is callable only in unfrozen source tests.
    Packaged CLI loads the absolute DLL next to its own gamja-engine.exe.
    The ABI takes the app-data directory, not the endpoint filename.
    """
    import ctypes
    path = Path(path)
    try:
        if not path.is_absolute() or path.name != "owner-cli.json" or "\0" in str(path):
            raise EndpointReadFailure("invalid_endpoint_request")
        encoded = str(path.parent).encode("utf-16-le")
        units = len(encoded) // 2
        if not 1 <= units <= 32767:
            raise EndpointReadFailure("invalid_endpoint_request")
    except UnicodeError:
        raise EndpointReadFailure("invalid_endpoint_request") from None
    frozen = bool(getattr(sys, "frozen", False))
    if _fixture_dll is not None:
        if frozen:
            raise EndpointReadFailure("endpoint_reader_unavailable")
        dll = Path(_fixture_dll)
    else:
        executable = Path(sys.executable)
        if not frozen or not executable.is_absolute() or executable.name.lower() != "gamja-engine.exe":
            raise EndpointReadFailure("endpoint_reader_unavailable")
        dll = executable.resolve().with_name("gamja_core_host.dll")
    if not dll.is_absolute() or not dll.is_file():
        raise EndpointReadFailure("endpoint_reader_unavailable")
    try:
        # Cdecl ABI. Primary DLL is an exact absolute filename; dependencies may
        # resolve only beside that DLL or in System32, never PATH/current dir.
        library = ctypes.CDLL(str(dll), winmode=0x900)
        reader = library.gamja_core_read_endpoint
        reader.argtypes = [ctypes.POINTER(ctypes.c_uint16), ctypes.c_size_t,
                          ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t,
                          ctypes.POINTER(ctypes.c_size_t)]
        reader.restype = ctypes.c_uint32
    except (OSError, AttributeError):
        raise EndpointReadFailure("endpoint_reader_unavailable") from None
    wide = (ctypes.c_uint16 * units).from_buffer_copy(encoded)
    output = (ctypes.c_ubyte * 4097)()
    written = ctypes.c_size_t()
    try:
        try:
            status = reader(wide, units, output, len(output), ctypes.byref(written))
        except (OSError, ctypes.ArgumentError):
            raise EndpointReadFailure("endpoint_reader_unavailable") from None
        if status != 0:
            code = {1: "invalid_endpoint_request", 2: "endpoint_unavailable", 3: "invalid_endpoint",
                    4: "endpoint_buffer_unavailable", 5: "endpoint_reader_unavailable"}.get(status, "endpoint_reader_unavailable")
            raise EndpointReadFailure(code)
        length = written.value
        raw = bytes(output)
        if not 1 <= length <= 4096 or raw[length] != 0 or b"\0" in raw[:length]:
            raise EndpointReadFailure("invalid_endpoint")
        try:
            data = json.loads(raw[:length].decode("utf-8"), object_pairs_hook=_unique_json_object)
            return _validate_endpoint(data, allow_legacy=False)
        except (ValueError, UnicodeError, RuntimeError):
            raise EndpointReadFailure("invalid_endpoint") from None
    finally:
        ctypes.memset(output, 0, ctypes.sizeof(output))
        ctypes.memset(wide, 0, ctypes.sizeof(wide))
        written.value = 0


def _backend_lock_held(app_data: Path) -> bool:
    """Probe the backend's lifetime lock without retaining or changing it."""
    lock = app_data / "gamja_bot.db.backend.lock"
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                return True
            return False
        import fcntl
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False


def _stop_owned_process(process) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def legacy_serve_is_supported() -> bool:
    """Keep direct Tauri ownership only on platforms not yet cut over."""
    return not (sys.platform == "darwin" and platform.machine().lower() == "arm64")


def serve(app_data: Path, binary: Path | None) -> None:
    if not legacy_serve_is_supported():
        raise RuntimeError("Legacy serve is unavailable on Apple Silicon; use public gamja start")
    try:
        existing = Client(capability(app_data / "owner-cli.json"))
        existing.call("GET", "/health")
    except RuntimeError:
        pass
    else:
        print("A running backend is already available; attach with status.")
        return
    if _backend_lock_held(app_data):
        raise RuntimeError("A running backend does not expose Owner CLI access; stop or upgrade it before starting headless mode")
    binary = binary or default_headless_binary()
    if not binary.is_file():
        raise RuntimeError("Headless Gamja Bot binary is unavailable")
    previous_handlers = {}
    process = None

    def interrupt(_signal, _frame):
        raise KeyboardInterrupt

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, interrupt)
        environment = os.environ.copy()
        environment["GAMJA_HEADLESS_OWNER_PID"] = str(os.getpid())
        process = subprocess.Popen(
            [str(binary), "--headless"],
            start_new_session=True,
            env=environment,
        )
        for _ in range(60):
            time.sleep(.5)
            if process.poll() is not None:
                raise RuntimeError("Headless Gamja Bot exited before it became ready")
            try:
                active = Client(capability(app_data / "owner-cli.json"))
                active.call("GET", "/health")
            except RuntimeError:
                continue
            print("Headless backend is ready; use a second CLI shell for status, login, or restore.")
            exit_code = process.wait()
            if exit_code:
                raise RuntimeError("Headless Gamja Bot exited unexpectedly")
            return
        raise RuntimeError("Headless Gamja Bot did not become ready")
    except KeyboardInterrupt:
        if process is not None:
            _stop_owned_process(process)
    except BaseException:
        if process is not None:
            _stop_owned_process(process)
        raise
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


class Client:
    def __init__(self, data: dict):
        self.base_url, self.capability, self.token = data["base_url"], data["capability"], None
        self.app_data: Path | None = None
        self._renew_lock = threading.Lock()
        self._renew_failed_token = None
        self._renew_failed_until = 0.0

    @staticmethod
    def _token_schedule(token):
        """Unverified claims are scheduling hints, never authentication."""
        if not isinstance(token, str) or len(token) > 8192:
            return None
        try:
            parts = token.split('.')
            if len(parts) != 3:
                return None
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
            subject, expiry = payload.get('sub'), payload.get('exp')
            if (not isinstance(subject, str) or not re.fullmatch(r'[1-9][0-9]{0,18}', subject)
                    or int(subject) >= 2 ** 63 or type(expiry) not in {int, float}
                    or not math.isfinite(expiry) or expiry <= 0):
                return None
            return int(subject), float(expiry)
        except (ValueError, TypeError, AttributeError, UnicodeError, OverflowError, RecursionError):
            return None

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('Running backend is unavailable')
        return min(65, remaining)

    def _renew_before_dispatch(self, token, owner_id, expiry, deadline):
        if not self._renew_lock.acquire(timeout=self._remaining(deadline)):
            raise RuntimeError('Running backend is unavailable')
        try:
            if self.token != token:
                current = self._token_schedule(self.token)
                if current and current[0] == owner_id and current[1] > time.time():
                    return self.token
                raise RuntimeError('Login session changed; try again')
            if expiry > time.time() + 60:
                return token
            if (self._renew_failed_token == token
                    and self._renew_failed_until > time.monotonic()):
                if expiry > time.time():
                    return token
                raise RuntimeError('Run login or restore first')
            try:
                restored = self._call_once('POST', '/auth/restore',
                    {'expected_owner_id': owner_id}, request_timeout=self._remaining(deadline))
                if self.token != token:
                    raise RuntimeError('Login session changed; try again')
                fresh = restored.get('access_token') if isinstance(restored, dict) else None
                schedule = self._token_schedule(fresh)
                if not schedule or schedule[0] != owner_id or schedule[1] <= time.time():
                    raise RuntimeError('Saved login is unavailable')
                profile = self._call_once('GET', '/auth/me', authenticated=True,
                    request_timeout=self._remaining(deadline), _token=fresh)
                if self.token != token:
                    raise RuntimeError('Login session changed; try again')
                if (not isinstance(profile, dict) or type(profile.get('id')) is not int
                        or profile['id'] != owner_id or schedule[1] <= time.time()):
                    raise RuntimeError('Saved login is unavailable')
                self.token = fresh
                self._renew_failed_token = None
                self._renew_failed_until = 0.0
                return fresh
            except RuntimeError:
                if self.token != token:
                    raise RuntimeError('Login session changed; try again') from None
                self._renew_failed_token = token
                self._renew_failed_until = time.monotonic() + 30
                if expiry > time.time():
                    return token
                raise
        finally:
            self._renew_lock.release()

    def call(self, method: str, path: str, body=None, *, authenticated=False,
             idempotency_key=None, request_timeout=None):
        if (request_timeout is not None
                and (type(request_timeout) not in {int, float}
                     or not 0 < request_timeout <= 65)):
            raise RuntimeError('Invalid internal request timeout')
        token = self.token
        schedule = self._token_schedule(token) if authenticated and not path.startswith('/auth/') else None
        if schedule and schedule[1] <= time.time() + 60:
            deadline = time.monotonic() + (65 if request_timeout is None else request_timeout)
            token = self._renew_before_dispatch(token, *schedule, deadline)
            if self.token != token:
                raise RuntimeError('Login session changed; try again')
            request_timeout = self._remaining(deadline)
        return self._call_once(method, path, body, authenticated=authenticated,
                               idempotency_key=idempotency_key, request_timeout=request_timeout,
                               _token=token if authenticated else None)

    def _call_once(self, method: str, path: str, body=None, *, authenticated=False,
                   idempotency_key=None, request_timeout=None, _token=None):
        if (request_timeout is not None
                and (type(request_timeout) not in {int, float}
                     or not 0 < request_timeout <= 65)):
            raise RuntimeError("Invalid internal request timeout")
        timeout = 65 if request_timeout is None else request_timeout
        request_deadline = (None if request_timeout is None
                            else time.monotonic() + request_timeout)
        headers = {"X-Gamja-CLI-Capability": self.capability, "Accept": "application/json"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        if authenticated:
            token = self.token if _token is None else _token
            if not token:
                raise RuntimeError("Run login or restore first")
            headers["Authorization"] = f"Bearer {token}"
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        url = self.base_url + (path if path == "/health" else "/api/v1" + path)
        request = Request(url, data=payload, headers=headers, method=method)
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(
                    request, timeout=timeout) as response:
                if request_deadline is None:
                    raw = response.read()
                else:
                    remaining = request_deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError("Running backend is unavailable")
                    response_socket = getattr(
                        getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                    if response_socket is not None and hasattr(response_socket, "settimeout"):
                        response_socket.settimeout(remaining)
                    raw = response.read(BOUNDED_RESPONSE_BYTES + 1)
                    if len(raw) > BOUNDED_RESPONSE_BYTES:
                        raise RuntimeError("Running backend returned an invalid response")
                    if request_deadline - time.monotonic() <= 0:
                        raise RuntimeError("Running backend is unavailable")
                try:
                    return json.loads(raw) if raw else None
                except (ValueError, UnicodeError):
                    raise RuntimeError("Running backend returned an invalid response") from None
        except HTTPError as error:
            # Server detail can contain failed validation input or upstream text.
            # The CLI reports only its fixed status class; detailed diagnostics
            # remain available through the separately schema-validated command.
            status = error.code if isinstance(error.code, int) and 100 <= error.code <= 599 else 0
            if (path.endswith("/clone") or "/room-templates" in path
                    or path == "/conversations/launch") and status == 409:
                try:
                    detail = json.loads(error.read(1024)).get("detail")
                    agent_ids = detail.get("agent_ids") if isinstance(detail, dict) else None
                    if (isinstance(detail, dict)
                            and set(detail) == {"code", "agent_ids"}
                            and detail["code"] == "room_clone_members_unavailable"
                            and isinstance(agent_ids, list) and 1 <= len(agent_ids) <= 1000
                            and all(type(agent_id) is int and 1 <= agent_id < 2 ** 63 for agent_id in agent_ids)
                            and agent_ids == sorted(set(agent_ids))):
                        raise RuntimeError(
                            "Room clone blocked; unavailable Agent IDs: "
                            + ", ".join(str(agent_id) for agent_id in agent_ids)
                        ) from None
                    if (isinstance(detail, dict) and set(detail) == {"code"}
                            and detail["code"] == "room_template_name_conflict"):
                        raise RuntimeError("A room template with that name already exists") from None
                    if (isinstance(detail, dict) and set(detail) == {"code", "agent_ids"}
                            and detail["code"] == "room_template_members_unavailable"
                            and isinstance(agent_ids, list) and 1 <= len(agent_ids) <= 64
                            and all(type(agent_id) is int and 1 <= agent_id < 2 ** 63 for agent_id in agent_ids)
                            and agent_ids == sorted(set(agent_ids))):
                        raise RuntimeError(
                            "Room template blocked; unavailable Agent IDs: "
                            + ", ".join(str(agent_id) for agent_id in agent_ids)
                        ) from None
                    if (isinstance(detail, dict) and set(detail) == {"code"}
                            and detail["code"] == "room_launch_idempotency_conflict"):
                        raise RuntimeError(
                            "Room launch key was already used for different input") from None
                    if (isinstance(detail, dict) and set(detail) == {"code", "agent_ids"}
                            and detail["code"] == "room_launch_agents_unavailable"
                            and isinstance(agent_ids, list) and 1 <= len(agent_ids) <= 64
                            and all(type(agent_id) is int and 1 <= agent_id < 2 ** 63 for agent_id in agent_ids)
                            and agent_ids == sorted(set(agent_ids))):
                        raise RuntimeError(
                            "Room launch blocked; unavailable Agent IDs: "
                            + ", ".join(str(agent_id) for agent_id in agent_ids)
                        ) from None
                except (ValueError, AttributeError, TypeError):
                    pass
            if path.startswith("/workspace/blueprint") and status == 409:
                try:
                    detail = json.loads(error.read(1024)).get("detail")
                    if isinstance(detail, dict) and set(detail) == {"code"}:
                        if detail["code"] == "workspace_source_invalid":
                            raise RuntimeError(
                                "Workspace export has an unavailable dependency") from None
                        if detail["code"] == "workspace_plan_stale":
                            raise RuntimeError(
                                "Workspace plan is stale; preview the file again") from None
                except (ValueError, AttributeError, TypeError):
                    pass
            if path.startswith("/projects/") and status == 409:
                try:
                    detail = json.loads(error.read(1024)).get("detail")
                    if isinstance(detail, dict):
                        code = detail.get("code")
                        if set(detail) == {"code"} and code == "project_archive_plan_stale":
                            raise RuntimeError("Project archive plan is stale; preview it again") from None
                        if set(detail) == {"code"} and code == "project_archive_preview_required":
                            raise RuntimeError("Project has active rooms; preview archive impact first") from None
                        if set(detail) == {"code"} and code == "project_not_archived":
                            raise RuntimeError("Project is not archived") from None
                        active_rooms = detail.get("active_rooms")
                        if (set(detail) == {"code", "active_rooms"}
                                and code == "project_restore_active_children"
                                and type(active_rooms) is int and 1 <= active_rooms < 2 ** 31):
                            raise RuntimeError(
                                f"Archived project still has {active_rooms} active room(s); preview and apply archive cleanup first"
                            ) from None
                except (ValueError, AttributeError, TypeError):
                    pass
            if path == "/auth/restore" and status == 503:
                try:
                    detail = json.loads(error.read(1024)).get("detail")
                    code = detail.get("code") if isinstance(detail, dict) else None
                    correlation = detail.get("correlation_id") if isinstance(detail, dict) else None
                    if (code in RESTORE_FAILURE_CODES and isinstance(correlation, str)
                            and len(correlation) == 32
                            and all(char in "0123456789abcdef" for char in correlation)):
                        raise RestoreFailure(code, correlation) from None
                except (ValueError, AttributeError):
                    pass
            if path == "/auth/login" and status == 503:
                try:
                    detail = json.loads(error.read(1024)).get("detail")
                    code = detail.get("code") if isinstance(detail, dict) else None
                    correlation = detail.get("correlation_id") if isinstance(detail, dict) else None
                    if (code in REMEMBER_FAILURE_CODES and isinstance(correlation, str)
                            and len(correlation) == 32
                            and all(char in "0123456789abcdef" for char in correlation)):
                        raise RememberFailure(code, correlation) from None
                except (ValueError, AttributeError):
                    pass
            if path.startswith("/connections") and status == 503:
                try:
                    detail = json.loads(error.read(1024)).get("detail")
                    code = detail.get("code") if isinstance(detail, dict) else None
                    correlation = detail.get("correlation_id") if isinstance(detail, dict) else None
                    if (code == "secret_store_unavailable" and isinstance(correlation, str)
                            and re.fullmatch(r"[0-9a-f]{32}", correlation)):
                        raise CredentialStoreFailure(correlation) from None
                except (ValueError, AttributeError):
                    pass
            if path == "/auth/restore" and status:
                raise RestoreHttpFailure(status) from None
            raise RuntimeError(f"Request failed ({status})") from None
        except (URLError, OSError):
            raise RuntimeError("Running backend is unavailable") from None

    def login(self):
        email = input("Email: ").strip()
        password = read_secret("Password: ")
        result = self.call("POST", "/auth/login", {
            "email": email, "password": password, "remember_device": False,
            "preserve_remembered_login": True,
        })
        self.token = result["access_token"]

    def restore(self, interactive=False):
        if not interactive:
            self.token = self.call("POST", "/auth/restore")["access_token"]
            return
        started = self.call("POST", "/auth/restore/interactive")
        unlock_id = started.get("unlock_id")
        for _ in range(120):
            time.sleep(.5)
            result = self.call("GET", f"/auth/restore/interactive?unlock_id={unlock_id}")
            if result.get("state") == "ready":
                self.token = result["access_token"]
                return
            if result.get("state") == "locked":
                break
        raise RuntimeError("Saved login is locked or unavailable")


SECRET_FIELDS = frozenset({"password", "api_key", "access_token", "token", "capability", "secret_ref", "hashed_password"})
DISCONNECT_WAIT_SECONDS = 30
PROVIDER_OPERATION_POLL_MAX_SECONDS = 300
PROVIDER_REVOKE_STATES = frozenset({
    "revoked", "skipped", "not_supported", "already_invalid", "remote_revoke_failed",
})
PROVIDER_RESTORED_STATES = frozenset({
    "authorizing", "present", "refresh_required", "rejected", "locked", "unavailable",
})
PROVIDER_OPERATION_ERROR_CODES = frozenset({
    "auth_method_unavailable", "configuration_changed", "idempotency_conflict",
    "auth_operation_busy", "owner_epoch_unavailable", "owner_epoch_changed",
    "credential_store_unavailable", "credential_recovery_required",
    "credential_cleanup_unconfirmed", "provider_authorization_denied",
    "provider_temporary_failure", "provider_invalid_response", "operation_cancelled",
    "operation_expired", "operation_interrupted", "internal_error",
})
PROVIDER_AUTH_METHODS = frozenset({
    "api_key", "local_none", "oauth_browser", "oauth_device", "native_chain",
})
PROVIDER_AUTH_PROGRESS_STATES = frozenset({
    "prepared", "awaiting_user", "polling", "exchanging", "committing",
    "recovering", "pending", "finalizing",
})


def compact(value, *, secrets=()):
    def safe(item):
        if isinstance(item, dict):
            return {key: "[redacted]" if key.lower() in SECRET_FIELDS else safe(part) for key, part in item.items()}
        if isinstance(item, list):
            return [safe(part) for part in item]
        if isinstance(item, str):
            for secret in secrets:
                if secret:
                    item = item.replace(secret, "[redacted]")
        return item
    print(json.dumps(safe(value), ensure_ascii=False, sort_keys=True))


def read_secret(prompt, *, from_stdin=False):
    if from_stdin:
        value = sys.stdin.readline().removesuffix("\n").removesuffix("\r")
    else:
        if not sys.stdin.isatty():
            raise RuntimeError("A TTY is required for hidden input; use --secret-stdin for a dedicated secret stream")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = getpass.getpass(prompt)
        except (getpass.GetPassWarning, EOFError):
            raise RuntimeError("Hidden secret input is unavailable; use --secret-stdin") from None
    if not value:
        raise RuntimeError("Empty secret input")
    return value


def _public_input(value):
    if isinstance(value, dict):
        for key, part in value.items():
            if key.lower() in SECRET_FIELDS:
                raise RuntimeError("JSON must not contain secrets; use hidden input or --secret-stdin")
            _public_input(part)
    elif isinstance(value, list):
        for part in value:
            _public_input(part)


def _read_input(source, *, json_input=True):
    try:
        raw = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
        if not json_input:
            return raw
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise RuntimeError("JSON input must be an object")
        _public_input(value)
        return value
    except (OSError, UnicodeError, ValueError):
        raise RuntimeError("Cannot read input; expected a UTF-8 JSON object" if json_input else "Cannot read UTF-8 text input") from None


def _command_options(words, input_body):
    """Parse and validate input sources without consuming any input."""
    args, options = [], {}
    index = 0
    while index < len(words):
        word = words[index]
        if word in {"--json", "--file", "--idempotency-key", "--timeout", "--output", "--status", "--project", "--query", "--limit", "--cursor", "--review-id"}:
            if word in options or index + 1 == len(words):
                raise RuntimeError("Invalid command options")
            options[word] = words[index + 1]
            index += 2
        elif word in {"--secret", "--secret-stdin", "--stdin", "--force", "--refresh", "--revoke",
                      "--remember-device", "--acknowledge-unverified-context", "--send-image-to-model"}:
            if word in options:
                raise RuntimeError("Invalid command options")
            options[word] = True
            index += 1
        else:
            args.append(word)
            index += 1
    if (sum(key in options for key in ("--json", "--file", "--stdin")) > 1
            or ("--secret" in options and "--secret-stdin" in options)
            or ("--secret-stdin" in options and (options.get("--json") == "-" or options.get("--file") == "-" or "--stdin" in options))
            or (input_body is not None and any(key in options for key in ("--json", "--file", "--stdin")))):
        raise RuntimeError("Input sources must be separate and unambiguous")
    if input_body is not None:
        if not isinstance(input_body, dict):
            raise RuntimeError("JSON input must be an object")
        _public_input(input_body)
    return args, options


def _command_input(words, input_body):
    args, options = _command_options(words, input_body)
    body = _read_input(options["--json"]) if "--json" in options else input_body
    if "--file" in options or "--stdin" in options:
        workspace_file = len(args) >= 2 and args[0] == "workspace" and args[1] in {"preview", "apply"}
        body = (_read_input(options.get("--file", "-")) if workspace_file
                else {"content": _read_input(options.get("--file", "-"), json_input=False)})
    return args, options, body


def _ids(args, count):
    return len(args) == count and all(re.fullmatch(r"[0-9]+", part) for part in args)


def _action_id(value):
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value))


def _event_id(value):
    if not isinstance(value, str) or len(value) != 36 or value != value.lower():
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _provider_operation_response(
        value, *, operation_id=None, allow_immediate=False, allow_terminal=True):
    invalid = RuntimeError("Canonical provider operation response is invalid")
    if not isinstance(value, dict):
        raise invalid
    if allow_immediate and set(value) == {"credential_state", "revoke_state"}:
        if (value["credential_state"] == "missing"
                and isinstance(value["revoke_state"], str)
                and value["revoke_state"] in PROVIDER_REVOKE_STATES):
            return value
        raise invalid
    actual_operation_id = value.get("operation_id")
    if (set(value) == {"version", "operation_id", "state", "expires_at", "poll_interval"}
            and value.get("version") == 1 and type(value.get("version")) is int
            and isinstance(actual_operation_id, str)
            and re.fullmatch(r"[0-9a-f]{64}", actual_operation_id)
            and (operation_id is None or actual_operation_id == operation_id)
            and isinstance(value.get("state"), str)
            and value.get("state") in {"pending", "finalizing"}
            and value.get("expires_at") is None
            and type(value.get("poll_interval")) is int
            and 1 <= value["poll_interval"] <= PROVIDER_OPERATION_POLL_MAX_SECONDS):
        return value
    if (value.get("version") == 1 and type(value.get("version")) is int
            and isinstance(actual_operation_id, str)
            and re.fullmatch(r"[0-9a-f]{64}", actual_operation_id)
            and (operation_id is None or actual_operation_id == operation_id)
            and isinstance(value.get("state"), str)
            and value.get("state") in PROVIDER_AUTH_PROGRESS_STATES):
        allowed = {"version", "operation_id", "state", "expires_at", "browser_url",
                   "verification_uri", "user_code", "poll_interval", "error_code"}
        if not set(value) <= allowed or "expires_at" not in value:
            raise invalid
        if (value["expires_at"] is not None
                and (not isinstance(value["expires_at"], str)
                     or not 1 <= len(value["expires_at"]) <= 64
                     or any(ord(char) <= 32 or ord(char) == 127
                            for char in value["expires_at"]))):
            raise invalid
        browser = "browser_url" in value
        device = {key for key in ("verification_uri", "user_code", "poll_interval") if key in value}
        if browser and (not _safe_public_auth_url(value["browser_url"]) or device):
            raise invalid
        if device and (device != {"verification_uri", "user_code", "poll_interval"}
                       or not _safe_public_auth_url(value["verification_uri"])
                       or not isinstance(value["user_code"], str)
                       or not 1 <= len(value["user_code"]) <= 64
                       or any(ord(char) < 32 or ord(char) == 127 for char in value["user_code"])
                       or type(value["poll_interval"]) is not int
                       or not 1 <= value["poll_interval"] <= PROVIDER_OPERATION_POLL_MAX_SECONDS):
            raise invalid
        if ("error_code" in value
                and value["error_code"] not in PROVIDER_OPERATION_ERROR_CODES):
            raise invalid
        return value
    if not allow_terminal:
        raise invalid
    if (set(value) == {"version", "operation_id", "state", "result"}
            and value.get("version") == 1 and type(value.get("version")) is int
            and isinstance(actual_operation_id, str)
            and re.fullmatch(r"[0-9a-f]{64}", actual_operation_id)
            and (operation_id is None or actual_operation_id == operation_id)
            and value.get("state") == "succeeded"):
        result = value.get("result")
        if (isinstance(result, dict)
                and set(result) == {"credential_state"}
                and result.get("credential_state") == "present"):
            return value
        if (isinstance(result, dict)
                and set(result) == {"credential_state", "revoke_state"}
                and result.get("credential_state") == "missing"
                and isinstance(result.get("revoke_state"), str)
                and result.get("revoke_state") in PROVIDER_REVOKE_STATES):
            return value
    if (set(value) == {"version", "operation_id", "state", "error_code"}
            and value.get("version") == 1 and type(value.get("version")) is int
            and isinstance(actual_operation_id, str)
            and re.fullmatch(r"[0-9a-f]{64}", actual_operation_id)
            and (operation_id is None or actual_operation_id == operation_id)
            and value.get("state") in {"failed", "cancelled", "expired", "interrupted"}
            and value.get("error_code") in PROVIDER_OPERATION_ERROR_CODES):
        return value
    if (set(value) == {"version", "operation_id", "state", "result", "error_code"}
            and value.get("version") == 1 and type(value.get("version")) is int
            and isinstance(actual_operation_id, str)
            and re.fullmatch(r"[0-9a-f]{64}", actual_operation_id)
            and (operation_id is None or actual_operation_id == operation_id)
            and isinstance(value.get("state"), str)
            and value.get("state") in {"failed", "cancelled", "expired", "interrupted"}
            and isinstance(value.get("error_code"), str)
            and value.get("error_code") in PROVIDER_OPERATION_ERROR_CODES):
        result = value.get("result")
        if (isinstance(result, dict)
                and set(result) == {"credential_state", "revoke_state"}
                and isinstance(result.get("credential_state"), str)
                and result.get("credential_state") in PROVIDER_RESTORED_STATES
                and result.get("revoke_state") == "skipped"):
            return value
    raise invalid


def _safe_public_auth_url(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 2048
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname) \
            and parsed.username is None and parsed.password is None
    except ValueError:
        return False


def _provider_auth_methods_response(value):
    invalid = RuntimeError("Canonical provider auth methods response is invalid")
    if not isinstance(value, dict) or set(value) != {"version", "methods"} \
            or value.get("version") != 1 or type(value.get("version")) is not int \
            or not isinstance(value.get("methods"), list):
        raise invalid
    seen = set()
    for method in value["methods"]:
        if (not isinstance(method, dict) or set(method) != {"method_id", "available"}
                or method.get("method_id") not in PROVIDER_AUTH_METHODS
                or type(method.get("available")) is not bool
                or method["method_id"] in seen):
            raise invalid
        seen.add(method["method_id"])
    return value


def _wait_provider_operation(client, connection_id, initial):
    latest = _provider_operation_response(initial)
    if latest["state"] not in {"pending", "finalizing"}:
        return latest
    operation_id = latest["operation_id"]
    deadline = time.monotonic() + DISCONNECT_WAIT_SECONDS
    try:
        while latest["state"] in {"pending", "finalizing"}:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(latest["poll_interval"], remaining))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            response = client.call(
                "GET", f"/connections/{connection_id}/auth/operations/{operation_id}",
                authenticated=True, request_timeout=remaining)
            if deadline - time.monotonic() <= 0:
                break
            latest = _provider_operation_response(response, operation_id=operation_id)
    except KeyboardInterrupt:
        pass
    return latest


def _timeout_seconds(value):
    if (not isinstance(value, str) or not value.isascii() or not value.isdigit()
            or not 1 <= int(value) <= 3600):
        raise RuntimeError("Timeout must be an integer from 1 to 3600 seconds")
    return int(value)


def _wait_exact_request(client, room, request_id, timeout):
    deadline = time.monotonic() + timeout
    latest = None
    try:
        while True:
            latest = client.call(
                "GET", f"/conversations/{room}/requests/{request_id}", authenticated=True)
            completion = latest.get("completion") if isinstance(latest, dict) else None
            state = completion.get("state") if isinstance(completion, dict) else None
            if state not in {"queued", "running"}:
                if state not in {"waiting_approval", "interrupted", "completed", "cancelled", "unknown"}:
                    raise RuntimeError("Canonical request completion response is invalid")
                return latest, None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return latest, "timed_out"
            time.sleep(min(.5, remaining))
    except KeyboardInterrupt:
        return latest or {"request": {"event_id": request_id}}, "detached"



def _valid_restore_diagnostic(detail, correlation):
    return valid_auth_diagnostic(detail, correlation, "auth_restore")


def _valid_remember_diagnostic(detail, correlation):
    return valid_auth_diagnostic(detail, correlation, "auth_remember")


def _valid_provider_diagnostic(detail, correlation):
    required = {"correlation_id", "stage", "code", "reason", "error_class"}
    optional = {"operation", "os_status"}
    if not isinstance(detail, dict) or not required.issubset(detail) or set(detail) - required - optional:
        return False
    if (detail["correlation_id"] != correlation
            or not isinstance(correlation, str) or not re.fullmatch(r"[0-9a-f]{32}", correlation)
            or detail["stage"] != "provider_credential"
            or detail["code"] != "secret_store_unavailable"
            or detail["reason"] not in PROVIDER_DIAGNOSTIC_REASONS
            or detail["error_class"] not in PROVIDER_DIAGNOSTIC_CLASSES):
        return False
    operation = detail.get("operation")
    os_status = detail.get("os_status")
    if detail["error_class"] in {"native_os_status", "interaction_required"}:
        return (operation in PROVIDER_DIAGNOSTIC_OPERATIONS
                and type(os_status) is int and -(2 ** 31) <= os_status < 2 ** 31
                and set(detail) == required | optional)
    return operation is None and os_status is None and set(detail) == required


def diagnostics(correlation=None, client=None):
    if correlation is not None and (len(correlation) != 32 or any(char not in "0123456789abcdef" for char in correlation)):
        raise RuntimeError("Invalid correlation id")
    if correlation and client:
        try:
            detail = client.call("GET", f"/auth/restore/diagnostics/{correlation}")
            if _valid_restore_diagnostic(detail, correlation):
                compact(detail)
                return
        except RuntimeError:
            pass
        try:
            detail = client.call("GET", f"/auth/remember/diagnostics/{correlation}")
            if _valid_remember_diagnostic(detail, correlation):
                compact(detail)
                return
        except RuntimeError:
            pass
        try:
            detail = client.call(
                "GET", f"/connections/diagnostics/{correlation}", authenticated=True)
            if _valid_provider_diagnostic(detail, correlation):
                compact(detail)
                return
        except RuntimeError:
            pass
    app_data = getattr(client, "app_data", None) or os.environ.get("GAMJA_APP_DATA_DIR")
    database = (Path(app_data) / "gamja_bot.db" if app_data
                else desktop_diagnostics.DEFAULT_DATABASE)
    output = {
        "observed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "app": desktop_diagnostics.app_status(desktop_diagnostics.DEFAULT_APP),
        "database_snapshot": desktop_diagnostics.database_status(database),
        "native_screen_recording_now": "unobservable_from_external_process",
        "error_details": desktop_diagnostics.error_details(database, 1, correlation),
    }
    compact(output)


def command(client: Client, words: list[str], *, input_body=None):
    """Thin adapters: JSON bodies are validated by the canonical business API."""
    if not words:
        return
    words, options, body = _command_input(words, input_body)
    if not words:
        raise RuntimeError("A command is required")
    name, *args = words
    name = {"signup": "register", "whoami": "me"}.get(name, name)
    room_index_options = {"--status", "--project", "--query", "--limit", "--cursor"}
    if {"--status", "--project", "--query"} & options.keys() and name != "rooms":
        raise RuntimeError("Room index filters are supported only by rooms")
    agent_workload = name == "agent" and args[:1] == ["workload"]
    provider_history = name == "provider" and args[:1] == ["test-history"]
    room_results = name == "room" and args[:1] == ["results"]
    room_memory = name == "room" and args[:1] == ["memory"]
    bot_memory = name == "bot" and args[:1] == ["memory"]
    bot_message_retry = name == "bot" and args[:2] == ["message", "retry"]
    if ({"--limit", "--cursor"} & options.keys()
            and name not in {"rooms", "attention"} and not agent_workload and not provider_history and not room_results):
        raise RuntimeError("Pagination options are not supported by this command")
    secret_options = {"--secret", "--secret-stdin"} & options.keys()
    if secret_options and not (name in {"register", "login"}
                               or name == "provider" and args[:1] in (["add"], ["set"])
                               or name == "setup" and args == ["connection-add"]):
        raise RuntimeError("Secret input is only supported for account and provider credentials")
    if "--remember-device" in options and not (name == "login" and not args):
        raise RuntimeError("Remember-device is supported only by login")
    if ("--idempotency-key" in options
            and not (name in {"send", "run"} or name == "room" and args == ["launch"]
                     or name == "setup" and args == ["apply"]
                     or name == "request" and args[:1] in (["retry"], ["finalize"])
                     or room_results or room_memory or bot_memory or bot_message_retry
                     or name == "provider" and args[:2] == ["auth", "start"])):
        raise RuntimeError("Idempotency keys are supported only for send, run, room launch, request retry, and request finalize")
    if "--timeout" in options and not (name == "run" or name == "request" and args[:1] == ["wait"]):
        raise RuntimeError("Timeout is supported only for run and request wait")
    if "--refresh" in options and not (name == "provider" and args[:1] == ["models"]):
        raise RuntimeError("Refresh is supported only by provider models")
    if "--revoke" in options and not (name == "provider" and args[:1] == ["disconnect"]):
        raise RuntimeError("Remote revoke is supported only by provider disconnect")
    workspace_export = name == "workspace" and args[:1] == ["export"]
    workspace_file = name == "workspace" and args[:1] in (["preview"], ["apply"])
    if any(key in options for key in ("--output", "--force")) and name != "room-export" and not workspace_export:
        raise RuntimeError("Output options are supported only for room-export and workspace export")
    if any(key in options for key in ("--file", "--stdin")) and name not in {"send", "continue", "summary", "run"} and not workspace_file:
        raise RuntimeError("Use --json for structured input")

    def call(method, path, payload=None, **kwargs):
        compact(client.call(method, path, payload, authenticated=True, **kwargs))

    def no_body():
        if body is not None or options:
            raise RuntimeError("This command does not accept input options")

    def request_key(*, required=False):
        key = options.get("--idempotency-key")
        if key is None and required:
            raise RuntimeError("An idempotency key is required")
        key = key or uuid4().hex
        if not key or len(key) > 200 or any(not char.isprintable() for char in key):
            raise RuntimeError("Invalid idempotency key")
        return key

    if name in {"register", "login"} and not args:
        if body is None:
            # Check before ordinary prompts, so a pipe cannot silently become a
            # password prompt via getpass's echoing fallback.
            if not sys.stdin.isatty():
                raise RuntimeError("A TTY is required; provide --json and --secret-stdin")
            body = {"email": input("Email: ").strip()}
            if name == "register":
                body["username"] = input("Username: ").strip()
        password = read_secret("Password: ", from_stdin="--secret-stdin" in options)
        payload = {**body, "password": password}
        if name == "login":
            remember_device = "--remember-device" in options
            payload.update(remember_device=remember_device,
                           preserve_remembered_login=not remember_device)
        result = client.call("POST", f"/auth/{name}", payload)
        if name == "login":
            client.token = result["access_token"]
            compact({"authenticated": True})
        else:
            compact(result, secrets=(password,))
    elif name in {"restore", "restore-unlock"} and not args:
        no_body()
        client.restore(interactive=name == "restore-unlock")
        compact({"authenticated": True})
    elif name == "logout" and not args:
        no_body()
        try:
            result = client.call("POST", "/auth/logout")
        finally:
            client.token = None
        compact(result)
    elif name == "me" and not args:
        no_body(); call("GET", "/auth/me")
    elif name == "status" and not args:
        no_body(); compact(client.call("GET", "/health"))
    elif name == "diagnostic" and len(args) in (0, 1):
        no_body(); diagnostics(args[0] if args else None, client)
    elif name == "readiness" and args == ["list"]:
        no_body(); call("GET", "/readiness")
    elif name == "setup" and args:
        operation, *rest = args
        if operation == "connection-add" and not rest and body is not None and set(options) <= {"--json", "--secret", "--secret-stdin"}:
            secret = read_secret("API key: ", from_stdin="--secret-stdin" in options)
            result = client.call("POST", "/connections", {**body, "api_key": secret}, authenticated=True)
            compact(result, secrets=(secret,))
        elif operation == "preview" and not rest and body is not None and set(options) <= {"--json"}:
            call("POST", "/starter/preview", body)
        elif operation == "apply" and not rest and body is not None and set(options) <= {"--json", "--idempotency-key"}:
            call("POST", "/starter/apply", body, idempotency_key=request_key(required=True))
        elif operation == "last-result" and not rest:
            no_body(); call("GET", "/starter/last-result")
        elif operation == "test" and len(rest) == 2 and _ids(rest[:1], 1) and valid_model_id(rest[1]):
            no_body()
            query = urlencode({'model_id': rest[1], 'profile': 'text'})
            call("GET", f"/connections/{rest[0]}/test-preview?{query}")
            call("POST", f"/connections/{rest[0]}/test?{query}", {})
        else:
            raise RuntimeError("Use setup connection-add --json FILE [hidden key], test CONNECTION MODEL, preview --json FILE, apply --json FILE --idempotency-key KEY, or last-result")
    elif (name == "request" and len(args) == 3 and args[0] == "wait"
          and _ids(args[1:2], 1) and int(args[1]) > 0 and _event_id(args[2])
          and body is None and set(options) == {"--timeout"}):
        result, wait_status = _wait_exact_request(
            client, args[1], args[2], _timeout_seconds(options["--timeout"]))
        compact({**result, "wait_status": wait_status} if wait_status else result)
    elif (name == "request" and len(args) == 3 and args[0] == "inspect"
          and _ids(args[1:2], 1) and int(args[1]) > 0 and _event_id(args[2])):
        no_body(); call("GET", f"/conversations/{args[1]}/requests/{args[2]}")
    elif (name == "request" and len(args) == 3 and args[0] == "retry-preview"
          and _ids(args[1:2], 1) and int(args[1]) > 0 and _event_id(args[2])):
        no_body(); call("GET", f"/conversations/{args[1]}/failures/{args[2]}/retry")
    elif (name == "request" and len(args) == 4 and args[0] == "retry"
          and _ids(args[1:2], 1) and int(args[1]) > 0 and _event_id(args[2])
          and re.fullmatch(r"[0-9a-f]{64}", args[3]) and body is None
          and set(options) <= {"--idempotency-key"}):
        call("POST", f"/conversations/{args[1]}/failures/{args[2]}/retry",
             {"plan_hash": args[3]}, idempotency_key=request_key())
    elif (name == "request" and len(args) == 4 and args[0] == "finalize-preview"
          and _ids((args[1], args[3]), 2) and int(args[1]) > 0 and int(args[3]) > 0
          and _event_id(args[2])):
        no_body(); call("POST", f"/conversations/{args[1]}/requests/{args[2]}/finalize/preview",
                        {"agent_id": int(args[3])})
    elif (name == "request" and len(args) == 5 and args[0] == "finalize"
          and _ids((args[1], args[3]), 2) and int(args[1]) > 0 and int(args[3]) > 0
          and _event_id(args[2]) and re.fullmatch(r"[0-9a-f]{64}", args[4])
          and body is None and set(options) == {"--idempotency-key"}):
        call("POST", f"/conversations/{args[1]}/requests/{args[2]}/finalize",
             {"agent_id": int(args[3]), "plan_hash": args[4]},
             idempotency_key=request_key(required=True))
    elif (name == "provider" and args[:2] == ["auth", "methods"] and len(args) == 3
          and _ids(args[2:], 1) and int(args[2]) > 0):
        no_body()
        result = client.call("GET", f"/connections/{args[2]}/auth/methods", authenticated=True)
        compact(_provider_auth_methods_response(result))
    elif (name == "provider" and args[:2] == ["auth", "start"] and len(args) == 4
          and _ids(args[2:3], 1) and int(args[2]) > 0
          and args[3] in {"oauth_browser", "oauth_device", "native_chain"}
          and body is None and set(options) <= {"--idempotency-key"}):
        revisions = client.call(
            "GET", f"/connections/{args[2]}/revisions", authenticated=True)
        if (not isinstance(revisions, dict)
                or set(revisions) != {"version", "config_revision", "auth_grant_revision"}
                or revisions.get("version") != 1 or type(revisions.get("version")) is not int
                or type(revisions.get("config_revision")) is not int
                or revisions["config_revision"] < 1
                or type(revisions.get("auth_grant_revision")) is not int
                or revisions["auth_grant_revision"] < 0):
            raise RuntimeError("Canonical connection revision response is invalid")
        result = client.call(
            "POST", f"/connections/{args[2]}/auth/{args[3]}/operations", revisions,
            authenticated=True, idempotency_key=request_key())
        compact(_provider_operation_response(result, allow_terminal=True))
    elif (name == "provider" and len(args) == 4 and args[0] == "auth"
          and args[1] in {"status", "cancel"} and _ids(args[2:3], 1)
          and int(args[2]) > 0 and re.fullmatch(r"[0-9a-f]{64}", args[3])):
        no_body()
        path = f"/connections/{args[2]}/auth/operations/{args[3]}"
        result = client.call(
            "GET" if args[1] == "status" else "DELETE", path, authenticated=True)
        result = _provider_operation_response(result, operation_id=args[3])
        if args[1] == "cancel" and result["state"] in {"pending", "finalizing"}:
            result = _wait_provider_operation(client, args[2], result)
        compact(result)
    elif (name in {"providers", "agents"} and not args) or (
            name == "projects" and len(args) <= 1 and (not args or args[0] in {"active", "archived", "all"})):
        no_body()
        path = {"providers": "/connections", "agents": "/agents/", "projects": "/projects/"}[name]
        if name == "projects" and args:
            path += "?" + urlencode({"status": args[0]})
        call("GET", path)
    elif name == "template" and args:
        operation, *ids = args
        if operation == "list" and _ids(ids, 1):
            no_body(); call("GET", f"/projects/{ids[0]}/room-templates")
        elif operation == "show" and _ids(ids, 2):
            no_body(); call("GET", f"/projects/{ids[0]}/room-templates/{ids[1]}")
        elif operation == "add" and _ids(ids, 1) and body is not None:
            call("POST", f"/projects/{ids[0]}/room-templates", body)
        elif operation == "set" and _ids(ids, 2) and body is not None:
            call("PUT", f"/projects/{ids[0]}/room-templates/{ids[1]}", body)
        elif operation == "delete" and _ids(ids, 2):
            no_body(); call("DELETE", f"/projects/{ids[0]}/room-templates/{ids[1]}")
        elif operation == "create-room" and _ids(ids, 2):
            no_body(); call("POST", f"/projects/{ids[0]}/room-templates/{ids[1]}/rooms", {})
        else:
            raise RuntimeError("Use template list/show/add/set/delete/create-room; add/set require --json")
    elif name == "workspace" and args:
        operation, *rest = args
        if operation == "export" and not rest and body is None and set(options) <= {"--output", "--force"}:
            if "--force" in options and "--output" not in options:
                raise RuntimeError("Use workspace export [--output FILE --force]")
            result = client.call("GET", "/workspace/blueprint", authenticated=True)
            if "--output" in options:
                rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
                write_private_atomic(options["--output"], rendered, force="--force" in options)
            else:
                compact(result)
        elif operation == "preview" and not rest and body is not None and set(options) <= {"--file", "--json"}:
            call("POST", "/workspace/blueprint/preview", body)
        elif (operation == "apply" and len(rest) == 1 and re.fullmatch(r"[0-9a-f]{64}", rest[0])
              and body is not None and set(options) <= {"--file", "--json"}):
            call("POST", "/workspace/blueprint/apply", {"blueprint": body, "plan_hash": rest[0]})
        else:
            raise RuntimeError("Use workspace export [--output FILE --force], preview --file FILE, or apply PLAN_HASH --file FILE")
    elif room_memory:
        operation, *ids = args[1:] if len(args) > 1 else [""]
        if not ids or not _ids(ids[:1],1) or int(ids[0])<1:
            raise RuntimeError("Use room memory list|show|add|edit|delete|legacy ROOM ...")
        base=f"/conversations/{ids[0]}/memories"
        if operation=='list' and len(ids)==1:
            no_body();call('GET',base)
        elif operation=='show' and len(ids)==2:
            no_body();call('GET',base+'/'+ids[1])
        elif operation in {'add','edit','delete','legacy'}:
            if len(ids)!=(1 if operation=='add' else 2) or body is None or set(options)-{'--json','--idempotency-key'}:
                raise RuntimeError("Use --json and an explicit --idempotency-key for memory changes")
            path=base if operation=='add' else base+('/legacy/' if operation=='legacy' else '/')+ids[1]
            method={'add':'POST','edit':'PATCH','delete':'DELETE','legacy':'POST'}[operation]
            call(method,path,body,idempotency_key=request_key(required=True))
        else:raise RuntimeError("Invalid room memory command")
    elif room_results:
        def result_id(value):
            return bool(re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value))
        operation, *ids = args[1:] if len(args) > 1 else [""]
        if not ids or not _ids(ids[:1], 1) or int(ids[0]) < 1:
            raise RuntimeError("Use room results list|show|save|versions|version|state|review|reviews|link-report ROOM ...")
        base = f"/conversations/{ids[0]}/results"
        if operation in {"list", "versions", "reviews"}:
            count = {"list": 1, "versions": 2, "reviews": 3}[operation]
            if (len(ids) != count or body is not None or set(options) - {"--limit", "--cursor"}
                    or count > 1 and not result_id(ids[1])
                    or count == 3 and (not _ids(ids[2:], 1) or int(ids[2]) < 1)):
                raise RuntimeError("Invalid room results list arguments")
            path = base if count == 1 else f"{base}/{ids[1]}/versions" + (f"/{ids[2]}/reviews" if count == 3 else "")
            params = {}
            if "--limit" in options:
                n = options["--limit"]
                if not n.isascii() or not n.isdigit() or not 1 <= int(n) <= 50:
                    raise RuntimeError("Invalid result limit")
                params["limit"] = n
            if "--cursor" in options:
                cursor = options["--cursor"]
                if operation == "versions":
                    if not cursor.isascii() or not cursor.isdigit() or int(cursor) < 1:
                        raise RuntimeError("Invalid version cursor")
                    params["before"] = cursor
                else:
                    if not result_id(cursor):
                        raise RuntimeError("Invalid result cursor")
                    params["after"] = cursor
            call("GET", path + ("?" + urlencode(params) if params else ""))
        elif operation == "show" and len(ids) == 3 and result_id(ids[1]) and _ids(ids[2:], 1) and int(ids[2]) > 0:
            no_body(); call("GET", f"{base}/{ids[1]}/versions/{ids[2]}")
        elif operation in {"save", "version", "state", "review", "link-report"}:
            count = 1 if operation == "save" else 2 if operation == "version" else 3
            if (len(ids) != count or body is None or set(options) - {"--json", "--idempotency-key"}
                    or count > 1 and not result_id(ids[1])
                    or count == 3 and (not _ids(ids[2:], 1) or int(ids[2]) < 1)):
                raise RuntimeError("Use --json and an explicit --idempotency-key for result changes")
            path = base if count == 1 else f"{base}/{ids[1]}/versions" + (
                f"/{ids[2]}/" + ("reviews" if operation == "review" else operation) if count == 3 else "")
            call("POST", path, body, idempotency_key=request_key(required=True))
        else:
            raise RuntimeError("Invalid room results command")
    elif name == "provider" and args[:1] in (["features"],["features-check"]):
        if len(args) not in (2,3) or not _ids(args[1:2],1) or options or body is not None:
            raise RuntimeError("Use provider features|features-check ID [MODEL]")
        connection_id=args[1]
        model_id=args[2] if len(args)==3 else client.call("GET",f"/connections/{connection_id}",authenticated=True)["default_model_id"]
        if args[0]=="features":call("GET",f"/connections/{connection_id}/features?"+urlencode({"model_id":model_id}))
        else:call("POST",f"/connections/{connection_id}/features/check",{"model_id":model_id})
    elif name == "provider" and args == ["local-models"]:
        if body is None or set(options)-{"--json"}:raise RuntimeError("Use provider local-models --json FILE")
        call("POST","/connections/local/models",body)
    elif name in {"provider", "agent", "project", "room"} and args:
        operation, *ids = args
        base = {"provider": "/connections", "agent": "/agents", "project": "/projects", "room": "/conversations"}[name]
        room_list = (name == "room" and len(ids) in (1, 2) and _ids(ids[:1], 1)
                     and (len(ids) == 1 or ids[1] in {"active", "closed", "all"}))
        if (name == "agent" and operation == "workload" and len(ids) <= 1
                and (not ids or _ids(ids, 1) and int(ids[0]) > 0) and body is None
                and set(options) <= {"--limit", "--cursor"}):
            limit_value = options.get("--limit")
            cursor_value = options.get("--cursor")
            if (limit_value is not None and (not limit_value.isascii() or not limit_value.isdigit()
                                             or not 1 <= int(limit_value) <= 100)):
                raise RuntimeError("Invalid workload limit")
            if cursor_value is not None and (len(cursor_value) > 512
                                              or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor_value)):
                raise RuntimeError("Invalid workload cursor")
            params = {}
            if ids: params["agent_id"] = ids[0]
            if limit_value is not None: params["limit"] = limit_value
            if cursor_value is not None: params["cursor"] = cursor_value
            call("GET", "/agent-workloads" + ("?" + urlencode(params) if params else ""))
        elif name == "project" and operation == "archive-preview" and _ids(ids, 1):
            no_body(); call("POST", f"{base}/{ids[0]}/archive/preview", {})
        elif (name == "project" and operation == "archive" and len(ids) == 2
              and _ids(ids[:1], 1) and re.fullmatch(r"[0-9a-f]{64}", ids[1])):
            no_body(); call("POST", f"{base}/{ids[0]}/archive", {"plan_hash": ids[1]})
        elif name == "project" and operation == "restore" and _ids(ids, 1):
            no_body(); call("POST", f"{base}/{ids[0]}/restore", {})
        elif name == "room" and operation == "launch" and not ids and body is not None:
            call("POST", f"{base}/launch", body, idempotency_key=request_key())
        elif name == "room" and operation == "context-preview" and _ids(ids, 1) and body is not None:
            call("POST", f"{base}/{ids[0]}/context-preview", body)
        elif name == "room" and operation in {"pin", "unpin"} and _ids(ids, 1):
            no_body(); call("PUT" if operation == "pin" else "DELETE", f"/rooms/{ids[0]}/pin")
        elif operation == "list" and ((name != "room" and not ids) or room_list):
            no_body()
            path = (f"{base}/project/{ids[0]}" + (f"?status={ids[1]}" if len(ids) == 2 else "")
                    if name == "room" else base + ("" if name == "provider" else "/"))
            call("GET", path)
        elif operation == "catalog" and name == "provider" and not ids:
            no_body(); call("GET", f"{base}/catalog")
        elif (operation == "models" and name == "provider" and len(ids) <= 1
              and (not ids or _ids(ids, 1) and int(ids[0]) > 0)
              and body is None and set(options) <= {"--refresh"}):
            if "--refresh" in options:
                if not ids:
                    raise RuntimeError("Use provider models CONNECTION --refresh")
                revisions = client.call(
                    "GET", f"{base}/{ids[0]}/revisions", authenticated=True)
                if (not isinstance(revisions, dict)
                        or set(revisions) != {"version", "config_revision", "auth_grant_revision"}
                        or revisions.get("version") != 1
                        or type(revisions.get("config_revision")) is not int
                        or revisions["config_revision"] < 1
                        or type(revisions.get("auth_grant_revision")) is not int
                        or revisions["auth_grant_revision"] < 0):
                    raise RuntimeError("Canonical connection revision response is invalid")
                call("POST", f"{base}/{ids[0]}/models/refresh", revisions)
            elif ids:
                call("GET", f"{base}/{ids[0]}/models")
            else:
                rows = client.call("GET", base, authenticated=True)
                if (not isinstance(rows, list)
                        or any(not isinstance(row, dict)
                               or type(row.get("id")) is not int or row["id"] < 1
                               for row in rows)
                        or len({row["id"] for row in rows}) != len(rows)):
                    raise RuntimeError("Canonical connection list response is invalid")
                catalogs = []
                for row in rows:
                    result = client.call(
                        "GET", f"{base}/{row['id']}/models", authenticated=True)
                    if not isinstance(result, dict) or "connection_id" in result:
                        raise RuntimeError("Canonical model catalog response is invalid")
                    catalogs.append({"connection_id": row["id"], **result})
                compact({"connections": catalogs})
        elif (operation == "disconnect" and name == "provider" and _ids(ids, 1)
              and int(ids[0]) > 0 and body is None and set(options) <= {"--revoke"}):
            result = client.call("POST", f"{base}/{ids[0]}/disconnect", {
                "version": 1, "revoke_remote": "--revoke" in options,
            }, authenticated=True, idempotency_key=request_key())
            result = _provider_operation_response(
                result, allow_immediate=True, allow_terminal=False)
            if "operation_id" in result:
                result = _wait_provider_operation(client, ids[0], result)
            compact(result)
        elif operation in {"show", "get"} and name != "provider" and _ids(ids, 1):
            no_body(); call("GET", f"{base}/{ids[0]}")
        elif operation in {"delete", "stop", "cancel"} and _ids(ids, 1) and (operation == "delete" or name == "room"):
            no_body()
            call("DELETE" if operation == "delete" else "POST", f"{base}/{ids[0]}" + (f"/{operation}" if operation != "delete" else ""))
        elif (operation == "test-history" and name == "provider" and _ids(ids, 1) and int(ids[0]) > 0
              and body is None and set(options) <= {"--limit"}):
            limit_value = options.get("--limit")
            if limit_value is not None and (not limit_value.isascii() or not limit_value.isdigit()
                                             or not 1 <= int(limit_value) <= 20):
                raise RuntimeError("Invalid test history limit")
            suffix = "?" + urlencode({"limit": limit_value}) if limit_value is not None else ""
            call("GET", f"{base}/{ids[0]}/test-history{suffix}")
        elif operation == "test" and name == "provider" and len(ids) in (1, 2) and _ids(ids[:1], 1):
            no_body()
            if len(ids) == 2 and not valid_model_id(ids[1]):
                raise RuntimeError("Invalid model ID")
            suffix = "?" + urlencode({"model_id": ids[1]}) if len(ids) == 2 else ""
            call("POST", f"{base}/{ids[0]}/test{suffix}")
        elif name == "room" and operation in {"set", "clone"} and _ids(ids, 1) and body is not None:
            call("PATCH" if operation == "set" else "POST",
                 f"{base}/{ids[0]}" + ("/clone" if operation == "clone" else ""), body)
        elif (operation == "add" and not ids or operation == "set" and name != "room" and _ids(ids, 1)) and body is not None:
            path = base + ("" if name == "provider" else "/") if operation == "add" else f"{base}/{ids[0]}"
            method = "POST" if operation == "add" else "PATCH" if name == "provider" else "PUT"
            typed_local_none = (body.get("version") == 1
                                and body.get("auth_method_id") == "local_none")
            if name == "provider" and secret_options and typed_local_none:
                raise RuntimeError("local_none provider connections do not accept a credential")
            needs_create_secret = (operation == "add"
                                   and (not typed_local_none)
                                   and body.get("auth_required", True))
            if name == "provider" and (secret_options or needs_create_secret):
                secret = read_secret("API key: ", from_stdin="--secret-stdin" in options)
                result = client.call(method, path, {**body, "api_key": secret}, authenticated=True)
                compact(result, secrets=(secret,))
            else:
                call(method, path, body)
        else:
            raise RuntimeError("Use list/show/add/set/delete with --json for add/set; provider also supports catalog/models/disconnect/auth status/auth cancel/test/test-history; agent also supports workload; project also supports archive-preview/archive/restore; room also supports launch/clone/context-preview/pin/unpin/stop/cancel and list status")
    elif name == "project-agent" and len(args) == 3 and args[0] in {"add", "remove"} and _ids(args[1:], 2):
        no_body(); call("POST" if args[0] == "add" else "DELETE", f"/projects/{args[1]}/agents/{args[2]}")
    elif name == "rooms":
        if body is not None:
            raise RuntimeError("Room index does not accept input")
        if _ids(args, 1) and not options:
            call("GET", f"/conversations/project/{args[0]}")
        elif not args and set(options) <= room_index_options:
            status_value = options.get("--status")
            project_value = options.get("--project")
            query_value = options.get("--query")
            limit_value = options.get("--limit")
            cursor_value = options.get("--cursor")
            if status_value is not None and status_value not in {"active", "closed", "all"}:
                raise RuntimeError("Invalid room status")
            if (project_value is not None
                    and (not project_value.isascii() or not project_value.isdigit() or int(project_value) <= 0)):
                raise RuntimeError("Invalid project ID")
            if query_value is not None and (len(query_value) > 200 or any(not char.isprintable() for char in query_value)):
                raise RuntimeError("Invalid room query")
            if limit_value is not None and (not limit_value.isascii() or not limit_value.isdigit()
                                            or not 1 <= int(limit_value) <= 200):
                raise RuntimeError("Invalid room limit")
            if cursor_value is not None and (len(cursor_value) > 512
                                             or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor_value)):
                raise RuntimeError("Invalid room cursor")
            params = {}
            if status_value is not None: params["status"] = status_value
            if project_value is not None: params["project_id"] = project_value
            if query_value is not None: params["q"] = query_value
            if limit_value is not None: params["limit"] = limit_value
            if cursor_value is not None: params["cursor"] = cursor_value
            call("GET", "/rooms/index" + ("?" + urlencode(params) if params else ""))
        else:
            raise RuntimeError("Use rooms [PROJECT] or rooms [--status active|closed|all --project ID --query TITLE --limit 1..200 --cursor CURSOR]")
    elif name == "attention" and args == ["list"] and body is None and set(options) <= {"--limit", "--cursor"}:
        limit_value = options.get("--limit")
        cursor_value = options.get("--cursor")
        if limit_value is not None and (not limit_value.isascii() or not limit_value.isdigit()
                                        or not 1 <= int(limit_value) <= 200):
            raise RuntimeError("Invalid attention limit")
        if cursor_value is not None and (len(cursor_value) > 512
                                         or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor_value)):
            raise RuntimeError("Invalid attention cursor")
        params = {}
        if limit_value is not None: params["limit"] = limit_value
        if cursor_value is not None: params["cursor"] = cursor_value
        call("GET", "/attention" + ("?" + urlencode(params) if params else ""))
    elif name in {"members", "activity", "grants", "unlock", "unlock-status"} and _ids(args, 1):
        no_body()
        path = {"members": f"/conversations/{args[0]}/members", "activity": f"/tools/rooms/{args[0]}/activity",
                "grants": f"/tools/rooms/{args[0]}/grants", "unlock": f"/connections/{args[0]}/unlock",
                "unlock-status": f"/connections/{args[0]}/unlock"}[name]
        call("POST" if name == "unlock" else "GET", path)
    elif name in {"member-add", "member-remove", "member-pause", "member-resume"} and _ids(args, 2):
        no_body()
        action = name.removeprefix("member-")
        path = f"/conversations/{args[0]}/members/{args[1]}" + (f"/{action}" if action in {"pause", "resume"} else "")
        call("DELETE" if action == "remove" else "POST", path)
    elif name == "events" and len(args) in (1, 2) and _ids(args, len(args)):
        no_body()
        call("GET", f"/conversations/{args[0]}/events" + (f"?after_seq={args[1]}" if len(args) == 2 else ""))
    elif name == "room-export" and _ids(args, 1):
        if body is not None or set(options) - {"--output", "--force"} or "--force" in options and "--output" not in options:
            raise RuntimeError("Use room-export ROOM [--output FILE] [--force]")
        rendered = export_room(client, args[0], output=options.get("--output"), force="--force" in options)
        if rendered is not None:
            sys.stdout.write(rendered)
    elif name == "preview" and len(args) == 2 and _ids(args[:1], 1) and _action_id(args[1]):
        no_body(); call("GET", f"/tools/rooms/{args[0]}/actions/{args[1]}/preview")
    elif name == "decide" and len(args) in (3, 4) and _ids(args[:1], 1) and _action_id(args[1]) and args[2] in {"approve", "deny"}:
        if body is not None or set(options) - {"--review-id", "--acknowledge-unverified-context", "--send-image-to-model"}:
            raise RuntimeError("Use decide ROOM ACTION approve [once|room_session] --review-id UUID [--send-image-to-model] or decide ROOM ACTION deny")
        scope = args[3] if len(args) == 4 else "once"
        if scope not in {"once", "room_session"}:
            raise RuntimeError("Invalid approval scope: use once or room_session")
        payload = {"decision": args[2], "scope": scope}
        if args[2] == "approve":
            review_id = options.get("--review-id")
            if not _event_id(review_id) or UUID(review_id).version != 4:
                raise RuntimeError("Inspect preview, then approve with its --review-id UUID; no automatic inspection is performed")
            payload["review_id"] = review_id
            payload["acknowledge_unverified_context"] = "--acknowledge-unverified-context" in options
            if "--send-image-to-model" in options:
                payload["send_image_to_model"] = True
        elif options:
            raise RuntimeError("Deny does not require review or context options")
        call("POST", f"/tools/rooms/{args[0]}/actions/{args[1]}/decision", payload)
    elif name == "grant-revoke" and _ids(args, 2):
        no_body(); call("POST", f"/tools/rooms/{args[0]}/grants/{args[1]}/revoke")
    elif name == "approval-mode":
        no_body()
        if not args:
            call("GET", "/tools/broad-mode")
        elif args == ["manual"]:
            call("PUT", "/tools/broad-mode", {"enabled": False})
        elif args == ["broad", "--always"]:
            call("PUT", "/tools/broad-mode", {"enabled": True, "always": True})
        elif len(args) == 3 and args[:2] == ["broad", "--minutes"] and _ids(args[2:], 1) and 1 <= int(args[2]) <= 60:
            call("PUT", "/tools/broad-mode", {"enabled": True, "expires_minutes": int(args[2]), "always": False})
        else:
            raise RuntimeError("Use approval-mode [manual|broad --minutes 1..60|broad --always]")
    elif name == "tool-lease" and not args:
        no_body(); call("GET", "/tools/desktop/lease")
    elif bot_memory:
        if args[1:2] == ['policy']:
            if len(args)!=4 or args[2] not in ('get','set') or not _ids(args[3:],1):
                raise RuntimeError('Use bot memory policy get|set AGENT_ID')
            path=f'/bots/{args[3]}/memory-policy'
            if args[2]=='get':no_body();call('GET',path)
            elif isinstance(body,dict):call('PUT',path,body)
            else:raise RuntimeError('Use --json with expected_revision and mode')
            return
        if args[1:2] == ['common']:
            if len(args)<3 or args[2] not in ('list','add','edit','delete'):
                raise RuntimeError('Use bot memory common list|add|edit|delete')
            op=args[2];ids=args[3:];path='/bots/memory/common'
            if op=='list' and not ids:no_body();call('GET',path);return
            if op in ('edit','delete'):
                if len(ids)!=1 or not _event_id(ids[0]):raise RuntimeError('Use a canonical fact UUID')
                path+='/'+ids[0]
            elif ids:raise RuntimeError('Unexpected fact ID')
            if not isinstance(body,dict):raise RuntimeError('Use --json and an idempotency key')
            call({'add':'POST','edit':'PATCH','delete':'DELETE'}[op],path,body,idempotency_key=request_key(required=True));return
        if args[1:2] == ['visibility']:
            if len(args)!=4 or not _ids(args[2:3],1) or not _event_id(args[3]) or not isinstance(body,dict):
                raise RuntimeError('Use bot memory visibility AGENT_ID MEMORY_ID --json and an idempotency key')
            call('PATCH',f'/bots/{args[2]}/memories/{args[3]}/visibility',body,idempotency_key=request_key(required=True));return
        operation, *ids = args[1:] if len(args) > 1 else [""]
        if not ids or not _ids(ids[:1], 1) or int(ids[0]) < 1:
            raise RuntimeError("Use bot memory list|show|add|edit|delete AGENT_ID ...")
        base = f"/bots/{ids[0]}/memories"
        if operation == "list" and len(ids) == 1:
            no_body(); call("GET", base)
        elif operation in {"show", "edit", "delete"} and (len(ids) != 2 or not re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", ids[1])):
            raise RuntimeError("Use one canonical memory UUID")
        elif operation == "show":
            no_body(); call("GET", base + "/" + ids[1])
        elif operation in {"add", "edit", "delete"}:
            if (len(ids) != (1 if operation == "add" else 2) or not isinstance(body, dict)
                    or set(options) - {"--json", "--idempotency-key"}):
                raise RuntimeError("Use --json and an explicit --idempotency-key for Bot memory changes")
            path = base if operation == "add" else base + "/" + ids[1]
            method = {"add": "POST", "edit": "PATCH", "delete": "DELETE"}[operation]
            call(method, path, body, idempotency_key=request_key(required=True))
        else:
            raise RuntimeError("Use bot memory list|show|add|edit|delete AGENT_ID ...")
    elif name == "bot":
        if len(args) == 2 and args[0] == 'creations' and _ids(args[1:], 1):
            no_body(); call('GET',f'/bots/conversations/{args[1]}/creations')
        elif len(args) == 2 and args[0] == 'creation' and re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',args[1]):
            no_body(); call('GET',f'/bots/creations/{args[1]}')
        elif len(args) == 2 and args[0] == 'batches' and _ids(args[1:], 1):
            no_body(); call('GET',f'/bots/conversations/{args[1]}/batches')
        elif len(args) == 2 and args[0] == 'scope-stops' and _ids(args[1:], 1):
            no_body(); call('GET', f'/bots/conversations/{args[1]}/scope-stops')
        elif len(args) == 3 and args[:2] in (['scope-stop', 'show'], ['scope-stop', 'confirm']):
            if not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', args[2]):
                raise RuntimeError('Use a canonical scope stop receipt UUID')
            no_body(); call('GET' if args[1] == 'show' else 'POST', f'/bots/scope-stops/{args[2]}' + ('/confirm' if args[1] == 'confirm' else ''))
        elif len(args) == 2 and args[0] == "messages" and _ids(args[1:], 1):
            no_body(); call("GET", f"/bots/conversations/{args[1]}/messages")
        elif len(args) == 3 and args[:2] in (["message", "actions"], ["message", "retry-preview"], ["message", "retry"]):
            if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", args[2]):
                raise RuntimeError("Use a canonical Bot message UUID")
            if args[1] == "actions":
                no_body(); call("GET", f"/bots/messages/{args[2]}/actions")
            elif args[1] == "retry-preview":
                no_body(); call("GET", f"/bots/messages/{args[2]}/retry")
            elif not isinstance(body, dict) or set(options) - {"--json", "--idempotency-key"}:
                raise RuntimeError("Use --json with expected_updated_at and --idempotency-key")
            else:
                call("POST", f"/bots/messages/{args[2]}/retry", body, idempotency_key=request_key(required=True))
        elif args == ["primary", "check-ins"]:
            no_body(); call("GET", "/bots/primary/check-ins")
        elif args == ["primary", "check-ins", "set"] and isinstance(body, dict):
            call("PUT", "/bots/primary/check-ins", body)
        elif args == ["primary", "check-ins", "runs"]:
            no_body(); call("GET", "/bots/primary/check-ins/runs")
        elif args == ["primary"]:
            no_body(); call("GET", "/bots/primary")
        elif args == ["primary", "set"] and isinstance(body, dict):
            call("PUT", "/bots/primary", body)
        elif len(args) == 2 and args[0] == "dm" and _ids(args[1:], 1):
            if body is not None and not isinstance(body, dict):
                raise RuntimeError("Use --json with a resolve object")
            call("POST", f"/bots/{args[1]}/conversation", body or {})
        elif len(args) == 2 and args[0] == "conversation" and _ids(args[1:], 1):
            no_body(); call("GET", f"/bots/conversations/{args[1]}")
        else:
            raise RuntimeError("Use bot primary; bot primary set --json FILE (agent_id/null, expected_revision); bot dm AGENT_ID [--json FILE]; bot conversation ROOM_ID")
    elif name == "teammates" and _ids(args, 1):
        no_body(); call("GET", f"/conversations/{args[0]}/teammates")
    elif name == "room-settings" and len(args) == 2 and args[0] in {"get", "set"} and _ids(args[1:], 1):
        if args[0] == "get":
            no_body(); call("GET", f"/conversations/{args[1]}/schedule")
        elif body is not None:
            call("PUT", f"/conversations/{args[1]}/schedule", body)
        else:
            raise RuntimeError("Use --json with all three schedule limits")
    elif name == "summary" and args[:1] == ["add"] and len(args) in (2, 4) and _ids(args[1:], len(args) - 1) and body is not None:
        if len(args) == 4:
            body = {**body, "source_start_seq": int(args[2]), "source_end_seq": int(args[3])}
        call("POST", f"/conversations/{args[1]}/summaries", body)
    elif (name == "run" and _ids(args, 1) and body is not None
          and set(options) == {"--stdin", "--idempotency-key", "--timeout"}):
        result = client.call("POST", f"/conversations/{args[0]}/events", body,
                             authenticated=True, idempotency_key=request_key(required=True))
        request_id = result.get("event_id") if isinstance(result, dict) else None
        if not _event_id(request_id):
            raise RuntimeError("Canonical send response did not include a request UUID")
        compact({"request_uuid": request_id})
        sys.stdout.flush()
        result, wait_status = _wait_exact_request(
            client, args[0], request_id, _timeout_seconds(options["--timeout"]))
        compact({**result, "wait_status": wait_status} if wait_status else result)
    elif name in {"send", "continue"} and args and _ids(args[:1], 1):
        if body is not None and len(args) != 1:
            raise RuntimeError("Choose text arguments or input, not both")
        if body is None:
            if name == "send" and len(args) == 1:
                raise RuntimeError("Provide message text or --json/--file/--stdin")
            body = {"content": " ".join(args[1:])} if len(args) > 1 else {}
        if name == "send":
            call("POST", f"/conversations/{args[0]}/events", body, idempotency_key=request_key())
        else:
            call("POST", f"/conversations/{args[0]}/continue", body)
    elif name == "wait" and _ids(args, 2):
        no_body()
        room, cursor = args[0], int(args[1])
        for _ in range(120):
            batch = client.call("GET", f"/conversations/{room}/events?after_seq={cursor}", authenticated=True)
            activity = client.call("GET", f"/tools/rooms/{room}/activity", authenticated=True)
            if batch or activity:
                compact({"events": batch, "activity": activity})
            if any(item.get("state") == "waiting" for item in activity):
                return
            if batch:
                cursor = max(cursor, *(event.get("seq", cursor) for event in batch))
                if any(event.get("kind") in {"failure", "message"} and event.get("actor_type") == "agent" for event in batch):
                    return
            time.sleep(.5)
        raise RuntimeError("Timed out waiting; the turn may still be running")
    else:
        raise RuntimeError("Unknown command or arguments; see --help and the CLI guide")


def batch(client, source):
    """Preflight stdin ownership for all rows before executing any commands."""
    try:
        handle = sys.stdin if source == "-" else Path(source).open(encoding="utf-8")
        rows = []
        secret_stdin = False
        bulk_stdin_count = 0
        try:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                if (not isinstance(row, dict) or set(row) - {"command", "input"}
                        or not isinstance(row.get("command"), list) or not row["command"]
                        or not all(isinstance(word, str) for word in row["command"])):
                    raise RuntimeError("Batch rows require command string arrays and optional input objects")
                _, options = _command_options(row["command"], row.get("input"))
                secret_stdin |= "--secret-stdin" in options
                bulk_stdin_count += int("--stdin" in options or options.get("--json") == "-"
                                        or options.get("--file") == "-")
                rows.append(row)
        finally:
            if handle is not sys.stdin:
                handle.close()
    except (OSError, UnicodeError, ValueError):
        raise RuntimeError("Cannot read batch; expected UTF-8 NDJSON command rows") from None

    if source == "-" and (secret_stdin or bulk_stdin_count):
        raise RuntimeError("A stdin batch cannot share its input stream")
    if secret_stdin and bulk_stdin_count:
        raise RuntimeError("A batch cannot mix secret stdin with public stdin input")
    if bulk_stdin_count > 1:
        raise RuntimeError("A batch can contain only one public stdin reader")
    for row in rows:
        command(client, row["command"], input_body=row.get("input"))


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        # argparse normally echoes rejected arguments, which may contain a key
        # entered by mistake even though no credential argv option is supported.
        self.exit(2, json.dumps({"error": "Invalid arguments; see --help"}) + "\n")


def main():
    parser = SafeArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--capability-file", type=Path, default=DEFAULT_CAPABILITY)
    parser.add_argument("--headless-binary", type=Path)
    parser.add_argument("--batch", metavar="FILE", help="Run NDJSON commands in one authenticated process; - reads stdin")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Account, setup, provider, agent, project, template, room, messaging and approval commands; see docs/desktop-cli-guide.md")
    args = parser.parse_args()
    try:
        if args.command and args.command[0] == "serve" and len(args.command) == 1:
            serve(args.capability_file.parent, args.headless_binary)
            return
        client = Client(capability(args.capability_file))
        client.app_data = args.capability_file.parent
        if args.batch:
            if args.command:
                raise RuntimeError("Choose --batch or a command")
            batch(client, args.batch)
            return
        if args.command:
            command(client, args.command)
            return
        while True:
            try:
                line = input("gamja> ")
            except EOFError:
                return
            if line.strip() in {"exit", "quit"}:
                return
            try:
                command(client, shlex.split(line))
            except ValueError:
                print("Invalid shell quoting", file=sys.stderr)
            except RuntimeError as error:
                print(str(error), file=sys.stderr)
    except RestoreFailure as error:
        print(json.dumps({"error": {
            "version": 1, "phase": "auth_restore", "code": error.code,
            "correlation_id": error.correlation_id,
        }}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
    except RememberFailure as error:
        print(json.dumps({"error": {
            "version": 1, "phase": "auth_remember", "code": error.code,
            "correlation_id": error.correlation_id,
        }}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
    except RestoreHttpFailure as error:
        print(json.dumps({"error": {
            "version": 1, "phase": "auth_restore", "http_status": error.status,
        }}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
    except RuntimeError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
