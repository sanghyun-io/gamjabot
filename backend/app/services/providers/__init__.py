"""Explicit provider-kind registry."""

from fastapi import HTTPException

from app.services import provider_catalog

from .base import ProviderFailure, ProviderRequest, ProviderResult, ToolCall, ToolObservation, UnsupportedOption, VisionInput
from .openai import OpenAIAdapter
from .anthropic import AnthropicAdapter
from .gemini import GeminiAdapter
from .xai import XAIAdapter
from .compatible import CompatibleAdapter


ADAPTERS = {"openai": OpenAIAdapter, "anthropic": AnthropicAdapter, "gemini": GeminiAdapter,
            "xai": XAIAdapter, "compatible": CompatibleAdapter}
CORE_ADAPTERS = {
    ("azure_openai", "azure-openai"): "azure_openai",
    ("aws_bedrock_converse", "amazon-bedrock"): "bedrock",
    ("vertex_generate_content", "google-vertex"): "vertex",
}
_CORE_PROVIDER_IDS = frozenset(provider_id for _, provider_id in CORE_ADAPTERS)


def adapter_for(protocol_id: str, provider_id=None, transport=None, *, core_transport=None,
                physical_request_budget=None):
    if provider_id is not None and not isinstance(provider_id, str):
        transport = provider_id
        provider_id = protocol_id
        protocol_id = None
    elif provider_id is None:
        provider_id = protocol_id
        protocol_id = None
    if provider_id == 'chatgpt':
        if protocol_id != 'openai_responses':
            raise ProviderFailure('invalid_configuration', 'ChatGPT requires Responses.')
        from .chatgpt import ChatGPTAdapter
        return ChatGPTAdapter(transport)
    core_adapter_name = CORE_ADAPTERS.get((protocol_id, provider_id))
    if core_adapter_name is not None:
        if core_transport is None:
            raise ProviderFailure("provider_unavailable", "Core provider transport is unavailable.")
        if core_adapter_name == "azure_openai":
            from .azure_openai import AzureOpenAIAdapter
            return AzureOpenAIAdapter(
                core_transport, physical_request_budget=physical_request_budget)
        if core_adapter_name == "bedrock":
            from .bedrock import BedrockAdapter
            return BedrockAdapter(
                core_transport, physical_request_budget=physical_request_budget)
        from .vertex import VertexAdapter
        return VertexAdapter(
            core_transport, physical_request_budget=physical_request_budget)
    if provider_id in _CORE_PROVIDER_IDS:
        raise ProviderFailure("invalid_configuration", "Provider protocol is unavailable.")
    try:
        descriptor = provider_catalog.get_descriptor(provider_id)
    except HTTPException:
        raise ProviderFailure("invalid_configuration", "Unknown provider kind.") from None
    if provider_id in {"opencode-go", "alibaba-coding-plan"} and protocol_id is None:
        raise ProviderFailure("invalid_configuration", "Provider protocol is required.")
    selected_protocol = protocol_id or descriptor.protocols[0]
    if (selected_protocol not in descriptor.protocols
            or (provider_id != "alibaba-coding-plan"
                and not any(method.selectable for method in descriptor.methods))):
        raise ProviderFailure("invalid_configuration", "Provider profile is unavailable.")
    if provider_id in ADAPTERS:
        return ADAPTERS[provider_id](transport)
    if provider_id == "alibaba-coding-plan":
        from .alibaba_coding_plan import AlibabaCodingPlanChatAdapter, AlibabaCodingPlanMessagesAdapter
        return (AlibabaCodingPlanChatAdapter if selected_protocol == "openai_chat_completions"
                else AlibabaCodingPlanMessagesAdapter)(transport)
    if provider_id == "opencode-go":
        from .opencode_go import OpenCodeGoChatAdapter, OpenCodeGoMessagesAdapter
        adapter = (OpenCodeGoChatAdapter if selected_protocol == "openai_chat_completions"
                   else OpenCodeGoMessagesAdapter)
        return adapter(transport)
    if selected_protocol != "openai_chat_completions":
        raise ProviderFailure("invalid_configuration", "Provider protocol is unavailable.")
    adapter = OpenAIAdapter(transport)
    adapter.kind = provider_id
    adapter.origin = descriptor.default_base_url or ""
    return adapter
