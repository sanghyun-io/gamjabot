"""Owner-private conversational persistence on disposable synthetic state."""
import json
import pytest
from test_room_state import room_client, headers
from test_bot_creation import private_team, seed_permit, tool
from app.models.models import Agent
from app.models.collaboration import RoomEvent
from app.services import bot_persistence
from app.services.memory_context import snapshot
from app.services.llm_service import LLMFailure
from app.services.providers import ProviderResult


def args(operation, **values):
    result=dict(operation=operation,memory_id=None,expected_scope_revision=None,
                expected_revision=None,category=None,content=None)
    result.update(values)
    return result


def test_chat_memory_and_role_roundtrip(room_client):
    client,(owner,other,project,_,_)=room_client
    agent,room,_=private_team(client,owner,project); receipts=[]; systems=[]
    async def provider(request):
        systems.append(request.system)
        if not request.observations:
            assert {'save_bot_memory','edit_bot_memory','forget_bot_memory','change_bot_role'} <= {x['name'] for x in request.tools}
            return tool('save_bot_memory',{'category':'preference','content':'SYNTHETIC_GREEN'},'add')
        result=json.loads(request.observations[-1].text); receipts.append(result)
        if len(receipts)==1:
            return tool('edit_bot_memory',{'memory_id':result['memory_id'],'content':'SYNTHETIC_BLUE'},'edit')
        if len(receipts)==2:
            from app.services.bot_memory import listing
            with client.app.state.session_factory() as db:
                assert listing(db,owner,agent)['items'][0]['category']=='preference'
            return tool('forget_bot_memory',{'memory_id':result['memory_id']},'delete')
        if len(receipts)==3:
            return tool('change_bot_role',{'content':'SYNTHETIC_NEW_ROLE'},'role')
        return ProviderResult(text='Saved the requested changes.')
    client.app.state.scheduler.provider=provider
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=headers(client,owner),
        json={'content':'Remember my preference, correct it, forget it and change your lasting role.'})
    assert response.status_code==201,response.text
    assert [x['operation'] for x in receipts]==['add','edit','delete','role']
    assert all(x['state']=='saved' for x in receipts)
    assert len(set(systems))==1 and 'SYNTHETIC_NEW_ROLE' not in systems[-1]
    with client.app.state.session_factory() as db:
        assert db.get(Agent,agent).system_prompt=='SYNTHETIC_NEW_ROLE'
        assert snapshot(db,room,agent)['bot_private']['entries']==[]
        for receipt in receipts:
            event=db.get(RoomEvent,receipt['result_ref']['event_id'])
            assert event.payload['bot_persistence']['receipt_id']==receipt['receipt_id']
            assert receipt['root_ref']['actor_type']=='user' and receipt['root_ref']['actor_id']==owner
    inspector=client.get(f"/api/v1/conversations/{room}/requests/{receipts[0]['root_request_event_id']}",headers=headers(client,owner)).json()
    assert inspector['completion']['outcome_status']=='succeeded',inspector['completion']
    for receipt in receipts:
        path=f"/api/v1/bots/{agent}/persistence/{receipt['receipt_id']}"
        assert client.get(path,headers=headers(client,owner)).json()['result_ref']==receipt['result_ref']
        assert client.get(path,headers=headers(client,other)).status_code==404


def test_replay_conflict_and_changed_memory_fail_closed(room_client):
    client,(owner,_,project,_,_)=room_client
    agent,room,_=private_team(client,owner,project)
    permit=seed_permit(client,owner,room,agent)
    value=args('add',expected_scope_revision=0,category='fact',content='SYNTHETIC_FACT')
    with client.app.state.session_factory() as db:
        digest=snapshot(db,room,agent)['digest']
        first=bot_persistence.apply(db,permit,'call',value,expected_memory_digest=digest)
        replay=bot_persistence.apply(db,permit,'call',value,expected_memory_digest=digest)
        assert replay['replayed'] and replay['receipt_id']==first['receipt_id']
        with pytest.raises(LLMFailure):
            bot_persistence.apply(db,permit,'call',dict(value,content='changed'),expected_memory_digest=digest)
        with pytest.raises(LLMFailure):
            bot_persistence.apply(db,permit,'another',value,expected_memory_digest=digest)


def test_stopped_generation_and_changed_profile_cannot_write(room_client):
    client,(owner,_,project,_,_)=room_client
    agent,room,_=private_team(client,owner,project)
    permit=seed_permit(client,owner,room,agent)
    from app.models.collaboration import RoomAgentRun
    value=args('role',content='MUST_NOT_APPLY')
    with client.app.state.session_factory() as db:
        digest=snapshot(db,room,agent)['digest']
        run=db.get(RoomAgentRun,(room,agent));run.state='stopped';db.commit()
        with pytest.raises(LLMFailure):
            bot_persistence.apply(db,permit,'stopped',value,expected_memory_digest=digest)
        run=db.get(RoomAgentRun,(room,agent));run.state='running'
        db.get(Agent,agent).system_prompt='EXTERNAL_ROLE';db.commit()
        with pytest.raises(LLMFailure):
            bot_persistence.apply(db,permit,'changed',value,expected_memory_digest=digest)
        assert db.get(Agent,agent).system_prompt=='EXTERNAL_ROLE'


def test_root_owner_only_not_peer_or_other_room(room_client):
    client,(owner,_,project,_,_)=room_client
    agent,room,group=private_team(client,owner,project)
    permit=seed_permit(client,owner,room,agent)
    from app.models.collaboration import RoomEvent
    with client.app.state.session_factory() as db:
        digest=snapshot(db,room,agent)['digest']
        root=db.get(RoomEvent,permit.root_id);root.actor_type='agent';root.actor_id=agent;db.commit()
        with pytest.raises(LLMFailure):
            bot_persistence.apply(db,permit,'peer',args('role',content='NO'),expected_memory_digest=digest)
        assert db.get(Agent,agent).system_prompt!='NO'


def test_original_user_root_can_save_after_batch_return(room_client):
    client,(owner,_,project,_,_)=room_client
    agent,room,_=private_team(client,owner,project); receipts=[]
    with client.app.state.session_factory() as db:
        connection=db.get(Agent,agent).connection_id
    peers=[]
    for name in ('One','Two'):
        response=client.post('/api/v1/agents/',headers=headers(client,owner),json={
            'name':name,'description':'Synthetic helper','system_prompt':'Synthetic role',
            'connection_id':connection,'model_id':'exact'})
        assert response.status_code==201,response.text
        peers.append(response.json()['id'])
    async def provider(request):
        if request.agent_id!=agent:
            return ProviderResult(text='SYNTHETIC_PEER_REPLY')
        if request.observations:
            result=json.loads(request.observations[-1].text)
            if 'receipt_id' in result:
                receipts.append(result)
                return ProviderResult(text='Saved after the replies.')
        body=request.system+json.dumps(request.events)
        if 'SYNTHETIC_PEER_REPLY' in body:
            assert 'change_bot_role' in {t['name'] for t in request.tools}
            return tool('change_bot_role',{'content':'NEW_SYNTHETIC_BATCH_ROLE'},'after-batch')
        return tool('message_bots',{'tasks':[{'agent_id':peers[0],'content':'Review'},
            {'agent_id':peers[1],'content':'Review'}]},'batch')
    client.app.state.scheduler.provider=provider
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=headers(client,owner),
        json={'content':'Ask two helpers to review, then change your role to NEW_SYNTHETIC_BATCH_ROLE.'})
    assert response.status_code==201,response.text
    assert len(receipts)==1 and receipts[0]['operation']=='role'
    with client.app.state.session_factory() as db:
        assert db.get(Agent,agent).system_prompt=='NEW_SYNTHETIC_BATCH_ROLE'


def test_malformed_direct_tool_payload_fails_without_receipt(room_client):
    client,(owner,_,project,_,_)=room_client
    agent,room,_=private_team(client,owner,project)
    permit=seed_permit(client,owner,room,agent)
    with client.app.state.session_factory() as db:
        digest=snapshot(db,room,agent)['digest']
        for invalid in (None, {}, dict(args('role',content='x'), unexpected=True),
                        args('add',expected_scope_revision=True,category='fact',content='x')):
            with pytest.raises(LLMFailure):
                bot_persistence.apply(db,permit,'malformed',invalid,expected_memory_digest=digest)
        from app.models.bot_memory import BotMemoryCommand
        assert db.query(BotMemoryCommand).filter_by(owner_id=owner,agent_id=agent).count()==0
    assert client.get(f'/api/v1/bots/{agent}/persistence/not-a-receipt',headers=headers(client,owner)).status_code==404


def test_null_or_omitted_revisions_bind_snapshot_and_stale_write_is_rejected(room_client):
    client,(owner,_,project,_,_)=room_client
    agent,room,_=private_team(client,owner,project)
    permit=seed_permit(client,owner,room,agent)
    with client.app.state.session_factory() as db:
        old=snapshot(db,room,agent)['digest']
        first=bot_persistence.apply(db,permit,'null-revision',
            {'operation':'add','category':'fact','content':'FIRST','expected_scope_revision':None},
            expected_memory_digest=old)
        assert first['revision']==1
        with pytest.raises(LLMFailure) as error:
            bot_persistence.apply(db,permit,'stale-omitted',
                {'operation':'add','category':'fact','content':'MUST_NOT_SAVE'},expected_memory_digest=old)
        assert error.value.code=='bot_persistence_changed'
        from app.models.bot_memory import BotMemoryEntry
        assert db.query(BotMemoryEntry).filter_by(owner_id=owner,agent_id=agent,deleted_at=None).count()==1


def test_prior_receipt_after_stop_is_read_only_and_new_call_denied(room_client):
    client,(owner,_,project,_,_)=room_client
    agent,room,_=private_team(client,owner,project)
    permit=seed_permit(client,owner,room,agent)
    from app.models.collaboration import RoomAgentRun
    from app.models.bot_memory import BotMemoryEntry
    with client.app.state.session_factory() as db:
        digest=snapshot(db,room,agent)['digest']
        value={'operation':'add','content':'FIRST'}
        first=bot_persistence.apply(db,permit,'same-call',value,expected_memory_digest=digest)
        run=db.get(RoomAgentRun,(room,agent));run.state='stopped';db.commit()
        replay=bot_persistence.apply(db,permit,'same-call',value,expected_memory_digest=digest)
        assert replay['replayed'] and replay['receipt_id']==first['receipt_id']
        with pytest.raises(LLMFailure):
            bot_persistence.apply(db,permit,'new-call',value,expected_memory_digest=digest)
        assert db.query(BotMemoryEntry).filter_by(owner_id=owner,agent_id=agent,deleted_at=None).count()==1


def test_invalid_provider_call_persists_safe_schema_diagnostic_without_receipt(room_client):
    from app.services.providers.base import normalized_call, wire_tool_name
    from app.models.bot_memory import BotMemoryCommand
    client,(owner,_,project,_,_)=room_client
    agent,room,_=private_team(client,owner,project)
    async def provider(request):
        normalized_call('bad',wire_tool_name('save_bot_memory'),
            {'content':'SENSITIVE_VALUE','SENSITIVE_KEY':'SENSITIVE_VALUE'},request.tools)
        raise AssertionError('invalid call should not pass')
    client.app.state.scheduler.provider=provider
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=headers(client,owner),json={'content':'Synthetic request'})
    assert response.status_code==502
    with client.app.state.session_factory() as db:
        events=db.query(RoomEvent).filter_by(room_id=room,kind='failure').all()
        assert len(events)==1
        diagnostic=events[0].payload['diagnostic']
        assert diagnostic['tool_schema']=={'validator':'additionalProperties','field':None}
        assert 'SENSITIVE_VALUE' not in json.dumps(events[0].payload)
        assert 'SENSITIVE_KEY' not in json.dumps(events[0].payload)
        assert db.query(BotMemoryCommand).filter_by(owner_id=owner,agent_id=agent).count()==0
