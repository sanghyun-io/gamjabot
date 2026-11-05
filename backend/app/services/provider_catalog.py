"""Pure built-in provider descriptors and lossless legacy projection."""

import re
from urllib.parse import urlsplit

from fastapi import HTTPException

from app.schemas.provider_catalog import (
    AuthMethodDescriptor,
    ModelSourceDescriptor,
    NormalizedConnection,
    ProviderDescriptor,
    PublicConfigEnvelope,
    PublicFieldDescriptor,
    ValidatedPublicConfig,
)

ALIBABA_CHAT_ORIGIN = "https://coding-intl.dashscope.aliyuncs.com/v1"
ALIBABA_MESSAGES_ORIGIN = "https://coding-intl.dashscope.aliyuncs.com/apps/anthropic"
ALIBABA_MODELS = (
    "qwen3.7-plus", "qwen3.6-plus", "kimi-k2.5", "glm-5", "MiniMax-M2.5", "qwen3.5-plus",
)


def _method(method_id: str = "api_key", *, selectable: bool = True,
            evidence_ref: str | None = None,
            code_state: str | None = None,
            delivery_phase: str | None = None,
            external_eligibility: str | None = None) -> AuthMethodDescriptor:
    return AuthMethodDescriptor(
        method_id=method_id,
        code_state=code_state or (
            "implemented" if method_id in {"api_key", "local_none"} else "absent"
        ),
        delivery_phase=delivery_phase or ("D1" if selectable else "D3"),
        external_eligibility=(external_eligibility
                              or ("verified" if selectable else "unknown")),
        selectable=selectable,
        evidence_ref=evidence_ref,
        evidence_date="2026-10-07" if evidence_ref else None,
    )


_CONFIGURED_MODELS = ModelSourceDescriptor(strategy="configured", manual_id=True)
OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"
OPENCODE_GO_CHAT_MODELS = (
    "glm-5.3-flash", "glm-5.3", "glm-5.2", "kimi-k3", "kimi-k2.7-code",
    "kimi-k2.6", "longcat-2.0", "longcat-2.5-preview-free",
    "step-5-preview-free", "deepseek-v4.1-flash", "deepseek-v4-pro",
    "deepseek-v4-flash", "deepseek-v4-flash-vision-exp", "mimo-v2.6-flash",
    "mimo-v2.6-pro", "mimo-v2.5", "mimo-v2.5-pro", "hy4-preview", "hy3",
    "space-bunny",
)
OPENCODE_GO_MESSAGES_MODELS = (
    "claude-haiku-5-5", "minimax-m3", "minimax-m2.7", "qwen3.8-max",
    "qwen3.8-flash", "qwen3.7-plus",
)
OPENCODE_GO_RESPONSES_MODELS = frozenset({
    "grok-4.7", "grok-4.6", "gpt-6-luna", "gpt-5.6-luna",
    "muse-spark-1.3-contributor", "muse-spark-1.2-contributor",
})
_OPENCODE_GO_MODEL_PROTOCOL = {
    **{model_id: "openai_chat_completions" for model_id in OPENCODE_GO_CHAT_MODELS},
    **{model_id: "anthropic_messages" for model_id in OPENCODE_GO_MESSAGES_MODELS},
}
_COMPATIBLE_FIELDS = (
    PublicFieldDescriptor(field_id="base_url", kind="url", required=True, max_length=2048),
    PublicFieldDescriptor(field_id="allow_local_http", kind="boolean", required=False),
)
_OPTIONAL_BASE_URL = (
    PublicFieldDescriptor(field_id="base_url", kind="url", required=False, max_length=2048),
)
_AZURE_OPENAI_FIELDS = (
    PublicFieldDescriptor(
        field_id="resource_endpoint", kind="url", required=True, max_length=2048),
    PublicFieldDescriptor(
        field_id="api_version", kind="enum", required=True, max_length=16),
)
_AWS_BEDROCK_FIELDS = (
    PublicFieldDescriptor(
        field_id="profile_name", kind="text", required=True, max_length=128),
    PublicFieldDescriptor(
        field_id="region", kind="region", required=True, max_length=32),
)
_VERTEX_FIELDS = (
    PublicFieldDescriptor(
        field_id="project_id", kind="project", required=True, max_length=63),
    PublicFieldDescriptor(
        field_id="location", kind="location", required=True, max_length=32),
)

CORE_PROVIDER_CONTRACT_REVISION = 1
RUNTIME_AUTH_METHODS = frozenset({
    ("azure-openai", "native_chain"),
    ("azure-openai", "api_key"),
    ("amazon-bedrock", "native_chain"),
    ("google-vertex", "native_chain"),
})


def _d2_method(method_id: str, evidence_ref: str) -> AuthMethodDescriptor:
    return _method(
        method_id, evidence_ref=evidence_ref, code_state="implemented",
        delivery_phase="D2", external_eligibility="verified",
    )


def _descriptor(provider_id: str, protocol_id: str, *, compatible: bool = False,
                methods: tuple[AuthMethodDescriptor, ...] | None = None,
                default_base_url: str | None = None,
                model_source: ModelSourceDescriptor = _CONFIGURED_MODELS,
                fields: tuple[PublicFieldDescriptor, ...] = (),
                technical_compatibility: str = "supported",
                usage_classification: str = "verified") -> ProviderDescriptor:
    return ProviderDescriptor(
        provider_id=provider_id,
        label_key=f"connections.providers.{provider_id}",
        icon=provider_id,
        protocols=(protocol_id,),
        fields=_COMPATIBLE_FIELDS if compatible else fields,
        model_source=model_source,
        methods=(methods if methods is not None else
                 (_method(), _method("local_none")) if compatible else (_method(),)),
        default_base_url=default_base_url,
        technical_compatibility=technical_compatibility,
        usage_classification=usage_classification,
    )


def _profile(provider_id: str, base_url: str, evidence_ref: str, *,
             strategy: str = "discovery", local: bool = False,
             fields: tuple[PublicFieldDescriptor, ...] = (),
             static_model_ids: tuple[str, ...] = (),
             static_evidence_ref: str | None = None) -> ProviderDescriptor:
    return _descriptor(
        provider_id,
        "openai_chat_completions",
        default_base_url=base_url,
        fields=fields,
        methods=((_method("local_none", evidence_ref=evidence_ref),)
                 if local else (_method(evidence_ref=evidence_ref),)),
        model_source=ModelSourceDescriptor(
            strategy=strategy,
            manual_id=True,
            static_model_ids=static_model_ids,
            static_evidence_ref=static_evidence_ref,
            static_evidence_date="2026-10-07" if static_evidence_ref else None,
        ),
    )


_CATALOG = (
    _descriptor("chatgpt", "openai_responses", default_base_url="https://api.openai.com/v1",
                methods=(_method("oauth_browser", code_state="implemented", delivery_phase="D2",
                                 evidence_ref="https://developers.openai.com/siwc/token-sharing-open-source"),),
                model_source=ModelSourceDescriptor(strategy="discovery", manual_id=True)),
    _descriptor("openai", "openai_chat_completions", default_base_url="https://api.openai.com/v1",
                methods=(_method(), _method("oauth_browser", selectable=False),
                         _method("oauth_device", selectable=False))),
    _descriptor("anthropic", "anthropic_messages", default_base_url="https://api.anthropic.com/v1"),
    _descriptor("gemini", "gemini_generate_content",
                default_base_url="https://generativelanguage.googleapis.com/v1beta",
                methods=(_method(), _method("oauth_browser", selectable=False))),
    _descriptor("xai", "openai_chat_completions", default_base_url="https://api.x.ai/v1",
                methods=(_method(), _method("oauth_device", selectable=False))),
    _descriptor("compatible", "openai_chat_completions", compatible=True),
    _descriptor(
        "azure-openai", "azure_openai", fields=_AZURE_OPENAI_FIELDS,
        methods=(
            _d2_method(
                "native_chain",
                "https://learn.microsoft.com/cli/azure/authenticate-azure-cli-interactively",
            ),
            _d2_method(
                "api_key",
                "https://learn.microsoft.com/en-us/rest/api/aifoundry/azureopenai/responses",
            ),
        ),
        usage_classification="unverified",
    ),
    _descriptor(
        "amazon-bedrock", "aws_bedrock_converse", fields=_AWS_BEDROCK_FIELDS,
        methods=(_d2_method(
            "native_chain",
            "https://docs.aws.amazon.com/sdkref/latest/guide/standardized-credentials.html",
        ),),
        usage_classification="unverified",
    ),
    _descriptor(
        "google-vertex", "vertex_generate_content", fields=_VERTEX_FIELDS,
        methods=(_d2_method(
            "native_chain",
            "https://docs.cloud.google.com/docs/authentication/application-default-credentials",
        ),),
        usage_classification="unverified",
    ),
    _profile("openrouter", "https://openrouter.ai/api/v1",
             "https://openrouter.ai/docs/quickstart"),
    _profile("deepseek", "https://api.deepseek.com",
             "https://api-docs.deepseek.com/"),
    _profile("groq", "https://api.groq.com/openai/v1",
             "https://console.groq.com/docs/openai"),
    _profile("moonshot-cn", "https://api.moonshot.cn/v1",
             "https://platform.kimi.com/docs/api/overview"),
    _profile("moonshot-global", "https://api.moonshot.ai/v1",
             "https://platform.kimi.ai/docs/api/overview"),
    _profile("minimax-cn", "https://api.minimax.cn/v1",
             "https://platform.minimax.cn/docs/api-reference/text-openai-api"),
    _profile("minimax-global", "https://api.minimax.io/v1",
             "https://platform.minimax.io/docs/api-reference/text-openai-api"),
    _profile(
        "zai", "https://api.z.ai/api/paas/v4",
        "https://docs.z.ai/guides/overview/quick-start",
        strategy="configured",
        static_model_ids=("glm-5.3", "glm-5.2", "glm-5.1"),
        static_evidence_ref="https://docs.z.ai/api-reference/llm/chat-completion",
    ),
    _profile("dashscope-cn", "https://dashscope.aliyuncs.com/compatible-mode/v1",
             "https://www.alibabacloud.com/help/en/model-studio/base-url",
             fields=_OPTIONAL_BASE_URL),
    _profile("dashscope-global", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
             "https://www.alibabacloud.com/help/en/model-studio/base-url",
             fields=_OPTIONAL_BASE_URL),
    _profile("local-openai", "http://127.0.0.1:8080/v1",
             "https://github.com/ggml-org/llama.cpp/tree/master/tools/server",
             strategy="native_catalog", local=True, fields=_OPTIONAL_BASE_URL),
    _profile("ollama", "http://localhost:11434/v1",
             "https://docs.ollama.com/api/openai-compatibility",
             strategy="native_catalog", local=True, fields=_OPTIONAL_BASE_URL),
    _profile("lm-studio", "http://localhost:1234/v1",
             "https://lmstudio.ai/docs/developer/openai-compat",
             strategy="native_catalog", local=True, fields=_OPTIONAL_BASE_URL),
    ProviderDescriptor(
        provider_id="alibaba-coding-plan",
        label_key="connections.providers.alibaba-coding-plan",
        icon="alibaba-coding-plan",
        protocols=("openai_chat_completions", "anthropic_messages"),
        fields=(),
        model_source=ModelSourceDescriptor(
            strategy="configured", manual_id=False, static_model_ids=ALIBABA_MODELS,
            static_evidence_ref="https://www.alibabacloud.com/help/en/model-studio/coding-plan",
            static_evidence_date="2026-10-12",
        ),
        methods=(_method(selectable=False, external_eligibility="verified",
                         evidence_ref="https://www.alibabacloud.com/help/en/model-studio/more-tools"),),
        technical_compatibility="supported",
        usage_classification="unverified",
        usage_scope="personal_interactive",
        default_base_url=ALIBABA_CHAT_ORIGIN,
    ),
    _descriptor("github-copilot", "openai_chat_completions",
                methods=(_method("oauth_device", selectable=False),),
                technical_compatibility="unknown", usage_classification="unknown"),
    _descriptor("opencode-zen", "openai_chat_completions",
                methods=(_method(selectable=False, code_state="absent"),),
                technical_compatibility="unknown", usage_classification="unknown"),
    ProviderDescriptor(
        provider_id="opencode-go",
        label_key="connections.providers.opencode-go",
        icon="opencode-go",
        protocols=("openai_chat_completions", "anthropic_messages"),
        fields=(),
        model_source=ModelSourceDescriptor(
            strategy="configured", manual_id=True,
            static_model_ids=OPENCODE_GO_CHAT_MODELS + OPENCODE_GO_MESSAGES_MODELS,
            static_model_protocols=_OPENCODE_GO_MODEL_PROTOCOL,
            static_evidence_ref="https://docs.opencode.ai/docs/go/#endpoints",
            static_evidence_date="2026-10-09",
        ),
        methods=(_method(
            evidence_ref="https://docs.opencode.ai/docs/go/#how-it-works"),),
        default_base_url=OPENCODE_GO_BASE_URL,
        technical_compatibility="supported",
        usage_classification="unverified",
    ),
)
_BY_PROVIDER = {descriptor.provider_id: descriptor for descriptor in _CATALOG}
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
_DASHSCOPE_WORKSPACE = {
    "dashscope-cn": "cn-beijing.maas.aliyuncs.com",
    "dashscope-global": "ap-southeast-1.maas.aliyuncs.com",
}
_DASHSCOPE_SHARED = {
    "dashscope-cn": "dashscope.aliyuncs.com",
    "dashscope-global": "dashscope-intl.aliyuncs.com",
}
_AWS_REGIONS = frozenset({
    "us-east-1", "us-east-2", "us-west-2", "ca-central-1", "sa-east-1",
    "eu-central-1", "eu-north-1", "eu-south-1", "eu-west-1", "eu-west-2",
    "eu-west-3", "ap-northeast-1", "ap-northeast-2", "ap-northeast-3",
    "ap-south-1", "ap-southeast-1", "ap-southeast-2",
})
_VERTEX_LOCATIONS = frozenset({
    "global", "us-central1", "us-east1", "us-east4", "us-west1",
    "europe-west1", "europe-west4", "asia-northeast1", "asia-southeast1",
})


def get_catalog(*, personal_agent_runtime: bool = False) -> tuple[ProviderDescriptor, ...]:
    if personal_agent_runtime is not True:
        return _CATALOG
    return tuple(
        item.model_copy(update={"methods": tuple(
            method.model_copy(update={"selectable": True}) for method in item.methods)})
        if item.provider_id == "alibaba-coding-plan" else item for item in _CATALOG)


def get_descriptor(provider_id: str) -> ProviderDescriptor:
    try:
        return _BY_PROVIDER[provider_id]
    except KeyError:
        raise HTTPException(422, detail={"code": "provider_unavailable"}) from None


def validate_provider_model_protocol(
        provider_id: str, protocol_id: str, model_id: str) -> None:
    """Keep Go's documented endpoint dialect explicit; unknown models are not guessed."""
    if provider_id == "alibaba-coding-plan":
        if protocol_id not in {"openai_chat_completions", "anthropic_messages"} or model_id not in ALIBABA_MODELS:
            raise HTTPException(422, detail={"code": "invalid_model_protocol"})
        return
    if provider_id != "opencode-go":
        return
    known_protocol = _OPENCODE_GO_MODEL_PROTOCOL.get(model_id)
    if (protocol_id not in {"openai_chat_completions", "anthropic_messages"}
            or model_id in OPENCODE_GO_RESPONSES_MODELS
            or (known_protocol is not None and known_protocol != protocol_id)):
        raise HTTPException(422, detail={"code": "invalid_model_protocol"})


def launchable_auth_method(provider_id: str, method_id: str) -> AuthMethodDescriptor:
    """Return one statically reviewed method without probing native state."""
    descriptor = get_descriptor(provider_id)
    method = next(
        (candidate for candidate in descriptor.methods
         if candidate.method_id == method_id), None)
    if (method is None or method.code_state != "implemented"
            or method.external_eligibility != "verified"
            or method.selectable is not True):
        raise HTTPException(422, detail={"code": "auth_method_unavailable"})
    return method


def _invalid_config() -> None:
    raise HTTPException(422, detail={"code": "invalid_public_config"})


def _split_url(value: object):
    if (not isinstance(value, str) or value != value.strip() or "\\" in value
            or "?" in value or "#" in value
            or any(character.isspace() for character in value)):
        _invalid_config()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        _invalid_config()
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or (port is not None and port < 1)):
        _invalid_config()
    return parsed


def _valid_url(value: object, *, allow_local_http: bool, local_none: bool) -> bool:
    try:
        parsed = _split_url(value)
    except HTTPException:
        return False
    if parsed.scheme not in {"https", "http"}:
        return False
    loopback_http = parsed.scheme == "http" and parsed.hostname in _LOOPBACK
    if parsed.scheme == "http" and not (loopback_http and allow_local_http):
        return False
    return not local_none or loopback_http


def _bounded_dns_label(value: object) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 63
            and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value)
            is not None)


def _bounded_aws_profile(value: object) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 128
            and re.fullmatch(r"[A-Za-z0-9._/:\-]+", value) is not None)


def resolve_origin(provider_id: str, fields: dict | None = None, *, protocol_id: str | None = None) -> str:
    """Resolve a reviewed base URL without network, credential, or native calls."""
    descriptor = get_descriptor(provider_id)
    values = dict(fields or {})
    if provider_id == "alibaba-coding-plan":
        if values or protocol_id not in {"openai_chat_completions", "anthropic_messages"}:
            _invalid_config()
        return ALIBABA_CHAT_ORIGIN if protocol_id == "openai_chat_completions" else ALIBABA_MESSAGES_ORIGIN
    if provider_id == "azure-openai":
        if set(values) != {"resource_endpoint", "api_version"}:
            _invalid_config()
        endpoint = values["resource_endpoint"]
        try:
            parsed = _split_url(endpoint)
        except HTTPException:
            _invalid_config()
        host = parsed.hostname or ""
        resource = host.removesuffix(".openai.azure.com")
        if (parsed.scheme != "https" or parsed.port is not None
                or parsed.path not in {"", "/"}
                or not host.endswith(".openai.azure.com")
                or not resource or len(resource) > 63
                or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", resource) is None
                or values["api_version"] != "v1"):
            _invalid_config()
        return str(endpoint).rstrip("/")
    if provider_id == "amazon-bedrock":
        if (set(values) != {"profile_name", "region"}
                or not _bounded_aws_profile(values.get("profile_name"))
                or values.get("region") not in _AWS_REGIONS):
            _invalid_config()
        return f"https://bedrock-runtime.{values['region']}.amazonaws.com"
    if provider_id == "google-vertex":
        if (set(values) != {"project_id", "location"}
                or not _bounded_dns_label(values.get("project_id"))
                or values.get("location") not in _VERTEX_LOCATIONS):
            _invalid_config()
        location = values["location"]
        host = ("aiplatform.googleapis.com" if location == "global"
                else f"{location}-aiplatform.googleapis.com")
        return f"https://{host}"
    if provider_id == "compatible":
        base_url = values.get("base_url")
        if not _valid_url(
                base_url,
                allow_local_http=bool(values.get("allow_local_http")),
                local_none=False):
            _invalid_config()
        return str(base_url).rstrip("/")
    if provider_id in _DASHSCOPE_SHARED:
        base_url = values.get("base_url") or descriptor.default_base_url
        parsed = _split_url(base_url)
        if (parsed.scheme != "https" or parsed.port is not None
                or parsed.path != "/compatible-mode/v1"):
            _invalid_config()
        host = parsed.hostname or ""
        workspace_suffix = _DASHSCOPE_WORKSPACE[provider_id]
        if host != _DASHSCOPE_SHARED[provider_id]:
            suffix = "." + workspace_suffix
            workspace_id = host[:-len(suffix)] if host.endswith(suffix) else ""
            if (not workspace_id or "." in workspace_id
                    or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", workspace_id)):
                _invalid_config()
        return str(base_url)
    if provider_id in {"ollama", "lm-studio", "local-openai"}:
        base_url = values.get("base_url") or descriptor.default_base_url
        parsed = _split_url(base_url)
        if (parsed.scheme != "http" or parsed.hostname not in _LOOPBACK
                or parsed.port is None or parsed.path != "/v1"):
            _invalid_config()
        return str(base_url)
    if values or descriptor.default_base_url is None:
        _invalid_config()
    return descriptor.default_base_url


def validate_public_config(envelope: PublicConfigEnvelope, *, personal_agent_runtime: bool = False,
                           metadata_only: bool = False) -> ValidatedPublicConfig:
    descriptor = get_descriptor(envelope.provider_id)
    if envelope.protocol_id not in descriptor.protocols:
        _invalid_config()
    methods = {method.method_id: method for method in descriptor.methods}
    method = methods.get(envelope.auth_method_id)
    scoped = (envelope.provider_id == "alibaba-coding-plan"
              and (personal_agent_runtime is True or metadata_only is True))
    if (method is None or method.code_state != "implemented"
            or method.external_eligibility != "verified" or (not method.selectable and not scoped)):
        _invalid_config()
    field_descriptors = {field.field_id: field for field in descriptor.fields}
    if not set(envelope.fields) <= set(field_descriptors):
        _invalid_config()
    if any(field.required and field.field_id not in envelope.fields for field in descriptor.fields):
        _invalid_config()
    fields = dict(envelope.fields)
    if envelope.provider_id == "compatible":
        fields.setdefault("allow_local_http", False)
        if not isinstance(fields["allow_local_http"], bool):
            _invalid_config()
        if not _valid_url(
                fields.get("base_url"),
                allow_local_http=fields["allow_local_http"],
                local_none=envelope.auth_method_id == "local_none"):
            _invalid_config()
    else:
        resolve_origin(envelope.provider_id, fields, protocol_id=envelope.protocol_id)
    return ValidatedPublicConfig(
        version=envelope.version,
        provider_id=envelope.provider_id,
        protocol_id=envelope.protocol_id,
        auth_method_id=envelope.auth_method_id,
        fields=fields,
    )


def normalize_legacy_connection(row) -> NormalizedConnection:
    descriptor = get_descriptor(row.kind)
    auth_required = bool(getattr(row, "auth_required", True))
    method_id = "local_none" if row.kind == "compatible" and not auth_required else "api_key"
    public_config = {}
    if row.kind == "compatible":
        base_url = getattr(row, "base_url", None)
        if base_url is not None:
            public_config["base_url"] = base_url
        try:
            parsed = urlsplit(base_url or "")
            allow_local_http = parsed.scheme == "http" and parsed.hostname in _LOOPBACK
        except ValueError:
            allow_local_http = False
        public_config["allow_local_http"] = allow_local_http
    if not auth_required:
        credential_state = "not_required"
    elif getattr(row, "secret_ref", None):
        credential_state = "present"
    else:
        credential_state = "missing"
    return NormalizedConnection(
        provider_id=descriptor.provider_id,
        protocol_id=descriptor.protocols[0],
        auth_method_id=method_id,
        public_config_version=1,
        public_config=public_config,
        auth_grant_revision=0,
        credential_state=credential_state,
    )
