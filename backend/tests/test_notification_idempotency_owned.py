"""Synthetic verification of duplicate notification setting requests."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
from fastapi import HTTPException
from test_room_state import room_client
from app.schemas.local_notification import SettingWrite
from app.services import local_notification
from app.models.local_notification import LocalNotificationSetting,LocalNotificationCommand

def test_same_operation_concurrent_writes_have_one_commit(room_client):
    client,(owner,_,_,agent,_)=room_client
    op=str(uuid4());request=SettingWrite(expected_owner_epoch='a'*64,operation_id=op,expected_revision=0,completion=True,failure=False,approval=False)
    barrier=Barrier(2)
    def invoke():
        with client.app.state.session_factory() as db:
            barrier.wait(timeout=5)
            try:return local_notification.write_setting(db,owner,agent,request,lambda:None)['state']
            except HTTPException as exc:return exc.detail['code']
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(lambda _:invoke(),range(2)))
    assert sorted(results)==['notification_operation_exists','saved']
    with client.app.state.session_factory() as db:
        assert db.get(LocalNotificationSetting,(owner,agent)).revision==1
        assert db.query(LocalNotificationCommand).filter_by(owner_id=owner,operation_id=op).count()==1
        assert local_notification.command(db,owner,op)['revision']==1

def test_exact_receipt_rejects_changed_payload_and_superseded_is_terminal(room_client):
    import pytest
    client,(owner,_,_,agent,_)=room_client
    op=str(uuid4());base={'expected_owner_epoch':'a'*64,'operation_id':op,'expected_revision':0,'completion':True,'failure':False,'approval':False}
    request=SettingWrite(**base)
    with client.app.state.session_factory() as db:
        assert local_notification.resolve_setting_command(db,owner,agent,request,lambda:None)['state']=='pending'
        local_notification.write_setting(db,owner,agent,request,lambda:None)
        assert local_notification.resolve_setting_command(db,owner,agent,request,lambda:None)['state']=='saved'
        for field,value in [('failure',True),('expected_owner_epoch','b'*64)]:
            wrong=SettingWrite(**{**base,field:value})
            with pytest.raises(HTTPException) as exc:local_notification.resolve_setting_command(db,owner,agent,wrong,lambda:None)
            assert exc.value.detail['code']=='notification_operation_mismatch'
        missing=SettingWrite(**{**base,'operation_id':str(uuid4())})
        assert local_notification.resolve_setting_command(db,owner,agent,missing,lambda:None)['state']=='superseded'
        with pytest.raises(HTTPException) as exc:local_notification.write_setting(db,owner,agent,missing,lambda:None)
        assert exc.value.detail['code']=='notification_revision_conflict'
