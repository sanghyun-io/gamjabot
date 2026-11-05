from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app.services.tools.browser_profiles import BrowserProfiles, ProfileAuthority, assigned_agents, has_room

class Db:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def get(self, model, key): return SimpleNamespace(user_id=1)

@pytest.fixture
def store(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    browser=SimpleNamespace(sessions=Db,workflows={},owner_epochs=SimpleNamespace(current=lambda owner:'epoch'))
    result=BrowserProfiles(browser,tmp_path)
    monkeypatch.setattr('app.services.room_service.require_room',lambda db,room,owner: None if owner==1 and room in (10,20,30) else (_ for _ in ()).throw(HTTPException(404)))
    def validate(db,owner,room,agents,origins):
        if owner!=1 or room not in (10,20,30) or any(a not in {10:{1},20:{2,3},30:{4}}[room] for a in agents): raise HTTPException(404)
        return ([['https','example.test',443]],['https://example.test'])
    monkeypatch.setattr(result,'validate',validate)
    return result

def make(store,name='Main',agents=(1,)):
    value=SimpleNamespace(name=name,account_label='',agent_ids=list(agents),origins=['https://example.test'])
    return store.put(Db(),1,10,value)

def test_share_binding_owner_scope_revoke_and_authority(store):
    profile=make(store);pid=profile['id'];raw=store.read(pid,1,10)
    result=store.share(Db(),1,10,pid,raw['revision'],[{'room_id':20,'agent_ids':[2]}])
    shared=store.read(pid,1,20)
    assert shared['room_id']==10 and shared['agent_ids']==[1]
    assert assigned_agents(shared,20)==[2] and has_room(shared,20)
    assert store.binding(20,2)['id']==pid and store.binding(20,1) is None
    assert store.status(shared,20)['agent_ids']==[2] and result['shared_room_ids']==[20]
    with pytest.raises(HTTPException):store.read(pid,2,20)
    base=SimpleNamespace(active=lambda:True,allows=lambda t:True,_live=lambda g:True)
    authority=ProfileAuthority(base,shared,store,20,2)
    assert authority.active() and authority.allows(('https','example.test',443)) and authority._live(None)
    store.share(Db(),1,10,pid,shared['revision'],[])
    assert not authority.active() and not authority.allows(('https','example.test',443)) and not authority._live(None)
    with pytest.raises(HTTPException):store.read(pid,1,20)

def test_busy_conflicts_and_cross_owner_fail_closed(store):
    profile=make(store);pid=profile['id'];m=store.read(pid,1,10)
    lease=store.claim(m,launch=False)
    try:
        with pytest.raises(HTTPException):store.share(Db(),1,10,pid,m['revision'],[{'room_id':20,'agent_ids':[2]}])
    finally:store.release(pid,lease)
    with pytest.raises(HTTPException):store.share(Db(),2,10,pid,m['revision'],[{'room_id':20,'agent_ids':[2]}])
    for bindings in ([{'room_id':10,'agent_ids':[1]}],[{'room_id':20,'agent_ids':[2]},{'room_id':20,'agent_ids':[3]}],[{'room_id':20,'agent_ids':[999]}]):
        with pytest.raises(HTTPException):store.share(Db(),1,10,pid,m['revision'],bindings)
    store.share(Db(),1,10,pid,m['revision'],[{'room_id':20,'agent_ids':[2]}])
    with pytest.raises(HTTPException):store.share(Db(),1,10,pid,m['revision'],[])

def test_cross_profile_conflicts_and_shared_room_not_home(store):
    profile=make(store);other=make(store,'Other',())
    store.share(Db(),1,10,other['id'],other['revision'],[{'room_id':20,'agent_ids':[2]}])
    with pytest.raises(HTTPException):store.share(Db(),1,10,profile['id'],profile['revision'],[{'room_id':20,'agent_ids':[2]}])
    with pytest.raises(HTTPException):store.share(Db(),1,20,other['id'],other['revision']+1,[])
    edit=SimpleNamespace(name='Other',account_label='',agent_ids=[2],origins=['https://example.test'],revision=other['revision']+1)
    with pytest.raises(HTTPException):store.put(Db(),1,20,edit,other['id'])


def test_owner_authority_uses_execution_room_and_revision(store):
    from app.services.tools.browser_profiles import UserAuthority
    profile=make(store);pid=profile['id']
    store.share(Db(),1,10,pid,profile['revision'],[{'room_id':20,'agent_ids':[2]}])
    m=store.read(pid,1,20)
    class StateDb:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,model,key):return SimpleNamespace(id=1,is_active=True,user_id=1,project_id=3)
    store.browser.sessions=StateDb
    authority=UserAuthority(store,m,'epoch',room_id=20)
    assert authority.active() and authority.allows(('https','example.test',443))
    store.share(Db(),1,10,pid,m['revision'],[])
    assert not authority.active() and not authority.allows(('https','example.test',443))

def test_demonstration_rejects_other_room_session(monkeypatch):
    import time
    from app.services.browser_demonstration import Demonstrations
    profile={'id':'p','owner_id':1,'room_id':10,'revision':2,'agent_ids':[1],
             'shared_rooms':{'20':[2]},'origins':[['https','example.test',443]]}
    class StateDb:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,*args):return SimpleNamespace(id=1,is_active=True,user_id=1)
    page=SimpleNamespace(url='https://example.test/',is_closed=lambda:False)
    session=SimpleNamespace(room_id=10,profile_lease=42,controller='user',closing=False,page=page,
        authority=SimpleNamespace(active=lambda:True,allows=lambda target:True))
    profiles=SimpleNamespace(browser=SimpleNamespace(sessions=StateDb),read=lambda *a:profile,
        session=lambda *a:('key',session),claims={'p':42})
    service=Demonstrations(profiles);monkeypatch.setattr(service,'persist_recording',lambda *a,**k:None)
    item={'id':'d','profile_id':'p','profile_revision':2,'owner_id':1,'room_id':20,'agent_id':2,
          'session':session,'lease':42,'state':'recording','deadline':time.monotonic()+60,'steps':[]}
    service.add(item,'click')
    assert item['state']=='interrupted' and item['steps']==[]
