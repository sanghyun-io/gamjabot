from app.services.connections import ConnectionConfig
from app.services.owner_routines import _supported_routine_tools
from app.services.providers.base import ProviderRequest,check_options
from app.services.providers.chatgpt import chatgpt_request_body
from app.services.tool_broker import TOOL_SCHEMAS

def request(proof=None,provider='chatgpt'):
    cap={'declared':{'tools':True},'verified':{'models':{'exact':{'model_id':'exact','base_url':'https://api.openai.com/v1','tools':True}}},
         'model_features':{'exact':{'model_id':'exact','base_url':'https://api.openai.com/v1','config_revision':1,'tool_families':proof or {}}}}
    c=ConnectionConfig(1,'compatible','exact','https://api.openai.com/v1','fake',cap,provider_id=provider,protocol_id='openai_responses',config_revision=1)
    return ProviderRequest(connection=c,model_id='exact',tools=TOOL_SCHEMAS,server_instruction='Synthetic registered routine',watermark_seq=1)

def test_unverified_file_families_leave_text_request_usable():
    source=request();result=_supported_routine_tools(source)
    assert result.tools==[] and source.tools
    assert result.server_instruction==source.server_instruction
    check_options(result);body,_=chatgpt_request_body(result)
    assert 'tools' not in body and 'Synthetic registered routine' in str(body['input'])

def test_only_proven_file_tool_survives_and_nonchatgpt_is_unchanged():
    result=_supported_routine_tools(request({'read_file':True}))
    assert [x['name'] for x in result.tools]==['read_file']
    check_options(result);body,_=chatgpt_request_body(result)
    assert len(body['tools'][0]['tools'])==1
    other=request(provider='another');assert _supported_routine_tools(other) is other
