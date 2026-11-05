import hashlib
import json
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app.services import room_results

def test_selection_checks_exact_canonical_block_and_hash(monkeypatch):
    doc={'format':'gamja.page.v1','title':'Synthetic','datasets':[],'views':[],
         'blocks':[{'type':'heading','level':1,'text':'Repeat'}, {'type':'markdown','text':'Repeat'}]}
    monkeypatch.setattr(room_results,'page_target',lambda *a,**k:(None,SimpleNamespace(content=json.dumps(doc))))
    s={'index':1,'type':'markdown','text':'Repeat','sha256':hashlib.sha256(b'Repeat').hexdigest()}
    room_results.validate_page_selection(None,1,1,{},s)
    for modified in ({**s,'index':0},{**s,'index':2},{**s,'text':'Other'},{**s,'sha256':'0'*64},{**s,'type':'heading'}):
        with pytest.raises(HTTPException) as exc: room_results.validate_page_selection(None,1,1,{},modified)
        assert exc.value.status_code==409

from test_room_state import room_client
def test_selected_section_reaches_scheduler_and_same_page(room_client,monkeypatch):
    import hashlib
    from test_room_state import headers
    from test_bot_creation import private_team,tool
    from app.models.room_result import RoomResult,RoomResultVersion
    from app.models.collaboration import RoomEvent
    from app.services.providers import ProviderResult
    from types import SimpleNamespace
    client,(owner,other,project,_,_)=room_client
    bot,room,_=private_team(client,owner,project)
    client.app.state.scheduler.broker.executor=SimpleNamespace()
    document={'format':'gamja.page.v1','title':'Synthetic page','datasets':[],'views':[],
              'blocks':[{'type':'heading','level':1,'text':'Hello'}]}
    async def provider(request):
        if request.observations:return ProviderResult(text='Page created.')
        assert any(t['name']=='write_page' for t in request.tools)
        return tool('write_page',{'document':document},call='page-one')
    client.app.state.scheduler.provider=provider
    response=client.post(f'/api/v1/conversations/{room}/messages',headers=headers(client,owner),json={'content':'@Ada create a Page'})
    assert response.status_code==201,response.text
    with client.app.state.session_factory() as db:
        results=db.query(RoomResult).filter_by(owner_id=owner,room_id=room).all()
        assert len(results)==1 and results[0].current_version==1
        version=db.get(RoomResultVersion,(results[0].result_id,1))
        assert version.kind=='page' and version.author_agent_id==bot
        event=db.get(RoomEvent,version.source_event_id)
        assert event.actor_type=='tool' and event.kind=='tool_result' and event.payload['page_result']['result_id']==results[0].result_id
        result_id=results[0].result_id
    from time import sleep
    target={'result_id':result_id,'version':1}
    selection={'index':0,'type':'heading','text':'Hello','sha256':hashlib.sha256(b'Hello').hexdigest()}
    revised={**document,'title':'Revised page'}
    async def edit(request):
        if request.observations:return ProviderResult(text='Page updated.')
        assert any('user selected this verified Page section' in event.get('payload',{}).get('content','') and 'Hello' in event.get('payload',{}).get('content','') for event in request.events)
        assert any(t['name']=='write_page' for t in request.tools)
        return tool('write_page',{'document':revised},call='page-update')
    client.app.state.scheduler.provider=edit
    own={**headers(client,owner),'Idempotency-Key':'page-edit-one'}
    preview=client.post(f'/api/v1/conversations/{room}/context-preview',headers=own,
                        json={'content':'Rename the Page','page_target':target})
    assert preview.status_code==200,preview.text
    sent=client.post(f'/api/v1/conversations/{room}/events',headers=own,
                     json={'content':'Rename the Page','page_target':target,'page_selection':selection})
    assert sent.status_code==201,sent.text
    for _ in range(200):
        with client.app.state.session_factory() as db:
            if db.get(RoomResult,(result_id)).current_version==2:break
        sleep(.01)
    with client.app.state.session_factory() as db:
        assert db.get(RoomResult,result_id).current_version==2
        assert db.get(RoomResultVersion,(result_id,1)).title=='Synthetic page'
        assert db.get(RoomResultVersion,(result_id,2)).title=='Revised page'
    receipt=client.get(f'/api/v1/conversations/{room}/page-message/receipt',headers=own)
    assert receipt.status_code==200 and receipt.json()['payload']['page_target']==target
    path=f'/api/v1/conversations/{room}/results/{result_id}'
    current=client.get(path+'/current',headers=own)
    assert current.status_code==200 and current.json()['kind']=='page' and current.json()['version']==2
    versions=client.get(path+'/versions',headers=own)
    assert [item['version'] for item in versions.json()['items']]==[2,1]
    history=client.get(path+'/conversation?limit=1',headers=own)
    assert history.status_code==200 and history.json()['items'][0]['event_id']==sent.json()['event_id']
    replay=client.post(f'/api/v1/conversations/{room}/events',headers=own,
                       json={'content':'Rename the Page','page_target':target,'page_selection':selection})
    assert replay.status_code==201 and replay.json()['event_id']==sent.json()['event_id']
    stale=client.post(f'/api/v1/conversations/{room}/events',headers={**own,'Idempotency-Key':'new-stale-key'},
                      json={'content':'Rename again','page_target':target})
    assert stale.status_code==409 and stale.json()['detail']['code']=='result_page_version_changed'
    # Both a retry and the delayed original carry the same stale target and key.
    # BEGIN IMMEDIATE + key lookup precedes the Page-version check.
    late_headers={**own,'Idempotency-Key':'page-delayed'}
    late={'content':'Rewrite selection','page_target':target,'page_selection':selection}
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    barrier=Barrier(2)
    def late_submit(_):
        barrier.wait(timeout=5)
        return client.post(f'/api/v1/conversations/{room}/events',headers=late_headers,json=late)
    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts=list(pool.map(late_submit,range(2)))
    for rejected in attempts:
        assert rejected.status_code==409 and rejected.json()['detail']['code']=='result_page_version_changed'
    assert client.get(f'/api/v1/conversations/{room}/page-message/receipt',headers=late_headers).status_code==404
    denied=client.get(f'/api/v1/conversations/{room}/page-message/receipt',
                      headers={**headers(client,other),'Idempotency-Key':'page-edit-one'})
    assert denied.status_code==404
    oversized=client.post(f'/api/v1/conversations/{room}/context-preview',headers=own,
                          json={'content':'x'*12000,'page_target':{'result_id':result_id,'version':2}})
    assert oversized.status_code==422 and oversized.json()['detail']['code']=='result_page_context_too_large'

