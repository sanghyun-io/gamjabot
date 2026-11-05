"""Four focused fake-resource checks; no OS process/browser/native action."""
import asyncio
from datetime import datetime,timedelta,timezone
import json
import pytest
from types import SimpleNamespace
from uuid import uuid4

from app.services.resource_binding import freeze_binding
from app.services.tools import LocalExecutor
from app.services.tools.browser import BrowserExecutor,WorkflowAuthority
from app.services.tools.desktop import DesktopExecutor,NativeDesktopClient
from app.services.tools.process import ProcessExecutor
from app.services.tool_broker import ActionResult


def proof(ids,*,generation=1):
    return {'version':1,'root_scope_id':ids[0],'scope_id':ids[-1], 'owner_id':1,
        'root_room_id':1,'root_request_event_id':str(uuid4()),'execution_room_id':2,
        'agent_id':2,'run_generation':generation,'job_id':str(uuid4()),'phase':'response',
        'context_mode':'delegated','deadline_at':(datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat(),
        'lineage':[{'scope_id':value,'epoch':0} for value in ids]}


def test_branch_stop_closes_descendants_but_not_parent_sibling_or_manual_browser():
    async def scenario():
        root,b,c,d=[str(uuid4()) for _ in range(4)]
        inputs=[proof([root,b]),proof([root,b,c]),proof([root,d])]
        frozen=[freeze_binding(value) for value in inputs]
        inputs[0]['lineage'][0]['epoch']=99
        assert frozen[0].lineage[0][1]==0  # Retained authority is not a mutable dict alias.
        authority=WorkflowAuthority(None,2,2,execution_binding=frozen[0])
        assert not authority._live(SimpleNamespace(execution_binding={'malformed':True}))
        processes=ProcessExecutor();closed=[]
        processes.running={str(i):SimpleNamespace(execution_binding=value) for i,value in enumerate(frozen)}
        async def stop(owned):closed.append(owned.execution_binding)
        processes._stop=stop
        browser=BrowserExecutor();browser.workflows={str(i):SimpleNamespace(execution_binding=value,controller='agent') for i,value in enumerate(frozen)}
        manual=SimpleNamespace(execution_binding=frozen[1],controller='user');browser.workflows['manual']=manual
        browser_closed=[]
        async def close(key,session):browser_closed.append(session)
        browser._close=close
        await processes.stop_scopes([{'scope_id':c,'epoch':1}])
        assert closed==[frozen[1]]  # C Stop preserves B's return scope.
        closed.clear()
        await processes.stop_scopes([{'scope_id':b,'epoch':1}])
        await browser.stop_scopes([{'scope_id':b,'epoch':1}])
        assert closed==frozen[:2] and len(browser_closed)==2 and manual not in browser_closed
    asyncio.run(scenario())


def test_stop_withdraws_inflight_start_before_reporting_settled(monkeypatch):
    async def scenario():
        root,b=[str(uuid4()) for _ in range(2)];binding=freeze_binding(proof([root,b]))
        executor=LocalExecutor.__new__(LocalExecutor)
        executor.sessions=None;executor._scope_calls={};executor._scope_stops={}
        stopped=[]
        async def resources(bindings):stopped.append(bindings)
        async def native(bindings):return [{**item,'state':'settled'} for item in bindings]
        executor.processes=SimpleNamespace(stop_scopes=resources)
        executor.browser=SimpleNamespace(stop_scopes=resources)
        executor.desktop=SimpleNamespace(scoped_execution_ready=True,stop_scopes=native)
        entered=asyncio.Event();cancel=asyncio.Event()
        async def effect(action,grant,token):
            entered.set();await token.wait();return ActionResult('cancelled','Fake start withdrawn.')
        executor._execute=effect
        monkeypatch.setattr('app.services.resource_authority.require_scope_start',lambda *args:binding)
        active=asyncio.create_task(executor(None,None,cancel));await entered.wait()
        receipts=await executor.stop_scopes([{'scope_id':b,'epoch':1}])
        assert cancel.is_set() and active.done() and receipts[0]['state']=='settled'
        assert len(stopped)==2 and executor._scope_calls=={}
    asyncio.run(scenario())


def test_native_stop_waits_for_ack_and_never_releases_new_generation(tmp_path,monkeypatch):
    async def scenario():
        root,b,c=[str(uuid4()) for _ in range(3)]
        binding=freeze_binding(proof([root,b,c]));ack=asyncio.Event()
        class Native:
            scope_capabilities=NativeDesktopClient.scope_capabilities
            def __init__(self):self.revoked={};self.calls=[]
            async def call(self,body,**kwargs):
                self.calls.append(body)
                if body['operation']=='scope_capabilities':
                    return {'state':'completed','text':json.dumps({'capability':'root_scope_revocation_v1'})}
                assert body['operation']=='scope_revoke'
                await ack.wait()
                self.revoked.update({item['scope_id']:item['epoch'] for item in body['revocations']})
                return {'state':'completed','text':json.dumps({'capability':'root_scope_revocation_v1',
                    'revocations':body['revocations'],'released_lease_id':'owned-lease'})}
        native=Native();desktop=DesktopExecutor(str(tmp_path/'unused.sqlite'),native=native)
        released=[]
        async def release(*owner):released.append(owner);desktop.lease._owner=None;return True
        desktop.lease=SimpleNamespace(_owner=(2,2,'workflow','session'),_lease_id='owned-lease',release=release)
        desktop._scope_binding=binding;desktop._scope_lease_id='owned-lease'
        assert await desktop.prepare_scoped_execution()
        await desktop.release_turn(2,2,generation=0)
        await desktop.release_turn(2,2,generation=1,workflow_id='other')
        assert released==[]
        monkeypatch.setattr('app.services.tools.desktop.RELEASE_WAIT_SECONDS',.01)
        pending=await desktop.stop_scopes([{'scope_id':b,'epoch':1}])
        assert pending[0]['state']=='unknown' and released==[]
        ack.set()
        done=await desktop.stop_scopes([{'scope_id':b,'epoch':1}])
        assert done[0]['state']=='settled' and len(released)==1
        assert binding.affected(native.revoked) and not freeze_binding(proof([root,str(uuid4())])).affected(native.revoked)
        assert all(call['operation']!='action' for call in native.calls)
    asyncio.run(scenario())


def test_missing_capability_and_persistent_profile_requests_fail_closed(monkeypatch):
    async def scenario():
        binding=freeze_binding(proof([str(uuid4()),str(uuid4())]))
        from app.services.resource_authority import require_scope_start
        from app.services.action_review import ReviewChanged
        with pytest.raises(ReviewChanged):
            require_scope_start(None,SimpleNamespace(execution_binding=binding.as_dict()),
                SimpleNamespace(execution_binding=None,review=None))
        monkeypatch.setattr('app.services.resource_authority.require_scope_start',lambda *args:binding)
        local=LocalExecutor.__new__(LocalExecutor);local.sessions=None
        local.desktop=SimpleNamespace(scoped_execution_ready=False)
        result=await local(None,None,asyncio.Event())
        assert result.error_code=='native_capability_unavailable'
        browser=BrowserExecutor()
        browser.profiles.binding=lambda *a:(_ for _ in ()).throw(AssertionError('Private profile must not be read'))
        action=SimpleNamespace(room_id=2,agent_id=2,arguments={'url':'https://example.invalid','profile_id':'explicit-private'})
        grant=SimpleNamespace(review=SimpleNamespace(targets=[SimpleNamespace(profile='persistent_room',profile_id='private')]))
        result=await browser(action,grant,asyncio.Event())
        assert result.error_code=='review_changed' and browser.workflows=={}
    asyncio.run(scenario())
