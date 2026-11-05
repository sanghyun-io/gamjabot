import json,pytest
from test_room_state import room_client
from app.services.context_projection import project_turn_context,ProjectionError,MAX_PROVIDER_CONTEXT_CHARS
from app.services.context import persistence_tools
from app.services import teammates,bot_messages
from app.services.tool_broker import TOOL_SCHEMAS
from app.services.providers.base import projected_text_size
from app.services.scheduler import ExecutorBudget


def event(seq,content,actor='user'):
    return {'event_id':f'id-{seq}','room_id':1,'seq':seq,'actor_type':actor,'actor_id':1,
            'actor_name_snapshot':'Synthetic','kind':'message','payload':{'content':content}}

def schemas():return [*TOOL_SCHEMAS,*teammates.TOOL_SCHEMAS,*bot_messages.TOOL_SCHEMAS,*persistence_tools(True,True,True)]

def test_bounded_checkpoint_and_real_38_tools_preserve_current():
    events=[event(i,'past '+str(i)) for i in range(1,51)]+[event(51,'CURRENT_OWNER_EXACT')]
    tools=schemas();assert len(tools)==39 and any(t['name']=='write_page' for t in tools)
    p=project_turn_context(events=events,extractive_checkpoints=[],watermark_seq=51,active_event_id='id-51',
        agent_id=1,system='Role'*200,memory={'owner':'a'*300},tools=tools,extra_system_for_events=lambda events:'R'*2500)
    assert any(e['event_id']=='id-51' and e['payload']['content']=='CURRENT_OWNER_EXACT' for e in p.events)
    assert p.checkpoint_plan is not None and p.used_chars<=MAX_PROVIDER_CONTEXT_CHARS
    assert projected_text_size(system='Role'*200+'R'*2500,tools=tools,messages=p.messages)==p.used_chars
    assert ExecutorBudget(60).remaining_seconds==60

def test_oversized_role_or_memory_fail_closed():
    kwargs=dict(events=[event(1,'CURRENT_OWNER_EXACT')],extractive_checkpoints=[],watermark_seq=1,active_event_id='id-1',agent_id=1,tools=schemas())
    with pytest.raises(ProjectionError):project_turn_context(**kwargs,system='r'*32000,memory={})
    with pytest.raises(ProjectionError):project_turn_context(**kwargs,system='role',memory={'secret':'x'*9000})

def test_retired_response_is_never_reintroduced():
    events=[event(1,'retired value','agent'),event(2,'fresh owner')]
    binding={'context_after_seq':0,'retired_persistence_responses':[{'event_id':'id-1','seq':1}]}
    p=project_turn_context(events=events,extractive_checkpoints=[],watermark_seq=2,active_event_id='id-2',agent_id=1,system='role',memory={},tools=[],memory_binding=binding)
    assert 'retired value' not in json.dumps(p.messages) and 'fresh owner' in json.dumps(p.messages)

@pytest.mark.asyncio
async def test_executor_budget_elapsed_and_cancellation():
    import asyncio
    budget=ExecutorBudget(2)
    async def completed():
        await asyncio.sleep(0.01)
        return 'done'
    assert await budget.run(completed())=='done'
    assert 0 < budget.consumed_seconds < 2 and 0 < budget.remaining_seconds < 2
    async def pending():
        await asyncio.Event().wait()
    task=asyncio.create_task(budget.run(pending()))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert budget.consumed_seconds>0

def test_checkpoint_can_be_omitted_without_losing_active_request():
    events=[event(i,'history '+('a'*4000)) for i in range(1,25)]+[event(25,'OWNER_CURRENT')]
    p=project_turn_context(events=events,extractive_checkpoints=[],watermark_seq=25,active_event_id='id-25',
        agent_id=1,system='role',memory={},tools=schemas(),extra_system_for_events=lambda e:'r'*10000)
    assert [e['event_id'] for e in p.events]==['id-25']
    assert p.events[0]['payload']['content']=='OWNER_CURRENT' and p.checkpoint_plan is None

def test_preview_and_dispatch_bounded_parity_with_verified_model(room_client):
    from test_room_state import headers
    from test_bot_creation import private_team,tool
    from app.models.models import Agent
    from app.models.collaboration import ProviderConnection
    from app.services.events import append_event,ActorRef
    from app.services.memory_context import snapshot
    from app.services.context import provider_text_size
    from app.services.providers.base import attributed_messages
    from app.services.providers import ProviderResult
    client,(owner,_,project,_,_)=room_client;auth=headers(client,owner)
    bot,room,_=private_team(client,owner,project)
    from test_execution_scopes import FakeExecutor
    client.app.state.scheduler.broker.executor=FakeExecutor()
    client.app.state.scheduler.broker.tool_schemas=TOOL_SCHEMAS
    with client.app.state.session_factory() as db:
        connection=db.get(ProviderConnection,db.get(Agent,bot).connection_id)
        caps=dict(connection.capabilities or {});caps['declared']={'tools':True}
        caps['verified']={'model_id':'exact','base_url':connection.base_url,'tools':True}
        connection.capabilities=caps;db.commit()
    seed=client.post(f'/api/v1/bots/{bot}/memories',headers={**auth,'Idempotency-Key':'parity-seed'},
                     json={'expected_scope_revision':0,'category':'fact','content':'OLD_PARITY_SENTINEL'})
    assert seed.status_code==200,seed.text
    async def transition(request):
        if not request.observations:return tool('edit_bot_memory',{'memory_id':seed.json()['memory_id'],'content':'NEW_PARITY_SENTINEL'},'parity-edit')
        return ProviderResult(text='OLD_PARITY_SENTINEL was corrected')
    client.app.state.scheduler.provider=transition
    first=client.post(f'/api/v1/conversations/{room}/messages',headers=auth,json={'content':'@Ada correct this memory'})
    assert first.status_code==201,first.text
    with client.app.state.session_factory() as db:
        assert snapshot(db,room,bot)['binding'].get('retired_persistence_responses')
        for i in range(25):append_event(db,room,ActorRef('user',owner),'message',{'content':f'prior synthetic {i}'})
    draft='@Ada CURRENT_PARITY_OWNER'
    preview=client.post(f'/api/v1/conversations/{room}/context-preview',headers=auth,json={'content':draft})
    assert preview.status_code==200,preview.text
    result=preview.json()['agents'][0];assert result['projection_status']=='ready'
    captured=[]
    async def provider(request):captured.append(request);return ProviderResult(text='Synthetic parity result')
    client.app.state.scheduler.provider=provider
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=auth,json={'content':draft})
    assert response.status_code==201,response.text
    request,=captured;assert len(request.tools)==39 and any(t['name']=='write_page' for t in request.tools)
    assert request.watermark_seq==preview.json()['projected_seq'] and request.trigger_event_id
    assert 'CURRENT_PARITY_OWNER' in json.dumps(attributed_messages(request))
    assert 'CURRENT_PARITY_OWNER' in json.dumps(result['messages'])
    assert 'OLD_PARITY_SENTINEL' not in json.dumps(attributed_messages(request))
    assert 'OLD_PARITY_SENTINEL' not in json.dumps(result['messages'])
    assert result['messages']==attributed_messages(request)
    actual=provider_text_size(request);estimate=result['budget']['used_chars']
    assert 0<actual<=estimate<=MAX_PROVIDER_CONTEXT_CHARS
