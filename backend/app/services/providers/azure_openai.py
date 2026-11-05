"""Azure OpenAI through the private Core provider channel."""

from app.services.core_provider_transport import CoreProviderAdapter


class AzureOpenAIAdapter(CoreProviderAdapter):
    provider_id = "azure-openai"
    public_protocol = "azure_openai"
