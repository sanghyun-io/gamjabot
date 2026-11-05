from copy import deepcopy
from importlib import import_module
import socket
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError


LEGACY_CASES = (
    ("openai", "openai_chat_completions", "api_key"),
    ("anthropic", "anthropic_messages", "api_key"),
    ("gemini", "gemini_generate_content", "api_key"),
    ("xai", "openai_chat_completions", "api_key"),
    ("compatible", "openai_chat_completions", "api_key"),
)

D1_PROFILE_CASES = (
    ("openrouter", "https://openrouter.ai/api/v1", "discovery", "api_key"),
    ("deepseek", "https://api.deepseek.com", "discovery", "api_key"),
    ("groq", "https://api.groq.com/openai/v1", "discovery", "api_key"),
    ("moonshot-cn", "https://api.moonshot.cn/v1", "discovery", "api_key"),
    ("moonshot-global", "https://api.moonshot.ai/v1", "discovery", "api_key"),
    ("minimax-cn", "https://api.minimax.cn/v1", "discovery", "api_key"),
    ("minimax-global", "https://api.minimax.io/v1", "discovery", "api_key"),
    ("zai", "https://api.z.ai/api/paas/v4", "configured", "api_key"),
    ("dashscope-cn", "https://dashscope.aliyuncs.com/compatible-mode/v1", "discovery", "api_key"),
    ("dashscope-global", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "discovery", "api_key"),
    ("ollama", "http://localhost:11434/v1", "native_catalog", "local_none"),
    ("lm-studio", "http://localhost:1234/v1", "native_catalog", "local_none"),
)

D2_RUNTIME_CASES = (
    ("azure-openai", "azure_openai", "native_chain",
     ("resource_endpoint", "api_version")),
    ("azure-openai", "azure_openai", "api_key",
     ("resource_endpoint", "api_version")),
    ("amazon-bedrock", "aws_bedrock_converse", "native_chain",
     ("profile_name", "region")),
    ("google-vertex", "vertex_generate_content", "native_chain",
     ("project_id", "location")),
)


def catalog_module():
    return import_module("app.services.provider_catalog")


def catalog_schema_module():
    return import_module("app.schemas.provider_catalog")


def test_catalog_has_closed_legacy_provider_protocol_and_method_descriptors():
    catalog = catalog_module().get_catalog()

    assert isinstance(catalog, tuple)
    assert [(item.provider_id, item.protocols, item.methods[0].method_id)
            for item in catalog[:5]] == [
        ("openai", ("openai_chat_completions",), "api_key"),
        ("anthropic", ("anthropic_messages",), "api_key"),
        ("gemini", ("gemini_generate_content",), "api_key"),
        ("xai", ("openai_chat_completions",), "api_key"),
        ("compatible", ("openai_chat_completions",), "api_key"),
    ]
    compatible = catalog[4]
    assert [(method.method_id, method.selectable) for method in compatible.methods] == [
        ("api_key", True), ("local_none", True),
    ]
    assert [(field.field_id, field.kind, field.required, field.max_length)
            for field in compatible.fields] == [
        ("base_url", "url", True, 2048),
        ("allow_local_http", "boolean", False, None),
    ]
    assert all(item.methods[0].selectable is True for item in catalog[:5])
    assert catalog_module().get_descriptor("openai") is catalog[0]
    with pytest.raises(HTTPException) as error:
        catalog_module().get_descriptor("not-a-provider")
    assert error.value.status_code == 422
    assert error.value.detail == {"code": "provider_unavailable"}


def test_d2_catalog_pins_compiled_revision_exact_methods_fields_and_evidence():
    service = catalog_module()

    assert service.CORE_PROVIDER_CONTRACT_REVISION == 1
    assert service.RUNTIME_AUTH_METHODS == frozenset(
        (provider_id, method_id)
        for provider_id, _protocol_id, method_id, _fields in D2_RUNTIME_CASES
    )
    for provider_id, protocol_id, method_id, fields in D2_RUNTIME_CASES:
        descriptor = service.get_descriptor(provider_id)
        assert descriptor.protocols == (protocol_id,)
        assert tuple(field.field_id for field in descriptor.fields) == fields
        method = next(item for item in descriptor.methods if item.method_id == method_id)
        assert (method.code_state, method.delivery_phase,
                method.external_eligibility, method.selectable) == (
            "implemented", "D2", "verified", True,
        )
        assert method.evidence_date == "2026-10-07"
        assert method.evidence_ref.startswith("https://")
        assert descriptor.usage_classification == "unverified"


@pytest.mark.parametrize(("provider_id", "protocol_id", "method_id", "fields", "origin"), [
    ("azure-openai", "azure_openai", "native_chain",
     {"resource_endpoint": "https://potato.openai.azure.com", "api_version": "v1"},
     "https://potato.openai.azure.com"),
    ("azure-openai", "azure_openai", "api_key",
     {"resource_endpoint": "https://potato.openai.azure.com", "api_version": "v1"},
     "https://potato.openai.azure.com"),
    ("amazon-bedrock", "aws_bedrock_converse", "native_chain",
     {"profile_name": "production/role-1", "region": "us-east-1"},
     "https://bedrock-runtime.us-east-1.amazonaws.com"),
    ("google-vertex", "vertex_generate_content", "native_chain",
     {"project_id": "potato-project", "location": "global"},
     "https://aiplatform.googleapis.com"),
    ("google-vertex", "vertex_generate_content", "native_chain",
     {"project_id": "potato-project", "location": "us-central1"},
     "https://us-central1-aiplatform.googleapis.com"),
])
def test_d2_public_config_is_closed_and_origin_resolution_is_local(
        monkeypatch, provider_id, protocol_id, method_id, fields, origin):
    schemas = catalog_schema_module()
    service = catalog_module()
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: pytest.fail(
        "D2 public validation must not inspect network or native credentials"))

    validated = service.validate_public_config(schemas.PublicConfigEnvelope(
        version=1, provider_id=provider_id, protocol_id=protocol_id,
        auth_method_id=method_id, fields=fields,
    ))

    assert validated.fields == fields
    assert service.resolve_origin(provider_id, fields) == origin


@pytest.mark.parametrize(("provider_id", "protocol_id", "method_id", "fields"), [
    ("azure-openai", "azure_openai", "native_chain",
     {"resource_endpoint": "https://potato.openai.azure.com", "api_version": "2026-01-01"}),
    ("amazon-bedrock", "aws_bedrock_converse", "native_chain",
     {"profile_name": "production", "region": "us-gov-west-1"}),
    ("amazon-bedrock", "aws_bedrock_converse", "native_chain",
     {"profile_name": "bad profile", "region": "us-east-1"}),
    ("google-vertex", "vertex_generate_content", "native_chain",
     {"project_id": "-potato", "location": "global"}),
    ("google-vertex", "vertex_generate_content", "native_chain",
     {"project_id": "potato-project", "location": "us"}),
])
def test_d2_public_config_rejects_values_outside_compiled_contract(
        provider_id, protocol_id, method_id, fields):
    schemas = catalog_schema_module()
    service = catalog_module()

    with pytest.raises(HTTPException) as error:
        service.validate_public_config(schemas.PublicConfigEnvelope(
            version=1, provider_id=provider_id, protocol_id=protocol_id,
            auth_method_id=method_id, fields=fields,
        ))

    assert error.value.detail == {"code": "invalid_public_config"}


def test_d1_named_profiles_pin_protocol_endpoint_auth_model_source_and_evidence():
    service = catalog_module()

    for provider_id, base_url, model_strategy, method_id in D1_PROFILE_CASES:
        descriptor = service.get_descriptor(provider_id)
        assert descriptor.protocols == ("openai_chat_completions",)
        assert descriptor.default_base_url == base_url
        assert descriptor.model_source.strategy == model_strategy
        assert descriptor.model_source.manual_id is True
        assert [(method.method_id, method.code_state, method.external_eligibility,
                 method.selectable, method.evidence_date)
                for method in descriptor.methods] == [
            (method_id, "implemented", "verified", True, "2026-10-07"),
        ]
        assert descriptor.methods[0].evidence_ref.startswith("https://")

    zai = service.get_descriptor("zai").model_source
    assert zai.static_model_ids == ("glm-5.3", "glm-5.2", "glm-5.1")
    assert zai.static_evidence_ref == "https://docs.z.ai/api-reference/llm/chat-completion"
    assert zai.static_evidence_date == "2026-10-07"
    assert service.get_descriptor("minimax-cn").default_base_url != "https://api.minimaxi.com/v1"
    assert [field.field_id for field in service.get_descriptor("dashscope-cn").fields] == ["base_url"]
    assert [field.field_id for field in service.get_descriptor("ollama").fields] == ["base_url"]


def test_catalog_local_origin_resolution_is_pure_and_does_not_resolve_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: pytest.fail(
        "catalog reads must not resolve network addresses",
    ))
    service = catalog_module()
    assert service.resolve_origin("ollama", {}) == "http://localhost:11434/v1"
    assert service.resolve_origin("lm-studio", {}) == "http://localhost:1234/v1"


def test_named_profile_origin_policies_are_closed_and_region_bounded():
    schemas = catalog_schema_module()
    service = catalog_module()

    fixed = schemas.PublicConfigEnvelope(
        version=1, provider_id="openrouter", protocol_id="openai_chat_completions",
        auth_method_id="api_key", fields={},
    )
    assert service.validate_public_config(fixed).fields == {}
    assert service.resolve_origin("openrouter", {}) == "https://openrouter.ai/api/v1"

    accepted = (
        ("dashscope-cn", "https://workspace-1.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"),
        ("dashscope-global", "https://workspace-2.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"),
        ("ollama", "http://127.0.0.1:11434/v1"),
        ("ollama", "http://[::1]:11434/v1"),
        ("lm-studio", "http://localhost:4321/v1"),
    )
    for provider_id, base_url in accepted:
        method_id = "local_none" if provider_id in {"ollama", "lm-studio"} else "api_key"
        envelope = schemas.PublicConfigEnvelope(
            version=1, provider_id=provider_id, protocol_id="openai_chat_completions",
            auth_method_id=method_id, fields={"base_url": base_url},
        )
        assert service.validate_public_config(envelope).fields["base_url"] == base_url
        assert service.resolve_origin(provider_id, envelope.fields) == base_url

    rejected = (
        ("openrouter", "api_key", "https://evil.example/api/v1"),
        ("dashscope-cn", "api_key", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
        ("dashscope-cn", "api_key", "https://a.b.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"),
        ("dashscope-cn", "api_key", "https://user@example.com/compatible-mode/v1"),
        ("dashscope-global", "api_key", "https://workspace.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1?token=x"),
        ("dashscope-global", "api_key", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1?"),
        ("dashscope-global", "api_key", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1#"),
        ("ollama", "local_none", "http://192.168.1.2:11434/v1"),
        ("ollama", "local_none", "https://ollama.com/v1"),
        ("ollama", "local_none", "http://localhost:11434/api/chat"),
        ("lm-studio", "local_none", "http://localhost:1234/v1/chat/completions"),
    )
    for provider_id, method_id, base_url in rejected:
        with pytest.raises(HTTPException) as error:
            service.validate_public_config(schemas.PublicConfigEnvelope(
                version=1, provider_id=provider_id, protocol_id="openai_chat_completions",
                auth_method_id=method_id, fields={"base_url": base_url},
            ))
        assert error.value.detail == {"code": "invalid_public_config"}


def test_unknown_auth_and_gateway_candidates_are_visible_but_unselectable():
    schemas = catalog_schema_module()
    service = catalog_module()

    expected_methods = {
        "openai": {"oauth_browser", "oauth_device"},
        "xai": {"oauth_device"},
        "gemini": {"oauth_browser"},
        "github-copilot": {"oauth_device"},
        "opencode-zen": {"api_key"},
    }
    for provider_id, method_ids in expected_methods.items():
        methods = {method.method_id: method for method in service.get_descriptor(provider_id).methods}
        for method_id in method_ids:
            assert methods[method_id].code_state == "absent"
            assert methods[method_id].external_eligibility == "unknown"
            assert methods[method_id].selectable is False

    coding_plan = service.get_descriptor("alibaba-coding-plan")
    assert coding_plan.technical_compatibility == "supported"
    assert coding_plan.usage_classification == "unverified"
    assert all(method.selectable is False for method in coding_plan.methods)

    go = service.get_descriptor("opencode-go")
    assert go.protocols == ("openai_chat_completions", "anthropic_messages")
    assert go.default_base_url == "https://opencode.ai/zen/go/v1"
    assert go.technical_compatibility == "supported"
    assert go.usage_classification == "unverified"
    assert go.methods[0].model_dump(exclude={"evidence_ref", "evidence_date"}) == {
        "method_id": "api_key", "code_state": "implemented", "delivery_phase": "D1",
        "external_eligibility": "verified", "selectable": True,
    }
    assert "glm-5.3-flash" in go.model_source.static_model_ids
    assert "qwen3.7-plus" in go.model_source.static_model_ids
    assert "gpt-6-luna" not in go.model_source.static_model_ids
    assert go.model_source.static_model_protocols["glm-5.3-flash"] == "openai_chat_completions"
    assert go.model_source.static_model_protocols["qwen3.7-plus"] == "anthropic_messages"
    assert set(go.model_source.static_model_protocols) == set(go.model_source.static_model_ids)
    assert service.validate_provider_model_protocol(
        "opencode-go", "openai_chat_completions", "glm-5.3-flash") is None
    assert service.validate_provider_model_protocol(
        "opencode-go", "anthropic_messages", "qwen3.7-plus") is None
    for protocol, model in (
        ("openai_chat_completions", "qwen3.7-plus"),
        ("anthropic_messages", "glm-5.3-flash"),
        ("openai_chat_completions", "gpt-6-luna"),
        ("anthropic_messages", "gpt-6-luna"),
    ):
        with pytest.raises(HTTPException) as mismatch:
            service.validate_provider_model_protocol("opencode-go", protocol, model)
        assert mismatch.value.detail == {"code": "invalid_model_protocol"}
    assert service.validate_provider_model_protocol(
        "opencode-go", "openai_chat_completions", "future-owner-model") is None
    assert service.validate_provider_model_protocol(
        "opencode-go", "anthropic_messages", "future-owner-model") is None

    for provider_id, method_id in (("openai", "oauth_browser"),
                                   ("github-copilot", "oauth_device"),
                                   ("opencode-zen", "api_key"),
                                   ("alibaba-coding-plan", "api_key")):
        with pytest.raises(HTTPException) as error:
            service.validate_public_config(schemas.PublicConfigEnvelope(
                version=1, provider_id=provider_id,
                protocol_id=service.get_descriptor(provider_id).protocols[0],
                auth_method_id=method_id, fields={},
            ))
        assert error.value.detail == {"code": "invalid_public_config"}


def test_launchable_auth_method_is_static_and_rejects_unreviewed_methods(monkeypatch):
    service = catalog_module()
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: pytest.fail(
        "auth method eligibility must not resolve network addresses",
    ))

    method = service.launchable_auth_method("openai", "api_key")
    assert (method.method_id, method.code_state, method.external_eligibility,
            method.selectable) == ("api_key", "implemented", "verified", True)

    for provider_id, method_id in (
        ("openai", "oauth_browser"),
        ("xai", "oauth_device"),
        ("gemini", "oauth_browser"),
        ("github-copilot", "oauth_device"),
        ("alibaba-coding-plan", "api_key"),
    ):
        with pytest.raises(HTTPException) as error:
            service.launchable_auth_method(provider_id, method_id)
        assert error.value.status_code == 422
        assert error.value.detail == {"code": "auth_method_unavailable"}


def test_public_config_rejects_unknown_fields_and_protocol_or_method_mismatch():
    schemas = catalog_schema_module()
    service = catalog_module()

    with pytest.raises(ValidationError):
        schemas.PublicConfigEnvelope.model_validate({
            "version": 1,
            "provider_id": "openai",
            "protocol_id": "openai_chat_completions",
            "auth_method_id": "api_key",
            "fields": {},
            "unexpected": True,
        })

    cases = (
        {"provider_id": "openai", "protocol_id": "openai_chat_completions",
         "auth_method_id": "api_key", "fields": {"base_url": "https://example.test"}},
        {"provider_id": "openai", "protocol_id": "anthropic_messages",
         "auth_method_id": "api_key", "fields": {}},
        {"provider_id": "openai", "protocol_id": "openai_chat_completions",
         "auth_method_id": "local_none", "fields": {}},
    )
    for body in cases:
        envelope = schemas.PublicConfigEnvelope(version=1, **body)
        with pytest.raises(HTTPException) as error:
            service.validate_public_config(envelope)
        assert error.value.status_code == 422
        assert error.value.detail == {"code": "invalid_public_config"}

    with pytest.raises(ValidationError):
        schemas.PublicConfigEnvelope(
            version=1,
            provider_id="compatible",
            protocol_id="openai_chat_completions",
            auth_method_id="api_key",
            fields={"base_url": "https://" + "x" * 2042},
        )


@pytest.mark.parametrize("kind,protocol,method", LEGACY_CASES)
def test_legacy_connections_normalize_in_memory_without_mutating_rows(kind, protocol, method):
    service = catalog_module()
    row = SimpleNamespace(
        kind=kind,
        base_url="https://compatible.example.test/v1" if kind == "compatible" else None,
        auth_required=True,
        secret_ref="synthetic-secret-reference",
    )
    before = deepcopy(vars(row))

    normalized = service.normalize_legacy_connection(row)

    assert vars(row) == before
    assert normalized.provider_id == kind
    assert normalized.protocol_id == protocol
    assert normalized.auth_method_id == method
    assert normalized.public_config_version == 1
    assert normalized.public_config == ({
        "base_url": "https://compatible.example.test/v1",
        "allow_local_http": False,
    } if kind == "compatible" else {})
    assert normalized.auth_grant_revision == 0
    assert normalized.credential_state == "present"


def test_authless_loopback_legacy_connection_normalizes_as_not_required():
    row = SimpleNamespace(
        kind="compatible",
        base_url="http://127.0.0.1:11434/v1",
        auth_required=False,
        secret_ref=None,
    )

    normalized = catalog_module().normalize_legacy_connection(row)

    assert normalized.auth_method_id == "local_none"
    assert normalized.public_config == {
        "base_url": "http://127.0.0.1:11434/v1",
        "allow_local_http": True,
    }
    assert normalized.credential_state == "not_required"


def test_catalog_route_is_authenticated_versioned_and_has_no_provider_or_secret_side_effects(
        tmp_path, monkeypatch):
    from app.api.routes.auth import create_access_token
    from app.core import migrate as migration
    from app.main import create_app
    from app.models.models import User
    from app.services import connections, providers

    monkeypatch.setenv("GAMJA_LOCAL_SESSION_TOKEN", "catalog-test-token")
    monkeypatch.setattr(migration, "REVISIONS", migration.REVISIONS[:22])
    monkeypatch.setattr(connections, "store_for",
                        lambda *_: pytest.fail("catalog read opened the credential store"))
    monkeypatch.setattr(providers, "adapter_for",
                        lambda *_args, **_kwargs: pytest.fail("catalog read opened a provider adapter"))
    app = create_app(database_url=f"sqlite:///{tmp_path / 'catalog.sqlite'}")
    with TestClient(app) as client:
        with client.app.state.session_factory() as db:
            user = User(email="catalog@example.test", username="catalog", hashed_password="unused")
            db.add(user)
            db.commit()
            owner_id = user.id
        local_headers = {"X-Gamja-Session": "catalog-test-token"}
        assert client.get("/api/v1/connections/catalog", headers=local_headers).status_code == 403
        response = client.get("/api/v1/connections/catalog", headers={
            **local_headers,
            "Authorization": f"Bearer {create_access_token({'sub': owner_id})}",
        })

    assert response.status_code == 200
    assert response.json()["version"] == 1
    provider_ids = [item["provider_id"] for item in response.json()["providers"]]
    assert provider_ids[:5] == ["openai", "anthropic", "gemini", "xai", "compatible"]
    assert provider_ids[5:8] == ["azure-openai", "amazon-bedrock", "google-vertex"]
    assert [case[0] for case in D1_PROFILE_CASES] == provider_ids[8:20]
    assert provider_ids[20:] == [
        "alibaba-coding-plan", "github-copilot", "opencode-zen", "opencode-go",
    ]
    assert "secret" not in response.text.lower()
