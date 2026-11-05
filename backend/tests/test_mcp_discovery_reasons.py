"""Synthetic MCP discovery diagnostics: fixed reasons, no external requests."""
import copy
import pytest
from fastapi import HTTPException
from app.services import mcp_registry as service
from test_room_state import room_client, headers


def item(schema=None):
    schema={'type':'object','properties':{}} if schema is None else schema
    raw=service.canonical(schema)
    return {'name':'synthetic','description':'synthetic','input_schema':schema,'schema_canonical':raw,'schema_sha256':service.sha256(raw.encode()).hexdigest()}


def failure(result):
    with pytest.raises(HTTPException) as caught:service.validate_discovery_tools(result)
    assert caught.value.status_code==422
    return caught.value.detail['code']


def test_discovery_validation_reason_categories_and_no_raw_values():
    assert failure({'tools':None})=='mcp_disc_invalid_list'
    assert failure({'tools':[{}]})=='mcp_disc_invalid_shape'
    bad=item();bad['name']='';assert failure({'tools':[bad]})=='mcp_disc_invalid_name'
    bad=item();bad['description']=None;assert failure({'tools':[bad]})=='mcp_disc_invalid_description'
    bad=item();bad['input_schema']=[];assert failure({'tools':[bad]})=='mcp_disc_invalid_schema_object'
    assert failure({'tools':[item({'anyOf':[{'type':'object'}]})]})=='mcp_disc_schema_type_missing'
    assert failure({'tools':[item({'type':['object','null']})]})=='mcp_disc_schema_type_invalid'
    assert failure({'tools':[item({'type':'object','properties':{'x':{'$ref':'#/$defs/x'}}})]})=='mcp_disc_invalid_schema_ref'
    assert failure({'tools':[item({'type':'object','properties':[]})]})=='mcp_disc_invalid_schema_meta'
    bad=item();bad['schema_canonical']='not-json';assert failure({'tools':[bad]})=='mcp_disc_invalid_canonical_parse'
    bad=item();bad['schema_canonical']='{}';assert failure({'tools':[bad]})=='mcp_disc_invalid_canonical_mismatch'
    assert failure({'tools':[item({'type':'object','description':'x'*service.MAX_MCP_SCHEMA_BYTES})]})=='mcp_disc_invalid_schema_size'
    large=item({'type':'object','description':'x'*60_000});items=[]
    for n in range(9):
        entry=copy.deepcopy(large);entry['name']=f'synthetic_{n}';items.append(entry)
    assert failure({'tools':items})=='mcp_disc_invalid_aggregate_size'
    bad=item();bad['schema_sha256']='synthetic-secret';assert failure({'tools':[bad]})=='mcp_disc_invalid_schema_hash'
    assert service.validate_discovery_tools({'tools':[item()]})==[item()]


def test_backend_discovery_persists_fixed_validation_reason(room_client,monkeypatch):
    client,(owner,_,_,_,_)=room_client;auth=headers(client,owner);calls=[]
    class Bridge:
        available=True;core_epoch='b'*64
        async def close(self):pass
        async def configure(self,p):return {'state':'configured'}
        async def bind(self,p):return {'state':'bound'}
        async def revoke(self,p):return {'state':'revoked'}
        async def issue(self,p):pass
        async def start(self,p):pass
        async def cancel(self,p):pass
        async def credential_status(self,p):pass
        async def discover(self,p):
            calls.append(1)
            return {'state':'completed','result':{'tools':[item({'anyOf':[{'type':'object'}]})]}}
    client.app.state.mcp_client=Bridge();monkeypatch.setattr(client.app.state.owner_epoch_provider,'current',lambda owner_id:'c'*64)
    body={'name':'Synthetic','enabled':True,'transport':{'type':'http','url':'https://example.invalid/mcp'}}
    server=client.post('/api/v1/mcp/servers',headers={**auth,'Idempotency-Key':'discovery-reason-server'},json=body).json()['server'];sid=server['server_id']
    preview=client.post(f'/api/v1/mcp/servers/{sid}/discovery-preview',headers=auth,json={'expected_revision':1}).json()
    response=client.post(f'/api/v1/mcp/servers/{sid}/discover',headers={**auth,'Idempotency-Key':'discovery-reason-run'},json={'expected_revision':1,'preview_id':preview['preview_id']})
    assert response.status_code==200
    receipt=client.get('/api/v1/mcp/commands/discovery-reason-run',headers=auth).json()['discovery']
    assert receipt['state']=='failed' and receipt['failure_code']=='mcp_disc_schema_type_missing' and len(calls)==1
