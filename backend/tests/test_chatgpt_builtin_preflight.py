import pytest
from app.services import connections, providers
from app.services.providers.base import ProviderResult, ToolCall, check_options

FAKE='00000000-0000-4000-8000-000000000001'

def config():
    return connections.ConnectionConfig(18,'compatible','exact','https://api.openai.com/v1','synthetic',
        {'declared':{'tools':True}},provider_id='chatgpt',protocol_id='openai_responses',owner_id=1,config_revision=2)

@pytest.mark.asyncio
@pytest.mark.parametrize('mode,expected,count',[('success',True,2),('wrong_args',False,1),('wrong_final',False,2),('no_call',False,1)])
async def test_builtin_challenge_without_filesystem_or_real_tools(monkeypatch,mode,expected,count):
    original=config();calls=[]
    class Adapter:
        async def generate(self,request):
            calls.append(request);check_options(request)
            assert request.room_id==0 and request.agent_id==0
            assert [t['name'] for t in request.tools]==['read_bot_routine']
            assert not request.connection.capabilities.get('model_features',{}).get('exact',{}).get('tool_families')
            if len(calls)==1:
                if mode=='no_call':return ProviderResult(text='not called')
                return ProviderResult(tool_calls=[ToolCall('call1','read_bot_routine',{'routine_id':FAKE if mode!='wrong_args' else 'wrong'})],continuation={'test':'opaque'})
            assert len(request.observations)==1 and request.observations[0].call_id=='call1'
            return ProviderResult(text='PROBE_COMPLETE' if mode!='wrong_final' else 'not verified')
    monkeypatch.setattr(connections,'validated_connection_contract',lambda *_a,**_k:None)
    monkeypatch.setattr(providers,'adapter_for',lambda *_a,**_k:Adapter())
    result=await connections.probe_capabilities(original)
    assert result['tools'] is expected and len(calls)==count
    assert original.capabilities=={'declared':{'tools':True}}

@pytest.mark.asyncio
async def test_undeclared_probe_never_calls_adapter(monkeypatch):
    c=config();c.capabilities={'declared':{'tools':False}}
    monkeypatch.setattr(connections,'validated_connection_contract',lambda *_a,**_k:None)
    monkeypatch.setattr(providers,'adapter_for',lambda *_a,**_k:pytest.fail('unexpected call'))
    assert not (await connections.probe_capabilities(c))['tools']

@pytest.mark.asyncio
async def test_real_adapter_wire_and_file_proof_remain_separate(monkeypatch):
    from app.services.providers.chatgpt import ChatGPTAdapter
    seen=[]
    async def discover(_config):return ([{'id':'exact'}],None,None)
    async def post(self,request,path,headers,body):
        seen.append(body)
        names=[t['name'] for ns in body['tools'] for t in ns['tools']]
        assert names==['read_bot_routine']
        if len(seen)==1:
            return {'output':[{'type':'function_call','namespace':'gamja','name':'read_bot_routine','call_id':'probe-1','arguments':'{"routine_id":"'+FAKE+'"}'}]}
        assert body['input'][-1]['type']=='function_call_output' and body['input'][-1]['call_id']=='probe-1'
        return {'output':[{'type':'message','content':[{'type':'output_text','text':'PROBE_COMPLETE'}]}]}
    monkeypatch.setattr(connections,'validated_connection_contract',lambda *_a,**_k:None)
    monkeypatch.setattr(connections,'discover_models',discover)
    monkeypatch.setattr(ChatGPTAdapter,'_post',post)
    result=await connections.probe_capabilities(config())
    assert result['tools'] is True and len(seen)==2

from test_connections import setup
from types import SimpleNamespace
from app.models.collaboration import ProviderConnection
from app.models.models import User

@pytest.mark.asyncio
@pytest.mark.parametrize('race',['none','epoch','config'])
async def test_owner_epoch_and_config_cas_before_persist(setup,monkeypatch,race):
    from app.api.routes.connections import test_connection as route
    client,_,headers,_=setup
    created=client.post('/api/v1/connections',headers=headers(),json={'provider_kind':'openai','label':'Synthetic','default_model_id':'exact','api_key':'fake'}).json()
    with client.app.state.session_factory() as db:
        row=db.get(ProviderConnection,created['id']);row.provider_id='chatgpt';row.capabilities={'declared':{'tools':True},'model_features':{'exact':{'model_id':'exact','base_url':'https://api.openai.com/v1','config_revision':1,'tested':{'text_generation':True},'tool_families':{'read_file':True}}}};db.commit()
    class Epoch:
        value='a'*64
        def current(self,owner):return self.value
    epoch=Epoch();client.app.state.owner_epoch_provider=epoch
    async def resolve(db,connection_id,owner_id,*args,**kwargs):
        c=config();c.connection_id=connection_id;c.owner_id=owner_id;c.config_revision=1;c.capabilities=dict(db.get(ProviderConnection,connection_id).capabilities);return c
    async def probe(*args,**kwargs):
        if race=='epoch':epoch.value='b'*64
        if race=='config':
            with client.app.state.session_factory() as other:
                r=other.get(ProviderConnection,created['id']);r.readiness_revision=(r.readiness_revision or 1)+1;other.commit()
        return {'ok':True,'code':'connected','message':'synthetic'}
    async def capabilities(*args,**kwargs):return {'model_id':'exact','base_url':'https://api.openai.com/v1','tools':True,'vision':False,'temperature':False,'max_tokens':False}
    monkeypatch.setattr(connections,'resolve_connection_async',resolve)
    monkeypatch.setattr(connections,'probe',probe)
    monkeypatch.setattr(connections,'probe_capabilities',capabilities)
    with client.app.state.session_factory() as db:
        user=db.query(User).filter_by(username='owner').one()
        result=await route(created['id'],SimpleNamespace(app=client.app),db=db,user=user)
    with client.app.state.session_factory() as db:
        saved=db.get(ProviderConnection,created['id']).capabilities
        if race=='none':
            assert result['ok'] and saved['verified']['models']['exact']['tools'] is True
            assert saved['model_features']['exact']['tested']['tool_calling'] is True
            assert saved['model_features']['exact']['tool_families']=={'read_file':True}
            assert 'write_file' not in saved['model_features']['exact']['tool_families']
        else:
            assert not result['ok'] and result['code']=='configuration_changed'
            assert 'verified' not in saved
