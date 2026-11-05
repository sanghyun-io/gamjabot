"""Closed OpenCode Go adapters for its documented chat and Messages endpoints."""

from hashlib import sha256

import httpx
from fastapi import HTTPException

from app.services import provider_catalog

from .anthropic import AnthropicAdapter
from .base import ProviderFailure, ProviderRequest
from .openai import OpenAIAdapter


USER_AGENT = "GamjaBot/1.0.0"


def _validate_request(request: ProviderRequest, protocol_id: str) -> None:
    try:
        provider_catalog.validate_provider_model_protocol(
            "opencode-go", protocol_id, request.model_id)
    except HTTPException:
        raise ProviderFailure(
            "unsupported_model_protocol",
            "This OpenCode Go model is not supported by the selected protocol.",
        ) from None
    if (type(request.connection.owner_id) is not int
            or request.connection.owner_id < 1
            or type(request.connection_id) is not int
            or request.connection_id < 1
            or type(request.room_id) is not int
            or request.room_id < 0):
        raise ProviderFailure(
            "invalid_configuration", "Connection configuration is invalid.")


def session_header(*, owner_id: int, connection_id: int, room_id: int) -> str:
    namespace = f"room:{room_id}" if room_id > 0 else "connection-test"
    material = f"gamja-opencode-go-session-v1\0{owner_id}\0{connection_id}\0{namespace}"
    return sha256(material.encode("utf-8")).hexdigest()


def request_headers(request: ProviderRequest, protocol_id: str) -> dict[str, str]:
    _validate_request(request, protocol_id)
    headers = {
        "User-Agent": USER_AGENT,
        "x-opencode-session": session_header(
            owner_id=request.connection.owner_id,
            connection_id=request.connection_id,
            room_id=request.room_id,
        ),
    }
    if protocol_id == "anthropic_messages":
        headers.update({
            "x-api-key": request.connection.api_key or "",
            "anthropic-version": "2023-06-01",
        })
    else:
        headers["Authorization"] = f"Bearer {request.connection.api_key or ''}"
    return headers


def rate_limit_failure(response: httpx.Response) -> tuple[str, str]:
    if len(response.content) <= 16_384:
        try:
            body = response.json()
            if isinstance(body, dict):
                scopes = (body, body.get("error"))
                if any(
                    isinstance(scope, dict)
                    and any(scope.get(field) == "GoUsageLimitError"
                            for field in ("code", "type"))
                    for scope in scopes
                ):
                    return "quota_exhausted", "OpenCode Go usage is exhausted. Wait for usage to recover and check the connection settings."
        except (ValueError, TypeError):
            pass
    return "rate_limited", "Provider rate limit reached."


class OpenCodeGoChatAdapter(OpenAIAdapter):
    kind = "opencode-go"
    origin = provider_catalog.OPENCODE_GO_BASE_URL

    def _headers(self, request: ProviderRequest) -> dict[str, str]:
        return request_headers(request, "openai_chat_completions")

    def _rate_limit_failure(self, response: httpx.Response) -> tuple[str, str]:
        return rate_limit_failure(response)


class OpenCodeGoMessagesAdapter(AnthropicAdapter):
    kind = "opencode-go"
    origin = provider_catalog.OPENCODE_GO_BASE_URL

    def _headers(self, request: ProviderRequest) -> dict[str, str]:
        return request_headers(request, "anthropic_messages")

    def _rate_limit_failure(self, response: httpx.Response) -> tuple[str, str]:
        return rate_limit_failure(response)
