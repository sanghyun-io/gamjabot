"""Synthetic MCP OAuth reference attachment; no real credential, Core or network."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from test_room_state import room_client, headers
from app.models.mcp import MCPBinding, MCPDiscovery, MCPServer


class Bridge:
    available=True
    core_epoch='b'*64
    def __init__(self): self.calls=[]; self.status='present'; self.on_status=None
    async def close(self): pass
    async def configure(self,p): self.calls.append(('configure',p)); return {'state':'configured'}
    async def bind(self,p): self.calls.append(('bind',p)); return {'state':'bound'}
    async def revoke(self,p): self.calls.append(('revoke',p)); return {'state':'revoked'}
    async def credential_status(self,p):
        self.calls.append(('status',p))
        if self.on_status: self.on_status()
        return {'state':self.status}
    async def issue(self,p): return {'state':'issued','nonce':'a'*64}
    async def start(self,p): return {'state':'uncertain'}
    async def cancel(self,p): return {'state':'cancel_requested'}
    async def discover(self,p): raise AssertionError('No discovery or external request')


@pytest.fixture
def notion_fixture(room_client, monkeypatch):
    client,(owner,other,_,agent,_)=room_client
    bridge=Bridge();client.app.state.mcp_client=bridge
    epoch={'value':'c'*64}
    monkeypatch.setattr(client.app.state.owner_epoch_provider,'current',lambda _:epoch['value'])
    def create(url='https://mcp.notion.com/mcp'):
        body={'name':'Synthetic MCP','enabled':True,'transport':{'type':'http','url':url}}
        response=client.post('/api/v1/mcp/servers',headers={**headers(client,owner),'Idempotency-Key':uuid4().hex},json=body)
        assert response.status_code==200
        return response.json()['server']
    return client,owner,other,agent,bridge,epoch,create


def attach(client,owner,server,ref,revision=1):
    return client.put(f"/api/v1/mcp/servers/{server['server_id']}/credential",headers=headers(client,owner),
                      json={'expected_revision':revision,'credential_ref':ref})


def test_oauth_reference_requires_core_proof_and_preserves_existing_cas_effects(notion_fixture):
    client,owner,_,agent,bridge,_,create=notion_fixture
    server=create();sid=server['server_id'];ref=f'mcp-oauth:{owner}:{sid}:'+('a'*32)
    now=datetime.now(timezone.utc)
    with client.app.state.session_factory() as db:
        db.add(MCPBinding(owner_id=owner,agent_id=agent,server_id=sid,revision=1,enabled=True,tool_ids=None))
        discovery=MCPDiscovery(discovery_id=str(uuid4()),owner_id=owner,server_id=sid,server_revision=1,
            preview_id=str(uuid4()),transport=server['transport'],state='preview',created_at=now,expires_at=now+timedelta(minutes=5))
        db.add(discovery);db.commit();discovery_id=discovery.discovery_id
    response=attach(client,owner,server,ref)
    assert response.status_code==200 and response.json()['revision']==2 and response.json()['credential_ref']==ref
    assert [item[0] for item in bridge.calls]==['configure','bind','status','revoke','configure']
    assert set(bridge.calls[2][1])=={'version','owner_id','owner_epoch','core_epoch','server_id','config_revision','credential_ref'}
    with client.app.state.session_factory() as db:
        assert db.get(MCPBinding,(owner,agent,sid)).enabled is False
        assert db.get(MCPDiscovery,discovery_id).state=='cancelled'


@pytest.mark.parametrize('which',['foreign_owner','foreign_server','malformed','wrong_endpoint','missing_proof','changed_owner','changed_core','stale_after_proof'])
def test_oauth_negative_proof_endpoint_and_cas(notion_fixture,which):
    client,owner,other,_,bridge,epoch,create=notion_fixture
    server=create('https://example.invalid/mcp' if which=='wrong_endpoint' else 'https://mcp.notion.com/mcp')
    sid=server['server_id'];ref=f'mcp-oauth:{owner}:{sid}:'+('a'*32)
    if which=='foreign_owner':ref=f'mcp-oauth:{other}:{sid}:'+('a'*32)
    elif which=='foreign_server':ref=f'mcp-oauth:{owner}:{uuid4()}:'+('a'*32)
    elif which=='malformed':ref=f'mcp-oauth:{owner}:{sid}:'+('A'*32)
    elif which=='missing_proof':bridge.status='missing'
    elif which=='changed_owner':bridge.on_status=lambda:epoch.update(value='d'*64)
    elif which=='changed_core':bridge.on_status=lambda:setattr(bridge,'core_epoch','d'*64)
    elif which=='stale_after_proof':
        def change_revision():
            with client.app.state.session_factory() as db:
                row=db.get(MCPServer,sid);row.revision+=1;db.commit()
        bridge.on_status=change_revision
    response=attach(client,owner,server,ref)
    assert response.status_code in (409,422)
    with client.app.state.session_factory() as db:
        row=db.get(MCPServer,sid)
        assert row.credential_ref is None
        assert row.revision==(2 if which=='stale_after_proof' else 1)
    if which in ('foreign_owner','foreign_server','malformed','wrong_endpoint'):
        assert not any(k=='status' for k,_ in bridge.calls)


def test_old_bearer_and_endpoint_edit_do_not_transfer_oauth_reference(notion_fixture):
    client,owner,_,_,_,_,create=notion_fixture
    bearer_server=create('https://example.invalid/mcp');sid=bearer_server['server_id']
    bearer=f'mcp:{owner}:{sid}:'+('b'*32)
    assert attach(client,owner,bearer_server,bearer).status_code==200
    notion=create();sid=notion['server_id'];ref=f'mcp-oauth:{owner}:{sid}:'+('a'*32)
    assert attach(client,owner,notion,ref).status_code==200
    changed=client.patch(f'/api/v1/mcp/servers/{sid}',headers=headers(client,owner),json={
        'name':'Synthetic MCP','enabled':True,'transport':{'type':'http','url':'https://example.invalid/mcp'},'expected_revision':2})
    assert changed.status_code==200 and changed.json()['credential_ref'] is None
    rejected=attach(client,owner,notion,ref,revision=3)
    assert rejected.status_code==409 and rejected.json()['detail']['code']=='mcp_oauth_endpoint_mismatch'
