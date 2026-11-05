"""Only fixed non-secret MCP stage/codes reach diagnostics."""
from types import SimpleNamespace
import pytest
from test_room_state import room_client
from test_mcp_delegated import bind_tool
from app.services import mcp_registry
from app.services.llm_service import LLMFailure
from app.services.diagnostic_detail import make_diagnostic


@pytest.mark.asyncio
@pytest.mark.parametrize(('at','returned','expected'),[
    ('configure','mcp_configuration_requires_revoke','mcp_configuration_requires_revoke'),
    ('bind','mcp_binding_requires_revoke','mcp_binding_requires_revoke'),
    ('configure','private-raw-argument-secret','unavailable'),
])
async def test_safe_sync_reason(room_client,at,returned,expected):
    client,(owner,_,_,agent,_)=room_client
    with client.app.state.session_factory() as db:bind_tool(db,owner,agent)
    calls=[]
    class Bridge:
        available=True;core_epoch='b'*64
        async def configure(self,p):
            calls.append('configure');return {'state':'failed','code':returned} if at=='configure' else {'state':'configured'}
        async def bind(self,p):calls.append('bind');return {'state':'failed','code':returned}
    app=SimpleNamespace(session_factory=client.app.state.session_factory,mcp_client=Bridge(),owner_epoch_provider=SimpleNamespace(current=lambda _: 'c'*64))
    with pytest.raises(mcp_registry.MCPSyncFailure) as captured:await mcp_registry.sync_for_agent(app,owner,agent)
    exc=captured.value;assert (exc.stage,exc.safe_code)==(at,expected)
    assert calls==(['configure'] if at=='configure' else ['configure','bind'])
    failure=LLMFailure('mcp_transport_unavailable','Unavailable');failure.mcp_sync_diagnostic={'stage':exc.stage,'code':exc.safe_code}
    detail=make_diagnostic(failure,stage='context_build',failure_code=failure.code,room_id=1,agent_id=agent,connection_id=None,trigger_event_id=None,action_id=None)
    assert detail['mcp_sync']=={'stage':at,'code':expected}
    if expected=='unavailable':assert returned not in str(detail)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['success','owner_changed','binding_changed'])
async def test_epoch_only_refresh_is_one_server_and_rechecks_frozen_state(room_client,mode):
    from app.models.mcp import MCPBinding
    from app.models.models import Agent
    client,(owner,_,_,agent,_)=room_client
    with client.app.state.session_factory() as db:
        meta=bind_tool(db,owner,agent)
        second=Agent(user_id=owner,name='Synthetic second',description='',system_prompt='')
        db.add(second);db.flush();db.add(MCPBinding(owner_id=owner,agent_id=second.id,server_id=meta['server_id'],
            revision=1,enabled=True,tool_ids=[meta['tool_id']]));db.commit();second_id=second.id
    calls=[];epoch=['c'*64]
    class Bridge:
        available=True;core_epoch='b'*64
        async def configure(self,p):
            calls.append(('configure',p['server_id']))
            return {'state':'failed','code':'mcp_configuration_epoch_changed'} if len([x for x in calls if x[0]=='configure'])==1 else {'state':'configured'}
        async def revoke(self,p):
            calls.append(('revoke',p['server_id']))
            if mode=='owner_changed':epoch[0]='d'*64
            if mode=='binding_changed':
                with client.app.state.session_factory() as db:
                    row=db.get(MCPBinding,(owner,agent,meta['server_id']));row.revision+=1;db.commit()
            return {'state':'revoked'}
        async def bind(self,p):calls.append(('bind',p['agent_id']));return {'state':'bound'}
    app=SimpleNamespace(session_factory=client.app.state.session_factory,mcp_client=Bridge(),owner_epoch_provider=SimpleNamespace(current=lambda _:epoch[0]))
    if mode=='success':
        await mcp_registry.sync_for_agent(app,owner,agent)
        assert calls==[('configure',meta['server_id']),('revoke',meta['server_id']),('configure',meta['server_id']),('bind',agent),('bind',second_id)]
    else:
        with pytest.raises(mcp_registry.MCPSyncFailure) as captured:await mcp_registry.sync_for_agent(app,owner,agent)
        assert captured.value.stage==('epoch' if mode=='owner_changed' else 'bind')
        assert calls==[('configure',meta['server_id']),('revoke',meta['server_id'])]
