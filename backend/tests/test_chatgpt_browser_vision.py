from dataclasses import replace
from uuid import uuid4
import pytest
from app.services import connections, providers
from app.services.providers.base import ProviderRequest, ProviderResult, ToolObservation, VisionInput, continuation_scope, UnsupportedOption
from app.services.providers.chatgpt import chatgpt_request_body, chatgpt_request_text_size
from app.services.tool_broker import TOOL_SCHEMAS
from test_chatgpt_builtin_preflight import config
from test_connections import setup


def request():
    c=config(); c.capabilities={'declared':{'tools':True,'vision':True},'verified':{'models':{'exact':{'model_id':'exact','base_url':c.base_url,'tools':True,'vision':True}}}}
    return ProviderRequest(connection=c, model_id='exact',room_id=1,agent_id=1,system='test',tools=TOOL_SCHEMAS)


def test_offers_managed_browser_only_and_serializes_approved_image():
    r=request(); state={'provider_kind':'chatgpt',**continuation_scope(r),'input':[],'call_ids':['one']}
    ref=f'rooms/1/screenshots/{uuid4()}.png'; png=connections._color_pixel(b'\xff\0\0')
    obs=ToolObservation('one','completed','captured',ref,png,'image/png',1,1)
    body,offered=chatgpt_request_body(replace(r,continuation=state,observations=[obs]))
    names={i['name'] for i in offered}
    assert {'browser_screenshot','browser_inspect','browser_navigate','browser_interact'} <= names
    assert not names.intersection({'screenshot','pointer_click','start_process','write_file'})
    output=body['input'][-1]['output']
    assert output[0]['type']=='input_text' and output[1]['type']=='input_image'
    assert output[1]['image_url'].startswith('data:image/png;base64,')
    assert chatgpt_request_text_size(replace(r,continuation=state,observations=[obs])) < len(str(body))
    for bad in [replace(obs,image_bytes=b'bad'),replace(obs,screenshot_ref=None),replace(obs,image_width=2)]:
        with pytest.raises(UnsupportedOption): chatgpt_request_body(replace(r,continuation=state,observations=[bad]))
    with pytest.raises(Exception): chatgpt_request_body(replace(r,continuation=state,observations=[replace(obs,call_id='wrong')]))
    text=chatgpt_request_body(replace(r,continuation=state,observations=[replace(obs,image_bytes=None)]))[0]
    assert isinstance(text['input'][-1]['output'],str)


@pytest.mark.asyncio
@pytest.mark.parametrize('answers,expected',[(['red','blue'],True),(['red','red'],False)])
async def test_vision_only_probe_two_distinct_pixels(monkeypatch,answers,expected):
    c=config(); c.capabilities={'declared':{'vision':True}}
    calls=[]
    class Adapter:
        async def generate(self,r):
            from app.services.providers.base import check_options
            check_options(r); calls.append(r); return ProviderResult(text=answers[len(calls)-1])
    monkeypatch.setattr(connections,'validated_connection_contract',lambda *_a,**_k:None)
    monkeypatch.setattr(providers,'adapter_for',lambda *_a,**_k:Adapter())
    result=await connections.probe_capabilities(c)
    assert result['vision'] is expected and len(calls)==2 and calls[0].vision.image_bytes!=calls[1].vision.image_bytes
    assert connections.test_request_limit('compatible',c.capabilities,'full')==3
    assert connections.test_request_limit('compatible',c.capabilities,'text')==1


def test_vision_input_verified_only():
    r=request(); r=replace(r,tools=[],vision=VisionInput(connections._color_pixel(b'\xff\0\0'),'image/png','color?'))
    assert chatgpt_request_body(r)[0]['input'][-1]['content'][-1]['type']=='input_image'
    r.connection.capabilities['verified']['models']['exact']['vision']=False
    with pytest.raises(UnsupportedOption): chatgpt_request_body(r)


def test_image_budget_exempts_only_actual_input_content():
    from app.services.providers.chatgpt import _bounded_text_size
    import base64
    image={'type':'input_image','image_url':'data:image/png;base64,'+base64.b64encode(connections._color_pixel(b'\xff\0\0')).decode()}
    actual=[{'role':'user','content':[image]}]
    fake=[{'type':'function_call','arguments':{'content':[image]}}]
    assert _bounded_text_size(actual,allow_image=True) < _bounded_text_size(fake,allow_image=True)
    invalid=[{'role':'user','content':[{'type':'input_image','image_url':'https://example.com/image.png'}]}]
    with pytest.raises(UnsupportedOption): _bounded_text_size(invalid,allow_image=True)
    with pytest.raises(UnsupportedOption): _bounded_text_size(actual,allow_image=False)


@pytest.mark.parametrize('race', [False, True])
def test_oauth_grant_preserves_declared_resets_proof_and_detects_race(setup, tmp_path, monkeypatch, race):
    from app.services import chatgpt_oauth
    from app.models.collaboration import ProviderConnection
    from app.models.models import User
    from sqlalchemy import update
    from types import SimpleNamespace
    import time
    client, _, headers, _ = setup
    created=client.post('/api/v1/connections',headers=headers(),json={'provider_kind':'openai','label':'Synthetic','default_model_id':'exact','api_key':'fake'}).json()
    with client.app.state.session_factory() as db:
        user=db.query(User).filter_by(username='owner').one(); owner=user.id
        row=db.get(ProviderConnection,created['id']); row.provider_id='chatgpt'; row.secret_ref=None
        row.auth_grant_revision=0; row.readiness_revision=1
        row.capabilities={'declared':{'tools':True,'vision':True,'temperature':False},
                          'verified':{'models':{'exact':{'vision':True}}},'model_features':{'stale':{'tested':{'image_input':True}}}}
        db.commit()
    manager=chatgpt_oauth.Manager(tmp_path/'oauth',client.app.state.session_factory,SimpleNamespace(current=lambda _: 'epoch'))
    class FakeClient:
        def __init__(self,**kwargs): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
    monkeypatch.setattr(chatgpt_oauth.httpx,'Client',FakeClient)
    monkeypatch.setattr(manager,'_json',lambda *a,**k:{})
    monkeypatch.setattr(manager,'_tokens',lambda *a,**k:{'client_id':'oaiapp_test','subject':'synthetic','label':'Synthetic'})
    real_row=manager._row
    if race:
        def changed_row(db, *args):
            row=real_row(db,*args)
            db.execute(update(ProviderConnection).where(ProviderConnection.id==row.id).values(
                capabilities={'declared':{'tools':False,'vision':False}}).execution_options(synchronize_session=False))
            return row
        monkeypatch.setattr(manager,'_row',changed_row)
    op={'state':'awaiting_user','expires':time.time()+60,'binding':(owner,created['id'],1,0,'epoch'),
        'previous':None,'old_ref':None,'verifier':'v','redirect':'http://localhost','nonce':'n','state_nonce':'s','url':'u'}
    manager._complete(op,{'client_id':['oaiapp_test'],'code':['synthetic']})
    with client.app.state.session_factory() as db:
        row=db.get(ProviderConnection,created['id'])
        if race:
            assert op['state']=='failed' and row.secret_ref is None and row.readiness_revision==1
            assert row.capabilities['declared']['vision'] is True
        else:
            assert op['state']=='succeeded' and row.auth_grant_revision==1 and row.readiness_revision==2
            assert row.capabilities == {'declared':{'tools':True,'vision':True,'temperature':False},'verified':{}}
