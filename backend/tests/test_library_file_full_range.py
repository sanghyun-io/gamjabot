"""Consent-scoped full-range file reading with isolated synthetic inputs."""
import hashlib,json,sys
from uuid import uuid4
import pytest
from test_library_file_questions import setup_question
from test_room_state import room_client
from test_room_results import headers,results
from app.services import library_file_text as real_extract
from app.services import library_file_questions as service
from app.models.collaboration import RoomEvent,RoomScheduleSettings,ProviderConnection
from app.models.models import Agent
from app.services.connections import ConnectionConfig
from app.services.providers import ProviderResult,ToolCall
from app.services.providers.base import continuation_scope,wire_tool_name

@pytest.mark.asyncio
async def test_utf8_range_and_search_pagination():
    data=('x '*20000+'needle '+'needle '*6+'TAIL_UNIQUE').encode()
    old=await real_extract.extract(data,'a.txt')
    full=await real_extract.extract(data,'a.txt',full_access=True)
    assert old['truncated'] and 'TAIL_UNIQUE' not in old['text']
    assert not full['truncated'] and 'TAIL_UNIQUE' in full['text']
    prepared={'full_access':True,'full_text':full['text'],'extraction':full}
    first=service.read_selected(prepared,{'offset':0,'query':'needle'})
    assert len(first['matches'])==5 and first['next_search_offset'] is not None
    second=service.read_selected(prepared,{'offset':first['next_search_offset'],'query':'needle'})
    assert len(second['matches'])==2 and second['matches'][0]>40000
    tail=service.read_selected(prepared,{'offset':40000})
    assert 'TAIL_UNIQUE' in tail['text']
    with pytest.raises(Exception):service.read_selected(prepared,{'offset':0,'file_id':'other'})


def test_full_range_consent_receipt_and_changed_file(setup_question,monkeypatch):
    client,ids,body,_,called,store=setup_question
    monkeypatch.setitem(sys.modules,'app.services.library_file_text',real_extract)
    marker='UNIQUE_END_'+uuid4().hex;blob=('a'*41000+marker).encode();upload=str(uuid4());sha=hashlib.sha256(blob).hexdigest()
    store.put(ids['owner'],body['bot_id'],upload,'long.txt',blob,sha)
    body={**body,'upload_id':upload,'sha256':sha,'full_access':True}
    with client.app.state.session_factory() as db:
        agent=db.get(Agent,body['bot_id']);conn=db.get(ProviderConnection,agent.connection_id)
        conn.capabilities={'declared':{'tools':True},'verified':{'model_id':'model-a','base_url':conn.base_url,'tools':True},'model_features':{'model-a':{'model_id':'model-a','base_url':conn.base_url,'config_revision':conn.readiness_revision,'tested':{'text_generation':True,'tool_calling':True}}}}
        db.commit()
    path=f"/api/v1/conversations/{ids['room']}/file-question"
    preview=client.post(path+'/preview',json=body,headers=headers(client,ids));assert preview.status_code==200,preview.text
    data=preview.json();assert data['extraction']['chars']==len(blob) and len(data['excerpt'])==40000 and marker not in data['excerpt']
    assert data['excerpt_sha256']==hashlib.sha256(data['excerpt'].encode()).hexdigest()
    payload={**body,'basis':data['basis'],'consent':True}
    assert client.post(path,json=payload,headers=headers(client,ids,'no-full-consent')).status_code==422
    sent=client.post(path,json={**payload,'full_access_consent':True},headers=headers(client,ids,'full-ok'))
    assert sent.status_code==201,sent.text
    ctx=sent.json()['payload']['file_context'];assert ctx['version']==4 and ctx['full_access_consent'] is True
    assert marker not in json.dumps(sent.json())
    assert client.post(path,json={**payload,'full_access_consent':True},headers=headers(client,ids,'full-ok')).status_code==200
    assert client.post(path,json={**payload,'full_access':False},headers=headers(client,ids,'full-ok')).status_code==409
    store.delete(ids['owner'],body['bot_id'],upload)
    with client.app.state.session_factory() as db:
        root=db.get(RoomEvent,sent.json()['event_id'])
        with pytest.raises(Exception):client.portal.call(lambda:service.current_for_root(db,ids['owner'],ids['room'],body['bot_id'],root,store,client.app.state.owner_epoch_provider))

@pytest.mark.parametrize('provider,protocol',[('opencode-go','openai_chat_completions'),('chatgpt','openai_responses')])
@pytest.mark.parametrize('mutation',['none','owner','file','stop','no_read','owner_after_read','file_after_read'])
def test_scheduler_reads_after_40k_with_consented_exact_root(setup_question,monkeypatch,provider,protocol,mutation):
    client,ids,body,_,_,store=setup_question
    from types import SimpleNamespace
    epoch={'value':'a'*64}
    client.app.state.owner_epoch_provider=SimpleNamespace(snapshot=lambda:object(),current=lambda owner:epoch['value'] if owner==ids['owner'] else None)
    monkeypatch.setitem(sys.modules,'app.services.library_file_text',real_extract)
    marker='SECRET_AFTER_40K_'+uuid4().hex;blob=('a'*41000+marker).encode();upload=str(uuid4());sha=hashlib.sha256(blob).hexdigest()
    store.put(ids['owner'],body['bot_id'],upload,'long.txt',blob,sha)
    body={**body,'upload_id':upload,'sha256':sha,'full_access':True}
    with client.app.state.session_factory() as db:
        agent=db.get(Agent,body['bot_id']);conn=db.get(ProviderConnection,agent.connection_id)
        conn.provider_id=provider;conn.protocol_id=protocol
        conn.capabilities={'declared':{'tools':True},'verified':{'model_id':'model-a','base_url':conn.base_url,'tools':True},
            'model_features':{'model-a':{'model_id':'model-a','base_url':conn.base_url,'config_revision':conn.readiness_revision,'tested':{'text_generation':True,'tool_calling':True}}}}
        settings=db.get(RoomScheduleSettings,ids['room'])
        if settings is None:settings=RoomScheduleSettings(room_id=ids['room']);db.add(settings)
        settings.max_rounds=1;settings.max_tool_calls=4;settings.max_executor_seconds=20;db.commit()
    async def connection(db,connection_id,owner_id,*args,**kwargs):
        row=db.get(ProviderConnection,connection_id)
        return ConnectionConfig(row.id,row.kind,row.default_model_id,row.base_url,'synthetic-only',row.capabilities,provider_id=row.provider_id,
          protocol_id=row.protocol_id,auth_method_id=row.auth_method_id,config_revision=row.readiness_revision,auth_grant_revision=row.auth_grant_revision)
    monkeypatch.setattr('app.services.context.resolve_connection_async',connection)
    monkeypatch.setattr('app.services.memory_policy.direct_eligible',lambda *a,**k:False)
    monkeypatch.setattr('app.services.skill_persistence.eligible',lambda *a,**k:False)
    scheduler=client.app.state.scheduler;monkeypatch.setattr(scheduler.broker,'owner_epochs',client.app.state.owner_epoch_provider)
    import app.services.scheduler as scheduler_module
    diagnostic=scheduler_module.make_diagnostic;errors=[]
    def capture(error,**kwargs):
        import traceback
        errors.append((type(error).__name__,str(error),traceback.extract_tb(error.__traceback__)[-3:]))
        return diagnostic(error,**kwargs)
    monkeypatch.setattr(scheduler_module,'make_diagnostic',capture)
    seen=[]
    async def transport(request):
        seen.append(request)
        if not request.observations:
            assert any(t['name']=='read_selected_file' for t in request.tools)
            if mutation=='no_read':return ProviderResult(text='Only the initial excerpt was used.')
            args=json.dumps({'offset':40990,'query':'SECRET_AFTER_40K_'})
            if provider=='chatgpt':
                from app.services.providers.chatgpt import chatgpt_request_body
                wire,_=chatgpt_request_body(request)
                assert any(x.get('name')==wire_tool_name('read_selected_file') for group in wire['tools'] for x in group.get('tools',[]))
                output={'type':'function_call','call_id':'read-file','name':wire_tool_name('read_selected_file'),'arguments':args}
                state={'provider_kind':'chatgpt',**continuation_scope(request),'input':[*wire['input'],output],'call_ids':['read-file']}
            else:
                state={'provider_kind':'opencode-go',**continuation_scope(request),'call_ids':['read-file'],
                       'assistant':{'role':'assistant','content':None,'tool_calls':[{'id':'read-file','type':'function','function':{'name':wire_tool_name('read_selected_file'),'arguments':args}}]}}
            if mutation=='owner':epoch['value']='b'*64
            elif mutation=='file':store.delete(ids['owner'],body['bot_id'],upload)
            elif mutation=='stop':scheduler._cancellations[(ids['room'],body['bot_id'])].set()
            return ProviderResult(tool_calls=[ToolCall('read-file','read_selected_file',{'offset':40990,'query':'SECRET_AFTER_40K_'})],continuation=state)
        assert marker in request.observations[-1].text
        if mutation=='owner_after_read':epoch['value']='b'*64
        elif mutation=='file_after_read':store.delete(ids['owner'],body['bot_id'],upload)
        return ProviderResult(text='Observed '+marker)
    scheduler.provider=transport
    path=f"/api/v1/conversations/{ids['room']}/file-question"
    preview=client.post(path+'/preview',json=body,headers=headers(client,ids));assert preview.status_code==200,preview.text
    sent=client.post(path,json={**body,'basis':preview.json()['basis'],'consent':True,'full_access_consent':True},headers=headers(client,ids,'range-e2e'))
    assert sent.status_code==201,sent.text
    original=type(scheduler).schedule_user_event.__get__(scheduler,type(scheduler))
    client.portal.call(lambda:original(ids['room'],sent.json()['event_id'],wait=True))
    with client.app.state.session_factory() as db:
        failures=[e.payload for e in db.query(RoomEvent).filter_by(room_id=ids['room'],kind='failure').all()]
        answers=[e.payload for e in db.query(RoomEvent).filter_by(room_id=ids['room'],kind='message',actor_type='agent').all()]
    if mutation=='none':
        assert len(seen)>=2,(len(seen),failures,errors)
        assert any(marker in str(a) for a in answers),failures
        coverage=next(a['file_read_coverage'] for a in answers if marker in str(a))
        assert coverage['root_event_id']==sent.json()['event_id'] and coverage['total_chars']==len(blob)
        assert coverage['read_ranges']==[[0,1600],[40990,len(blob)]] and coverage['search_used'] is True and coverage['extraction_truncated'] is False
    elif mutation=='no_read':
        assert len(seen)==1 and answers
        coverage=answers[-1]['file_read_coverage']
        assert coverage['read_ranges']==[[0,1600]] and coverage['search_used'] is False
    else:
        assert len(seen)==(2 if mutation.endswith('_after_read') else 1),(len(seen),failures,errors)
        assert not any(marker in str(a) for a in answers)


def test_full_range_requires_verified_tools_before_send(setup_question,monkeypatch):
    client,ids,body,_,called,store=setup_question
    monkeypatch.setitem(sys.modules,'app.services.library_file_text',real_extract)
    body={**body,'full_access':True}
    broker=client.app.state.scheduler.broker
    monkeypatch.setattr(broker,'requires_verified_tools',True)
    path=f"/api/v1/conversations/{ids['room']}/file-question"
    response=client.post(path+'/preview',json=body,headers=headers(client,ids))
    assert response.status_code==409 and response.json()['detail']['code']=='file_question_tools_unavailable'
    assert not called

@pytest.mark.asyncio
async def test_full_limit_and_observation_wire_bound():
    data=('😀'*200001).encode()
    full=await real_extract.extract(data,'a.txt',full_access=True)
    assert len(full['text'])==200000 and full['truncated'] is True
    prepared={'full_access':True,'full_text':full['text'],'extraction':full}
    value=service.read_selected(prepared,{'offset':0,'query':'😀'})
    assert len(json.dumps(value,ensure_ascii=True))<16000

@pytest.mark.asyncio
async def test_pdf_and_office_workers_extract_past_40k():
    import io,zipfile
    from test_library_file_text import text_pdf
    marker='PDF_AFTER_40K'
    pdf=text_pdf('A'*40500+marker)
    short=await real_extract.extract(pdf,'sample.pdf')
    full=await real_extract.extract(pdf,'sample.pdf',full_access=True)
    assert short['truncated'] and marker not in short['text']
    assert marker in full['text'] and not full['truncated']
    target=io.BytesIO()
    with zipfile.ZipFile(target,'w',zipfile.ZIP_STORED) as archive:
        archive.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'+('😀'*41000)+'DOCX_AFTER_40K</w:t></w:r></w:p></w:body></w:document>')
    short=await real_extract.extract(target.getvalue(),'sample.docx')
    full=await real_extract.extract(target.getvalue(),'sample.docx',full_access=True)
    assert short['truncated'] and 'DOCX_AFTER_40K' not in short['text']
    assert 'DOCX_AFTER_40K' in full['text'] and not full['truncated']

@pytest.mark.asyncio
async def test_go_messages_selected_file_wire_roundtrip(monkeypatch):
    from dataclasses import replace
    from app.services import connections, model_features, provider_catalog
    from app.services.providers.base import ProviderRequest, ToolObservation
    from app.services.providers.opencode_go import OpenCodeGoMessagesAdapter
    model='qwen3.7-plus';base=provider_catalog.OPENCODE_GO_BASE_URL
    caps={'verified':{'model_id':model,'base_url':base,'tools':True},
          'model_features':{model:{'model_id':model,'base_url':base,'config_revision':1,
                                    'tested':{'text_generation':True,'tool_calling':True}}}}
    cfg=ConnectionConfig(1,'opencode-go',model,base,'synthetic',caps,provider_id='opencode-go',
                         protocol_id='anthropic_messages',config_revision=1,owner_id=1)
    req=ProviderRequest(connection=cfg,model_id=model,room_id=0,system='Synthetic',tools=[service.FILE_READ_SCHEMA])
    async def enforce(_request):return None
    async def discover(_config,transport=None):return [{'id':model}],False,None
    monkeypatch.setattr(model_features,'enforce',enforce)
    monkeypatch.setattr(connections,'discover_models',discover)
    adapter=OpenCodeGoMessagesAdapter();captured=[]
    async def post(request,path,headers,body):
        captured.append(body)
        if request.observations:return {'content':[{'type':'text','text':'TAIL_AFTER_40K'}]}
        return {'content':[{'type':'tool_use','id':'read1','name':wire_tool_name('read_selected_file'),
                            'input':{'offset':40990,'query':'TAIL_AFTER_40K'}}]}
    monkeypatch.setattr(adapter,'_post',post)
    first=await adapter.generate(req)
    assert wire_tool_name('read_selected_file') in {entry['name'] for entry in captured[0]['tools']}
    assert first.tool_calls[0].family=='read_selected_file'
    assert first.tool_calls[0].arguments=={'offset':40990,'query':'TAIL_AFTER_40K'}
    observation=ToolObservation('read1','completed',json.dumps({'offset':40990,'text':'TAIL_AFTER_40K'}))
    final=await adapter.generate(replace(req,continuation=first.continuation,observations=[observation]))
    assert 'TAIL_AFTER_40K' in json.dumps(captured[1]) and final.text=='TAIL_AFTER_40K'
