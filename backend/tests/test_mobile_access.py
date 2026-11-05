"""Personal mobile boundary against disposable SQLite + the existing Core routes.
No real credentials, provider requests, Tailscale mutation or desktop effects.
"""
import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from uuid import uuid4
import httpx
import pytest
from test_room_state import room_client, headers, create_room
from test_review_preview import preview_fixture
from app.services.mobile_access import COOKIE, MobileAccess
from app.services.core_auth_broker import OwnerBinding
from app.models.models import Conversation
from app.models.collaboration import RoomEvent, ToolAction, RoomGrant

ORIGIN='https://fixture.example.ts.net:8443'

@pytest.fixture
def mobile(preview_fixture,tmp_path):
    client,owner,broker,path,proposal=preview_fixture
    epoch=SimpleNamespace(binding=OwnerBinding(owner,'ab'*32))
    provider=SimpleNamespace(snapshot=lambda:epoch.binding)
    client.app.state.owner_epoch_provider=provider
    m=MobileAccess(tmp_path/'mobile-fixture',client.app)
    m.state.update(enabled=True,owner=owner,origin=ORIGIN);m._save();m.task=SimpleNamespace(done=lambda:False)
    client.app.state.mobile_access=m
    yield m,epoch,client,owner,broker,path,proposal
    m.task=None

@asynccontextmanager
async def paired(m):
    code=m.pairing(m.state['owner'])['code']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m.web),base_url=ORIGIN,headers={'Origin':ORIGIN,'X-Gamja-Mobile':'1'}) as c:
        response=await c.post('/pair',json={'code':code,'name':'Synthetic phone'})
        assert response.status_code==200
        assert 'Secure' in response.headers['set-cookie'] and 'HttpOnly' in response.headers['set-cookie'] and 'SameSite=strict' in response.headers['set-cookie']
        yield c,code

@pytest.mark.asyncio
async def test_pair_one_use_origin_scope_and_revoke(mobile):
    m,_,client,owner,_,_,_=mobile
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m.web),base_url=ORIGIN) as c:
        assert (await c.get('/api/rooms')).status_code==401
    async with paired(m) as (c,code):
        assert (await c.post('/pair',json={'code':code,'name':'Second'})).status_code==401
        assert (await c.get('/api/rooms')).status_code==200
        assert (await c.get('/api/connections')).status_code==404
        assert (await c.post('/api/auth/login',json={})).status_code==404
        assert (await c.post('/api/rooms/1/stop',json={},headers={'Origin':'https://evil.invalid'})).status_code==403
        assert (await c.get('/session',headers={'Host':'evil.invalid'})).status_code==403
        assert c.cookies.get(COOKIE) not in m.path.read_text()
        key=next(iter(m.state['devices']));m.revoke(owner,key)
        assert (await c.get('/api/rooms')).status_code==401

@pytest.mark.asyncio
async def test_owner_unbind_switch_and_disabled_user_block(mobile):
    m,epoch,client,owner,_,_,_=mobile
    async with paired(m) as (c,_):
        epoch.binding=None;assert (await c.get('/session')).status_code==401
        epoch.binding=OwnerBinding(owner+1,'cd'*32);assert (await c.get('/api/rooms')).status_code==401
        epoch.binding=OwnerBinding(owner,'ef'*32)
        from app.models.models import User
        with client.app.state.session_factory() as db:db.get(User,owner).is_active=False;db.commit()
        assert (await c.get('/api/rooms')).status_code==401

@pytest.mark.asyncio
async def test_message_idempotency_owner_isolation_and_real_stop(mobile):
    m,_,client,owner,broker,path,proposal=mobile;action=proposal();room=action.room_id
    calls=[]
    async def schedule(room_id,event_id):calls.append(event_id)
    async def stop(room_id):calls.append(('stop',room_id))
    client.app.state.scheduler.schedule_user_event=schedule
    client.app.state.local_executor.stop_room=stop
    with client.app.state.session_factory() as db:
        from app.models.models import Project,User
        other=db.query(User).filter(User.id!=owner).first();project=Project(user_id=other.id,name='foreign');db.add(project);db.flush();foreign=Conversation(project_id=project.id,user_id=other.id,title='not shared');db.add(foreign);db.commit();foreign_id=foreign.id
    async with paired(m) as (c,_):
        key=str(uuid4());url=f'/api/rooms/{room}/messages'
        first=await c.post(url,json={'content':'synthetic mobile message'},headers={'Idempotency-Key':key});assert first.status_code==201,first.text
        replay=await c.post(url,json={'content':'synthetic mobile message'},headers={'Idempotency-Key':key});assert replay.status_code==201 and first.json()['event_id']==replay.json()['event_id']
        with client.app.state.session_factory() as db:assert db.query(RoomEvent).filter_by(event_id=first.json()['event_id']).count()==1
        assert (await c.get(f'/api/rooms/{foreign_id}/events')).status_code==404
        assert (await c.post(f'/api/rooms/{foreign_id}/stop',json={})).status_code==404
        assert (await c.post(f'/api/rooms/{room}/stop',json={})).status_code==204
        assert ('stop',room) in calls

@pytest.mark.asyncio
async def test_real_once_review_rejects_stale_and_broad_then_approves(mobile):
    m,_,client,owner,broker,path,proposal=mobile;action=proposal('write_file',{'path':str(path),'content':'replacement'})
    async with paired(m) as (c,_):
        url=f'/api/rooms/{action.room_id}/actions/{action.action_id}'
        view=await c.get(url+'/preview');assert view.status_code==200,view.text
        review=view.json()['review_id'];assert view.json()['arguments']['content']=='replacement'
        invalid=await c.post(url+'/decision',json={'decision':'approve','review_id':review,'scope':'room_session'});assert invalid.status_code==422
        path.write_text('changed')
        stale=await c.post(url+'/decision',json={'decision':'approve','review_id':review});assert stale.status_code==409,stale.text
        fresh=await c.get(url+'/preview');assert fresh.status_code==200 and fresh.json()['review_id']!=review
        approved=await c.post(url+'/decision',json={'decision':'approve','review_id':fresh.json()['review_id']});assert approved.status_code==200,approved.text
        with client.app.state.session_factory() as db:
            saved=db.get(ToolAction,action.action_id);grant=db.get(RoomGrant,saved.grant_id);assert saved.state=='approved' and grant.scope['kind']=='once'
        assert path.read_text()=='changed' # Approval is not execution.

@pytest.mark.asyncio
async def test_pair_guess_limit_and_expiry(mobile):
    m,_,_,owner,_,_,_=mobile
    m.pairing(owner)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m.web),base_url=ORIGIN,headers={'Origin':ORIGIN,'X-Gamja-Mobile':'1'}) as c:
        for _ in range(5):assert (await c.post('/pair',json={'code':'notvalid','name':'phone'})).status_code==422
        # Well-shaped wrong guesses consume the finite attempt budget.
        m.pending['hash']='0'*64
        for _ in range(5):assert (await c.post('/pair',json={'code':'12345678','name':'phone'})).status_code==401
        assert m.pending['attempts']==5
        m.pairing(owner);m.pending['expires']=0
        assert (await c.post('/pair',json={'code':'12345678','name':'phone'})).status_code==401

@pytest.mark.asyncio
async def test_internal_scope_cannot_be_created_by_header(mobile):
    _,_,client,owner,_,_,_=mobile
    response=client.get('/api/v1/rooms/index',headers={'X-Gamja-Session':client.app.state.local_session_token,'Authorization':'Bearer mobile-bridge','X-Gamja-Mobile-Binding':'fake'})
    assert response.status_code==401
