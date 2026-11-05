import json
from dataclasses import replace
from types import SimpleNamespace
import pytest
from app.services.context import projection_wire_size, provider_text_size, MAX_PROVIDER_CONTEXT_CHARS
from app.services.context_projection import project_turn_context, ProjectionError
from app.services.providers.base import ProviderRequest, ToolObservation, attributed_messages, continuation_scope, ProviderFailure
from app.services.providers.chatgpt import chatgpt_request_body, freeze_history_prefix, compact_frozen_history
from test_continuation_budget import configured, tools, event


def row(config):
    return SimpleNamespace(id=1,kind=config.provider_kind,provider_id=config.provider_id,
        protocol_id=config.protocol_id,base_url=config.base_url,capabilities=config.capabilities,readiness_revision=1)


def test_korean_initial_wire_projection_preserves_owner_and_memory():
    cfg=configured('chatgpt','synthetic','https://api.openai.com/v1','openai_responses')
    events=[event(i,'과거기록'*450) for i in range(1,10)]+[event(10,'현재 요청 원문')]
    t=tools(); size=lambda s,m: projection_wire_size(row(cfg),'synthetic',s,m,t)
    p=project_turn_context(events=events,extractive_checkpoints=[],watermark_seq=10,active_event_id='event-10',agent_id=1,system='역할 원문',memory={'fact':'기억 원문'},tools=t,size_for_candidate=size)
    req=ProviderRequest(connection=cfg,model_id='synthetic',system='역할 원문',agent_id=1,room_id=1,events=p.events,watermark_seq=10,trigger_event_id='event-10',memory=p.memory_text,tools=t)
    body,_=chatgpt_request_body(req)
    assert p.used_chars==provider_text_size(req)==len(json.dumps(body))<=MAX_PROVIDER_CONTEXT_CHARS
    assert '현재 요청 원문' in str(body) and '기억 원문' in str(body) and '역할 원문' in str(body)
    with pytest.raises(ProjectionError):
        project_turn_context(events=[event(1,'현재')],extractive_checkpoints=[],watermark_seq=1,active_event_id='event-1',agent_id=1,system='역'*9000,memory={},tools=t,size_for_candidate=size)


def test_frozen_history_compaction_preserves_memory_owner_call_and_receipt():
    cfg=configured('chatgpt','synthetic','https://api.openai.com/v1','openai_responses')
    req=ProviderRequest(connection=cfg,model_id='synthetic',system='역할',agent_id=1,room_id=1,
        events=[event(1,'과거'*2000),event(2,'더 이전'*1200),event(3,'현재 원문')],watermark_seq=3,
        trigger_event_id='event-3',memory='기억 원문',tools=tools())
    frozen=freeze_history_prefix(req); assert frozen['removable']==[1,2]
    call={'type':'function_call','call_id':'exact-call','name':'test_bot_routine','arguments':'{}'}
    state={'provider_kind':'chatgpt',**continuation_scope(req),'input':[*attributed_messages(req),call],'call_ids':['exact-call']}
    request=replace(req,continuation=state,observations=[ToolObservation('exact-call','completed','정확 영수증')])
    compacted,updated=compact_frozen_history(request,frozen,MAX_PROVIDER_CONTEXT_CHARS)
    assert provider_text_size(compacted)<=MAX_PROVIDER_CONTEXT_CHARS
    body,_=chatgpt_request_body(compacted); text=str(body)
    for value in ['기억 원문','현재 원문','역할','exact-call','정확 영수증']:
        assert value in text
    assert len(updated['removable'])<2 and body['input'][-2]==call
    broken=replace(request,continuation={**state,'input':[{'role':'user','content':'changed'},*state['input'][1:]]})
    with pytest.raises(ProviderFailure):compact_frozen_history(broken,frozen,MAX_PROVIDER_CONTEXT_CHARS)
    huge=replace(request,observations=[ToolObservation('exact-call','completed','영'*20000)])
    oversized,_=compact_frozen_history(huge,frozen,MAX_PROVIDER_CONTEXT_CHARS)
    assert provider_text_size(oversized)>MAX_PROVIDER_CONTEXT_CHARS


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['anthropic_messages','openai_chat_completions'])
async def test_go_initial_wire_is_adapter_body(protocol, monkeypatch):
    from app.services.providers.opencode_go import OpenCodeGoMessagesAdapter, OpenCodeGoChatAdapter
    cfg=configured('opencode-go','synthetic','https://opencode.ai/zen/go/v1',protocol)
    request=ProviderRequest(connection=cfg,model_id='synthetic',system='역할',agent_id=1,room_id=1,
        events=[event(1,'현재 한글')],watermark_seq=1,trigger_event_id='event-1',tools=tools())
    adapter=OpenCodeGoMessagesAdapter() if protocol=='anthropic_messages' else OpenCodeGoChatAdapter()
    sent=[]
    async def post(_request,_path,_headers,body):
        sent.append(body)
        return {'content':[{'type':'text','text':'done'}]} if protocol=='anthropic_messages' else {'choices':[{'message':{'content':'done'}}]}
    monkeypatch.setattr(adapter,'_post',post)
    await adapter.generate(request)
    assert provider_text_size(request)==len(json.dumps(sent[0]))
    assert projection_wire_size(row(cfg),'synthetic',request.system,attributed_messages(request),request.tools)==len(json.dumps(sent[0]))


from test_room_state import room_client

def test_oauth_preview_dispatch_same_korean_wire_budget(room_client,monkeypatch):
    from test_room_state import headers
    from test_bot_creation import private_team
    from test_execution_scopes import FakeExecutor
    from app.models.models import Agent
    from app.models.collaboration import ProviderConnection
    from app.services.events import append_event,ActorRef
    from app.services.providers import ProviderResult
    import app.services.context as context
    client,(owner,_,project,_,_)=room_client;auth=headers(client,owner)
    bot,room,_=private_team(client,owner,project)
    client.app.state.scheduler.broker.executor=FakeExecutor()
    from app.services.tool_broker import TOOL_SCHEMAS
    client.app.state.scheduler.broker.tool_schemas=TOOL_SCHEMAS[:1]
    cfg=configured('chatgpt','synthetic','https://api.openai.com/v1','openai_responses')
    with client.app.state.session_factory() as db:
        agent=db.get(Agent,bot); connection=db.get(ProviderConnection,agent.connection_id)
        cfg=replace(cfg,connection_id=connection.id,owner_id=owner)
        agent.model_id='synthetic';connection.provider_id='chatgpt';connection.protocol_id='openai_responses'
        connection.kind='chatgpt';connection.base_url=cfg.base_url;connection.default_model_id='synthetic';connection.capabilities=cfg.capabilities
        for i in range(20):append_event(db,room,ActorRef('user',owner),'message',{'content':('과거기록 '+str(i))*2})
        db.commit()
        from app.services.memory_context import snapshot
        state=snapshot(db,room,bot)
        assert state['binding']['context_after_seq']==0, state['binding']['context_after_seq']
    async def resolve(*args,**kwargs):return cfg
    monkeypatch.setattr(context,'resolve_connection_async',resolve)
    prompt='@Ada 현재 요청 원문'
    response=client.post(f'/api/v1/conversations/{room}/context-preview',headers=auth,json={'content':prompt})
    assert response.status_code==200,response.text
    preview=response.json()['agents'][0];assert preview['projection_status']=='ready'
    captured=[]
    async def provider(request):
        from app.services.providers import ToolCall
        captured.append(request)
        if request.continuation is None:
            call={'type':'function_call','call_id':'scheduler-call','name':'list_bots','arguments':'{}'}
            padding={'type':'message','content':'가'*((MAX_PROVIDER_CONTEXT_CHARS-provider_text_size(request))//6+50)}
            state={'provider_kind':'chatgpt',**continuation_scope(request),
                   'input':[*attributed_messages(request),padding,call],'call_ids':['scheduler-call']}
            return ProviderResult(tool_calls=[ToolCall('scheduler-call','list_bots',{})],continuation=state)
        return ProviderResult(text='synthetic')
    client.app.state.scheduler.provider=provider
    result=client.post(f'/api/v1/conversations/{room}/messages',headers=auth,json={'content':prompt})
    assert result.status_code==201,result.text
    assert len(captured)==2
    request=captured[0];actual=provider_text_size(request)
    second=captured[1];assert provider_text_size(second)<=MAX_PROVIDER_CONTEXT_CHARS
    assert second.observations[0].call_id=='scheduler-call'
    assert freeze_history_prefix(request)['removable'], [(e['kind'],e['seq']) for e in request.events]
    assert len(second.continuation['input']) < len(attributed_messages(request))+2
    assert '현재 요청 원문' in str(second.continuation['input'])
    assert actual<=MAX_PROVIDER_CONTEXT_CHARS and preview['budget']['used_chars']<=MAX_PROVIDER_CONTEXT_CHARS
    assert '현재 요청 원문' in str(attributed_messages(request)) and '현재 요청 원문' in str(preview['messages'])
    assert preview['budget']['used_chars']>=actual
