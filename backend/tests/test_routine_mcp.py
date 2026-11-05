"""Synthetic routine MCP approval, origin and no-replay coverage."""
import asyncio
from datetime import datetime,timedelta
from types import SimpleNamespace
import pytest
from test_room_state import room_client,headers
from test_bot_creation import private_team
from test_execution_scopes import FakeExecutor
from test_mcp_delegated import bind_tool
from test_primary_checkins import model_use
from app.models.collaboration import ToolAction,RoomEvent,RoomAgentRun
from app.models.mcp import MCPBinding
from app.models.owner_routines import OwnerRoutine,OwnerRoutineRun,OwnerRoutineWebhookKey
from app.schemas.owner_routines import RoutineTest
from app.services import owner_routines as routines
from app.services.providers import ProviderResult,ToolCall
from app.services.tool_broker import ToolBroker


@pytest.mark.asyncio
@pytest.mark.parametrize('origin,outcome',[('test','completed'),('scheduled','completed'),('webhook','completed'),('test','uncertain'),('test','stale'),('test','routine_changed'),('test','stopped'),('webhook','key_changed'),('test','stop_after_start')])
async def test_routine_mcp_requires_once_and_retains_exact_run(room_client,monkeypatch,origin,outcome):
    client,(owner,_,project,_,_)=room_client;auth=headers(client,owner);bot,room,_=private_team(client,owner,project)
    scheduler=client.app.state.scheduler;calls=[];requests=[];started=asyncio.Event()
    class Bridge:
        available=True;core_epoch='b'*64
        async def close(self):pass
        async def configure(self,p):return {'state':'configured'}
        async def bind(self,p):return {'state':'bound'}
        async def revoke(self,p):return {'state':'revoked'}
        async def credential_status(self,p):return {'state':'missing'}
        async def discover(self,p):return {'state':'failed'}
        async def issue(self,p):calls.append(('issue',p));return {'state':'issued','nonce':'a'*64}
        async def start(self,p):
            calls.append(('start',p));started.set()
            if outcome=='stop_after_start':await asyncio.Event().wait()
            return {'state':'uncertain'} if outcome=='uncertain' else {'state':'completed','result':{'content':'SYNTHETIC_MCP_RESULT'}}
        async def cancel(self,p):calls.append(('cancel',p));return {'state':'cancel_requested'}
    bridge=Bridge();client.app.state.mcp_client=bridge
    monkeypatch.setattr(client.app.state.owner_epoch_provider,'current',lambda _: 'c'*64)
    scheduler.broker=ToolBroker(client.app.state.session_factory,FakeExecutor(),owner_epochs=SimpleNamespace(current=lambda _:'c'*64))
    with client.app.state.session_factory() as db:meta=bind_tool(db,owner,bot)
    async def provider(request):
        requests.append(request)
        from app.services.context import TOOL_APPROVAL_GUIDANCE
        assert request.tools and TOOL_APPROVAL_GUIDANCE in request.system
        if not request.observations:
            offered=[t for t in request.tools if t.get('mcp_binding')]
            assert len(offered)==1 and offered[0]['mcp_binding']==meta
            return ProviderResult(tool_calls=[ToolCall('mcp-routine',offered[0]['name'],{'message':'synthetic'})],continuation={'call_ids':['mcp-routine']})
        assert request.observations[0].state=='completed' and 'SYNTHETIC_MCP_RESULT' in request.observations[0].text
        return ProviderResult(text='SYNTHETIC_ROUTINE_MCP_DONE',model_use={**model_use(request),'watermark_seq':request.watermark_seq})
    scheduler.provider=provider
    definition={'name':'Synthetic','instruction':'Use the selected synthetic echo','enabled':origin!='test',
                'schedule':{'kind':'webhook'} if origin=='webhook' else {'kind':'interval','interval_minutes':5,'timezone':'UTC'}}
    made=client.post(f'/api/v1/bots/{bot}/routines',headers={**auth,'Idempotency-Key':'routine-mcp-create'},json=definition)
    assert made.status_code==200;rid=made.json()['routine']['routine_id']
    if origin=='test':
        with client.app.state.session_factory() as db:claim,_=routines.test_claim(db,owner,bot,rid,RoutineTest(expected_revision=1),'routine-mcp-test')
        run_id=claim['run']['run_id']
    elif origin=='scheduled':
        with client.app.state.session_factory() as db:
            row=db.get(OwnerRoutine,rid);row.next_run_at=datetime.utcnow()-timedelta(seconds=1);db.commit()
            run_id=routines.claim_due(db)
    else:
        key=client.post(f'/api/v1/bots/{bot}/routines/{rid}/webhook-key',headers=auth,json={'expected_revision':1,'expected_generation':0}).json()['key']
        with client.app.state.session_factory() as db:claim,created=routines.webhook_claim(db,rid,key,'synthetic-event',{'example':'value'})
        assert created;run_id=claim['run_id']
    task=asyncio.create_task(routines.execute(scheduler,run_id));action_id=None
    try:
        for _ in range(150):
            await asyncio.sleep(.02)
            with client.app.state.session_factory() as db:
                action=db.query(ToolAction).filter_by(room_id=room,agent_id=bot,family='mcp_tool_call',state='waiting').first()
                if action:action_id=action.action_id;break
            if task.done():break
        with client.app.state.session_factory() as db:
            run=db.get(OwnerRoutineRun,run_id);detail=(run.state,run.failure_code)
        assert action_id and not calls,detail
        preview=client.get(f'/api/v1/tools/rooms/{room}/actions/{action_id}/preview',headers=auth)
        assert preview.status_code==200,preview.text
        proof=preview.json();assert proof['routine_binding']['run_id']==run_id and proof['routine_binding']['origin']==origin
        assert proof['mcp_binding']['server_id']==meta['server_id'] and proof['mcp_binding']['schema_sha256']==meta['schema_sha256']
        if outcome in ('stale','routine_changed','stopped','key_changed'):
            with client.app.state.session_factory() as db:
                if outcome=='stale':
                    binding=db.get(MCPBinding,(owner,bot,meta['server_id']));binding.enabled=False;binding.revision+=1
                elif outcome=='routine_changed':db.get(OwnerRoutine,rid).revision+=1
                elif outcome=='stopped':
                    member=db.get(RoomAgentRun,(room,bot));member.state='stopped';member.generation+=1
                else:db.get(OwnerRoutineWebhookKey,rid).generation+=1
                db.commit()
            assert client.get(f'/api/v1/tools/rooms/{room}/actions/{action_id}/preview',headers=auth).status_code==409
            assert not calls
            return
        decision={'decision':'approve','scope':'once','review_id':proof['review_id'],'acknowledge_unverified_context':proof['approval_policy']['unverified_context_required']}
        approved=client.post(f'/api/v1/tools/rooms/{room}/actions/{action_id}/decision',headers=auth,json=decision)
        assert approved.status_code==200,approved.text
        if outcome=='stop_after_start':
            await asyncio.wait_for(started.wait(),5);scheduler.cancel_routine(owner,routine_id=rid)
        await asyncio.wait_for(task,5)
        if outcome=='stop_after_start':
            assert [v[0] for v in calls]==['issue','start','cancel']
            with client.app.state.session_factory() as db:assert db.get(OwnerRoutineRun,run_id).state=='unknown'
            assert len(requests)==1
            return
        assert [v[0] for v in calls]==['issue','start']
        with client.app.state.session_factory() as db:
            run=db.get(OwnerRoutineRun,run_id);root=db.get(RoomEvent,run.root_event_id)
            assert root.actor_type=='system' and root.payload['routine']['run_id']==run_id
            assert calls[0][1]['binding']['root_event_id']==run.root_event_id and calls[0][1]['binding']['generation']==run.generation
            if outcome=='completed':
                result=db.get(RoomEvent,run.result_event_id);assert run.state=='completed'
                assert result.payload['trigger_event_id']==root.event_id and result.payload['routine']['run_id']==run_id
                assert len(requests)==2 and requests[1].trigger_event_id==root.event_id and requests[1].watermark_seq==root.seq
            else:assert run.state=='unknown' and len(requests)==1
    finally:
        if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)


@pytest.mark.asyncio
async def test_routine_without_offered_tools_omits_approval_guidance(room_client):
    from app.services.context import TOOL_APPROVAL_GUIDANCE
    client,(owner,_,project,_,_)=room_client;bot,_,_=private_team(client,owner,project)
    scheduler=client.app.state.scheduler;scheduler.broker=None;seen=[]
    definition={'name':'Synthetic','instruction':'Answer a synthetic question','enabled':False,
                'schedule':{'kind':'interval','interval_minutes':5,'timezone':'UTC'}}
    made=client.post(f'/api/v1/bots/{bot}/routines',headers={**headers(client,owner),'Idempotency-Key':'no-tools-routine'},json=definition)
    with client.app.state.session_factory() as db:
        claim,_=routines.test_claim(db,owner,bot,made.json()['routine']['routine_id'],RoutineTest(expected_revision=1),'no-tools-test')
    async def provider(request):
        seen.append(request);return ProviderResult(text='SYNTHETIC_RESULT',model_use={**model_use(request),'watermark_seq':request.watermark_seq})
    scheduler.provider=provider;await routines.execute(scheduler,claim['run']['run_id'])
    assert len(seen)==1 and not seen[0].tools and TOOL_APPROVAL_GUIDANCE not in seen[0].system
    with client.app.state.session_factory() as db:assert db.get(OwnerRoutineRun,claim['run']['run_id']).state=='completed'
