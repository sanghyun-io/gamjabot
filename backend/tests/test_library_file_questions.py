"""Synthetic in-process file question tests; no provider or real user data."""
import hashlib
import json
import sys
from types import SimpleNamespace
from uuid import uuid4
import pytest
from app.models.models import Agent
from app.models.collaboration import ProviderConnection,RoomMembership,RoomAgentRun,RoomEvent
from app.services.library_uploads import LibraryUploads
from test_room_results import results,headers

@pytest.fixture
def setup_question(results,tmp_path,monkeypatch):
    client,ids=results;tmp_path.chmod(0o700);store=LibraryUploads(tmp_path)
    client.app.state.local_executor.library_uploads=store
    client.app.state.owner_epoch_provider=SimpleNamespace(snapshot=lambda:object(),current=lambda owner:'synthetic-epoch' if owner==ids['owner'] else None)
    excerpt='UNTRUSTED FILE TEXT SYNTHETIC'
    class ExtractionError(Exception):pass
    async def extract(data,name):return {'text':excerpt,'text_sha256':hashlib.sha256(excerpt.encode()).hexdigest(),
        'format':'utf8','page_count':None,'truncated':False,'extractor':'synthetic'}
    monkeypatch.setitem(sys.modules,'app.services.library_file_text',SimpleNamespace(extract=extract,ExtractionError=ExtractionError))
    with client.app.state.session_factory() as db:
        conn=ProviderConnection(user_id=ids['owner'],kind='compatible',name='Example connection',base_url='https://example.invalid',
            default_model_id='model-a',enabled=True,auth_required=True,readiness_revision=1,auth_grant_revision=1,
            provider_id='openai',protocol_id='openai_chat_completions',capabilities={'model_features':{'model-a':{'model_id':'model-a','base_url':'https://example.invalid','config_revision':1,'tested':{'text_generation':True}}}})
        db.add(conn);db.flush();agent=db.get(Agent,ids['author']);agent.connection_id=conn.id;agent.model_id='model-a'
        db.add(RoomMembership(room_id=ids['room'],agent_id=agent.id));db.flush();db.add(RoomAgentRun(room_id=ids['room'],agent_id=agent.id));db.commit()
    upload_id=str(uuid4());blob=b'synthetic text file';sha=hashlib.sha256(blob).hexdigest()
    store.put(ids['owner'],ids['author'],upload_id,'report.txt',blob,sha)
    called=[]
    async def schedule(room,event,**kwargs):called.append((room,event))
    monkeypatch.setattr(client.app.state.scheduler,'schedule_user_event',schedule)
    body={'bot_id':ids['author'],'upload_id':upload_id,'sha256':sha,'content':'Summarize this file'}
    return client,ids,body,excerpt,called,store


def test_preview_send_receipt_and_replay_without_storing_file_text(setup_question):
    client,ids,body,excerpt,called,_=setup_question;path=f"/api/v1/conversations/{ids['room']}/file-question"
    preview=client.post(path+'/preview',json=body,headers=headers(client,ids));assert preview.status_code==200,preview.text
    data=preview.json();assert data['excerpt']==excerpt and data['target']['model_id']=='model-a'
    assert data['context_preview']['selected_agent_ids']==[body['bot_id']]
    payload={**body,'basis':data['basis'],'consent':True}
    sent=client.post(path,json=payload,headers=headers(client,ids,'key-one'));assert sent.status_code==201,sent.text
    event=sent.json();assert event['actor_type']=='user' and event['actor_id']==ids['owner']
    assert excerpt not in json.dumps(event,ensure_ascii=False) and event['payload']['content']==body['content']
    assert event['payload']['file_context']['proof']['extraction']['text_sha256']==data['extraction']['text_sha256']
    assert client.get(path+'/receipt',headers=headers(client,ids,'key-one')).json()['event_id']==event['event_id']
    assert client.post(path,json=payload,headers=headers(client,ids,'key-one')).status_code==200
    assert len(called)==1
    assert client.get(path+'/receipt',headers=headers(client,ids,'key-one',owner=ids['other'])).status_code==404
    assert client.post(path,json={**payload,'content':'Different'},headers=headers(client,ids,'key-one')).status_code==409
    with client.app.state.session_factory() as db:
        saved=db.get(RoomEvent,event['event_id']);assert excerpt not in json.dumps(saved.payload,ensure_ascii=False)


def test_stale_file_or_model_refused_without_event(setup_question):
    client,ids,body,_,called,store=setup_question;path=f"/api/v1/conversations/{ids['room']}/file-question"
    preview=client.post(path+'/preview',json=body,headers=headers(client,ids));assert preview.status_code==200
    payload={**body,'basis':preview.json()['basis'],'consent':True}
    store.delete(ids['owner'],body['bot_id'],body['upload_id'])
    assert client.post(path,json=payload,headers=headers(client,ids,'key-two')).status_code in (409,410)
    assert not called and client.get(path+'/receipt',headers=headers(client,ids,'key-two')).status_code==404

def test_changed_exact_model_or_missing_consent_never_dispatches(setup_question):
    client,ids,body,_,called,_=setup_question;path=f"/api/v1/conversations/{ids['room']}/file-question"
    preview=client.post(path+'/preview',json=body,headers=headers(client,ids));assert preview.status_code==200
    payload={**body,'basis':preview.json()['basis'],'consent':True}
    assert client.post(path,json={**payload,'consent':False},headers=headers(client,ids,'no-consent')).status_code==422
    with client.app.state.session_factory() as db:
        agent=db.get(Agent,body['bot_id']);conn=db.get(ProviderConnection,agent.connection_id)
        conn.readiness_revision+=1;db.commit()
    assert client.post(path,json=payload,headers=headers(client,ids,'changed-model')).status_code in (409,422)
    assert not called

@pytest.mark.asyncio
async def test_current_root_rechecks_event_after_extraction(setup_question,monkeypatch):
    from app.services import library_file_questions as service
    from fastapi import HTTPException
    client,ids,body,excerpt,_,store=setup_question;path=f"/api/v1/conversations/{ids['room']}/file-question"
    preview=client.post(path+'/preview',json=body,headers=headers(client,ids))
    sent=client.post(path,json={**body,'basis':preview.json()['basis'],'consent':True},headers=headers(client,ids,'root-check'))
    root_id=sent.json()['event_id']
    class ExtractionError(Exception):pass
    async def changed_extract(data,name):
        with client.app.state.session_factory() as db:
            root=db.get(RoomEvent,root_id);root.payload={**root.payload,'content':'changed while awaiting'};db.commit()
        return {'text':excerpt,'text_sha256':hashlib.sha256(excerpt.encode()).hexdigest(),'format':'utf8','page_count':None,'truncated':False,'extractor':'synthetic'}
    monkeypatch.setitem(sys.modules,'app.services.library_file_text',SimpleNamespace(extract=changed_extract,ExtractionError=ExtractionError))
    with client.app.state.session_factory() as db:
        root=db.get(RoomEvent,root_id)
        with pytest.raises(HTTPException):await service.current_for_root(db,ids['owner'],ids['room'],body['bot_id'],root,store,client.app.state.owner_epoch_provider)

def test_receipt_rejects_modified_proof(setup_question):
    client,ids,body,_,_,_=setup_question;path=f"/api/v1/conversations/{ids['room']}/file-question"
    preview=client.post(path+'/preview',json=body,headers=headers(client,ids));assert preview.status_code==200
    sent=client.post(path,json={**body,'basis':preview.json()['basis'],'consent':True},headers=headers(client,ids,'receipt-key'))
    root_id=sent.json()['event_id']
    with client.app.state.session_factory() as db:
        root=db.get(RoomEvent,root_id);ctx=dict(root.payload['file_context']);ctx['proof']={**ctx['proof'],'model_id':'tampered'}
        root.payload={**root.payload,'file_context':ctx};db.commit()
    assert client.get(path+'/receipt',headers=headers(client,ids,'receipt-key')).status_code==404
    assert client.post(path,json={**body,'basis':preview.json()['basis'],'consent':True},headers=headers(client,ids,'receipt-key')).status_code==409


@pytest.mark.parametrize('provider', ['opencode-go', 'chatgpt'])
@pytest.mark.parametrize('history', ['matching', 'stale', 'other_model', 'missing'])
def test_preview_uses_only_matching_saved_text_verification(setup_question, provider, history):
    from app.models.collaboration import ConnectionTestHistory
    from app.services import readiness
    client,ids,body,_,called,_=setup_question
    with client.app.state.session_factory() as db:
        agent=db.get(Agent,body['bot_id']);conn=db.get(ProviderConnection,agent.connection_id)
        conn.provider_id=provider;conn.capabilities={};db.flush()
        if history!='missing':
            model='another-model' if history=='other_model' else agent.model_id
            db.add(ConnectionTestHistory(owner_id=ids['owner'],connection_id=conn.id,
                config_revision=conn.readiness_revision,config_fingerprint=readiness.test_fingerprint(conn,model,'text'),
                model_id=model,ok=True,outcome_code='ready',credential_observation='present',
                duration_ms=1,profile='text',actual_request_count=1))
        db.commit()
        if history=='stale':conn.readiness_revision+=1;db.commit()
    response=client.post(f"/api/v1/conversations/{ids['room']}/file-question/preview",
        json=body,headers=headers(client,ids))
    if history=='matching':
        assert response.status_code==200,response.text
        assert response.json()['target']['model_id']=='model-a'
    else:
        assert response.status_code==409,response.text
        assert response.json()['detail']['code']=='file_question_model_unverified'
    assert not called  # Preview never dispatches a provider request.


def test_png_requires_verified_model_and_separate_consent_and_keeps_pixels_private(setup_question):
    from test_full_flow import PNG
    client,ids,body,_,called,store=setup_question
    upload=str(uuid4());sha=hashlib.sha256(PNG).hexdigest()
    store.put(ids['owner'],body['bot_id'],upload,'pixel.png',PNG,sha)
    body={**body,'upload_id':upload,'sha256':sha,'mode':'image'}
    path=f"/api/v1/conversations/{ids['room']}/file-question"
    invalid=b'not a PNG';invalid_upload=str(uuid4());invalid_sha=hashlib.sha256(invalid).hexdigest()
    store.put(ids['owner'],body['bot_id'],invalid_upload,'invalid.png',invalid,invalid_sha)
    invalid_body={**body,'upload_id':invalid_upload,'sha256':invalid_sha}
    assert client.post(path+'/preview',json=invalid_body,headers=headers(client,ids)).json()['detail']['code']=='file_question_image_invalid'
    detail=client.post(path+'/preview',json=body,headers=headers(client,ids)).json()['detail']
    assert detail['code']=='file_question_image_unverified' and detail['image']['width']==1
    assert detail['file']['sha256']==sha and detail['target']['bot_id']==body['bot_id']
    assert set(detail)=={'code','bot_id','connection_id','model_id','config_revision','auth_revision','target','image','file'}
    with client.app.state.session_factory() as db:
        conn=db.get(ProviderConnection,db.get(Agent,body['bot_id']).connection_id)
        conn.capabilities={'declared':{'vision':True},'verified':{'model_id':'model-a','base_url':conn.base_url,'vision':True},
            'model_features':{'model-a':{'model_id':'model-a','base_url':conn.base_url,'config_revision':conn.readiness_revision,
                                         'tested':{'text_generation':True,'image_input':True}}}}
        db.commit()
    preview=client.post(path+'/preview',json=body,headers=headers(client,ids));assert preview.status_code==200,preview.text
    info=preview.json();assert info['image']=={'mime':'image/png','width':1,'height':1,'size_bytes':len(PNG)}
    assert info['requires_image_consent'] is True and 'image_bytes' not in info and 'excerpt' not in info
    payload={**body,'basis':info['basis'],'consent':True}
    assert client.post(path,json=payload,headers=headers(client,ids,'png-no-consent')).status_code==422
    sent=client.post(path,json={**payload,'image_consent':True},headers=headers(client,ids,'png-yes'))
    assert sent.status_code==201,sent.text
    root=sent.json();ctx=root['payload']['file_context']
    assert ctx['version']==2 and ctx['image_consent'] is True and ctx['proof']['image']==info['image']
    assert PNG.hex() not in json.dumps(root) and len(called)==1
    assert client.get(path+'/receipt',headers=headers(client,ids,'png-yes')).json()['event_id']==root['event_id']
    import asyncio
    from app.services.library_file_questions import current_for_root
    from fastapi import HTTPException
    with client.app.state.session_factory() as db:
        with pytest.raises(HTTPException):
            asyncio.run(current_for_root(db,ids['owner'],ids['room'],body['bot_id']+10000,db.get(RoomEvent,root['event_id']),store,client.app.state.owner_epoch_provider))
    with client.app.state.session_factory() as db:
        conn=db.get(ProviderConnection,db.get(Agent,body['bot_id']).connection_id);conn.auth_grant_revision+=1;db.commit()
    with client.app.state.session_factory() as db:
        with pytest.raises(HTTPException):
            asyncio.run(current_for_root(db,ids['owner'],ids['room'],body['bot_id'],db.get(RoomEvent,root['event_id']),store,client.app.state.owner_epoch_provider))


def test_official_image_check_unlocks_png_preview_without_changing_declaration(setup_question,monkeypatch):
    import base64
    from test_full_flow import PNG
    from app.services.connections import ConnectionConfig
    from app.services import providers
    from types import SimpleNamespace
    client,ids,body,_,_,store=setup_question
    upload=str(uuid4());sha=hashlib.sha256(PNG).hexdigest()
    store.put(ids['owner'],body['bot_id'],upload,'pixel.png',PNG,sha)
    body={**body,'upload_id':upload,'sha256':sha,'mode':'image'}
    with client.app.state.session_factory() as db:
        agent=db.get(Agent,body['bot_id']);conn=db.get(ProviderConnection,agent.connection_id);conn_id=conn.id
        conn.capabilities={'declared':{'tools':True},'verified':{'models':{'model-a':{'model_id':'model-a','base_url':conn.base_url,'tools':True}}},
            'model_features':{'model-a':{'model_id':'model-a','base_url':conn.base_url,'config_revision':conn.readiness_revision,'tested':{'text_generation':True,'tool_calling':True}}}}
        db.commit();revision=conn.readiness_revision;auth_revision=conn.auth_grant_revision
    async def resolve(db,connection_id,owner_id,*args,**kwargs):
        fresh=db.get(ProviderConnection,connection_id)
        return ConnectionConfig(fresh.id,fresh.kind,'model-a',fresh.base_url,None,fresh.capabilities,provider_id='openai',protocol_id='openai_chat_completions',config_revision=fresh.readiness_revision,auth_grant_revision=fresh.auth_grant_revision)
    class Adapter:
        async def generate(self,request):
            seen.append(request.vision.image_bytes)
            return SimpleNamespace(text='red' if len(seen)==1 else 'blue')
    seen=[]
    monkeypatch.setattr('app.services.connections.resolve_connection_async',resolve)
    monkeypatch.setattr(providers,'adapter_for',lambda *args,**kwargs:Adapter())
    file_path=f"/api/v1/conversations/{ids['room']}/file-question/preview"
    assert client.post(file_path,json=body,headers=headers(client,ids)).json()['detail']['code']=='file_question_image_unverified'
    check_path=f'/api/v1/connections/{conn_id}/features/image-input/check'
    result=client.post(check_path,json={'model_id':'model-a','config_revision':revision,'auth_grant_revision':auth_revision},headers=headers(client,ids,'image-check-synthetic'))
    assert result.status_code==200 and result.json()['status']=='verified',result.text
    report=client.get(f'/api/v1/connections/{conn_id}/features?model_id=model-a',headers=headers(client,ids))
    assert report.status_code==200 and report.json()['features']['image_input']['available'] is True
    preview=client.post(file_path,json=body,headers=headers(client,ids));assert preview.status_code==200,preview.text
    assert len(seen)==2 and preview.json()['image']['size_bytes']==len(PNG)
    for response in (result,report,preview):
        for image in [*seen,PNG]:assert base64.b64encode(image).decode() not in response.text
    with client.app.state.session_factory() as db:
        conn=db.get(ProviderConnection,conn_id);saved=conn.capabilities
        assert conn.readiness_revision==revision and conn.auth_grant_revision==auth_revision
        assert saved['declared']=={'tools':True} and saved['verified']['models']['model-a']['tools'] is True
        assert saved['model_features']['model-a']['tested']['text_generation'] is True
        assert saved['model_features']['model-a']['tested']['tool_calling'] is True
