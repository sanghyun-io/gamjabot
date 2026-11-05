from types import SimpleNamespace
from dataclasses import replace
import pytest
from fastapi import HTTPException
from app.models.collaboration import ProviderConnection, RoomTrigger, RoomAgentRun, ToolAction
from app.models.models import Agent
from app.services import browser_demo_draft as draft
from app.services.providers.base import ProviderResult, attributed_messages
from app.schemas.browser_demonstration import DraftPreflight, DraftSubmission
from test_room_state import room_client, create_room
from test_scheduler import ready_agents


class Registry:
    def __init__(self,owner,room,agent):
        self.item={'owner_id':owner,'room_id':room,'agent_id':agent,'state':'stopped','profile_id':'p','profile_revision':1}
        self.profiles=SimpleNamespace(read=lambda *args:{'revision':1,'agent_ids':[agent]})
    def owned(self,demo,owner,room):
        assert demo=='a'*32 and (owner,room)==(self.item['owner_id'],self.item['room_id'])
        return self.item


def prepare(client,owner,room):
    agents=ready_agents(client,owner,room,('Ada','Ben')); agent=agents[0]
    epochs=SimpleNamespace(value='a'*64); epochs.current=lambda _: epochs.value
    client.app.state.owner_epoch_provider=epochs
    client.app.state.scheduler.broker.owner_epochs=epochs
    with client.app.state.session_factory() as db:
        a=db.get(Agent,agent); c=db.get(ProviderConnection,a.connection_id)
        c.capabilities={'model_features':{'exact':{'model_id':'exact','base_url':c.base_url,'config_revision':c.readiness_revision,'tested':{'text_generation':True}}}}
        from app.models.browser_demonstration import BrowserDemonstration
        db.add(BrowserDemonstration(demo_id='a'*32,owner_id=owner,room_id=room,agent_id=agent,profile_id='p',profile_revision=1,
            state='stopped',goal='Find a page',steps=['Click search'],include_target_labels=False,
            review={'goal':'Find a page','steps':['Click search'],'rules':'Ask before changes','fields':None},review_revision=1,
            command_key='start',command_digest='a'*64,instance_id='b'*32))
        db.commit()
    return agent,agents,Registry(owner,room,agent)


def input_value(agent): return DraftPreflight(agent_id=agent,review_revision=1,reviewed_goal='Find a page',reviewed_steps=['Click search'],reviewed_rules='Ask before changes')


def test_preview_binds_reviewed_text_and_idempotency(room_client):
    client,(owner,_,project,_,_)=room_client; room=create_room(client,owner,project,'draft');agent,_,registry=prepare(client,owner,room);value=input_value(agent)
    with client.app.state.session_factory() as db:
        preview=draft.preflight(db,owner,room,'a'*32,value,registry,client.app.state.owner_epoch_provider)
        assert preview['connection_name']=='Synthetic' and preview['provider_id']=='compatible'
        v=DraftSubmission(**value.model_dump(),consent=True,preflight_token=preview['preflight_token'])
        with pytest.raises(HTTPException):draft.submit(db,owner,room,'a'*32,v.model_copy(update={'reviewed_goal':'changed'}),'key',registry,client.app.state.owner_epoch_provider)
        result=draft.submit(db,owner,room,'a'*32,v,'key',registry,client.app.state.owner_epoch_provider)
        assert draft.submit(db,owner,room,'a'*32,v,'key',registry,client.app.state.owner_epoch_provider)['replayed'] is True
        trigger=db.get(RoomTrigger,result['event_id']);assert trigger.selected_agents==[agent] and trigger.max_tool_calls==0
        assert draft.read(db,owner,room,'a'*32,'key')['status']=='submitted'
        with pytest.raises(HTTPException): draft.submit(db,owner,room,'a'*32,v.model_copy(update={'reviewed_rules':'changed'}),'key',registry,client.app.state.owner_epoch_provider)


def test_one_scheduler_call_only_reviewed_input_and_draft_result(room_client):
    client,(owner,_,project,_,_)=room_client;room=create_room(client,owner,project,'draft');agent,agents,registry=prepare(client,owner,room);value=input_value(agent)
    calls=[]
    async def provider(req):
        calls.append(req); assert req.tools==[] and req.memory is None and req.events==[]
        assert req.system==draft.SYSTEM
        messages=attributed_messages(req);assert len(messages)==1 and 'Click search' in messages[0]['content'] and 'system-Ada' not in str(messages)
        return ProviderResult(text='{"name":"Search","description":"Draft","content":"Follow reviewed steps"}')
    client.app.state.scheduler.provider=provider
    with client.app.state.session_factory() as db:
        token=draft.preflight(db,owner,room,'a'*32,value,registry,client.app.state.owner_epoch_provider)['preflight_token']
        sent=draft.submit(db,owner,room,'a'*32,DraftSubmission(**value.model_dump(),consent=True,preflight_token=token),'key',registry,client.app.state.owner_epoch_provider)
    async def run(): await client.app.state.scheduler.schedule_user_event(room,sent['event_id'],wait=True)
    client.portal.call(run)
    with client.app.state.session_factory() as db:
        result=draft.read(db,owner,room,'a'*32,'key'); assert result['status']=='completed',result
        assert result['fields']=={'name':'Search','description':'Draft','content':'Follow reviewed steps'}
        from app.models.collaboration import RoomEvent
        event=db.query(RoomEvent).filter_by(room_id=room,actor_type='agent',kind='message').one()
        assert event.payload['content']=='Draft ready for review: Search'
        assert db.query(ToolAction).count()==0
    assert len(calls)==1


def test_pending_stop_is_scoped_and_never_calls_model(room_client):
    client,(owner,_,project,_,_)=room_client;room=create_room(client,owner,project,'draft');agent,agents,registry=prepare(client,owner,room);value=input_value(agent)
    with client.app.state.session_factory() as db:
        token=draft.preflight(db,owner,room,'a'*32,value,registry,client.app.state.owner_epoch_provider)['preflight_token']
        result=draft.submit(db,owner,room,'a'*32,DraftSubmission(**value.model_dump(),consent=True,preflight_token=token),'key',registry,client.app.state.owner_epoch_provider)
        out=draft.stop(db,owner,room,'a'*32,'key',client.app.state.scheduler)
        assert out['status']=='cancelled'
        assert db.get(RoomTrigger,result['event_id']).state=='cancelled'
        assert db.get(RoomAgentRun,(room,agents[1])).state=='idle'
        assert draft.stop(db,owner,room,'a'*32,'key',client.app.state.scheduler)['status']=='cancelled'


@pytest.mark.parametrize('mode', ['bad_json','tool_call','model_change','epoch_change'])
def test_invalid_output_tool_call_or_changed_model_never_saves_draft(room_client,mode):
    from app.services.providers.base import ToolCall
    client,(owner,_,project,_,_)=room_client;room=create_room(client,owner,project,'draft');agent,_,registry=prepare(client,owner,room);value=input_value(agent)
    async def provider(req):
        if mode=='model_change':
            with client.app.state.session_factory() as db:
                db.get(Agent,agent).model_id='changed';db.commit()
        if mode=='epoch_change': client.app.state.owner_epoch_provider.value='b'*64
        if mode=='tool_call': return ProviderResult(tool_calls=[ToolCall('x','write_file',{})],continuation={'call_ids':['x']})
        return ProviderResult(text='bad json' if mode=='bad_json' else '{"name":"X","description":"","content":"x"}')
    client.app.state.scheduler.provider=provider
    with client.app.state.session_factory() as db:
        token=draft.preflight(db,owner,room,'a'*32,value,registry,client.app.state.owner_epoch_provider)['preflight_token']
        result=draft.submit(db,owner,room,'a'*32,DraftSubmission(**value.model_dump(),consent=True,preflight_token=token),'key',registry,client.app.state.owner_epoch_provider)
    async def run(): await client.app.state.scheduler.schedule_user_event(room,result['event_id'],wait=True)
    client.portal.call(run)
    with client.app.state.session_factory() as db:
        state=draft.read(db,owner,room,'a'*32,'key'); assert state['status']=='failed',state
        assert state['fields'] is None and db.query(ToolAction).count()==0


def test_stop_after_admission_is_uncertain_and_does_not_recall(room_client):
    client,(owner,_,project,_,_)=room_client;room=create_room(client,owner,project,'draft');agent,_,registry=prepare(client,owner,room);value=input_value(agent);calls=[]
    async def provider(req):
        calls.append(req)
        with client.app.state.session_factory() as db:
            result=draft.stop(db,owner,room,'a'*32,'key',client.app.state.scheduler)
            assert result['status']=='uncertain'
        return ProviderResult(text='{"name":"X","description":"","content":"x"}')
    client.app.state.scheduler.provider=provider
    with client.app.state.session_factory() as db:
        token=draft.preflight(db,owner,room,'a'*32,value,registry,client.app.state.owner_epoch_provider)['preflight_token']
        sent=draft.submit(db,owner,room,'a'*32,DraftSubmission(**value.model_dump(),consent=True,preflight_token=token),'key',registry,client.app.state.owner_epoch_provider)
    async def run(): await client.app.state.scheduler.schedule_user_event(room,sent['event_id'],wait=True)
    client.portal.call(run);client.portal.call(run)
    with client.app.state.session_factory() as db:
        result=draft.read(db,owner,room,'a'*32,'key');assert result['status']=='uncertain' and result['fields'] is None
    assert len(calls)==1


def test_review_revision_rejects_stale_preflight_and_interrupted_allowed(room_client):
    from app.models.browser_demonstration import BrowserDemonstration
    client,(owner,_,project,_,_)=room_client;room=create_room(client,owner,project,'draft');agent,_,registry=prepare(client,owner,room);value=input_value(agent)
    registry.item['state']='interrupted'
    with client.app.state.session_factory() as db:
        row=db.get(BrowserDemonstration,'a'*32);row.state='interrupted';db.commit()
        token=draft.preflight(db,owner,room,'a'*32,value,registry,client.app.state.owner_epoch_provider)['preflight_token']
        row.review_revision=2;db.commit()
        with pytest.raises(HTTPException): draft.submit(db,owner,room,'a'*32,DraftSubmission(**value.model_dump(),consent=True,preflight_token=token),'key',registry,client.app.state.owner_epoch_provider)


def test_changed_review_after_response_is_not_applied_on_read(room_client):
    from app.models.browser_demonstration import BrowserDemonstration
    client,(owner,_,project,_,_)=room_client;room=create_room(client,owner,project,'draft');agent,_,registry=prepare(client,owner,room);value=input_value(agent)
    async def provider(req): return ProviderResult(text='{"name":"Search","description":"Draft","content":"Steps"}')
    client.app.state.scheduler.provider=provider
    with client.app.state.session_factory() as db:
        token=draft.preflight(db,owner,room,'a'*32,value,registry,client.app.state.owner_epoch_provider)['preflight_token']
        result=draft.submit(db,owner,room,'a'*32,DraftSubmission(**value.model_dump(),consent=True,preflight_token=token),'key',registry,client.app.state.owner_epoch_provider)
    async def run(): await client.app.state.scheduler.schedule_user_event(room,result['event_id'],wait=True)
    client.portal.call(run)
    with client.app.state.session_factory() as db:
        assert draft.read(db,owner,room,'a'*32,'key')['status']=='completed'
        row=db.get(BrowserDemonstration,'a'*32);row.review_revision=2;row.review={**row.review,'fields':{'name':'edited'}};db.commit()
        # Reads use only persisted command/events, no live registry.
        result=draft.read(db,owner,room,'a'*32,'key');assert result['status']=='stale' and result['fields'] is None and result['review_revision']==1


def test_review_changed_during_model_call_never_persists_result(room_client):
    from app.models.browser_demonstration import BrowserDemonstration
    client,(owner,_,project,_,_)=room_client;room=create_room(client,owner,project,'draft');agent,_,registry=prepare(client,owner,room);value=input_value(agent);calls=[]
    async def provider(req):
        calls.append(req)
        with client.app.state.session_factory() as db:
            row=db.get(BrowserDemonstration,'a'*32);row.review_revision=2;db.commit()
        return ProviderResult(text='{"name":"Search","description":"Draft","content":"Steps"}')
    client.app.state.scheduler.provider=provider
    with client.app.state.session_factory() as db:
        token=draft.preflight(db,owner,room,'a'*32,value,registry,client.app.state.owner_epoch_provider)['preflight_token']
        result=draft.submit(db,owner,room,'a'*32,DraftSubmission(**value.model_dump(),consent=True,preflight_token=token),'key',registry,client.app.state.owner_epoch_provider)
    async def run(): await client.app.state.scheduler.schedule_user_event(room,result['event_id'],wait=True)
    client.portal.call(run);client.portal.call(run)
    with client.app.state.session_factory() as db:
        state=draft.read(db,owner,room,'a'*32,'key');assert state['fields'] is None and state['status']=='failed'
    assert len(calls)==1
