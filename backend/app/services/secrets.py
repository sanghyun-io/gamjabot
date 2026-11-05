"""Only native OS credential stores are accepted; no process or file fallback."""

import json
import os
import re
import socket
import struct
import sys
import time
from ctypes import POINTER, byref, c_uint8, c_void_p
from concurrent.futures import Future, ThreadPoolExecutor
from threading import RLock


class SecretStoreUnavailable(Exception):
    OPERATIONS = frozenset({
        "sec_item_add", "sec_item_update", "sec_item_copy_matching", "sec_item_delete",
    })
    ERROR_CLASSES = frozenset({
        "native_os_status", "interaction_required", "bridge_transport",
        "bridge_protocol", "request_validation", "store_unavailable",
    })

    def __init__(self, reason: str = "keychain_store_unavailable", os_status: int | None = None,
                 *, operation: str | None = None, error_class: str = "store_unavailable"):
        self.reason = reason
        self.os_status = os_status if isinstance(os_status, int) and -(2 ** 31) <= os_status < 2 ** 31 else None
        self.operation = operation if operation in self.OPERATIONS else None
        self.error_class = error_class if error_class in self.ERROR_CLASSES else "store_unavailable"
        super().__init__(reason)


class SecretInteractionRequired(SecretStoreUnavailable):
    """A Keychain item needs user interaction, which this operation forbids."""
    def __init__(self, reason: str = "keychain_interaction_required", os_status: int | None = None,
                 *, operation: str | None = None):
        super().__init__(reason, os_status, operation=operation, error_class="interaction_required")


class SecretReentryRequired(SecretStoreUnavailable):
    """The stable credential Core has no usable value for this reference."""

    def __init__(self):
        super().__init__("credential_reentry_required", error_class="store_unavailable")


def consume_inherited_socket(descriptor: int) -> socket.socket:
    """Take ownership of one inherited private FD and prevent further inheritance."""
    if isinstance(descriptor, bool) or not isinstance(descriptor, int) or descriptor < 3:
        raise OSError("invalid inherited descriptor")
    try:
        os.set_inheritable(descriptor, False)
        return socket.socket(fileno=descriptor)
    except OSError:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


class CoreSecretStore:
    """Fixed-namespace adapter for the inherited native credential channel."""

    _NAMESPACES = frozenset(("provider", "remembered_login", "template_publisher"))
    supports_interactive_unlock = False

    def __init__(self, core, namespace: str, *, personal_agent_runtime: bool = False,
                 auth_broker=None):
        if namespace not in self._NAMESPACES:
            raise ValueError("Invalid credential namespace")
        self.core = core
        self.namespace = namespace
        self._personal_agent_runtime = namespace == "provider" and personal_agent_runtime is True
        self._personal_auth_broker = auth_broker

    @property
    def personal_agent_runtime(self) -> bool:
        from app.services.core_auth_broker import CoreAuthBrokerClient
        if (not self._personal_agent_runtime or type(self.core) is not CoreCredentialClient
                or type(self._personal_auth_broker) is not CoreAuthBrokerClient):
            return False
        with self.core._lock:
            live = not self.core._failed and self.core._channel.fileno() >= 0
        return live and self._personal_auth_broker.available

    def put(self, ref: str, value: str) -> None:
        self.core.call("put", self.namespace, ref, value)

    def get(self, ref: str) -> str | None:
        return self.core.call("get", self.namespace, ref)

    def get_no_ui(self, ref: str) -> str | None:
        return self.get(ref)

    def delete(self, ref: str) -> None:
        self.core.call("delete", self.namespace, ref)


class CoreCredentialClient:
    """Synchronous framed client over the inherited Core socket pair."""

    _MAX_FRAME = 65_536
    _TIMEOUT_SECONDS = 5.0
    _PROVIDER_REFERENCE = re.compile(
        r"[1-9][0-9]*:[1-9][0-9]*:(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\Z"
    )
    _REMEMBERED_LOGIN_REFERENCE = re.compile(r"[0-9a-f]{32}\Z")
    _TEMPLATE_PUBLISHER_REFERENCE = re.compile(r"template-publisher:[1-9][0-9]*:[1-9][0-9]*:[0-9a-f]{32}\Z")

    def __init__(self, channel: socket.socket):
        self._channel = channel
        self._channel.settimeout(self._TIMEOUT_SECONDS)
        self._lock = RLock()
        self._failed = False

    @classmethod
    def from_fd(cls, descriptor: int):
        try:
            return cls(consume_inherited_socket(descriptor))
        except OSError:
            raise SecretStoreUnavailable("credential_channel_unavailable", error_class="bridge_transport") from None

    def call(self, operation: str, namespace: str, reference: str,
             value: str | None = None) -> str | None:
        if self._failed:
            raise SecretStoreUnavailable("credential_channel_unavailable", error_class="bridge_transport")
        valid_reference = (
            namespace == "remembered_login" and isinstance(reference, str)
            and (reference == "current" or self._REMEMBERED_LOGIN_REFERENCE.fullmatch(reference) is not None)
        ) or (
            namespace == "provider" and isinstance(reference, str)
            and self._PROVIDER_REFERENCE.fullmatch(reference) is not None
        ) or (
            namespace == "template_publisher" and isinstance(reference, str)
            and self._TEMPLATE_PUBLISHER_REFERENCE.fullmatch(reference) is not None
        )
        if (operation not in ("get", "put", "delete")
                or namespace not in CoreSecretStore._NAMESPACES
                or not valid_reference
                or (operation == "put") != isinstance(value, str)
                or (value is not None and (not value or len(value.encode()) > 32_768))):
            raise SecretStoreUnavailable("credential_request_invalid", error_class="request_validation")
        request = {"version": 1, "operation": operation,
                   "namespace": namespace, "reference": reference}
        if value is not None:
            request["value"] = value
        encoded = json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode()
        if len(encoded) > self._MAX_FRAME:
            raise SecretStoreUnavailable("credential_request_invalid", error_class="request_validation")
        with self._lock:
            if self._failed:
                raise SecretStoreUnavailable("credential_channel_unavailable", error_class="bridge_transport")
            deadline = time.monotonic() + self._TIMEOUT_SECONDS
            try:
                self._channel.settimeout(max(0.001, deadline - time.monotonic()))
                self._channel.sendall(struct.pack(">I", len(encoded)) + encoded)
                size = struct.unpack(">I", self._read_exact(4, deadline))[0]
                if size > self._MAX_FRAME:
                    raise ValueError
                response = json.loads(self._read_exact(size, deadline))
            except (OSError, ValueError, TypeError, json.JSONDecodeError, struct.error):
                self._poison()
                raise SecretStoreUnavailable("credential_channel_unavailable", error_class="bridge_transport") from None
            try:
                self._channel.settimeout(self._TIMEOUT_SECONDS)
            except OSError:
                self._poison()
                raise SecretStoreUnavailable("credential_channel_unavailable", error_class="bridge_transport") from None
            if not isinstance(response, dict) or set(response) - {
                    "status", "value", "os_status", "operation", "error_class"}:
                self._reject_response()
            status = response.get("status")
            os_status = response.get("os_status")
            native_operation = response.get("operation")
            error_class = response.get("error_class")
            if os_status is not None and (isinstance(os_status, bool) or not isinstance(os_status, int)
                                          or not -(2 ** 31) <= os_status < 2 ** 31):
                self._reject_response()
            if status == "missing" and set(response) == {"status"}:
                raise SecretReentryRequired()
            native_fields = {"status", "operation", "error_class", "os_status"}
            operation_matches_request = (
                (operation == "get" and native_operation == "sec_item_copy_matching")
                or (operation == "put" and native_operation in {"sec_item_add", "sec_item_update"})
                or (operation == "delete" and native_operation == "sec_item_delete")
            )
            if (status == "interaction_required" and set(response) == native_fields
                    and operation_matches_request
                    and error_class == "interaction_required" and os_status is not None):
                raise SecretInteractionRequired(
                    "keychain_interaction_required", os_status, operation=native_operation)
            if (status == "failed" and set(response) == native_fields
                    and operation_matches_request
                    and error_class == "native_os_status" and os_status is not None):
                raise SecretStoreUnavailable(
                    "keychain_query_failed", os_status,
                    operation=native_operation, error_class="native_os_status",
                )
            if status != "ok":
                self._reject_response()
            if operation == "get":
                result = response.get("value")
                if set(response) != {"status", "value"} or not isinstance(result, str) or not result or len(result.encode()) > 32_768:
                    self._reject_response()
                return result
            if set(response) != {"status"}:
                self._reject_response()
            return None

    def _read_exact(self, size: int, deadline: float) -> bytes:
        result = bytearray()
        while len(result) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            self._channel.settimeout(remaining)
            chunk = self._channel.recv(size - len(result))
            if not chunk:
                raise OSError("credential channel closed")
            result.extend(chunk)
        return bytes(result)

    def close(self) -> None:
        with self._lock:
            self._poison()

    def _reject_response(self):
        self._poison()
        raise SecretStoreUnavailable("credential_response_invalid", error_class="bridge_protocol")

    def _poison(self) -> None:
        self._failed = True
        try:
            self._channel.close()
        except OSError:
            pass


class SessionCredentialCache:
    """Process-memory credentials and at most two owner-requested OS reads."""

    def __init__(self):
        self._lock = RLock()
        self._values: dict[tuple, str] = {}
        self._pending: dict[tuple, tuple[int, Future]] = {}
        self._generation: dict[tuple, int] = {}
        self._epoch = 0
        self._owner_epoch: dict[int, int] = {}
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gamja-secret-unlock")

    def get(self, key: tuple) -> str | None:
        with self._lock:
            return self._values.get(key)

    def put(self, key: tuple, value: str) -> None:
        with self._lock:
            self._values[key] = value

    def reserve(self, key: tuple) -> tuple[int, int, int]:
        with self._lock:
            owner_epoch = self._owner_epoch.get(key[0], 0) if key and isinstance(key[0], int) else 0
            return (self._generation.get(key, 0), self._epoch, owner_epoch)

    def put_if_current(self, key: tuple, reservation: tuple[int, int, int], value: str) -> bool:
        with self._lock:
            generation, epoch, owner_epoch = reservation
            current_owner_epoch = self._owner_epoch.get(key[0], 0) if key and isinstance(key[0], int) else 0
            if (self._generation.get(key, 0) != generation or self._epoch != epoch
                    or current_owner_epoch != owner_epoch):
                return False
            self._values[key] = value
            return True

    def start(self, key: tuple, store, ref: str) -> str:
        with self._lock:
            if key in self._values:
                return "ready"
            if key in self._pending:
                return "pending"
            if len(self._pending) >= 2:
                return "busy"
            generation = self._generation.get(key, 0) + 1
            self._generation[key] = generation
            future = self._executor.submit(store.get, ref)
            self._pending[key] = (generation, future)
            future.add_done_callback(lambda done: self._complete(key, generation, done))
            return "pending"

    def _complete(self, key: tuple, generation: int, future: Future) -> None:
        try:
            value = future.result()
        except Exception:
            value = None
        with self._lock:
            pending = self._pending.get(key)
            if pending is None or pending[0] != generation:
                return
            self._pending.pop(key, None)
            if value and self._generation.get(key) == generation:
                self._values[key] = value

    def status(self, key: tuple) -> str:
        with self._lock:
            if key in self._values:
                return "ready"
            return "pending" if key in self._pending else "locked"

    def clear(self, key: tuple) -> None:
        with self._lock:
            self._values.pop(key, None)
            self._generation[key] = self._generation.get(key, 0) + 1
            pending = self._pending.get(key)
            if pending and pending[1].cancel():
                self._pending.pop(key, None)

    def clear_owner(self, owner_id: int) -> None:
        with self._lock:
            self._owner_epoch[owner_id] = self._owner_epoch.get(owner_id, 0) + 1
            for key in set(self._values) | set(self._pending):
                if key and key[0] == owner_id:
                    self.clear(key)

    def clear_all(self) -> None:
        with self._lock:
            self._epoch += 1
            for key in set(self._values) | set(self._pending):
                self.clear(key)

    def close(self) -> None:
        self.clear_all()
        with self._lock:
            self._pending.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)


class PlatformSecretStore:
    SERVICE = "cloud.gamja.desktop.provider-connection"
    # SecKeychainSetUserInteractionAllowed is process-wide.  All access to a
    # native store therefore shares one lock, including separate subclasses
    # for remembered login and provider credentials.
    _keychain_lock = RLock()
    _legacy_interaction_uncertain = False

    def __init__(self):
        try:
            if sys.platform == "darwin":
                # The signed Rust Core is the only production Keychain caller.
                # Source interpreters and a directly launched frozen engine fail
                # closed instead of creating items under an unstable Python DR.
                raise SecretStoreUnavailable("credential_core_required")
            elif sys.platform == "win32":
                from keyring.backends.Windows import WinVaultKeyring as Keyring
            else:
                raise SecretStoreUnavailable("keychain_backend_unsupported")
            self.backend = Keyring()
        except SecretStoreUnavailable:
            raise
        except Exception:
            raise SecretStoreUnavailable("keychain_backend_unavailable") from None

    def put(self, ref: str, value: str) -> None:
        with self._keychain_lock:
            self._ensure_legacy_interaction_known()
            try:
                self.backend.set_password(self.SERVICE, ref, value)
            except Exception:
                raise SecretStoreUnavailable() from None

    def get(self, ref: str) -> str | None:
        with self._keychain_lock:
            self._ensure_legacy_interaction_known()
            try:
                return self.backend.get_password(self.SERVICE, ref)
            except Exception:
                raise SecretStoreUnavailable() from None

    def get_no_ui(self, ref: str) -> str | None:
        """Read a macOS item only when Keychain can do so without a prompt."""
        if sys.platform != "darwin":
            return self.get(ref)
        # Do not queue a background chat/startup read behind an owner-initiated
        # Keychain interaction: a queued read used to reach the five-second
        # resolver timeout even though it could never show UI.
        if not self._keychain_lock.acquire(blocking=False):
            raise SecretInteractionRequired("keychain_operation_in_progress")
        try:
            try:
                self._ensure_legacy_interaction_known()
                from keyring.backends.macOS import api
                get_allowed, set_allowed = self._legacy_interaction_functions(api)
                previous = c_uint8()
                status = get_allowed(byref(previous))
                if status != 0:
                    raise SecretStoreUnavailable("keychain_interaction_state_read_failed", status)
                disabled = False
                restore_failed = False
                restore_status = None
                result = None
                try:
                    status = set_allowed(c_uint8(0))
                    if status != 0:
                        PlatformSecretStore._legacy_interaction_uncertain = True
                        raise SecretStoreUnavailable("keychain_interaction_disable_failed", status)
                    disabled = True
                    result = self._copy_without_ui(api, ref)
                except Exception:
                    if not disabled:
                        # A call that raised while changing a process-wide
                        # setting leaves its resulting state unknowable.
                        PlatformSecretStore._legacy_interaction_uncertain = True
                    raise
                finally:
                    if disabled:
                        try:
                            restore_status = set_allowed(c_uint8(previous.value))
                            restore_failed = restore_status != 0
                        except Exception:
                            restore_failed = True
                        if restore_failed:
                            PlatformSecretStore._legacy_interaction_uncertain = True
                if restore_failed:
                    # Do not return a credential after a failed restoration of the
                    # process-wide interaction state.
                    raise SecretStoreUnavailable("keychain_interaction_restore_failed", restore_status)
                return result
            except SecretStoreUnavailable:
                raise
            except Exception:
                # Binding or Security framework failures are unavailable, never
                # an excuse to use an interactive fallback.
                raise SecretStoreUnavailable("keychain_no_ui_guard_failed") from None
        finally:
            self._keychain_lock.release()

    @classmethod
    def _ensure_legacy_interaction_known(cls) -> None:
        if sys.platform == "darwin" and cls._legacy_interaction_uncertain:
            raise SecretStoreUnavailable("keychain_interaction_state_uncertain")

    @staticmethod
    def _legacy_interaction_functions(api):
        """Bind the documented legacy UI controls with their Boolean ABI."""
        get_allowed = api._sec.SecKeychainGetUserInteractionAllowed
        set_allowed = api._sec.SecKeychainSetUserInteractionAllowed
        get_allowed.argtypes = (POINTER(c_uint8),)
        get_allowed.restype = api.OS_status
        set_allowed.argtypes = (c_uint8,)
        set_allowed.restype = api.OS_status
        return get_allowed, set_allowed

    def _copy_without_ui(self, api, ref: str) -> str | None:
        try:
            query = api.create_query(
                kSecClass=api.k_("kSecClassGenericPassword"),
                kSecMatchLimit=api.k_("kSecMatchLimitOne"),
                kSecAttrService=self.SERVICE,
                kSecAttrAccount=ref,
                kSecReturnData=True,
                kSecUseAuthenticationUI=api.k_("kSecUseAuthenticationUIFail"),
            )
            data = c_void_p()
            status = api.SecItemCopyMatching(query, byref(data))
            api.Error.raise_for_status(status)
            return api.cfstr_to_str(data)
        except api.NotFound:
            return None
        except api.Error as error:
            status = error.args[0] if error.args else None
            # With legacy optional UI disabled, Security can report access
            # denial/authentication failure rather than -25308.  They are
            # actionable as an explicit unlock; unknown statuses remain a
            # fail-closed store outage.
            if status in (api.error.sec_interaction_not_allowed,
                          api.error.keychain_denied,
                          api.error.sec_auth_failed):
                raise SecretInteractionRequired("keychain_interaction_required", status) from None
            raise SecretStoreUnavailable("keychain_query_failed", status) from None
        except Exception:
            raise SecretStoreUnavailable("keychain_query_binding_failed") from None

    def delete(self, ref: str) -> None:
        with self._keychain_lock:
            self._ensure_legacy_interaction_known()
            try:
                self.backend.delete_password(self.SERVICE, ref)
            except Exception:
                raise SecretStoreUnavailable() from None
