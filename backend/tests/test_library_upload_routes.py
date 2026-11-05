"""Synthetic in-process route tests; no user authentication or service calls."""
import hashlib
from types import SimpleNamespace
from urllib.parse import quote
from uuid import uuid4
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from app.api.routes import library_uploads as api
from app.api.routes.auth import get_current_user
from app.core.database import get_db
from app.services.library_uploads import LibraryUploads

class Db:
    def rollback(self): pass
    def expire_all(self): pass
    def get(self,*args): return SimpleNamespace(is_active=True)

@pytest.fixture
def client(tmp_path,monkeypatch):
    tmp_path.chmod(0o700); app=FastAPI();app.include_router(api.router,prefix='/bots')
    state=SimpleNamespace(owner=1,epoch='epoch')
    app.state.owner_epoch_provider=SimpleNamespace(current=lambda owner:state.epoch if owner==state.owner else None)
    app.state.local_executor=SimpleNamespace(library_uploads=LibraryUploads(tmp_path))
    app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(id=state.owner)
    app.dependency_overrides[get_db]=lambda:Db()
    def owned(db,owner,bot):
        if owner!=1 or bot!=7:raise HTTPException(404,'Bot unavailable')
    monkeypatch.setattr(api,'owned_agent',owned)
    with TestClient(app) as c:yield c,state

def request_headers(data,name='보고서.pdf'):
    return {'content-type':'application/octet-stream','X-Upload-Filename':quote(name,safe=''),
            'X-Upload-SHA256':hashlib.sha256(data).hexdigest()}

def test_upload_recovery_conflict_binary_and_tombstone(client):
    c,state=client;id=str(uuid4());path=f'/bots/7/library/uploads/{id}';data=b'%PDF\x00\xff'
    first=c.put(path,content=data,headers=request_headers(data));assert first.status_code==201,first.text
    item=first.json();assert item['filename']=='보고서.pdf' and item['origin']=='owner_upload' and not item['removed']
    assert set(item)=={'upload_id','bot_id','filename','size_bytes','sha256','created_at','media_type','origin','removed'}
    assert c.put(path,content=data,headers=request_headers(data)).status_code==200
    assert c.put(path,content=b'other',headers=request_headers(b'other')).status_code==409
    assert c.get(path).json()==item and c.get('/bots/7/library/uploads').json()['items']==[item]
    response=c.get(path+'/file');assert response.content==data and response.headers['content-type']=='application/octet-stream'
    assert response.headers['cache-control']=='no-store' and response.headers['x-content-type-options']=='nosniff'
    assert response.headers['content-disposition'].startswith("attachment; filename*=UTF-8''")
    state.owner=2;assert c.get(path).status_code==404 and c.delete(path).status_code==404
    state.owner=1;assert c.delete(path).json()['removed'] is True
    assert c.delete(path).json()['removed'] is True and c.get(path+'/file').status_code==410
    assert c.put(path,content=data,headers=request_headers(data)).status_code==410

def test_zero_byte_and_bounded_validation(client):
    c,_=client;path=f'/bots/7/library/uploads/{uuid4()}'
    assert c.put(path,content=b'',headers=request_headers(b'',name='empty')).status_code==201
    assert c.put('/bots/7/library/uploads/INVALID',content=b'',headers=request_headers(b'')).status_code==422
    assert c.put(f'/bots/7/library/uploads/{uuid4()}',content=b'',headers={**request_headers(b''),'X-Upload-Filename':'..%2Fsecret'}).status_code==422
    assert c.put(f'/bots/7/library/uploads/{uuid4()}',content=b'',headers={**request_headers(b''),'X-Upload-SHA256':'0'*64}).status_code==422
    assert c.put(f'/bots/7/library/uploads/{uuid4()}',content=b'',headers={**request_headers(b''),'content-length':str(api.MAX_BYTES+1)}).status_code==413

@pytest.mark.asyncio
async def test_owner_epoch_change_during_stream_never_persists(monkeypatch):
    state=SimpleNamespace(epoch='first',calls=0)
    class Request:
        headers={'content-type':'application/octet-stream'}
        app=SimpleNamespace(state=SimpleNamespace(owner_epoch_provider=SimpleNamespace(current=lambda _:state.epoch),
             local_executor=SimpleNamespace(library_uploads=SimpleNamespace(put=lambda *args:setattr(state,'calls',state.calls+1)))))
        async def stream(self):
            yield b'x';state.epoch='second'
    monkeypatch.setattr(api,'owned_agent',lambda *args:None)
    with pytest.raises(HTTPException) as caught:
        await api.put_upload(7,str(uuid4()),Request(),quote('x'),hashlib.sha256(b'x').hexdigest(),Db(),SimpleNamespace(id=1))
    assert caught.value.status_code==409 and state.calls==0

def test_listing_keyset_and_foreign_bot(client):
    c,_=client; ids=sorted(str(uuid4()) for _ in range(3))
    for id in ids:
        assert c.put(f'/bots/7/library/uploads/{id}',content=b'x',headers=request_headers(b'x')).status_code==201
    page=c.get('/bots/7/library/uploads?limit=2').json()
    assert [x['upload_id'] for x in page['items']]==ids[:2] and page['next_cursor']==ids[1]
    assert [x['upload_id'] for x in c.get('/bots/7/library/uploads',params={'after':page['next_cursor']}).json()['items']]==ids[2:]
    assert c.get('/bots/8/library/uploads').status_code==404

@pytest.mark.asyncio
async def test_stream_over_limit_stops_before_store(monkeypatch):
    state=SimpleNamespace(calls=0)
    class Request:
        headers={'content-type':'application/octet-stream'}
        app=SimpleNamespace(state=SimpleNamespace(owner_epoch_provider=SimpleNamespace(current=lambda _:'epoch'),
             local_executor=SimpleNamespace(library_uploads=SimpleNamespace(put=lambda *args:setattr(state,'calls',state.calls+1)))))
        async def stream(self):
            yield b'aa';yield b'bb'
    monkeypatch.setattr(api,'owned_agent',lambda *args:None);monkeypatch.setattr(api,'MAX_BYTES',3)
    with pytest.raises(HTTPException) as caught:
        await api.put_upload(7,str(uuid4()),Request(),quote('x'),hashlib.sha256(b'aabb').hexdigest(),Db(),SimpleNamespace(id=1))
    assert caught.value.status_code==413 and state.calls==0
