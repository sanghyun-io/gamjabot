"""Connection ownership, secret lifecycle, and a small explicit provider probe."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import wraps
from ipaddress import ip_address
import json
from threading import RLock
import re
import socket
import struct
import zlib
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy import delete as sql_delete, inspect, select, text, update as sql_update
from sqlalchemy.orm import Session

from app.models.collaboration import ConnectionModelCache, ProviderConnection
from app.schemas.provider_catalog import PublicConfigEnvelope
from app.schemas.provider_models import MODEL_ID_PATTERN, valid_model_id
from app.services import provider_catalog
from app.services.secrets import (CoreSecretStore, PlatformSecretStore, SecretInteractionRequired,
                                  SecretReentryRequired, SecretStoreUnavailable)


# Leave room for reasoning before the short answer, while keeping an explicit
# connection test small. An eight-token ceiling can return HTTP200 without text.
CONNECTION_TEXT_PROBE_MAX_TOKENS = 256
CONNECTION_PROBE_DEADLINE_SECONDS = 30


class ConnectionFailure(Exception):
    def __init__(self, code: str, message: str, credential_error: SecretStoreUnavailable | None = None):
        self.code, self.message = code, message
        self.credential_error = credential_error
        super().__init__(code)


MESSAGES = {
    'personal_agent_runtime_required': 'Use this connection in the local Agent app or CLI.',
    'missing_secret': 'Credential is missing. Re-enter the API key.',
    'missing_model': 'Set an exact model ID before using this connection.',
    'secret_store_unavailable': 'Operating system credential store is unavailable.',
    'invalid_credential': 'Provider rejected the credential.',
    'invalid_url': 'Enter a safe HTTPS URL, or explicitly allow a local loopback HTTP URL.',
    'invalid_configuration': 'Connection settings are incomplete or invalid.',
    'storage_unavailable': 'Connection storage is unavailable. Check the saved connection before retrying.',
    'credential_cleanup_unconfirmed': 'Credential cleanup could not be confirmed. Check the saved connection before retrying.',
    'unreachable': 'Provider endpoint could not be reached.',
    'redirect_refused': 'Provider endpoint redirected the request; check its URL.',
    'provider_error': 'Provider rejected the connection test.',
    'rate_limited': 'Provider rate limit reached. Try again later.',
    'quota_exhausted': 'OpenCode Go usage is exhausted. Wait for usage to recover and check the connection settings.',
    'invalid_response': 'Provider returned an invalid response.',
    'connection_disabled': 'Enable this connection before testing or using it.',
    'credential_locked': 'Unlock this connection credential in Connections before sending a message.',
    'credential_reentry_required': 'Re-enter the API key for this saved connection.',
    'credential_operation_pending': 'Credential recovery is still in progress.',
    'configuration_changed': 'Connection changed while testing. Run the test again.',
    'owner_epoch_unavailable': 'Core owner binding is unavailable.',
    'provider_immutable': 'Create another connection to use a different provider.',
    'invalid_configuration': 'Provider connection configuration is invalid.',
}


def fail(code: str, status: int = 400):
    raise HTTPException(status_code=status, detail={'code': code, 'message': MESSAGES[code]})


def validate_url(kind: str, raw: str | None, local_allowed: bool, auth_required: bool) -> tuple[str | None, bool]:
    if kind != 'compatible' and (raw is not None or local_allowed or not auth_required):
        fail('invalid_url', 422)
    if kind != 'compatible' and raw is None:
        if not auth_required or local_allowed:
            fail('invalid_url', 422)
        return None, False
    if kind == 'compatible' and not raw:
        fail('invalid_url', 422)
    if not raw:
        fail('invalid_url', 422)
    try:
        parsed = urlsplit(raw)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        fail('invalid_url', 422)
    if (parsed.scheme not in ('https', 'http') or not host or parsed.username is not None or parsed.password is not None
            or '?' in raw or '#' in raw or '\\' in raw or not (re.fullmatch(r'[A-Za-z0-9.-]+', host) or host == '::1')
            or raw != raw.strip() or any(c.isspace() for c in raw)
            or (port is not None and port < 1)):
        fail('invalid_url', 422)
    local = parsed.scheme == 'http' and host in ('localhost', '127.0.0.1', '::1')
    if parsed.scheme == 'http' and not (local and local_allowed and kind == 'compatible'):
        fail('invalid_url', 422)
    if local_allowed and not local:
        fail('invalid_url', 422)
    if not auth_required and not local:
        fail('invalid_url', 422)
    if parsed.scheme == 'https' and host in ('localhost', '127.0.0.1', '::1'):
        local = True
    if parsed.scheme == 'https' and not auth_required:
        fail('invalid_url', 422)
    # Keep a single canonical origin and path; never retain query or userinfo.
    authority = host if ':' not in host else f'[{host}]'
    if port is not None:
        authority += f':{port}'
    return urlunsplit((parsed.scheme, authority, parsed.path.rstrip('/'), '', '')), local


def owned(db: Session, connection_id: int, owner_id: int) -> ProviderConnection:
    row = db.query(ProviderConnection).filter_by(id=connection_id, user_id=owner_id).first()
    if row is None:
        raise HTTPException(404, 'Connection not found')
    return row


def public(row: ProviderConnection) -> dict:
    result = {'id': row.id, 'owner_id': row.user_id, 'provider_kind': row.kind,
              'label': row.name, 'base_url': row.base_url, 'default_model_id': row.default_model_id,
              'enabled': bool(row.enabled), 'auth_required': bool(row.auth_required),
              'has_secret': bool(row.secret_ref),
              'is_local': bool(row.base_url and urlsplit(row.base_url).hostname in ('localhost', '127.0.0.1', '::1')),
              'capabilities': row.capabilities or {'declared': {}, 'verified': {}}}
    if row.provider_id is not None:
        result.update({
            'version': row.public_config_version,
            'provider_id': row.provider_id,
            'protocol_id': row.protocol_id,
            'auth_method_id': row.auth_method_id,
            'fields': deepcopy(row.public_config or {}),
            'config_revision': int(row.readiness_revision or 1),
            'auth_grant_revision': int(row.auth_grant_revision or 0),
            'credential_state': row.credential_state,
        })
    if row.provider_id == 'chatgpt':
        from app.services.chatgpt_oauth import manager
        try:result['chatgpt_account'] = manager().account(row)
        except HTTPException:result['chatgpt_account'] = None
    return result


def store_for(app):
    store = (app.state.secret_store if hasattr(app.state, 'secret_store')
             else PlatformSecretStore())
    coordinator = getattr(app.state, 'connection_recovery', None)
    if coordinator is not None:
        return _CredentialServices(store, coordinator)
    return store


@dataclass(repr=False)
class _CredentialServices:
    store: object
    coordinator: object

    def __getattr__(self, name):
        return getattr(self.store, name)


def personal_agent_runtime(store) -> bool:
    if type(store) is _CredentialServices:
        store = store.store
    return (type(store) is CoreSecretStore and store.namespace == 'provider'
            and store.personal_agent_runtime is True)


def _personal_agent_connection(provider_id, base_url) -> bool:
    try:
        host = urlsplit(base_url or '').hostname
    except ValueError:
        host = None
    return (provider_id == 'alibaba-coding-plan'
            or host in {'coding-intl.dashscope.aliyuncs.com', 'coding.dashscope.aliyuncs.com'})


def _require_personal_agent_runtime(store, provider_id, base_url) -> bool:
    trusted = personal_agent_runtime(store)
    if _personal_agent_connection(provider_id, base_url) and not trusted:
        raise ConnectionFailure('personal_agent_runtime_required',
                                MESSAGES['personal_agent_runtime_required'])
    return trusted


def _bind_personal_agent_runtime(config, store):
    config.personal_agent_runtime = _require_personal_agent_runtime(
        store, config.provider_id, config.base_url)
    config.personal_agent_binding = store
    return config


def _direct_coordinator(store):
    return getattr(store, 'coordinator', None)


def _commit_direct_api_key(db: Session, row: ProviderConnection, secret: str, store):
    coordinator = _direct_coordinator(store)
    if coordinator is None:
        raise ConnectionFailure(
            'owner_epoch_unavailable', MESSAGES['owner_epoch_unavailable'])
    try:
        result = coordinator.commit_direct_api_key(db, row, secret)
    except Exception as error:
        from app.services.auth_operations import AuthOperationError
        from app.services.core_auth_broker import CoreAuthBrokerUnavailable
        if isinstance(error, AuthOperationError):
            code = error.code if error.code in MESSAGES else 'credential_operation_pending'
            raise ConnectionFailure(code, MESSAGES[code]) from None
        if isinstance(error, CoreAuthBrokerUnavailable):
            raise ConnectionFailure(
                'owner_epoch_unavailable', MESSAGES['owner_epoch_unavailable']) from None
        raise
    if result.state != 'succeeded':
        raise ConnectionFailure(
            'credential_operation_pending', MESSAGES['credential_operation_pending'])
    try:
        db.expire_all()
        committed = db.get(ProviderConnection, row.id)
        db.refresh(committed)
        return committed
    except Exception:
        raise ConnectionFailure(
            'storage_unavailable', MESSAGES['storage_unavailable']) from None


def is_core_managed_credential(value) -> bool:
    """Identify the exact public triplets whose credential stays opaque in Core."""
    provider_id = getattr(value, 'provider_id', None)
    protocol_id = getattr(value, 'protocol_id', None)
    auth_method_id = getattr(value, 'auth_method_id', None)
    if auth_method_id == 'api_key':
        return (provider_id, protocol_id) == ('azure-openai', 'azure_openai')
    selected = _NATIVE_CONTRACTS.get(provider_id)
    return (auth_method_id == 'native_chain' and selected is not None
            and protocol_id == selected[0])


@dataclass(repr=False)
class ConnectionConfig:
    connection_id: int
    provider_kind: str
    model_id: str
    base_url: str | None
    api_key: str | None
    capabilities: dict
    provider_id: str | None = None
    protocol_id: str | None = None
    auth_method_id: str | None = None
    public_config_version: int | None = None
    public_fields: dict | None = None
    auth_grant_revision: int | None = None
    credential_state: str | None = None
    credential_ref: str | None = None
    owner_id: int | None = None
    owner_epoch: str | None = None
    config_revision: int | None = None
    public_config_fingerprint: str | None = None
    descriptor_revision: int | None = None
    personal_agent_runtime: bool = False
    personal_agent_binding: object | None = None


@dataclass(frozen=True)
class ConnectionContract:
    provider_id: str
    protocol_id: str
    auth_method_id: str | None
    public_fields: dict
    legacy: bool


_LEGACY_PROVIDER_IDS = frozenset({'openai', 'anthropic', 'gemini', 'xai', 'compatible'})
_CREDENTIAL_STATES = frozenset({
    'missing', 'authorizing', 'present', 'refresh_required', 'rejected',
    'locked', 'unavailable', 'not_required',
})
_NATIVE_CONTRACTS = {
    'azure-openai': ('azure_openai', frozenset({'resource_endpoint', 'api_version'})),
    'amazon-bedrock': ('aws_bedrock_converse', frozenset({'profile_name', 'region'})),
    'google-vertex': ('vertex_generate_content', frozenset({'project_id', 'location'})),
}


def _native_connection_contract(config: ConnectionConfig) -> ConnectionContract | None:
    if not is_core_managed_credential(config):
        return None
    selected = _NATIVE_CONTRACTS.get(config.provider_id)
    fields = config.public_fields
    if (selected is None or config.protocol_id != selected[0]
            or config.public_config_version != 1 or config.base_url is not None
            or not isinstance(fields, dict) or set(fields) != selected[1]
            or any(not isinstance(value, str) or not value or value != value.strip()
                   or len(value.encode('utf-8')) > 2048 for value in fields.values())):
        raise ConnectionFailure('invalid_configuration', MESSAGES['invalid_configuration'])
    if config.provider_id == 'azure-openai':
        try:
            endpoint = urlsplit(fields['resource_endpoint'])
            if (endpoint.scheme != 'https' or not endpoint.hostname
                    or endpoint.username is not None or endpoint.password is not None
                    or endpoint.port is not None or endpoint.path not in {'', '/'}
                    or endpoint.query or endpoint.fragment
                    or not endpoint.hostname.endswith('.openai.azure.com')
                    or fields['api_version'] != 'v1'):
                raise ValueError()
        except (TypeError, ValueError):
            raise ConnectionFailure(
                'invalid_configuration', MESSAGES['invalid_configuration']) from None
    return ConnectionContract(
        config.provider_id, config.protocol_id, config.auth_method_id,
        dict(fields), False,
    )


def _validate_persisted_auth_semantics(
        contract: ConnectionContract, *, auth_required: bool | None,
        has_secret: bool | None) -> None:
    if contract.legacy or (auth_required is None and has_secret is None):
        return
    if type(auth_required) is not bool or type(has_secret) is not bool:
        raise ConnectionFailure('invalid_configuration', MESSAGES['invalid_configuration'])
    expected_auth = contract.auth_method_id != 'local_none'
    if auth_required != expected_auth or (not expected_auth and has_secret):
        raise ConnectionFailure('invalid_configuration', MESSAGES['invalid_configuration'])


def validated_connection_contract(
        config: ConnectionConfig, *, auth_required: bool | None = None,
        has_secret: bool | None = None) -> ConnectionContract:
    identity_metadata = (
        config.provider_id, config.protocol_id, config.auth_method_id,
        config.public_config_version, config.public_fields,
    )
    if all(value is None for value in identity_metadata):
        if config.provider_kind not in _LEGACY_PROVIDER_IDS:
            raise ConnectionFailure('invalid_configuration', MESSAGES['invalid_configuration'])
        if (config.auth_grant_revision is not None
                and (type(config.auth_grant_revision) is not int
                     or config.auth_grant_revision < 0)):
            raise ConnectionFailure('invalid_configuration', MESSAGES['invalid_configuration'])
        if config.credential_state is not None and config.credential_state not in _CREDENTIAL_STATES:
            raise ConnectionFailure('invalid_configuration', MESSAGES['invalid_configuration'])
        try:
            descriptor = provider_catalog.get_descriptor(config.provider_kind)
        except HTTPException:
            raise ConnectionFailure('invalid_configuration', MESSAGES['invalid_configuration']) from None
        fields = {}
        if config.provider_kind == 'compatible':
            fields = {
                'base_url': config.base_url,
                'allow_local_http': bool(config.base_url and config.base_url.startswith('http://')),
            }
        contract = ConnectionContract(
            descriptor.provider_id, descriptor.protocols[0], None, fields, True,
        )
        _validate_persisted_auth_semantics(
            contract, auth_required=auth_required, has_secret=has_secret)
        return contract
    if any(value is None for value in identity_metadata) or any(
            value is None for value in (config.auth_grant_revision, config.credential_state)):
        raise ConnectionFailure('invalid_configuration', MESSAGES['invalid_configuration'])
    if (type(config.auth_grant_revision) is not int or config.auth_grant_revision < 0
            or config.credential_state not in _CREDENTIAL_STATES):
        raise ConnectionFailure('invalid_configuration', MESSAGES['invalid_configuration'])
    native = _native_connection_contract(config)
    if native is not None:
        _validate_persisted_auth_semantics(
            native, auth_required=auth_required, has_secret=has_secret)
        return native
    try:
        fields = deepcopy(config.public_fields)
        validated = provider_catalog.validate_public_config(
            provider_catalog.PublicConfigEnvelope(
                version=config.public_config_version,
                provider_id=config.provider_id,
                protocol_id=config.protocol_id,
                auth_method_id=config.auth_method_id,
                fields=fields,
            ), metadata_only=True,
        )
        if (config.base_url is not None
                and provider_catalog.resolve_origin(
                    validated.provider_id, validated.fields,
                    protocol_id=validated.protocol_id) != config.base_url):
            raise ValueError()
    except Exception:
        raise ConnectionFailure('invalid_configuration', MESSAGES['invalid_configuration']) from None
    if ((validated.auth_method_id == 'local_none')
            != (config.credential_state == 'not_required')):
        raise ConnectionFailure('invalid_configuration', MESSAGES['invalid_configuration'])
    contract = ConnectionContract(
        validated.provider_id, validated.protocol_id, validated.auth_method_id,
        dict(validated.fields), False,
    )
    _validate_persisted_auth_semantics(
        contract, auth_required=auth_required, has_secret=has_secret)
    return contract


def provider_identity(config: ConnectionConfig) -> tuple[str, str]:
    contract = validated_connection_contract(config)
    return contract.provider_id, contract.protocol_id


def provider_public_fields(config: ConnectionConfig) -> dict:
    return validated_connection_contract(config).public_fields


def _pin_loopback_origin(origin: str) -> str:
    parsed = urlsplit(origin)
    host = parsed.hostname
    if parsed.scheme != 'http' or host not in {'localhost', '127.0.0.1', '::1'}:
        return origin
    if host != 'localhost':
        try:
            if not ip_address(host).is_loopback:
                raise ValueError()
        except ValueError:
            raise ConnectionFailure('invalid_configuration', MESSAGES['invalid_configuration']) from None
        return origin
    try:
        answers = socket.getaddrinfo(
            host, parsed.port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM,
        )
        addresses = [ip_address(answer[4][0]) for answer in answers]
    except (OSError, ValueError, IndexError, TypeError):
        raise ConnectionFailure('invalid_configuration', MESSAGES['invalid_configuration']) from None
    if not addresses or any(not address.is_loopback for address in addresses):
        raise ConnectionFailure('invalid_configuration', MESSAGES['invalid_configuration'])
    selected_address = next(
        (address for address in addresses if str(address) == '127.0.0.1'),
        next((address for address in addresses if address.version == 4), addresses[0]),
    )
    selected = str(selected_address)
    authority = f'[{selected}]' if ':' in selected else selected
    if parsed.port is not None:
        authority += f':{parsed.port}'
    return urlunsplit((parsed.scheme, authority, parsed.path, '', ''))


def provider_runtime_origin(config: ConnectionConfig) -> tuple[ConnectionContract, str]:
    if (_personal_agent_connection(config.provider_id, config.base_url)
            and (config.personal_agent_runtime is not True
                 or not personal_agent_runtime(config.personal_agent_binding))):
        raise ConnectionFailure('personal_agent_runtime_required',
                                MESSAGES['personal_agent_runtime_required'])
    contract = validated_connection_contract(config)
    try:
        if contract.provider_id == 'compatible':
            origin, _ = validate_url(
                'compatible', config.base_url,
                bool(config.base_url and config.base_url.startswith('http://')),
                bool(config.api_key),
            )
        else:
            origin = provider_catalog.resolve_origin(
                contract.provider_id, contract.public_fields, protocol_id=contract.protocol_id)
    except (HTTPException, ConnectionFailure):
        raise ConnectionFailure('invalid_configuration', MESSAGES['invalid_configuration']) from None
    return contract, _pin_loopback_origin(origin)


class PhysicalRequestBudget:
    """One explicit Test's physical model-attempt total and truth state."""

    __slots__ = ('maximum', '_count', '_pending', '_unknown')

    def __init__(self, maximum: int):
        if type(maximum) is not int or not 0 <= maximum <= 7:
            raise ValueError('invalid physical request budget')
        self.maximum = maximum
        self._count = 0
        self._pending = False
        self._unknown = False

    @property
    def actual_count(self) -> int | None:
        return None if self._unknown or self._pending else self._count

    @property
    def remaining(self) -> int:
        if self._unknown or self._pending:
            raise ConnectionFailure('provider_error', MESSAGES['provider_error'])
        return self.maximum - self._count

    def try_admit(self, count: int = 1) -> None:
        if (type(count) is not int or count < 1 or self._unknown or self._pending
                or self._count + count > self.maximum):
            raise ConnectionFailure('provider_error', MESSAGES['provider_error'])
        self._count += count

    def open_core_slot(self) -> int:
        remaining = self.remaining
        self._pending = True
        return remaining

    def close_core_slot(self, count: int) -> None:
        remaining = self.maximum - self._count
        if (not self._pending or type(count) is not int or count < 0
                or count > remaining):
            self.mark_unknown()
            raise ConnectionFailure('provider_error', MESSAGES['provider_error'])
        self._pending = False
        self._count += count

    def mark_unknown(self) -> None:
        self._pending = False
        self._unknown = True


class RequestMeter(httpx.AsyncBaseTransport):
    """Count every physical provider request while keeping retries explicit."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, *,
                 physical_request_budget: PhysicalRequestBudget | None = None):
        self._transport = transport or httpx.AsyncHTTPTransport(retries=0)
        self._owns_transport = transport is None
        self.count = 0
        self.physical_request_budget = physical_request_budget

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.physical_request_budget is not None:
            self.physical_request_budget.try_admit()
        self.count += 1
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        # Probe and provider adapters each own a short-lived AsyncClient, but all
        # of those clients share this meter for one explicit Test operation.
        pass

    async def close_owned_transport(self) -> None:
        if self._owns_transport:
            await self._transport.aclose()


def test_request_limit(provider_kind: str, capabilities: dict | None, profile: str) -> int:
    """Return the canonical maximum physical requests for one explicit Test."""
    if profile == 'text':
        return 1
    declared = (capabilities or {}).get('declared', {})
    return (1
            + int(provider_kind == 'compatible' and declared.get('max_tokens') is True)
            + int(declared.get('temperature') is True)
            + 2 * int(declared.get('tools') is True)
            + 2 * int(declared.get('vision') is True))


def _cache_key(owner_id: int, connection_id: int, reference: str) -> tuple:
    return (owner_id, connection_id, reference)


def resolve_connection(db: Session, connection_id: int, owner_id: int, store=None, cache=None) -> ConnectionConfig:
    config, reference = _connection_metadata(db, connection_id, owner_id)
    _bind_personal_agent_runtime(config, store)
    if config.provider_id == 'chatgpt':
        from app.services.chatgpt_oauth import manager
        config.api_key = manager().token(config)
        return config
    if is_core_managed_credential(config):
        return config
    if reference:
        try:
            key = _cache_key(owner_id, connection_id, reference)
            config.api_key = cache.get(key) if cache else None
            if not config.api_key:
                generation = cache.reserve(key) if cache else None
                config.api_key = (store or PlatformSecretStore()).get_no_ui(reference)
                if config.api_key and cache:
                    cache.put_if_current(key, generation, config.api_key)
        except SecretReentryRequired:
            raise ConnectionFailure('missing_secret', MESSAGES['credential_reentry_required']) from None
        except SecretInteractionRequired:
            raise ConnectionFailure('credential_locked', MESSAGES['credential_locked']) from None
        except SecretStoreUnavailable as error:
            raise ConnectionFailure(
                'secret_store_unavailable', MESSAGES['secret_store_unavailable'], error) from None
        if not config.api_key:
            raise ConnectionFailure('missing_secret', MESSAGES['missing_secret'])
    return config


def _connection_metadata(db: Session, connection_id: int, owner_id: int,
                         model_id_override: str | None = None) -> tuple[ConnectionConfig, str | None]:
    """Snapshot owner-checked SQL data before any await or worker-thread access."""
    _assert_connection_resolvable(db, owner_id, connection_id)
    row = owned(db, connection_id, owner_id)
    model_id = row.default_model_id if model_id_override is None else model_id_override
    if not model_id:
        raise ConnectionFailure('missing_model', MESSAGES['missing_model'])
    if not valid_model_id(model_id):
        raise ConnectionFailure('invalid_configuration', MESSAGES['invalid_configuration'])
    config = ConnectionConfig(
        row.id, row.kind, model_id, row.base_url, None, deepcopy(row.capabilities or {}),
        provider_id=row.provider_id,
        protocol_id=row.protocol_id,
        auth_method_id=row.auth_method_id,
        public_config_version=row.public_config_version,
        public_fields=deepcopy(row.public_config),
        auth_grant_revision=row.auth_grant_revision,
        credential_state=row.credential_state,
        owner_id=row.user_id,
        # Every inference proposal needs the public revision frozen before
        # provider/credential awaits, including legacy HTTP connections.
        config_revision=row.readiness_revision,
    )
    from app.services.model_features import bind_saved_text_test
    bind_saved_text_test(db,row,config)
    validated_connection_contract(
        config, auth_required=bool(row.auth_required), has_secret=bool(row.secret_ref))
    if not row.enabled:
        raise ConnectionFailure('connection_disabled', MESSAGES['connection_disabled'])
    if row.auth_required and not row.secret_ref:
        raise ConnectionFailure('missing_secret', MESSAGES['missing_secret'])
    if is_core_managed_credential(config):
        from app.services.connection_recovery import _public_config_fingerprint
        config.credential_ref = row.secret_ref
        config.owner_id = row.user_id
        config.public_config_fingerprint = _public_config_fingerprint(
            row, config.auth_method_id)
        config.descriptor_revision = 1
    return config, row.secret_ref if row.auth_required else None


_secret_read_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='gamja-secret-read')


def _read_secret_no_ui(store, reference: str) -> str | None:
    return (store or PlatformSecretStore()).get_no_ui(reference)


async def resolve_connection_async(db: Session, connection_id: int, owner_id: int, store=None,
                                   release_snapshot_transaction: bool = False, cache=None,
                                   model_id_override: str | None = None) -> ConnectionConfig:
    config, reference = _connection_metadata(db, connection_id, owner_id, model_id_override)
    _bind_personal_agent_runtime(config, store)
    if release_snapshot_transaction:
        # Connection tests can spend seconds in the OS store or provider probes.
        # Their later capability write uses a fresh, conditional transaction.
        db.rollback()
    if config.provider_id == 'chatgpt':
        from app.services.chatgpt_oauth import manager
        config.api_key = await asyncio.to_thread(manager().token, config)
        return config
    if is_core_managed_credential(config):
        return config
    if reference:
        key = _cache_key(owner_id, connection_id, reference)
        config.api_key = cache.get(key) if cache else None
        if config.api_key:
            return config
        generation = cache.reserve(key) if cache else None
        try:
            config.api_key = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(_secret_read_pool, _read_secret_no_ui, store, reference), 5)
        except SecretReentryRequired:
            raise ConnectionFailure('missing_secret', MESSAGES['credential_reentry_required']) from None
        except SecretInteractionRequired:
            raise ConnectionFailure('credential_locked', MESSAGES['credential_locked']) from None
        except SecretStoreUnavailable as error:
            raise ConnectionFailure(
                'secret_store_unavailable', MESSAGES['secret_store_unavailable'], error) from None
        except Exception:
            raise ConnectionFailure('secret_store_unavailable', MESSAGES['secret_store_unavailable']) from None
        if not config.api_key:
            raise ConnectionFailure('missing_secret', MESSAGES['missing_secret'])
        if cache:
            cache.put_if_current(key, generation, config.api_key)
    return config


def bind_native_runtime(config: ConnectionConfig, owner_epoch: str | None) -> ConnectionConfig:
    """Attach the live Core owner epoch without resolving an opaque credential ref."""
    if not is_core_managed_credential(config):
        return config
    if (not isinstance(owner_epoch, str) or len(owner_epoch) != 64
            or any(character not in '0123456789abcdef' for character in owner_epoch)):
        raise ConnectionFailure(
            'owner_epoch_unavailable', MESSAGES['owner_epoch_unavailable'])
    validated_connection_contract(config, auth_required=True, has_secret=True)
    return replace(config, owner_epoch=owner_epoch)


def _discovery_metadata(db: Session, connection_id: int, owner_id: int) -> tuple[ConnectionConfig, str | None]:
    """Take a model-less credential snapshot without weakening inference resolution."""
    _assert_connection_resolvable(db, owner_id, connection_id)
    row = owned(db, connection_id, owner_id)
    config = ConnectionConfig(
        row.id, row.kind, "", row.base_url, None, deepcopy(row.capabilities or {}),
        provider_id=row.provider_id,
        protocol_id=row.protocol_id,
        auth_method_id=row.auth_method_id,
        public_config_version=row.public_config_version,
        public_fields=deepcopy(row.public_config),
        auth_grant_revision=row.auth_grant_revision,
        credential_state=row.credential_state,
        owner_id=row.user_id,
        config_revision=row.readiness_revision,
    )
    validated_connection_contract(
        config, auth_required=bool(row.auth_required), has_secret=bool(row.secret_ref))
    if not row.enabled:
        raise ConnectionFailure('connection_disabled', MESSAGES['connection_disabled'])
    if row.auth_required and not row.secret_ref:
        raise ConnectionFailure('missing_secret', MESSAGES['missing_secret'])
    return config, row.secret_ref if row.auth_required else None


def _assert_connection_resolvable(db: Session, owner_id: int, connection_id: int) -> None:
    """Block before cache, OS credential, discovery, Test, or provider access."""
    from app.services.connection_recovery import (
        CredentialOperationPending, assert_connection_resolvable,
    )

    try:
        assert_connection_resolvable(db, owner_id, connection_id)
    except CredentialOperationPending:
        raise ConnectionFailure(
            'credential_operation_pending', MESSAGES['credential_operation_pending']) from None


async def resolve_discovery_connection_async(db: Session, connection_id: int, owner_id: int,
                                             store=None, cache=None) -> ConnectionConfig:
    config, reference = _discovery_metadata(db, connection_id, owner_id)
    _bind_personal_agent_runtime(config, store)
    db.rollback()
    if config.provider_id == 'chatgpt':
        from app.services.chatgpt_oauth import manager
        config.api_key = await asyncio.to_thread(manager().token, config)
        return config
    if is_core_managed_credential(config):
        return config
    if reference:
        key = _cache_key(owner_id, connection_id, reference)
        config.api_key = cache.get(key) if cache else None
        if not config.api_key:
            generation = cache.reserve(key) if cache else None
            try:
                config.api_key = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        _secret_read_pool, _read_secret_no_ui, store, reference), 5)
            except SecretReentryRequired:
                raise ConnectionFailure('missing_secret', MESSAGES['credential_reentry_required']) from None
            except SecretInteractionRequired:
                raise ConnectionFailure('credential_locked', MESSAGES['credential_locked']) from None
            except SecretStoreUnavailable as error:
                raise ConnectionFailure(
                    'secret_store_unavailable', MESSAGES['secret_store_unavailable'], error) from None
            except Exception:
                raise ConnectionFailure(
                    'secret_store_unavailable', MESSAGES['secret_store_unavailable']) from None
            if not config.api_key:
                raise ConnectionFailure('missing_secret', MESSAGES['missing_secret'])
            if cache:
                cache.put_if_current(key, generation, config.api_key)
    return config


async def discover_models(config: ConnectionConfig, transport=None):
    """Fetch one reviewed model-list endpoint and return bounded public facts."""
    contract, origin = provider_runtime_origin(config)
    descriptor = provider_catalog.get_descriptor(contract.provider_id)
    source = descriptor.model_source.strategy
    if source == 'configured':
        items = [{'id': model_id, 'label': model_id, 'source': 'configured'}
                 for model_id in descriptor.model_source.static_model_ids]
        return items, False, None
    if contract.provider_id == 'ollama':
        endpoint = origin.removesuffix('/v1') + '/api/tags'
    elif contract.provider_id == 'lm-studio':
        endpoint = origin.rstrip('/') + '/models'
    elif contract.provider_id in {'dashscope-cn', 'dashscope-global'}:
        endpoint = origin.removesuffix('/compatible-mode/v1') + '/api/v1/models'
    else:
        endpoint = origin.rstrip('/') + '/models'
    headers = ({'Authorization': f'Bearer {config.api_key}'} if config.api_key else {})
    try:
        async with httpx.AsyncClient(
                transport=transport, trust_env=False, follow_redirects=False, timeout=5) as client:
            async with client.stream('GET', endpoint, headers=headers) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 1_000_000:
                        raise ConnectionFailure('invalid_response', 'Model refresh failed.')
    except (httpx.HTTPError, ValueError):
        raise ConnectionFailure('unreachable', 'Model refresh failed.') from None
    if 300 <= response.status_code < 400:
        raise ConnectionFailure('redirect_refused', 'Model refresh failed.')
    if response.status_code in (401, 403):
        raise ConnectionFailure('invalid_credential', 'Model refresh failed.')
    if response.status_code == 429:
        raise ConnectionFailure('rate_limited', 'Model refresh failed.')
    if response.status_code >= 400:
        raise ConnectionFailure('provider_error', 'Model refresh failed.')
    try:
        payload = json.loads(content)
        rows = payload['models'] if contract.provider_id in {'ollama','chatgpt'} else payload['data']
        if not isinstance(rows, list):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise ConnectionFailure('invalid_response', 'Model refresh failed.') from None
    model_ids = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if contract.provider_id == 'chatgpt' and row.get('visibility') != 'list': continue
        model_id = (row.get('slug') if contract.provider_id == 'chatgpt' else (row.get('name') or row.get('model')) if contract.provider_id == 'ollama' else row.get('id'))
        if (isinstance(model_id, str) and model_id == model_id.strip()
                and re.fullmatch(MODEL_ID_PATTERN, model_id) and len(model_id) <= 200):
            model_ids.append(model_id)
    if contract.provider_id == 'chatgpt':
        names={item.get('slug'):item.get('display_name') for item in rows if isinstance(item,dict)}
        ordered=list(dict.fromkeys(model_ids))
        return ([{'id':name,'label':names.get(name) if isinstance(names.get(name),str) and 0<len(names[name])<=200 else name,'source':'discovery'} for name in ordered[:200]], len(ordered)>200, datetime.now(UTC))
    unique = sorted(set(model_ids))
    truncated = len(unique) > 200
    unique = unique[:200]
    items = [{'id': model_id, 'label': model_id, 'source': source} for model_id in unique]
    return items, truncated, datetime.now(UTC)


_lifecycle_lock = RLock()


def synchronized(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        with _lifecycle_lock:
            return function(*args, **kwargs)
    return wrapper


def _compensate_unreferenced_secret(db: Session, bind, connection_id: int | None, ref: str | None, store) -> None:
    try:
        db.rollback()
    except Exception:
        pass  # A post-commit hook can leave this Session unable to roll back.
    if not ref or connection_id is None:
        return
    try:
        with Session(bind=bind) as check:
            saved_ref = check.scalar(select(ProviderConnection.secret_ref).where(ProviderConnection.id == connection_id))
    except Exception:
        return  # Uncertain commit: retain a possible orphan, never delete a referenced key.
    if saved_ref != ref:
        try:
            store.delete(ref)
        except SecretStoreUnavailable:
            pass


def _save_failure(error: Exception) -> None:
    if isinstance(error, SecretStoreUnavailable):
        raise error from None
    raise ConnectionFailure('storage_unavailable', MESSAGES['storage_unavailable']) from None


def _legacy_kind(provider_id: str) -> str:
    return provider_id if provider_id in {'openai', 'anthropic', 'gemini', 'xai'} else 'compatible'


def _validated_versioned(data, *, metadata_only=False):
    return provider_catalog.validate_public_config(PublicConfigEnvelope(
        version=data.version,
        provider_id=data.provider_id,
        protocol_id=data.protocol_id,
        auth_method_id=data.auth_method_id,
        fields=data.fields,
    ), metadata_only=metadata_only)


def _grant_revision(row: ProviderConnection) -> int:
    return int(row.auth_grant_revision or 0)


def _confirmed_committed_connection(bind, *, owner_id: int, connection_id: int,
                                    secret_ref: str | None, config_revision: int,
                                    auth_grant_revision: int,
                                    auth_method_id: str | None = None):
    try:
        with Session(bind=bind) as check:
            saved = check.query(ProviderConnection).filter_by(
                id=connection_id, user_id=owner_id).first()
            if (saved is None or saved.secret_ref != secret_ref
                    or int(saved.readiness_revision or 1) != config_revision
                    or int(saved.auth_grant_revision or 0) != auth_grant_revision
                    or (auth_method_id is not None
                        and saved.auth_method_id != auth_method_id)):
                return None
            check.expunge(saved)
            return saved
    except Exception:
        return None


@synchronized
def create(db: Session, owner_id: int, data, store) -> ProviderConnection:
    versioned = hasattr(data, 'version')
    if versioned:
        contract = _validated_versioned(data, metadata_only=True)
        if contract.provider_id == 'chatgpt' and data.api_key: fail('invalid_configuration', 422)
        auth_required = contract.auth_method_id != 'local_none'
        if auth_required and not data.api_key and contract.provider_id != 'chatgpt':
            raise ConnectionFailure('missing_secret', MESSAGES['missing_secret'])
        if not auth_required and data.api_key:
            fail('invalid_configuration', 422)
        direct = (contract.provider_id == 'azure-openai'
                  and contract.protocol_id == 'azure_openai'
                  and contract.auth_method_id == 'api_key')
        resolved_origin = provider_catalog.resolve_origin(
            contract.provider_id, contract.fields, protocol_id=contract.protocol_id)
        _require_personal_agent_runtime(store, contract.provider_id, resolved_origin)
        provider_catalog.validate_provider_model_protocol(
            contract.provider_id, contract.protocol_id, data.default_model_id.strip())
        base_url = None if direct else resolved_origin
        if direct and _direct_coordinator(store) is None:
            raise ConnectionFailure(
                'owner_epoch_unavailable', MESSAGES['owner_epoch_unavailable'])
        row = ProviderConnection(
            user_id=owner_id, kind=_legacy_kind(contract.provider_id),
            name=data.label.strip(), base_url=base_url,
            default_model_id=data.default_model_id.strip(), enabled=data.enabled,
            auth_required=auth_required,
            capabilities={'declared': dict(data.capabilities), 'verified': {}},
            readiness_revision=1, provider_id=contract.provider_id,
            protocol_id=contract.protocol_id, auth_method_id=contract.auth_method_id,
            public_config_version=1, public_config=dict(contract.fields),
            auth_grant_revision=0 if direct or contract.provider_id == 'chatgpt' else 1 if auth_required else 0,
            credential_state='missing' if direct or contract.provider_id == 'chatgpt' else (
                'present' if auth_required else 'not_required'),
        )
    else:
        direct = False
        base_url, _ = validate_url(
            data.provider_kind, data.base_url, data.allow_local_http, data.auth_required)
        _require_personal_agent_runtime(store, data.provider_kind, base_url)
        if not data.auth_required and not base_url:
            fail('invalid_url', 422)
        if not data.auth_required and data.api_key:
            fail('invalid_url', 422)
        if data.auth_required and not data.api_key:
            raise ConnectionFailure('missing_secret', MESSAGES['missing_secret'])
        row = ProviderConnection(
            user_id=owner_id, kind=data.provider_kind, name=data.label.strip(),
            base_url=base_url, default_model_id=data.default_model_id.strip(),
            enabled=data.enabled, auth_required=data.auth_required,
            capabilities={'declared': dict(data.capabilities), 'verified': {}},
            auth_grant_revision=1 if data.auth_required else 0,
            credential_state='present' if data.auth_required else 'not_required',
        )
    if not row.name or not row.default_model_id:
        fail('invalid_configuration', 422)
    db.add(row)
    bind = db.get_bind()
    connection_id = None
    new_ref = None
    try:
        db.flush()
        connection_id = row.id
        if data.api_key and not direct:
            new_ref = f'{owner_id}:{row.id}:{uuid4().hex}'
            row.secret_ref = new_ref
            store.put(new_ref, data.api_key)
        db.commit()
    except Exception as error:
        _compensate_unreferenced_secret(db, bind, connection_id, new_ref, store)
        _save_failure(error)
    if direct:
        return _commit_direct_api_key(db, row, data.api_key, store)
    try:
        db.refresh(row)
    except Exception:
        raise ConnectionFailure('storage_unavailable', MESSAGES['storage_unavailable']) from None
    return row


@synchronized
def update(db: Session, row: ProviderConnection, changes: dict, store) -> ProviderConnection:
    _assert_connection_resolvable(db, row.user_id, row.id)
    versioned = not isinstance(changes, dict)
    old_readiness = int(row.readiness_revision or 1)
    old_grant_raw = row.auth_grant_revision
    old_grant = _grant_revision(row)
    old_ref = row.secret_ref
    if versioned:
        contract = _validated_versioned(changes, metadata_only=True)
        normalized = provider_catalog.normalize_legacy_connection(row) if row.provider_id is None else None
        current_provider = row.provider_id or normalized.provider_id
        current_protocol = row.protocol_id or normalized.protocol_id
        current_method = row.auth_method_id or normalized.auth_method_id
        if contract.provider_id != current_provider:
            raise ConnectionFailure('provider_immutable', MESSAGES['provider_immutable'])
        key_changed = 'api_key' in changes.model_fields_set
        if contract.provider_id == 'chatgpt' and 'api_key' in changes.model_fields_set: fail('invalid_configuration', 422)
        auth_required = contract.auth_method_id != 'local_none'
        if not auth_required and changes.api_key:
            fail('invalid_configuration', 422)
        if auth_required and not changes.api_key and not old_ref and contract.provider_id != 'chatgpt':
            raise ConnectionFailure('missing_secret', MESSAGES['missing_secret'])
        direct = (contract.provider_id == 'azure-openai'
                  and contract.protocol_id == 'azure_openai'
                  and contract.auth_method_id == 'api_key')
        resolved_origin = provider_catalog.resolve_origin(
            contract.provider_id, contract.fields, protocol_id=contract.protocol_id)
        base_url = None if direct else resolved_origin
        label = row.name if changes.label is None else changes.label.strip()
        model_id = row.default_model_id if changes.default_model_id is None else changes.default_model_id.strip()
        provider_catalog.validate_provider_model_protocol(
            contract.provider_id, contract.protocol_id, model_id)
        enabled = row.enabled if changes.enabled is None else changes.enabled
        declared = ((row.capabilities or {}).get('declared', {}) if changes.capabilities is None
                    else dict(changes.capabilities))
        method_changed = current_method != contract.auth_method_id
        typed_values = {
            'kind': _legacy_kind(contract.provider_id),
            'name': label,
            'base_url': base_url,
            'default_model_id': model_id,
            'enabled': enabled,
            'auth_required': auth_required,
            'provider_id': contract.provider_id,
            'protocol_id': contract.protocol_id,
            'auth_method_id': contract.auth_method_id,
            'public_config_version': 1,
            'public_config': dict(contract.fields),
        }
        if row.provider_id is None:
            typed_values.update({
                'auth_grant_revision': old_grant,
                'credential_state': ('present' if old_ref else
                                     'not_required' if not auth_required else 'missing'),
            })
        readiness_changed = (key_changed or method_changed
                             or current_protocol != contract.protocol_id
                             or row.public_config_version != 1
                             or (row.public_config or {}) != dict(contract.fields)
                             or row.base_url != base_url or row.default_model_id != model_id
                             or bool(row.enabled) != bool(enabled)
                             or bool(row.auth_required) != auth_required
                             or (row.capabilities or {}).get('declared', {}) != declared)
        grant_changed = key_changed or method_changed or bool(old_ref) != auth_required
        api_key = changes.api_key
    else:
        direct = False
        if any(changes.get(field) is None for field in (
                'label', 'default_model_id', 'enabled', 'auth_required', 'capabilities')
                if field in changes):
            fail('invalid_configuration', 422)
        if 'label' in changes and not changes['label'].strip():
            fail('invalid_configuration', 422)
        base_url = changes.get('base_url', row.base_url)
        local_allowed = changes.get(
            'allow_local_http', bool(base_url and urlsplit(base_url).scheme == 'http'))
        auth_required = changes.get('auth_required', row.auth_required)
        base_url, _ = validate_url(row.kind, base_url, local_allowed, auth_required)
        if not auth_required and changes.get('api_key'):
            fail('invalid_configuration', 422)
        if auth_required and not changes.get('api_key') and not old_ref:
            raise ConnectionFailure('missing_secret', MESSAGES['missing_secret'])
        key_changed = 'api_key' in changes and changes['api_key'] is not None
        readiness_changed = key_changed or any(
            field in changes and changes[field] != getattr(
                row, {'default_model_id': 'default_model_id',
                      'auth_required': 'auth_required'}.get(field, field))
            for field in ('base_url', 'default_model_id', 'auth_required', 'enabled'))
        readiness_changed = readiness_changed or (
            'capabilities' in changes
            and dict(changes['capabilities']) != (row.capabilities or {}).get('declared', {}))
        grant_changed = key_changed or bool(old_ref) != bool(auth_required)
        declared = (dict(changes['capabilities']) if 'capabilities' in changes
                    else (row.capabilities or {}).get('declared', {}))
        typed_values = {'base_url': base_url}
        for field, target in (('label', 'name'), ('default_model_id', 'default_model_id'),
                              ('enabled', 'enabled'), ('auth_required', 'auth_required')):
            if field in changes:
                value = changes[field]
                typed_values[target] = value.strip() if field == 'label' else value
        api_key = changes.get('api_key')
    if not (row.name if not versioned else label) or not (
            row.default_model_id if not versioned and 'default_model_id' not in changes else
            typed_values.get('default_model_id', row.default_model_id)):
        fail('invalid_configuration', 422)
    usage_change = (bool(key_changed and api_key)
                    or (bool(typed_values.get('enabled', row.enabled)) and not bool(row.enabled))
                    or typed_values.get('base_url', row.base_url) != row.base_url
                    or typed_values.get('default_model_id', row.default_model_id) != row.default_model_id
                    or typed_values.get('protocol_id', row.protocol_id) != row.protocol_id
                    or typed_values.get('auth_method_id', row.auth_method_id) != row.auth_method_id)
    if usage_change:
        _require_personal_agent_runtime(
            store, typed_values.get('provider_id', row.provider_id or row.kind), base_url)
    invalidate_verified = readiness_changed
    bind = db.get_bind()
    connection_id = row.id
    new_ref = f'{row.user_id}:{row.id}:{uuid4().hex}' if key_changed and api_key else None
    target_ref = new_ref if key_changed and api_key else None if (
        (key_changed and not api_key) or not auth_required) else old_ref
    values = dict(typed_values)
    values['secret_ref'] = target_ref
    if invalidate_verified:
        values['capabilities'] = {'declared': declared, 'verified': {}}
    if readiness_changed:
        values['readiness_revision'] = old_readiness + 1
    if grant_changed:
        values['auth_grant_revision'] = old_grant + 1
        values['credential_state'] = 'present' if target_ref else (
            'not_required' if not auth_required else 'missing')
    target_readiness = old_readiness + int(readiness_changed)
    target_grant = old_grant + int(grant_changed)
    target_method = values.get('auth_method_id', row.auth_method_id)
    if direct and key_changed and api_key:
        current_values = {
            'kind': row.kind, 'name': row.name, 'base_url': row.base_url,
            'default_model_id': row.default_model_id, 'enabled': row.enabled,
            'auth_required': row.auth_required, 'provider_id': row.provider_id,
            'protocol_id': row.protocol_id, 'auth_method_id': row.auth_method_id,
            'public_config_version': row.public_config_version,
            'public_config': row.public_config,
        }
        if (any(current_values.get(key) != value for key, value in typed_values.items())
                or (row.capabilities or {}).get('declared', {}) != declared):
            raise ConnectionFailure(
                'invalid_configuration', MESSAGES['invalid_configuration'])
        return _commit_direct_api_key(db, row, api_key, store)
    committed_row = None
    try:
        if new_ref:
            store.put(new_ref, api_key)
        saved = db.execute(sql_update(ProviderConnection).where(
            ProviderConnection.id == connection_id,
            ProviderConnection.user_id == row.user_id,
            ProviderConnection.secret_ref == old_ref,
            ProviderConnection.readiness_revision == old_readiness,
            ProviderConnection.auth_grant_revision == old_grant_raw,
        ).values(**values).execution_options(synchronize_session=False)).rowcount
        if saved != 1:
            db.rollback()
            _compensate_unreferenced_secret(db, bind, connection_id, new_ref, store)
            raise ConnectionFailure('configuration_changed', MESSAGES['configuration_changed'])
        db.commit()
    except ConnectionFailure:
        raise
    except Exception as error:
        try:
            db.rollback()
        except Exception:
            pass
        committed_row = _confirmed_committed_connection(
            bind,
            owner_id=row.user_id,
            connection_id=connection_id,
            secret_ref=target_ref,
            config_revision=target_readiness,
            auth_grant_revision=target_grant,
            auth_method_id=target_method,
        )
        if committed_row is None:
            _compensate_unreferenced_secret(db, bind, connection_id, new_ref, store)
            _save_failure(error)
    if committed_row is not None:
        row = committed_row
    else:
        try:
            db.expire_all()
            row = db.get(ProviderConnection, connection_id)
            db.refresh(row)
        except Exception:
            raise ConnectionFailure('storage_unavailable', MESSAGES['storage_unavailable']) from None
    if old_ref and target_ref != old_ref:
        try:
            store.delete(old_ref)
        except SecretStoreUnavailable:
            pass  # The committed ref remains valid; old ref is an OS-store orphan.
    return row


@synchronized
def commit_prepared_credential_grant(
        db: Session, *, owner_id: int, connection_id: int,
        config_revision: int, auth_grant_revision: int,
        current_auth_method_id: str, target_auth_method_id: str,
        prepared_ref: str, safe_grant_identity: str, transition: str,
        store) -> ProviderConnection:
    """Commit a Core-prepared ref; Core owns the opaque grant-identity proof."""
    bind = db.get_bind()
    bound_ref = (isinstance(prepared_ref, str)
                 and re.fullmatch(
                     rf'{owner_id}:{connection_id}:[0-9a-f]{{32}}', prepared_ref) is not None)
    valid_proof = (
        transition in {'proven_new_grant', 'same_grant_refresh'}
        and isinstance(safe_grant_identity, str)
        and 1 <= len(safe_grant_identity) <= 256
        and safe_grant_identity == safe_grant_identity.strip()
        and all(character.isprintable() for character in safe_grant_identity)
        and bound_ref
        and isinstance(current_auth_method_id, str)
        and isinstance(target_auth_method_id, str)
    )
    if not valid_proof:
        if bound_ref:
            _compensate_unreferenced_secret(
                db, bind, connection_id, prepared_ref, store)
        raise ConnectionFailure('invalid_configuration', MESSAGES['invalid_configuration'])
    if (transition == 'same_grant_refresh'
            and target_auth_method_id != current_auth_method_id):
        _compensate_unreferenced_secret(
            db, bind, connection_id, prepared_ref, store)
        raise ConnectionFailure('invalid_configuration', MESSAGES['invalid_configuration'])
    row = db.query(ProviderConnection).filter_by(
        id=connection_id, user_id=owner_id).first()
    if (row is None or row.provider_id is None
            or int(row.readiness_revision or 1) != config_revision
            or _grant_revision(row) != auth_grant_revision
            or row.auth_method_id != current_auth_method_id
            or not row.secret_ref or row.credential_state != 'present'
            or prepared_ref == row.secret_ref):
        db.rollback()
        _compensate_unreferenced_secret(
            db, bind, connection_id, prepared_ref, store)
        raise ConnectionFailure('configuration_changed', MESSAGES['configuration_changed'])
    try:
        provider_catalog.validate_public_config(PublicConfigEnvelope(
            version=row.public_config_version,
            provider_id=row.provider_id,
            protocol_id=row.protocol_id,
            auth_method_id=target_auth_method_id,
            fields=deepcopy(row.public_config or {}),
        ))
    except (HTTPException, ValueError):
        db.rollback()
        _compensate_unreferenced_secret(
            db, bind, connection_id, prepared_ref, store)
        raise ConnectionFailure('invalid_configuration', MESSAGES['invalid_configuration'])
    old_ref = row.secret_ref
    old_grant_raw = row.auth_grant_revision
    increment = int(transition == 'proven_new_grant')
    target_readiness = config_revision + increment
    target_grant = auth_grant_revision + increment
    committed_row = None
    try:
        saved = db.execute(sql_update(ProviderConnection).where(
            ProviderConnection.id == connection_id,
            ProviderConnection.user_id == owner_id,
            ProviderConnection.secret_ref == old_ref,
            ProviderConnection.readiness_revision == config_revision,
            ProviderConnection.auth_grant_revision == old_grant_raw,
            ProviderConnection.auth_method_id == current_auth_method_id,
            ProviderConnection.credential_state == 'present',
        ).values(
            secret_ref=prepared_ref,
            auth_method_id=target_auth_method_id,
            credential_state='present',
            readiness_revision=target_readiness,
            auth_grant_revision=target_grant,
        ).execution_options(synchronize_session=False)).rowcount
        if saved != 1:
            db.rollback()
            _compensate_unreferenced_secret(
                db, bind, connection_id, prepared_ref, store)
            raise ConnectionFailure('configuration_changed', MESSAGES['configuration_changed'])
        db.commit()
    except ConnectionFailure:
        raise
    except Exception as error:
        try:
            db.rollback()
        except Exception:
            pass
        committed_row = _confirmed_committed_connection(
            bind,
            owner_id=owner_id,
            connection_id=connection_id,
            secret_ref=prepared_ref,
            config_revision=target_readiness,
            auth_grant_revision=target_grant,
            auth_method_id=target_auth_method_id,
        )
        if committed_row is None:
            _compensate_unreferenced_secret(
                db, bind, connection_id, prepared_ref, store)
            _save_failure(error)
    if committed_row is None:
        try:
            db.expire_all()
            committed_row = db.get(ProviderConnection, connection_id)
            db.refresh(committed_row)
        except Exception:
            raise ConnectionFailure(
                'storage_unavailable', MESSAGES['storage_unavailable']) from None
    try:
        store.delete(old_ref)
    except SecretStoreUnavailable:
        pass  # The committed prepared ref remains authoritative.
    return committed_row


@dataclass(frozen=True)
class DisconnectResult:
    credential_state: str
    revoke_state: str


def disconnect_connection_durable(db: Session, row: ProviderConnection,
                                  revoke_remote: bool, *, recovery,
                                  idempotency_key: str | None = None,
                                  remote_revoker=None):
    """Enter the R3 ledger/Core path; this function never accepts a raw store."""
    return recovery.disconnect(
        db, row, revoke_remote, idempotency_key=idempotency_key,
        remote_revoker=remote_revoker)


def commit_prepared_credential_grant_durable(
        db: Session, row: ProviderConnection, *, recovery, operation,
        binding: dict, target_auth_method_id: str, transition: str):
    """Commit a prepared winner through R3; Core performs all old cleanup."""
    return recovery.commit_prepared_grant(
        db, row, operation=operation, binding=binding,
        target_auth_method_id=target_auth_method_id, transition=transition)


def _persisted_lifecycle_state(bind, connection_id: int):
    with Session(bind=bind) as check:
        return check.execute(select(
            ProviderConnection.secret_ref,
            ProviderConnection.readiness_revision,
            ProviderConnection.auth_grant_revision,
            ProviderConnection.credential_state,
        ).where(ProviderConnection.id == connection_id)).one_or_none()


def _model_cache_snapshot(db: Session, owner_id: int, connection_id: int) -> dict | None:
    cached = db.query(ConnectionModelCache).filter_by(
        owner_id=owner_id, connection_id=connection_id).first()
    if cached is None:
        return None
    return {
        'owner_id': cached.owner_id,
        'connection_id': cached.connection_id,
        'config_revision': cached.config_revision,
        'auth_grant_revision': cached.auth_grant_revision,
        'config_fingerprint': cached.config_fingerprint,
        'observed_at': cached.observed_at,
        'truncated': bool(cached.truncated),
        'models': deepcopy(cached.models or []),
        'updated_at': cached.updated_at,
    }


def _compensate_disconnect(bind, *, owner_id: int, connection_id: int,
                           old_ref: str, old_readiness: int,
                           old_grant_raw: int | None, old_credential_state: str | None,
                           target_readiness: int, target_grant: int,
                           cache_snapshot: dict | None) -> bool:
    try:
        with Session(bind=bind) as repair:
            saved = repair.execute(sql_update(ProviderConnection).where(
                ProviderConnection.id == connection_id,
                ProviderConnection.user_id == owner_id,
                ProviderConnection.secret_ref.is_(None),
                ProviderConnection.readiness_revision == target_readiness,
                ProviderConnection.auth_grant_revision == target_grant,
                ProviderConnection.credential_state == 'missing',
            ).values(
                secret_ref=old_ref,
                credential_state=old_credential_state,
                readiness_revision=old_readiness,
                auth_grant_revision=old_grant_raw,
            ).execution_options(synchronize_session=False)).rowcount
            if saved != 1:
                repair.rollback()
            else:
                if cache_snapshot is not None:
                    repair.add(ConnectionModelCache(**cache_snapshot))
                repair.commit()
    except Exception:
        pass
    try:
        state = _persisted_lifecycle_state(bind, connection_id)
    except Exception:
        return False
    if (state is None or state.secret_ref != old_ref
            or state.credential_state != old_credential_state):
        return False
    if (int(state.readiness_revision or 1) != old_readiness
            or state.auth_grant_revision != old_grant_raw):
        # A concurrent winner still references the verified-present old ref.
        return True
    if cache_snapshot is None:
        return True
    try:
        with Session(bind=bind) as check:
            cached = check.query(ConnectionModelCache).filter_by(
                owner_id=owner_id, connection_id=connection_id).first()
            return bool(
                cached is not None
                and cached.config_revision == cache_snapshot['config_revision']
                and cached.auth_grant_revision == cache_snapshot['auth_grant_revision']
                and cached.config_fingerprint == cache_snapshot['config_fingerprint']
                and bool(cached.truncated) == cache_snapshot['truncated']
                and (cached.models or []) == cache_snapshot['models']
            )
    except Exception:
        return False


@synchronized
def disconnect_connection(db: Session, row: ProviderConnection, revoke_remote: bool,
                          store, remote_revoker=None) -> DisconnectResult:
    old_ref = row.secret_ref
    if not old_ref:
        return DisconnectResult(
            credential_state='missing',
            revoke_state='already_invalid' if revoke_remote else 'skipped',
        )
    bind = db.get_bind()
    connection_id = row.id
    owner_id = row.user_id
    old_readiness = int(row.readiness_revision or 1)
    old_grant_raw = row.auth_grant_revision
    old_grant = _grant_revision(row)
    old_credential_state = row.credential_state
    target_readiness = old_readiness + 1
    target_grant = old_grant + 1
    provider_id = row.provider_id or provider_catalog.normalize_legacy_connection(row).provider_id
    protocol_id = row.protocol_id or provider_catalog.normalize_legacy_connection(row).protocol_id
    auth_method_id = row.auth_method_id or provider_catalog.normalize_legacy_connection(row).auth_method_id
    fields = deepcopy(row.public_config or {})
    credential = store.get(old_ref)
    cache_snapshot = _model_cache_snapshot(db, owner_id, connection_id)
    try:
        saved = db.execute(sql_update(ProviderConnection).where(
            ProviderConnection.id == connection_id,
            ProviderConnection.user_id == owner_id,
            ProviderConnection.secret_ref == old_ref,
            ProviderConnection.readiness_revision == old_readiness,
            ProviderConnection.auth_grant_revision == old_grant_raw,
        ).values(
            secret_ref=None,
            credential_state='missing',
            readiness_revision=target_readiness,
            auth_grant_revision=target_grant,
        ).execution_options(synchronize_session=False)).rowcount
        if saved != 1:
            db.rollback()
            raise ConnectionFailure('configuration_changed', MESSAGES['configuration_changed'])
        db.execute(sql_delete(ConnectionModelCache).where(
            ConnectionModelCache.owner_id == owner_id,
            ConnectionModelCache.connection_id == connection_id,
        ))
        try:
            db.commit()
        except Exception as error:
            try:
                db.rollback()
            except Exception:
                pass
            persisted = _persisted_lifecycle_state(bind, connection_id)
            committed = (persisted is not None and persisted.secret_ref is None
                         and int(persisted.readiness_revision or 1) == target_readiness
                         and int(persisted.auth_grant_revision or 0) == target_grant
                         and persisted.credential_state == 'missing')
            if not committed:
                _save_failure(error)
    except ConnectionFailure:
        raise
    except Exception as error:
        db.rollback()
        _save_failure(error)

    try:
        store.delete(old_ref)
    except SecretStoreUnavailable as delete_error:
        try:
            remaining = store.get_no_ui(old_ref)
        except SecretStoreUnavailable:
            raise ConnectionFailure(
                'credential_cleanup_unconfirmed',
                MESSAGES['credential_cleanup_unconfirmed'],
            ) from None
        if remaining is not None:
            restored = _compensate_disconnect(
                bind,
                owner_id=owner_id,
                connection_id=connection_id,
                old_ref=old_ref,
                old_readiness=old_readiness,
                old_grant_raw=old_grant_raw,
                old_credential_state=old_credential_state,
                target_readiness=target_readiness,
                target_grant=target_grant,
                cache_snapshot=cache_snapshot,
            )
            if restored:
                raise delete_error
            raise ConnectionFailure(
                'credential_cleanup_unconfirmed',
                MESSAGES['credential_cleanup_unconfirmed'],
            ) from None

    if not revoke_remote:
        revoke_state = 'skipped'
    elif credential is None:
        revoke_state = 'already_invalid'
    elif remote_revoker is None:
        revoke_state = 'not_supported'
    else:
        try:
            outcome = remote_revoker(
                provider_id=provider_id,
                protocol_id=protocol_id,
                auth_method_id=auth_method_id,
                fields=fields,
                credential=credential,
            )
            revoke_state = (outcome if outcome in {'revoked', 'already_invalid', 'not_supported'}
                            else 'revoked' if outcome is True
                            else 'remote_revoke_failed')
        except Exception:
            revoke_state = 'remote_revoke_failed'
    return DisconnectResult(credential_state='missing', revoke_state=revoke_state)


@synchronized
def delete(db: Session, row: ProviderConnection, store) -> None:
    _assert_connection_resolvable(db, row.user_id, row.id)
    if 'connection_id' in {column['name'] for column in inspect(db.bind).get_columns('agents')}:
        if db.execute(text('SELECT 1 FROM agents WHERE connection_id=:id LIMIT 1'), {'id': row.id}).first():
            raise HTTPException(409, detail={'code': 'connection_in_use', 'message': 'An Agent uses this connection.'})
    previous_enabled = row.enabled
    reference = row.secret_ref
    row.enabled = False
    try:
        db.commit()  # Crash after this point cannot leave an enabled dangling ref.
    except Exception:
        db.rollback()
        raise
    previous = None
    try:
        previous = store.get(reference) if reference else None
        if previous is not None:
            store.delete(reference)
        db.delete(row)
        db.commit()
    except Exception:
        db.rollback()
        restored = reference is None
        if previous is not None:
            try:
                store.put(reference, previous)
                restored = True
            except SecretStoreUnavailable:
                pass
        row.enabled = previous_enabled if restored else False
        db.commit()
        raise


async def probe(config: ConnectionConfig, transport=None) -> dict:
    contract, origin = provider_runtime_origin(config)
    provider_id, protocol_id = contract.provider_id, contract.protocol_id
    if provider_id == 'chatgpt':
        from app.services.providers.chatgpt import ChatGPTAdapter
        from app.services.providers.base import ProviderRequest, ProviderFailure
        try:
            result=await ChatGPTAdapter(transport).generate(ProviderRequest(connection=config,model_id=config.model_id,system='Reply with the single word OK.',events=[{'seq':1,'event_id':'chatgpt-connection-test','actor_type':'user','actor_id':0,'actor_name_snapshot':'Connection test','kind':'message','payload':{'content':'Reply with exactly OK.'}}],watermark_seq=1,trigger_event_id='chatgpt-connection-test'))
            if not result.text.strip():raise ValueError()
            return {'ok':True,'code':'connected','message':'ChatGPT text response completed; tokens recorded when the usage ledger is available.'}
        except ProviderFailure as error:
            raise ConnectionFailure(error.code,error.message) from None
    if provider_id in {'opencode-go', 'alibaba-coding-plan'}:
        try:
            provider_catalog.validate_provider_model_protocol(
                provider_id, protocol_id, config.model_id)
        except HTTPException:
            raise ConnectionFailure(
                'invalid_configuration', MESSAGES['invalid_configuration']) from None
        if (type(config.owner_id) is not int or config.owner_id < 1
                or type(config.connection_id) is not int or config.connection_id < 1):
            raise ConnectionFailure(
                'invalid_configuration', MESSAGES['invalid_configuration'])
    if protocol_id == 'gemini_generate_content':
        path = f'/models/{quote(config.model_id, safe="")}:generateContent'
        body = {'contents': [{'parts': [{'text': 'Reply OK.'}]}], 'generationConfig': {'maxOutputTokens': CONNECTION_TEXT_PROBE_MAX_TOKENS}}
        headers = {'x-goog-api-key': config.api_key or ''}
    elif protocol_id == 'anthropic_messages':
        path = '/v1/messages' if provider_id == 'alibaba-coding-plan' else '/messages'
        body = {'model': config.model_id, 'max_tokens': CONNECTION_TEXT_PROBE_MAX_TOKENS, 'messages': [{'role': 'user', 'content': 'Reply OK.'}]}
        headers = {'x-api-key': config.api_key or '', 'anthropic-version': '2023-06-01'}
    elif protocol_id == 'openai_chat_completions':
        path = '/chat/completions'
        body = {'model': config.model_id, 'messages': [{'role': 'user', 'content': 'Reply OK.'}]}
        if provider_id != 'compatible':
            body['max_tokens'] = CONNECTION_TEXT_PROBE_MAX_TOKENS
        headers = {'Authorization': f'Bearer {config.api_key}'} if config.api_key else {}
    else:
        raise ConnectionFailure('invalid_configuration', MESSAGES['invalid_configuration'])
    if provider_id == 'opencode-go':
        from app.services.providers.opencode_go import USER_AGENT, session_header
        headers.update({
            'User-Agent': USER_AGENT,
            'x-opencode-session': session_header(
                owner_id=config.owner_id, connection_id=config.connection_id, room_id=0),
        })
    try:
        async with asyncio.timeout(CONNECTION_PROBE_DEADLINE_SECONDS):
            async with httpx.AsyncClient(
                    transport=transport, trust_env=False, follow_redirects=False,
                    timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10)) as client:
                result = await client.post(origin.rstrip('/') + path, headers=headers, json=body)
    except (httpx.HTTPError, ValueError, TimeoutError):
        raise ConnectionFailure('unreachable', MESSAGES['unreachable']) from None
    if 300 <= result.status_code < 400:
        raise ConnectionFailure('redirect_refused', MESSAGES['redirect_refused'])
    if result.status_code in (401, 403):
        raise ConnectionFailure('invalid_credential', MESSAGES['invalid_credential'])
    if result.status_code == 429:
        if provider_id == 'opencode-go':
            from app.services.providers.opencode_go import rate_limit_failure
            failure_code, _ = rate_limit_failure(result)
            raise ConnectionFailure(failure_code, MESSAGES[failure_code])
        raise ConnectionFailure('rate_limited', MESSAGES['rate_limited'])
    if result.status_code >= 400:
        raise ConnectionFailure('provider_error', MESSAGES['provider_error'])
    try:
        if len(result.content) > 1_000_000:
            raise ValueError()
        data = result.json()
        if protocol_id == 'gemini_generate_content':
            answer = data['candidates'][0]['content']['parts'][0]['text']
        elif protocol_id == 'anthropic_messages':
            answer = next(part['text'] for part in data['content'] if part['type'] == 'text')
        else:
            answer = data['choices'][0]['message']['content']
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError()
    except (ValueError, TypeError, KeyError, IndexError, StopIteration):
        raise ConnectionFailure('invalid_response', MESSAGES['invalid_response']) from None
    return {'ok': True, 'code': 'connected', 'message': 'Connection test succeeded.'}


async def probe_native(config: ConnectionConfig, core_transport, *, physical_request_budget=None) -> dict:
    """Send one exact-model Test through Core without reading credentials or using HTTPX."""
    from app.services.providers import ProviderFailure, ProviderRequest, adapter_for

    contract = validated_connection_contract(config, auth_required=True, has_secret=True)
    if not is_core_managed_credential(config):
        raise ConnectionFailure('invalid_configuration', MESSAGES['invalid_configuration'])
    request = ProviderRequest(
        connection=config, model_id=config.model_id,
        system='Answer this connection test with OK.',
        agent_id=config.owner_id or 0, room_id=config.connection_id,
        events=[{
            'seq': 1, 'event_id': 'connection-test', 'actor_type': 'user',
            'actor_id': config.owner_id or 0, 'actor_name_snapshot': 'Connection test',
            'kind': 'message', 'payload': {'content': 'Reply OK.'},
        }],
        watermark_seq=1, trigger_event_id='connection-test',
    )
    try:
        result = await adapter_for(
            contract.protocol_id, contract.provider_id, None,
            core_transport=core_transport, physical_request_budget=physical_request_budget,
        ).generate(request)
    except ProviderFailure as error:
        code = error.code if error.code in MESSAGES else 'provider_error'
        raise ConnectionFailure(code, MESSAGES[code]) from None
    if not result.text.strip():
        raise ConnectionFailure('invalid_response', MESSAGES['invalid_response'])
    return {'ok': True, 'code': 'connected', 'message': 'Connection test succeeded.'}


def _color_pixel(rgb: bytes) -> bytes:
    """One trusted PNG pixel for an explicit vision challenge; no local path is read."""
    def chunk(name: bytes, data: bytes) -> bytes:
        return struct.pack('!I', len(data)) + name + data + struct.pack('!I', zlib.crc32(name + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00' + rgb)) + chunk(b'IEND', b''))


async def probe_capabilities(config: ConnectionConfig, transport=None, *, core_transport=None,
                             physical_request_budget=None) -> dict:
    """Probe only opted-in options. A text probe never proves tool or vision support."""
    from app.services.providers import ProviderFailure, ProviderRequest, ToolObservation, VisionInput, adapter_for

    validated_connection_contract(config)
    if config.provider_id == 'chatgpt':
        tested={'model_id':config.model_id,'base_url':config.base_url,'max_tokens':False,'temperature':False,'tools':False,'vision':False}
        declared=(config.capabilities or {}).get('declared',{})
        if declared.get('tools') is True:
            # A synthetic, isolated built-in challenge. No tool broker, database,
            # real routine, filesystem or external effect is involved.
            from app.services.routine_persistence import TOOL_SCHEMAS
            spec=next(t for t in TOOL_SCHEMAS if t['name']=='read_bot_routine')
            fake_id='00000000-0000-4000-8000-000000000001'
            caps=deepcopy(config.capabilities or {})
            caps['verified']={'models':{config.model_id:{'model_id':config.model_id,'base_url':config.base_url,'tools':True}}}
            temporary=replace(config,capabilities=caps)
            request=ProviderRequest(connection=temporary,model_id=config.model_id,system='This is a synthetic connection test. Call the offered read_bot_routine with the exact test UUID, then reply exactly PROBE_COMPLETE after its synthetic result.',
                events=[{'seq':1,'event_id':'chatgpt-tool-test','actor_type':'user','actor_id':0,'actor_name_snapshot':'Connection test',
                         'kind':'message','payload':{'content':'Call read_bot_routine with routine_id '+fake_id+'.'}}],
                watermark_seq=1,trigger_event_id='chatgpt-tool-test',tools=[spec])
            try:
                adapter=adapter_for(config.protocol_id,config.provider_id,transport,core_transport=core_transport,physical_request_budget=physical_request_budget)
                result=await adapter.generate(request)
                if len(result.tool_calls)!=1 or result.tool_calls[0].family!='read_bot_routine' or result.tool_calls[0].arguments!={'routine_id':fake_id} or not result.continuation:
                    raise ProviderFailure('provider_invalid_response', 'Synthetic tool challenge failed.')
                completed=await adapter.generate(replace(request,continuation=result.continuation,
                    observations=[ToolObservation(result.tool_calls[0].call_id,'completed','Synthetic test result; reply exactly PROBE_COMPLETE.')]))
                tested['tools']=completed.text.strip()=='PROBE_COMPLETE' and not completed.tool_calls
            except ProviderFailure:
                if physical_request_budget is not None and physical_request_budget.actual_count is None:
                    raise ConnectionFailure('provider_error',MESSAGES['provider_error']) from None
        if declared.get('vision') is True:
            caps=deepcopy(config.capabilities or {})
            caps['verified']={'models':{config.model_id:{'model_id':config.model_id,'base_url':config.base_url,'vision':True}}}
            temporary=replace(config,capabilities=caps)
            request=ProviderRequest(connection=temporary,model_id=config.model_id,system='Answer the test request.',
                vision=VisionInput(_color_pixel(b'\xff\x00\x00'),'image/png','What color is the pixel in this image?'))
            try:
                adapter=adapter_for(config.protocol_id,config.provider_id,transport,core_transport=core_transport,physical_request_budget=physical_request_budget)
                first=await adapter.generate(request)
                second=await adapter.generate(replace(request,vision=VisionInput(_color_pixel(b'\x00\x00\xff'),'image/png','What color is the pixel in this image?')))
                tested['vision']=bool(re.search(r'\bred\b',first.text,re.IGNORECASE) and re.search(r'\bblue\b',second.text,re.IGNORECASE))
            except ProviderFailure:
                if physical_request_budget is not None and physical_request_budget.actual_count is None:
                    raise ConnectionFailure('provider_error',MESSAGES['provider_error']) from None
        return tested
    declared = (config.capabilities or {}).get('declared', {})
    # The built-in protocol probe sent a bounded token ceiling; custom endpoints
    # opt in separately.
    tested = {'base_url': config.base_url}
    for option in ('max_tokens', 'temperature', 'tools', 'vision'):
        if (option == 'max_tokens' and config.provider_kind != 'compatible'
                and not is_core_managed_credential(config)):
            tested[option] = True
            continue
        tested[option] = False
        if declared.get(option) is not True:
            continue
        allowed = {'model_id': config.model_id, 'base_url': config.base_url,
                   'temperature': True, 'max_tokens': True, 'tools': True, 'vision': True}
        temporary = replace(config, capabilities={**deepcopy(config.capabilities or {}), 'declared': declared, 'verified': allowed})
        prompt = 'Call the provided probe_echo function with no arguments.' if option == 'tools' else 'Name the color of the image pixel.' if option == 'vision' else 'Reply OK.'
        native = is_core_managed_credential(config)
        request = ProviderRequest(connection=temporary, model_id=config.model_id, system='Answer the test request.',
                                  agent_id=(config.owner_id or 0) if native else 0,
                                  room_id=config.connection_id if native else 0,
                                  events=[{'seq': 1, 'event_id': 'connection-test', 'actor_type': 'user',
                                  'actor_id': (config.owner_id or 0) if native else 0,
                                  'actor_name_snapshot': 'Connection test', 'kind': 'message',
                                  'payload': {'content': prompt}}], watermark_seq=1,
                                  max_tokens=CONNECTION_TEXT_PROBE_MAX_TOKENS if option == 'max_tokens' else None,
                                  temperature=0.3 if option == 'temperature' else None,
                                  tools=[{'name': 'probe_echo', 'description': 'Connection capability test',
                                          'parameters': {'type': 'object', 'properties': {}}}] if option == 'tools' else [],
                                  vision=VisionInput(_color_pixel(b'\xff\x00\x00'), 'image/png', 'What color is the pixel in this image?') if option == 'vision' else None)
        try:
            if config.provider_id and config.protocol_id:
                adapter = adapter_for(
                    config.protocol_id, config.provider_id, transport,
                    core_transport=core_transport,
                    physical_request_budget=physical_request_budget,
                )
            else:
                adapter = adapter_for(config.provider_kind, transport)
            if option == 'vision':
                first = await adapter.generate(request)
                second = await adapter.generate(replace(request, vision=VisionInput(
                    _color_pixel(b'\x00\x00\xff'), 'image/png', 'What color is the pixel in this image?')))
                tested[option] = bool(re.search(r'\bred\b', first.text, re.IGNORECASE) and
                                      re.search(r'\bblue\b', second.text, re.IGNORECASE))
            else:
                result = await adapter.generate(request)
                if option == 'tools':
                    if len(result.tool_calls) != 1 or result.tool_calls[0].family != 'probe_echo' or not result.continuation:
                        continue
                    completed = await adapter.generate(replace(request, continuation=result.continuation,
                        observations=[ToolObservation(result.tool_calls[0].call_id, 'completed', 'Probe echo completed.')]))
                    tested[option] = bool(completed.text.strip() and not completed.tool_calls)
                else:
                    tested[option] = bool(result.text.strip())
        except ProviderFailure:
            if (physical_request_budget is not None
                    and physical_request_budget.actual_count is None):
                raise ConnectionFailure(
                    'provider_error', MESSAGES['provider_error']) from None
    return tested
