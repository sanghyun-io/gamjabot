"""Real local PDF/store/SQL/projection; transport and Owner epoch are synthetic."""
import hashlib
import json
import sys
from uuid import uuid4

from test_room_state import room_client
from test_room_results import results, headers
from test_library_file_questions import setup_question
from test_library_file_text import text_pdf
from app.models.collaboration import ProviderConnection, RoomEvent, RoomScheduleSettings
from app.models.models import Agent
from app.services import library_file_text as actual_extractor
from app.services.connections import ConnectionConfig
from app.services.events import append_event, ActorRef
from app.services.providers import ProviderResult
from app.services.providers.base import attributed_messages

def test_pdf_preview_to_exact_bot_transport_then_no_automatic_reuse(setup_question,monkeypatch):
    client,ids,body,_,_,uploads=setup_question
    monkeypatch.setitem(sys.modules,'app.services.library_file_text',actual_extractor)
    marker='PDF_BLIND_'+uuid4().hex;blob=text_pdf(marker);upload=str(uuid4());digest=hashlib.sha256(blob).hexdigest()
    uploads.put(ids['owner'],body['bot_id'],upload,'context.pdf',blob,digest)
    body={**body,'upload_id':upload,'sha256':digest,'content':'Use the selected document to answer.'}
    path=f"/api/v1/conversations/{ids['room']}/file-question"
    preview=client.post(path+'/preview',json=body,headers=headers(client,ids))
    assert preview.status_code==200,preview.text
    data=preview.json();assert data['extraction']['format']=='pdf' and marker in data['excerpt']
    assert any(marker in item['content'] for item in data['context_preview']['agents'][0]['messages'])
    async def connection(db,connection_id,owner_id,*args,**kwargs):
        row=db.get(ProviderConnection,connection_id)
        return ConnectionConfig(row.id,row.kind,row.default_model_id,row.base_url,'synthetic-only',row.capabilities,
            provider_id=row.provider_id,protocol_id=row.protocol_id,auth_method_id=row.auth_method_id,
            config_revision=row.readiness_revision)
    monkeypatch.setattr('app.services.context.resolve_connection_async',connection)
    calls=[]
    async def transport(request):
        calls.append((request.agent_id,str(attributed_messages(request))))
        return ProviderResult(text='Synthetic response')
    scheduler=client.app.state.scheduler;scheduler.provider=transport
    monkeypatch.setattr(scheduler.broker,'owner_epochs',client.app.state.owner_epoch_provider)
    with client.app.state.session_factory() as db:
        settings=db.get(RoomScheduleSettings,ids['room'])
        if settings is None:
            settings=RoomScheduleSettings(room_id=ids['room']);db.add(settings)
        settings.max_rounds=1;settings.max_tool_calls=0;settings.max_executor_seconds=5;db.commit()
    sent=client.post(path,json={**body,'basis':data['basis'],'consent':True},headers=headers(client,ids,'actual-pdf-synthetic-transport'))
    assert sent.status_code==201,sent.text
    root=sent.json()
    assert marker not in json.dumps(root)  # No file body is persisted in its event.
    # The API fixture only records scheduling. Execute the real scheduler explicitly now.
    original=type(scheduler).schedule_user_event.__get__(scheduler,type(scheduler))
    client.portal.call(lambda:original(ids['room'],root['event_id'],wait=True))
    with client.app.state.session_factory() as db:
        failures=[row.payload for row in db.query(RoomEvent).filter_by(room_id=ids['room'],kind='failure').all()]
    assert calls and all(bot==body['bot_id'] and marker in text for bot,text in calls),failures
    calls.clear()
    with client.app.state.session_factory() as db:
        agent=db.get(Agent,body['bot_id'])
        next_event=append_event(db,ids['room'],ActorRef('user',ids['owner']),'message',{'content':f'@{agent.name} next request'})
        next_id=next_event.event_id
    client.portal.call(lambda:original(ids['room'],next_id,wait=True))
    assert calls and all(marker not in text for _,text in calls)


import pytest
@pytest.mark.parametrize('provider,protocol',[('opencode-go','openai_chat_completions'),('chatgpt','openai_responses')])
@pytest.mark.parametrize('change_binding',[False,True])
def test_png_exact_root_only_and_restart_requires_new_consent(setup_question,monkeypatch,provider,protocol,change_binding):
    from test_full_flow import PNG
    client,ids,body,_,_,uploads=setup_question
    from types import SimpleNamespace
    client.app.state.owner_epoch_provider=SimpleNamespace(snapshot=lambda:object(),current=lambda owner: 'a'*64 if owner==ids['owner'] else None)
    upload=str(uuid4());digest=hashlib.sha256(PNG).hexdigest()
    uploads.put(ids['owner'],body['bot_id'],upload,'pixel.png',PNG,digest)
    body={**body,'upload_id':upload,'sha256':digest,'mode':'image'}
    with client.app.state.session_factory() as db:
        conn=db.get(ProviderConnection,db.get(Agent,body['bot_id']).connection_id)
        conn.provider_id=provider;conn.protocol_id=protocol
        conn.capabilities={'declared':{'vision':True,'tools':True},'verified':{'model_id':'model-a','base_url':conn.base_url,'vision':True,'tools':True},
            'model_features':{'model-a':{'model_id':'model-a','base_url':conn.base_url,'config_revision':conn.readiness_revision,
                                         'tested':{'text_generation':True,'image_input':True,'tool_calling':True}}}}
        settings=db.get(RoomScheduleSettings,ids['room'])
        if settings is None:
            settings=RoomScheduleSettings(room_id=ids['room']);db.add(settings)
        settings.max_rounds=1;settings.max_tool_calls=2;settings.max_executor_seconds=5
        db.commit()
    async def connection(db,connection_id,owner_id,*args,**kwargs):
        row=db.get(ProviderConnection,connection_id)
        return ConnectionConfig(row.id,row.kind,row.default_model_id,row.base_url,'synthetic-only',row.capabilities,
            provider_id=row.provider_id,protocol_id=row.protocol_id,config_revision=row.readiness_revision,auth_grant_revision=row.auth_grant_revision)
    monkeypatch.setattr('app.services.context.resolve_connection_async',connection)
    scheduler=client.app.state.scheduler
    monkeypatch.setattr(scheduler.broker,'owner_epochs',client.app.state.owner_epoch_provider)
    calls=[]
    async def transport(request):
        calls.append((request.agent_id,request.vision.image_bytes if request.vision else None,len(request.tools)))
        if request.vision is not None and not request.observations:
            from app.services.providers import ToolCall
            assert any(tool['name']=='list_teammates' for tool in request.tools)
            if provider=='chatgpt':
                from app.services.providers.chatgpt import chatgpt_request_body
                from app.services.providers.base import continuation_scope,wire_tool_name
                wire,_=chatgpt_request_body(request)
                output={'type':'function_call','call_id':'synthetic-call','name':wire_tool_name('list_teammates'),'arguments':'{}'}
                state={'provider_kind':'chatgpt',**continuation_scope(request),'input':[*wire['input'],output],'call_ids':['synthetic-call']}
            else:
                from app.services.providers.base import continuation_scope,wire_tool_name
                state={'provider_kind':'opencode-go',**continuation_scope(request),'call_ids':['synthetic-call'],
                       'assistant':{'role':'assistant','content':None,'tool_calls':[{'id':'synthetic-call','type':'function','function':{'name':wire_tool_name('list_teammates'),'arguments':'{}'}}]}}
            if change_binding:
                with client.app.state.session_factory() as db:
                    row=db.get(ProviderConnection,db.get(Agent,body['bot_id']).connection_id);row.auth_grant_revision+=1;db.commit()
            return ProviderResult(tool_calls=[ToolCall('synthetic-call','list_teammates',{})],continuation=state)
        return ProviderResult(text='Synthetic response')
    scheduler.provider=transport
    path=f"/api/v1/conversations/{ids['room']}/file-question"
    preview=client.post(path+'/preview',json=body,headers=headers(client,ids));assert preview.status_code==200,preview.text
    sent=client.post(path,json={**body,'basis':preview.json()['basis'],'consent':True,'image_consent':True},headers=headers(client,ids,'png-root'))
    assert sent.status_code==201,sent.text
    root=sent.json()['event_id'];original=type(scheduler).schedule_user_event.__get__(scheduler,type(scheduler))
    client.portal.call(lambda:original(ids['room'],root,wait=True))
    with client.app.state.session_factory() as db:
        failures=[e.payload for e in db.query(RoomEvent).filter_by(room_id=ids['room'],kind='failure')]
    if change_binding:
        assert len(calls)==1 and any(f.get('code')=='file_context_changed' for f in failures),json.dumps(failures)
        return
    assert len(calls)==2 and calls[0][0]==body['bot_id'] and calls[0][1]==PNG,json.dumps(failures)
    assert calls[1][1]==(None if provider=='chatgpt' else PNG) and calls[1][2]>0
    calls.clear()
    with client.app.state.session_factory() as db:
        agent=db.get(Agent,body['bot_id']);event=append_event(db,ids['room'],ActorRef('user',ids['owner']),'message',{'content':f'@{agent.name} next question'})
        next_id=event.event_id
    client.portal.call(lambda:original(ids['room'],next_id,wait=True))
    assert calls and all(image is None for _,image,_ in calls)
    preview=client.post(path+'/preview',json=body,headers=headers(client,ids));assert preview.status_code==200
    sent=client.post(path,json={**body,'basis':preview.json()['basis'],'consent':True,'image_consent':True},headers=headers(client,ids,'png-restart'))
    root=sent.json()['event_id'];scheduler._image_roots.clear();calls.clear()
    client.portal.call(lambda:original(ids['room'],root,wait=True))
    with client.app.state.session_factory() as db:
        failures=[e.payload for e in db.query(RoomEvent).filter_by(room_id=ids['room'],kind='failure') if e.payload.get('trigger_event_id')==root]
    assert not calls and any(e.get('code')=='file_question_image_reconfirmation_required' for e in failures)
