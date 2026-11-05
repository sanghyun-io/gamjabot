import json
from dataclasses import replace
import pytest
from app.services import connections
from app.services.context import provider_text_size,MAX_PROVIDER_CONTEXT_CHARS
from app.services.providers.base import attributed_messages,continuation_scope,ToolObservation
from app.services.providers.chatgpt import ChatGPTAdapter,chatgpt_request_parts,chatgpt_request_text_size,chatgpt_request_body
from test_continuation_budget import configured,tools,event
from app.services.providers.base import ProviderRequest

@pytest.mark.asyncio
async def test_oauth_budget_equals_filtered_wire_parts_after_tool_result(monkeypatch):
    cfg=configured('chatgpt','synthetic','https://api.openai.com/v1','openai_responses')
    history=[event(i,'h'*3200) for i in range(1,4)]+[event(4,'OWNER_EXACT')]
    req=ProviderRequest(connection=cfg,model_id='synthetic',room_id=1,agent_id=1,system='EXACT_ROLE',events=history,
        watermark_seq=4,trigger_event_id='event-4',tools=tools())
    # This deliberately exercises the complete fixture, including the newer
    # Vault login and voice memo tools, before the ChatGPT adapter filters it.
    expected_names = {
        'browser_inspect', 'browser_interact', 'browser_navigate', 'browser_screenshot',
        'cancel_process', 'change_bot_role', 'create_bot', 'create_bot_routine', 'delete_file',
        'edit_bot_memory', 'edit_bot_routine', 'edit_owner_fact', 'edit_owner_skill',
        'forget_bot_memory', 'forget_owner_fact', 'keyboard_chord', 'keyboard_text',
        'list_bots', 'list_teammates', 'message_bot', 'message_bots', 'message_teammate',
        'move_file', 'pointer_click', 'pointer_move', 'pointer_scroll', 'read_bot_routine',
        'read_file', 'request_vault_login', 'save_bot_memory', 'save_owner_fact',
        'save_owner_skill', 'screenshot', 'set_bot_memory_scope', 'set_bot_routine',
        'set_memory_group_access', 'start_process', 'synthesize_voice_memo',
        'test_bot_routine', 'write_file', 'write_page',
    }
    assert len(req.tools)==len(expected_names)==41
    assert {tool['name'] for tool in req.tools}==expected_names
    state={'provider_kind':'chatgpt',**continuation_scope(req),'input':[*attributed_messages(req),
        {'type':'function_call','call_id':'call-1','name':'test_bot_routine','arguments':'{}'}],'call_ids':['call-1']}
    obs=ToolObservation('call-1','completed','QUEUED_RECEIPT')
    req=replace(req,continuation=state,observations=[obs])
    generic=len(req.system)+len(json.dumps(req.tools,ensure_ascii=False))+len(json.dumps(state['input'],ensure_ascii=False))
    parts,offered=chatgpt_request_parts(req)
    assert generic>MAX_PROVIDER_CONTEXT_CHARS and provider_text_size(req)<MAX_PROVIDER_CONTEXT_CHARS
    body,_=chatgpt_request_body(req)
    from app.services.providers.base import provider_json_text
    from app.services.providers.chatgpt import chatgpt_filtered_tools
    from app.services.providers.base import wire_tool_name
    assert offered==chatgpt_filtered_tools(req.connection,req.tools)
    assert {tool['name'] for group in body['tools'] for tool in group['tools']} == {
        wire_tool_name(tool['name']) for tool in offered}
    assert len(offered)<len(req.tools) and provider_text_size(req)==len(provider_json_text(body))==chatgpt_request_text_size(req)
    async def discover(_, transport=None):return ([{'id':'synthetic'}],None,None)
    monkeypatch.setattr(connections,'discover_models',discover)
    sent=[]
    async def post(_request,_path,_headers,body):sent.append(body);return {'output':[{'type':'message','content':[{'type':'output_text','text':'done'}]}]}
    adapter=ChatGPTAdapter();monkeypatch.setattr(adapter,'_post',post)
    await adapter.generate(req)
    assert sent[0]['instructions']==parts['instructions'] and sent[0]['input']==parts['input'] and sent[0]['tools']==parts['tools']
    assert 'EXACT_ROLE' in parts['instructions'] and 'OWNER_EXACT' in json.dumps(parts['input'])
    assert 'QUEUED_RECEIPT' in json.dumps(parts['input']) and 'call-1' in json.dumps(parts['input'])
    huge=replace(req,continuation={**state,'input':state['input']+[{'role':'user','content':'x'*32000}]})
    assert provider_text_size(huge)>MAX_PROVIDER_CONTEXT_CHARS
