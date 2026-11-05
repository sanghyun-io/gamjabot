import asyncio
import time
from types import SimpleNamespace
from uuid import uuid4
import pytest
from app.services.action_review import ReviewChanged
from app.services.resource_binding import freeze_binding
from app.services.review_runtime import observe_runtime_target
from app.services.tools.browser import BrowserExecutor
from app.services.tools.browser_profiles import BrowserProfiles


def proof(owner=1):
    root=str(uuid4());return {'version':1,'root_scope_id':root,'scope_id':root,'owner_id':owner,'root_room_id':10,
        'root_request_event_id':str(uuid4()),'execution_room_id':20,'agent_id':2,'run_generation':1,
        'job_id':str(uuid4()),'phase':'response','context_mode':'delegated','deadline_at':'2099-01-01T00:00:00Z',
        'lineage':[{'scope_id':root,'epoch':0}]}

def action(binding):
    return SimpleNamespace(room_id=20,agent_id=2,session_id=str(uuid4()),workflow_id='workflow',
        family='browser_navigate',action_id=str(uuid4()),arguments={'url':'https://example.test/'},execution_binding=binding)

@pytest.fixture
def setup():
    browser=BrowserExecutor(); profiles=browser.profiles
    metadata={'id':'a'*32,'owner_id':1,'room_id':10,'revision':3,'agent_ids':[],
              'shared_rooms':{'20':[2]},'allow_delegated':False,'name':'Example','account_label':'',
              'origins':[['https','example.test',443]]}
    profiles.binding=lambda room,agent:metadata if (room,agent)==(20,2) else None
    return browser,metadata

def test_default_temporary_explicit_optin_owner_proof_and_arguments(setup):
    browser,m=setup;a=action(proof());assert browser.profiles.selected(a) is None
    m['allow_delegated']=True;assert browser.profiles.selected(a)['id']==m['id']
    with pytest.raises(ReviewChanged):browser.profiles.selected(action(proof(owner=9)))
    a.arguments['profile_id']='a'*32
    with pytest.raises(ReviewChanged):browser.profiles.selected(a)

def test_review_records_profile_and_busy_or_mismatched_context_unavailable(setup):
    browser,m=setup;a=action(proof());executor=SimpleNamespace(browser=browser)
    target=observe_runtime_target(a,None,executor);assert target.profile=='temporary_workflow' and target.status=='absent'
    m['allow_delegated']=True
    target=observe_runtime_target(a,None,executor)
    assert target.profile=='persistent_room' and target.profile_id==m['id'] and target.profile_revision==3 and target.status=='absent'
    browser.profiles.claims[m['id']]=123
    assert observe_runtime_target(a,None,executor).status=='unavailable'
    browser.profiles.claims.clear()
    key=(a.room_id,a.agent_id,a.workflow_id)
    session=SimpleNamespace(room_id=20,agent_id=2,workflow_id='workflow',profile_id=None,execution_binding=freeze_binding(a.execution_binding),
        controller='agent',closing=False,deadline=time.monotonic()+60,page=SimpleNamespace(is_closed=lambda:False),
        authority=SimpleNamespace(active=lambda:True),context_id='c'*32)
    browser.workflows[key]=session
    assert observe_runtime_target(a,None,executor).status=='unavailable'
    session.profile_id=m['id'];assert observe_runtime_target(a,None,executor).status=='verified'
    session.closing=True;browser.profiles.claims[m['id']]=123
    assert observe_runtime_target(a,None,executor).status=='unavailable'
    session.closing=False;browser.profiles.claims.clear()
    session.execution_binding=freeze_binding(proof());assert observe_runtime_target(a,None,executor).status=='unavailable'

@pytest.mark.asyncio
async def test_old_temporary_review_never_upgrades_to_optin_profile(setup,monkeypatch):
    browser,m=setup;m['allow_delegated']=True;a=action(proof());binding=freeze_binding(a.execution_binding)
    monkeypatch.setattr('app.services.resource_authority.require_scope_start',lambda *args:binding)
    old=SimpleNamespace(profile='temporary_workflow',profile_id=None,profile_revision=None)
    grant=SimpleNamespace(review=SimpleNamespace(targets=[old]),scope='once')
    result=await browser(a,grant,asyncio.Event())
    assert result.state=='failed' and result.error_code=='review_changed' and not browser.workflows

@pytest.mark.asyncio
async def test_reviewed_old_profile_revision_never_executes(setup,monkeypatch):
    browser,m=setup;m['allow_delegated']=True;a=action(proof());binding=freeze_binding(a.execution_binding)
    monkeypatch.setattr('app.services.resource_authority.require_scope_start',lambda *args:binding)
    target=SimpleNamespace(profile='persistent_room',profile_id=m['id'],profile_revision=2)
    grant=SimpleNamespace(review=SimpleNamespace(targets=[target]),scope='once')
    result=await browser(a,grant,asyncio.Event())
    assert result.state=='failed' and result.error_code=='review_changed' and not browser.workflows

from test_browser_profile_sharing import store, make

def test_optin_change_uses_global_revision_and_requires_idle_lease(store):
    profile=make(store);pid=profile['id'];revision=profile['revision']
    first=store.share(None,1,10,pid,revision,[{'room_id':20,'agent_ids':[2]}],allow_delegated=True)
    assert first['allow_delegated'] is True and first['revision']==revision+1
    raw=store.read(pid,1,10);lease=store.claim(raw,launch=False)
    try:
        with pytest.raises(Exception):store.share(None,1,10,pid,raw['revision'],[],allow_delegated=False)
    finally:store.release(pid,lease)
    second=store.share(None,1,10,pid,raw['revision'],[],allow_delegated=False)
    assert second['allow_delegated'] is False and second['revision']==revision+2

@pytest.mark.asyncio
async def test_scoped_manual_browser_is_rejected_before_launch(setup):
    browser,m=setup;a=action(proof())
    with pytest.raises(ReviewChanged):await browser._create(a,None,('https','example.test',443),manual=m,manual_epoch='epoch')
    assert not browser.workflows
