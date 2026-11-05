from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.api.routes import tools
from app.models.models import User,Project,Conversation,Agent
from app.models.collaboration import RoomMembership
from app.services.tools.browser_profiles import BrowserProfiles


@pytest.fixture
def fixture(tmp_path):
    tmp_path.chmod(0o700)
    engine=create_engine('sqlite:///:memory:')
    tables=[User.__table__,Project.__table__,Conversation.__table__,Agent.__table__,RoomMembership.__table__]
    User.metadata.create_all(engine,tables=tables)
    sessions=sessionmaker(bind=engine)
    browser=SimpleNamespace(sessions=sessions,workflows={},owner_epochs=SimpleNamespace(current=lambda _: 'epoch'))
    store=BrowserProfiles(browser,tmp_path)
    request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(local_executor=SimpleNamespace(browser=SimpleNamespace(profiles=store)))))
    with sessions() as db:
        db.add_all([User(id=i,email=f'{i}@example.invalid',username=f'Owner{i}',hashed_password='synthetic') for i in (1,2)])
        db.add_all([Project(id=1,user_id=1,name='Own'),Project(id=2,user_id=2,name='Other'),Project(id=3,user_id=1,name='Closed',is_active=False)])
        db.flush()
        db.add_all([Conversation(id=r,user_id=o,project_id=p,title=f'Room {r}',is_active=active) for r,o,p,active in
                    [(10,1,1,True),(20,1,1,True),(30,2,2,True),(40,1,1,False),(50,1,3,True),(60,1,1,True)]])
        db.add_all([Agent(id=a,user_id=o,name=f'Bot {a}',description='',system_prompt='',is_active=active) for a,o,active in
                    [(1,1,True),(2,1,True),(3,1,False),(4,2,True),(5,1,True)]])
        db.flush()
        db.add_all([RoomMembership(room_id=r,agent_id=a,is_active=True) for r,a in [(10,1),(20,2),(20,3),(20,4),(60,5)]])
        db.commit()
        main=store.put(db,1,10,SimpleNamespace(name='Main',account_label='',agent_ids=[1],origins=['https://example.test']))
        store.put(db,1,20,SimpleNamespace(name='Other login',account_label='',agent_ids=[2],origins=['https://example.test']))
        yield db,store,request,main
    engine.dispose()


def test_sharing_options_are_owner_active_room_member_scoped_and_mark_conflicts(fixture):
    db,store,request,main=fixture
    data=tools.browser_profile_sharing(10,main['id'],request,db,SimpleNamespace(id=1))
    assert [r['room_id'] for r in data['rooms']]==[20,60]
    assert data['rooms'][0]['agents']==[{'id':2,'name':'Bot 2','available':False}]
    assert data['rooms'][1]['agents']==[{'id':5,'name':'Bot 5','available':True}]
    assert data['bindings']==[] and data['home_room_id']==10 and data['allow_delegated'] is False
    with pytest.raises(HTTPException):tools.browser_profile_sharing(10,main['id'],request,db,SimpleNamespace(id=2))
    store.put(db,1,60,SimpleNamespace(name='Main',account_label='',agent_ids=[],origins=['https://example.test']))
    changed=tools.browser_profile_sharing(10,main['id'],request,db,SimpleNamespace(id=1))
    assert changed['rooms'][1]['agents'][0]['available'] is False


@pytest.mark.asyncio
async def test_sharing_route_connects_target_bot_without_changing_home_and_old_revision_fails(fixture):
    db,store,request,main=fixture
    value=tools.BrowserProfileSharing(revision=main['revision'],bindings=[{'room_id':60,'agent_ids':[5]}],allow_delegated=True)
    result=await tools.update_browser_profile_sharing(10,main['id'],value,request,db,SimpleNamespace(id=1))
    assert result['home_room_id']==10 and result['shared_room_ids']==[60]
    assert result['allow_delegated'] is True
    assert tools.browser_profile_sharing(10,main['id'],request,db,SimpleNamespace(id=1))['allow_delegated'] is True
    assert store.binding(60,5)['id']==main['id'] and store.read(main['id'],1,60)['room_id']==10
    view=tools.browser_profiles(60,request,db,SimpleNamespace(id=1))['items'][0]
    assert view['agent_ids']==[5] and view['home_room_id']==10
    with pytest.raises(HTTPException):await tools.update_browser_profile_sharing(10,main['id'],value,request,db,SimpleNamespace(id=1))
    with pytest.raises(HTTPException):tools.browser_profile_sharing(60,main['id'],request,db,SimpleNamespace(id=1))
    from app.services.browser_demo_draft import _demo
    item={'profile_id':main['id'],'profile_revision':result['revision'],'agent_id':5,'state':'stopped'}
    registry=SimpleNamespace(profiles=store,owned=lambda *args:item)
    assert _demo(registry,1,60,'synthetic-demo',5) is item
