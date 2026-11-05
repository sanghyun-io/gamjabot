"""Pure Core cloud-provider projection; no credential or provider I/O."""

import base64
import asyncio
import json
from threading import Event

import httpx
import pytest

from app.services.connections import ConnectionConfig
from app.services.core_provider_transport import (
    CoreProviderClient,
    CoreProviderUnavailable,
    MAX_PROVIDER_FRAME_BYTES,
    build_core_request,
    provider_result_from_core,
)
from app.services.connections import PhysicalRequestBudget
from app.services.providers import adapter_for
from app.services.providers.openai import OpenAIAdapter
from app.services.providers.base import (
    ProviderFailure,
    ProviderRequest,
    ToolObservation,
    UnsupportedOption,
    VisionInput,
)


def _connection(*, vision=True, tools=True):
    model = "gemini-test"
    return ConnectionConfig(
        71, "vertex", model, None, None,
        {"declared": {}, "verified": {"model_id": model, "base_url": None,
         "tools": tools, "vision": vision, "max_tokens": True}},
        provider_id="google-vertex", protocol_id="vertex_generate_content",
        auth_method_id="adc", public_config_version=1,
        public_fields={"project_id": "potato-project", "location": "us-central1"},
        auth_grant_revision=4, credential_state="present",
    )


def _request(**changes):
    values = dict(
        connection=_connection(), model_id="gemini-test", system="System rule",
        room_id=9, agent_id=7, watermark_seq=3, trigger_event_id="u1",
        events=[
            {"seq": 1, "event_id": "u1", "actor_type": "user", "actor_id": 1,
             "actor_name_snapshot": "Owner", "kind": "message", "payload": {"content": "Do it"}},
            {"seq": 2, "event_id": "p2", "actor_type": "agent", "actor_id": 8,
             "actor_name_snapshot": "Peer", "kind": "message", "payload": {"content": "Peer fact"}},
            {"seq": 3, "event_id": "s3", "actor_type": "agent", "actor_id": 7,
             "actor_name_snapshot": "Self", "kind": "message", "payload": {"content": "Prior reply"}},
        ],
        tools=[{"name": "browser", "description": "Use the browser",
                "parameters": {"type": "object", "properties": {"url": {"type": "string"},
                                                                  "workflow_id": {"type": "string"}},
                               "required": ["url"], "additionalProperties": False}}],
        max_tokens=64,
    )
    values.update(changes)
    return ProviderRequest(**values)


class _FakeExecution:
    def __init__(self, core, payload, *, waits_for_cancel=False):
        self.core = core
        self.payload = payload
        self.waits_for_cancel = waits_for_cancel
        self.request_id = 2
        self.terminal = Event()
        if not waits_for_cancel:
            self.terminal.set()

    def result(self, timeout=None):
        assert timeout is None
        assert self.terminal.wait(2)
        self.core.events.append("terminal")
        return self.payload

    def cancel(self):
        self.core.events.append("cancel")
        self.terminal.set()


class _FakeCoreProviderClient(CoreProviderClient):
    def __init__(self, payload=None, *, waits_for_cancel=False):
        self.payload = payload or ({
            "ok": False, "error_code": "cancelled", "text": "", "tool_calls": [],
            "physical_model_request_count": 1,
        } if waits_for_cancel else {
            "ok": True, "response_id": "response-1", "model": "gemini-test",
            "text": "safe", "tool_calls": [], "physical_model_request_count": 1,
        })
        self.waits_for_cancel = waits_for_cancel
        self.bindings = []
        self.calls = []
        self.events = []

    def bind(self, binding, *, timeout=None):
        assert timeout is None
        self.events.append("bind")
        self.bindings.append(binding)
        return "ab" * 32

    def begin_execute(self, handle, call):
        assert handle == "ab" * 32
        self.events.append("execute")
        self.calls.append(call)
        return _FakeExecution(self, self.payload, waits_for_cancel=self.waits_for_cancel)

    def release(self, handle, *, timeout=None):
        assert timeout is None
        assert handle == "ab" * 32
        self.events.append("release")


class _GatedExecution:
    def __init__(self, core):
        self.core = core
        self.request_id = 2

    def result(self, timeout=None):
        assert timeout is None
        self.core.mark("result_enter")
        assert self.core.result_gate.wait(2)
        self.core.mark("terminal")
        return {"ok": False, "error_code": "cancelled", "text": "", "tool_calls": [],
                "physical_model_request_count": 1}

    def cancel(self):
        self.core.mark("cancel_enter")
        self.core.cancel_calls += 1
        assert self.core.cancel_gate.wait(2)
        self.core.mark("cancel_return")
        if self.core.cancel_sets_terminal:
            self.core.result_gate.set()


class _GatedCoreProviderClient(CoreProviderClient):
    def __init__(self, *, bind_blocked=False, begin_blocked=False,
                 result_blocked=True, cancel_blocked=False,
                 cancel_sets_terminal=True, release_blocked=False):
        self.events = []
        self.stage_events = {}
        self.bind_gate = Event()
        self.begin_gate = Event()
        self.result_gate = Event()
        self.cancel_gate = Event()
        self.release_gate = Event()
        self.cancel_sets_terminal = cancel_sets_terminal
        self.cancel_calls = 0
        if not bind_blocked:
            self.bind_gate.set()
        if not begin_blocked:
            self.begin_gate.set()
        if not result_blocked:
            self.result_gate.set()
        if not cancel_blocked:
            self.cancel_gate.set()
        if not release_blocked:
            self.release_gate.set()

    def mark(self, name):
        self.events.append(name)
        self.stage_events.setdefault(name, Event()).set()

    def bind(self, binding, *, timeout=None):
        assert timeout is None
        self.mark("bind_enter")
        assert self.bind_gate.wait(2)
        self.mark("bind_return")
        return "ab" * 32

    def begin_execute(self, handle, call):
        assert handle == "ab" * 32
        self.mark("begin_enter")
        assert self.begin_gate.wait(2)
        self.mark("begin_return")
        return _GatedExecution(self)

    def release(self, handle, *, timeout=None):
        assert timeout is None
        assert handle == "ab" * 32
        self.mark("release_enter")
        assert self.release_gate.wait(2)
        self.mark("release_return")


async def _seen(core, name):
    await asyncio.wait_for(asyncio.to_thread(
        core.stage_events.setdefault(name, Event()).wait), 2)


def _gated_request(cancel=None):
    return _request(
        connection=_native_connection(
            "google-vertex", "vertex_generate_content",
            {"project_id": "potato-project", "location": "us-central1"},
        ),
        cancel=cancel,
    )


def _gated_generate(core, cancel=None):
    return adapter_for(
        "vertex_generate_content", "google-vertex", None, core_transport=core,
    ).generate(_gated_request(cancel))


def _metered_generate(core, budget):
    return adapter_for(
        "vertex_generate_content", "google-vertex", None,
        core_transport=core, physical_request_budget=budget,
    ).generate(_gated_request())


@pytest.mark.asyncio
async def test_core_terminal_count_closes_pending_test_slot_before_projection():
    budget = PhysicalRequestBudget(2)
    await _metered_generate(_FakeCoreProviderClient(), budget)
    assert budget.actual_count == 1
    assert budget.remaining == 1


@pytest.mark.asyncio
async def test_missing_terminal_count_makes_test_unknown_and_blocks_next_dispatch():
    budget = PhysicalRequestBudget(2)
    core = _FakeCoreProviderClient(payload={
        "ok": True, "response_id": "response-1", "model": "gemini-test",
        "text": "safe", "tool_calls": [],
    })
    with pytest.raises(ProviderFailure):
        await _metered_generate(core, budget)
    assert budget.actual_count is None
    with pytest.raises(ProviderFailure):
        await _metered_generate(core, budget)
    assert core.events.count("execute") == 1
    assert core.events.count("bind") == 1


@pytest.mark.asyncio
async def test_ambiguous_begin_marks_unknown_and_blocks_a_second_core_call():
    budget = PhysicalRequestBudget(2)

    class AmbiguousBegin(_FakeCoreProviderClient):
        def begin_execute(self, handle, call):
            self.events.append("execute")
            raise CoreProviderUnavailable()

    core = AmbiguousBegin()
    with pytest.raises(ProviderFailure):
        await _metered_generate(core, budget)
    assert budget.actual_count is None
    with pytest.raises(ProviderFailure):
        await _metered_generate(core, budget)
    assert core.events.count("execute") == 1
    assert core.events.count("bind") == 1


@pytest.mark.asyncio
async def test_cancelled_execution_records_terminal_count_before_reraising_failure():
    budget = PhysicalRequestBudget(2)
    cancel = asyncio.Event()
    core = _FakeCoreProviderClient(waits_for_cancel=True)
    pending = asyncio.create_task(adapter_for(
        "vertex_generate_content", "google-vertex", None,
        core_transport=core, physical_request_budget=budget,
    ).generate(_gated_request(cancel)))
    while "execute" not in core.events:
        await asyncio.sleep(0)
    cancel.set()
    with pytest.raises(ProviderFailure) as failure:
        await pending
    assert failure.value.code == "cancelled"
    assert budget.actual_count == 1


@pytest.mark.asyncio
async def test_coroutine_cancel_during_bind_recovers_handle_then_releases_once():
    core = _GatedCoreProviderClient(bind_blocked=True)
    pending = asyncio.create_task(_gated_generate(core))
    await _seen(core, "bind_enter")
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    core.bind_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert core.events == ["bind_enter", "bind_return", "release_enter", "release_return"]


@pytest.mark.asyncio
async def test_coroutine_cancel_during_begin_recovers_execution_then_cancels_and_releases():
    core = _GatedCoreProviderClient(begin_blocked=True)
    pending = asyncio.create_task(_gated_generate(core))
    await _seen(core, "begin_enter")
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    core.begin_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert core.cancel_calls == 1
    assert core.events == [
        "bind_enter", "bind_return", "begin_enter", "begin_return", "result_enter",
        "cancel_enter", "cancel_return", "terminal", "release_enter", "release_return",
    ]


@pytest.mark.asyncio
async def test_coroutine_cancel_without_request_event_cancels_terminal_before_release():
    core = _GatedCoreProviderClient()
    pending = asyncio.create_task(_gated_generate(core))
    await _seen(core, "result_enter")
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert core.cancel_calls == 1
    assert core.events == [
        "bind_enter", "bind_return", "begin_enter", "begin_return", "result_enter",
        "cancel_enter", "cancel_return", "terminal", "release_enter", "release_return",
    ]


@pytest.mark.asyncio
async def test_request_cancel_and_repeated_coroutine_cancel_keep_cancel_single_flight():
    request_cancel = asyncio.Event()
    core = _GatedCoreProviderClient(cancel_blocked=True)
    pending = asyncio.create_task(_gated_generate(core, request_cancel))
    await _seen(core, "result_enter")
    request_cancel.set()
    await _seen(core, "cancel_enter")
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    await asyncio.sleep(0)
    assert pending.done() is False
    core.cancel_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert core.cancel_calls == 1
    assert core.events == [
        "bind_enter", "bind_return", "begin_enter", "begin_return", "result_enter",
        "cancel_enter", "cancel_return", "terminal", "release_enter", "release_return",
    ]


@pytest.mark.asyncio
async def test_repeated_cancel_while_draining_terminal_does_not_release_early():
    request_cancel = asyncio.Event()
    core = _GatedCoreProviderClient(cancel_sets_terminal=False)
    pending = asyncio.create_task(_gated_generate(core, request_cancel))
    await _seen(core, "result_enter")
    request_cancel.set()
    await _seen(core, "cancel_return")
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    await asyncio.sleep(0)
    assert pending.done() is False
    assert "release_enter" not in core.events
    core.result_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert core.cancel_calls == 1
    assert core.events[-3:] == ["terminal", "release_enter", "release_return"]


@pytest.mark.asyncio
async def test_repeated_cancel_while_releasing_waits_for_release_completion():
    core = _GatedCoreProviderClient(result_blocked=False, release_blocked=True)
    pending = asyncio.create_task(_gated_generate(core))
    await _seen(core, "release_enter")
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    await asyncio.sleep(0)
    assert pending.done() is False
    core.release_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert core.cancel_calls == 0
    assert core.events[-3:] == ["terminal", "release_enter", "release_return"]


def _native_connection(provider_id, protocol_id, public_fields):
    connection = _connection()
    connection.provider_id = provider_id
    connection.protocol_id = protocol_id
    connection.auth_method_id = "native_chain"
    connection.public_fields = public_fields
    connection.owner_id = 7
    connection.owner_epoch = "11" * 32
    connection.credential_ref = "7:71:" + "22" * 16
    connection.config_revision = 3
    connection.auth_grant_revision = 4
    connection.public_config_fingerprint = "33" * 32
    connection.descriptor_revision = 1
    return connection


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider_id", "public_protocol", "private_protocol", "public_fields", "expected_budget"), [
    ("azure-openai", "azure_openai", "azure_openai_responses_v1",
     {"resource_endpoint": "https://example.openai.azure.com", "api_version": "v1"}, 1),
    ("amazon-bedrock", "aws_bedrock_converse", "aws_bedrock_converse",
     {"profile_name": "production", "region": "us-east-1"}, 2),
    ("google-vertex", "vertex_generate_content", "vertex_generate_content_v1",
     {"project_id": "potato-project", "location": "us-central1"}, 1),
])
async def test_exact_public_d2_pair_uses_core_bounded_dto_and_releases_handle(
        provider_id, public_protocol, private_protocol, public_fields, expected_budget):
    core = _FakeCoreProviderClient()
    request = _request(connection=_native_connection(provider_id, public_protocol, public_fields))

    result = await adapter_for(
        public_protocol, provider_id, None, core_transport=core,
    ).generate(request)

    assert result.text == "safe"
    assert core.events == ["bind", "execute", "terminal", "release"]
    assert core.bindings == [{
        "credential_ref": "7:71:" + "22" * 16,
        "owner_id": 7,
        "owner_epoch": "11" * 32,
        "connection_id": 71,
        "config_revision": 3,
        "auth_grant_revision": 4,
        "provider_id": provider_id,
        "auth_method_id": "native_chain",
        "public_config_fingerprint": "33" * 32,
        "descriptor_revision": 1,
        "protocol": private_protocol,
        "model_id": "gemini-test",
        "public_fields": public_fields,
    }]
    assert core.calls[0]["descriptor_revision"] == 1
    assert core.calls[0]["protocol"] == private_protocol
    assert core.calls[0]["model_id"] == "gemini-test"
    assert core.calls[0]["public_fields"] == public_fields
    assert core.calls[0]["physical_model_request_budget"] == expected_budget
    assert core.calls[0]["request"] == build_core_request(request, descriptor_revision=1)


@pytest.mark.asyncio
async def test_azure_api_key_uses_same_core_d2_path_without_python_key_or_http():
    connection = _native_connection(
        "azure-openai", "azure_openai",
        {"resource_endpoint": "https://example.openai.azure.com", "api_version": "v1"})
    connection.auth_method_id = "api_key"
    core = _FakeCoreProviderClient()
    request = _request(connection=connection)

    result = await adapter_for(
        "azure_openai", "azure-openai", None, core_transport=core).generate(request)

    assert result.text == "safe"
    assert request.connection.api_key is None
    assert core.bindings[0]["auth_method_id"] == "api_key"
    assert core.events == ["bind", "execute", "terminal", "release"]


@pytest.mark.asyncio
async def test_d2_cancel_waits_for_terminal_before_release():
    cancel = asyncio.Event()
    core = _FakeCoreProviderClient(waits_for_cancel=True)
    request = _request(
        connection=_native_connection(
            "google-vertex", "vertex_generate_content",
            {"project_id": "potato-project", "location": "us-central1"},
        ),
        cancel=cancel,
    )
    pending = asyncio.create_task(adapter_for(
        "vertex_generate_content", "google-vertex", None, core_transport=core,
    ).generate(request))
    while "execute" not in core.events:
        await asyncio.sleep(0)
    cancel.set()

    with pytest.raises(ProviderFailure) as failure:
        await pending
    assert failure.value.code == "cancelled"
    assert core.events == ["bind", "execute", "cancel", "terminal", "release"]


def test_d1_http_transport_is_unchanged_and_private_wire_ids_are_not_public_aliases():
    http_transport = httpx.MockTransport(lambda _request: pytest.fail("no HTTP expected"))
    d1 = adapter_for("openai_chat_completions", "openai", http_transport)
    assert isinstance(d1, OpenAIAdapter)
    assert d1.transport is http_transport

    core = _FakeCoreProviderClient()
    for private_protocol, provider_id in (
        ("azure_openai_responses_v1", "azure-openai"),
        ("vertex_generate_content_v1", "google-vertex"),
    ):
        with pytest.raises(ProviderFailure) as failure:
            adapter_for(private_protocol, provider_id, http_transport, core_transport=core)
        assert failure.value.code == "invalid_configuration"


@pytest.mark.parametrize(("protocol_id", "provider_id"), [
    ("azure_openai", "azure-openai"),
    ("aws_bedrock_converse", "amazon-bedrock"),
    ("vertex_generate_content", "google-vertex"),
])
def test_d2_requires_the_private_core_client_without_http_fallback(protocol_id, provider_id):
    http_transport = httpx.MockTransport(lambda _request: pytest.fail("D2 reached HTTP"))
    with pytest.raises(ProviderFailure) as failure:
        adapter_for(protocol_id, provider_id, http_transport)
    assert failure.value.code == "provider_unavailable"


@pytest.mark.asyncio
async def test_d2_keeps_control_failure_separate_from_terminal_provider_failure():
    connection = _native_connection(
        "google-vertex", "vertex_generate_content",
        {"project_id": "potato-project", "location": "us-central1"},
    )

    class ControlFailure(_FakeCoreProviderClient):
        def bind(self, binding, *, timeout=None):
            raise CoreProviderUnavailable("configuration_changed")

    with pytest.raises(ProviderFailure) as control:
        await adapter_for(
            "vertex_generate_content", "google-vertex", None,
            core_transport=ControlFailure(),
        ).generate(_request(connection=connection))
    assert control.value.code == "configuration_changed"

    terminal_core = _FakeCoreProviderClient(payload={
        "ok": False, "error_code": "provider_error", "text": "", "tool_calls": [],
        "physical_model_request_count": 1,
    })
    with pytest.raises(ProviderFailure) as terminal:
        await adapter_for(
            "vertex_generate_content", "google-vertex", None,
            core_transport=terminal_core,
        ).generate(_request(connection=connection))
    assert terminal.value.code == "provider_error"
    assert terminal_core.events == ["bind", "execute", "terminal", "release"]


def test_projection_preserves_system_room_provenance_and_closed_tools():
    wire = build_core_request(_request(memory="Remember this"), descriptor_revision=12)
    assert wire["scope"] == {"connection_id": 71, "provider_kind": "vertex", "base_url": None,
                             "room_id": 9, "agent_id": 7,
                             "watermark_seq": 3, "descriptor_revision": 12,
                             "model_id": "gemini-test"}
    assert wire["system"] == "System rule"
    assert wire["messages"][0]["role"] == "user"
    assert "Room-local memory" in wire["messages"][0]["content"]
    assert "Peer fact" in wire["messages"][2]["content"]
    assert wire["messages"][3] == {"role": "assistant", "content": "Prior reply"}
    assert wire["tools"] == [{"family": "browser", "wire_name": "gamja_62726f77736572",
                               "description": "Use the browser",
                               "parameters": _request().tools[0]["parameters"]}]
    assert len(json.dumps(wire, ensure_ascii=False, separators=(",", ":")).encode()) <= MAX_PROVIDER_FRAME_BYTES
    assert "api_key" not in json.dumps(wire)
    intruder = {"room_id": 10, "seq": 2, "event_id": "other-room", "actor_type": "agent",
                "actor_id": 8, "actor_name_snapshot": "Other", "kind": "message",
                "payload": {"content": "must not cross rooms"}}
    events = [{**event, "room_id": 9, "seq": index * 2 + 1}
              for index, event in enumerate(_request().events)] + [intruder]
    separated = build_core_request(_request(events=events, watermark_seq=7), descriptor_revision=12)
    encoded = json.dumps(separated, ensure_ascii=False)
    assert "Peer fact" in encoded
    assert "must not cross rooms" not in encoded


def test_projection_binds_observations_and_continuation_to_exact_scope():
    prior = {
        "provider_kind": "vertex", "protocol": "vertex_generate_content_v1",
        "connection_id": 71, "model_id": "gemini-test", "base_url": None,
        "room_id": 9, "agent_id": 7, "watermark_seq": 3,
        "descriptor_revision": 12, "response_id": "response-1",
        "pending_call_ids": ["call-1"], "provider_state": {"kind": "vertex_generate_content",
            "assistant_text": "", "tool_calls": [{"call_id": "call-1",
                "name": "gamja_62726f77736572", "arguments": {"url": "https://example.test"}}]},
    }
    wire = build_core_request(_request(
        continuation=prior,
        observations=[ToolObservation("call-1", "succeeded", "done")],
    ), descriptor_revision=12)
    assert wire["observations"] == [{"call_id": "call-1", "state": "succeeded",
                                      "text": "succeeded: done", "screenshot_ref": None,
                                      "image": None}]
    assert wire["continuation"]["pending_call_ids"] == ["call-1"]

    for key, value in (("room_id", 10), ("descriptor_revision", 13), ("model_id", "other")):
        changed = dict(prior, **{key: value})
        with pytest.raises(ProviderFailure) as failure:
            build_core_request(_request(continuation=changed,
                observations=[ToolObservation("call-1", "succeeded", "done")]),
                descriptor_revision=12)
        assert failure.value.code == "invalid_continuation"


def test_vision_frame_accepts_exact_image_limit_and_rejects_overflow_or_invalid_base64():
    raw = b"x" * 10_000_000
    wire = build_core_request(_request(vision=VisionInput(raw, "image/png", "inspect")),
                              descriptor_revision=12)
    assert len(base64.b64decode(wire["vision"]["image"]["data_base64"], validate=True)) == 10_000_000
    assert len(json.dumps(wire, ensure_ascii=False, separators=(",", ":")).encode()) <= MAX_PROVIDER_FRAME_BYTES

    with pytest.raises(UnsupportedOption):
        build_core_request(_request(vision=VisionInput(raw + b"x", "image/png", "inspect")),
                           descriptor_revision=12)
    damaged = {"provider_kind": "vertex", "protocol": "vertex_generate_content_v1",
               "connection_id": 71, "model_id": "gemini-test", "base_url": None,
               "room_id": 9, "agent_id": 7, "watermark_seq": 3,
               "descriptor_revision": 12, "response_id": "response-1",
               "pending_call_ids": ["call-1"],
               "provider_state": {"kind": "vertex_generate_content", "assistant_text": "",
                  "tool_calls": [{"call_id": "call-1", "name": "gamja_62726f77736572",
                                  "arguments": {"url": "x"}, "thought_signature": "***"}]}}
    with pytest.raises(ProviderFailure):
        provider_result_from_core(_request(), {"ok": True, "text": "ok", "response_id": "response-1",
                                                "model": "gemini-test", "tool_calls": [{"call_id": "call-1",
                                                "name": "gamja_62726f77736572", "arguments": {"url": "x"}}],
                                                "continuation": damaged}, descriptor_revision=12)


def test_unverified_options_fail_before_projection_and_large_utf8_frame_is_not_truncated():
    calls = _request(connection=_connection(vision=False), vision=VisionInput(b"x", "image/png", "p"))
    with pytest.raises(UnsupportedOption):
        build_core_request(calls, descriptor_revision=12)

    huge = _request(system="감" * MAX_PROVIDER_FRAME_BYTES)
    with pytest.raises(ProviderFailure) as failure:
        build_core_request(huge, descriptor_revision=12)
    assert failure.value.code == "invalid_bounded_request"
    for invalid in (0, False, -1, 8193):
        with pytest.raises(ProviderFailure) as failure:
            build_core_request(_request(max_tokens=invalid), descriptor_revision=12)
        assert failure.value.code == "invalid_bounded_request"
    assert build_core_request(_request(max_tokens=None), descriptor_revision=12)["max_output_tokens"] == 1024


def test_closed_item_counts_accept_maximum_and_reject_one_over():
    tools = [{"name": f"t{index}", "description": "d",
              "parameters": {"type": "object", "properties": {}}}
             for index in range(64)]
    assert len(build_core_request(_request(tools=tools), descriptor_revision=12)["tools"]) == 64
    with pytest.raises(ProviderFailure) as failure:
        build_core_request(_request(tools=tools + [{"name": "overflow", "description": "d",
            "parameters": {"type": "object", "properties": {}}}]), descriptor_revision=12)
    assert failure.value.code == "invalid_bounded_request"


def test_provider_frame_distinguishes_just_below_and_over_limit():
    raw = b"x" * 10_000_000
    events = [{"room_id": 9, "seq": index + 1, "event_id": f"e{index}",
               "actor_type": "user", "actor_id": 1, "actor_name_snapshot": "Owner",
               "kind": "message", "payload": {"content": "x" * 55_000}}
              for index in range(11)]
    common = dict(events=events, watermark_seq=11, trigger_event_id=None,
                  vision=VisionInput(raw, "image/png", "inspect"))
    base = build_core_request(_request(**common), descriptor_revision=12)
    base_size = len(json.dumps(base, ensure_ascii=False, separators=(",", ":")).encode())
    probe_event = {"room_id": 9, "seq": 12, "event_id": "last", "actor_type": "user",
                   "actor_id": 1, "actor_name_snapshot": "Owner", "kind": "message",
                   "payload": {"content": "x"}}
    probe = build_core_request(_request(**{**common, "events": events + [probe_event],
                                           "watermark_seq": 12}), descriptor_revision=12)
    probe_size = len(json.dumps(probe, ensure_ascii=False, separators=(",", ":")).encode())
    overhead = probe_size - base_size - 1
    final_chars = MAX_PROVIDER_FRAME_BYTES - 1 - base_size - overhead
    assert 0 < final_chars < 65_000
    final_event = {**probe_event, "payload": {"content": "x" * final_chars}}
    wire = build_core_request(_request(**{**common, "events": events + [final_event],
                                          "watermark_seq": 12}), descriptor_revision=12)
    assert len(json.dumps(wire, ensure_ascii=False, separators=(",", ":")).encode()) == MAX_PROVIDER_FRAME_BYTES - 1
    overflow = {**probe_event, "payload": {"content": "x" * (final_chars + 2)}}
    with pytest.raises(ProviderFailure) as failure:
        build_core_request(_request(**{**common, "events": events + [overflow],
                                      "watermark_seq": 12}), descriptor_revision=12)
    assert failure.value.code == "invalid_bounded_request"


def test_core_result_reuses_full_schema_validation_and_returns_text_and_continuation():
    request = _request()
    payload = {
        "ok": True, "text": "final text", "response_id": "response-1", "model": "gemini-test",
        "input_tokens": 5, "output_tokens": 7,
        "tool_calls": [{"call_id": "call-1", "name": "gamja_62726f77736572",
                        "arguments": {"url": "https://example.test", "workflow_id": "wf-1"}}],
        "continuation": {"provider_kind": "vertex", "protocol": "vertex_generate_content_v1",
                         "connection_id": 71, "model_id": "gemini-test", "base_url": None,
                         "room_id": 9, "agent_id": 7, "watermark_seq": 3,
                         "descriptor_revision": 12, "response_id": "response-1",
                         "pending_call_ids": ["call-1"],
                         "provider_state": {"kind": "vertex_generate_content", "assistant_text": "",
                            "tool_calls": [{"call_id": "call-1", "name": "gamja_62726f77736572",
                                            "arguments": {"url": "https://example.test"}}]}},
    }
    result = provider_result_from_core(request, payload, descriptor_revision=12)
    assert result.text == "final text"
    assert result.tool_calls[0].family == "browser"
    assert result.tool_calls[0].arguments == {"url": "https://example.test"}
    assert result.tool_calls[0].workflow_id == "wf-1"
    assert result.model_use == {"response_id": "response-1", "model": "gemini-test",
                                "input_tokens": 5, "output_tokens": 7}

    bad = dict(payload, tool_calls=[{"call_id": "call-1", "name": "gamja_62726f77736572",
                                    "arguments": {"url": 5}}])
    with pytest.raises(ProviderFailure):
        provider_result_from_core(request, bad, descriptor_revision=12)
    with pytest.raises(ProviderFailure):
        provider_result_from_core(request, dict(payload, raw_chain="must-not-cross"),
                                  descriptor_revision=12)
