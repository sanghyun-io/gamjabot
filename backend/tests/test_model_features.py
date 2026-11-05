"""Feature preflight must not turn metadata or another model's success into execution authority."""
from dataclasses import replace
import asyncio
import httpx
import pytest
from fastapi import HTTPException
from app.services.connections import ConnectionConfig
from app.services.model_features import permitted,report,output_request
from app.services.providers import ProviderRequest,ProviderFailure,VisionInput,adapter_for


def test_remote_image_evidence_binds_auth_without_enabling_declaration():
    from app.services.providers.base import verified
    image={'model_id':'remote-model','base_url':'https://example.invalid/v1','config_revision':2,
           'auth_grant_revision':1,'verified':True}
    c=ConnectionConfig(1,'compatible','remote-model',image['base_url'],None,{
        'declared':{'tools':True},
        'verified':{'models':{'remote-model':{'model_id':'remote-model','base_url':image['base_url'],'tools':True,'vision':True}}},
        'model_features':{'remote-model':{'model_id':'remote-model','base_url':image['base_url'],
            'config_revision':2,'tested':{'text_generation':True,'tool_calling':True,'image_input':True},'image_check':image}}},
        config_revision=2,auth_grant_revision=1)
    req=ProviderRequest(connection=c,model_id='remote-model')
    assert permitted(c,'image_input') and verified(req,'vision') and verified(req,'tools')
    assert not c.capabilities['declared'].get('vision',False)
    changed=replace(c,auth_grant_revision=2)
    assert not permitted(changed,'image_input') and not verified(replace(req,connection=changed),'vision')
    assert verified(replace(req,connection=changed),'tools')
    assert not verified(replace(req,model_id='other'),'vision')


def config():
    row={'model_id':'local-test','base_url':'http://127.0.0.1:11434/v1','config_revision':1,'reported':{'text_generation':True,'tool_calling':True,'image_input':False},'tested':{'text_generation':True,'tool_calling':False}}
    return ConnectionConfig(1,'compatible','local-test',row['base_url'],None,{'model_features':{'local-test':row}},provider_id='ollama',protocol_id='openai_chat_completions',auth_method_id='local_none',config_revision=1)


def test_metadata_cannot_enable_failed_or_untested_feature():
    c=config()
    assert permitted(c,'text_generation')
    assert not permitted(c,'tool_calling')
    assert report(c)['features']['tool_calling']['reason']=='failed'
    assert report(c)['features']['image_input']['reason']=='unsupported'


@pytest.mark.parametrize('field,value',[('model_id','another-model'),('base_url','http://127.0.0.1:1234/v1'),('config_revision',2)])
def test_evidence_is_bound_to_exact_saved_target(field,value):
    c=config();setattr(c,field,value)
    assert not permitted(c,'text_generation')


@pytest.mark.parametrize('feature',['image_generation','speech_generation','audio_input','embeddings'])
def test_model_advertisement_does_not_create_an_app_adapter(feature):
    c=config();record=c.capabilities['model_features'][c.model_id];record['reported'][feature]=True;record['tested'][feature]=True
    assert not permitted(c,feature)
    assert report(c)['features'][feature]['reason']=='app_unsupported'


@pytest.mark.parametrize('content,kind',[('draw a picture','image_generation'),('speak hello','speech_generation'),('이미지를 생성해줘','auto'),('Generate an image of a cat','auto')])
def test_unsupported_output_is_rejected_before_dispatch(content,kind):
    with pytest.raises(HTTPException) as error:output_request(content,kind)
    assert error.value.status_code==422
    assert error.value.detail['code']=='model_output_unavailable'


def test_explanation_is_not_image_generation():
    assert output_request('이미지 생성 모델의 차이를 설명해줘')=='text'
    assert output_request('Generate an SVG example','text')=='text'


@pytest.mark.parametrize('mode',['image_output','image_input','tools','unverified_text'])
def test_real_adapter_cannot_send_unsupported_features(mode):
    calls=[]
    async def handler(request):calls.append(request);raise AssertionError('network must not be reached')
    async def run():
        c=config();e={'seq':1,'event_id':'active','actor_type':'user','actor_id':1,'actor_name_snapshot':'Synthetic','kind':'message','payload':{'content':'hello'}}
        req=ProviderRequest(connection=c,model_id=c.model_id,events=[e],watermark_seq=1,trigger_event_id='active')
        if mode=='image_output':e['payload']['output_kind']='image_generation'
        elif mode=='image_input':req=replace(req,vision=VisionInput(b'not-read','image/png','describe'))
        elif mode=='tools':req=replace(req,tools=[{'name':'echo','parameters':{'type':'object'}}])
        else:c.capabilities={}
        with pytest.raises(ProviderFailure):await adapter_for('openai_chat_completions','ollama',httpx.MockTransport(handler)).generate(req)
        assert calls==[]
    asyncio.run(run())

@pytest.mark.parametrize('profile',['text','full'])
def test_existing_cloud_text_test_binds_exact_agent_model_and_profile(profile):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.core.database import Base
    import app.models
    from app.models.collaboration import ProviderConnection,ConnectionTestHistory
    from app.services.readiness import test_fingerprint
    from app.services.model_features import bind_saved_text_test
    engine=create_engine('sqlite:///:memory:');Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            row=ProviderConnection(id=1,user_id=1,kind='openai',name='Synthetic',base_url=None,default_model_id='default-model',enabled=True,auth_required=True,secret_ref='synthetic-reference-not-a-secret',readiness_revision=3,provider_id='openai',protocol_id='openai_chat_completions',auth_method_id='api_key',public_config_version=1,public_config={},auth_grant_revision=1,credential_state='present',capabilities={})
            db.add(row);db.flush()
            db.add(ConnectionTestHistory(owner_id=1,connection_id=1,config_revision=3,config_fingerprint=test_fingerprint(row,'agent-model',profile),model_id='agent-model',ok=True,outcome_code='connected',credential_observation='verified',duration_ms=1,profile=profile,actual_request_count=1));db.flush()
            c=ConnectionConfig(1,'openai','agent-model',None,None,{},provider_id='openai',protocol_id='openai_chat_completions',auth_method_id='api_key',config_revision=3)
            bind_saved_text_test(db,row,c)
            assert permitted(c,'text_generation')
            changed=replace(c,model_id='another-model');assert not permitted(changed,'text_generation')
            row.readiness_revision=4
            changed=replace(c,config_revision=4,capabilities={});bind_saved_text_test(db,row,changed)
            assert not permitted(changed,'text_generation')
    finally:engine.dispose()


def test_compatible_modalities_are_advisory_and_dynamic_created_is_not_identity():
    from app.services.model_features import compatible_metadata
    item={'id':'hf-tiny','owned_by':'llamacpp','created':1,'architecture':{'input_modalities':['text','image'],'output_modalities':['text']},'meta':{'n_params':256000000}}
    props={'modalities':{'vision':True,'audio':False},'chat_template_caps':{'supports_tools':False,'supports_tool_calls':False},'build_info':'synthetic-runtime'}
    advertised,identity=compatible_metadata(item,props)
    assert advertised['image_input'] is True and advertised['tool_calling'] is False
    assert advertised['image_generation'] is False and advertised['speech_generation'] is False
    assert compatible_metadata({**item,'created':999},props)[1]==identity
    c=config();c.capabilities['model_features'][c.model_id].update(reported=advertised,tested={})
    assert not permitted(c,'image_input')
    assert compatible_metadata(item,{**props,'modalities':{'vision':False}})[0]['image_input'] is False


def test_unknown_compatible_server_does_not_advertise_all_features():
    from app.services.model_features import compatible_metadata
    advertised,_=compatible_metadata({'id':'plain-server'})
    assert all(v is None for v in advertised.values())


@pytest.mark.parametrize('legacy',[False,True])
@pytest.mark.parametrize('image_success',[False,True])
def test_image_check_same_key_replays_without_extra_requests(monkeypatch,legacy,image_success):
    import json
    from types import SimpleNamespace
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.core.database import Base
    import app.models
    from app.models.collaboration import ProviderConnection
    from app.api.routes import connections as routes
    from app.services import providers
    engine=create_engine('sqlite:///:memory:');Base.metadata.create_all(engine)
    calls=[]
    try:
        with Session(engine) as db:
            saved={'model_id':'remote-model','base_url':'https://example.invalid/v1','tools':True}
            caps={'declared':{'tools':True},'verified':saved if legacy else {'models':{'remote-model':saved}}}
            row=ProviderConnection(id=1,user_id=1,kind='compatible',name='Synthetic',base_url='https://example.invalid/v1',default_model_id='remote-model',enabled=True,auth_required=False,readiness_revision=2,auth_grant_revision=1,capabilities=caps)
            db.add(row);db.commit()
            async def resolve(*args,**kwargs):
                fresh=db.get(ProviderConnection,1)
                return ConnectionConfig(1,'compatible','remote-model',fresh.base_url,None,fresh.capabilities,config_revision=2,auth_grant_revision=1,provider_id='compatible',protocol_id='openai_chat_completions')
            class Adapter:
                async def generate(self,request):
                    from app.services.tools.desktop import validate_screenshot_png
                    validate_screenshot_png(request.vision.image_bytes, (64,64))
                    assert 'exactly one word' in request.vision.prompt
                    calls.append(1); return SimpleNamespace(text='red' if len(calls)==1 else ('blue' if image_success else 'not blue'))
            monkeypatch.setattr(routes.connections,'resolve_connection_async',resolve)
            monkeypatch.setattr(providers,'adapter_for',lambda *a,**k:Adapter())
            request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(owner_epoch_provider=SimpleNamespace(current=lambda _: 'epoch'))))
            body=routes.ImageInputCheckRequest(model_id='remote-model',config_revision=2,auth_grant_revision=1)
            async def run():
                result=await routes.image_input_check(1,body,request,'same-key',db,SimpleNamespace(id=1))
                assert json.loads(result.body)['status']==('verified' if image_success else 'failed')
                if not image_success:
                    assert json.loads(result.body)['color_results']==['red','other']
                    assert 'not blue' not in result.body.decode()
                again=await routes.image_input_check(1,body,request,'same-key',db,SimpleNamespace(id=1))
                assert json.loads(again.body)['status']==('verified' if image_success else 'failed')
            asyncio.run(run())
            assert len(calls)==2
            db.expire_all(); saved=db.get(ProviderConnection,1)
            assert saved.readiness_revision==2 and saved.auth_grant_revision==1
            assert saved.capabilities['declared']=={'tools':True}
            assert saved.capabilities['verified']['models']['remote-model']['tools'] is True
            assert saved.capabilities['verified']['models']['remote-model']['vision'] is image_success
            from app.services.providers.base import verified
            config=ConnectionConfig(1,'compatible','remote-model',saved.base_url,None,saved.capabilities,config_revision=2,auth_grant_revision=1)
            assert verified(ProviderRequest(connection=config,model_id='remote-model'),'tools')
            report_response=asyncio.run(routes.get_model_features(1,request,'remote-model',db,SimpleNamespace(id=1)))
            assert json.loads(report_response.body)['features']['image_input']['available'] is image_success
            request.app.state.owner_epoch_provider.current=lambda _: 'new-epoch'
            read=routes.image_input_command(1,'same-key',request,db,SimpleNamespace(id=1))
            assert json.loads(read.body)['status']==('verified' if image_success else 'failed')
            with pytest.raises(HTTPException) as conflict:
                asyncio.run(routes.image_input_check(1,body,request,'same-key',db,SimpleNamespace(id=1)))
            assert conflict.value.status_code == 409
            assert len(calls)==2
    finally:
        engine.dispose()


def test_image_check_color_category_requires_one_exact_word():
    from io import BytesIO
    from PIL import Image
    from app.api.routes.connections import _image_color_category, _image_solid_color
    assert [_image_color_category(s) for s in [' Red ', 'BLUE', 'not red', 'red.', '  ']] == ['red','blue','other','other','empty']
    for rgb in [(255,0,0),(0,0,255)]:
        with Image.open(BytesIO(_image_solid_color(bytes(rgb)))) as image:
            assert image.size == (64,64) and image.getpixel((0,0)) == rgb and image.getpixel((63,63)) == rgb


def test_chatgpt_catalog_and_generations_share_physical_meter(monkeypatch):
    from app.services import connections
    from app.services.providers import chatgpt
    calls=[]
    async def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200,json={'ok':True})
    budget=connections.PhysicalRequestBudget(4)
    meter=connections.RequestMeter(httpx.MockTransport(handler),physical_request_budget=budget)
    async def catalog(config,transport=None):
        await transport.handle_async_request(httpx.Request('GET','https://example.invalid/models'))
        return ([{'id':'synthetic'}],False,None)
    async def post(self,request,path,headers,body):
        await self.transport.handle_async_request(httpx.Request('POST','https://example.invalid/responses'))
        return {'output':[{'type':'message','content':[{'type':'output_text','text':'red' if len(calls)==2 else 'blue'}]}]}
    monkeypatch.setattr(connections,'discover_models',catalog)
    monkeypatch.setattr(chatgpt.ChatGPTAdapter,'_post',post)
    monkeypatch.setattr(chatgpt,'chatgpt_request_body',lambda request:({'input':[]},[]))
    monkeypatch.setattr(chatgpt,'chatgpt_wire_diagnostic',lambda body:{})
    c=ConnectionConfig(1,'openai','synthetic','https://example.invalid','synthetic',{},provider_id='chatgpt',protocol_id='openai_responses')
    req=ProviderRequest(connection=c,model_id='synthetic')
    async def run():
        adapter=chatgpt.ChatGPTAdapter(meter)
        assert (await adapter.generate(req)).text=='red'
        assert (await adapter.generate(req)).text=='blue'
    asyncio.run(run())
    assert calls==['/models','/responses','/models','/responses']
    assert budget.actual_count==4


@pytest.mark.parametrize('fail_on,code,expected_code,phase',[
    (1,'rate_limited','rate_limited','first_image'),
    (2,'untrusted_code','image_check_unavailable','second_image'),
])
def test_image_check_safe_failure_and_core_probe_scope(monkeypatch,fail_on,code,expected_code,phase):
    import json
    from types import SimpleNamespace
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.core.database import Base
    import app.models
    from app.models.collaboration import ProviderConnection
    from app.api.routes import connections as routes
    from app.services import providers
    from app.services.core_provider_transport import _scope
    engine=create_engine('sqlite:///:memory:');Base.metadata.create_all(engine)
    calls=[]
    try:
        with Session(engine) as db:
            caps={'declared':{'tools':True},'verified':{'models':{'remote-model':{'model_id':'remote-model','base_url':'https://example.invalid/v1','tools':True}}}}
            db.add(ProviderConnection(id=1,user_id=1,kind='compatible',name='Synthetic',base_url='https://example.invalid/v1',default_model_id='remote-model',enabled=True,auth_required=False,readiness_revision=2,auth_grant_revision=1,capabilities=caps));db.commit()
            async def resolve(*args,**kwargs):
                fresh=db.get(ProviderConnection,1)
                return ConnectionConfig(1,'compatible','remote-model',fresh.base_url,None,fresh.capabilities,config_revision=2,auth_grant_revision=1,owner_id=1,provider_id='compatible',protocol_id='openai_chat_completions')
            class Adapter:
                async def generate(self,request):
                    assert _scope(request,1)['room_id']==1
                    assert _scope(request,1)['agent_id']==1
                    calls.append(1)
                    if len(calls)==fail_on:raise ProviderFailure(code,'secret-response-must-never-be-stored')
                    return SimpleNamespace(text='red')
            monkeypatch.setattr(routes.connections,'resolve_connection_async',resolve)
            monkeypatch.setattr(routes.connections,'is_core_managed_credential',lambda _:True)
            monkeypatch.setattr(routes.connections,'bind_native_runtime',lambda config,epoch:config)
            monkeypatch.setattr(providers,'adapter_for',lambda *a,**k:Adapter())
            request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(owner_epoch_provider=SimpleNamespace(current=lambda _: 'epoch'))))
            body=routes.ImageInputCheckRequest(model_id='remote-model',config_revision=2,auth_grant_revision=1)
            async def run():
                result=await routes.image_input_check(1,body,request,'safe-failure',db,SimpleNamespace(id=1))
                payload=json.loads(result.body)
                assert (payload['status'],payload['error_code'],payload['phase'])==('unknown',expected_code,phase)
                assert 'secret-response' not in result.body.decode()
                again=await routes.image_input_check(1,body,request,'safe-failure',db,SimpleNamespace(id=1))
                assert json.loads(again.body)==payload
            asyncio.run(run())
            assert len(calls)==fail_on
            db.expire_all();saved=db.get(ProviderConnection,1)
            assert saved.capabilities['verified']==caps['verified']
            assert 'secret-response' not in json.dumps(saved.capabilities)
    finally:
        engine.dispose()
