"""Actual disposable queue/transaction contracts; all model answers and OS drivers are fake."""
import json
from datetime import timedelta
from dataclasses import replace
from uuid import uuid4
import pytest
from test_room_state import room_client,headers,create_room
from test_teammates import team
from app.models.bot_creation import BotCreation
from app.models.bot_profile import BotOwnerProfile,BotConversation
from app.models.bot_message import BotMessage
from app.models.models import Agent,User,Conversation
from app.models.collaboration import RoomEvent,RoomTrigger,RoomAgentRun,RoomMembership,RoomGrant,ProviderConnection
from app.models.teammate import TeammateBudget
from app.services import bot_creation,execution_scopes as scopes
from app.services.bot_configuration_binding import fingerprint
from app.services.action_review import ProposerBinding
from app.services.events import ActorRef,append_event
from app.services.providers import ProviderResult,ToolCall
from app.services.llm_service import LLMFailure

ARGS={'name':'Researcher','description':'Reviews the supplied task','role':'SPECIALIST_ROLE_PERSISTENT_36'}


def tool(name,args,call='create-one'):
    return ProviderResult(tool_calls=[ToolCall(call,name,args)],continuation={'call_ids':[call]})


def private_team(client,owner,project):
    group=create_room(client,owner,project,'Legacy group')
    ada,=team(client,owner,group,('Ada',))
    response=client.post(f'/api/v1/bots/{ada}/conversation',headers=headers(client,owner),json={})
    assert response.status_code==200,response.text
    return ada,response.json()['room_id'],group


def creations(client,owner,room):
    response=client.get(f'/api/v1/bots/conversations/{room}/creations',headers=headers(client,owner))
    assert response.status_code==200,response.text
    return response.json()['creations']


def seed_permit(client,owner,room,ada):
    with client.app.state.session_factory() as db:
        event=append_event(db,room,ActorRef('user',owner),'message',{'content':'Create a specialist'})
        root_id=event.event_id
        client.app.state.scheduler._create_trigger(db,event)
        trigger=db.get(RoomTrigger,root_id);trigger.state='running';trigger.current_round=1
        run=db.get(RoomAgentRun,(room,ada));run.state='running';run.trigger_event_id=root_id;run.generation+=1
        agent=db.get(Agent,ada);conn=db.get(ProviderConnection,agent.connection_id)
        proposer=ProposerBinding(owner_id=owner,room_id=room,agent_id=ada,connection_id=conn.id,model_id=agent.model_id,
            config_revision=conn.readiness_revision,auth_grant_revision=conn.auth_grant_revision,
            provider_id=conn.provider_id,protocol_id=conn.protocol_id)
        db.commit()
        return bot_creation.CreationPermit(owner,room,root_id,ada,run.generation,fingerprint(agent),proposer)


def test_create_actual_bot_then_delegate_and_receive_its_actual_answer(room_client):
    client,(owner,other,project,_,_)=room_client
    ada,room,group=private_team(client,owner,project);source=[];recipient=[];created=[]
    with client.app.state.session_factory() as db:
        connection_id=db.get(Agent,ada).connection_id
        append_event(db,room,ActorRef('user',owner),'message',{'content':'SOURCE_PRIVATE_HISTORY_NEVER_COPY_36'})
        db.commit()
    baseline=client.post('/api/v1/agents/',headers=headers(client,owner),json={'name':'Baseline',
        'description':'Owner-created existing helper','system_prompt':'BASELINE_ROLE_36','connection_id':connection_id,'model_id':'exact'})
    assert baseline.status_code==201,baseline.text
    baseline_id=baseline.json()['id']
    async def provider(request):
        body=request.system+json.dumps(request.events,ensure_ascii=False)
        if request.agent_id!=ada:
            recipient.append(request)
            if request.agent_id==baseline_id:
                assert 'BASELINE_ROLE_36' in request.system
                answer='BASELINE_ACTUAL_ANSWER_36'
            else:
                receipt=next(r for r in created if r['created_agent']['id']==request.agent_id)
                assert receipt['created_agent']['system_prompt'] in request.system
                answer='SPECIALIST_ACTUAL_ANSWER_36' if receipt is created[0] else 'SECOND_ACTUAL_ANSWER_36'
            assert 'SOURCE_PRIVATE_HISTORY_NEVER_COPY_36' not in body and request.memory is None
            assert 'create_bot' not in {s['name'] for s in request.tools}
            assert request.model_id=='exact'
            return ProviderResult(text=answer)
        source.append(request)
        if request.observations:
            value=json.loads(request.observations[-1].text)
            if 'creation_id' in value:
                created.append(value)
                assert value['readback']['available'] is True
                with client.app.state.session_factory() as db:
                    assert db.query(RoomGrant).filter_by(agent_id=value['created_agent']['id']).count()==0
                    assert db.get(RoomMembership,(room,value['created_agent']['id'])) is None
                    assert db.get(BotOwnerProfile,owner).primary_agent_id is None
                if len(created)==1:
                    assert not recipient
                    return tool('message_bots',{'tasks':[{'agent_id':value['created_agent']['id'],'content':'Review only this supplied task'},
                        {'agent_id':baseline_id,'content':'Independent check of supplied task'}]},'delegate-created-batch')
                return tool('message_bot',{'agent_id':value['created_agent']['id'],'content':'Apply your second role to this supplied task'},'delegate-second-created')
            return ProviderResult(text='Waiting for actual replies.')
        if 'SECOND_ACTUAL_ANSWER_36' in body:
            assert 'create_bot' in {s['name'] for s in request.tools}
            return ProviderResult(text='A_FINAL_WITH_SECOND_ACTUAL_ANSWER_36')
        if 'SPECIALIST_ACTUAL_ANSWER_36' in body and 'BASELINE_ACTUAL_ANSWER_36' in body:
            # The same real Owner root still authorizes a new creation after
            # its exact user_room batch return; peers grant no authority.
            assert len(created)==1 and 'create_bot' in {s['name'] for s in request.tools}
            return tool('create_bot',dict(ARGS,name='Second specialist',role='SECOND_PERSISTENT_ROLE_36'),'create-after-join')
        assert 'create_bot' in {s['name'] for s in request.tools}
        return tool('create_bot',ARGS)
    client.app.state.scheduler.provider=provider
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=headers(client,owner),
        json={'content':'Create a focused Bot, ask it and Baseline to review this task, then create a second specialist and get its answer.'})
    assert response.status_code==201,response.text
    rows=creations(client,owner,room)
    assert len(rows)==len(created)==2 and len(recipient)==3
    receipt=next(r for r in rows if r['created_agent']['id']==created[0]['created_agent']['id'])
    new_id=receipt['created_agent']['id'];root=receipt['root_ref']['event_id']
    assert receipt['created_agent']['system_prompt']==ARGS['role'] and receipt['created_agent']['temperature']=='auto'
    assert receipt['root_ref']['actor_type']=='user' and receipt['root_ref']['actor_id']==owner
    assert receipt['request_ref']['actor_type']=='agent' and receipt['result_ref']['actor_type']=='system'
    assert client.get(f"/api/v1/bots/creations/{receipt['creation_id']}",headers=headers(client,other)).status_code==404
    inspector_path=f'/api/v1/conversations/{room}/requests/{root}'
    inspector=client.get(inspector_path,headers=headers(client,owner)).json()
    assert inspector['completion']=={'state':'completed','outcome_status':'succeeded','result_available':True,
        'completed_rounds':1,'pending_agent_count':0},inspector['completion']
    assert any(o['agent_id']==ada and o['kind']=='message' for o in inspector['outcomes'])
    with client.app.state.session_factory() as db:
        assert db.query(BotCreation).count()==2
        jobs=db.query(BotMessage).filter_by(root_request_event_id=root).all()
        assert len(jobs)==3 and all(j.reply_state=='delivered' for j in jobs)
        last=next(j for j in jobs if j.recipient_agent_id==created[1]['created_agent']['id'])
        assert db.get(RoomEvent,last.return_event_id).payload['content']=='A_FINAL_WITH_SECOND_ACTUAL_ANSWER_36'
        assert db.get(BotConversation,(owner,new_id)).room_id==receipt['conversation']['room_id']
        assert db.get(TeammateBudget,root).provider_calls==len(source)+len(recipient)<=32
        assert db.query(RoomEvent).filter_by(room_id=receipt['conversation']['room_id'],actor_type='user').count()==0
        agent=db.get(Agent,new_id);agent.system_prompt='Later explicit Owner change';db.commit()
    stale=next(r for r in creations(client,owner,room) if r['created_agent']['id']==new_id)
    assert stale['readback']=={'agent_matches':False,'conversation_matches':True,'available':False}
    # Only exact receipt-backed creation provenance is nonterminal. A changed
    # tag or an unbacked lookalike must still invalidate the raw-outcome audit.
    with client.app.state.session_factory() as db:
        request_event=db.get(RoomEvent,receipt['request_ref']['event_id'])
        original_payload=dict(request_event.payload)
        request_event.payload=dict(original_payload,bot_creation=dict(original_payload['bot_creation'],phase='forged'))
        db.commit()
    assert client.get(inspector_path,headers=headers(client,owner)).json()['completion']['outcome_status']=='unknown'
    with client.app.state.session_factory() as db:
        db.get(RoomEvent,receipt['request_ref']['event_id']).payload=original_payload
        forged=dict(original_payload,bot_creation=dict(original_payload['bot_creation'],creation_id=str(uuid4())))
        append_event(db,room,ActorRef('agent',ada),'activity',forged)
        db.commit()
    assert client.get(inspector_path,headers=headers(client,owner)).json()['completion']['outcome_status']=='unknown'



def test_atomic_replay_payload_binding_cap_and_no_implicit_run(room_client):
    client,(owner,_,project,_,_)=room_client
    ada,room,_=private_team(client,owner,project);permit=seed_permit(client,owner,room,ada)
    def create(call,args=ARGS):
        with client.app.state.session_factory() as db:
            return bot_creation.create(db,permit,call,args,provider_calls=1)
    first=create('same-call');again=create('same-call')
    assert first['creation_id']==again['creation_id'] and again['replayed']
    with pytest.raises(LLMFailure) as changed:create('same-call',dict(ARGS,name='Different'))
    assert changed.value.code=='bot_create_delivery_changed'
    second=create('second-call',dict(ARGS,name='Second'))
    with pytest.raises(LLMFailure) as capped:create('third-call')
    assert capped.value.code=='bot_create_budget_exhausted'
    with client.app.state.session_factory() as db:
        assert db.query(BotCreation).count()==2
        assert db.query(BotMessage).count()==0
        for receipt in (first,second):
            run=db.get(RoomAgentRun,(receipt['conversation']['room_id'],receipt['created_agent']['id']))
            assert run.state=='idle' and run.trigger_event_id is None
            assert db.query(RoomEvent).filter_by(room_id=receipt['conversation']['room_id']).count()==0


def test_owner_profile_generation_deadline_and_delegated_origin_reject_before_effect(room_client):
    client,(owner,other,project,_,_)=room_client
    ada,room,group=private_team(client,owner,project);permit=seed_permit(client,owner,room,ada)
    default_permit=object()
    def attempt(p=default_permit,**kwargs):
        if p is default_permit:p=permit
        with client.app.state.session_factory() as db:
            return bot_creation.create(db,p,'guarded',ARGS,provider_calls=kwargs.pop('provider_calls',1),**kwargs)
    with pytest.raises(LLMFailure):attempt(replace(permit,owner_id=other))
    with pytest.raises(LLMFailure):attempt(replace(permit,generation=permit.generation+1))
    with pytest.raises(LLMFailure):attempt(None)
    with client.app.state.session_factory() as db:
        run=db.get(RoomAgentRun,(room,ada));run.pending_task={'execution_job':{'message_id':'00000000-0000-0000-0000-000000000000','phase':'response'}};db.commit()
    with pytest.raises(LLMFailure):attempt()
    with client.app.state.session_factory() as db:
        db.get(RoomAgentRun,(room,ada)).pending_task={};db.commit()
    with pytest.raises(LLMFailure):attempt(replace(permit,room_id=group))
    with pytest.raises(LLMFailure) as auth_changed:attempt(replace(permit,proposer=permit.proposer.model_copy(update={'auth_grant_revision':999})))
    assert auth_changed.value.code=='bot_create_source_changed'
    with pytest.raises(LLMFailure) as budget:attempt(provider_calls=33)
    assert budget.value.code=='teammate_budget_exhausted'
    with client.app.state.session_factory() as db:
        root_scope=scopes.root_scope(db,permit.root_id,create=True)
        proof=scopes.bind(db,room,ada,permit.root_id,permit.generation)
        root_scope.epoch+=1;db.commit()
    with pytest.raises(LLMFailure) as scope_changed:attempt(replace(permit,execution_binding=proof))
    assert scope_changed.value.code=='bot_create_scope_changed' 
    with client.app.state.session_factory() as db:
        source=db.get(Agent,ada);original=source.system_prompt;source.system_prompt='Changed without generation';db.commit()
    with pytest.raises(LLMFailure) as stale:attempt()
    assert stale.value.code=='bot_configuration_changed'
    with client.app.state.session_factory() as db:
        db.get(Agent,ada).system_prompt=original;db.commit()
        permit=replace(permit,source_profile_fingerprint=fingerprint(db.get(Agent,ada)))
        root=db.get(RoomEvent,permit.root_id);root.created_at=scopes.now()-timedelta(seconds=301);db.commit()
    with pytest.raises(LLMFailure):attempt()
    with client.app.state.session_factory() as db:
        db.get(RoomEvent,permit.root_id).created_at=scopes.now()
        db.get(RoomTrigger,permit.root_id).state='cancelled';db.commit()
    with pytest.raises(LLMFailure):attempt()
    with client.app.state.session_factory() as db:
        assert db.query(BotCreation).count()==0
        assert db.query(Agent).filter_by(name=ARGS['name']).count()==0


def test_mixed_create_is_effect_free_and_group_cannot_advertise_or_invoke(room_client):
    client,(owner,_,project,_,_)=room_client
    ada,room,group=private_team(client,owner,project);private_count=0;group_seen=[]
    async def provider(request):
        nonlocal private_count
        names={s['name'] for s in request.tools}
        if request.room_id==group:
            group_seen.append(names);assert 'create_bot' not in names
            return tool('create_bot',ARGS,'forbidden-group')
        private_count+=1
        if private_count==1:
            assert 'create_bot' in names
            return ProviderResult(tool_calls=[ToolCall('mixed-create','create_bot',ARGS),
                ToolCall('mixed-list','list_bots',{})],continuation={'call_ids':['mixed-create','mixed-list']})
        assert all(o.state=='denied' for o in request.observations)
        return ProviderResult(text='No Bot was created from the mixed proposal.')
    client.app.state.scheduler.provider=provider
    assert client.post(f'/api/v1/conversations/{room}/messages',headers=headers(client,owner),json={'content':'Create a Bot'}).status_code==201
    assert client.post(f'/api/v1/conversations/{group}/messages',headers=headers(client,owner),json={'content':'@Ada create a Bot'}).status_code==502
    assert private_count==2 and group_seen and not creations(client,owner,room)
    with client.app.state.session_factory() as db:assert db.query(BotCreation).count()==0
