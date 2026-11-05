"""Exact-model capability evidence. Metadata is not an execution test or a generation adapter."""
import asyncio,base64,copy,hashlib,json,re,secrets,struct,time,zlib
from dataclasses import replace
from datetime import datetime,timezone
from urllib.parse import urlsplit
import httpx
from fastapi import HTTPException

LOCAL_PROVIDERS=frozenset({'ollama','lm-studio','local-openai'})
FEATURES=('text_generation','tool_calling','image_input','image_generation','audio_input','speech_generation','embeddings')
APP_FEATURES=frozenset({'text_generation','tool_calling','image_input'})
ALIASES={'tools':'tool_calling','vision':'image_input'}


def local(config):
    try:return urlsplit(config.base_url or '').hostname in {'localhost','127.0.0.1','::1'} and config.auth_method_id in (None,'local_none') and not config.api_key and config.protocol_id in (None,'openai_chat_completions')
    except ValueError:return False


def record(config):
    r=(config.capabilities or {}).get('model_features',{}).get(config.model_id,{})
    return r if (r.get('base_url')==config.base_url and r.get('model_id')==config.model_id and r.get('config_revision')==config.config_revision) else {}


def verified_remote_image(config):
    """A remote image check is bound to the exact current authorization."""
    evidence=record(config).get('image_check',{})
    return (evidence.get('verified') is True and
            type(config.auth_grant_revision) is int and
            evidence.get('auth_grant_revision') == config.auth_grant_revision and
            evidence.get('config_revision') == config.config_revision and
            evidence.get('model_id') == config.model_id and
            evidence.get('base_url') == config.base_url)


def permitted(config,feature):
    r=record(config)
    if feature == 'image_input' and r.get('image_check') is not None:
        return verified_remote_image(config) and r.get('tested',{}).get(feature) is True and r.get('reported',{}).get(feature) is not False
    return feature in APP_FEATURES and r.get('tested',{}).get(feature) is True and r.get('reported',{}).get(feature) is not False


def report(config):
    r=record(config);result={}
    if not r and not local(config):
        old=(config.capabilities or {}).get('verified',{});old=old.get('models',{}).get(config.model_id,old)
        if old.get('base_url')==config.base_url:
            tested={ALIASES[k]:old[k] for k in ALIASES if type(old.get(k)) is bool}
            r={'tested':tested,'source':'saved_connection_feature_tests'}

    for name in FEATURES:
        advertised=r.get('reported',{}).get(name);tested=r.get('tested',{}).get(name)
        available=permitted(config,name) if local(config) or (name == 'image_input' and r.get('image_check') is not None) else name in APP_FEATURES and r.get("tested",{}).get(name) is True
        reason='verified' if available else 'app_unsupported' if name not in APP_FEATURES else 'unsupported' if advertised is False else 'failed' if tested is False else 'unknown'
        result[name]={'reported':advertised,'tested':tested,'available':available,'reason':reason}
    return {'model_id':config.model_id,'base_url':config.base_url,'features':result,'observed_at':r.get('observed_at'),'tested_at':r.get('tested_at'),'source':r.get('source','not_checked'),'actual_generation_requests':r.get('actual_generation_requests'),'local':local(config)}


async def _json(client,method,url,body=None):
    try:
        async with client.stream(method,url,json=body) as response:
            if response.status_code!=200:raise ValueError('local_model_http_'+str(response.status_code))
            content=bytearray()
            async for part in response.aiter_bytes():
                content.extend(part)
                if len(content)>1000000:raise ValueError('local_model_response_too_large')
        return json.loads(content)
    except (httpx.HTTPError,TimeoutError):raise ValueError('local_model_unreachable') from None


def compatible_metadata(item, props=None):
    """Server metadata is advisory; only an actual feature probe can enable use."""
    props=props if isinstance(props,dict) else {}
    reported={name:None for name in FEATURES}
    architecture=item.get('architecture') if isinstance(item.get('architecture'),dict) else {}
    for field,mapping in [('input_modalities',{'image':'image_input','audio':'audio_input'}),('output_modalities',{'text':'text_generation','image':'image_generation','audio':'speech_generation'})]:
        values=architecture.get(field)
        if isinstance(values,list) and values and all(isinstance(x,str) for x in values):
            for modality,feature in mapping.items():reported[feature]=modality in values
    modalities=props.get('modalities') if isinstance(props.get('modalities'),dict) else {}
    for flag,feature in [('vision','image_input'),('audio','audio_input')]:
        if type(modalities.get(flag)) is bool:
            advertised=modalities[flag]
            reported[feature]=False if reported[feature] is False or not advertised else True
    caps=props.get('chat_template_caps') if isinstance(props.get('chat_template_caps'),dict) else {}
    flags=[caps.get(k) for k in ('supports_tools','supports_tool_calls')]
    if any(x is False for x in flags):reported['tool_calling']=False
    elif all(x is True for x in flags):reported['tool_calling']=True
    # /v1/models.created can be the current wall clock, not an artifact revision.
    # Metadata fingerprints cannot detect same-name, same-metadata weight replacement.
    identity={k:item.get(k) for k in ('id','owned_by','architecture','meta') if k in item}
    identity['runtime']={k:props[k] for k in ('model_path','build_info','modalities','chat_template_caps') if k in props}
    return reported,identity


async def describe(config):
    from app.services.connections import provider_runtime_origin
    from app.schemas.provider_models import valid_model_id
    if not local(config):raise ValueError('local_model_required')
    contract,origin=provider_runtime_origin(config);root=origin.removesuffix('/v1').rstrip('/')
    reported={name:None for name in FEATURES};identity={};source='openai_models_only'
    async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=10) as client:
        if contract.provider_id=='ollama':
            tags=await _json(client,'GET',root+'/api/tags');model=next((x for x in tags.get('models',[]) if isinstance(x,dict) and config.model_id in (x.get('name'),x.get('model'))),None)
            if not model:raise ValueError('local_model_not_found')
            details=await _json(client,'POST',root+'/api/show',{'model':config.model_id,'verbose':False})
            if details.get('remote_host') or details.get('remote_model'):raise ValueError('local_cloud_model_not_allowed')
            caps=details.get('capabilities')
            if isinstance(caps,list) and all(isinstance(x,str) for x in caps):
                for feature,flag in [('text_generation','completion'),('tool_calling','tools'),('image_input','vision'),('embeddings','embedding')]:reported[feature]=flag in caps
            identity={'digest':model.get('digest'),'modified_at':model.get('modified_at'),'capabilities':caps};source='ollama_api_show'
        elif contract.provider_id=='lm-studio':
            try:payload=await _json(client,'GET',root+'/api/v1/models')
            except ValueError as error:
                if str(error)!='local_model_http_404':raise
                payload={'models':[]}
            item=None
            for x in payload.get('models',[]):
                if isinstance(x,dict) and (x.get('key')==config.model_id or any(i.get('id')==config.model_id for i in x.get('loaded_instances',[]) if isinstance(i,dict))):item=x;break
            if item:
                c=item.get('capabilities') or {};reported.update(text_generation=item.get('type')=='llm',embeddings=item.get('type')=='embedding',image_input=c.get('vision') if type(c.get('vision')) is bool else None,tool_calling=c.get('trained_for_tool_use') if type(c.get('trained_for_tool_use')) is bool else None)
                identity={k:item.get(k) for k in ('key','type','size_bytes','quantization','capabilities')};source='lmstudio_api_v1_models'
        if not identity:
            payload=await _json(client,'GET',origin.rstrip('/')+'/models');items=payload.get('data',[])
            item=next((x for x in items if isinstance(x,dict) and x.get('id')==config.model_id),None)
            if item is None:raise ValueError('local_model_not_found')
            props={}
            if item.get('owned_by')=='llamacpp':
                try:props=await _json(client,'GET',root+'/props')
                except ValueError:pass
            reported,identity=compatible_metadata(item,props)
            identity['base_url']=origin
            source='llamacpp_models_props' if props else 'openai_models_metadata'
    digest=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
    return {'model_id':config.model_id,'base_url':config.base_url,'config_revision':config.config_revision,'identity':digest,'source':source,'reported':reported,'observed_at':datetime.now(timezone.utc).isoformat()}


async def enforce(request):
    from app.services.providers.base import ProviderFailure
    if not local(request.connection):return
    required=['text_generation']
    if request.tools or request.continuation or request.observations:required.append('tool_calling')
    if request.vision is not None or any(x.image_bytes is not None for x in request.observations):required.append('image_input')
    for feature in required:
        if not permitted(request.connection,feature):raise ProviderFailure('model_feature_unavailable','This exact local model has not passed the required feature check: '+feature)
    try:current=await describe(request.connection)
    except Exception:raise ProviderFailure('local_model_unavailable','The local model server or exact model is unavailable. Check its connection and features.') from None
    saved=record(request.connection)
    if saved.get('identity')!=current['identity']:
        raise ProviderFailure('model_capabilities_changed','The local model or its capabilities changed. Run its feature check again.')
    if any(current['reported'].get(f) is False for f in required):raise ProviderFailure('model_feature_unavailable','The local server does not advertise the requested model feature.')


def _png(color=b'\xff\0\0'):
    def chunk(k,b):return struct.pack('!I',len(b))+k+b+struct.pack('!I',zlib.crc32(k+b))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',16,16,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+color*16)*16))+chunk(b'IEND',b'')


async def probe(config):
    """At most five synthetic generation requests, no real tool or private room data."""
    r=await describe(config);tested={};outcomes={};count=0;_,origin=__import__('app.services.connections',fromlist=['provider_runtime_origin']).provider_runtime_origin(config)
    async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=httpx.Timeout(120,connect=5)) as client:
        async def chat(messages,**extra):
            nonlocal count
            count+=1
            data=await _json(client,'POST',origin.rstrip('/')+'/chat/completions',{'model':config.model_id,'messages':messages,'max_tokens':256,**extra})
            message=data['choices'][0]['message']
            if not isinstance(message,dict):raise ValueError('invalid_chat_response')
            return message
        if r['reported'].get('text_generation') is not False:
            try:
                msg=await chat([{'role':'user','content':'Reply with the word READY. /no_think'}]);tested['text_generation']=isinstance(msg.get('content'),str) and bool(msg['content'].strip());outcomes['text_generation']='nonempty_text' if tested['text_generation'] else 'empty_response'
            except Exception:tested['text_generation']=False;outcomes['text_generation']='request_failed'
        if tested.get('text_generation') and r['reported'].get('tool_calling') is not False:
            nonce='probe_'+secrets.token_hex(4)
            from app.services.providers.base import wire_tool_name
            probe_name=wire_tool_name('probe_echo')
            tools=[{'type':'function','function':{'name':probe_name,'description':'Return the provided value unchanged. Always call this function to answer the user.','parameters':{'type':'object','properties':{'value':{'type':'string'}},'required':['value'],'additionalProperties':False}}}]
            messages=[{'role':'user','content':f'Call {probe_name} once with value "{nonce}". Do not answer without calling it. /no_think'}]
            try:
                msg=await chat(messages,tools=tools);calls=msg.get('tool_calls') or []
                valid=len(calls)==1 and isinstance(calls[0].get('id'),str) and calls[0].get('type')=='function' and calls[0].get('function',{}).get('name')==probe_name and json.loads(calls[0]['function']['arguments'])=={'value':nonce}
                if not valid:raise ValueError('invalid_tool_call')
                result=await chat([*messages,{'role':'assistant',**msg},{'role':'tool','tool_call_id':calls[0]['id'],'content':nonce}])
                tested['tool_calling']=nonce in (result.get('content') or '');outcomes['tool_calling']='synthetic_call_and_result_roundtrip' if tested['tool_calling'] else 'result_not_used'
            except Exception:tested['tool_calling']=False;outcomes['tool_calling']='tool_roundtrip_failed'
        if tested.get('text_generation') and r['reported'].get('image_input') is True:
            try:
                matches=[]
                for color,pattern in [(b'\xff\0\0',r'\bred\b|빨강|빨간'),(b'\0\0\xff',r'\bblue\b|파랑|파란')]:
                    msg=await chat([{'role':'user','content':[{'type':'text','text':'Name the main color in one word. /no_think'},{'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(_png(color)).decode()}}]}]);matches.append(bool(re.search(pattern,(msg.get('content') or '').lower())))
                tested['image_input']=all(matches);outcomes['image_input']='red_and_blue_synthetic_images' if tested['image_input'] else 'visual_answer_not_confirmed'
            except Exception:tested['image_input']=False;outcomes['image_input']='vision_request_failed'
    # Bind the observations to the same model identity after the await, not a replaced artifact.
    current=await describe(config)
    if current['identity']!=r['identity']:raise ValueError('local_model_changed')
    r.update(tested=tested,outcomes=outcomes,tested_at=datetime.now(timezone.utc).isoformat(),actual_generation_requests=count)
    return r


def output_request(content,kind='auto', *, local_speech=False):
    """Structured kinds are authoritative; plain-text imperative detection is conservative, not semantic parsing."""
    if kind not in ('auto','text','image_generation','speech_generation'):raise HTTPException(422,{'code':'unsupported_output_kind'})
    inferred=kind
    if kind=='auto':
        text=content.strip()
        image=re.search(r'^(?:please\s+)?(?:generate|create|draw|render)\b.{0,60}\b(?:image|picture|photo)\b|^(?:이미지|사진|그림)(?:를|을)?\s*.{0,30}(?:생성해|만들어|그려)|^.{0,30}(?:그려줘|그려\s*주세요)',text,re.I)
        voice=re.search(r'^(?:please\s+)?(?:generate|create|synthesize)\b.{0,50}\b(?:audio|speech|voice)\b|^(?:음성|오디오)(?:를|을)?\s*.{0,30}(?:생성해|만들어)',text,re.I)
        inferred='image_generation' if image else 'speech_generation' if voice else 'text'
    if inferred == 'speech_generation' and local_speech:
        return 'text'  # A local tool creates the memo after approval, not the model.
    if inferred in ('image_generation','speech_generation'):
        raise HTTPException(422,{'code':'model_output_unavailable','feature':inferred,'message':'This app has no verified generation adapter for that output. No model request was sent. Use text for explanations or code.'})
    return 'text'


def bind_saved_text_test(db,row,config):
    """Existing exact-model test history is evidence, not a provider-name capability guess."""
    if local(config):return config
    from app.models.collaboration import ConnectionTestHistory
    from app.services import readiness
    candidates=db.query(ConnectionTestHistory).filter_by(owner_id=row.user_id,connection_id=row.id,model_id=config.model_id,ok=True).order_by(ConnectionTestHistory.id.desc()).limit(20).all()
    old=next((item for item in candidates if item.config_fingerprint==readiness.test_fingerprint(row,config.model_id,item.profile or 'full')),None)
    if old:
        caps=copy.deepcopy(config.capabilities or {});tested={'text_generation':True}
        saved=(config.capabilities or {}).get('verified',{});saved=saved.get('models',{}).get(config.model_id,saved)
        for key,target in ALIASES.items():
            if (old.verified_capabilities or {}).get(key) is True or (saved.get('base_url')==config.base_url and saved.get('model_id',config.model_id)==config.model_id and saved.get(key) is True):tested[target]=True
        previous=record(config)
        tested={**previous.get('tested',{}),**tested}
        caps.setdefault('model_features',{})[config.model_id]={**previous,'model_id':config.model_id,'base_url':config.base_url,'config_revision':config.config_revision,'source':'saved_connection_tests','tested_at':old.tested_at.isoformat(),'actual_generation_requests':old.actual_request_count,'tested':tested}
        config.capabilities=caps
    return config
