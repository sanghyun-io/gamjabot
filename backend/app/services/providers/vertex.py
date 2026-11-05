"""Google Vertex through the private Core provider channel."""

from app.services.core_provider_transport import CoreProviderAdapter


class VertexAdapter(CoreProviderAdapter):
    provider_id = "google-vertex"
    public_protocol = "vertex_generate_content"
