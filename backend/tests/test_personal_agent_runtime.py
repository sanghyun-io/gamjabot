"""Private-channel/owner/usage boundaries using synthetic local data only."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.routes.auth import create_access_token
from app.main import RuntimeConfig, _personal_agent_core, create_app
from app.models.models import User
from app.models.collaboration import ProviderConnection
from app.schemas.connection import VersionedConnectionCreate
from app.schemas.provider_catalog import PublicConfigEnvelope
from app.services import connections as service, provider_catalog as catalog
from app.services.providers import adapter_for
from app.services.providers.base import ProviderFailure
from app.services.secrets import CoreSecretStore


def body(protocol='openai_chat_completions', **changes):
    return {'version': 1, 'provider_id': 'alibaba-coding-plan', 'protocol_id': protocol,
            'auth_method_id': 'api_key', 'fields': {}, 'label': 'Personal Agent plan',
            'default_model_id': 'qwen3.7-plus', 'api_key': 'synthetic-plan-key', **changes}


@pytest.fixture
def client(sqlite_url, monkeypatch, personal_agent_core):
    monkeypatch.setenv('GAMJA_LOCAL_SESSION_TOKEN', 'synthetic-test-session')
    with TestClient(create_app(database_url=sqlite_url)) as api:
        api.app.state.secret_store = personal_agent_core[0]
        with api.app.state.session_factory() as db:
            owners = [User(email=f'{name}@example.test', username=name, hashed_password='unused')
                      for name in ('owner', 'other')]
            db.add_all(owners); db.commit(); ids = [u.id for u in owners]
        headers = lambda owner=0: {'X-Gamja-Session': 'synthetic-test-session',
            'Authorization': f'Bearer {create_access_token({"sub": ids[owner]})}'}
        yield api, headers, ids


def test_scope_requires_owned_live_channels_and_is_not_a_public_store_flag(personal_agent_core):
    store, _, _, auth = personal_agent_core
    assert service.personal_agent_runtime(store)
    assert service.personal_agent_runtime(service._CredentialServices(store, object()))
    assert not service.personal_agent_runtime(SimpleNamespace(personal_agent_runtime=True))
    assert not service.personal_agent_runtime(CoreSecretStore(object(), 'provider', personal_agent_runtime=True))
    assert not service.personal_agent_runtime(CoreSecretStore(store.core, 'remembered_login',
        personal_agent_runtime=True, auth_broker=auth))
    with pytest.raises(AttributeError): store.personal_agent_runtime = True
    auth.close()
    assert not service.personal_agent_runtime(store)


def test_main_binding_requires_both_core_transports_and_local_capability(personal_agent_core):
    store, _, _, auth = personal_agent_core
    config = RuntimeConfig(database_url='sqlite:///:memory:', credential_fd=store.core._channel.fileno(),
        auth_broker_fd=auth._channel.fileno(), local_session_token='synthetic-local-session-token-32chars',
        cli_capability_hash='ab' * 32)
    assert _personal_agent_core(config, store.core, auth)
    for changes in ({'credential_fd': None}, {'auth_broker_fd': None},
                    {'local_session_token': None}, {'cli_capability_hash': None},
                    {'cli_capability_hash': 'X' * 64}):
        assert not _personal_agent_core(replace(config, **changes), store.core, auth)
    assert not _personal_agent_core(config, object(), auth)
    assert not _personal_agent_core(config, store.core, object())


def test_catalog_syntax_scope_and_public_payload_are_separate(client, personal_agent_core):
    api, headers, _ = client
    scoped = api.get('/api/v1/connections/catalog', headers=headers()).json()
    profile = next(p for p in scoped['providers'] if p['provider_id'] == 'alibaba-coding-plan')
    assert profile['methods'][0]['selectable'] and profile['usage_scope'] == 'personal_interactive'
    assert profile['model_source']['static_model_ids'] == list(catalog.ALIBABA_MODELS)
    assert profile['fields'] == [] and profile['usage_classification'] == 'unverified'
    api.app.state.secret_store = SimpleNamespace(personal_agent_runtime=True)
    unscoped = api.get('/api/v1/connections/catalog', headers=headers()).json()
    assert not next(p for p in unscoped['providers'] if p['provider_id'] == 'alibaba-coding-plan')['methods'][0]['selectable']
    with pytest.raises(ValidationError):
        VersionedConnectionCreate(**body(personal_agent_runtime=True))
    envelope = PublicConfigEnvelope(version=1, provider_id='alibaba-coding-plan',
        protocol_id='openai_chat_completions', auth_method_id='api_key',
        fields={'personal_agent_runtime': True})
    with pytest.raises(Exception): catalog.validate_public_config(envelope, personal_agent_runtime=True)
    result = api.post('/api/v1/connections', headers=headers(), json=body())
    assert result.status_code == 422
    assert personal_agent_core[2] == []


@pytest.mark.parametrize('protocol,origin', [
    ('openai_chat_completions', catalog.ALIBABA_CHAT_ORIGIN),
    ('anthropic_messages', catalog.ALIBABA_MESSAGES_ORIGIN),
])
def test_typed_creation_resolves_one_protocol_and_keeps_secret_private(client, personal_agent_core, protocol, origin):
    api, headers, ids = client
    result = api.post('/api/v1/connections', headers=headers(), json=body(protocol))
    assert result.status_code == 201, result.text
    row = result.json()
    assert row['base_url'] == origin and row['fields'] == {} and row['has_secret']
    assert 'api_key' not in row and 'personal_agent_runtime' not in row and 'secret_ref' not in row
    assert personal_agent_core[2] == ['put']
    with api.app.state.session_factory() as db:
        config = service.resolve_connection(db, row['id'], ids[0], store=personal_agent_core[0])
        assert config.personal_agent_runtime and service.provider_runtime_origin(config)[1] == origin
    # No owner fallback, credential/cache lookup or physical request.
    before = list(personal_agent_core[2])
    with api.app.state.session_factory() as db, pytest.raises(Exception):
        service.resolve_connection(db, row['id'], ids[1], store=personal_agent_core[0])
    assert personal_agent_core[2] == before


@pytest.mark.asyncio
async def test_all_resolvers_refuse_untrusted_scope_before_cache_or_secret(client, personal_agent_core):
    api, headers, ids = client
    created = api.post('/api/v1/connections', headers=headers(), json=body()).json()
    personal_agent_core[2].clear()
    cache = SimpleNamespace(get=lambda _: pytest.fail('cache accessed before usage guard'))
    fake = SimpleNamespace(personal_agent_runtime=True, get_no_ui=lambda _: pytest.fail('secret read'))
    for resolver in (service.resolve_connection, service.resolve_connection_async, service.resolve_discovery_connection_async):
        with api.app.state.session_factory() as db, pytest.raises(service.ConnectionFailure) as caught:
            value = resolver(db, created['id'], ids[0], store=fake, cache=cache)
            if asyncio.iscoroutine(value): await value
        assert caught.value.code == 'personal_agent_runtime_required'
    assert personal_agent_core[2] == []
    # A cached, previously resolved config loses use when the private attachment closes.
    with api.app.state.session_factory() as db:
        config = service.resolve_connection(db, created['id'], ids[0], store=personal_agent_core[0])
    personal_agent_core[3].close()
    with pytest.raises(service.ConnectionFailure): service.provider_runtime_origin(config)


def test_existing_metadata_disable_delete_work_without_personal_use_scope(client, personal_agent_core):
    api, headers, _ = client
    created = api.post('/api/v1/connections', headers=headers(), json=body()).json()
    personal_agent_core[3].close()
    update = body(api_key=None, enabled=False, label='Kept metadata')
    update.pop('api_key')
    disabled = api.patch(f'/api/v1/connections/{created["id"]}', headers=headers(), json=update)
    assert disabled.status_code == 200, disabled.text
    assert not disabled.json()['enabled'] and disabled.json()['has_secret']
    enabled = api.patch(f'/api/v1/connections/{created["id"]}', headers=headers(), json={**update, 'enabled': True})
    assert enabled.status_code == 422
    deleted = api.delete(f'/api/v1/connections/{created["id"]}', headers=headers())
    assert deleted.status_code == 204, deleted.text


@pytest.mark.parametrize('protocol', ['openai_chat_completions', 'anthropic_messages'])
def test_explicit_api_text_test_and_metadata_export_share_selected_contract(client, personal_agent_core, protocol):
    from app.services import workspace_blueprint
    api, headers, ids = client
    created = api.post('/api/v1/connections', headers=headers(), json=body(protocol)).json()
    seen=[]
    def handler(wire):
        seen.append(wire)
        result = ({'content':[{'type':'text','text':'OK'}]} if protocol=='anthropic_messages'
                  else {'choices':[{'message':{'content':'OK'}}]})
        return httpx.Response(200,json=result)
    api.app.state.connection_transport = httpx.MockTransport(handler)
    tested = api.post(f'/api/v1/connections/{created["id"]}/test', headers=headers(),
        json={'profile':'text','model_id':'qwen3.7-plus'})
    assert tested.status_code == 200 and tested.json()['code']=='connected', tested.text
    assert len(seen)==1
    assert seen[0].url.path == ('/apps/anthropic/v1/messages' if protocol=='anthropic_messages' else '/v1/chat/completions')
    with api.app.state.session_factory() as db:
        row = db.get(ProviderConnection, created['id'])
        exported = workspace_blueprint._export_public_config(row)
        assert exported['provider_id']=='alibaba-coding-plan' and exported['protocol_id']==protocol
        assert exported['fields']=={} and 'personal_agent_runtime' not in exported
        assert catalog.resolve_origin(exported['provider_id'], exported['fields'],
            protocol_id=exported['protocol_id'])==row.base_url


def test_legacy_coding_host_cannot_bypass_scope_and_is_not_reclassified(client, personal_agent_core):
    api, headers, ids = client
    api.app.state.secret_store = SimpleNamespace(personal_agent_runtime=True)
    legacy = {'provider_kind': 'compatible', 'label': 'Existing compatible plan',
        'base_url': catalog.ALIBABA_CHAT_ORIGIN, 'default_model_id': 'qwen3.7-plus',
        'auth_required': True, 'api_key': 'synthetic-plan-key'}
    result = api.post('/api/v1/connections', headers=headers(), json=legacy)
    assert result.status_code == 422 and personal_agent_core[2] == []
    with api.app.state.session_factory() as db:
        row = ProviderConnection(user_id=ids[0], kind='compatible', name='Legacy plan',
            base_url=catalog.ALIBABA_CHAT_ORIGIN, default_model_id='qwen3.7-plus', enabled=True,
            auth_required=True, secret_ref='synthetic-legacy-ref', capabilities={})
        db.add(row); db.commit(); row_id = row.id
        with pytest.raises(service.ConnectionFailure): service.resolve_connection(db, row_id, ids[0], store=None)
        assert row.provider_id is None and row.secret_ref == 'synthetic-legacy-ref'
        assert row.base_url == catalog.ALIBABA_CHAT_ORIGIN


@pytest.mark.parametrize('protocol', ['openai_chat_completions', 'anthropic_messages'])
def test_factory_selects_explicit_dialect_only(protocol):
    assert adapter_for(protocol, 'alibaba-coding-plan').kind == 'alibaba-coding-plan'
    with pytest.raises(ProviderFailure): adapter_for('alibaba-coding-plan')
    with pytest.raises(ProviderFailure): adapter_for('gemini_generate_content', 'alibaba-coding-plan')
