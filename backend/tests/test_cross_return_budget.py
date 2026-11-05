"""The verified return envelope participates in the actual provider budget."""
from types import SimpleNamespace
from uuid import uuid4
from app.services.context import projection_wire_size,MAX_PROVIDER_CONTEXT_CHARS,MEMORY_TOOL_GUIDANCE
from app.services.context_projection import project_turn_context,execution_context_event
from app.services.execution_origin import ExecutionOrigin,EventReference
from app.services.providers.base import projected_messages
from test_continuation_budget import configured,tools,event


def test_go_return_reserves_exact_wire_and_verified_peer_before_history_selection():
    cfg=configured('opencode-go','qwen3.7-plus','https://opencode.ai/zen/go/v1','anthropic_messages')
    row=SimpleNamespace(id=1,kind=cfg.provider_kind,provider_id=cfg.provider_id,protocol_id=cfg.protocol_id,
        base_url=cfg.base_url,capabilities=cfg.capabilities,readiness_revision=1)
    root_id=str(uuid4());root=EventReference(1,root_id,31,'user',1,'message')
    task=EventReference(1,str(uuid4()),30,'agent',1,'message');peer=EventReference(2,str(uuid4()),5,'agent',2,'message')
    content='PEER_RESULT_'+('한글'*500)
    origin=ExecutionOrigin(1,1,root_id,1,1,str(uuid4()),'return',4,'user_room','TASK',task,root,None,peer,content,None,len(content),len(content),0)
    events=[event(i,'과거자료'*8) for i in range(1,31)]+[event(31,'CURRENT_OWNER')];events[-1]['event_id']=root_id
    schemas=tools();size=lambda system,messages:projection_wire_size(row,cfg.model_id,system,messages,schemas)
    kwargs=dict(events=events,extractive_checkpoints=[],watermark_seq=31,active_event_id=root_id,agent_id=1,
        system='ROLE',memory={'fact':'MEMORY'},tools=schemas,extra_system_for_events=lambda _:MEMORY_TOOL_GUIDANCE,size_for_candidate=size)
    base=project_turn_context(**kwargs);with_result=project_turn_context(**kwargs,execution_origin=origin)
    assert with_result.used_chars<=MAX_PROVIDER_CONTEXT_CHARS
    assert len(with_result.events)-1 < len(base.events)
    old_messages=projected_messages(events=[*base.events,execution_context_event(origin,31)],watermark_seq=31,
        agent_id=1,trigger_event_id=root_id,memory=base.memory_text,room_id=1)
    assert size('ROLE'+MEMORY_TOOL_GUIDANCE,old_messages)>MAX_PROVIDER_CONTEXT_CHARS
    rendered=str(with_result.messages)
    assert all(value in rendered for value in ('CURRENT_OWNER','MEMORY','PEER_RESULT_'))
    assert any(e['payload'].get('execution_context') for e in with_result.events)
    assert with_result.used_chars==size('ROLE'+MEMORY_TOOL_GUIDANCE,with_result.messages)
    assert 31 not in with_result.source_coverage['included_event_seqs']
