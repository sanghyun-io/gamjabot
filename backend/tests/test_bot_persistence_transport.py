"""Synthetic OpenAI-compatible and OAuth function-call transport boundaries."""
import json
import pytest
from app.services import connections
from app.services.bot_persistence import TOOL_SCHEMAS
from app.services.connections import ConnectionConfig
from app.services.providers.base import ProviderRequest, ProviderFailure, normalized_call, wire_tool_name
from app.services.providers.openai import OpenAIAdapter
from app.services.providers.chatgpt import ChatGPTAdapter

CASES=[('save_bot_memory',{'content':'SYNTHETIC'}),
       ('edit_bot_memory',{'memory_id':'5caa6853-5aaa-44e5-8a91-8b59e67f6826','content':'new'}),
       ('forget_bot_memory',{'memory_id':'5caa6853-5aaa-44e5-8a91-8b59e67f6826'}),
       ('change_bot_role',{'content':'new role'})]

@pytest.mark.parametrize('name,args',CASES)
def test_normalized_call_accepts_minimal_arguments(name,args):
    call=normalized_call('call',wire_tool_name(name),args,TOOL_SCHEMAS)
    assert call.arguments==args

@pytest.mark.parametrize('name,args',[('save_bot_memory',{}),('edit_bot_memory',{'content':'new'}),
    ('forget_bot_memory',{'memory_id':'id','unexpected':'PRIVATE'}),('change_bot_role',{'content':42})])
def test_normalized_call_rejects_invalid_arguments(name,args):
    with pytest.raises(ProviderFailure): normalized_call('call',wire_tool_name(name),args,TOOL_SCHEMAS)

@pytest.mark.asyncio
@pytest.mark.parametrize('adapter_kind',['openai','chatgpt'])
@pytest.mark.parametrize('name,args',CASES)
async def test_transport_decodes_minimal_function_call(monkeypatch,adapter_kind,name,args):
    model='synthetic-model';base='https://api.openai.com/v1'
    caps={'verified':{'model_id':model,'base_url':base,'tools':True},
        'model_features':{model:{'model_id':model,'base_url':base,'config_revision':1,
            'tested':{'text_generation':True,'tool_calling':True},'tool_families':{}}}}
    cfg=ConnectionConfig(1,adapter_kind,model,base,'synthetic-token',caps,
        provider_id='chatgpt' if adapter_kind=='chatgpt' else 'openai',
        protocol_id='openai_responses' if adapter_kind=='chatgpt' else 'openai_chat_completions',config_revision=1)
    req=ProviderRequest(connection=cfg,model_id=model,room_id=1,agent_id=1,tools=TOOL_SCHEMAS)
    wire=wire_tool_name(name)
    if adapter_kind=='chatgpt':
        async def discover(_):return [{'id':model}],False,None
        monkeypatch.setattr(connections,'discover_models',discover)
        adapter=ChatGPTAdapter()
        async def post(request,path,headers,body):
            assert wire in {t['name'] for t in body['tools'][0]['tools']}
            return {'output':[{'type':'function_call','namespace':'gamja','call_id':'call','name':wire,'arguments':json.dumps(args)}]}
    else:
        adapter=OpenAIAdapter()
        from app.services import model_features
        async def enforce(_):pass
        monkeypatch.setattr(model_features,'enforce',enforce)
        async def post(request,path,headers,body):
            assert wire in {t['function']['name'] for t in body['tools']}
            return {'choices':[{'message':{'content':'','tool_calls':[{'id':'call','function':{'name':wire,'arguments':json.dumps(args)}}]}}]}
    monkeypatch.setattr(adapter,'_post',post)
    result=await adapter.generate(req)
    assert result.tool_calls[0].arguments==args

@pytest.mark.parametrize('name,args,expected',[
    ('edit_bot_memory',{'content':'PRIVATE_VALUE'}, {'validator':'required','field':'memory_id'}),
    ('save_bot_memory',{'content':'PRIVATE_VALUE','PRIVATE_KEY':'PRIVATE_VALUE'}, {'validator':'additionalProperties','field':None}),
    ('change_bot_role',{'content':42}, {'validator':'type','field':'content'}),
])
def test_invalid_tool_diagnostic_has_no_input(name,args,expected):
    from app.services.diagnostic_detail import make_diagnostic
    with pytest.raises(ProviderFailure) as caught:
        normalized_call('call',wire_tool_name(name),args,TOOL_SCHEMAS)
    record=make_diagnostic(caught.value,stage='provider_request',failure_code=caught.value.code,
        room_id=1,agent_id=1,connection_id=1,trigger_event_id=None,action_id=None)
    assert record['tool_schema']==expected
    assert 'PRIVATE_VALUE' not in json.dumps(record) and 'PRIVATE_KEY' not in json.dumps(record)


def test_only_nine_offered_persistence_names_are_plain():
    from app.services.bot_persistence import EXTENDED_SCHEMAS
    from app.services.providers.base import PERSISTENCE_WIRE_NAMES
    from app.services.core_provider_transport import _valid_wire_name
    offered=TOOL_SCHEMAS+EXTENDED_SCHEMAS
    assert {item['name'] for item in offered} <= PERSISTENCE_WIRE_NAMES
    assert PERSISTENCE_WIRE_NAMES - {item['name'] for item in offered} == {'save_owner_skill','edit_owner_skill','create_bot_routine','edit_bot_routine','set_bot_routine','test_bot_routine','read_bot_routine'}
    for item in offered:
        name=item['name']
        assert wire_tool_name(name)==name and _valid_wire_name(name)
        assert not _valid_wire_name('other_'+name)
        with pytest.raises(ProviderFailure):
            normalized_call('id',name,{},[tool for tool in offered if tool['name']!=name])
        with pytest.raises(ProviderFailure):
            normalized_call('id','gamja_'+name.encode().hex(),{},offered)
    assert wire_tool_name('read_file')=='gamja_726561645f66696c65'
    assert _valid_wire_name(wire_tool_name('read_file'))
    assert not _valid_wire_name('save_bot_memory_extra')


@pytest.mark.parametrize('call_id,name,args,reason,offered',[
    (None,'save_bot_memory',{'content':'PRIVATE'},'missing_id',True),
    ('id','PRIVATE_UNKNOWN',{'content':'PRIVATE'},'unrecognized_name',False),
    ('id','save_bot_memory','PRIVATE','nonobject_arguments',True),
    ('id','save_bot_memory',{'workflow_id':{'PRIVATE':'PRIVATE'}},'workflow_type',True),
])
def test_invalid_call_diagnostics_are_fixed_and_never_include_input(call_id,name,args,reason,offered):
    from app.services.diagnostic_detail import make_diagnostic
    with pytest.raises(ProviderFailure) as caught:
        normalized_call(call_id,name,args,TOOL_SCHEMAS)
    record=make_diagnostic(caught.value,stage='provider_request',failure_code=caught.value.code,
        room_id=1,agent_id=1,connection_id=1,trigger_event_id=None,action_id=None)
    assert record['tool_call']=={'reason':reason,'offered_plain_builtin':offered}
    assert 'PRIVATE' not in json.dumps(record)



@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['openai','anthropic'])
async def test_direct_adapter_missing_id_reports_safe_shape(monkeypatch,kind):
    from app.services.providers.anthropic import AnthropicAdapter
    from app.services import model_features
    async def enforce(_):pass
    monkeypatch.setattr(model_features,'enforce',enforce)
    model='synthetic-model';base='https://api.openai.com/v1'
    caps={'verified':{'model_id':model,'base_url':base,'tools':True},
        'model_features':{model:{'model_id':model,'base_url':base,'config_revision':1,
            'tested':{'text_generation':True,'tool_calling':True},'tool_families':{}}}}
    cfg=ConnectionConfig(1,'openai',model,base,'synthetic-token',caps,config_revision=1)
    req=ProviderRequest(connection=cfg,model_id=model,room_id=1,agent_id=1,tools=TOOL_SCHEMAS)
    adapter=OpenAIAdapter() if kind=='openai' else AnthropicAdapter()
    async def post(request,path,headers,body):
        if kind=='openai':return {'choices':[{'message':{'tool_calls':[{'function':{'name':'save_bot_memory','arguments':'{"content":"PRIVATE"}'}}]}}]}
        return {'content':[{'type':'tool_use','name':'save_bot_memory','input':{'content':'PRIVATE'}}]}
    monkeypatch.setattr(adapter,'_post',post)
    with pytest.raises(ProviderFailure) as caught:await adapter.generate(req)
    assert caught.value.tool_call_diagnostic=={'reason':'missing_id','offered_plain_builtin':True}


def test_tool_validation_diagnostic_is_bounded_and_never_contains_arguments():
    from app.services.diagnostic_detail import make_diagnostic
    from app.services.page_persistence import TOOL_SCHEMAS as PAGE_TOOLS
    mcp={'name':'mcp_0123456789abcdef0123','mcp_binding':{},
         'parameters':{'type':'object','properties':{'count':{'type':'integer'}},'required':['count']}}
    cases=[(PAGE_TOOLS[0],{'document':'PRIVATE_VALUE'},'write_page','argument_validation'),
           (mcp,{'count':'PRIVATE_VALUE'},'mcp','argument_validation'),
           ({**mcp,'parameters':{'$ref':'https://PRIVATE.invalid/schema'}},{},'mcp','schema_reference'),
           ({'name':'read_file','parameters':{'type':'object'}},{'PRIVATE_KEY':'PRIVATE_VALUE'*4000},'other_builtin','arguments_size'),
           ({'name':'read_file','parameters':{'type':'object'}},{'PRIVATE_KEY':float('nan')},'other_builtin','arguments_json')]
    for tool,args,family,reason in cases:
        with pytest.raises(ProviderFailure) as caught:
            normalized_call('call',wire_tool_name(tool['name']),args,[tool])
        record=make_diagnostic(caught.value,stage='provider_request',failure_code=caught.value.code,
            room_id=1,agent_id=1,connection_id=1,trigger_event_id=None,action_id=None)
        assert record['tool_validation']=={'family':family,'reason':reason}
        assert 'PRIVATE' not in json.dumps(record)
    error=ProviderFailure('provider_invalid_response','fixed')
    error.tool_validation_diagnostic={'family':['PRIVATE'],'reason':'argument_validation'}
    record=make_diagnostic(error,stage='provider_request',failure_code=error.code,
        room_id=1,agent_id=1,connection_id=1,trigger_event_id=None,action_id=None)
    assert 'tool_validation' not in record and 'PRIVATE' not in json.dumps(record)
