import json
from dataclasses import replace
import pytest
from app.services.context import provider_text_size,MAX_PROVIDER_CONTEXT_CHARS
from app.services.context_projection import project_turn_context
from app.services.providers.base import ProviderRequest,ToolObservation,attributed_messages,continuation_scope,provider_json_text
from app.services.providers.go_budget import go_continuation_body,go_continuation_text_size
from app.services.providers.opencode_go import OpenCodeGoMessagesAdapter
from test_continuation_budget import configured,tools,event

@pytest.mark.asyncio
async def test_go_reprojects_history_for_exact_continuation(monkeypatch):
    protocol='anthropic_messages';model='qwen3.7-plus';cfg=configured('opencode-go',model,'https://opencode.ai/zen/go/v1',protocol)
    events=[event(i,'history-'+str(i)+'x'*900) for i in range(1,31)]+[event(31,'OWNER_EXACT')]
    req=ProviderRequest(connection=cfg,model_id=model,room_id=1,agent_id=1,system='ROLE_EXACT',events=events,
        watermark_seq=31,trigger_event_id='event-31',tools=tools())
    state={'provider_kind':'opencode-go',**continuation_scope(req),'assistant_content':[{'type':'tool_use','id':'call-1','name':'edit_bot_routine','input':{}}],'call_ids':['call-1']}
    obs=[ToolObservation('call-1','completed','RECEIPT_EXACT '+('r'*5000))]
    before=project_turn_context(events=events,extractive_checkpoints=[],watermark_seq=31,active_event_id='event-31',agent_id=1,system=req.system,memory={'fact':'KEPT'},tools=req.tools)
    def size(system,messages):return go_continuation_text_size(protocol=protocol,model=model,system=system,tools=req.tools,messages=messages,state=state,observations=obs)
    after=project_turn_context(events=events,extractive_checkpoints=[],watermark_seq=31,active_event_id='event-31',agent_id=1,system=req.system,memory={'fact':'KEPT'},tools=req.tools,size_for_candidate=size)
    assert len(after.events)<len(before.events) and after.used_chars<=MAX_PROVIDER_CONTEXT_CHARS
    assert 'OWNER_EXACT' in json.dumps(after.messages) and 'KEPT' in json.dumps(after.messages)
    req=replace(req,events=after.events,memory=after.memory_text,continuation=state,observations=obs)
    expected=go_continuation_body(protocol=protocol,model=model,system=req.system,tools=req.tools,messages=attributed_messages(req),state=state,observations=obs)
    assert provider_text_size(req)==len(provider_json_text(expected))
    sent=[]
    async def post(_request,_path,_headers,body):sent.append(body);return {'content':[{'type':'text','text':'done'}]}
    adapter=OpenCodeGoMessagesAdapter();monkeypatch.setattr(adapter,'_post',post);await adapter.generate(req)
    assert sent[0]==expected and 'RECEIPT_EXACT' in json.dumps(sent[0]) and 'call-1' in json.dumps(sent[0])
    assert sent[0]['system']=='ROLE_EXACT'
    huge=replace(req,system='z'*32000,events=[event(31,'OWNER_EXACT')])
    assert provider_text_size(huge)>MAX_PROVIDER_CONTEXT_CHARS

@pytest.mark.asyncio
async def test_go_chat_actual_wire_cost_matches_body(monkeypatch):
    from app.services.providers.opencode_go import OpenCodeGoChatAdapter
    protocol='openai_chat_completions';model='glm-5.3-flash';cfg=configured('opencode-go',model,'https://opencode.ai/zen/go/v1',protocol)
    req=ProviderRequest(connection=cfg,model_id=model,room_id=1,agent_id=1,system='ROLE',events=[event(1,'OWNER 한글')],watermark_seq=1,trigger_event_id='event-1',tools=tools())
    assistant={'role':'assistant','content':None,'tool_calls':[{'id':'call-1','type':'function','function':{'name':'test_bot_routine','arguments':'{}'}}]}
    state={'provider_kind':'opencode-go',**continuation_scope(req),'assistant':assistant,'call_ids':['call-1']};obs=[ToolObservation('call-1','completed','RECEIPT')]
    req=replace(req,continuation=state,observations=obs);sent=[]
    async def post(_request,_path,_headers,body):sent.append(body);return {'choices':[{'message':{'content':'done'}}]}
    adapter=OpenCodeGoChatAdapter();monkeypatch.setattr(adapter,'_post',post);await adapter.generate(req)
    assert provider_text_size(req)==len(provider_json_text(sent[0]))
    assert 'OWNER 한글' in json.dumps(sent[0],ensure_ascii=False) and 'RECEIPT' in json.dumps(sent[0])


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol',['openai_chat_completions','anthropic_messages'])
async def test_go_preserves_text_pairs_in_same_turn_and_bounds_wire(monkeypatch,protocol):
    from app.services.providers.base import ProviderFailure, wire_tool_name, provider_json_text
    from app.services.providers.go_budget import validate_go_history
    from app.services.providers.opencode_go import OpenCodeGoChatAdapter
    from app.services.context import provider_wire_limit
    model='glm-5.3-flash' if protocol=='openai_chat_completions' else 'qwen3.7-plus';cfg=configured('opencode-go',model,'https://opencode.ai/zen/go/v1',protocol)
    cfg=replace(cfg,auth_grant_revision=4,owner_epoch='synthetic-epoch')
    offered=[{'name':'synthetic_mcp','description':'synthetic','parameters':{'type':'object','properties':{},'additionalProperties':False}},
             {'name':'write_page','description':'synthetic','parameters':{'type':'object','properties':{},'additionalProperties':False}}]
    req=ProviderRequest(connection=cfg,model_id=model,room_id=1,agent_id=1,system='ROLE',events=[event(1,'OWNER')],
        watermark_seq=1,trigger_event_id='event-1',turn_generation=7,tools=offered)
    adapter=OpenCodeGoChatAdapter() if protocol=='openai_chat_completions' else OpenCodeGoMessagesAdapter()
    sent=[]
    async def post(_request,_path,_headers,body):
        sent.append(body)
        if len(sent)==3:
            return {'choices':[{'message':{'content':'done'}}]} if protocol=='openai_chat_completions' else {'content':[{'type':'text','text':'done'}]}
        name=wire_tool_name('synthetic_mcp' if len(sent)==1 else 'write_page');call='call-mcp' if len(sent)==1 else 'call-page';args={} if len(sent)==1 else {'document':{}}
        if protocol=='openai_chat_completions':
            return {'choices':[{'message':{'content':'','tool_calls':[{'id':call,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}}]}
        return {'content':[{'type':'tool_use','id':call,'name':name,'input':args}]}
    monkeypatch.setattr(adapter,'_post',post)
    first=await adapter.generate(req)
    second_req=replace(req,continuation=first.continuation,observations=[ToolObservation('call-mcp','completed','MCP_RESULT_EXACT'+'x'*16001)])
    second=await adapter.generate(second_req)
    retained=second.continuation['_go_history'][0]['results'][0]['content']
    assert retained=='completed: '+('MCP_RESULT_EXACT'+'x'*16001)[:16000]
    third_req=replace(req,continuation=second.continuation,observations=[ToolObservation('call-page','completed','PAGE_V2_RECEIPT')])
    await adapter.generate(third_req)
    wire=json.dumps(sent[-1],ensure_ascii=False)
    assert wire.count('MCP_RESULT_EXACT')==1 and wire.count('PAGE_V2_RECEIPT')==1
    assert wire.count('call-mcp')==2 and wire.count('call-page')==2
    assert wire.index('MCP_RESULT_EXACT')<wire.index('PAGE_V2_RECEIPT')
    assert provider_text_size(third_req)==len(provider_json_text(sent[-1]))
    assert provider_wire_limit(third_req)==64000
    exact=replace(third_req,system=third_req.system+'x'*(64000-provider_text_size(third_req)))
    assert provider_text_size(exact)==64000 and provider_text_size(replace(exact,system=exact.system+'x'))==64001
    for altered in (replace(third_req,room_id=2),replace(third_req,agent_id=2),replace(third_req,trigger_event_id='other'),
                    replace(third_req,turn_generation=8),replace(third_req,watermark_seq=2),
                    replace(third_req,connection=replace(cfg,owner_id=2)),
                    replace(third_req,connection=replace(cfg,config_revision=2)),
                    replace(third_req,connection=replace(cfg,auth_grant_revision=5)),
                    replace(third_req,connection=replace(cfg,owner_epoch='other'))):
        with pytest.raises(ProviderFailure):validate_go_history(altered,altered.continuation)
    malformed={**second.continuation,'_go_history':[{'assistant':second.continuation['_go_history'][0]['assistant'],'results':[]}]}
    with pytest.raises(ProviderFailure):validate_go_history(third_req,malformed)
    duplicate={**second.continuation,'call_ids':['call-mcp']}
    with pytest.raises(ProviderFailure):validate_go_history(third_req,duplicate)
