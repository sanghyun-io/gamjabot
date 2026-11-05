"""Disposable authority/IPC fixtures, never real native screen/input effects."""
import asyncio
from dataclasses import replace
from datetime import timedelta, timezone
import base64
import hashlib
import hmac
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_room_state import room_client
from test_review_preview import preview_fixture
from test_review_claim import inspected, locked_claim
from app.models.collaboration import ProviderConnection, RoomAgentRun, RoomGrant, ToolAction
from app.models.models import User
from app.services.action_review import ReviewChanged
from app.services.grants import now
from app.services.review_operation import operation_start
from app.services.tools.desktop import DesktopExecutor, NativeDesktopClient


class OwnedNative:
    context_id = "c" * 32
    def __init__(self):
        self.effects = 0
        self.after_acquire = None
        self.before_send = None
        self.deadline = None
    async def call(self, body, *, start_deadline_ms=None, preflight=None):
        if body['operation'] == 'lease_acquire' and self.after_acquire:
            self.after_acquire()
        if body['operation'] == 'action':
            if self.before_send:
                self.before_send()
            preflight()
            self.deadline = start_deadline_ms
            self.effects += 1
        return {'state':'completed','text':'synthetic input'}


def pending(preview_fixture, family='pointer_click'):
    client,owner,broker,path,proposal=preview_fixture
    native=OwnedNative()
    desktop=DesktopExecutor(str(path.parent/'owned-native.sqlite'),native=native,
        sessions=broker.sessions,owner_epochs=broker.owner_epochs,lock_path=str(path.parent/'host.lock'))
    # The lease manager uses the same schema as production, isolated here.
    import sqlite3
    with sqlite3.connect(desktop.lease.database_path) as db:
        db.execute('CREATE TABLE desktop_lease (id INTEGER PRIMARY KEY, room_id INTEGER, agent_id INTEGER, workflow_id TEXT, session_id TEXT, lease_id TEXT, acquired_at TEXT, expires_at TEXT)')
    args={'x':1,'y':1} if family.startswith('pointer_') else {'text':'owned transient fixture'}
    action=proposal(family,args,SimpleNamespace(desktop=desktop),'owned-native')
    with broker.sessions() as db:
        run=db.get(RoomAgentRun,(action.room_id,action.agent_id));run.generation=9
        run.trigger_event_id=action.trigger_event_id;run.state='idle';run.paused=False;db.commit()
    inspected(client,owner,broker,action)
    ref=locked_claim(broker,action)
    with broker.sessions() as db:
        row=db.get(ToolAction,action.action_id);row.state='started';row.started_at=now();db.commit()
    action.arguments=broker._previews[action.action_id].raw_arguments
    return owner,broker,action,ref,desktop,native


@pytest.mark.parametrize('change',['missing','context','payload','review','connection','owner','run','consent','completed'])
def test_final_native_guard_rejects_stale_original_context_or_authority(preview_fixture,change):
    owner,broker,action,ref,desktop,native=pending(preview_fixture)
    assert operation_start(action,ref,sessions=broker.sessions,owner_epochs=broker.owner_epochs,
                           context_id=native.context_id)>0
    context=native.context_id
    if change=='missing':ref=replace(ref,review=None)
    elif change=='context':context='d'*32
    elif change=='payload':action.arguments={'x':2,'y':1}
    elif change=='review':action.review_id=str(uuid4())
    else:
        with broker.sessions() as db:
            if change=='connection':db.get(ProviderConnection,ref.review.authority.proposer.connection_id).readiness_revision+=1
            elif change=='owner':db.get(User,owner).is_active=False
            elif change=='run':db.get(RoomAgentRun,(action.room_id,action.agent_id)).generation+=1
            elif change=='completed':db.get(ToolAction,action.action_id).state='completed'
            else:
                g=db.get(RoomGrant,ref.grant_id);g.scope={**g.scope,'acknowledge_unverified_context':False}
            db.commit()
    with pytest.raises(ReviewChanged):
        operation_start(action,ref,sessions=broker.sessions,owner_epochs=broker.owner_epochs,context_id=context)
    assert native.effects==0


def test_opaque_deadline_is_original_approval_time_not_new_dispatch_time(preview_fixture,monkeypatch):
    _,broker,action,ref,_,native=pending(preview_fixture)
    clock=ref.approved_at+timedelta(seconds=59)
    monkeypatch.setattr('app.services.review_operation.now',lambda:clock)
    expected=int((ref.approved_at+timedelta(seconds=60)).replace(tzinfo=timezone.utc).timestamp()*1000)
    assert operation_start(action,ref,sessions=broker.sessions,owner_epochs=None,context_id=native.context_id)==expected
    clock+=timedelta(seconds=1)
    with pytest.raises(ReviewChanged):operation_start(action,ref,sessions=broker.sessions,owner_epochs=None,context_id=native.context_id)
    with broker.sessions() as db:
        assert db.get(ToolAction,action.action_id).state=='started'
        assert db.get(RoomGrant,ref.grant_id).decision=='consumed'


def test_direct_fixture_cannot_substitute_a_different_os_identity(preview_fixture):
    import os
    _,_,action,ref,_,native=pending(preview_fixture)
    other=ref.review.model_copy(update={'authority':ref.review.authority.model_copy(update={'euid':os.geteuid()+1})})
    with pytest.raises(ReviewChanged):
        operation_start(action,replace(ref,review=other),sessions=None,owner_epochs=None,context_id=native.context_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary',['lease-await','send-await','lease-owner','adapter'])
async def test_desktop_rechecks_after_wait_without_effect_or_automatic_retry(preview_fixture,boundary):
    owner,broker,action,ref,desktop,native=pending(preview_fixture)
    def changed():
        if boundary=='lease-owner':
            import sqlite3
            with sqlite3.connect(desktop.lease.database_path) as db:db.execute('DELETE FROM desktop_lease WHERE id=1')
        elif boundary=='adapter':native.context_id='e'*32
        else:
            with broker.sessions() as db:
                db.get(ProviderConnection,ref.review.authority.proposer.connection_id).readiness_revision+=1;db.commit()
    if boundary=='lease-await':native.after_acquire=changed
    else:native.before_send=changed
    try:
        result=await desktop(action,ref,asyncio.Event())
        assert result.state=='failed' and result.error_code=='review_changed'
        assert native.effects==0
        with broker.sessions() as db:
            assert db.get(ToolAction,action.action_id).state=='started'
            assert db.get(RoomGrant,ref.grant_id).decision=='consumed'
    finally:
        await desktop.close()


@pytest.mark.asyncio
async def test_supported_once_input_dispatch_uses_same_review_and_deadline(preview_fixture):
    _,broker,action,ref,desktop,native=pending(preview_fixture)
    try:
        result=await desktop(action,ref,asyncio.Event())
        assert result.state=='completed' and native.effects==1
        assert native.deadline==int((ref.approved_at+timedelta(seconds=60)).replace(tzinfo=timezone.utc).timestamp()*1000)
    finally:await desktop.close()


@pytest.mark.asyncio
async def test_native_client_signed_existing_deadline_is_bounded_and_body_cannot_override(monkeypatch):
    client=NativeDesktopClient('/synthetic.sock','00'*32)
    deadline=int((now()+timedelta(seconds=2)).replace(tzinfo=timezone.utc).timestamp()*1000)
    packets=[]
    def transport(envelope,preflight=None,deadline_ms=None):
        preflight();packets.append(json.loads(envelope));return {'state':'completed'}
    monkeypatch.setattr(client,'_call_sync',transport)
    calls=[]
    await client.call({'operation':'action','deadline_ms':2**60},start_deadline_ms=deadline,
                      preflight=lambda:calls.append(True))
    raw=base64.b64decode(packets[0]['body']);body=json.loads(raw)
    assert body['deadline_ms']==deadline
    assert hmac.compare_digest(packets[0]['mac'],hmac.new(client.key,raw,hashlib.sha256).hexdigest())
    assert len(calls)==2


def test_native_transport_rechecks_after_connect_before_any_send(monkeypatch):
    from app.services.tools import desktop as module
    state={'connected':False,'sent':0}
    class Socket:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def settimeout(self,*_):pass
        def connect(self,*_):state['connected']=True
        def sendall(self,*_):state['sent']+=1;raise AssertionError('no action bytes')
    monkeypatch.setattr(module.socket,'socket',lambda *_:Socket())
    def preflight():
        if state['connected']:raise ReviewChanged('synthetic authority change during connect')
    client=NativeDesktopClient('/synthetic.sock','00'*32)
    with pytest.raises(ReviewChanged):client._call_sync(b'owned',preflight,2**63)
    assert state['sent']==0


def test_app_executor_threads_same_original_epoch_to_desktop(preview_fixture):
    client,*_=preview_fixture
    assert client.app.state.local_executor.desktop.sessions is client.app.state.session_factory
    assert client.app.state.local_executor.desktop.owner_epochs is client.app.state.scheduler.broker.owner_epochs
