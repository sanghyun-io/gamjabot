"""Amazon Bedrock through the private Core provider channel."""

from app.services.core_provider_transport import CoreProviderAdapter


class BedrockAdapter(CoreProviderAdapter):
    provider_id = "amazon-bedrock"
    public_protocol = "aws_bedrock_converse"
