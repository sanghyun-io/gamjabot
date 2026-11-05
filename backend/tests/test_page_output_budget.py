"""Synthetic provider output budget and Page argument boundaries."""
import json
from dataclasses import replace

import pytest
from test_room_state import room_client

from app.services.connections import ConnectionConfig
from app.services.providers.base import ProviderFailure, ProviderRequest, ToolObservation, continuation_scope, normalized_call, check_options
from app.services.providers.anthropic import AnthropicAdapter
from app.services.providers.opencode_go import OpenCodeGoMessagesAdapter
from app.services.providers.go_budget import go_initial_body, go_continuation_body
from app.services.page_persistence import TOOL_SCHEMAS
from app.services.diagnostic_detail import make_diagnostic
from app.services.scheduler import validate_tool_calls
from app.services.llm_service import LLMFailure


def request():
    base='https://opencode.ai/zen/go/v1';model='qwen3.7-plus'
    caps={'declared':{'tools':True,'max_tokens':True},'verified':{'model_id':model,'base_url':base,'tools':True,'max_tokens':True}}
    config=ConnectionConfig(2,'compatible',model,base,'synthetic',caps,provider_id='opencode-go',
        protocol_id='anthropic_messages',owner_id=1,config_revision=1)
    return ProviderRequest(connection=config,model_id=model,room_id=1,agent_id=1,system='role')


@pytest.mark.asyncio
async def test_go_auto_initial_continuation_explicit_and_non_go_default(monkeypatch):
    req=request();sent=[]
    async def post(_request,_path,_headers,body):
        sent.append(body);return {'content':[{'type':'text','text':'ok'}]}
    go=OpenCodeGoMessagesAdapter();monkeypatch.setattr(go,'_post',post)
    await go.generate(req)
    state={'provider_kind':'opencode-go',**continuation_scope(req),'assistant_content':[], 'call_ids':['c']}
    await go.generate(replace(req,continuation=state,observations=[ToolObservation('c','completed','ok')]))
    check_options(replace(req,max_tokens=777))
    await go.generate(replace(req,max_tokens=777))
    assert [body['max_tokens'] for body in sent]==[4096,4096,777]
    assert go_initial_body(protocol='anthropic_messages',model=req.model_id,system='',tools=[],messages=[])['max_tokens']==4096
    assert go_continuation_body(protocol='anthropic_messages',model=req.model_id,system='',tools=[],messages=[],state=state,observations=[ToolObservation('c','completed','ok')])['max_tokens']==4096
    plain=AnthropicAdapter();monkeypatch.setattr(plain,'_post',post);monkeypatch.setattr(plain,'_headers',lambda _: {})
    await plain.generate(req)
    assert sent[-1]['max_tokens']==1024


@pytest.mark.asyncio
async def test_explicit_output_limit_stop_rejects_even_structurally_valid_tool(monkeypatch):
    req=replace(request(),tools=TOOL_SCHEMAS)
    document={'format':'gamja.page.v1','title':'A','datasets':[],'views':[],
              'blocks':[{'type':'heading','level':1,'text':'secret-value'}]}
    data={'stop_reason':'max_tokens','usage':{'output_tokens':1234},'content':[{'type':'tool_use','id':'c','name':'write_page','input':{'document':document}}]}
    async def post(*_args):return data
    adapter=OpenCodeGoMessagesAdapter();monkeypatch.setattr(adapter,'_post',post)
    monkeypatch.setattr('app.services.providers.anthropic.normalized_call',lambda *_a,**_k:pytest.fail('tool parser must not run'))
    with pytest.raises(ProviderFailure) as exc:await adapter.generate(req)
    assert exc.value.code=='provider_output_truncated'
    diag=make_diagnostic(exc.value,stage='provider_request',failure_code=exc.value.code,room_id=1,agent_id=1,connection_id=2,trigger_event_id=None,action_id=None)
    assert diag['provider_output']=={'stop_reason':'max_tokens','output_limit':4096,'reported_output_tokens':1234}
    assert 'secret-value' not in json.dumps(diag)


@pytest.mark.asyncio
@pytest.mark.parametrize('stop',[None,'other'])
async def test_missing_or_unknown_stop_reason_does_not_infer_truncation(monkeypatch,stop):
    req=request();data={'content':[{'type':'text','text':'ok'}]}
    if stop is not None:data['stop_reason']=stop
    async def post(*_args):return data
    adapter=OpenCodeGoMessagesAdapter();monkeypatch.setattr(adapter,'_post',post)
    result=await adapter.generate(req)
    assert result.text=='ok'


@pytest.mark.parametrize('args,expected',[( {},{'tool':'write_page','validator':'required','field':'document'}),
    ({'document':'secret-value'},{'tool':'write_page','validator':'type','field':'document'}),
    ({'document':{},'secret-key':'secret-value'},{'tool':'write_page','validator':'additionalProperties','field':None})])
def test_page_bad_envelope_has_only_fixed_safe_diagnostic(args,expected):
    with pytest.raises(ProviderFailure) as exc:normalized_call('c','write_page',args,TOOL_SCHEMAS)
    assert exc.value.schema_diagnostic==expected
    diag=make_diagnostic(exc.value,stage='provider_request',failure_code=exc.value.code,room_id=1,agent_id=1,connection_id=2,trigger_event_id=None,action_id=None)
    assert diag['tool_schema']==expected and 'secret' not in json.dumps(diag)
    from app.services.providers.base import ToolCall
    with pytest.raises(LLMFailure) as scheduler_exc:validate_tool_calls([ToolCall('c','write_page',args)],TOOL_SCHEMAS,{'call_ids':['c']})
    assert scheduler_exc.value.schema_diagnostic==expected
    scheduler_diag=make_diagnostic(scheduler_exc.value,stage='tool_validation',failure_code=scheduler_exc.value.code,room_id=1,agent_id=1,connection_id=2,trigger_event_id=None,action_id=None)
    assert scheduler_diag['tool_schema']==expected and 'secret' not in json.dumps(scheduler_diag)


@pytest.mark.parametrize('case',['truncated','bad_envelope'])
def test_page_provider_failure_is_persisted_without_page_or_action(room_client,monkeypatch,case):
    from types import SimpleNamespace
    from test_room_state import headers
    from test_bot_creation import private_team
    from app.models.room_result import RoomResult
    from app.models.collaboration import RoomEvent,ToolAction
    client,(owner,_,project,_,_)=room_client
    bot,room,_=private_team(client,owner,project)
    scheduler=client.app.state.scheduler;scheduler.broker.executor=SimpleNamespace()
    document={'format':'gamja.page.v1','title':'A','datasets':[],'views':[],
              'blocks':[{'type':'heading','level':1,'text':'private-value'}]}
    payload={'content':[{'type':'tool_use','id':'c','name':'write_page',
                         'input':{'document':document if case=='truncated' else 'private-value'}}]}
    if case=='truncated':payload.update(stop_reason='max_tokens',usage={'output_tokens':4096})
    async def post(*_args):return payload
    adapter=AnthropicAdapter();adapter.kind='opencode-go'
    monkeypatch.setattr(adapter,'_headers',lambda _: {})
    monkeypatch.setattr(adapter,'_post',post)
    scheduler.provider=adapter.generate
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=headers(client,owner),json={'content':'@Ada create Page'})
    assert response.status_code==502
    with client.app.state.session_factory() as db:
        assert db.query(RoomResult).filter_by(owner_id=owner,room_id=room).count()==0
        assert db.query(ToolAction).filter_by(room_id=room).count()==0
        failure=db.query(RoomEvent).filter_by(room_id=room,kind='failure').one().payload
        assert 'private-value' not in json.dumps(failure)
        if case=='truncated':
            assert failure['code']=='provider_output_truncated'
            assert failure['diagnostic']['provider_output']=={'stop_reason':'max_tokens','output_limit':4096,'reported_output_tokens':4096}
        else:
            assert failure['code']=='provider_invalid_response'
            assert failure['diagnostic']['tool_schema']=={'tool':'write_page','validator':'type','field':'document'}
