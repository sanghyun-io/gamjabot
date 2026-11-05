"""Discovery must finish after its request-scoped Owner session has closed."""
import asyncio
import json
from types import SimpleNamespace
import pytest
from test_room_state import room_client, headers
from app.api.routes.mcp import discover
from app.models.models import User
from app.schemas.mcp import MCPDiscover
from app.services import mcp_registry


@pytest.mark.asyncio
@pytest.mark.parametrize(('outcome','expected'), [('completed','completed'),('failed','failed'),('uncertain','unknown'),('exception','unknown')])
async def test_delayed_discovery_after_owner_session_closes(room_client,monkeypatch,outcome,expected):
    client,(owner,_,_,_,_)=room_client
    auth=headers(client,owner)
    made=client.post('/api/v1/mcp/servers',headers={**auth,'Idempotency-Key':'detached-create'},
        json={'name':'Synthetic','enabled':True,'transport':{'type':'http','url':'https://example.invalid/mcp'}})
    assert made.status_code==200
    sid=made.json()['server']['server_id']
    preview=client.post(f'/api/v1/mcp/servers/{sid}/discovery-preview',headers=auth,json={'expected_revision':1}).json()
    started=asyncio.Event();release=asyncio.Event()
    class Bridge:
        available=True;core_epoch='b'*64
        async def close(self):pass
        async def configure(self,p):return {'state':'configured'}
        async def bind(self,p):return {'state':'bound'}
        async def revoke(self,p):return {'state':'revoked'}
        async def issue(self,p):return {'state':'issued'}
        async def start(self,p):return {'state':'completed'}
        async def cancel(self,p):return {'state':'cancel_requested'}
        async def credential_status(self,p):return {'state':'missing'}
        async def discover(self,p):
            started.set();await release.wait()
            if outcome=='exception':raise RuntimeError('synthetic')
            schema={'type':'object','properties':{},'additionalProperties':False}
            return {'state':outcome,'result':{'tools':[{'name':'echo','description':'Synthetic',
                'input_schema':schema,'schema_canonical':mcp_registry.canonical(schema),
                'schema_sha256':mcp_registry.digest(schema)}]}}
    client.app.state.mcp_client=Bridge()
    monkeypatch.setattr(client.app.state.owner_epoch_provider,'current',lambda _: 'c'*64)
    db=client.app.state.session_factory();user=db.get(User,owner)
    response=await discover(sid,MCPDiscover(expected_revision=1,preview_id=preview['preview_id']),
        SimpleNamespace(app=client.app),key='detached-discover',db=db,owner=user)
    assert json.loads(response.body)['discovery']['state']=='running'
    db.close()
    await asyncio.wait_for(started.wait(),1)
    tasks=list(client.app.state.mcp_discovery_tasks);assert len(tasks)==1
    release.set();await asyncio.wait_for(asyncio.gather(*tasks),2)
    with client.app.state.session_factory() as read_db:
        row=mcp_registry.command(read_db,owner,'detached-discover')['discovery']
        assert row['state']==expected
        assert row['failure_code']==('mcp_discovery_failed' if expected=='failed' else 'mcp_discovery_uncertain' if expected=='unknown' else None)
        if expected=='completed':assert [t['name'] for t in mcp_registry.tools(read_db,owner,sid)['items']]==['echo']
