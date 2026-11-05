import json
from dataclasses import replace
import pytest
from app.services.connections import ConnectionConfig
from app.services.context import provider_text_size,MAX_PROVIDER_CONTEXT_CHARS,persistence_tools
from app.services.providers.base import ProviderRequest,ToolObservation,continuation_scope,attributed_messages,observation_text,projected_text_size
from app.services.providers.chatgpt import ChatGPTAdapter
from app.services.providers.opencode_go import OpenCodeGoMessagesAdapter
from app.services import teammates,bot_messages,connections
from app.services.tool_broker import TOOL_SCHEMAS


def configured(provider,model,base,protocol):
    caps={'declared':{'tools':True},'verified':{'model_id':model,'base_url':base,'tools':True},
          'model_features':{model:{'model_id':model,'base_url':base,'config_revision':1,'reported':{'text_generation':True,'tool_calling':True},'tested':{'text_generation':True,'tool_calling':True}}}}
    return ConnectionConfig(1,provider,model,base,'synthetic-only',caps,provider_id=provider,protocol_id=protocol,owner_id=1,config_revision=1)

def tools():return [*TOOL_SCHEMAS,*teammates.TOOL_SCHEMAS,*bot_messages.TOOL_SCHEMAS,*persistence_tools(True,True,True)]
def event(seq,content):return {'event_id':f'event-{seq}','room_id':1,'seq':seq,'actor_type':'user','actor_id':1,'actor_name_snapshot':'Owner','kind':'message','payload':{'content':content}}

@pytest.mark.asyncio
async def test_oauth_counts_actual_frozen_input_once_and_preserves_receipt(monkeypatch):
    cfg=configured('chatgpt','synthetic','https://api.openai.com/v1','openai_responses')
    req=ProviderRequest(connection=cfg,model_id='synthetic',room_id=1,agent_id=1,system='ROLE_PRESERVED',events=[event(1,'history '*900),event(2,'CURRENT_OWNER')],watermark_seq=2,trigger_event_id='event-2',tools=tools())
    call={'type':'function_call','name':'create_bot_routine','call_id':'call-1','arguments':'{"name":"test"}'}
    state={'provider_kind':'chatgpt',**continuation_scope(req),'input':[*attributed_messages(req),call],'call_ids':['call-1']}
    obs=ToolObservation('call-1','completed','RECEIPT '+('r'*800));req=replace(req,continuation=state,observations=[obs])
    assert len(req.tools)==39 and any(t['name']=='write_page' for t in req.tools)
    prior=projected_text_size(system=req.system,tools=req.tools,messages=attributed_messages(req),continuation=state,observations=[obs])
    assert prior>MAX_PROVIDER_CONTEXT_CHARS and provider_text_size(req)<MAX_PROVIDER_CONTEXT_CHARS
    async def discover(_, transport=None):return ([{'id':'synthetic'}],None,None)
    monkeypatch.setattr(connections,'discover_models',discover)
    sent=[]
    async def post(_request,_path,_headers,body):sent.append(body);return {'output':[{'type':'message','content':[{'type':'output_text','text':'saved'}]}]}
    adapter=ChatGPTAdapter();monkeypatch.setattr(adapter,'_post',post)
    await adapter.generate(req)
    actual=sent[0]['input'];assert actual==[*state['input'],{'type':'function_call_output','call_id':'call-1','output':observation_text(obs)}]
    assert 'CURRENT_OWNER' in json.dumps(actual) and 'RECEIPT' in json.dumps(actual) and sent[0]['instructions'].startswith('ROLE_PRESERVED')
    assert provider_text_size(replace(req,continuation={**state,'input':state['input']+[{'role':'user','content':'x'*32000}]}))>MAX_PROVIDER_CONTEXT_CHARS

@pytest.mark.asyncio
async def test_go_continuation_keeps_rebuilt_history_and_tool_observation(monkeypatch):
    cfg=configured('opencode-go','qwen3.7-plus','https://opencode.ai/zen/go/v1','anthropic_messages')
    req=ProviderRequest(connection=cfg,model_id=cfg.model_id,room_id=1,agent_id=1,system='ROLE_PRESERVED',events=[event(1,'CURRENT_OWNER')],watermark_seq=1,trigger_event_id='event-1',tools=tools())
    blocks=[{'type':'tool_use','id':'call-1','name':'create_bot_routine','input':{}}]
    state={'provider_kind':'opencode-go',**continuation_scope(req),'assistant_content':blocks,'call_ids':['call-1']}
    obs=ToolObservation('call-1','completed','RECEIPT');req=replace(req,continuation=state,observations=[obs]);sent=[]
    async def post(_request,_path,_headers,body):sent.append(body);return {'content':[{'type':'text','text':'done'}]}
    adapter=OpenCodeGoMessagesAdapter();monkeypatch.setattr(adapter,'_post',post)
    await adapter.generate(req)
    assert sent[0]['messages']==[*attributed_messages(req),{'role':'assistant','content':blocks},
        {'role':'user','content':[{'type':'tool_result','tool_use_id':'call-1','content':[{'type':'text','text':observation_text(obs)}],'is_error':False}]}]
    assert sent[0]['system']=='ROLE_PRESERVED'
    from app.services.providers.base import provider_json_text
    assert provider_text_size(req)==len(provider_json_text(sent[0]))

@pytest.mark.parametrize('protocol',['anthropic_messages','openai_chat_completions'])
def test_go_wire_allowance_preserves_tool_pair_and_fixed_bound(protocol):
    from app.services.context import provider_wire_limit, MAX_GO_CONTINUATION_WIRE_CHARS
    cfg=configured('opencode-go','qwen3.7-plus','https://example.invalid',protocol)
    req=ProviderRequest(connection=cfg,model_id=cfg.model_id,room_id=1,agent_id=1,system='ROLE',events=[event(1,'OWNER')],watermark_seq=1,trigger_event_id='event-1',tools=tools())
    assert provider_wire_limit(req)==32000
    obs=ToolObservation('call-1','completed','SAVED_PAGE_RECEIPT')
    def with_body(size):
        if protocol=='anthropic_messages':
            state={'provider_kind':'opencode-go',**continuation_scope(req),'assistant_content':[{'type':'tool_use','id':'call-1','name':'write_page','input':{'document':{'text':'x'*size}}}],'call_ids':['call-1']}
        else:
            state={'provider_kind':'opencode-go',**continuation_scope(req),'assistant':{'role':'assistant','content':None,'tool_calls':[{'id':'call-1','type':'function','function':{'name':'write_page','arguments':json.dumps({'document':{'text':'x'*size}})}}]},'call_ids':['call-1']}
        return replace(req,continuation=state,observations=[obs])
    mid=with_body(10000)
    assert 32000 < provider_text_size(mid) < MAX_GO_CONTINUATION_WIRE_CHARS
    assert provider_wire_limit(mid)==64000
    from app.services.providers.go_budget import go_continuation_body
    body=go_continuation_body(protocol=protocol,model=cfg.model_id,system=mid.system,tools=mid.tools,messages=attributed_messages(mid),state=mid.continuation,observations=mid.observations)
    assert 'SAVED_PAGE_RECEIPT' in json.dumps(body) and 'write_page' in json.dumps(body)
    overhead=provider_text_size(with_body(0))
    exact=with_body(64000-overhead)
    over=with_body(64001-overhead)
    assert provider_text_size(exact)==provider_wire_limit(exact)==64000
    assert provider_text_size(over)==64001 > provider_wire_limit(over)
    other=replace(mid,connection=configured('other',cfg.model_id,'https://example.invalid',protocol))
    assert provider_wire_limit(other)==32000
