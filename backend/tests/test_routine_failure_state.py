import pytest
from test_room_state import room_client,headers
from test_bot_creation import private_team
from test_owner_routines import data
from app.models.owner_routines import OwnerRoutineRun
from app.models.collaboration import RoomEvent
from app.schemas.owner_routines import RoutineTest
from app.services import owner_routines as routines
from app.services.work_state import project_work_states
from app.services.llm_service import LLMFailure

@pytest.mark.asyncio
async def test_registered_failure_exact_ref_and_old_failure_unknown(room_client):
    client,(owner,_,project,_,_)=room_client;bot,room,_=private_team(client,owner,project)
    scheduler=client.app.state.scheduler;scheduler.broker=None
    async def provider(_):raise LLMFailure('unsupported_option','private diagnostic should not be written')
    scheduler.provider=provider
    created=client.post(f'/api/v1/bots/{bot}/routines',headers={**headers(client,owner),'Idempotency-Key':'failure'},json=data(False)).json()
    with client.app.state.session_factory() as db:
        claim,_=routines.test_claim(db,owner,bot,created['routine']['routine_id'],RoutineTest(expected_revision=1),'test-failure')
    run_id=claim['run']['run_id'];await routines.execute(scheduler,run_id)
    with client.app.state.session_factory() as db:
        run=db.get(OwnerRoutineRun,run_id);event=db.get(RoomEvent,run.result_event_id)
        assert run.state=='failed' and run.failure_code=='unsupported_option'
        assert event.kind=='failure' and event.actor_id==bot and event.payload['code']=='routine_failed'
        assert event.payload['trigger_event_id']==run.root_event_id and event.payload['generation']==run.generation
        assert 'private diagnostic' not in str(event.payload)
        state=project_work_states(db,room,owner,[{'agent_id':bot}])[bot]
        assert state.phase=='failed' and state.result.event_id==event.event_id
        run.result_event_id=None;db.commit()
        assert project_work_states(db,room,owner,[{'agent_id':bot}])[bot].phase=='unknown'
