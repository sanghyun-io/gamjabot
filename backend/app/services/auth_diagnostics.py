"""Bounded, content-free auth history in the Core's private data directory.

Core owns a single backend for this directory. The shared mutex also serializes
separate store instances within that backend; atomic replacement protects readers.
Storage failure never changes the authentication outcome.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
from threading import RLock
from uuid import uuid4

from app.services.remembered_login import REMEMBER_ERROR_CLASSES, REMEMBER_REASONS


RESTORE_REASONS = frozenset({
    "keychain_operation_in_progress", "keychain_interaction_required",
    "keychain_interaction_state_read_failed", "keychain_interaction_disable_failed",
    "keychain_interaction_restore_failed", "keychain_no_ui_guard_failed",
    "keychain_interaction_state_uncertain", "keychain_query_failed",
    "keychain_query_binding_failed", "keychain_store_unavailable",
    "keychain_backend_unsupported", "keychain_backend_unavailable",
    "credential_reentry_required",
})
_OPERATIONS = frozenset({"sec_item_add", "sec_item_update", "sec_item_copy_matching", "sec_item_delete"})
_STAGES = frozenset({"auth_remember", "auth_restore"})
_MUTEX = RLock()
_NAME = "auth-diagnostics.json"
_LIMIT = 65536


def valid_auth_diagnostic(value, correlation, stage):
    if (type(value) is not dict or type(correlation) is not str
            or not re.fullmatch(r"[0-9a-f]{32}", correlation)
            or type(stage) is not str or stage not in _STAGES):
        return False
    required = {"correlation_id", "stage", "code", "reason", "error_class"}
    optional = {"os_status"}
    if stage == "auth_remember":
        required.add("cleanup")
        optional.add("operation")
    if not required <= value.keys() or value.keys() - required - optional:
        return False
    if any(type(value[key]) is not str for key in required):
        return False
    if value["correlation_id"] != correlation or value["stage"] != stage:
        return False
    if value["code"] not in {"credential_locked", "secret_store_unavailable"}:
        return False
    if stage == "auth_remember":
        if (value["reason"] not in REMEMBER_REASONS
                or value["error_class"] not in REMEMBER_ERROR_CLASSES
                or value["cleanup"] not in {"success", "failed", "unknown"}):
            return False
    else:
        classes = ({"secret_interaction_required", "credential_reentry_required"}
                   if value["code"] == "credential_locked" else {"secret_store_unavailable"})
        if value["reason"] not in RESTORE_REASONS or value["error_class"] not in classes:
            return False
    if "operation" in value and (type(value["operation"]) is not str or value["operation"] not in _OPERATIONS):
        return False
    return ("os_status" not in value or
            (type(value["os_status"]) is int and -(2 ** 31) <= value["os_status"] < 2 ** 31))


def _private(info, directory=False):
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        return False
    if getattr(info, "st_file_attributes", 0) & 0x400:  # Windows reparse point
        return False
    if not directory and info.st_nlink != 1:
        return False
    if os.name == "posix":
        return info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600)
    # Windows inherits the directory ACL established and verified by Core.
    return True


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("diagnostic_record_invalid")
        result[key] = value
    return result


def _reject_constant(_):
    raise ValueError("diagnostic_record_invalid")


def _binding(info):
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size


class AuthDiagnosticStore:
    def __init__(self, root=None):
        self.root = Path(root) if isinstance(root, (str, Path)) else None
        if self.root is not None and (not self.root.is_absolute() or ".." in self.root.parts
                or len(str(self.root)) > 4096 or any(ord(c) < 32 for c in str(self.root))):
            self.root = None

    @contextmanager
    def _directory(self):
        if self.root is None:
            raise ValueError("diagnostic_storage_unavailable")
        if os.name == "posix":
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                if not _private(os.fstat(fd), directory=True):
                    raise ValueError("diagnostic_storage_unavailable")
                yield fd
            finally:
                os.close(fd)
        else:
            if not _private(self.root.lstat(), directory=True):
                raise ValueError("diagnostic_storage_unavailable")
            yield None

    def _open(self, name, flags, directory):
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        if directory is not None:
            return os.open(name, flags, 0o600, dir_fd=directory)
        if flags & os.O_CREAT and flags & os.O_EXCL:
            # Return ownership immediately after exclusive creation so the
            # caller can clean up even if subsequent descriptor checks fail.
            return os.open(self.root / name, flags, 0o600)
        # Windows has no O_NOFOLLOW: validate the path and the opened binding
        # before returning a descriptor that can read any bytes.
        path = self.root / name
        try:
            before = path.lstat()
        except FileNotFoundError:
            before = None
        if before is not None and not _private(before):
            raise ValueError("diagnostic_storage_unavailable")
        fd = os.open(path, flags, 0o600)
        try:
            opened, current = os.fstat(fd), path.lstat()
            if (not _private(opened) or not _private(current)
                    or _binding(opened) != _binding(current)
                    or (before is not None and _binding(before) != _binding(opened))):
                raise ValueError("diagnostic_storage_unavailable")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _current_binding(self, directory):
        try:
            info = (os.stat(_NAME, dir_fd=directory, follow_symlinks=False) if directory is not None
                    else (self.root / _NAME).lstat())
        except FileNotFoundError:
            return None
        if not _private(info) or info.st_size > _LIMIT:
            raise ValueError("diagnostic_storage_unavailable")
        return _binding(info)

    def _read(self, directory):
        try:
            fd = self._open(_NAME, os.O_RDONLY, directory)
        except FileNotFoundError:
            return [], None
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not _private(info) or info.st_size > _LIMIT:
                raise ValueError("diagnostic_storage_unavailable")
            raw = stream.read(_LIMIT + 1)
            if len(raw) > _LIMIT or _binding(os.fstat(stream.fileno())) != _binding(info):
                raise ValueError("diagnostic_storage_unavailable")
        value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_reject_constant)
        if (type(value) is not dict or set(value) != {"version", "records"}
                or type(value["version"]) is not int or value["version"] != 1
                or type(value["records"]) is not list or len(value["records"]) > 40):
            raise ValueError("diagnostic_record_invalid")
        records = value["records"]
        seen = set()
        for record in records:
            if not valid_auth_diagnostic(record, record.get("correlation_id") if type(record) is dict else None,
                                        record.get("stage") if type(record) is dict else None):
                raise ValueError("diagnostic_record_invalid")
            key = record["stage"], record["correlation_id"]
            if key in seen:
                raise ValueError("diagnostic_record_invalid")
            seen.add(key)
        if any(sum(record["stage"] == stage for record in records) > 20 for stage in _STAGES):
            raise ValueError("diagnostic_record_invalid")
        return records, _binding(info)

    def get(self, correlation, stage):
        if type(correlation) is not str or not re.fullmatch(r"[0-9a-f]{32}", correlation) or type(stage) is not str or stage not in _STAGES:
            return None
        try:
            with _MUTEX, self._directory() as directory:
                records, _ = self._read(directory)
                return next((dict(r) for r in records if r["correlation_id"] == correlation and r["stage"] == stage), None)
        except (OSError, ValueError, TypeError):
            return None

    def put(self, record):
        if not valid_auth_diagnostic(record, record.get("correlation_id") if type(record) is dict else None,
                                     record.get("stage") if type(record) is dict else None):
            return False
        try:
            with _MUTEX, self._directory() as directory:
                records, old = self._read(directory)
                key = record["stage"], record["correlation_id"]
                records = [r for r in records if (r["stage"], r["correlation_id"]) != key] + [dict(record)]
                keep = set()
                for stage in _STAGES:
                    keep.update((r["stage"], r["correlation_id"]) for r in [r for r in records if r["stage"] == stage][-20:])
                records = [r for r in records if (r["stage"], r["correlation_id"]) in keep]
                payload = json.dumps({"version": 1, "records": records}, separators=(",", ":"), allow_nan=False).encode()
                if len(payload) > _LIMIT:
                    return False
                temporary = ".auth-diagnostics-" + uuid4().hex + ".tmp"
                created = False
                try:
                    fd = self._open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, directory)
                    created = True
                    with os.fdopen(fd, "wb") as stream:
                        if not _private(os.fstat(stream.fileno())):
                            raise ValueError("diagnostic_storage_unavailable")
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if self._current_binding(directory) != old:
                        return False
                    if directory is not None:
                        os.replace(temporary, _NAME, src_dir_fd=directory, dst_dir_fd=directory)
                        os.fsync(directory)
                    else:
                        if not _private(self.root.lstat(), directory=True):
                            return False
                        os.replace(self.root / temporary, self.root / _NAME)
                    return True
                finally:
                    if created:
                        try:
                            if directory is not None:
                                os.unlink(temporary, dir_fd=directory)
                            else:
                                os.unlink(self.root / temporary)
                        except FileNotFoundError:
                            pass
        except (OSError, ValueError, TypeError):
            return False


def make_auth_report_diagnostic(record):
    """Convert an auth diagnostic record to a ReportDiagnostic format for the reporting pipe.
    
    Returns None if the record is invalid. The returned dict has:
    - version: 1
    - correlation_id: from record
    - occurred_at: UTC now
    - stage: from record (auth_remember or auth_restore)
    - failure_code: from record.code
    - reason/error_class: from record
    - room_id, agent_id, connection_id, trigger_event_id, action_id: all NULL
    - exception_chain, frames: empty
    - os_status: optional signed int32 from record
    - operation: optional fixed value from record (only for auth_remember)
    - cleanup: fixed value from record (only for auth_remember, required)
    """
    if not valid_auth_diagnostic(record, record.get("correlation_id") if type(record) is dict else None,
                                 record.get("stage") if type(record) is dict else None):
        return None
    result = {
        "version": 1,
        "correlation_id": record["correlation_id"],
        "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "stage": record["stage"],
        "failure_code": record["code"],
        "reason": record["reason"],
        "error_class": record["error_class"],
        "room_id": None,
        "agent_id": None,
        "connection_id": None,
        "trigger_event_id": None,
        "action_id": None,
        "exception_chain": [],
        "frames": [],
    }
    if "os_status" in record:
        result["os_status"] = record["os_status"]
    if "operation" in record:
        result["operation"] = record["operation"]
    if "cleanup" in record:
        result["cleanup"] = record["cleanup"]
    return result
