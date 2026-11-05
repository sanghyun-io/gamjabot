"""Focused synthetic delegation and MCP scope revocation contracts."""
from types import SimpleNamespace
from uuid import uuid4
import pytest
from test_room_state import room_client
from test_execution_scopes import seed, FakeExecutor
from app.models.collaboration import RoomAgentRun, ToolAction
from app.models.execution_scope import ExecutionScope
from app.models.mcp import MCPServer,MCPTool,MCPBinding
from app.services import execution_scopes as scopes, mcp_registry, room_service, teammates
from app.services.mcp_execution import require_origin
from app.services.action_review import ReviewChanged
from app.services.tool_broker import ToolBroker


def bind_tool(db,owner,agent):
    now=mcp_registry.now();sid=str(uuid4());schema={'type':'object','properties':{'message':{'type':'string'}},'required':['message']}
    digest=mcp_registry.digest(schema);tid=mcp_registry.digest([sid,1,'echo',digest])
    db.add(MCPServer(server_id=sid,owner_id=owner,revision=1,name='Synthetic',enabled=True,transport={'type':'http','url':'https://example.invalid/mcp'},created_at=now,updated_at=now));db.flush()
    db.add(MCPTool(tool_id=tid,server_id=sid,server_revision=1,name='echo',description='Synthetic',input_schema=schema,schema_canonical=mcp_registry.canonical(schema),schema_sha256=digest))
    db.add(MCPBinding(owner_id=owner,agent_id=agent,server_id=sid,revision=1,enabled=True,tool_ids=[tid]));db.commit()
    return mcp_registry.offered(db,owner,agent)[0]['mcp_binding']


def test_cross_bot_scope_receiver_binding_and_stale(room_client):
    client,(owner,other,project,_,_)=room_client;s=seed(client,owner,project);broker=ToolBroker(client.app.state.session_factory,FakeExecutor())
    with client.app.state.session_factory() as db:
        meta=bind_tool(db,owner,s.ben)
        prepared=broker.prepare_mcp(db,owner_id=owner,agent_id=s.ben,room_id=s.personal[s.ben],root_id=s.root,generation=s.gen,meta=meta,arguments={'message':'synthetic'},execution_binding=s.proof)
        assert prepared.family=='mcp_tool_call' and prepared.mcp_binding['agent_id']==s.ben
        with pytest.raises(ReviewChanged):require_origin(db,owner_id=other,room_id=s.personal[s.ben],agent_id=s.ben,root_id=s.root,generation=s.gen,execution_binding=s.proof)
        with pytest.raises(ValueError):broker.prepare_mcp(db,owner_id=owner,agent_id=s.cy,room_id=s.personal[s.cy],root_id=s.root,generation=s.gen,meta=meta,arguments={'message':'synthetic'},execution_binding=s.proof)
        scope=db.get(ExecutionScope,s.proof['scope_id']);scope.state='revoked';scope.epoch+=1;db.commit()
        with pytest.raises(ReviewChanged):require_origin(db,owner_id=owner,room_id=s.personal[s.ben],agent_id=s.ben,root_id=s.root,generation=s.gen,execution_binding=s.proof)


def test_same_room_handoff_requires_exact_provenance(room_client):
    client,(owner,_,project,_,_)=room_client;s=seed(client,owner,project)
    with client.app.state.session_factory() as db:
        sender=db.get(RoomAgentRun,(s.room,s.ada));receiver=db.get(RoomAgentRun,(s.room,s.dana));receiver.state='idle';db.commit()
        teammates.send(db,s.room,s.root,s.ada,sender.generation,'same-room',{'agent_id':s.dana,'content':'Synthetic task'})
        job=teammates.next_job(db,s.room,s.root)
        gen=room_service.begin_turn(db,s.room,s.dana,trigger_event_id=s.root,round_number=1,watermark=1)
        proof=scopes.bind(db,s.room,s.dana,s.root,gen,teammate_job=job);db.commit()
        binding={'handoff_id':job['handoff_id'],'phase':job['phase']}
        require_origin(db,owner_id=owner,room_id=s.room,agent_id=s.dana,root_id=s.root,generation=gen,execution_binding=proof,teammate_binding=binding)
        with pytest.raises(ReviewChanged):require_origin(db,owner_id=owner,room_id=s.room,agent_id=s.dana,root_id=s.root,generation=gen,execution_binding=proof)
        with pytest.raises(ReviewChanged):require_origin(db,owner_id=owner,room_id=s.room,agent_id=s.dana,root_id=s.root,generation=gen,execution_binding=proof,teammate_binding={**binding,'handoff_id':str(uuid4())})


@pytest.mark.asyncio
@pytest.mark.parametrize(('ack','started'), [(True,False),(False,False),(True,True)])
async def test_stop_requires_mcp_ack_only_for_related_scope(room_client,ack,started):
    client,(owner,_,project,_,_)=room_client;s=seed(client,owner,project);executor=FakeExecutor();calls=[]
    class Bridge:
        available=True;core_epoch='b'*64
        async def scope_revoke(self,p):calls.append(p);return {'state':'revoked' if ack else 'failed'}
    app=SimpleNamespace(session_factory=client.app.state.session_factory,local_executor=executor,mcp_client=Bridge(),owner_epoch_provider=SimpleNamespace(current=lambda _: 'c'*64))
    from sqlalchemy import text
    with app.session_factory() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        require_origin(db,owner_id=owner,room_id=s.personal[s.ben],agent_id=s.ben,root_id=s.root,generation=s.gen,execution_binding=s.proof)
        action=ToolAction(action_id=str(uuid4()),room_id=s.personal[s.ben],agent_id=s.ben,trigger_event_id=s.root,call_id='synthetic',family='mcp_tool_call',arguments={},state='started' if started else 'waiting',
            started_at=mcp_registry.now() if started else None,execution_binding=s.proof)
        db.add(action);db.commit()
    with app.session_factory() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        row=db.get(ExecutionScope,s.proof['scope_id']);row.epoch+=1;row.state='revoked';db.commit()
        stop_id=scopes._stop_receipt(db,s.room,[{'scope_id':row.scope_id,'epoch':row.epoch}])
    with app.session_factory() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        with pytest.raises(ReviewChanged):require_origin(db,owner_id=owner,room_id=s.personal[s.ben],agent_id=s.ben,root_id=s.root,generation=s.gen,execution_binding=s.proof)
        db.rollback()
    result=await scopes.confirm_stop(app,stop_id,owner)
    assert result['state']==('settled' if ack and not started else 'unknown') and len(calls)==1
    assert calls[0]['scopes']==result['revocations']


@pytest.mark.parametrize('cross',[True,False])
def test_dispatch_mcp_once_returns_to_sender(room_client,monkeypatch,cross):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from test_room_state import headers,create_room
    from test_teammates import team,send_call
    from test_bot_messages import cross_team,call_bot
    from app.services.providers import ProviderResult,ToolCall
    client,(owner,_,project,_,_)=room_client;auth=headers(client,owner)
    if cross:room,(ada,ben),personal=cross_team(client,owner,project)
    else:
        room=create_room(client,owner,project,'Synthetic teammates');ada,ben=team(client,owner,room,('Ada','Ben'));personal={ben:room}
    calls=[]
    class Bridge:
        available=True;core_epoch='b'*64
        async def close(self):pass
        async def configure(self,p):return {'state':'configured'}
        async def bind(self,p):return {'state':'bound'}
        async def revoke(self,p):return {'state':'revoked'}
        async def scope_revoke(self,p):return {'state':'revoked'}
        async def credential_status(self,p):return {'state':'missing'}
        async def cancel(self,p):return {'state':'cancel_requested'}
        async def discover(self,p):return {'state':'failed'}
        async def issue(self,p):calls.append(('issue',p));return {'state':'issued','nonce':'a'*64}
        async def start(self,p):calls.append(('start',p));return {'state':'completed','result':{'content':'SYNTHETIC_MCP_RESULT'}}
    bridge=Bridge();client.app.state.mcp_client=bridge
    monkeypatch.setattr(client.app.state.owner_epoch_provider,'current',lambda _: 'c'*64)
    executor=FakeExecutor();executor.scoped_execution_ready=False
    broker=ToolBroker(client.app.state.session_factory,executor,owner_epochs=SimpleNamespace(current=lambda _:'c'*64))
    client.app.state.scheduler.broker=broker
    with client.app.state.session_factory() as db:bind_tool(db,owner,ben)
    async def provider(request):
        if request.agent_id==ben:
            if request.observations:return ProviderResult(text='BEN_GOT_SYNTHETIC_MCP_RESULT')
            tools=[t for t in request.tools if t['name'].startswith('mcp_')]
            assert len(tools)==1
            return ProviderResult(tool_calls=[ToolCall('mcp-delegated',tools[0]['name'],{'message':'synthetic'})],continuation={'call_ids':['mcp-delegated']})
        if request.observations:return ProviderResult(text='Delegation started')
        if any('BEN_GOT_SYNTHETIC_MCP_RESULT' in str(e) for e in request.events):return ProviderResult(text='ADA_SYNTHETIC_FINAL')
        return call_bot(ben,'Delegate synthetic task') if cross else send_call(ben,'Delegate synthetic task')
    client.app.state.scheduler.provider=provider
    with ThreadPoolExecutor(max_workers=1) as pool:
        work=pool.submit(client.post,f'/api/v1/conversations/{room}/messages',headers=auth,json={'content':'@Ada ask Ben to use synthetic tool'})
        action_id=None
        for _ in range(250):
            with client.app.state.session_factory() as db:
                row=db.query(ToolAction).filter_by(room_id=personal[ben],agent_id=ben,family='mcp_tool_call',state='waiting').first()
                if row:action_id=row.action_id;break
            if work.done():break
            time.sleep(.02)
        assert action_id, work.result(timeout=2).text if work.done() else 'no scoped MCP action'
        preview=client.get(f'/api/v1/tools/rooms/{personal[ben]}/actions/{action_id}/preview',headers=auth)
        assert preview.status_code==200,preview.text
        value=preview.json();assert value['execution_binding']['context_mode']==('delegated' if cross else 'user_room')
        if not cross:assert value['mcp_binding']['teammate_binding']['phase']=='response'
        result=client.post(f'/api/v1/tools/rooms/{personal[ben]}/actions/{action_id}/decision',headers=auth,
            json={'decision':'approve','scope':'once','review_id':value['review_id'],'acknowledge_unverified_context':True})
        assert result.status_code==200,result.text
        response=work.result(timeout=8);assert response.status_code==201,response.text
    assert [v[0] for v in calls]==['issue','start']
    assert calls[0][1]['binding']['execution_binding']==value['execution_binding']
    assert calls[0][1]['review']['action']['execution_binding']==value['execution_binding']
    if not cross:assert calls[0][1]['binding']['teammate_binding']==value['mcp_binding']['teammate_binding']
    assert 'ADA_SYNTHETIC_FINAL' in response.text
