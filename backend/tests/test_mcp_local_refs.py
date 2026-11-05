"""Synthetic local-reference policy and argument-validation tests."""
import copy
import pytest
from jsonschema import Draft202012Validator,ValidationError
from referencing.exceptions import Unresolvable
from app.services.mcp_schema import safe_mcp_schema,validate_mcp_meta,validate_mcp_arguments,_NO_RETRIEVAL
from app.services.providers.base import normalized_call,wire_tool_name,ProviderFailure
from app.services.scheduler import validate_tool_calls
from app.services import mcp_registry


def schema():
    return {'type':'object','$defs':{'leaf':{'type':'string'},'alias':{'$ref':'#/$defs/leaf'},'a/b':{'type':'integer'}},
            'properties':{'value':{'$ref':'#/$defs/alias'},'number':{'$ref':'#/$defs/a~1b'}},'required':['value']}


def test_local_graph_unchanged_validation_and_untrusted_retrieval_denied(monkeypatch):
    s=schema();before=copy.deepcopy(s);raw=mcp_registry.canonical(s);digest=mcp_registry.digest(s)
    assert safe_mcp_schema(s);validate_mcp_meta(s);validate_mcp_arguments(s,{'value':'yes','number':1})
    with pytest.raises(ValidationError):validate_mcp_arguments(s,{'value':1})
    monkeypatch.setattr('urllib.request.urlopen',lambda *args,**kwargs:pytest.fail('network retrieval forbidden'))
    with pytest.raises(Unresolvable):Draft202012Validator({'$ref':'https://example.invalid/schema'},registry=_NO_RETRIEVAL).validate(1)
    assert s==before and mcp_registry.canonical(s)==raw and mcp_registry.digest(s)==digest

    draft7={'$schema':'http://json-schema.org/draft-07/schema#','type':'object','definitions':{'x':{'type':'string'}},'properties':{'value':{'$ref':'#/definitions/x','maxLength':1}}}
    assert safe_mcp_schema(draft7);validate_mcp_meta(draft7);validate_mcp_arguments(draft7,{'value':'long'})
    draft2020=copy.deepcopy(draft7);draft2020['$schema']='https://json-schema.org/draft/2020-12/schema'
    validate_mcp_meta(draft2020)
    with pytest.raises(ValidationError):validate_mcp_arguments(draft2020,{'value':'long'})
    no_ref={'$schema':'http://json-schema.org/draft-07/schema#','type':'object','properties':{'items':{'type':'array','items':[{'type':'string'}]}}}
    assert safe_mcp_schema(no_ref);validate_mcp_meta(no_ref);validate_mcp_arguments(no_ref,{'items':['ok']})


def test_remote_dynamic_id_cycle_pointer_and_expansion_rejected():
    for bad in [
        {'type':'object','$ref':'https://example.invalid/schema'},
        {'type':'object','$defs':{'x':{}},'$ref':'#/$defs/missing'},
        {'type':'object','$id':'https://example.invalid/','$defs':{'x':{}},'$ref':'#/$defs/x'},
        {'type':'object','$defs':{'a':{'$ref':'#/$defs/b'},'b':{'$ref':'#/$defs/a'}},'$ref':'#/$defs/a'},
        {'type':'object','$dynamicRef':'#/$defs/x'},
        {'type':'object','$recursiveRef':'#/$defs/x'},
        {'type':'object','$ref':'#/%24defs/x'},
        {'type':'object','$ref':'#/$defs/bad~2name'},
        {'type':'object','$schema':'http://json-schema.org/draft-06/schema#','$defs':{'x':{}},'$ref':'#/$defs/x'},
        {'type':'object','$schema':'http://json-schema.org/draft-07/schema#','properties':{'x':{'$schema':'https://json-schema.org/draft/2020-12/schema','type':'string'}}},
        {'type':'object','$schema':'https://unknown.invalid/schema'},
    ]:assert not safe_mcp_schema(bad)
    defs={'leaf':{'type':'string'}}
    for i in reversed(range(19)):
        nxt='leaf' if i==18 else f'x{i+1}'
        defs[f'x{i}']={'allOf':[{'$ref':f'#/$defs/{nxt}'},{'$ref':f'#/$defs/{nxt}'}]}
    assert not safe_mcp_schema({'type':'object','$defs':defs,'properties':{'x':{'$ref':'#/$defs/x0'}}})


def test_provider_and_scheduler_accept_bound_local_schema_only():
    s=schema();alias='mcp_'+'a'*20
    tools=[{'name':alias,'description':'synthetic','parameters':s,'mcp_binding':{'schema_sha256':mcp_registry.digest(s)}}]
    call=normalized_call('call',wire_tool_name(alias),{'value':'synthetic'},tools)
    validate_tool_calls([call],tools,{'call_ids':['call']})
    draft7={'$schema':'http://json-schema.org/draft-07/schema#','type':'object','definitions':{'x':{'type':'string'}},'properties':{'value':{'$ref':'#/definitions/x','maxLength':1}}}
    draft_tools=[{'name':alias,'description':'synthetic','parameters':draft7,'mcp_binding':{'schema_sha256':mcp_registry.digest(draft7)}}]
    draft_call=normalized_call('call',wire_tool_name(alias),{'value':'long'},draft_tools)
    validate_tool_calls([draft_call],draft_tools,{'call_ids':['call']})
    with pytest.raises(ProviderFailure):normalized_call('call',wire_tool_name(alias),{'value':1},tools)
    unbound=[{'name':'other','parameters':s}]
    with pytest.raises(ProviderFailure):normalized_call('call',wire_tool_name('other'),{'value':'synthetic'},unbound)


def test_discovery_and_approval_preserve_original_schema_binding(monkeypatch):
    from types import SimpleNamespace
    from app.services import mcp_execution
    from app.services.tool_broker import ToolBroker
    s=schema();raw=mcp_registry.canonical(s);digest=mcp_registry.digest(s)
    item={'name':'synthetic','description':'synthetic','input_schema':s,'schema_canonical':raw,'schema_sha256':digest}
    assert mcp_registry.validate_discovery_tools({'tools':[item]})==[item]
    server=SimpleNamespace(transport={'type':'http','url':'https://example.invalid/mcp'},name='synthetic')
    tool=SimpleNamespace(input_schema=s,name='synthetic')
    monkeypatch.setattr(mcp_execution,'require_origin',lambda *a,**k:None)
    monkeypatch.setattr(mcp_registry,'current_tool',lambda *a,**k:(server,tool))
    meta={'server_id':'server','config_revision':1,'binding_revision':1,'tool_id':'a'*64,'tool_name':'synthetic','schema_sha256':digest}
    prepared=ToolBroker(None).prepare_mcp(None,owner_id=1,agent_id=2,room_id=3,root_id='root',generation=1,meta=meta,arguments={'value':'synthetic'})
    assert prepared.mcp_binding['schema_sha256']==digest and mcp_registry.canonical(s)==raw
    with pytest.raises(ValueError):ToolBroker(None).prepare_mcp(None,owner_id=1,agent_id=2,room_id=3,root_id='root',generation=1,meta=meta,arguments={'value':1})
