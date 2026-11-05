"""Four focused, provider-free privacy checks on disposable synthetic SQLite."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.models import Agent, Conversation, Project, User
from app.models.bot_memory import BotMemoryEntry, BotMemoryState
from app.models.bot_message import BotMessage
from app.models.bot_profile import BotConversation
from app.models.collaboration import RoomAgentRun, RoomEvent, RoomMembership, RoomTrigger
from app.services.context import build_turn_context
from app.services.context_projection import project_finalize_context
from app.services.execution_origin import load_execution_origin
from app.services.llm_service import LLMFailure
from app.services.memory_context import snapshot
from app.services.providers.base import attributed_messages


@pytest.fixture
def isolated(monkeypatch):
    # No application startup, migration, credential store, live DB or provider.
    engine=create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    now=datetime.now(timezone.utc)
    with Session(engine) as db:
        db.add(User(id=1,email='synthetic@example.invalid',username='Owner',hashed_password='unused',is_active=True))
        db.add(Project(id=1,user_id=1,name='Synthetic',is_active=True))
        for n in (1,2,3):
            db.add(Agent(id=n,user_id=1,name=f'Bot{n}',description='synthetic',
                system_prompt='Answer the task.',connection_id=1,model_id='synthetic-model',is_active=True))
            db.add(Conversation(id=n,user_id=1,project_id=1,title=f'Private{n}',is_active=True))
            db.add(RoomMembership(room_id=n,agent_id=n,is_active=True))
            db.add(RoomAgentRun(room_id=n,agent_id=n,state='running',generation=1,
                memory={'secret':f'LEGACY_SECRET_{n}'},checkpoint={}))
            db.add(BotConversation(owner_id=1,agent_id=n,room_id=n,created_at=now,updated_at=now))
            db.add(BotMemoryState(owner_id=1,agent_id=n,revision=1))
            db.add(BotMemoryEntry(memory_id=str(uuid4()),owner_id=1,agent_id=n,category='fact',
                content=f'BOT_PRIVATE_SECRET_{n}',source_kind='manual',source_snapshot={},revision=1,
                created_by=1,updated_by=1,created_at=now,updated_at=now))
        db.commit()
        def event(room,actor,aid,content,kind='message'):
            seq=db.query(RoomEvent).filter_by(room_id=room).count()+1
            item=RoomEvent(event_id=str(uuid4()),room_id=room,seq=seq,actor_type=actor,
                actor_id=aid,actor_name_snapshot='Synthetic',kind=kind,payload={'content':content},created_at=now)
            db.add(item);db.commit();return item
        root=event(1,'user',1,'ROOT_USER_PRIVATE_BODY')
        db.add(RoomTrigger(event_id=root.event_id,room_id=1,state='running',selected_agents=[1]))
        for n in (1,2,3):
            db.get(RoomAgentRun,(n,n)).trigger_event_id=root.event_id
            event(n,'user',1,f'PERSONAL_TRANSCRIPT_SECRET_{n}')
        request=event(1,'agent',1,'TASK_FOR_B_ONLY')
        delivery=event(2,'system',None,'delivery','activity')
        job=BotMessage(message_id=str(uuid4()),owner_id=1,root_room_id=1,root_request_event_id=root.event_id,
            sender_room_id=1,recipient_room_id=2,sender_agent_id=1,recipient_agent_id=2,
            sender_name='Bot1',recipient_name='Bot2',content='TASK_FOR_B_ONLY',delivery_key='synthetic1',
            depth=1,state='running',reply_state='pending',request_event_id=request.event_id,
            delivery_event_id=delivery.event_id,deadline_at=now+timedelta(seconds=300),created_at=now,updated_at=now)
        db.add(job);db.commit()
        calls=[]
        async def connection(*args,**kwargs):calls.append('resolved');return object()
        monkeypatch.setattr('app.services.context.resolve_connection_async',connection)
        monkeypatch.setattr('app.services.model_features.permitted',lambda *a:True)
        yield db,job,root,event,calls
    engine.dispose()


def run_context(db,origin):
    watermark=db.query(RoomEvent).filter_by(room_id=origin.execution_room_id).count()
    return asyncio.run(build_turn_context(db,origin.execution_room_id,origin.agent_id,watermark,
        trigger_event_id=origin.root_request_event_id,execution_origin=origin))


def text(request):
    return json.dumps(attributed_messages(request),ensure_ascii=False)


def test_delegated_response_excludes_personal_history_memory_and_root_body(isolated):
    db,job,root,_,_=isolated
    # The same Bot's genuine personal snapshot still works, but is never shared.
    normal=snapshot(db,2,2)
    assert 'BOT_PRIVATE_SECRET_2' in str(normal['memory']) and 'LEGACY_SECRET_2' in str(normal['memory'])
    origin=load_execution_origin(db,job_id=job.message_id,phase='response',execution_room_id=2,agent_id=2,generation=1)
    request=run_context(db,origin)
    result=text(request)
    assert 'TASK_FOR_B_ONLY' in result and job.request_event_id in result and root.event_id in result
    assert all(secret not in result for secret in ('ROOT_USER_PRIVATE_BODY','PERSONAL_TRANSCRIPT_SECRET',
                                                   'BOT_PRIVATE_SECRET','LEGACY_SECRET'))
    assert request.memory is None and len(request.events)==1 and request.events[0]['actor_type']=='system'
    assert snapshot(db,2,2,execution_origin=origin)['bot_private']['eligible'] is False


def test_direct_return_keeps_source_context_but_nested_return_and_failure_stay_isolated(isolated):
    db,job,root,event,_=isolated
    result=event(2,'agent',2,'ACTUAL_B_REPLY')
    job.result_event_id=result.event_id;job.state='completed';job.reply_state='running';db.commit()
    origin=load_execution_origin(db,job_id=job.message_id,phase='return',execution_room_id=1,agent_id=1,generation=1)
    request=run_context(db,origin)
    assert 'BOT_PRIVATE_SECRET_1' in text(request) and 'ACTUAL_B_REPLY' in text(request)
    assert 'BOT_PRIVATE_SECRET_2' not in text(request) and result.event_id in text(request)
    outgoing=event(2,'agent',2,'TASK_FOR_C_ONLY')
    delivery=event(3,'system',None,'delivery','activity')
    answer=event(3,'agent',3,'ACTUAL_C_REPLY')
    child=BotMessage(message_id=str(uuid4()),owner_id=1,root_room_id=1,root_request_event_id=root.event_id,
        sender_room_id=2,recipient_room_id=3,sender_agent_id=2,recipient_agent_id=3,
        sender_name='Bot2',recipient_name='Bot3',content='TASK_FOR_C_ONLY',delivery_key='synthetic2',
        parent_job_id=job.message_id,parent_kind='bot_message',depth=2,state='completed',reply_state='running',
        request_event_id=outgoing.event_id,delivery_event_id=delivery.event_id,result_event_id=answer.event_id,
        deadline_at=job.deadline_at,created_at=job.created_at,updated_at=job.updated_at)
    db.add(child);db.commit()
    nested=load_execution_origin(db,job_id=child.message_id,phase='return',execution_room_id=2,agent_id=2,generation=1)
    result_text=text(run_context(db,nested))
    assert nested.context_mode=='delegated' and 'TASK_FOR_B_ONLY' in result_text and 'ACTUAL_C_REPLY' in result_text
    assert job.request_event_id in result_text and answer.event_id in result_text
    assert all(secret not in result_text for secret in ('ROOT_USER_PRIVATE_BODY','PERSONAL_TRANSCRIPT_SECRET',
                                                        'BOT_PRIVATE_SECRET','LEGACY_SECRET'))
    # Exact foreign outcomes remain visible even when their room seq exceeds
    # this room's finalize watermark; omitted characters are explicit.
    keys=('event_id','room_id','seq','actor_type','actor_id','actor_name_snapshot','kind','payload')
    source={key:getattr(root,key) for key in keys}
    foreign={key:getattr(answer,key) for key in keys}
    foreign.update(seq=99999,payload={'content':'X'*4501})
    active={**source,'event_id':str(uuid4()),'seq':99,'payload':{'content':'Finalize the exact result.'}}
    final=project_finalize_context(source_request=source,outcomes=[foreign],active_event=active,agent_id=1,system='')
    quote=next(item for item in final.messages if 'finalize-foreign:' in item['content'])['content']
    assert answer.event_id in quote and '99999' in quote and '"partial": true' in quote
    assert '"original_chars": 4501' in quote and '"included_chars": 4000' in quote and '"omitted_chars": 501' in quote
    child.result_event_id=None;child.state='failed';child.failure_code='target_busy_timeout';db.commit()
    failed=load_execution_origin(db,job_id=child.message_id,phase='return',execution_room_id=2,agent_id=2,generation=1)
    failed_text=text(run_context(db,failed))
    assert 'target_busy_timeout' in failed_text and 'peer_response_received' in failed_text
    assert 'ACTUAL_C_REPLY' not in failed_text and failed.peer_result_ref is None


def test_forged_mode_owner_and_generation_never_resolve_credentials(isolated):
    db,job,_,_,calls=isolated
    origin=load_execution_origin(db,job_id=job.message_id,phase='response',execution_room_id=2,agent_id=2,generation=1)
    for forged in (replace(origin,context_mode='user_room'),replace(origin,owner_id=2),replace(origin,generation=9)):
        with pytest.raises(LLMFailure):run_context(db,forged)
    assert calls==[]
    job.owner_id=2;db.commit()
    with pytest.raises(LLMFailure):
        load_execution_origin(db,job_id=job.message_id,phase='response',execution_room_id=2,agent_id=2,generation=1)


def test_task_changed_during_connection_await_rejects_stale_context(isolated,monkeypatch):
    db,job,_,_,calls=isolated
    origin=load_execution_origin(db,job_id=job.message_id,phase='response',execution_room_id=2,agent_id=2,generation=1)
    async def changed(*args,**kwargs):
        calls.append('resolved');job.content='CHANGED_AFTER_AWAIT';db.commit();return object()
    monkeypatch.setattr('app.services.context.resolve_connection_async',changed)
    with pytest.raises(LLMFailure) as error:run_context(db,origin)
    assert error.value.code=='execution_context_changed' and calls==['resolved']
