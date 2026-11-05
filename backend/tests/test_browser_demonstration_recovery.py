from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models.browser_demonstration import BrowserDemonstration, BrowserDemonstrationReviewCommand
from app.services.browser_demonstration import Demonstrations
from app.schemas.browser_demonstration import DemonstrationReview

@pytest.fixture
def fixture():
    engine=create_engine('sqlite:///:memory:')
    BrowserDemonstration.__table__.create(engine);BrowserDemonstrationReviewCommand.__table__.create(engine)
    sessions=sessionmaker(bind=engine,expire_on_commit=False)
    profile=SimpleNamespace(browser=SimpleNamespace(sessions=sessions),claims={},session=lambda _:None,
        read=lambda *args:{'id':'b'*32},claim=lambda *a,**k:123,release=lambda *a:None)
    service=Demonstrations(profile)
    with sessions() as db:
        db.add(BrowserDemonstration(demo_id='a'*32,owner_id=1,room_id=2,agent_id=5,profile_id='b'*32,profile_revision=1,
            state='stopped',goal='Original',steps=['Click button'],include_target_labels=False,
            review={'goal':'Original','steps':['Click button'],'rules':'','fields':None},review_revision=1,
            command_key='start-key',command_digest='c'*64,instance_id='d'*32));db.commit()
    yield service,sessions
    engine.dispose()

def test_recovery_review_cas_idempotency_and_owner_isolation(fixture):
    service,sessions=fixture;item=service.owned('a'*32,1,2)
    assert service.view(item)['steps']==['Click button']
    with pytest.raises(HTTPException):service.owned('a'*32,9,2)
    with sessions() as db:
        value=DemonstrationReview(expected_revision=1,goal='Edited',steps=[''],rules='Review first',fields=None)
        result=service.update_review(db,item,value,'review-key')
        assert result['review_revision']==2 and result['review']['steps']==['']
        assert service.update_review(db,item,value,'review-key')==result
        with pytest.raises(HTTPException):service.update_review(db,item,value.model_copy(update={'rules':'changed'}),'review-key')
        with pytest.raises(HTTPException):service.update_review(db,item,value,'another-key')
        row=db.get(BrowserDemonstration,'a'*32);assert row.goal=='Original' and row.steps==['Click button']
    assert service.command(1,2,'start-key')['review_revision']==2
    assert len(service.list(1,2)['items'])==1

def test_restart_interrupts_only_after_profile_lease_is_confirmed(fixture):
    service,sessions=fixture
    with sessions() as db:
        row=db.get(BrowserDemonstration,'a'*32);row.state='recording';db.commit()
    service.profiles.claims['b'*32]=99
    with pytest.raises(HTTPException):service.owned('a'*32,1,2)
    with sessions() as db:assert db.get(BrowserDemonstration,'a'*32).state=='recording'
    service.profiles.claims.clear()
    item=service.owned('a'*32,1,2)
    assert item['state']=='interrupted' and item['review']['steps']==['Click button']
    with sessions() as db:assert db.get(BrowserDemonstration,'a'*32).state=='interrupted'

@pytest.mark.asyncio
async def test_start_stop_survives_new_registry_without_restarting_collector(fixture,monkeypatch):
    import asyncio
    from app.schemas.browser_demonstration import StartDemonstration
    service,sessions=fixture; p=service.profiles
    class Page:
        url='https://example.test/';main_frame=object()
        def is_closed(self): return False
        async def expose_binding(self,*a): pass
        def on(self,*a): pass
        def remove_listener(self,*a): pass
        async def evaluate(self,*a): pass
    session=SimpleNamespace(page=Page(),room_id=2,lock=asyncio.Lock(),profile_lease=44)
    p.operations=asyncio.Lock();p.session=lambda _:('key',session)
    p.read=lambda *args:{'id':'b'*32,'room_id':2,'revision':1,'agent_ids':[5]}
    monkeypatch.setattr(service,'live',lambda item: item['state']=='recording')
    monkeypatch.setattr('app.services.room_service.require_room',lambda *a,**k:None)
    monkeypatch.setattr('app.services.owner_skills.agent_owned',lambda *a,**k:None)
    fake_db=SimpleNamespace(get=lambda *a:SimpleNamespace(is_active=True))
    value=StartDemonstration(agent_id=5,profile_id='b'*32,profile_revision=1,goal='New')
    started=await service.start(fake_db,1,2,value,'second-key')
    item=service.owned(started['demo_id'],1,2);service.add(item,'click_button','ignored')
    await service.stop(item)
    p.session=lambda _:None
    resumed=Demonstrations(p)
    recovered=resumed.command(1,2,'second-key')
    assert recovered['state']=='stopped' and recovered['steps']==['Click a button']
    assert recovered['review']['steps']==['Click a button'] and not resumed.items

def test_migration_44_creates_required_tables_without_runtime_data():
    import importlib, sqlite3
    migration=importlib.import_module('app.core.migrations.0044_browser_demonstrations')
    db=sqlite3.connect(':memory:');migration.upgrade(db)
    for name, columns in migration.REQUIRED_COLUMNS.items():
        actual={row[1] for row in db.execute(f'PRAGMA table_info({name})')}
        assert columns <= actual
    db.close()

def test_lease_race_preserves_already_stopped_and_edited_review(fixture):
    service,sessions=fixture
    with sessions() as db:
        row=db.get(BrowserDemonstration,'a'*32);row.state='recording';db.commit()
    def claim(*args,**kwargs):
        with sessions() as db:
            row=db.get(BrowserDemonstration,'a'*32)
            row.state='stopped';row.steps=['Newest capture'];row.review={'goal':'Edited','steps':['Reviewed'],'rules':'keep','fields':None};row.review_revision=4
            db.commit()
        return 123
    service.profiles.claim=claim
    item=service.owned('a'*32,1,2)
    assert item['state']=='stopped' and item['steps']==['Newest capture']
    assert item['review']['goal']=='Edited' and item['review_revision']==4
    with sessions() as db:
        row=db.get(BrowserDemonstration,'a'*32)
        assert row.state=='stopped' and row.review['rules']=='keep'

def test_keyset_pagination_and_unknown_lease_do_not_hide_other_records(fixture):
    service,sessions=fixture
    with sessions() as db:
        for index in range(205):
            db.add(BrowserDemonstration(demo_id=f'{index:032x}',owner_id=1,room_id=2,agent_id=5,profile_id='b'*32,profile_revision=1,
                state='recording' if index==3 else 'stopped',goal='G',steps=[],include_target_labels=False,
                review={'goal':'G','steps':[],'rules':'','fields':None},review_revision=1,
                command_key=f'k{index}',command_digest='c'*64,instance_id='d'*32))
        db.commit()
    service.profiles.claims['b'*32]=99
    first=service.list(1,2)
    assert len(first['items'])==199 and first['unavailable_count']==1 and first['next_after_demo_id']
    second=service.list(1,2,first['next_after_demo_id'])
    assert len(second['items'])==6 and second['next_after_demo_id'] is None
    assert len({x['demo_id'] for x in first['items']+second['items']})==205
    assert service.list(9,2)['items']==[]
    with pytest.raises(HTTPException):service.list(1,2,'bad')
