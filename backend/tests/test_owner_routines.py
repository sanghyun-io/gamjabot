"""Synthetic scheduled work lifecycle and Owner authority."""
import asyncio,json
from datetime import datetime,timedelta,timezone
import pytest
from test_room_state import room_client,headers
from test_bot_creation import private_team
from app.models.owner_routines import OwnerRoutine,OwnerRoutineRun
from app.services import owner_routines as routines
from app.services.providers import ProviderResult
from test_primary_checkins import model_use


def data(enabled=True):return {'name':'Synthetic routine','instruction':'Ask a synthetic question','enabled':enabled,
    'schedule':{'kind':'interval','interval_minutes':5,'timezone':'Asia/Seoul'}}

def test_create_cas_readback_and_dst(room_client):
    client,(owner,other,project,_,_)=room_client;own=headers(client,owner);bot,room,_=private_team(client,owner,project)
    path=f'/api/v1/bots/{bot}/routines';key={**own,'Idempotency-Key':'routine-create'}
    response=client.post(path,headers=key,json=data());assert response.status_code==200,response.text
    record=response.json();row=record['routine'];assert row['availability']=='available' and row['next_run_at']
    assert client.post(path,headers=key,json=data()).json()==record
    assert client.post(path,headers=key,json={**data(),'name':'Changed'}).status_code==409
    assert client.get(path+'/commands/routine-create',headers=own).json()==record
    assert client.get(path,headers=headers(client,other)).status_code==404
    assert client.post(path,headers={**own,'Idempotency-Key':'bad'},json={**data(),'schedule':{'kind':'interval','interval_minutes':4,'timezone':'UTC'}}).status_code==422
    assert client.put(path+'/'+row['routine_id'],headers=own,json={'expected_revision':1,**data(False)}).status_code==200
    assert client.put(path+'/'+row['routine_id'],headers=own,json={'expected_revision':1,**data(False)}).status_code==409
    spring={'kind':'weekly','weekdays':[6],'hour':2,'minute':30,'timezone':'America/New_York'}
    assert routines.next_time(spring,datetime(2026,3,8,6,0))==datetime(2026,3,15,6,30)
    fall={'kind':'weekly','weekdays':[6],'hour':1,'minute':30,'timezone':'America/New_York'}
    assert routines.next_time(fall,datetime(2026,11,1,4,0))==datetime(2026,11,1,5,30)
    assert routines.next_time(fall,datetime(2026,11,1,5,31))==datetime(2026,11,8,6,30)

@pytest.mark.asyncio
async def test_real_claim_origin_result_and_replay_no_duplicate(room_client):
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner);bot,room,_=private_team(client,owner,project)
    create=client.post(f'/api/v1/bots/{bot}/routines',headers={**own,'Idempotency-Key':'create'},json=data(False)).json()
    rid=create['routine']['routine_id'];scheduler=client.app.state.scheduler;scheduler.broker=None;seen=[]
    async def provider(request):seen.append(request);return ProviderResult(text='SYNTHETIC_RESULT',model_use={**model_use(request),'watermark_seq':request.watermark_seq})
    scheduler.provider=provider
    from app.schemas.owner_routines import RoutineTest
    with client.app.state.session_factory() as db:
        result,created=routines.test_claim(db,owner,bot,rid,RoutineTest(expected_revision=1),'test-once')
        replay,again=routines.test_claim(db,owner,bot,rid,RoutineTest(expected_revision=1),'test-once')
        assert created and not again and replay==result
    run_id=result['run']['run_id']
    from app.services.work_state import project_work_states
    with client.app.state.session_factory() as db:
        projected=project_work_states(db,room,owner,[{'agent_id':bot}])[bot]
        assert projected.phase=='thinking' and projected.round==1
    await routines.execute(scheduler,run_id)
    with client.app.state.session_factory() as db:
        row=db.get(OwnerRoutineRun,run_id);assert row.state=='completed' and row.result_event_id
        assert routines.run_dto(db,row)['root_ref']['actor_type']=='system'
        projected=project_work_states(db,room,owner,[{'agent_id':bot}])[bot]
        assert projected.phase=='completed' and projected.round==1
    member=next(x for x in client.get(f'/api/v1/conversations/{room}/members',headers=own).json() if x['agent_id']==bot)
    assert member['work_state']['phase']=='completed' and member['work_state']['round']==1
    assert member['work_state']['request']['event_id']==row.root_event_id and member['work_state']['result']['event_id']==row.result_event_id
    assert len(seen)==1 and seen[0].server_instruction.startswith('SERVER-REGISTERED OWNER ROUTINE')

@pytest.mark.asyncio
async def test_routine_tool_requires_review_and_exact_origin(room_client,tmp_path):
    from app.models.collaboration import ToolAction
    from app.services.providers import ToolCall
    from app.services.grants import decide
    client,(owner,_,project,_,_)=room_client;broker=client.app.state.scheduler.broker
    bot,room,_=private_team(client,owner,project);scheduler=client.app.state.scheduler;scheduler.broker=broker
    target=tmp_path/'fixture';target.write_text('synthetic');calls=[]
    async def executor(action,grant,cancel):calls.append(action.action_id);return 'synthetic result'
    broker.executor=executor
    async def provider(request):
        if not request.observations:return ProviderResult(tool_calls=[ToolCall('call','read_file',{'path':str(target)})],continuation={'call_ids':['call']})
        return ProviderResult(text='REVIEWED_RESULT',model_use={**model_use(request),'watermark_seq':request.watermark_seq})
    scheduler.provider=provider
    record=client.post(f'/api/v1/bots/{bot}/routines',headers={**headers(client,owner),'Idempotency-Key':'create'},json=data(False)).json()
    from app.schemas.owner_routines import RoutineTest
    with client.app.state.session_factory() as db:claim,_=routines.test_claim(db,owner,bot,record['routine']['routine_id'],RoutineTest(expected_revision=1),'test')
    task=asyncio.create_task(routines.execute(scheduler,claim['run']['run_id']))
    action_id=None
    try:
        for _ in range(100):
            await asyncio.sleep(.02)
            with client.app.state.session_factory() as db:
                action=db.query(ToolAction).filter_by(room_id=room,state='waiting').first()
                if action:action_id=action.action_id;break
        with client.app.state.session_factory() as db:
            status=db.get(OwnerRoutineRun,claim['run']['run_id']);detail=(status.state,status.failure_code)
        assert action_id and not calls,detail
        from app.services.work_state import project_work_states
        with client.app.state.session_factory() as db:
            projected=project_work_states(db,room,owner,[{'agent_id':bot}])[bot]
            assert projected.phase=='approval_waiting' and projected.round==1 and projected.action_id==action_id
        member=next(x for x in client.get(f'/api/v1/conversations/{room}/members',headers=headers(client,owner)).json() if x['agent_id']==bot)
        assert member['work_state']['phase']=='approval_waiting' and member['work_state']['round']==1 and member['work_state']['action_id']==action_id
        with client.app.state.session_factory() as db:
            from app.models.collaboration import RoomAgentRun
            action=db.get(ToolAction,action_id);action.state='started';db.commit()
            projected=project_work_states(db,room,owner,[{'agent_id':bot}])[bot]
            assert projected.phase=='tool_running' and projected.action_id==action_id and projected.round==1
            action.state='waiting';member_run=db.get(RoomAgentRun,(room,bot));marker=dict(member_run.active_tool)
            member_run.active_tool={**marker,'generation':marker['generation']+1};db.commit()
            projected=project_work_states(db,room,owner,[{'agent_id':bot}])[bot]
            assert projected.phase=='unknown' and projected.action_id is None
            member_run.active_tool=marker;db.commit()
        activities=client.get(f'/api/v1/tools/rooms/{room}/activity',headers=headers(client,owner)).json()
        visible=next(x for x in activities if x['action_id']==action_id)
        preview=client.get(f'/api/v1/tools/rooms/{room}/actions/{action_id}/preview',headers=headers(client,owner))
        assert preview.status_code==200,preview.text
        proof=preview.json()['routine_binding']
        assert proof==visible['routine_binding'] and proof['owner_id']==owner and proof['agent_id']==bot
        assert preview.json()['root_ref']['actor_type']=='system' and preview.json()['scope_status']=='active'
        decision={'decision':'approve','scope':'once','review_id':preview.json()['review_id'],
            'acknowledge_unverified_context':preview.json()['approval_policy']['unverified_context_required']}
        approved=client.post(f'/api/v1/tools/rooms/{room}/actions/{action_id}/decision',headers=headers(client,owner),json=decision)
        assert approved.status_code==200,approved.text
        await asyncio.wait_for(task,3)
        assert calls==[action_id]
        with client.app.state.session_factory() as db:
            run=db.get(OwnerRoutineRun,claim['run']['run_id']);assert run.state=='completed',run.failure_code
    finally:
        if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)

@pytest.mark.asyncio
async def test_stale_routine_blocks_approved_action_before_effect(room_client,tmp_path):
    from app.models.collaboration import ToolAction
    from app.services.providers import ToolCall
    from app.schemas.owner_routines import RoutineTest,RoutineEdit
    client,(owner,_,project,_,_)=room_client;broker=client.app.state.scheduler.broker
    bot,room,_=private_team(client,owner,project);scheduler=client.app.state.scheduler;scheduler.broker=broker
    target=tmp_path/'fixture';target.write_text('synthetic');calls=[]
    async def executor(action,grant,cancel):calls.append(1);return 'synthetic'
    broker.executor=executor
    async def provider(request):return ProviderResult(tool_calls=[ToolCall('call','read_file',{'path':str(target)})],continuation={'call_ids':['call']})
    scheduler.provider=provider
    record=client.post(f'/api/v1/bots/{bot}/routines',headers={**headers(client,owner),'Idempotency-Key':'create'},json=data(False)).json()
    rid=record['routine']['routine_id']
    with client.app.state.session_factory() as db:claim,_=routines.test_claim(db,owner,bot,rid,RoutineTest(expected_revision=1),'test')
    task=asyncio.create_task(routines.execute(scheduler,claim['run']['run_id']))
    try:
        for _ in range(100):
            await asyncio.sleep(.02)
            with client.app.state.session_factory() as db:
                action=db.query(ToolAction).filter_by(room_id=room,state='waiting').first()
                if action:break
        assert action and not calls
        with client.app.state.session_factory() as db:
            routines.update(db,owner,bot,rid,RoutineEdit(expected_revision=1,**data(False)))
        await asyncio.wait_for(task,3)
        assert not calls
        with client.app.state.session_factory() as db:
            assert db.get(OwnerRoutineRun,claim['run']['run_id']).state in ('failed','cancelled')
    finally:
        if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)


def test_natural_create_pause_and_deferred_test_exact_receipt(room_client):
    from test_bot_creation import tool
    from app.models.owner_routines import OwnerRoutinePendingTest
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner);bot,room,_=private_team(client,owner,project)
    scheduler=client.app.state.scheduler;receipts=[]
    async def create(request):
        if request.observations:
            receipts.append(json.loads(request.observations[-1].text));return ProviderResult(text='Routine saved for later.')
        assert 'create_bot_routine' in {x['name'] for x in request.tools}
        return tool('create_bot_routine',data(),'create-routine')
    scheduler.provider=create
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'@Ada create this recurring task'})
    assert response.status_code==201,response.text
    saved=receipts[-1];rid=saved['routine_id'];assert saved['state']=='saved' and saved['operation']=='create'
    assert client.get(f"/api/v1/bots/{bot}/routine-persistence/{saved['receipt_id']}",headers=own).json()['result_ref']==saved['result_ref']
    async def pause(request):
        if request.observations:
            receipts.append(json.loads(request.observations[-1].text));return ProviderResult(text='Paused.')
        return tool('set_bot_routine',{'routine_id':rid,'revision':1,'enabled':False},'pause-routine')
    scheduler.provider=pause
    assert client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'@Ada pause it'}).status_code==201
    assert receipts[-1]['operation']=='pause' and receipts[-1]['revision']==2
    async def queue(request):
        if request.observations:
            receipts.append(json.loads(request.observations[-1].text));return ProviderResult(text='Test queued.')
        return tool('test_bot_routine',{'routine_id':rid,'revision':2},'test-routine')
    scheduler.provider=queue
    assert client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'@Ada test it once'}).status_code==201
    queued=receipts[-1];assert queued['state']=='queued' and queued['operation']=='test'
    with client.app.state.session_factory() as db:
        pending=db.get(OwnerRoutinePendingTest,queued['receipt_id']);assert pending.state=='pending' and pending.run_id is None
        run_id=routines.claim_pending(db);assert run_id
        assert db.get(OwnerRoutinePendingTest,queued['receipt_id']).run_id==run_id
        assert routines.claim_pending(db) is None
    read=client.get(f"/api/v1/bots/{bot}/routine-persistence/{queued['receipt_id']}",headers=own).json()
    assert read['test']['state']=='started' and read['test']['run']['run_id']==run_id
    async def test_provider(request):return ProviderResult(text='NATURAL_TEST_RESULT',model_use={**model_use(request),'watermark_seq':request.watermark_seq})
    scheduler.provider=test_provider;scheduler.broker=None
    asyncio.run(routines.execute(scheduler,run_id))
    with client.app.state.session_factory() as db:
        run=db.get(OwnerRoutineRun,run_id);assert run.state=='completed' and run.result_event_id


@pytest.mark.asyncio
async def test_stop_after_tool_started_marks_effect_unknown(room_client,tmp_path):
    from app.models.collaboration import ToolAction
    from app.services.providers import ToolCall
    from app.services.room_service import cancel_room_work
    from app.schemas.owner_routines import RoutineTest
    client,(owner,_,project,_,_)=room_client;broker=client.app.state.scheduler.broker
    bot,room,_=private_team(client,owner,project);scheduler=client.app.state.scheduler;scheduler.broker=broker
    path=tmp_path/'file';path.write_text('synthetic');entered=asyncio.Event()
    async def executor(action,grant,cancel):entered.set();await asyncio.Event().wait()
    broker.executor=executor
    async def provider(request):return ProviderResult(tool_calls=[ToolCall('call','read_file',{'path':str(path)})],continuation={'call_ids':['call']})
    scheduler.provider=provider
    record=client.post(f'/api/v1/bots/{bot}/routines',headers={**headers(client,owner),'Idempotency-Key':'create'},json=data(False)).json()
    with client.app.state.session_factory() as db:claim,_=routines.test_claim(db,owner,bot,record['routine']['routine_id'],RoutineTest(expected_revision=1),'test')
    task=asyncio.create_task(routines.execute(scheduler,claim['run']['run_id']));action_id=None
    try:
        for _ in range(100):
            await asyncio.sleep(.02)
            with client.app.state.session_factory() as db:
                action=db.query(ToolAction).filter_by(room_id=room,state='waiting').first()
                if action:action_id=action.action_id;break
        assert action_id
        preview=client.get(f'/api/v1/tools/rooms/{room}/actions/{action_id}/preview',headers=headers(client,owner)).json()
        decision={'decision':'approve','scope':'once','review_id':preview['review_id'],
            'acknowledge_unverified_context':preview['approval_policy']['unverified_context_required']}
        assert client.post(f'/api/v1/tools/rooms/{room}/actions/{action_id}/decision',headers=headers(client,owner),json=decision).status_code==200
        await asyncio.wait_for(entered.wait(),2)
        with client.app.state.session_factory() as db:cancel_room_work(db,room)
        scheduler.cancel(room)
        await asyncio.wait_for(task,2)
        with client.app.state.session_factory() as db:
            action=db.get(ToolAction,action_id);run=db.get(OwnerRoutineRun,claim['run']['run_id'])
            assert action.state=='uncertain' and run.state=='unknown' and run.failure_code=='routine_effect_uncertain'
            assert run.result_event_id is None
    finally:
        if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)

@pytest.mark.asyncio
async def test_result_wrong_watermark_is_not_saved(room_client):
    from app.schemas.owner_routines import RoutineTest
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner)
    bot,room,_=private_team(client,owner,project);scheduler=client.app.state.scheduler;scheduler.broker=None
    row=client.post(f'/api/v1/bots/{bot}/routines',headers={**own,'Idempotency-Key':'create'},json=data(False)).json()['routine']
    with client.app.state.session_factory() as db:receipt,_=routines.test_claim(db,owner,bot,row['routine_id'],RoutineTest(expected_revision=1),'test')
    async def provider(request):return ProviderResult(text='MUST_NOT_SAVE',model_use={**model_use(request),'watermark_seq':request.watermark_seq+1})
    scheduler.provider=provider;await routines.execute(scheduler,receipt['run']['run_id'])
    with client.app.state.session_factory() as db:
        from app.models.collaboration import RoomEvent
        run=db.get(OwnerRoutineRun,receipt['run']['run_id'])
        assert run.state=='failed' and run.failure_code=='model_identity_unconfirmed'
        failure=db.get(RoomEvent,run.result_event_id)
        assert failure and failure.room_id==room and failure.kind=='failure'
        assert failure.payload['code']=='routine_failed' and failure.payload['trigger_event_id']==run.root_event_id
        assert failure.payload['routine']['run_id']==run.run_id
        assert all('MUST_NOT_SAVE' not in json.dumps(event.payload) for event in db.query(RoomEvent).filter_by(room_id=room))

@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['go','oauth'])
@pytest.mark.parametrize('family,args',[
    ('create_bot_routine',{'name':'R','instruction':'I','enabled':True,'schedule':{'kind':'interval','interval_minutes':120,'timezone':'UTC'}}),
    ('test_bot_routine',{'routine_id':'00000000-0000-4000-8000-000000000001','revision':1}),
])
async def test_routine_provider_wire_shape(monkeypatch,kind,family,args):
    from app.services.connections import ConnectionConfig
    from app.services.providers.base import ProviderRequest,wire_tool_name
    from app.services.providers.opencode_go import OpenCodeGoMessagesAdapter
    from app.services.providers.chatgpt import ChatGPTAdapter
    from app.services.routine_persistence import TOOL_SCHEMAS
    from app.services import connections
    base='https://api.openai.com/v1';model='synthetic-model'
    caps={'verified':{'model_id':model,'base_url':base,'tools':True},'model_features':{model:{
        'model_id':model,'base_url':base,'config_revision':1,'tested':{'text_generation':True,'tool_calling':True},'tool_families':{}}}}
    cfg=ConnectionConfig(1,'chatgpt' if kind=='oauth' else 'opencode-go',model,base,'synthetic-token',caps,
        provider_id='chatgpt' if kind=='oauth' else 'opencode-go',protocol_id='openai_responses' if kind=='oauth' else 'anthropic_messages',config_revision=1)
    req=ProviderRequest(connection=cfg,model_id=model,room_id=1,agent_id=1,tools=TOOL_SCHEMAS)
    adapter=ChatGPTAdapter() if kind=='oauth' else OpenCodeGoMessagesAdapter();assert wire_tool_name(family)==family
    if kind=='oauth':
        async def discover(_,transport=None):return [{'id':model}],False,None
        monkeypatch.setattr(connections,'discover_models',discover)
        async def post(request,path,headers,body):
            assert family in {x['name'] for x in body['tools'][0]['tools']}
            return {'output':[{'type':'function_call','namespace':'gamja','call_id':'call','name':family,'arguments':json.dumps(args)}]}
    else:
        monkeypatch.setattr(adapter,'_headers',lambda _: {})
        async def post(request,path,headers,body):
            assert family in {x['name'] for x in body['tools']}
            return {'content':[{'type':'tool_use','id':'call','name':family,'input':args}]}
    monkeypatch.setattr(adapter,'_post',post)
    result=await adapter.generate(req);assert result.tool_calls[0].arguments==args


def test_queued_test_is_cancelled_by_new_owner_message_before_trigger(room_client):
    from test_bot_creation import tool
    from app.models.owner_routines import OwnerRoutinePendingTest
    from app.services.events import append_event,ActorRef
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner);bot,room,_=private_team(client,owner,project)
    row=client.post(f'/api/v1/bots/{bot}/routines',headers={**own,'Idempotency-Key':'create'},json=data(False)).json()['routine']
    seen=[]
    async def provider(request):
        if request.observations:seen.append(json.loads(request.observations[-1].text));return ProviderResult(text='Queued.')
        return tool('test_bot_routine',{'routine_id':row['routine_id'],'revision':1},'test')
    client.app.state.scheduler.provider=provider
    assert client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'@Ada test once'}).status_code==201
    with client.app.state.session_factory() as db:
        append_event(db,room,ActorRef('user',owner),'message',{'content':'New request'})
        assert routines.claim_pending(db) is None
        pending=db.get(OwnerRoutinePendingTest,seen[0]['receipt_id']);assert pending.state=='cancelled' and pending.run_id is None


def test_natural_webhook_create_edit_preserves_key_boundary(room_client):
    from jsonschema import Draft202012Validator
    from test_bot_creation import tool
    from app.services.routine_persistence import TOOL_SCHEMAS
    client,(owner,_,project,_,_)=room_client;own=headers(client,owner);bot,room,_=private_team(client,owner,project)
    scheduler=client.app.state.scheduler;receipts=[]
    if not hasattr(scheduler.broker,'executor'):scheduler.broker.executor=None
    definition={'name':'Synthetic webhook','instruction':'Handle synthetic event','enabled':True,'schedule':{'kind':'webhook'}}
    schemas={item['name']:item['parameters'] for item in TOOL_SCHEMAS}
    assert Draft202012Validator(schemas['create_bot_routine']).is_valid(definition)
    assert not Draft202012Validator(schemas['create_bot_routine']).is_valid({**definition,'schedule':{'kind':'webhook','url':'extra'}})
    async def create(request):
        if request.observations:
            receipts.append(json.loads(request.observations[-1].text));return ProviderResult(text='Saved.')
        return tool('create_bot_routine',definition,'webhook-create')
    scheduler.provider=create
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'@Ada create webhook routine'})
    assert response.status_code==201, response.json().get('detail',{}).get('code')
    created=receipts[-1];rid=created['routine_id']
    assert created['operation']=='create' and created['revision']==1 and created['routine']['schedule']=={'kind':'webhook'}
    assert created['routine']['webhook']=={'configured':False,'generation':0}
    path=f'/api/v1/bots/{bot}/routines/{rid}'
    issued=client.post(path+'/webhook-key',headers=own,json={'expected_revision':1,'expected_generation':0})
    assert issued.status_code==200
    edited={**definition,'instruction':'Handle revised event','routine_id':rid,'revision':1}
    assert Draft202012Validator(schemas['edit_bot_routine']).is_valid(edited)
    assert not Draft202012Validator(schemas['edit_bot_routine']).is_valid({**edited,'schedule':{'kind':'webhook','timezone':'UTC'}})
    async def edit(request):
        if request.observations:
            receipts.append(json.loads(request.observations[-1].text));return ProviderResult(text='Updated.')
        return tool('edit_bot_routine',edited,'webhook-edit')
    scheduler.provider=edit
    assert client.post(f'/api/v1/conversations/{room}/messages',headers=own,json={'content':'@Ada edit this webhook routine'}).status_code==201
    changed=receipts[-1]
    assert changed['operation']=='edit' and changed['routine_id']==rid and changed['revision']==2
    assert changed['routine']['schedule']=={'kind':'webhook'} and changed['routine']['webhook']=={'configured':False,'generation':1}
    assert client.get(path,headers=own).json()['webhook']=={'configured':False,'generation':1}


def test_partial_routine_source_requires_exact_owner_denial(room_client):
    from types import SimpleNamespace
    from uuid import uuid4
    from app.models.collaboration import ToolAction,RoomTrigger
    from app.services.events import ActorRef,append_event
    client,(owner,other,project,_,_)=room_client;bot,room,_=private_team(client,owner,project)
    with client.app.state.session_factory() as db:
        root=append_event(db,room,ActorRef('user',owner),'message',{'content':'synthetic request'})
        trigger=RoomTrigger(event_id=root.event_id,room_id=room,state='done',selected_agents=[bot],current_round=1)
        action=ToolAction(action_id=str(uuid4()),room_id=room,agent_id=bot,trigger_event_id=root.event_id,family='read_file',arguments={},state='denied')
        db.add_all([trigger,action]);db.flush()
        denial=append_event(db,room,ActorRef('user',owner),'activity',{'action_id':action.action_id,'status':'denied','scope':None},idempotency_key='consent-deny:'+action.action_id)
        db.commit()
        pending=SimpleNamespace(agent_id=bot,room_id=room,owner_id=owner,root_request_event_id=root.event_id,activity_event_id=str(uuid4()))
        proof={'completion':{'state':'completed','outcome_status':'partial','pending_agent_count':0},'outcomes_truncated':False,'actions_truncated':False,
               'outcome_count':1,'outcomes':[{'agent_id':bot,'kind':'message','phase':'succeeded'}],
               'action_count':1,'actions':[{'action_id':action.action_id}]}
        assert routines._only_owner_denied_actions(db,pending,trigger,proof)
        correction=append_event(db,room,ActorRef('system',None),'activity',{'category':'provider_tool_correction','status':'retrying','trigger_event_id':root.event_id},idempotency_key='provider-tool-correction:'+root.event_id)
        db.flush();assert routines._only_owner_denied_actions(db,pending,trigger,proof)
        correction.payload={'category':'provider_tool_correction','status':'retrying','trigger_event_id':root.event_id,'extra':True};db.flush()
        assert not routines._only_owner_denied_actions(db,pending,trigger,proof)
        correction.payload={'category':'provider_tool_correction','status':'retrying','trigger_event_id':root.event_id};db.flush()
        correction.actor_type='user';correction.actor_id=owner;db.flush();assert not routines._only_owner_denied_actions(db,pending,trigger,proof)
        correction.actor_type='system';correction.actor_id=None;db.flush()
        action.started_at=datetime.utcnow();db.flush();assert not routines._only_owner_denied_actions(db,pending,trigger,proof)
        action.started_at=None;denial.actor_id=other;db.flush();assert not routines._only_owner_denied_actions(db,pending,trigger,proof)
        denial.actor_id=owner;action.approval_scope='once';db.flush();assert not routines._only_owner_denied_actions(db,pending,trigger,proof)


@pytest.mark.parametrize('mode',['single','multi','twice','profile_changed'])
def test_go_unrecognized_tool_correction_is_single_bounded(room_client,mode,monkeypatch):
    from app.models.models import Agent
    from app.models.collaboration import ProviderConnection
    from app.services.providers import ProviderFailure
    client,(owner,_,project,_,_)=room_client;bot,room,_=private_team(client,owner,project)
    scheduler=client.app.state.scheduler
    if not hasattr(scheduler.broker,'executor'):scheduler.broker.executor=None
    import app.services.scheduler as scheduler_module
    from uuid import uuid4
    scheduler.broker.session_id=str(uuid4())
    from dataclasses import replace
    original_build=scheduler_module.build_turn_context
    async def synthetic_go_context(*args,**kwargs):
        request=await original_build(*args,**kwargs)
        with client.app.state.session_factory() as db:
            db.get(ProviderConnection,request.connection_id).provider_id='opencode-go';db.commit()
        return replace(request,connection=replace(request.connection,provider_id='opencode-go'))
    monkeypatch.setattr(scheduler_module,'build_turn_context',synthetic_go_context)
    requests=[]; failures=[]
    async def provider(request):
        requests.append(request)
        if len(requests)==1 or mode=='twice':
            if mode=='profile_changed':
                with client.app.state.session_factory() as db:
                    db.get(Agent,bot).description='changed synthetic profile';db.commit()
            failure=ProviderFailure('provider_invalid_response','invalid')
            failure.tool_call_diagnostic={'reason':'unrecognized_name','offered_plain_builtin':False}
            if mode!='multi':failure.single_unrecognized_candidate=True
            failures.append(failure);raise failure
        return ProviderResult(text='SYNTHETIC_CORRECTED')
    scheduler.provider=provider
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=headers(client,owner),json={'content':'@Ada synthetic request'})
    assert len(requests)==(1 if mode in ('multi','profile_changed') else 2),response.json().get('detail',{}).get('code')
    assert response.status_code==(201 if mode=='single' else 502)
    if len(requests)==2:
        assert 'Use only the exact function names' in requests[1].system
        assert requests[1].tools==requests[0].tools and not requests[1].observations and requests[1].continuation is None


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol',['openai','anthropic'])
@pytest.mark.parametrize('count',[1,2])
async def test_go_wire_marks_only_one_unrecognized_candidate(monkeypatch,protocol,count):
    from app.services.connections import ConnectionConfig
    from app.services.providers.base import ProviderRequest,ProviderFailure
    from app.services.providers.opencode_go import OpenCodeGoMessagesAdapter,OpenCodeGoChatAdapter
    from app.services.routine_persistence import TOOL_SCHEMAS
    model='synthetic-model';base='https://api.openai.com/v1'
    caps={'verified':{'model_id':model,'base_url':base,'tools':True},'model_features':{model:{'model_id':model,'base_url':base,'config_revision':1,'tested':{'text_generation':True,'tool_calling':True},'tool_families':{}}}}
    config=ConnectionConfig(1,'opencode-go',model,base,'synthetic-token',caps,provider_id='opencode-go',protocol_id='anthropic_messages' if protocol=='anthropic' else 'openai_chat_completions',config_revision=1)
    request=ProviderRequest(connection=config,model_id=model,room_id=1,agent_id=1,tools=TOOL_SCHEMAS)
    adapter=OpenCodeGoMessagesAdapter() if protocol=='anthropic' else OpenCodeGoChatAdapter()
    monkeypatch.setattr(adapter,'_headers',lambda _: {})
    async def post(*_):
        if protocol=='anthropic':return {'content':[{'type':'tool_use','id':str(i),'name':'unknown_synthetic','input':{}} for i in range(count)]}
        return {'choices':[{'message':{'content':'','tool_calls':[{'id':str(i),'function':{'name':'unknown_synthetic','arguments':'{}'}} for i in range(count)]}}]}
    monkeypatch.setattr(adapter,'_post',post)
    with pytest.raises(ProviderFailure) as caught:await adapter.generate(request)
    assert caught.value.tool_call_diagnostic['reason']=='unrecognized_name'
    assert getattr(caught.value,'single_unrecognized_candidate',False) is (count==1)


@pytest.mark.asyncio
async def test_routine_projects_history_as_background_and_webhook_untrusted(room_client):
    from app.services.events import ActorRef,append_event
    from app.models.collaboration import RoomTrigger
    from app.services.providers.base import attributed_messages
    client,(owner,_,project,_,_)=room_client;bot,room,_=private_team(client,owner,project)
    scheduler=client.app.state.scheduler;scheduler.broker=None;requests=[]
    with client.app.state.session_factory() as db:
        prior_user=append_event(db,room,ActorRef('user',owner),'message',{'content':'OLD_USER_REQUEST_TEST_ROUTINE'})
        db.add(RoomTrigger(event_id=prior_user.event_id,room_id=room,state='done',selected_agents=[bot],current_round=1))
        prior_bot=append_event(db,room,ActorRef('agent',bot),'message',{'content':'OLD_AGENT_REPLY'})
        expected=[(prior_user.event_id,prior_user.seq,'user',owner,'OLD_USER_REQUEST_TEST_ROUTINE'),
                  (prior_bot.event_id,prior_bot.seq,'agent',bot,'OLD_AGENT_REPLY')]
        db.commit()
    definition={'name':'Synthetic webhook','instruction':'Use selected echo for CURRENT_ROUTINE_TASK','enabled':True,'schedule':{'kind':'webhook'}}
    made=client.post(f'/api/v1/bots/{bot}/routines',headers={**headers(client,owner),'Idempotency-Key':'background-webhook'},json=definition)
    rid=made.json()['routine']['routine_id']
    key=client.post(f'/api/v1/bots/{bot}/routines/{rid}/webhook-key',headers=headers(client,owner),json={'expected_revision':1,'expected_generation':0}).json()['key']
    with client.app.state.session_factory() as db:
        receipt,created=routines.webhook_claim(db,rid,key,'background-event',{'body':'UNTRUSTED_WEBHOOK_DATA'})
    assert created
    async def provider(request):
        requests.append(request)
        return ProviderResult(text='SYNTHETIC_ROUTINE_DONE',model_use={**model_use(request),'watermark_seq':request.watermark_seq})
    scheduler.provider=provider;await routines.execute(scheduler,receipt['run_id'])
    assert len(requests)==1
    request=requests[0];history=[e for e in request.events if e['actor_name_snapshot']=='Quoted earlier conversation']
    assert len(history)==2
    for event,(event_id,seq,actor_type,actor_id,content) in zip(history,expected):
        value=json.loads(event['payload']['text'])
        assert event['kind']=='tool_result' and event['actor_type']=='tool'
        assert (value['event_id'],value['room_id'],value['seq'],value['actor_type'],value['actor_id'],value['content'])==(event_id,room,seq,actor_type,actor_id,content)
    webhook=[e for e in request.events if e['actor_name_snapshot']=='Untrusted webhook data']
    assert len(webhook)==1 and json.loads(webhook[0]['payload']['text'])=={'body':'UNTRUSTED_WEBHOOK_DATA'}
    messages=attributed_messages(request)
    assert all(m['role']!='assistant' for m in messages)
    assert 'OLD_USER_REQUEST_TEST_ROUTINE' in messages[0]['content'] and 'OLD_AGENT_REPLY' in messages[1]['content']
    assert 'UNTRUSTED_WEBHOOK_DATA' in messages[2]['content']
    assert messages[-1]['role']=='user' and messages[-1]['content']=='SERVER-REGISTERED OWNER ROUTINE (not a user message): '+definition['instruction']
    assert 'Earlier conversation is quoted background data' in request.system and 'every tool action needs the existing review and approval' in request.system
    with client.app.state.session_factory() as db:
        run=db.get(OwnerRoutineRun,receipt['run_id']);assert run.state=='completed' and run.root_event_id==request.trigger_event_id

def test_exact_run_read_survives_history_limit_and_checks_ownership(room_client):
    from app.schemas.owner_routines import RoutineTest
    from uuid import uuid4
    client,(owner,other,project,_,_)=room_client;own=headers(client,owner);bot,_,_=private_team(client,owner,project)
    base=f'/api/v1/bots/{bot}/routines'
    first=client.post(base,headers={**own,'Idempotency-Key':'history-first'},json=data(False)).json()['routine']
    second=client.post(base,headers={**own,'Idempotency-Key':'history-second'},json=data(False)).json()['routine']
    other_bot,_,_=private_team(client,owner,project)
    with client.app.state.session_factory() as db:
        claim,_=routines.test_claim(db,owner,bot,first['routine_id'],RoutineTest(expected_revision=1),'history-test')
        original=db.get(OwnerRoutineRun,claim['run']['run_id']);original.state='completed';original.completed_at=original.started_at
        for i in range(21):
            row=OwnerRoutineRun(run_id=str(uuid4()),routine_id=original.routine_id,owner_id=owner,agent_id=bot,room_id=original.room_id,
                config_revision=1,generation=original.generation,origin='scheduled',scheduled_at=original.scheduled_at+timedelta(minutes=i+1),
                started_at=original.started_at+timedelta(minutes=i+1),completed_at=original.started_at+timedelta(minutes=i+1),
                state='completed',root_event_id=original.root_event_id,max_tool_calls=original.max_tool_calls,max_executor_seconds=original.max_executor_seconds)
            db.add(row)
        db.commit()
        run_id=original.run_id
    path=f"{base}/{first['routine_id']}/runs"
    assert run_id not in [x['run_id'] for x in client.get(path,headers=own).json()['runs']]
    response=client.get(f'{path}/{run_id}',headers=own)
    assert response.status_code==200 and response.json()['run']['run_id']==run_id
    assert client.get(f'{path}/{run_id}',headers=headers(client,other)).status_code==404
    assert client.get(f"{base}/{second['routine_id']}/runs/{run_id}",headers=own).status_code==404
    assert client.get(f"/api/v1/bots/{other_bot}/routines/{first['routine_id']}/runs/{run_id}",headers=own).status_code==404
    assert client.get(f'{path}/{uuid4()}',headers=own).status_code==404
