"""Private, pure DTO seam for Core-owned cloud provider transports."""

import asyncio
import base64
import binascii
import hmac
import json
import re
import socket
import struct
import time
from dataclasses import dataclass, field
from threading import Event, Lock, RLock, Thread, current_thread
from typing import Any

from app.services.providers.base import (
    ProviderFailure,
    ProviderRequest,
    ProviderResult,
    check_continuation,
    check_options,
    normalized_call,
    observation_text,
    projected_messages,
    wire_tool_name,
    PERSISTENCE_WIRE_NAMES,
)
from app.services.secrets import consume_inherited_socket
from app.services.mcp_schema import is_mcp_family,safe_mcp_schema


MAX_PROVIDER_FRAME_BYTES = 14_000_000
MAX_IMAGE_BYTES = 10_000_000
MAX_ITEMS = 64
MAX_TEXT_BYTES = 65_536
MAX_SCHEMA_BYTES = 65_536
MAX_RESPONSE_TEXT_BYTES = 1_000_000
MAX_TEST_PHYSICAL_REQUESTS = 7
_PROTOCOLS = {
    "azure_openai": "azure_openai_responses_v1",
    "aws_bedrock_converse": "aws_bedrock_converse",
    "vertex_generate_content": "vertex_generate_content_v1",
}
def _valid_wire_name(value):
    return isinstance(value, str) and (value in PERSISTENCE_WIRE_NAMES
        or bool(re.fullmatch(r"gamja_(?:[0-9a-f]{2}){1,29}", value)))
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_ORDINARY_REFERENCE = re.compile(
    r"[1-9][0-9]*:[1-9][0-9]*:(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\Z"
)
_COMPILED_PROTOCOLS = {
    ("azure-openai", "native_chain"): "azure_openai_responses_v1",
    ("azure-openai", "api_key"): "azure_openai_responses_v1",
    ("amazon-bedrock", "native_chain"): "aws_bedrock_converse",
    ("google-vertex", "native_chain"): "vertex_generate_content_v1",
}
_CONTROL_ERROR_CODES = frozenset((
    "operation_interrupted", "owner_epoch_unavailable", "configuration_changed",
    "credential_store_unavailable", "credential_recovery_required",
    "native_source_stale", "native_source_unsupported", "invalid_bounded_request",
    "provider_capacity_exceeded", "provider_handle_not_found",
    "provider_request_not_found", "internal_error",
))
_BINDING_KEYS = frozenset((
    "credential_ref", "owner_id", "owner_epoch", "connection_id", "config_revision",
    "auth_grant_revision", "provider_id", "auth_method_id", "public_config_fingerprint",
    "descriptor_revision", "protocol", "model_id", "public_fields",
))
_CALL_KEYS = frozenset((
    "descriptor_revision", "protocol", "model_id", "public_fields", "request",
    "physical_model_request_budget",
))
_RESULT_KEYS = frozenset((
    "ok", "error_code", "response_id", "model", "input_tokens", "output_tokens",
    "text", "tool_calls", "continuation", "physical_model_request_count",
))


def _fail(code: str = "invalid_bounded_request") -> None:
    raise ProviderFailure(code, "Core provider request is outside the bounded wire contract.")


def _compact(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        _fail()


def _bounded_text(value: Any, *, required: bool = False, maximum: int = MAX_TEXT_BYTES) -> str:
    if not isinstance(value, str) or (required and not value) or len(value.encode("utf-8")) > maximum:
        _fail()
    return value


def _no_remote_refs(value: Any) -> bool:
    if isinstance(value, dict):
        return not any(key in value for key in ("$ref", "$dynamicRef", "$recursiveRef")) and all(
            _no_remote_refs(item) for item in value.values())
    if isinstance(value, list):
        return all(_no_remote_refs(item) for item in value)
    return True


def _image(image_bytes: bytes, mime: str, width: int | None, height: int | None) -> dict:
    if (not isinstance(image_bytes, bytes) or not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES
            or mime not in ("image/png", "image/jpeg")
            or width is not None and (type(width) is not int or not 0 < width <= 100_000)
            or height is not None and (type(height) is not int or not 0 < height <= 100_000)):
        from app.services.providers.base import UnsupportedOption
        raise UnsupportedOption("vision image")
    return {"mime_type": mime, "data_base64": base64.b64encode(image_bytes).decode("ascii"),
            "width": width, "height": height}


def _scope(request: ProviderRequest, descriptor_revision: int) -> dict:
    if (type(descriptor_revision) is not int or descriptor_revision < 1
            or type(request.connection_id) is not int or request.connection_id < 1
            or type(request.room_id) is not int or request.room_id < 1
            or type(request.agent_id) is not int or request.agent_id < 1
            or type(request.watermark_seq) is not int or request.watermark_seq < 0):
        _fail()
    return {"connection_id": request.connection_id,
            "provider_kind": request.connection.provider_kind,
            "base_url": request.connection.base_url,
            "room_id": request.room_id,
            "agent_id": request.agent_id, "watermark_seq": request.watermark_seq,
            "descriptor_revision": descriptor_revision, "model_id": request.model_id}


def _validate_continuation(request: ProviderRequest, continuation: Any,
                           descriptor_revision: int) -> dict | None:
    if continuation is None:
        return None
    if not isinstance(continuation, dict):
        _fail("invalid_continuation")
    expected_keys = {"provider_kind", "protocol", "connection_id", "model_id", "base_url",
                     "room_id", "agent_id", "watermark_seq", "descriptor_revision",
                     "response_id", "pending_call_ids", "provider_state"}
    if set(continuation) != expected_keys:
        _fail("invalid_continuation")
    check_continuation(request, continuation, request.connection.provider_kind)
    expected = _scope(request, descriptor_revision)
    if (continuation.get("protocol") != _PROTOCOLS.get(request.connection.protocol_id)
            or any(continuation.get(key) != value for key, value in expected.items())
            or not isinstance(continuation.get("response_id"), str)
            or not continuation["response_id"]
            or not isinstance(continuation.get("pending_call_ids"), list)
            or len(continuation["pending_call_ids"]) > MAX_ITEMS
            or any(not isinstance(item, str) or not item or len(item.encode()) > 256
                   for item in continuation["pending_call_ids"])
            or len(set(continuation["pending_call_ids"])) != len(continuation["pending_call_ids"])
            or not isinstance(continuation.get("provider_state"), dict)):
        _fail("invalid_continuation")
    provider_state = continuation["provider_state"]
    kind = provider_state.get("kind")
    if kind == "azure_responses":
        if set(provider_state) != {"kind", "previous_response_id"} or provider_state["previous_response_id"] != continuation["response_id"]:
            _fail("invalid_continuation")
    elif kind in ("bedrock_converse", "vertex_generate_content"):
        if (set(provider_state) != {"kind", "assistant_text", "tool_calls"}
                or not isinstance(provider_state["assistant_text"], str)
                or len(provider_state["assistant_text"].encode()) > MAX_RESPONSE_TEXT_BYTES
                or not isinstance(provider_state["tool_calls"], list)
                or len(provider_state["tool_calls"]) != len(continuation["pending_call_ids"])):
            _fail("invalid_continuation")
        for index, call in enumerate(provider_state["tool_calls"]):
            if (not isinstance(call, dict)
                    or set(call) not in ({"call_id", "name", "arguments"},
                                         {"call_id", "name", "arguments", "thought_signature"})
                    or call.get("call_id") != continuation["pending_call_ids"][index]
                    or not isinstance(call.get("name"), str) or not _valid_wire_name(call["name"])
                    or not isinstance(call.get("arguments"), dict)
                    or len(_compact(call["arguments"])) > 32_000):
                _fail("invalid_continuation")
    else:
        _fail("invalid_continuation")
    _validate_encoded_fields(provider_state)
    if len(_compact(continuation)) > MAX_SCHEMA_BYTES * 4:
        _fail("invalid_continuation")
    return continuation


def _validate_encoded_fields(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("thought_signature", "data_base64"):
                if not isinstance(item, str):
                    _fail("invalid_continuation")
                try:
                    base64.b64decode(item, validate=True)
                except (ValueError, binascii.Error):
                    _fail("invalid_continuation")
            else:
                _validate_encoded_fields(item)
    elif isinstance(value, list):
        for item in value:
            _validate_encoded_fields(item)


def build_core_request(request: ProviderRequest, *, descriptor_revision: int) -> dict:
    """Project an existing ProviderRequest into Core's closed provider DTO."""
    check_options(request)
    protocol = _PROTOCOLS.get(request.connection.protocol_id)
    if protocol is None:
        raise ProviderFailure("unsupported_protocol", "This connection has no verified Core provider protocol.")
    same_room_events = [item for item in request.events
                        if (item.get("room_id") if isinstance(item, dict) else getattr(item, "room_id", None))
                        in (None, request.room_id)]
    messages = projected_messages(events=same_room_events, watermark_seq=request.watermark_seq,
                                  agent_id=request.agent_id,
                                  trigger_event_id=request.trigger_event_id,
                                  memory=request.memory, room_id=request.room_id,
                                  server_instruction=request.server_instruction)
    if not messages or len(messages) > MAX_ITEMS:
        _fail()
    closed_messages = []
    for item in messages:
        if not isinstance(item, dict) or set(item) != {"role", "content"} or item["role"] not in ("user", "assistant"):
            _fail()
        closed_messages.append({"role": item["role"], "content": _bounded_text(item["content"], required=True)})

    if len(request.tools) > MAX_ITEMS:
        _fail()
    tools = []
    for item in request.tools:
        if not isinstance(item, dict):
            _fail()
        family = item.get("name")
        name = wire_tool_name(family)
        parameters = item.get("parameters")
        mcp=is_mcp_family(family) and isinstance(item.get("mcp_binding"),dict)
        if (not isinstance(parameters, dict) or not (safe_mcp_schema(parameters) if mcp else _no_remote_refs(parameters))
                or len(_compact(parameters)) > MAX_SCHEMA_BYTES):
            _fail()
        description = item.get("description", "")
        tools.append({"family": _bounded_text(family, required=True, maximum=128),
                      "wire_name": name,
                      "description": _bounded_text(description, maximum=4096),
                      "parameters": parameters})

    continuation = _validate_continuation(request, request.continuation, descriptor_revision)
    if request.observations and continuation is None:
        _fail("invalid_continuation")
    if len(request.observations) > MAX_ITEMS:
        _fail()
    observations = []
    raw_image_bytes = 0
    for item in request.observations:
        image = None
        if item.image_bytes is not None:
            image = _image(item.image_bytes, item.image_mime, item.image_width, item.image_height)
            raw_image_bytes += len(item.image_bytes)
        observations.append({"call_id": _bounded_text(item.call_id, required=True, maximum=256),
                             "state": _bounded_text(item.state, required=True, maximum=128),
                             "text": _bounded_text(observation_text(item), maximum=MAX_TEXT_BYTES),
                             "screenshot_ref": (_bounded_text(item.screenshot_ref, maximum=4096)
                                                if item.screenshot_ref is not None else None),
                             "image": image})
    if continuation is not None:
        if [item["call_id"] for item in observations] != continuation["pending_call_ids"]:
            _fail("invalid_continuation")

    vision = None
    if request.vision is not None:
        image = _image(request.vision.image_bytes, request.vision.image_mime, None, None)
        raw_image_bytes += len(request.vision.image_bytes)
        vision = {"prompt": _bounded_text(request.vision.prompt, required=True), "image": image}
    if raw_image_bytes > MAX_IMAGE_BYTES:
        from app.services.providers.base import UnsupportedOption
        raise UnsupportedOption("vision image")

    max_output_tokens = 1024 if request.max_tokens is None else request.max_tokens
    wire = {"scope": _scope(request, descriptor_revision), "protocol": protocol,
            "system": _bounded_text(request.system), "messages": closed_messages,
            "tools": tools, "observations": observations, "vision": vision,
            "continuation": continuation, "temperature": request.temperature,
            "max_output_tokens": max_output_tokens}
    if (type(max_output_tokens) is not int or not 0 < max_output_tokens <= 8192
            or request.temperature is not None and (type(request.temperature) not in (int, float)
                                                     or not 0 <= request.temperature <= 2)
            or len(_compact(wire)) > MAX_PROVIDER_FRAME_BYTES):
        _fail()
    return wire


def provider_result_from_core(request: ProviderRequest, payload: Any, *,
                              descriptor_revision: int) -> ProviderResult:
    """Validate Core's bounded result and reuse Python's full tool-schema boundary."""
    allowed = {"ok", "error_code", "response_id", "model", "input_tokens", "output_tokens",
               "text", "tool_calls", "continuation", "physical_model_request_count"}
    if not isinstance(payload, dict) or not set(payload).issubset(allowed) or payload.get("ok") is not True:
        code = payload.get("error_code") if isinstance(payload, dict) else None
        raise ProviderFailure(code if isinstance(code, str) else "provider_error",
                              "Core provider transport failed.")
    text = _bounded_text(payload.get("text", ""), maximum=MAX_RESPONSE_TEXT_BYTES)
    calls = payload.get("tool_calls", [])
    if not isinstance(calls, list) or len(calls) > MAX_ITEMS:
        _fail("provider_invalid_response")
    normalized = []
    for item in calls:
        if not isinstance(item, dict) or set(item) != {"call_id", "name", "arguments"}:
            _fail("provider_invalid_response")
        normalized.append(normalized_call(item["call_id"], item["name"], item["arguments"], request.tools))
    continuation = _validate_continuation(request, payload.get("continuation"), descriptor_revision)
    if normalized:
        if continuation is None or [item.call_id for item in normalized] != continuation["pending_call_ids"]:
            _fail("provider_invalid_response")
    elif continuation is not None:
        _fail("provider_invalid_response")
    response_id, model = payload.get("response_id"), payload.get("model")
    if not isinstance(response_id, str) or not response_id or model != request.model_id:
        _fail("provider_invalid_response")
    usage = {}
    for key in ("input_tokens", "output_tokens"):
        value = payload.get(key)
        if value is not None and (type(value) is not int or value < 0):
            _fail("provider_invalid_response")
        usage[key] = value
    return ProviderResult(text=text, tool_calls=normalized, continuation=continuation,
                          model_use={"response_id": response_id, "model": model, **usage})


class _CancellationState:
    __slots__ = ("error",)

    def __init__(self):
        self.error: asyncio.CancelledError | None = None

    def remember(self, error: asyncio.CancelledError) -> None:
        if self.error is None:
            self.error = error

    async def finish(self, task: asyncio.Task):
        """Recover one owned task result without abandoning it on repeated cancellation."""
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError as error:
                if task.done():
                    if task.cancelled():
                        raise
                    self.remember(error)
                    return task.result()
                self.remember(error)


class CoreProviderAdapter:
    """Adapt the existing provider DTO to one exact Core-owned fixed protocol."""

    provider_id = ""
    public_protocol = ""

    def __init__(self, core_transport: "CoreProviderClient", *, physical_request_budget=None):
        if not isinstance(core_transport, CoreProviderClient):
            raise ProviderFailure("provider_unavailable", "Core provider transport is unavailable.")
        self.core_transport = core_transport
        self.physical_request_budget = physical_request_budget

    async def generate(self, request: ProviderRequest) -> ProviderResult:
        from app.services import model_usage
        key = model_usage.begin(request, 'native_operation')
        try:
            result = await self._generate_unmetered(request)
        except BaseException:
            model_usage.finish(key, state='failed_unknown')
            raise
        model_usage.finish(key, result.model_use if result else None, 'native')
        return result

    async def _generate_unmetered(self, request: ProviderRequest) -> ProviderResult:
        accounting = self.physical_request_budget
        default_budget = 2 if self.public_protocol == "aws_bedrock_converse" else 1
        if accounting is None:
            planned_budget = default_budget
        else:
            try:
                planned_budget = accounting.remaining
            except Exception:
                raise ProviderFailure(
                    "provider_unavailable", "Core provider request accounting is unavailable.") from None
        binding, call, descriptor_revision = _core_provider_frames(
            request, provider_id=self.provider_id, public_protocol=self.public_protocol,
            physical_model_request_budget=planned_budget)
        cancellation = _CancellationState()
        handle = None
        execution = None
        result = None
        primary_error = None
        slot_open = False
        try:
            bind_task = asyncio.create_task(asyncio.to_thread(self.core_transport.bind, binding))
            handle = await cancellation.finish(bind_task)
            if cancellation.error is None:
                if accounting is not None:
                    actual_budget = accounting.open_core_slot()
                    slot_open = True
                    if actual_budget != planned_budget:
                        call["physical_model_request_budget"] = actual_budget
                        planned_budget = actual_budget
                        if not _valid_call(call):
                            raise ProviderFailure(
                                "invalid_configuration", "Core provider request is invalid.")
                begin_task = asyncio.create_task(asyncio.to_thread(
                    self.core_transport.begin_execute, handle, call))
                execution = await cancellation.finish(begin_task)
            if execution is not None:
                payload = await self._await_execution(
                    execution, request.cancel, cancellation,
                    force_cancel=cancellation.error is not None)
                count = _terminal_physical_count(payload, planned_budget)
                if accounting is not None:
                    accounting.close_core_slot(count)
                    slot_open = False
                if cancellation.error is None:
                    result = provider_result_from_core(
                        request, payload, descriptor_revision=descriptor_revision)
        except CoreProviderUnavailable as error:
            if slot_open:
                if execution is None and error.reason in {
                        "provider_request_invalid", "provider_capacity_exceeded",
                        "provider_handle_not_found"}:
                    accounting.close_core_slot(0)
                else:
                    accounting.mark_unknown()
                slot_open = False
            primary_error = _provider_failure(error)
        except BaseException as error:
            if slot_open:
                accounting.mark_unknown()
                slot_open = False
            primary_error = error
        if handle is not None:
            try:
                release_task = asyncio.create_task(asyncio.to_thread(
                    self.core_transport.release, handle))
                await cancellation.finish(release_task)
            except CoreProviderUnavailable as error:
                if primary_error is None:
                    primary_error = _provider_failure(error)
        if cancellation.error is not None:
            raise cancellation.error
        if primary_error is not None:
            raise primary_error
        return result

    async def _await_execution(self, execution: "ProviderExecution",
                               cancel: asyncio.Event | None,
                               cancellation: _CancellationState, *,
                               force_cancel: bool = False) -> dict:
        result_task = asyncio.create_task(asyncio.to_thread(execution.result))
        cancel_task = None
        should_cancel = force_cancel
        try:
            if not should_cancel:
                try:
                    if cancel is None:
                        return await asyncio.shield(result_task)
                    cancel_task = asyncio.create_task(cancel.wait())
                    done, _ = await asyncio.wait(
                        (result_task, cancel_task), return_when=asyncio.FIRST_COMPLETED)
                    if result_task in done:
                        return result_task.result()
                    should_cancel = True
                except asyncio.CancelledError as error:
                    cancellation.remember(error)
                    if result_task.done():
                        return result_task.result()
                    should_cancel = True
            if should_cancel:
                core_cancel_task = asyncio.create_task(asyncio.to_thread(execution.cancel))
                cancel_failure = None
                try:
                    await cancellation.finish(core_cancel_task)
                except CoreProviderUnavailable as error:
                    cancel_failure = error
                payload = await cancellation.finish(result_task)
                if cancel_failure is not None:
                    raise cancel_failure
                return payload
        finally:
            if cancel_task is not None and not cancel_task.done():
                cancel_task.cancel()


def _terminal_physical_count(payload: Any, budget: int) -> int:
    count = payload.get("physical_model_request_count") if isinstance(payload, dict) else None
    if (type(count) is not int or count < 0 or count > budget
            or count > MAX_TEST_PHYSICAL_REQUESTS):
        raise ProviderFailure("provider_unavailable", "Core provider terminal accounting is invalid.")
    return count


def _core_provider_frames(request: ProviderRequest, *, provider_id: str,
                          public_protocol: str,
                          physical_model_request_budget: int) -> tuple[dict, dict, int]:
    config = request.connection
    descriptor_revision = getattr(config, "descriptor_revision", None)
    private_protocol = _PROTOCOLS.get(public_protocol)
    public_fields = getattr(config, "public_fields", None)
    auth_method_id = getattr(config, "auth_method_id", None)
    if (getattr(config, "provider_id", None) != provider_id
            or getattr(config, "protocol_id", None) != public_protocol
            or (provider_id, auth_method_id) not in _COMPILED_PROTOCOLS
            or _COMPILED_PROTOCOLS[(provider_id, auth_method_id)] != private_protocol
            or type(descriptor_revision) is not int or descriptor_revision < 1
            or not isinstance(public_fields, dict)):
        raise ProviderFailure("invalid_configuration", "Core provider binding is invalid.")
    binding = {
        "credential_ref": getattr(config, "credential_ref", None),
        "owner_id": getattr(config, "owner_id", None),
        "owner_epoch": getattr(config, "owner_epoch", None),
        "connection_id": request.connection_id,
        "config_revision": getattr(config, "config_revision", None),
        "auth_grant_revision": getattr(config, "auth_grant_revision", None),
        "provider_id": provider_id,
        "auth_method_id": auth_method_id,
        "public_config_fingerprint": getattr(config, "public_config_fingerprint", None),
        "descriptor_revision": descriptor_revision,
        "protocol": private_protocol,
        "model_id": request.model_id,
        "public_fields": dict(public_fields),
    }
    if not _valid_binding(binding):
        raise ProviderFailure("invalid_configuration", "Core provider binding is invalid.")
    call = {
        "descriptor_revision": descriptor_revision,
        "protocol": private_protocol,
        "model_id": request.model_id,
        "public_fields": dict(public_fields),
        "physical_model_request_budget": physical_model_request_budget,
        "request": build_core_request(request, descriptor_revision=descriptor_revision),
    }
    if not _valid_call(call):
        raise ProviderFailure("invalid_configuration", "Core provider request is invalid.")
    return binding, call, descriptor_revision


def _provider_failure(error: "CoreProviderUnavailable") -> ProviderFailure:
    code = error.reason if error.reason in _CONTROL_ERROR_CODES | {
        "provider_timeout", "provider_capacity_exceeded", "provider_handle_not_found",
        "provider_request_not_found",
    } else "provider_unavailable"
    return ProviderFailure(code, "Core provider transport failed.")


class CoreProviderUnavailable(Exception):
    """The inherited provider channel is absent, invalid, or no longer unambiguous."""

    def __init__(self, reason: str = "provider_channel_unavailable"):
        self.reason = reason
        super().__init__(reason)


@dataclass
class _PendingProviderRequest:
    request_id: int
    operation: str
    handle: str | None = None
    target_request_id: int | None = None
    expected_model: str | None = None
    expected_budget: int | None = None
    event: Event = field(default_factory=Event)
    response: Any = None
    error: CoreProviderUnavailable | None = None


@dataclass(frozen=True)
class _RetiredProviderRequest:
    handle: str
    expected_model: str
    expected_budget: int


class ProviderExecution:
    """One provider execute correlation that can be cancelled from another thread."""

    __slots__ = ("_client", "_pending")

    def __init__(self, client: "CoreProviderClient", pending: _PendingProviderRequest):
        self._client = client
        self._pending = pending

    @property
    def request_id(self) -> int:
        return self._pending.request_id

    def result(self, timeout: float | None = None) -> dict:
        return self._client._wait(self._pending, timeout=timeout)

    def cancel(self) -> None:
        self._client.cancel(self._pending.handle, self.request_id)


class CoreProviderClient:
    """Concurrent framed client for the inherited Core-owned provider channel."""

    _TIMEOUT_SECONDS = 5.0
    _MAX_IN_FLIGHT = 4
    _MAX_U64 = 2 ** 64 - 1

    def __init__(self, channel: socket.socket, *, expected_generation_fingerprint: str):
        self._channel = channel
        self._state_lock = RLock()
        self._send_lock = Lock()
        self._failed = False
        self._next_request_id = 1
        self._pending: dict[int, _PendingProviderRequest] = {}
        self._retired: dict[int, _RetiredProviderRequest] = {}
        self._handles: set[str] = set()
        self._reader: Thread | None = None
        if not _hex64(expected_generation_fingerprint):
            self._poison()
            raise CoreProviderUnavailable("provider_handshake_invalid")
        try:
            self._channel.settimeout(self._TIMEOUT_SECONDS)
            handshake = self._read_message(
                deadline=time.monotonic() + self._TIMEOUT_SECONDS)
            if (not isinstance(handshake, dict)
                    or set(handshake) != {
                        "version", "current_generation_fingerprint",
                        "max_frame_bytes", "max_in_flight",
                    }
                    or not _version_one(handshake.get("version"))
                    or not _hex64(handshake.get("current_generation_fingerprint"))
                    or type(handshake.get("max_frame_bytes")) is not int
                    or handshake["max_frame_bytes"] != MAX_PROVIDER_FRAME_BYTES
                    or type(handshake.get("max_in_flight")) is not int
                    or handshake["max_in_flight"] != self._MAX_IN_FLIGHT
                    or not hmac.compare_digest(
                        handshake["current_generation_fingerprint"],
                        expected_generation_fingerprint,
                    )):
                raise ValueError
            self._generation_fingerprint = expected_generation_fingerprint
            self._channel.settimeout(None)
        except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError,
                RecursionError, struct.error):
            self._poison()
            raise CoreProviderUnavailable("provider_handshake_invalid") from None
        self._reader = Thread(
            target=self._reader_loop,
            name="core-provider-reader",
            daemon=True,
        )
        self._reader.start()

    @classmethod
    def from_fd(cls, descriptor: int, *, expected_generation_fingerprint: str):
        try:
            channel = consume_inherited_socket(descriptor)
        except OSError:
            raise CoreProviderUnavailable("provider_channel_unavailable") from None
        try:
            return cls(
                channel,
                expected_generation_fingerprint=expected_generation_fingerprint,
            )
        except Exception:
            try:
                channel.close()
            except OSError:
                pass
            raise

    @property
    def available(self) -> bool:
        with self._state_lock:
            return not self._failed

    @property
    def current_generation_fingerprint(self) -> str:
        with self._state_lock:
            if self._failed:
                raise CoreProviderUnavailable()
            return self._generation_fingerprint

    def bind(self, binding: dict, *, timeout: float | None = None) -> str:
        if not _valid_binding(binding):
            raise CoreProviderUnavailable("provider_request_invalid")
        with self._state_lock:
            pending_binds = sum(item.operation == "bind" for item in self._pending.values())
            if len(self._handles) + pending_binds >= self._MAX_IN_FLIGHT:
                raise CoreProviderUnavailable("provider_capacity_exceeded")
        pending = self._send_request("bind", {
            "type": "provider_bind_v1",
            "current_generation_fingerprint": self._generation_fingerprint,
            "binding": binding,
        })
        return self._wait(pending, timeout=timeout)

    def begin_execute(self, handle: str, call: dict) -> ProviderExecution:
        if not _hex64(handle) or not _valid_call(call):
            raise CoreProviderUnavailable("provider_request_invalid")
        with self._state_lock:
            if self._failed:
                raise CoreProviderUnavailable()
            if handle not in self._handles:
                raise CoreProviderUnavailable("provider_handle_not_found")
            if len(self._retired) >= self._MAX_IN_FLIGHT:
                self._poison_locked()
                raise CoreProviderUnavailable()
            execute_count = sum(
                item.operation == "execute" for item in self._pending.values()
            ) + len(self._retired)
            if execute_count >= self._MAX_IN_FLIGHT:
                raise CoreProviderUnavailable("provider_capacity_exceeded")
        pending = self._send_request("execute", {
            "type": "provider_execute_v1", "handle": handle, "call": call,
        }, handle=handle, expected_model=call["model_id"],
            expected_budget=call["physical_model_request_budget"])
        return ProviderExecution(self, pending)

    def execute(self, handle: str, call: dict, *, timeout: float | None = None) -> dict:
        return self.begin_execute(handle, call).result(timeout=timeout)

    def cancel(self, handle: str | None, target_request_id: int,
               *, timeout: float | None = None) -> None:
        if not _hex64(handle) or not _positive_u64(target_request_id):
            raise CoreProviderUnavailable("provider_request_invalid")
        with self._state_lock:
            target = self._pending.get(target_request_id)
            retired = self._retired.get(target_request_id)
            if (target is not None and (target.operation != "execute" or target.handle != handle)):
                raise CoreProviderUnavailable("provider_request_not_found")
            if target is None and (retired is None or retired.handle != handle):
                raise CoreProviderUnavailable("provider_request_not_found")
        pending = self._send_request("cancel", {
            "type": "provider_cancel_v1", "handle": handle,
            "target_request_id": target_request_id,
        }, handle=handle, target_request_id=target_request_id)
        self._wait(pending, timeout=timeout)

    def release(self, handle: str, *, timeout: float | None = None) -> None:
        if not _hex64(handle):
            raise CoreProviderUnavailable("provider_request_invalid")
        with self._state_lock:
            if handle not in self._handles:
                raise CoreProviderUnavailable("provider_handle_not_found")
        pending = self._send_request("release", {
            "type": "provider_release_v1", "handle": handle,
        }, handle=handle)
        self._wait(pending, timeout=timeout)

    def close(self) -> None:
        self._poison()
        reader = self._reader
        if reader is not None and reader is not current_thread():
            reader.join(timeout=1.0)

    def _send_request(self, operation: str, body: dict, *, handle: str | None = None,
                      target_request_id: int | None = None,
                      expected_model: str | None = None,
                      expected_budget: int | None = None) -> _PendingProviderRequest:
        with self._send_lock:
            with self._state_lock:
                if self._failed:
                    raise CoreProviderUnavailable()
                if operation == "bind" and (
                        len(self._handles)
                        + sum(item.operation == "bind" for item in self._pending.values())
                        >= self._MAX_IN_FLIGHT):
                    raise CoreProviderUnavailable("provider_capacity_exceeded")
                if operation == "execute":
                    if len(self._retired) >= self._MAX_IN_FLIGHT:
                        self._poison_locked()
                        raise CoreProviderUnavailable()
                    if (sum(item.operation == "execute" for item in self._pending.values())
                            + len(self._retired) >= self._MAX_IN_FLIGHT):
                        raise CoreProviderUnavailable("provider_capacity_exceeded")
                if len(self._pending) >= self._MAX_IN_FLIGHT * 3:
                    raise CoreProviderUnavailable("provider_capacity_exceeded")
                if self._next_request_id > self._MAX_U64:
                    self._poison_locked()
                    raise CoreProviderUnavailable()
                request_id = self._next_request_id
                message = {**body, "version": 1, "request_id": request_id}
                try:
                    encoded = json.dumps(
                        message, ensure_ascii=False, allow_nan=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                except (TypeError, ValueError, UnicodeError):
                    raise CoreProviderUnavailable("provider_request_invalid") from None
                if not 0 < len(encoded) <= MAX_PROVIDER_FRAME_BYTES:
                    raise CoreProviderUnavailable("provider_request_invalid")
                pending = _PendingProviderRequest(
                    request_id=request_id,
                    operation=operation,
                    handle=handle,
                    target_request_id=target_request_id,
                    expected_model=expected_model,
                    expected_budget=expected_budget,
                )
                self._next_request_id += 1
                self._pending[request_id] = pending
            try:
                self._channel.sendall(struct.pack(">I", len(encoded)) + encoded)
            except OSError:
                self._poison()
                raise CoreProviderUnavailable() from None
        return pending

    def _wait(self, pending: _PendingProviderRequest, *, timeout: float | None):
        wait_seconds = (
            None if timeout is None and pending.operation == "execute"
            else self._TIMEOUT_SECONDS if timeout is None
            else timeout
        )
        if (wait_seconds is not None
                and (isinstance(wait_seconds, bool)
                     or not isinstance(wait_seconds, (int, float))
                     or wait_seconds < 0)):
            raise ValueError("timeout")
        if not pending.event.wait(wait_seconds):
            if pending.operation == "execute":
                self._retire_execute(pending)
                if pending.error is not None:
                    raise pending.error
                raise CoreProviderUnavailable("provider_timeout")
            self._poison()
            raise CoreProviderUnavailable()
        if pending.error is not None:
            raise pending.error
        return pending.response

    def _retire_execute(self, pending: _PendingProviderRequest) -> None:
        with self._state_lock:
            if pending.event.is_set():
                return
            current = self._pending.get(pending.request_id)
            if current is not pending:
                return
            if len(self._retired) >= self._MAX_IN_FLIGHT:
                self._poison_locked()
                return
            self._pending.pop(pending.request_id, None)
            self._retired[pending.request_id] = _RetiredProviderRequest(
                handle=pending.handle,
                expected_model=pending.expected_model,
                expected_budget=pending.expected_budget,
            )
            pending.error = CoreProviderUnavailable("provider_timeout")
            pending.event.set()

    def _reader_loop(self) -> None:
        try:
            while True:
                self._accept_response(self._read_message())
        except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError,
                RecursionError, struct.error):
            self._poison()

    def _read_message(self, *, deadline: float | None = None):
        header = self._read_exact(4, deadline=deadline)
        size = struct.unpack(">I", header)[0]
        if not 0 < size <= MAX_PROVIDER_FRAME_BYTES:
            raise ValueError
        raw = self._read_exact(size, deadline=deadline)
        return _decode_closed_json(raw)

    def _read_exact(self, size: int, *, deadline: float | None = None) -> bytes:
        result = bytearray()
        while len(result) < size:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                self._channel.settimeout(remaining)
            chunk = self._channel.recv(size - len(result))
            if not chunk:
                raise OSError("provider channel closed")
            result.extend(chunk)
        return bytes(result)

    def _accept_response(self, response) -> None:
        if (not isinstance(response, dict)
                or not _version_one(response.get("version"))
                or not _positive_u64(response.get("request_id"))):
            raise ValueError
        request_id = response["request_id"]
        with self._state_lock:
            pending = self._pending.get(request_id)
            if pending is None:
                retired = self._retired.get(request_id)
                if retired is None or not _valid_result_reply(
                    response, request_id=request_id, handle=retired.handle,
                    expected_model=retired.expected_model,
                    expected_budget=retired.expected_budget,
                ):
                    raise ValueError
                self._retired.pop(request_id, None)
                return
            control_error = _control_error(response, pending)
            if control_error is not None:
                self._pending.pop(request_id, None)
                pending.error = CoreProviderUnavailable(control_error)
                pending.event.set()
                return
            result = self._validate_success_reply(response, pending)
            self._pending.pop(request_id, None)
            pending.response = result
            pending.event.set()

    def _validate_success_reply(self, response: dict, pending: _PendingProviderRequest):
        request_id = pending.request_id
        if pending.operation == "bind":
            if (set(response) != {"type", "version", "request_id", "handle"}
                    or response.get("type") != "provider_bound_v1"
                    or not _hex64(response.get("handle"))
                    or response.get("handle") in self._handles
                    or len(self._handles) >= self._MAX_IN_FLIGHT):
                raise ValueError
            self._handles.add(response["handle"])
            return response["handle"]
        if pending.operation == "execute":
            if not _valid_result_reply(
                    response, request_id=request_id, handle=pending.handle,
                    expected_model=pending.expected_model,
                    expected_budget=pending.expected_budget):
                raise ValueError
            return response["result"]
        if pending.operation == "cancel":
            if (set(response) != {
                    "type", "version", "request_id", "handle", "target_request_id"}
                    or response.get("type") != "provider_cancelled_v1"
                    or response.get("handle") != pending.handle
                    or response.get("target_request_id") != pending.target_request_id
                    or pending.target_request_id in self._pending
                    or pending.target_request_id in self._retired):
                raise ValueError
            return None
        if pending.operation == "release":
            if (set(response) != {"type", "version", "request_id", "handle"}
                    or response.get("type") != "provider_released_v1"
                    or response.get("handle") != pending.handle
                    or any(item.operation == "execute" and item.handle == pending.handle
                           for item in self._pending.values())
                    or any(item.handle == pending.handle for item in self._retired.values())):
                raise ValueError
            self._handles.discard(pending.handle)
            return None
        raise ValueError

    def _poison(self) -> None:
        with self._state_lock:
            self._poison_locked()

    def _poison_locked(self) -> None:
        if self._failed:
            return
        self._failed = True
        error = CoreProviderUnavailable()
        for pending in self._pending.values():
            pending.error = error
            pending.event.set()
        self._pending.clear()
        self._retired.clear()
        self._handles.clear()
        try:
            self._channel.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._channel.close()
        except OSError:
            pass


def _version_one(value: Any) -> bool:
    return type(value) is int and value == 1


def _positive_u64(value: Any) -> bool:
    return type(value) is int and 0 < value <= CoreProviderClient._MAX_U64


def _hex64(value: Any) -> bool:
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def _valid_binding(binding: Any) -> bool:
    if not isinstance(binding, dict) or set(binding) != _BINDING_KEYS:
        return False
    if (not isinstance(binding.get("provider_id"), str)
            or not isinstance(binding.get("auth_method_id"), str)):
        return False
    protocol = _COMPILED_PROTOCOLS.get((binding.get("provider_id"), binding.get("auth_method_id")))
    return (
        isinstance(binding.get("credential_ref"), str)
        and _ORDINARY_REFERENCE.fullmatch(binding["credential_ref"]) is not None
        and _positive_u64(binding.get("owner_id"))
        and _hex64(binding.get("owner_epoch"))
        and _positive_u64(binding.get("connection_id"))
        and _positive_u64(binding.get("config_revision"))
        and _positive_u64(binding.get("auth_grant_revision"))
        and _hex64(binding.get("public_config_fingerprint"))
        and _version_one(binding.get("descriptor_revision"))
        and protocol is not None and binding.get("protocol") == protocol
        and _bounded_utf8(binding.get("model_id"), maximum=200, required=True)
        and isinstance(binding.get("public_fields"), dict)
    )


def _valid_call(call: Any) -> bool:
    return (
        isinstance(call, dict) and set(call) == _CALL_KEYS
        and _version_one(call.get("descriptor_revision"))
        and call.get("protocol") in _COMPILED_PROTOCOLS.values()
        and _bounded_utf8(call.get("model_id"), maximum=200, required=True)
        and isinstance(call.get("public_fields"), dict)
        and isinstance(call.get("request"), dict)
        and type(call.get("physical_model_request_budget")) is int
        and 0 <= call["physical_model_request_budget"] <= MAX_TEST_PHYSICAL_REQUESTS
    )


def _control_error(response: dict, pending: _PendingProviderRequest) -> str | None:
    if response.get("type") != "provider_error_v1":
        return None
    if (set(response) != {"type", "version", "request_id", "operation", "error_code"}
            or response.get("operation") != pending.operation
            or response.get("error_code") not in _CONTROL_ERROR_CODES):
        raise ValueError
    return response["error_code"]


def _valid_result_reply(response: Any, *, request_id: int, handle: str,
                        expected_model: str, expected_budget: int) -> bool:
    if (not isinstance(response, dict)
            or set(response) != {"type", "version", "request_id", "handle", "result"}
            or response.get("type") != "provider_result_v1"
            or not _version_one(response.get("version"))
            or response.get("request_id") != request_id
            or response.get("handle") != handle):
        return False
    result = response.get("result")
    if not isinstance(result, dict) or not set(result).issubset(_RESULT_KEYS):
        return False
    count = result.get("physical_model_request_count")
    if (type(count) is not int or count < 0 or count > expected_budget
            or count > MAX_TEST_PHYSICAL_REQUESTS):
        return False
    if result.get("ok") is False:
        return (set(result) == {"ok", "error_code", "text", "tool_calls",
                                "physical_model_request_count"}
                and isinstance(result.get("error_code"), str)
                and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", result["error_code"]) is not None
                and result.get("text") == "" and result.get("tool_calls") == [])
    if result.get("ok") is not True:
        return False
    required = {"ok", "response_id", "model", "text", "tool_calls",
                "physical_model_request_count"}
    if (not required.issubset(result)
            or "error_code" in result
            or not _bounded_utf8(result.get("response_id"), maximum=256, required=True)
            or result.get("model") != expected_model
            or not _bounded_utf8(result.get("text"), maximum=MAX_RESPONSE_TEXT_BYTES)
            or not isinstance(result.get("tool_calls"), list)
            or len(result["tool_calls"]) > MAX_ITEMS):
        return False
    for call in result["tool_calls"]:
        if (not isinstance(call, dict)
                or set(call) != {"call_id", "name", "arguments"}
                or not _bounded_utf8(call.get("call_id"), maximum=256, required=True)
                or not isinstance(call.get("name"), str)
                or not _valid_wire_name(call["name"])
                or not isinstance(call.get("arguments"), dict)):
            return False
        try:
            if len(_compact(call["arguments"])) > 32_000:
                return False
        except ProviderFailure:
            return False
    continuation = result.get("continuation")
    if continuation is not None and not isinstance(continuation, dict):
        return False
    for key in ("input_tokens", "output_tokens"):
        if key not in result:
            continue
        value = result[key]
        if type(value) is not int or value < 0 or value > 2 ** 32 - 1:
            return False
    if "continuation" in result and not isinstance(result["continuation"], dict):
        return False
    return True


def _bounded_utf8(value: Any, *, maximum: int, required: bool = False) -> bool:
    if not isinstance(value, str) or (required and not value):
        return False
    try:
        return len(value.encode("utf-8")) <= maximum
    except UnicodeError:
        return False


def _decode_closed_json(raw: bytes):
    def closed_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("invalid JSON number")

    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=closed_object,
        parse_constant=reject_constant,
    )
