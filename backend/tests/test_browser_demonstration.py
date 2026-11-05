import asyncio
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app.services.browser_demonstration import Demonstrations, MAX_STEPS
from app.schemas.browser_demonstration import SaveDemonstration

class Authority:
    enabled = True
    def active(self): return self.enabled
    def allows(self, value): return self.enabled and value == ('https','example.test',443)

class Page:
    url = 'https://example.test/path?secret=ignored'
    main_frame = object()
    def is_closed(self): return False

class Profiles:
    def __init__(self):
        self.session_value = SimpleNamespace(page=Page(), room_id=2, profile_lease=123, controller='user', closing=False, authority=Authority())
        self.metadata = {'room_id':2, 'revision':1, 'agent_ids':[5]}
        self.claims = {'b'*32:123}
        class Db:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def get(self, model, key): return SimpleNamespace(is_active=True, user_id=1)
            def commit(self): pass
        self.browser = SimpleNamespace(sessions=Db)
    def read(self, *args): return self.metadata
    def session(self, *args): return ('key', self.session_value)

def item(profiles):
    import time
    return {'id':'a'*32, 'owner_id':1, 'room_id':2, 'agent_id':5, 'profile_id':'b'*32,
            'profile_revision':1, 'lease':123, 'session':profiles.session_value,
            'origin':('https','example.test',443), 'goal':'Review safe sample',
            'deadline':time.monotonic()+600, 'state':'recording', 'steps':[]}

def test_hints_are_valueless_bounded_and_revocation_stops(monkeypatch):
    monkeypatch.setattr(Demonstrations,'persist_recording',lambda *a,**k:None)
    p=Profiles(); service=Demonstrations(p); demo=item(p)
    service.add(demo,'input'); service.add(demo,'arbitrary secret payload')
    assert demo['steps']==['Enter a value (not recorded)']
    assert 'secret' not in str(service.view(demo))
    p.session_value.authority.enabled=False
    service.add(demo,'click')
    assert demo['state']=='interrupted' and len(demo['steps'])==1
    p.session_value.authority.enabled=True
    demo=item(p); demo['steps']=['x']*MAX_STEPS
    service.add(demo,'click')
    assert demo['state']=='interrupted' and len(demo['steps'])==MAX_STEPS

def test_changed_origin_controller_and_revision_interrupt(monkeypatch):
    monkeypatch.setattr(Demonstrations,'persist_recording',lambda *a,**k:None)
    for mutate in (lambda p:setattr(p.session_value.page,'url','https://other.test/'),
                   lambda p:setattr(p.session_value,'controller','agent'),
                   lambda p:p.metadata.update(revision=2)):
        p=Profiles(); service=Demonstrations(p); demo=item(p); mutate(p)
        assert service.view(demo)['state']=='interrupted'

@pytest.mark.asyncio
async def test_stop_and_save_require_reviewed_source_and_no_execution(monkeypatch):
    monkeypatch.setattr(Demonstrations,'persist_recording',lambda *a,**k:None)
    p=Profiles(); service=Demonstrations(p); demo=item(p)
    demo['watcher']=asyncio.create_task(asyncio.sleep(60))
    service.add(demo,'click')
    result=await service.stop(demo)
    assert result['state']=='stopped' and result['source_trust']=='untrusted_hints'
    assert 'does not grant permission' in result['draft']['content']
    captured={}
    def create(db, owner, value, key, source):
        captured.update(owner=owner,key=key,source=source)
        return {'active':False,'source':source}
    monkeypatch.setattr('app.services.owner_skills.create_demonstration',create)
    saved=await service.save(None,demo,SaveDemonstration(name='Safe',description='',content='Reviewed'), 'key')
    assert saved['active'] is False and captured['source']['kind']=='browser_demonstration'
    assert demo['state']=='saved'
    with pytest.raises(HTTPException): await service.stop(demo)

def test_real_inactive_source_idempotency_and_cas():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.core.database import Base
    from app.models.owner_skills import OwnerSkill, OwnerSkillVersion, OwnerSkillBinding, OwnerSkillCommand
    from app.services import owner_skills
    from app.schemas.owner_skills import SkillEdit
    engine=create_engine('sqlite:///:memory:')
    for table in (OwnerSkill.__table__,OwnerSkillVersion.__table__,OwnerSkillBinding.__table__,OwnerSkillCommand.__table__):
        table.create(engine)
    value=SaveDemonstration(name='Reviewed',description='',content='Use safe input.')
    source={'kind':'browser_demonstration','demo_id':'a'*32,'room_id':2,'agent_id':5,'trust':'untrusted_hints'}
    with sessionmaker(bind=engine)() as db:
        result=owner_skills.create_demonstration(db,1,value,'demo-key',source)
        assert result['active'] is False and result['source']==source
        assert owner_skills.create_demonstration(db,1,value,'demo-key',source)==result
        with pytest.raises(HTTPException):
            owner_skills.create_demonstration(db,1,SaveDemonstration(name='Different',description='',content='x'),'demo-key',source)
        edit=SkillEdit(expected_revision=1,name='Reviewed',description='',content='Updated',active=False)
        assert owner_skills.edit(db,1,result['skill_id'],edit)['revision']==2
        with pytest.raises(HTTPException): owner_skills.edit(db,1,result['skill_id'],edit)
        assert db.query(OwnerSkillBinding).count()==0
    engine.dispose()

@pytest.mark.asyncio
async def test_start_recovery_conflict_and_collector_cleanup(monkeypatch):
    from app.schemas.browser_demonstration import StartDemonstration
    import time
    p=Profiles(); p.operations=asyncio.Lock(); session=p.session_value
    session.lock=asyncio.Lock(); page=session.page; page.listeners=[]; page.exposes=0; page.cleaned=0
    async def expose(name, callback): page.exposes+=1; page.callback=callback
    async def evaluate(script, *args):
        if args: page.cleaned+=1
    page.expose_binding=expose; page.evaluate=evaluate
    page.on=lambda event, callback:page.listeners.append(callback)
    page.remove_listener=lambda event, callback:page.listeners.remove(callback)
    monkeypatch.setattr('app.services.room_service.require_room',lambda *a,**k:None)
    monkeypatch.setattr('app.services.owner_skills.agent_owned',lambda *a,**k:None)
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models.browser_demonstration import BrowserDemonstration, BrowserDemonstrationReviewCommand
    engine=create_engine('sqlite:///:memory:')
    BrowserDemonstration.__table__.create(engine);BrowserDemonstrationReviewCommand.__table__.create(engine)
    p.browser.sessions=sessionmaker(bind=engine,expire_on_commit=False)
    service=Demonstrations(p); db=SimpleNamespace(get=lambda *a:SimpleNamespace(is_active=True))
    monkeypatch.setattr(service,'live',lambda item: item['state']=='recording' and session.authority.active())
    value=StartDemonstration(agent_id=5,profile_id='b'*32,profile_revision=1,goal='Sample')
    first=await service.start(db,1,2,value,'recover-key')
    assert (await service.start(db,1,2,value,'recover-key'))['demo_id']==first['demo_id']
    assert service.command(1,2,'recover-key')['demo_id']==first['demo_id']
    with pytest.raises(HTTPException): await service.start(db,1,2,value.model_copy(update={'goal':'Different'}),'recover-key')
    with pytest.raises(HTTPException): service.command(1,3,'recover-key')
    demo=service.owned(first['demo_id'],1,2)
    await service.stop(demo)
    assert not page.listeners and page.cleaned==1 and session._demonstration_item is None
    second=await service.start(db,1,2,value,'next-key')
    assert second['demo_id']!=first['demo_id'] and page.exposes==1
    session.authority.enabled=False
    service.add(service.owned(second['demo_id'],1,2),'click')
    await asyncio.sleep(1.05)
    assert not page.listeners and page.cleaned==2
    demo['finished_at']=time.monotonic()-601
    service.evict()
    assert service.command(1,2,'recover-key')['state']=='stopped'
    assert service.command(1,2,'next-key')['state']=='interrupted'
    engine.dispose()

def test_opt_in_semantic_hints_are_bounded_and_sensitive_values_omitted(monkeypatch):
    monkeypatch.setattr(Demonstrations,'persist_recording',lambda *a,**k:None)
    from app.schemas.browser_demonstration import StartDemonstration
    p=Profiles(); service=Demonstrations(p); demo=item(p)
    value=StartDemonstration(agent_id=5,profile_id='b'*32,profile_revision=1,goal='sample')
    assert value.include_target_labels is False
    service.add(demo,'click_button','Save report')
    assert demo['steps'][-1]=='Click a button'
    demo['include_target_labels']=True
    service.add(demo,'click_button','Save report')
    service.add(demo,'click_link','https://example.test/?token=secret')
    service.add(demo,'click_button','Password')
    service.add(demo,'click_button','x'*129)
    service.add(demo,'input','Unrecorded personal input')
    assert demo['steps'][1:] == ['Click a button: Save report','Click a link','Click a button','Click a button','Enter a value (not recorded): Unrecorded personal input']
    assert service.view(demo)['include_target_labels'] is True

def test_semantic_label_accepts_korean_spaces_and_rejects_sensitive_fragments():
    from app.services.browser_demonstration import safe_label
    assert safe_label('보고서 저장') == '보고서 저장'
    assert safe_label('다음 단계 (검토)') == '다음 단계 (검토)'
    assert safe_label('인증 번호') is None
    assert safe_label('계좌 12345678') is None

def test_draft_submission_requires_explicit_consent_and_bounded_reviewed_data():
    from pydantic import ValidationError
    from app.schemas.browser_demonstration import DraftSubmission
    body={'review_revision':1,'agent_id':5,'reviewed_goal':'보고서 만들기','reviewed_steps':['보고서 저장'],
          'reviewed_rules':'전송은 승인받기','consent':True,'preflight_token':'a'*64}
    assert DraftSubmission(**body).reviewed_steps == ['보고서 저장']
    for change in ({'consent':False}, {'preflight_token':'bad'}, {'reviewed_steps':['x'*501]}, {'reviewed_steps':[]}):
        with pytest.raises(ValidationError): DraftSubmission(**(body|change))

@pytest.mark.asyncio
async def test_draft_routes_bind_owner_epoch_and_recover_without_duplicate_schedule(monkeypatch):
    from app.api.routes import tools
    from app.services import browser_demo_draft
    from app.schemas.browser_demonstration import DraftPreflight, DraftSubmission
    seen=[]
    epoch=object(); registry=object(); scheduled=[]
    async def schedule(room, event): scheduled.append((room,event))
    request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(owner_epoch_provider=epoch,scheduler=SimpleNamespace(schedule_user_event=schedule))))
    monkeypatch.setattr(tools,'_demonstrations',lambda request:registry)
    monkeypatch.setattr(browser_demo_draft,'preflight',lambda *args: seen.append(args) or {'model_id':'exact'})
    monkeypatch.setattr(browser_demo_draft,'submit',lambda *args: seen.append(args) or {'event_id':'e','replayed':True})
    monkeypatch.setattr(browser_demo_draft,'read',lambda *args:{'status':'completed'})
    base={'review_revision':1,'agent_id':5,'reviewed_goal':'목표','reviewed_steps':['보고서 저장'],'reviewed_rules':''}
    value=DraftPreflight(**base); owner=SimpleNamespace(id=1); db=object()
    assert tools.demonstration_draft_preflight(2,'demo',value,request,db,owner)['model_id']=='exact'
    assert seen[-1][-2:]==(registry,epoch)
    submit=DraftSubmission(**base,consent=True,preflight_token='a'*64)
    await tools.demonstration_draft_submit(2,'demo',submit,request,'key',db,owner)
    assert seen[-1][-2:]==(registry,epoch) and scheduled==[]
    monkeypatch.setattr(browser_demo_draft,'read',lambda *args:{'status':'submitted'})
    await tools.demonstration_draft_submit(2,'demo',submit,request,'key',db,owner)
    assert scheduled==[(2,'e')]
    stopped=[]
    monkeypatch.setattr(browser_demo_draft,'stop',lambda *args:stopped.append(args) or {'status':'cancelled'})
    assert tools.demonstration_draft_stop(2,'demo',request,'key',db,owner)['status']=='cancelled'
    assert stopped[0][1:5]==(1,2,'demo','key')


def test_demonstration_follows_only_current_approved_origins(monkeypatch):
    monkeypatch.setattr(Demonstrations,'persist_recording',lambda *a,**k:None)
    p=Profiles(); service=Demonstrations(p); demo=item(p)
    allowed={('https','example.test',443),('https','second.test',443)}
    p.session_value.authority.allows=lambda target: p.session_value.authority.enabled and target in allowed
    service.add(demo,'click')
    p.session_value.page.url='https://second.test/private?ignored=yes'
    service.add(demo,'navigation')
    assert demo['state']=='recording' and len(demo['steps'])==2
    assert all('private' not in step and 'second.test' not in step for step in demo['steps'])
    p.session_value.page.url='https://outside.test/'
    service.add(demo,'click')
    assert demo['state']=='interrupted' and len(demo['steps'])==2
    demo=item(p); p.session_value.page.url='https://second.test/'
    p.metadata['revision']=2
    service.add(demo,'click')
    assert demo['state']=='interrupted' and demo['steps']==[]
    p.metadata['revision']=1; demo=item(p); p.session_value.authority.enabled=False
    service.add(demo,'click')
    assert demo['state']=='interrupted' and demo['steps']==[]
