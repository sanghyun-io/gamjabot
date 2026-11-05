"""Reasoning output must fit in a bounded, single-request connection probe."""

import asyncio
import json

import httpx
import pytest

from app.services import connections


def _config(kind):
    if kind == "opencode-go":
        return connections.ConnectionConfig(
            3, "compatible", "longcat-2.5-preview-free", None, "synthetic-key", {},
            provider_id=kind, protocol_id="openai_chat_completions",
            auth_method_id="api_key", public_config_version=1, public_fields={},
            auth_grant_revision=1, credential_state="present", owner_id=1,
        )
    return connections.ConnectionConfig(
        4, kind, "synthetic-model", None, "synthetic-key", {},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["opencode-go", "anthropic", "gemini"])
async def test_text_probe_allows_reasoning_before_answer_without_retry(kind):
    calls = []

    def handler(wire):
        body = json.loads(wire.content)
        calls.append(body)
        budget = (body["generationConfig"]["maxOutputTokens"]
                  if kind == "gemini" else body["max_tokens"])
        # Real Go observation: reasoning plus the short answer used 54 completion
        # tokens. A tiny ceiling returns HTTP200 with no user-visible answer.
        assert 0 < budget <= 1024, "a small connection test must stay bounded"
        complete = budget >= 54
        if kind == "gemini":
            payload = {"candidates": [{"content": {"parts": [
                {"text": "OK" if complete else ""},
            ]}}]}
        elif kind == "anthropic":
            payload = {"content": ([{"type": "text", "text": "OK"}]
                                   if complete else [{"type": "thinking", "thinking": "…"}])}
        else:
            payload = {"choices": [{"message": {
                "content": "OK" if complete else None,
                "reasoning_content": "Synthetic reasoning",
            }, "finish_reason": "stop" if complete else "length"}]}
        return httpx.Response(200, json=payload)

    result = await connections.probe(_config(kind), httpx.MockTransport(handler))

    assert result["ok"] is True
    assert result["code"] == "connected"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_explicit_custom_max_tokens_probe_can_reach_answer():
    calls = []
    config = connections.ConnectionConfig(
        5, "compatible", "synthetic-reasoning-model", "http://127.0.0.1:8765/v1",
        "synthetic-key", {"declared": {"max_tokens": True}},
    )

    def handler(wire):
        body = json.loads(wire.content)
        calls.append(body)
        budget = body["max_tokens"]
        assert 0 < budget <= 1024
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "OK" if budget >= 54 else None,
            "reasoning_content": "Synthetic reasoning",
        }, "finish_reason": "stop" if budget >= 54 else "length"}]})

    result = await connections.probe_capabilities(config, httpx.MockTransport(handler))

    assert result["max_tokens"] is True
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_custom_text_probe_keeps_optional_parameters_opt_in():
    calls = []
    config = connections.ConnectionConfig(
        6, "compatible", "synthetic-model", "http://127.0.0.1:8765/v1",
        "synthetic-key", {},
    )

    def handler(wire):
        body = json.loads(wire.content)
        calls.append(body)
        assert "max_tokens" not in body
        assert "temperature" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    result = await connections.probe(config, httpx.MockTransport(handler))
    options = await connections.probe_capabilities(config, httpx.MockTransport(handler))

    assert result["ok"] is True
    assert options["max_tokens"] is False
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_valid_reasoning_response_can_take_longer_than_five_seconds():
    calls = []

    def handler(wire):
        calls.append(wire)
        # Real valid responses varied between4.5 and7.7s. Simulate the wait
        # against the actual HTTP timeout policy without sleeping or using APIs.
        if wire.extensions["timeout"]["read"] < 7.7:
            raise httpx.ReadTimeout("Synthetic seven-second response", request=wire)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    result = await connections.probe(_config("opencode-go"), httpx.MockTransport(handler))

    assert result["ok"] is True
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_whole_probe_deadline_cancels_a_stalled_response(monkeypatch):
    monkeypatch.setattr(connections, "CONNECTION_PROBE_DEADLINE_SECONDS", 0.005, raising=False)
    calls = []
    cancelled = []

    async def handler(wire):
        calls.append(wire)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    with pytest.raises(connections.ConnectionFailure) as failure:
        await asyncio.wait_for(connections.probe(
            _config("opencode-go"), httpx.MockTransport(handler)), timeout=0.25)

    assert failure.value.code == "unreachable"
    assert len(calls) == 1
    assert cancelled == [True]


@pytest.mark.asyncio
async def test_user_cancellation_propagates_without_retry():
    started = asyncio.Event()
    cancelled = []
    calls = []

    async def handler(wire):
        calls.append(wire)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    task = asyncio.create_task(connections.probe(
        _config("opencode-go"), httpx.MockTransport(handler)))
    await asyncio.wait_for(started.wait(), timeout=0.25)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(calls) == 1
    assert cancelled == [True]
