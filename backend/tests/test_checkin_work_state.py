"""Primary check-in work state must use exact durable ledger and result identity."""
import pytest
from test_room_state import room_client
from test_primary_checkins import seed_due,model_use
from app.models.primary_checkins import PrimaryCheckinRun
from app.models.collaboration import RoomEvent,RoomTrigger,RoomAgentRun
from app.services import primary_checkins as service
from app.services.work_state import project_work_states
from app.services.providers import ProviderResult


def view(db,room,owner,agent):
    return project_work_states(db,room,owner,[{'agent_id':agent}])[agent]

@pytest.mark.asyncio
async def test_checkin_completion_not_confused_with_previous_owner_request(room_client):
    client,(owner,_,project,_,_)=room_client
    agent,room,rid=seed_due(client,owner,project,history=True)
    with client.app.state.session_factory() as db:
        previous=db.query(RoomEvent).filter_by(room_id=room,actor_type='user',kind='message').order_by(RoomEvent.seq.desc()).first()
        db.add(RoomTrigger(event_id=previous.event_id,room_id=room,state='done',selected_agents=[agent],
            max_rounds=1,max_tool_calls=0,max_executor_seconds=60,current_round=1));db.commit()
        active=view(db,room,owner,agent);assert active.phase=='thinking' and active.scope=='current'
        row=db.get(PrimaryCheckinRun,rid);root_id=row.root_event_id
    async def provider(request):return ProviderResult(text='REAL_CHECKIN',model_use=model_use(request))
    client.app.state.scheduler.provider=provider
    await service.execute(client.app.state.scheduler,rid)
    with client.app.state.session_factory() as db:
        state=view(db,room,owner,agent)
        assert state.phase=='completed' and state.scope=='previous' and state.trigger_event_id==root_id
        assert state.request.event_id==root_id and state.result.event_id==db.get(PrimaryCheckinRun,rid).result_event_id
        result=db.get(RoomEvent,state.result.event_id);result.payload={**result.payload,'checkin':{'version':1,'run_id':'forged'}};db.commit()
        assert view(db,room,owner,agent).phase=='unknown'

@pytest.mark.parametrize('state',['failed','cancelled','interrupted'])
def test_terminal_requires_matching_ledger_and_generation(room_client,state):
    client,(owner,_,project,_,_)=room_client
    agent,room,rid=seed_due(client,owner,project)
    with client.app.state.session_factory() as db:
        row=db.get(PrimaryCheckinRun,rid);row.state=state;row.completed_at=service.now()
        run=db.get(RoomAgentRun,(room,agent));run.state='idle';run.pending_task={};db.commit()
        assert view(db,room,owner,agent).phase==state
        run.generation+=4;db.commit()
        assert view(db,room,owner,agent).phase=='unknown'

@pytest.mark.asyncio
async def test_actual_room_cancel_generation_projects_cancelled(room_client):
    import asyncio
    from app.services import room_service
    client,(owner,_,project,_,_)=room_client
    agent,room,rid=seed_due(client,owner,project)
    entered=asyncio.Event()
    async def provider(request):
        entered.set()
        await asyncio.Event().wait()
    scheduler=client.app.state.scheduler;scheduler.provider=provider
    task=asyncio.create_task(service.execute(scheduler,rid))
    await asyncio.wait_for(entered.wait(),2)
    with client.app.state.session_factory() as db:
        row=db.get(PrimaryCheckinRun,rid);generation=row.generation;root=row.root_event_id
        room_service.cancel_room_work(db,room)
    scheduler.cancel(room)
    await asyncio.wait_for(task,2)
    with client.app.state.session_factory() as db:
        row=db.get(PrimaryCheckinRun,rid);run=db.get(RoomAgentRun,(room,agent))
        assert row.state=='cancelled' and row.result_event_id is None
        assert run.generation==generation+1 and run.trigger_event_id==root
        assert view(db,room,owner,agent).phase=='cancelled'
        run.generation+=1;db.commit()
        assert view(db,room,owner,agent).phase=='unknown'
