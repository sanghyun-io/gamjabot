"""Coding Plan dialect adapters; enrollment/runtime integration is separately gated."""

from .anthropic import AnthropicAdapter
from .base import ProviderFailure, ProviderRequest, ProviderResult
from .openai import OpenAIAdapter
from app.services.provider_catalog import ALIBABA_CHAT_ORIGIN, ALIBABA_MESSAGES_ORIGIN, ALIBABA_MODELS
from app.services.connections import personal_agent_runtime

CHAT_ORIGIN = ALIBABA_CHAT_ORIGIN
MESSAGES_ORIGIN = ALIBABA_MESSAGES_ORIGIN
SUPPORTED_MODELS = frozenset(ALIBABA_MODELS)


def _validate_request(request: ProviderRequest, protocol: str) -> None:
    config = request.connection
    if (config.provider_id != 'alibaba-coding-plan'
            or config.protocol_id != protocol
            or config.auth_method_id != 'api_key'):
        raise ProviderFailure('invalid_configuration', 'Provider protocol is invalid.')
    # This internal value is bound by the trusted resolver, never by public config.
    if (config.personal_agent_runtime is not True
            or not personal_agent_runtime(config.personal_agent_binding)):
        raise ProviderFailure('personal_agent_runtime_required',
                              'Use this connection in the local personal Agent Core.')
    if request.model_id not in SUPPORTED_MODELS:
        raise ProviderFailure('unsupported_model_protocol',
                              'The exact model is not supported by this Coding Plan profile.')


class AlibabaCodingPlanChatAdapter(OpenAIAdapter):
    kind = 'alibaba-coding-plan'
    origin = CHAT_ORIGIN

    async def generate(self, request: ProviderRequest) -> ProviderResult:
        _validate_request(request, 'openai_chat_completions')
        return await super().generate(request)


class AlibabaCodingPlanMessagesAdapter(AnthropicAdapter):
    kind = 'alibaba-coding-plan'
    origin = MESSAGES_ORIGIN

    async def generate(self, request: ProviderRequest) -> ProviderResult:
        _validate_request(request, 'anthropic_messages')
        return await super().generate(request)

    async def _post(self, request: ProviderRequest, path: str, headers: dict, body: dict) -> dict:
        # The documented SDK base excludes /v1; our direct HTTP client adds it.
        if path != '/messages':
            raise ProviderFailure('invalid_configuration', 'Provider request path is invalid.')
        return await super()._post(request, '/v1/messages', headers, body)
