"""Adapter wire/gating checks; not proof of real provider access or native delivery."""
import json

import httpx
import pytest

from app.services.connections import ConnectionConfig
from app.services.providers.base import ProviderFailure, ProviderRequest
from app.services.providers.alibaba_coding_plan import (
    CHAT_ORIGIN, MESSAGES_ORIGIN,
    AlibabaCodingPlanChatAdapter, AlibabaCodingPlanMessagesAdapter,
)


def request(protocol, *, runtime=False, model='qwen3.7-plus'):
    origin = CHAT_ORIGIN if protocol == 'openai_chat_completions' else MESSAGES_ORIGIN
    config = ConnectionConfig(41, 'compatible', model, origin, 'synthetic-plan-key', {},
        provider_id='alibaba-coding-plan', protocol_id=protocol, auth_method_id='api_key',
        public_config_version=1, public_fields={}, auth_grant_revision=1,
        credential_state='present', owner_id=9)
    config.personal_agent_runtime = runtime
    return ProviderRequest(connection=config, model_id=model, agent_id=2, room_id=3,
        events=[{'event_id': 'synthetic-user-trigger', 'seq': 1, 'kind': 'message', 'actor_type': 'user', 'actor_id': 9,
                 'actor_name_snapshot': 'Test user', 'payload': {'content': 'Reply OK.'}}],
        watermark_seq=1, trigger_event_id='synthetic-user-trigger')


@pytest.mark.asyncio
@pytest.mark.parametrize('adapter,protocol', [
    (AlibabaCodingPlanChatAdapter, 'openai_chat_completions'),
    (AlibabaCodingPlanMessagesAdapter, 'anthropic_messages'),
])
@pytest.mark.parametrize('flag', [False, None, 'true', 1, True])
async def test_non_runtime_or_forged_flag_refused_before_transport(adapter, protocol, flag):
    sent=[]
    with pytest.raises(ProviderFailure) as error:
        await adapter(httpx.MockTransport(lambda wire: sent.append(wire))).generate(
            request(protocol, runtime=flag))
    assert error.value.code == 'personal_agent_runtime_required'
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize('adapter,protocol,model', [
    (AlibabaCodingPlanChatAdapter, 'anthropic_messages', 'qwen3.7-plus'),
    (AlibabaCodingPlanMessagesAdapter, 'openai_chat_completions', 'qwen3.7-plus'),
    (AlibabaCodingPlanChatAdapter, 'openai_chat_completions', 'gpt-6-luna'),
    (AlibabaCodingPlanMessagesAdapter, 'anthropic_messages', 'minimax-m2.5'),
])
async def test_wrong_dialect_or_exact_model_refused_before_transport(personal_agent_core, adapter, protocol, model):
    sent=[]
    config = request(protocol, runtime=True, model=model)
    config.connection.personal_agent_binding = personal_agent_core[0]
    with pytest.raises(ProviderFailure) as error:
        await adapter(httpx.MockTransport(lambda wire: sent.append(wire))).generate(
            config)
    assert error.value.code in {'invalid_configuration', 'unsupported_model_protocol'}
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize('adapter,protocol,origin,expected_path', [
    (AlibabaCodingPlanChatAdapter, 'openai_chat_completions', CHAT_ORIGIN, '/v1/chat/completions'),
    (AlibabaCodingPlanMessagesAdapter, 'anthropic_messages', MESSAGES_ORIGIN, '/apps/anthropic/v1/messages'),
])
async def test_typed_wire_contract_uses_real_catalog_and_parent_transport(
        personal_agent_core, adapter, protocol, origin, expected_path):
    # Use the real typed catalog/origin contract; native bindings are covered separately.
    seen=[]
    def handler(wire):
        seen.append(wire)
        if protocol == 'anthropic_messages':
            return httpx.Response(200,json={'content':[{'type':'text','text':'OK'}]})
        return httpx.Response(200,json={'choices':[{'message':{'content':'OK'}}]})
    config = request(protocol, runtime=True)
    config.connection.personal_agent_binding = personal_agent_core[0]
    from app.services.providers import adapter_for
    selected = adapter_for(protocol, 'alibaba-coding-plan', httpx.MockTransport(handler))
    assert isinstance(selected, adapter)
    result=await selected.generate(config)
    assert result.text == 'OK' and len(seen)==1
    wire=seen[0]
    assert wire.url.scheme=='https' and wire.url.host=='coding-intl.dashscope.aliyuncs.com'
    assert wire.url.path==expected_path
    body=json.loads(wire.content)
    assert body['model']=='qwen3.7-plus' and 'api_key' not in body
    if protocol=='anthropic_messages':
        assert wire.headers['x-api-key']=='synthetic-plan-key'
        assert wire.headers['anthropic-version']=='2023-06-01'
        assert 'authorization' not in wire.headers and body['max_tokens']>0
    else:
        assert wire.headers['authorization']=='Bearer synthetic-plan-key'
        assert 'x-api-key' not in wire.headers and body['messages'][0]['role']=='system'


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol,path', [
    ('openai_chat_completions', '/v1/chat/completions'),
    ('anthropic_messages', '/apps/anthropic/v1/messages'),
])
async def test_explicit_text_test_uses_selected_origin_without_discovery(personal_agent_core, protocol, path):
    from app.services.connections import probe
    config = request(protocol, runtime=True).connection
    config.personal_agent_binding = personal_agent_core[0]
    seen=[]
    def handler(wire):
        seen.append(wire)
        answer = ({'content':[{'type':'text','text':'OK'}]} if protocol=='anthropic_messages'
                  else {'choices':[{'message':{'content':'OK'}}]})
        return httpx.Response(200,json=answer)
    assert (await probe(config, httpx.MockTransport(handler)))['code'] == 'connected'
    assert len(seen)==1 and seen[0].url.path==path
    assert json.loads(seen[0].content)['max_tokens'] > 0
