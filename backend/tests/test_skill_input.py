"""Synthetic input binding and reference projection; no provider or user data."""
from datetime import datetime, timezone
from uuid import uuid4
from unittest.mock import patch
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.database import Base
import app.main  # register models
from app.models.models import User,Agent
from app.models.owner_skills import OwnerSkill,OwnerSkillBinding
from app.schemas.skill_input import SkillInputPreview
from app.services import skill_input

@pytest.fixture
def data():
    engine=create_engine('sqlite:///:memory:');Base.metadata.create_all(engine);db=sessionmaker(bind=engine)()
    owner=User(email='skill@example.invalid',username='owner',hashed_password='x');other=User(email='other@example.invalid',username='other',hashed_password='x');db.add_all([owner,other]);db.flush()
    agent=Agent(user_id=owner.id,name='bot',description='d',system_prompt='p');db.add(agent);db.flush()
    sid=str(uuid4());now=datetime.now(timezone.utc)
    skill=OwnerSkill(skill_id=sid,owner_id=owner.id,revision=1,name='s',description='d',content='Hello {{name}} and {{name}}',active=True,created_at=now,updated_at=now)
    binding=OwnerSkillBinding(owner_id=owner.id,agent_id=agent.id,skill_id=sid,enabled=True,revision=2)
    db.add_all([skill,binding]);db.commit()
    value=SkillInputPreview(bot_id=agent.id,skill_id=sid,skill_revision=1,binding_revision=2,question='Do this',values={'name':'{{other}}'})
    with patch('app.services.library_file_questions._snapshot',return_value=({'owner_id':owner.id,'room_id':3,'bot_id':agent.id,'owner_epoch_sha256':'f'*64},{})):
        yield db,owner,other,skill,binding,value
    db.close();engine.dispose()

def test_literal_substitution_and_reference_only_projection(data):
    db,owner,other,skill,binding,value=data
    result=skill_input.prepare(db,owner.id,3,value,None)
    assert result['variables']==['name'] and result['expanded_reference']=='Hello {{other}} and {{other}}'
    original={'owner_skills':[{'skill_id':skill.skill_id,'content':skill.content,'authority':'reference_only'}, {'skill_id':'other','content':'unchanged'}]}
    projected=skill_input.apply_reference(original,{'skill_id':skill.skill_id},result)
    assert projected['owner_skills'][0]['content']==result['expanded_reference']
    assert projected['owner_skills'][0]['authority']=='reference_only' and projected['owner_skills'][1]['content']=='unchanged'
    assert original['owner_skills'][0]['content']==skill.content

def test_other_owner_stale_and_disabled_binding_rejected(data):
    db,owner,other,skill,binding,value=data
    with pytest.raises(HTTPException):skill_input.prepare(db,other.id,3,value,None)
    skill.revision+=1;db.commit()
    with pytest.raises(HTTPException):skill_input.prepare(db,owner.id,3,value,None)
    skill.revision=1;binding.enabled=False;db.commit()
    with pytest.raises(HTTPException):skill_input.prepare(db,owner.id,3,value,None)
    binding.enabled=True;skill.active=False;db.commit()
    with pytest.raises(HTTPException):skill_input.prepare(db,owner.id,3,value,None)

def test_missing_extra_inputs_and_duplicate_projection_fail_closed(data):
    db,owner,other,skill,binding,value=data
    for values in ({},{'name':'x','extra':'y'}):
        with pytest.raises(HTTPException):skill_input.prepare(db,owner.id,3,value.model_copy(update={'values':values}),None)
    with pytest.raises(ValueError):skill_input.apply_reference({'owner_skills':[{'skill_id':skill.skill_id},{'skill_id':skill.skill_id}]},{'skill_id':skill.skill_id},{'expanded_reference':'x'})

from test_library_file_questions import setup_question, headers, results

def test_preview_send_receipt_and_stale_proof(setup_question):
    client,ids,_,_,called,_=setup_question
    with client.app.state.session_factory() as db:
        now=datetime.now(timezone.utc);sid=str(uuid4())
        db.add(OwnerSkill(skill_id=sid,owner_id=ids['owner'],revision=1,name='synthetic',description='d',content='Summarize {{topic}}',active=True,created_at=now,updated_at=now))
        db.add(OwnerSkillBinding(owner_id=ids['owner'],agent_id=ids['author'],skill_id=sid,enabled=True,revision=1));db.commit()
    body={'bot_id':ids['author'],'skill_id':sid,'skill_revision':1,'binding_revision':1,'question':'Please summarize','values':{'topic':'{{not_recursive}}'}}
    path=f"/api/v1/conversations/{ids['room']}/skill-use"
    preview=client.post(path+'/preview',headers=headers(client,ids),json=body)
    assert preview.status_code==200,preview.text
    result=preview.json();assert result['expanded_reference']=='Summarize {{not_recursive}}'
    assert result['target']['bot_id']==ids['author'] and result['target']['model_id']=='model-a'
    import hashlib,json
    digest=lambda obj: hashlib.sha256(json.dumps(obj,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    proof=result['proof']
    assert digest(proof)==result['basis'] and proof['target_sha256']==digest(result['target'])
    assert proof['owner_id']==ids['owner'] and proof['room_id']==ids['room'] and proof['bot_id']==ids['author']
    assert proof['question_sha256']==hashlib.sha256(body['question'].encode()).hexdigest()
    assert proof['values_sha256']==digest(body['values'])
    assert proof['expanded_sha256']==hashlib.sha256(result['expanded_reference'].encode()).hexdigest()
    assert 'synthetic-only' not in json.dumps(proof) and '{{not_recursive}}' not in json.dumps(proof)
    payload={**body,'basis':result['basis'],'consent':True}
    sent=client.post(path,headers=headers(client,ids,'one'),json=payload);assert sent.status_code==201,sent.text
    event=sent.json();assert event['payload']['content']==body['question'] and event['payload']['skill_context']['values']==body['values']
    assert client.get(path+'/receipt',headers=headers(client,ids,'one')).json()['event_id']==event['event_id']
    assert event['payload']['skill_context']['proof']==proof
    assert client.post(path,headers=headers(client,ids,'one'),json=payload).status_code==200 and len(called)==1
    assert client.post(path,headers=headers(client,ids,'one'),json={**payload,'values':{'topic':'changed'}}).status_code==409
    assert client.get(path+'/receipt',headers=headers(client,ids,'one',owner=ids['other'])).status_code==404
    with client.app.state.session_factory() as db:
        skill=db.get(OwnerSkill,sid);skill.revision=2;db.commit()
    assert client.post(path,headers=headers(client,ids,'two'),json=payload).status_code==409
    assert len(called)==1
    with client.app.state.session_factory() as db:
        from app.models.collaboration import RoomEvent
        row=db.get(RoomEvent,event['event_id']);ctx=dict(row.payload['skill_context'])
        ctx['proof']={**ctx['proof'],'model_id':'tampered'};row.payload={**row.payload,'skill_context':ctx};db.commit()
    assert client.get(path+'/receipt',headers=headers(client,ids,'one')).status_code==404
    assert client.post(path,headers=headers(client,ids,'one'),json=payload).status_code==409


@pytest.mark.parametrize(('template','values','expected'), [
    ('Analyze {{topic}}', {'topic':'synthetic-input-unique'}, 'Analyze synthetic-input-unique'),
    ('Analyze this without variables', {}, 'Analyze this without variables'),
])
def test_reviewed_input_reaches_exact_bot_only_as_reference(setup_question,monkeypatch,template,values,expected):
    from app.models.collaboration import ProviderConnection,RoomScheduleSettings,RoomEvent
    from app.services.connections import ConnectionConfig
    from app.services.providers import ProviderResult
    from app.services.providers.base import attributed_messages
    client,ids,_,_,_,_=setup_question
    sid=str(uuid4());now=datetime.now(timezone.utc)
    with client.app.state.session_factory() as db:
        db.add(OwnerSkill(skill_id=sid,owner_id=ids['owner'],revision=1,name='synthetic',description='d',content=template,active=True,created_at=now,updated_at=now))
        db.add(OwnerSkillBinding(owner_id=ids['owner'],agent_id=ids['author'],skill_id=sid,enabled=True,revision=1))
        settings=db.get(RoomScheduleSettings,ids['room'])
        if settings is None: settings=RoomScheduleSettings(room_id=ids['room']);db.add(settings)
        settings.max_rounds=1;settings.max_tool_calls=0;settings.max_executor_seconds=5;db.commit()
    async def connection(db,connection_id,owner_id,*args,**kwargs):
        row=db.get(ProviderConnection,connection_id)
        return ConnectionConfig(row.id,row.kind,row.default_model_id,row.base_url,'synthetic-only',row.capabilities,provider_id=row.provider_id,protocol_id=row.protocol_id,auth_method_id=row.auth_method_id,config_revision=row.readiness_revision,auth_grant_revision=row.auth_grant_revision)
    monkeypatch.setattr('app.services.context.resolve_connection_async',connection)
    scheduler=client.app.state.scheduler;monkeypatch.setattr(scheduler.broker,'owner_epochs',client.app.state.owner_epoch_provider)
    calls=[]
    async def transport(request):calls.append((request.agent_id,attributed_messages(request)));return ProviderResult(text='Synthetic response')
    scheduler.provider=transport
    body={'bot_id':ids['author'],'skill_id':sid,'skill_revision':1,'binding_revision':1,'question':'What happened?','values':values}
    path=f"/api/v1/conversations/{ids['room']}/skill-use"
    preview=client.post(path+'/preview',headers=headers(client,ids),json=body);assert preview.status_code==200,preview.text
    sent=client.post(path,headers=headers(client,ids,'model-synthetic'),json={**body,'basis':preview.json()['basis'],'consent':True});assert sent.status_code==201,sent.text
    original=type(scheduler).schedule_user_event.__get__(scheduler,type(scheduler))
    client.portal.call(lambda:original(ids['room'],sent.json()['event_id'],wait=True))
    with client.app.state.session_factory() as db:
        failures=[x.payload for x in db.query(RoomEvent).filter_by(room_id=ids['room'],kind='failure').all()]
    assert len(calls)==1,failures
    bot,messages=calls[0];assert bot==ids['author']
    rendered=str(messages);assert rendered.count(expected)==1 and '{{topic}}' not in rendered
    assert 'untrusted reference data' in rendered and 'What happened?' in rendered


def test_changed_skill_after_commit_never_dispatches(setup_question):
    from app.models.collaboration import RoomEvent
    client,ids,_,_,_,_=setup_question;sid=str(uuid4());now=datetime.now(timezone.utc)
    with client.app.state.session_factory() as db:
        db.add(OwnerSkill(skill_id=sid,owner_id=ids['owner'],revision=1,name='synthetic',description='d',content='Analyze {{topic}}',active=True,created_at=now,updated_at=now))
        db.add(OwnerSkillBinding(owner_id=ids['owner'],agent_id=ids['author'],skill_id=sid,enabled=True,revision=1));db.commit()
    body={'bot_id':ids['author'],'skill_id':sid,'skill_revision':1,'binding_revision':1,'question':'What happened?','values':{'topic':'value'}}
    path=f"/api/v1/conversations/{ids['room']}/skill-use"
    preview=client.post(path+'/preview',headers=headers(client,ids),json=body);assert preview.status_code==200
    sent=client.post(path,headers=headers(client,ids,'stale-dispatch'),json={**body,'basis':preview.json()['basis'],'consent':True});assert sent.status_code==201
    with client.app.state.session_factory() as db:
        skill=db.get(OwnerSkill,sid);skill.revision=2;db.commit()
        root=db.get(RoomEvent,sent.json()['event_id'])
        with pytest.raises(HTTPException):
            skill_input.current_for_root(db,ids['owner'],ids['room'],ids['author'],root,client.app.state.owner_epoch_provider)


def test_orphan_skill_event_interrupts_without_blocking_other_requests(setup_question):
    from app.models.collaboration import RoomEvent,RoomTrigger
    from app.services.events import ActorRef,append_event
    client,ids,_,_,_,_=setup_question;scheduler=client.app.state.scheduler
    with client.app.state.session_factory() as db:
        orphan=append_event(db,ids['room'],ActorRef('user',ids['owner']),'message',{'content':'Review','skill_context':{'version':1}})
        ordinary=append_event(db,ids['room'],ActorRef('user',ids['owner']),'message',{'content':'Another request'})
        scheduler._interrupt_incomplete_skill_trigger(db,orphan)
        marker=db.get(RoomTrigger,orphan.event_id);assert marker.state=='interrupted'
        failures=db.query(RoomEvent).filter_by(room_id=ids['room'],actor_type='system',kind='failure').all()
        assert any(x.payload.get('code')=='skill_input_interrupted' for x in failures)
        assert scheduler._create_trigger(db,ordinary).state=='pending'


def test_preview_target_label_change_requires_review(setup_question):
    from app.models.collaboration import ProviderConnection
    client,ids,_,_,called,_=setup_question;sid=str(uuid4());now=datetime.now(timezone.utc)
    with client.app.state.session_factory() as db:
        db.add(OwnerSkill(skill_id=sid,owner_id=ids['owner'],revision=1,name='s',description='d',content='No variables',active=True,created_at=now,updated_at=now))
        db.add(OwnerSkillBinding(owner_id=ids['owner'],agent_id=ids['author'],skill_id=sid,enabled=True,revision=1));db.commit()
    body={'bot_id':ids['author'],'skill_id':sid,'skill_revision':1,'binding_revision':1,'question':'Do this','values':{}}
    path=f"/api/v1/conversations/{ids['room']}/skill-use"
    preview=client.post(path+'/preview',headers=headers(client,ids),json=body);assert preview.status_code==200
    assert preview.json()['variables']==[] and preview.json()['target']['connection_name']=='Example connection'
    with client.app.state.session_factory() as db:
        agent=db.get(Agent,ids['author']);conn=db.get(ProviderConnection,agent.connection_id);conn.name='Renamed';db.commit()
    response=client.post(path,headers=headers(client,ids,'label-changed'),json={**body,'basis':preview.json()['basis'],'consent':True})
    assert response.status_code==409 and response.json()['detail']['code']=='skill_input_changed' and not called
