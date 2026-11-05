"""Provider wire contracts, exercised through fake HTTP transports only."""

import asyncio
from ipaddress import ip_address
import json
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.routes.auth import create_access_token
from app.main import create_app
from app.models.collaboration import ProviderConnection
from app.models.models import User

from app.services.connections import ConnectionConfig
from app.services.providers.base import ProviderFailure, ProviderRequest, ToolObservation, UnsupportedOption
from app.services.providers import adapter_for
from app.services.providers.opencode_go import session_header


def config(kind, connection_id=1, model_id="claude-looking-model", capabilities=None):
    return ConnectionConfig(connection_id, kind, model_id,
                            "http://127.0.0.1:8765/v1" if kind == "compatible" else None,
                            f"key-{connection_id}", capabilities or {"declared": {"temperature": True, "max_tokens": True}, "verified": {"model_id": model_id,
                            "base_url": "http://127.0.0.1:8765/v1" if kind == "compatible" else None,
                            "temperature": True, "max_tokens": True}})


def named_config(provider_id, base_url=None, *, api_key="profile-key",
                 protocol_id="openai_chat_completions", model_id="profile-model",
                 connection_id=41, owner_id=9):
    return ConnectionConfig(
        connection_id, "compatible", model_id, base_url, api_key,
        {"declared": {}, "verified": {}}, provider_id=provider_id,
        protocol_id=protocol_id,
        auth_method_id="local_none" if provider_id in {"ollama", "lm-studio"} else "api_key",
        public_config_version=1,
        public_fields=({"base_url": base_url}
                       if base_url and provider_id != "opencode-go" else {}),
        auth_grant_revision=3,
        credential_state="not_required" if provider_id in {"ollama", "lm-studio"} else "present",
        owner_id=owner_id,
    )


def request(kind, **changes):
    values = dict(connection=config(kind), model_id="claude-looking-model", system="Follow instructions",
                  agent_id=7, events=[{"seq": 1, "event_id": "u1", "actor_type": "user", "actor_id": 1,
                                       "actor_name_snapshot": "Owner", "kind": "message", "payload": {"content": "Now"}},
                                      {"seq": 2, "event_id": "a2", "actor_type": "agent", "actor_id": 8,
                                       "actor_name_snapshot": "Peer", "kind": "message", "payload": {"content": "Peer note"}},
                                      {"seq": 3, "event_id": "a3", "actor_type": "agent", "actor_id": 7,
                                       "actor_name_snapshot": "Self", "kind": "message", "payload": {"content": "Earlier"}},
                                      {"seq": 4, "event_id": "u4", "actor_type": "user", "actor_id": 1,
                                       "actor_name_snapshot": "Owner", "kind": "message", "payload": {"content": "After"}}],
                  watermark_seq=3, max_tokens=32, temperature=0.7)
    values.update(changes)
    return ProviderRequest(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "anthropic", "gemini", "xai", "compatible"])
async def test_exact_provider_model_attribution_and_frozen_watermark(kind):
    seen = []
    def handler(wire):
        body = json.loads(wire.content)
        seen.append((wire, body))
        if kind == "anthropic":
            return httpx.Response(200, json={"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"})
        if kind == "gemini":
            return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": "done"}]}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})
    result = await adapter_for(kind, httpx.MockTransport(handler)).generate(request(kind))
    assert result.text == "done" and not result.tool_calls
    wire, body = seen[0]
    assert "key-1" in str(dict(wire.headers))
    assert "After" not in json.dumps(body)
    assert json.dumps(body).count("Now") == 1
    assert "Peer" in json.dumps(body) and "Peer note" in json.dumps(body)
    assert "Earlier" in json.dumps(body)
    if kind == "gemini":
        assert "claude-looking-model" in str(wire.url)
        assert body["contents"][1]["role"] == "user"
    else:
        assert body["model"] == "claude-looking-model"
        assert body["messages"][1]["role"] == "user"
    assert wire.url.host == ("127.0.0.1" if kind == "compatible" else
                             {"openai": "api.openai.com", "anthropic": "api.anthropic.com", "gemini": "generativelanguage.googleapis.com", "xai": "api.x.ai"}[kind])


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_model_id", (
    "..", "vendor/../model", "vendor//model", "vendor/model?key=x",
    "vendor\\model", "vendor/model\n",
))
async def test_provider_boundary_rejects_malformed_legacy_model_before_transport(invalid_model_id):
    calls = []
    adapter = adapter_for("openai", httpx.MockTransport(
        lambda wire: calls.append(wire) or httpx.Response(
            200, json={"choices": [{"message": {"content": "must-not-send"}}]}),
    ))

    with pytest.raises(ProviderFailure) as invalid:
        await adapter.generate(request(
            "openai", connection=config("openai", model_id=invalid_model_id),
            model_id=invalid_model_id, temperature=None, max_tokens=None,
        ))

    assert invalid.value.code == (
        "missing_model" if invalid_model_id.endswith("\n") else "invalid_request"
    )
    assert calls == []


@pytest.mark.asyncio
async def test_valid_slash_model_is_exact_in_body_and_one_encoded_gemini_resource():
    openai_calls = []
    gemini_calls = []

    await adapter_for("openai", httpx.MockTransport(
        lambda wire: openai_calls.append(wire) or httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}),
    )).generate(request(
        "openai", connection=config("openai", model_id="publisher/model"),
        model_id="publisher/model", temperature=None, max_tokens=None,
    ))
    await adapter_for("gemini", httpx.MockTransport(
        lambda wire: gemini_calls.append(wire) or httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}),
    )).generate(request(
        "gemini", connection=config("gemini", model_id="publisher/model"),
        model_id="publisher/model", temperature=None, max_tokens=None,
    ))

    assert json.loads(openai_calls[0].content)["model"] == "publisher/model"
    assert (openai_calls[0].url.scheme, openai_calls[0].url.host) == ("https", "api.openai.com")
    assert gemini_calls[0].url.raw_path == b"/v1beta/models/publisher%2Fmodel:generateContent"
    assert (gemini_calls[0].url.scheme, gemini_calls[0].url.host) == (
        "https", "generativelanguage.googleapis.com",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "anthropic", "gemini", "xai", "compatible"])
async def test_default_agent_options_allow_basic_text_for_every_provider(kind):
    seen = []
    def handler(wire):
        seen.append(json.loads(wire.content))
        if kind == "anthropic":
            return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
        if kind == "gemini":
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    unverified = config(kind, capabilities={"declared": {}, "verified": {}})
    result = await adapter_for(kind, httpx.MockTransport(handler)).generate(
        request(kind, connection=unverified, temperature=None, max_tokens=None))
    assert result.text == "ok"
    if kind == "anthropic":
        assert seen[0]["max_tokens"] == 1024  # Messages API requires this field.
    else:
        assert "max_tokens" not in seen[0] and "generationConfig" not in seen[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id,expected_url,api_key", [
    ("openrouter", "https://openrouter.ai/api/v1/chat/completions", "profile-key"),
    ("deepseek", "https://api.deepseek.com/chat/completions", "profile-key"),
    ("groq", "https://api.groq.com/openai/v1/chat/completions", "profile-key"),
    ("moonshot-cn", "https://api.moonshot.cn/v1/chat/completions", "profile-key"),
    ("moonshot-global", "https://api.moonshot.ai/v1/chat/completions", "profile-key"),
    ("minimax-cn", "https://api.minimax.cn/v1/chat/completions", "profile-key"),
    ("minimax-global", "https://api.minimax.io/v1/chat/completions", "profile-key"),
    ("zai", "https://api.z.ai/api/paas/v4/chat/completions", "profile-key"),
    ("dashscope-cn", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", "profile-key"),
    ("dashscope-global", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions", "profile-key"),
    ("ollama", "http://localhost:11434/v1/chat/completions", None),
    ("lm-studio", "http://localhost:1234/v1/chat/completions", None),
])
async def test_named_profiles_use_fixed_chat_completions_routing(provider_id, expected_url, api_key):
    seen = []

    def handler(wire):
        seen.append(wire)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    connection = named_config(provider_id, api_key=api_key)
    result = await adapter_for(
        "openai_chat_completions", provider_id, httpx.MockTransport(handler),
    ).generate(request(
        "compatible", connection=connection, model_id="profile-model",
        temperature=None, max_tokens=None,
    ))

    assert result.text == "ok"
    if provider_id in {"ollama", "lm-studio"}:
        expected = httpx.URL(expected_url)
        assert ip_address(seen[0].url.host).is_loopback
        assert seen[0].url.port == expected.port
        assert seen[0].url.path == expected.path
    else:
        assert str(seen[0].url) == expected_url
    assert json.loads(seen[0].content)["model"] == "profile-model"
    assert seen[0].headers.get("authorization") == (
        "Bearer profile-key" if api_key else None
    )


@pytest.mark.asyncio
async def test_named_profile_adapter_refuses_protocol_mismatch_candidate_and_bad_origin():
    with pytest.raises(ProviderFailure) as mismatch:
        adapter_for("anthropic_messages", "openrouter")
    assert mismatch.value.code == "invalid_configuration"

    for provider_id in ("github-copilot", "opencode-zen"):
        with pytest.raises(ProviderFailure) as unavailable:
            adapter_for("openai_chat_completions", provider_id)
        assert unavailable.value.code == "invalid_configuration"

    # Coding Plan now has an explicit adapter, with use bound to the private Core.
    plan = named_config("alibaba-coding-plan", "https://coding-intl.dashscope.aliyuncs.com/v1")
    with pytest.raises(ProviderFailure) as unscoped:
        await adapter_for("openai_chat_completions", "alibaba-coding-plan", httpx.MockTransport(
            lambda wire: pytest.fail("unscoped plan reached transport"))).generate(request(
                "compatible", connection=plan, model_id="qwen3.7-plus",
                temperature=None, max_tokens=None))
    assert unscoped.value.code == "personal_agent_runtime_required"

    connection = named_config("dashscope-cn", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
    adapter = adapter_for("openai_chat_completions", "dashscope-cn", httpx.MockTransport(
        lambda wire: pytest.fail("invalid region origin reached the transport"),
    ))
    with pytest.raises(ProviderFailure) as invalid_origin:
        await adapter.generate(request(
            "compatible", connection=connection, model_id="profile-model",
            temperature=None, max_tokens=None,
        ))
    assert invalid_origin.value.code == "invalid_configuration"


@pytest.mark.asyncio
async def test_opencode_go_fixed_headers_session_and_exact_protocol_routing():
    seen = []

    def handler(wire):
        seen.append(wire)
        body = json.loads(wire.content)
        if wire.url.path.endswith("/messages"):
            return httpx.Response(200, json={"content": [{"type": "text", "text": "messages-ok"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "chat-ok"}}]})

    transport = httpx.MockTransport(handler)
    chat_config = named_config(
        "opencode-go", "https://opencode.ai/zen/go/v1",
        protocol_id="openai_chat_completions", model_id="glm-5.3-flash",
    )
    messages_config = named_config(
        "opencode-go", "https://opencode.ai/zen/go/v1",
        protocol_id="anthropic_messages", model_id="qwen3.7-plus",
    )
    chat_request = request(
        "compatible", connection=chat_config, model_id="glm-5.3-flash",
        room_id=51, temperature=None, max_tokens=None,
    )
    messages_request = request(
        "compatible", connection=messages_config, model_id="qwen3.7-plus",
        room_id=51, temperature=None, max_tokens=None,
    )

    assert (await adapter_for(
        "openai_chat_completions", "opencode-go", transport,
    ).generate(chat_request)).text == "chat-ok"
    assert (await adapter_for(
        "anthropic_messages", "opencode-go", transport,
    ).generate(messages_request)).text == "messages-ok"

    assert [wire.url.path for wire in seen] == [
        "/zen/go/v1/chat/completions", "/zen/go/v1/messages",
    ]
    for wire in seen:
        assert wire.headers["user-agent"] == "GamjaBot/1.0.0"
        assert len(wire.headers["x-opencode-session"]) == 64
        assert all(character in "0123456789abcdef" for character in wire.headers["x-opencode-session"])
    assert seen[0].headers["x-opencode-session"] == seen[1].headers["x-opencode-session"]
    assert seen[0].headers["x-opencode-session"] == session_header(
        owner_id=9, connection_id=41, room_id=51)
    assert seen[0].headers["x-opencode-session"] != session_header(
        owner_id=9, connection_id=41, room_id=52)
    assert session_header(owner_id=9, connection_id=41, room_id=0) == session_header(
        owner_id=9, connection_id=41, room_id=0)
    assert session_header(owner_id=9, connection_id=41, room_id=0) != session_header(
        owner_id=9, connection_id=42, room_id=0)
    assert seen[0].headers["authorization"] == "Bearer profile-key"
    assert seen[1].headers["x-api-key"] == "profile-key"
    assert seen[1].headers["anthropic-version"] == "2023-06-01"


@pytest.mark.asyncio
@pytest.mark.parametrize(("protocol_id", "model_id"), [
    ("openai_chat_completions", "qwen3.7-plus"),
    ("anthropic_messages", "glm-5.3-flash"),
    ("openai_chat_completions", "gpt-6-luna"),
    ("anthropic_messages", "gpt-6-luna"),
])
async def test_opencode_go_rejects_known_mismatches_and_responses_before_transport(
        protocol_id, model_id):
    calls = []
    connection = named_config(
        "opencode-go", "https://opencode.ai/zen/go/v1",
        protocol_id=protocol_id, model_id=model_id,
    )
    adapter = adapter_for(protocol_id, "opencode-go", httpx.MockTransport(
        lambda wire: calls.append(wire) or httpx.Response(200, json={}),
    ))
    with pytest.raises(ProviderFailure) as unsupported:
        await adapter.generate(request(
            "compatible", connection=connection, model_id=model_id,
            temperature=None, max_tokens=None,
        ))
    assert unsupported.value.code == "unsupported_model_protocol"
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_id", ["openai_chat_completions", "anthropic_messages"])
async def test_opencode_go_unknown_manual_model_uses_the_explicit_protocol(protocol_id):
    calls = []
    connection = named_config(
        "opencode-go", "https://opencode.ai/zen/go/v1",
        protocol_id=protocol_id, model_id="future-owner-model",
    )

    def handler(wire):
        calls.append(wire)
        if protocol_id == "anthropic_messages":
            return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    result = await adapter_for(protocol_id, "opencode-go", httpx.MockTransport(handler)).generate(
        request("compatible", connection=connection, model_id="future-owner-model",
                temperature=None, max_tokens=None))
    assert result.text == "ok"
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("body", "expected"), [
    ({"code": "GoUsageLimitError", "message": "raw vendor allocation detail"}, "quota_exhausted"),
    ({"type": "GoUsageLimitError", "message": "raw vendor allocation detail"}, "quota_exhausted"),
    ({"error": {"code": "GoUsageLimitError", "message": "raw nested detail"}}, "quota_exhausted"),
    ({"error": {"type": "GoUsageLimitError", "message": "raw nested detail"}}, "quota_exhausted"),
    ({"message": "GoUsageLimitError"}, "rate_limited"),
    ({"error": "GoUsageLimitError"}, "rate_limited"),
    ({"code": "OtherLimit", "message": "raw other detail"}, "rate_limited"),
    ({"code": "gousagelimiterror"}, "rate_limited"),
    ({"code": "GoUsageLimitError", "padding": "x" * 17_000}, "rate_limited"),
])
async def test_opencode_go_only_exact_bounded_429_code_is_quota_exhausted(body, expected):
    connection = named_config(
        "opencode-go", "https://opencode.ai/zen/go/v1",
        protocol_id="openai_chat_completions", model_id="glm-5.3-flash",
    )
    adapter = adapter_for("openai_chat_completions", "opencode-go", httpx.MockTransport(
        lambda wire: httpx.Response(429, json=body),
    ))
    with pytest.raises(ProviderFailure) as failed:
        await adapter.generate(request(
            "compatible", connection=connection, model_id="glm-5.3-flash",
            temperature=None, max_tokens=None,
        ))
    assert failed.value.code == expected
    assert "raw" not in str(failed.value).lower()


@pytest.mark.asyncio
async def test_opencode_go_malformed_429_stays_generic_rate_limited():
    connection = named_config(
        "opencode-go", "https://opencode.ai/zen/go/v1",
        protocol_id="openai_chat_completions", model_id="glm-5.3-flash",
    )
    adapter = adapter_for("openai_chat_completions", "opencode-go", httpx.MockTransport(
        lambda wire: httpx.Response(429, content=b"{not-json"),
    ))
    with pytest.raises(ProviderFailure) as failed:
        await adapter.generate(request(
            "compatible", connection=connection, model_id="glm-5.3-flash",
            temperature=None, max_tokens=None,
        ))
    assert failed.value.code == "rate_limited"


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_protocol,provider_id,protocol_id,auth_method_id", [
    ("openai_chat_completions", "openai", "openai_chat_completions", "oauth_browser"),
    ("openai_chat_completions", "ollama", "openai_chat_completions", "api_key"),
    ("anthropic_messages", "anthropic", "openai_chat_completions", "api_key"),
    ("openai_chat_completions", "openrouter", None, "api_key"),
])
async def test_dispatch_rejects_exact_auth_protocol_and_partial_metadata_before_transport(
        adapter_protocol, provider_id, protocol_id, auth_method_id):
    calls = []
    connection = ConnectionConfig(
        52, provider_id, "profile-model", None, "profile-key",
        {"declared": {}, "verified": {}}, provider_id=provider_id,
        protocol_id=protocol_id, auth_method_id=auth_method_id,
        public_config_version=1, public_fields={}, auth_grant_revision=1,
        credential_state="present",
    )
    adapter = adapter_for(adapter_protocol, provider_id, httpx.MockTransport(
        lambda wire: calls.append(wire) or httpx.Response(200, json={}),
    ))

    with pytest.raises(ProviderFailure) as invalid:
        await adapter.generate(request(
            provider_id, connection=connection, model_id="profile-model",
            temperature=None, max_tokens=None,
        ))

    assert invalid.value.code == "invalid_configuration"
    assert calls == []


@pytest.mark.asyncio
async def test_dispatch_pins_localhost_before_send_and_rejects_non_loopback_resolution(monkeypatch):
    calls = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.9", 11434)),
    ])
    adapter = adapter_for("openai_chat_completions", "ollama", httpx.MockTransport(
        lambda wire: calls.append(wire) or httpx.Response(200, json={}),
    ))

    with pytest.raises(ProviderFailure) as invalid:
        await adapter.generate(request(
            "ollama", connection=named_config("ollama", api_key=None),
            model_id="profile-model", temperature=None, max_tokens=None,
        ))

    assert invalid.value.code == "invalid_configuration"
    assert calls == []


@pytest.mark.asyncio
async def test_dispatch_prefers_ipv4_pin_for_dual_stack_localhost_and_skips_dns_for_literals(monkeypatch):
    calls = []
    resolutions = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: resolutions.append(args) or [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 11434, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 11434)),
    ])
    adapter = adapter_for("openai_chat_completions", "ollama", httpx.MockTransport(
        lambda wire: calls.append(wire) or httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]},
        ),
    ))

    await adapter.generate(request(
        "ollama", connection=named_config("ollama", api_key=None),
        model_id="profile-model", temperature=None, max_tokens=None,
    ))
    assert str(calls.pop().url) == "http://127.0.0.1:11434/v1/chat/completions"
    assert len(resolutions) == 1

    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: pytest.fail(
        "numeric loopback must not use DNS",
    ))
    await adapter.generate(request(
        "ollama", connection=named_config(
            "ollama", "http://[::1]:11434/v1", api_key=None,
        ), model_id="profile-model", temperature=None, max_tokens=None,
    ))
    assert calls.pop().url.host == "::1"


@pytest.mark.asyncio
async def test_same_vendor_connections_do_not_mix_credentials_or_models():
    seen = []
    def handler(wire):
        seen.append((wire.headers["authorization"], json.loads(wire.content)["model"]))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    adapter = adapter_for("openai", httpx.MockTransport(handler))
    first = request("openai")
    second = request("openai", connection=config("openai", 2, "gpt-looking-second"), model_id="gpt-looking-second")
    await asyncio.gather(adapter.generate(first), adapter.generate(second))
    assert set(seen) == {("Bearer key-1", "claude-looking-model"), ("Bearer key-2", "gpt-looking-second")}


@pytest.mark.asyncio
async def test_unsupported_options_and_text_only_screenshot_reference():
    seen = []
    def handler(wire):
        seen.append(json.loads(wire.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    adapter = adapter_for("compatible", httpx.MockTransport(handler))
    no_caps = config("compatible", capabilities={"declared": {"tools": True}, "verified": {}})
    with pytest.raises(UnsupportedOption):
        await adapter.generate(request("compatible", connection=no_caps, tools=[{"name": "read_file", "description": "Read", "parameters": {"type": "object"}}]))
    with pytest.raises(UnsupportedOption):
        await adapter.generate(request("compatible", connection=no_caps, temperature=0.7))
    observation = ToolObservation("call-1", "completed", "Screenshot captured", screenshot_ref="artifact:1", image_bytes=b"pixels")
    tools_only = config("compatible", capabilities={"declared": {"tools": True},
        "verified": {"model_id": "claude-looking-model", "base_url": "http://127.0.0.1:8765/v1", "tools": True}})
    with pytest.raises(UnsupportedOption):
        await adapter.generate(request("compatible", connection=tools_only, temperature=None, max_tokens=None,
                                       observations=[observation], continuation={"provider_kind": "compatible",
                                       "connection_id": 1, "model_id": "claude-looking-model", "base_url": "http://127.0.0.1:8765/v1",
                                       "room_id": 0, "agent_id": 7,
                                       "assistant": {"role": "assistant", "content": "", "tool_calls": []}, "call_ids": ["call-1"]}))
    await adapter.generate(request("compatible", connection=no_caps, temperature=None, max_tokens=None,
                                   events=[{"seq": 1, "event_id": "s1", "actor_type": "tool", "actor_id": None,
                                            "actor_name_snapshot": "Tool", "kind": "tool_result",
                                            "payload": {"text": "Screenshot captured", "screenshot_ref": "artifact:1"}}], watermark_seq=1))
    rendered = json.dumps(seen[-1])
    assert "Screenshot artifact available; image content was not supplied" in rendered
    assert "artifact:1" not in rendered and "pixels" not in rendered


@pytest.mark.asyncio
async def test_persisted_screenshot_reference_is_exposed_only_for_canonical_current_room_uuid():
    seen = []

    def handler(wire):
        seen.append(json.loads(wire.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    canonical = "rooms/7/screenshots/11111111-1111-1111-1111-111111111111.png"
    rejected = (
        "rooms/8/screenshots/11111111-1111-1111-1111-111111111111.png",
        "artifact:1",
        "/rooms/7/screenshots/11111111-1111-1111-1111-111111111111.png",
        "rooms/7/screenshots/../11111111-1111-1111-1111-111111111111.png",
        "rooms/7/screenshots/11111111-1111-1111-1111-111111111111.jpg",
        "rooms/7/screenshots/not-a-uuid.png",
    )
    events = [{
        "seq": index, "event_id": f"tool-{index}", "actor_type": "tool",
        "actor_id": None, "actor_name_snapshot": "Tool", "kind": "tool_result",
        "payload": {"text": f"capture {index}", "screenshot_ref": screenshot_ref},
    } for index, screenshot_ref in enumerate((canonical, *rejected), start=1)]

    await adapter_for("compatible", httpx.MockTransport(handler)).generate(request(
        "compatible", room_id=7, events=events, watermark_seq=len(events),
        temperature=None, max_tokens=None,
        connection=config("compatible", capabilities={"declared": {}, "verified": {}}),
    ))

    rendered = json.dumps(seen[0])
    assert canonical in rendered
    assert all(reference not in rendered for reference in rejected)
    assert rendered.count("Screenshot artifact available; image content was not supplied") == len(rejected)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "anthropic", "gemini", "xai", "compatible"])
async def test_native_tool_call_continuation_preserves_matching_envelope(kind):
    bodies = []
    wire_name = "gamja_66696c652e72656164"  # file.read; valid provider function name
    def handler(wire):
        body = json.loads(wire.content)
        bodies.append(body)
        if len(bodies) == 1:
            if kind == "anthropic":
                return httpx.Response(200, json={"content": [{"type": "text", "text": "working"}, {"type": "tool_use", "id": "call-1", "name": wire_name, "input": {"path": "a"}}], "stop_reason": "tool_use"})
            if kind == "gemini":
                return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": "working", "thoughtSignature": "opaque"}, {"functionCall": {"id": "call-1", "name": wire_name, "args": {"path": "a"}}}]}}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": "working", "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": wire_name, "arguments": "{\"path\":\"a\"}"}}]}}]})
        if kind == "anthropic":
            return httpx.Response(200, json={"content": [{"type": "text", "text": "finished"}], "stop_reason": "end_turn"})
        if kind == "gemini":
            return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": "finished"}]}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "finished"}}]})
    caps = {"declared": {"tools": True}, "verified": {"model_id": "claude-looking-model", "base_url": "http://127.0.0.1:8765/v1" if kind == "compatible" else None, "tools": True}}
    adapter = adapter_for(kind, httpx.MockTransport(handler))
    first = await adapter.generate(request(kind, connection=config(kind, capabilities=caps), temperature=None, max_tokens=None,
                                           tools=[{"name": "file.read", "description": "Read", "parameters": {"type": "object"}}]))
    assert first.tool_calls[0].call_id == "call-1"
    assert first.tool_calls[0].family == "file.read" and first.tool_calls[0].arguments == {"path": "a"}
    second = await adapter.generate(request(kind, connection=config(kind, capabilities=caps), temperature=None, max_tokens=None,
                                            tools=[{"name": "file.read", "description": "Read", "parameters": {"type": "object"}}],
                                            continuation=first.continuation,
                                            observations=[ToolObservation("call-1", "completed", "file content")]))
    assert second.text == "finished"
    body = bodies[1]
    first_body = bodies[0]
    if kind == "anthropic":
        assert first_body["tools"][0]["name"] == wire_name
    elif kind == "gemini":
        assert first_body["tools"][0]["functionDeclarations"][0]["name"] == wire_name
    else:
        assert first_body["tools"][0]["function"]["name"] == wire_name
    if kind == "anthropic":
        assert body["messages"][-2]["content"][1]["id"] == "call-1"
        assert body["messages"][-1]["content"][0]["tool_use_id"] == "call-1"
    elif kind == "gemini":
        assert body["contents"][-2]["parts"][0]["thoughtSignature"] == "opaque"
        assert body["contents"][-1]["parts"][0]["functionResponse"]["id"] == "call-1"
    else:
        assert body["messages"][-2]["tool_calls"][0]["id"] == "call-1"
        assert body["messages"][-1]["tool_call_id"] == "call-1"


@pytest.mark.asyncio
async def test_stalled_room_provider_does_not_block_other_room():
    release = asyncio.Event()
    entered = asyncio.Event()
    async def handler(wire):
        if "slow" in str(wire.url):
            entered.set()
            await release.wait()
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    adapter = adapter_for("compatible", httpx.MockTransport(handler))
    slow = request("compatible", room_id=1, connection=ConnectionConfig(1, "compatible", "claude-looking-model", "http://127.0.0.1:8765/slow", "key-1", {}), temperature=None, max_tokens=None)
    task = asyncio.create_task(adapter.generate(slow))
    await asyncio.wait_for(entered.wait(), 1)
    fast = request("compatible", room_id=2, temperature=None, max_tokens=None)
    assert (await asyncio.wait_for(adapter.generate(fast), 1)).text == "ok"
    release.set()
    assert (await task).text == "ok"


@pytest.mark.asyncio
async def test_native_call_arguments_are_validated_and_continuation_is_connection_bound():
    seen = []
    def handler(wire):
        seen.append(json.loads(wire.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "call-1", "type": "function", "function": {
                "name": "gamja_66696c652e72656164", "arguments": '{"path":42}'}}]}}]})
    adapter = adapter_for("openai", httpx.MockTransport(handler))
    caps = {"verified": {"model_id": "claude-looking-model", "base_url": None, "tools": True}}
    tools = [{"name": "file.read", "description": "Read", "parameters": {
        "type": "object", "required": ["path"], "properties": {"path": {"type": "string"}},
        "additionalProperties": False}}]
    with pytest.raises(ProviderFailure) as invalid:
        await adapter.generate(request("openai", connection=config("openai", capabilities=caps),
                                       temperature=None, max_tokens=None, tools=tools))
    assert invalid.value.code == "provider_invalid_response"

    def valid_handler(wire):
        return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "call-1", "type": "function", "function": {
                "name": "gamja_66696c652e72656164", "arguments": '{"path":"a"}'}}]}}]})
    first = await adapter_for("openai", httpx.MockTransport(valid_handler)).generate(
        request("openai", connection=config("openai", capabilities=caps), temperature=None, max_tokens=None, tools=tools))
    with pytest.raises(ProviderFailure) as mismatch:
        await adapter.generate(request("openai", connection=config("openai", 2, capabilities=caps),
                                       temperature=None, max_tokens=None, tools=tools, continuation=first.continuation,
                                       observations=[ToolObservation("call-1", "completed", "done")]))
    assert mismatch.value.code == "invalid_continuation"
    with pytest.raises(ProviderFailure) as wrong_room:
        await adapter.generate(request("openai", connection=config("openai", capabilities=caps), room_id=2,
                                       temperature=None, max_tokens=None, tools=tools, continuation=first.continuation,
                                       observations=[ToolObservation("call-1", "completed", "done")]))
    assert wrong_room.value.code == "invalid_continuation"
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(302, "redirect_refused"), (401, "invalid_credential"),
                                         (429, "rate_limited"), (500, "provider_rejected")])
async def test_provider_http_failure_codes_are_redacted(status, code):
    adapter = adapter_for("openai", httpx.MockTransport(lambda wire: httpx.Response(
        status, json={"error": "raw-upstream-secret"}, headers={"Location": "https://evil.invalid"})))
    with pytest.raises(ProviderFailure) as failed:
        await adapter.generate(request("openai"))
    assert failed.value.code == code and "raw-upstream-secret" not in str(failed.value)


@pytest.mark.asyncio
async def test_cancellation_interrupts_a_stalled_provider_request():
    entered = asyncio.Event()
    never = asyncio.Event()
    cancel = asyncio.Event()
    async def handler(wire):
        entered.set()
        await never.wait()
        return httpx.Response(200, json={"choices": [{"message": {"content": "late"}}]})
    adapter = adapter_for("openai", httpx.MockTransport(handler))
    running = asyncio.create_task(adapter.generate(request("openai", cancel=cancel)))
    await asyncio.wait_for(entered.wait(), 1)
    cancel.set()
    with pytest.raises(ProviderFailure) as stopped:
        await asyncio.wait_for(running, 1)
    assert stopped.value.code == "cancelled"


@pytest.mark.asyncio
async def test_reasoning_response_has_read_budget_without_slow_connection_budget():
    calls = []

    async def handler(wire):
        calls.append(wire)
        # Model completion can outlast 30s even when connection establishment
        # is immediate. MockTransport inspects the actual HTTP timeout policy.
        timeout = wire.extensions["timeout"]
        assert timeout["connect"] <= 10 and timeout["write"] <= 10 and timeout["pool"] <= 10
        if timeout["read"] <= 30:
            raise httpx.ReadTimeout("synthetic slow reasoning response", request=wire)
        assert timeout["read"] <= 120
        return httpx.Response(200, json={"choices": [{"message": {"content": "completed"}}]})

    result = await adapter_for("compatible", httpx.MockTransport(handler)).generate(request("compatible"))
    assert result.text == "completed" and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("with_cancel", [False, True])
async def test_provider_total_deadline_stops_one_request_and_cleans_children(monkeypatch, with_cancel):
    from app.services.providers import base
    monkeypatch.setattr(base, "PROVIDER_REQUEST_DEADLINE_SECONDS", 0.02, raising=False)
    finished = asyncio.Event()
    calls = []

    async def handler(wire):
        calls.append(wire)
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    before = set(asyncio.all_tasks())
    adapter = adapter_for("compatible", httpx.MockTransport(handler))
    with pytest.raises(ProviderFailure) as expired:
        await asyncio.wait_for(adapter.generate(request(
            "compatible", cancel=asyncio.Event() if with_cancel else None)), 0.5)
    assert expired.value.code == "provider_unavailable"
    assert "synthetic" not in str(expired.value)
    assert isinstance(expired.value.__context__, TimeoutError)
    assert finished.is_set() and len(calls) == 1
    await asyncio.sleep(0)
    assert set(asyncio.all_tasks()) <= before


def test_agent_binding_requires_owned_saved_connection_and_exact_model(sqlite_url, monkeypatch):
    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "agents-test-token")
    with TestClient(create_app(database_url=sqlite_url)) as client:
        with client.app.state.session_factory() as db:
            owner = User(email="agent-owner@test.invalid", username="owner", hashed_password="x")
            foreign = User(email="agent-foreign@test.invalid", username="foreign", hashed_password="x")
            db.add_all([owner, foreign])
            db.flush()
            connection = ProviderConnection(user_id=owner.id, kind="openai", name="Saved",
                                            default_model_id="some-default", enabled=True, auth_required=False,
                                            capabilities={"declared": {}, "verified": {}})
            other = ProviderConnection(user_id=foreign.id, kind="openai", name="Other",
                                       default_model_id="other", enabled=True, auth_required=False,
                                       capabilities={"declared": {}, "verified": {}})
            db.add_all([connection, other])
            db.commit()
            owner_id, connection_id, other_id = owner.id, connection.id, other.id
        headers = {"X-Gamja-Session": "agents-test-token", "Authorization": f"Bearer {create_access_token({'sub': owner_id})}"}
        body = {"name": "Ada", "description": "desc", "system_prompt": "prompt",
                "connection_id": connection_id, "model_id": "claude-looking-model"}
        assert client.post("/api/v1/agents/", headers=headers, json={**body, "connection_id": other_id}).status_code == 404
        created = client.post("/api/v1/agents/", headers=headers, json=body)
        assert created.status_code == 201, created.text
        assert (created.json()["connection_id"], created.json()["model_id"]) == (connection_id, "claude-looking-model")
        assert created.json()["max_tokens"] == 0
        assert client.put(f"/api/v1/agents/{created.json()['id']}", headers=headers,
                          json={"connection_id": other_id}).status_code == 404
