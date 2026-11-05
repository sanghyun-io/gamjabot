"""Synthetic result adapter and in-process routes; no real account or runtime data."""
from datetime import datetime, timezone
from uuid import uuid4
import pytest
from app.models.collaboration import RoomEvent, ToolAction
from app.services.generated_files import GeneratedFiles
from test_room_results import results, headers, base

@pytest.fixture
def generated(results,tmp_path):
    client,ids=results
    tmp_path.chmod(0o700)
    store=GeneratedFiles(tmp_path);client.app.state.local_executor.generated_files=store
    action_id=str(uuid4()); event_id=str(uuid4());content='Synthetic example 한글\n'
    capture=store.capture(ids['owner'],ids['room'],action_id,'/synthetic/report.txt',content)
    assert capture is not None
    with client.app.state.session_factory() as db:
        db.add(ToolAction(action_id=action_id,room_id=ids['room'],agent_id=ids['author'],family='write_file',
            arguments={},state='completed',trigger_event_id=ids['request'],
            observation={'state':'completed','generated_file':capture.public}))
        db.add(RoomEvent(event_id=event_id,room_id=ids['room'],seq=7,actor_type='tool',actor_id=None,
            actor_name_snapshot='',kind='tool_result',payload={'action_id':action_id,'agent_id':ids['author'],
                'state':'completed','generated_file':capture.public},created_at=datetime.now(timezone.utc),
            idempotency_key='tool-result:'+action_id))
        db.commit()
    return client,ids,action_id,event_id,content,capture.public

def create(generated):
    client,ids,_,event_id,_,_=generated
    result=client.post(base(ids),json={'source_event_id':event_id,'kind':'file','title':'Saved file'},headers=headers(client,ids,'generated-save'))
    assert result.status_code==201,result.text
    return result.json()

def test_generated_file_owner_read_delete_and_tombstone(generated):
    client,ids,action_id,event_id,content,public=generated; result=create(generated)
    path=f"{base(ids)}/{result['result_id']}/versions/1"
    detail=client.get(path,headers=headers(client,ids)).json()
    assert detail['file_ref']=={'adapter':'generated_file','action_id':action_id,'event_seq':7,**public}
    assert detail['file_status']=='unchecked' and detail['author_name_snapshot']=='' and detail['review_support']=='unsupported'
    response=client.get(path+'/file',headers=headers(client,ids))
    assert response.status_code==200 and response.content==content.encode()
    assert response.headers['cache-control']=='no-store' and response.headers['x-content-type-options']=='nosniff'
    assert response.headers['content-disposition'].startswith("attachment; filename*=UTF-8''")
    assert client.get(path+'/file',headers=headers(client,ids,owner=ids['other'])).status_code==404
    assert client.delete(path+'/file',headers=headers(client,ids)).json()=={'removed':True}
    assert client.get(path+'/file',headers=headers(client,ids)).status_code==410
    assert client.get(path,headers=headers(client,ids)).json()['file_status']=='removed'
    assert client.delete(path+'/file',headers=headers(client,ids)).json()=={'removed':True}

def test_tampered_event_or_version_metadata_denied(generated):
    client,ids,action_id,event_id,_,public=generated; result=create(generated)
    path=f"{base(ids)}/{result['result_id']}/versions/1"
    with client.app.state.session_factory() as db:
        event=db.get(RoomEvent,event_id);event.payload={**event.payload,'generated_file':{**public,'sha256':'0'*64}};db.commit()
    assert client.get(path,headers=headers(client,ids)).json()['file_status']=='unavailable'
    assert client.get(path+'/file',headers=headers(client,ids)).status_code in (404,422)
    with client.app.state.session_factory() as db:
        event=db.get(RoomEvent,event_id);event.payload={**event.payload,'generated_file':public};event.idempotency_key='wrong';db.commit()
    assert client.get(path+'/file',headers=headers(client,ids)).status_code in (404,422)


def test_changed_version_file_ref_is_unavailable_and_cannot_delete(generated):
    from app.models.room_result import RoomResultVersion
    client,ids,action_id,event_id,_,_=generated;result=create(generated)
    path=f"{base(ids)}/{result['result_id']}/versions/1"
    with client.app.state.session_factory() as db:
        row=db.query(RoomResultVersion).filter_by(result_id=result['result_id'],version=1).one()
        row.file_ref={**row.file_ref,'sha256':'f'*64};db.commit()
    assert client.get(path,headers=headers(client,ids)).json()['file_status']=='unavailable'
    assert client.delete(path+'/file',headers=headers(client,ids)).status_code in (404,422)
    with client.app.state.session_factory() as db:
        action=db.get(ToolAction,action_id);assert action.observation.get('generated_file_status') is None

def test_delete_db_failure_keeps_tombstone_and_retry_repairs_status(generated,monkeypatch):
    from types import SimpleNamespace
    from app.api.routes.room_results import delete_generated_file
    client,ids,_,_,_,_=generated;result=create(generated)
    path=f"{base(ids)}/{result['result_id']}/versions/1"
    with client.app.state.session_factory() as db:
        monkeypatch.setattr(db,'commit',lambda: (_ for _ in ()).throw(RuntimeError('synthetic commit failure')))
        with pytest.raises(RuntimeError):
            delete_generated_file(ids['room'],result['result_id'],1,SimpleNamespace(app=client.app),db,SimpleNamespace(id=ids['owner']))
    assert client.get(path+'/file',headers=headers(client,ids)).status_code==410
    assert client.get(path,headers=headers(client,ids)).json()['file_status']=='unchecked'
    assert client.delete(path+'/file',headers=headers(client,ids)).json()=={'removed':True}
    assert client.get(path,headers=headers(client,ids)).json()['file_status']=='removed'
