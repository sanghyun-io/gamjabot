"""Pure Go continuation wire-body construction shared by projection and adapters."""
import json
from hashlib import sha256
from app.services.providers.base import observation_text,wire_tool_name,provider_json_text,image_part,ToolObservation,canonical_screenshot_ref

def go_history_scope(request):
    """Bind ephemeral Go transcript to the same authority and scheduler turn."""
    c=request.connection
    return {'owner_id':c.owner_id,'room_id':request.room_id,'agent_id':request.agent_id,
            'trigger_event_id':request.trigger_event_id,'generation':request.turn_generation,
            'watermark_seq':request.watermark_seq,'connection_id':request.connection_id,
            'model_id':request.model_id,'base_url':c.base_url,'config_revision':c.config_revision,
            'auth_grant_revision':c.auth_grant_revision,'public_config_fingerprint':c.public_config_fingerprint,
            'owner_epoch_digest':sha256(c.owner_epoch.encode()).hexdigest() if isinstance(c.owner_epoch,str) else None}


def _pair_ids(protocol, assistant):
    if type(assistant) is not dict: raise ValueError('Invalid Go history')
    if protocol=='openai_chat_completions':
        if assistant.get('role')!='assistant' or type(assistant.get('tool_calls')) is not list: raise ValueError('Invalid Go history')
        calls=assistant['tool_calls'];ids=[c.get('id') if type(c) is dict else None for c in calls]
    elif protocol=='anthropic_messages':
        if assistant.get('role')!='assistant' or type(assistant.get('content')) is not list: raise ValueError('Invalid Go history')
        ids=[c.get('id') for c in assistant['content'] if type(c) is dict and c.get('type')=='tool_use']
    else: raise ValueError('Invalid Go history')
    if not ids or any(type(x) is not str or not x for x in ids) or len(set(ids))!=len(ids): raise ValueError('Invalid Go history')
    return ids


def _image(o,style,budget_only):
    if not budget_only:return image_part(o,style)
    if not isinstance(o.image_bytes,bytes) or not 1<=len(o.image_bytes)<=10000000 or o.image_mime not in ('image/png','image/jpeg'):
        raise ValueError('Invalid image')
    if style=='anthropic':return {'type':'image','source':{'type':'base64','media_type':o.image_mime,'data':'[bounded image]'}}
    return {'type':'image_url','image_url':{'url':'data:'+o.image_mime+';base64,[bounded image]'}}


def go_history_has_images(state):
    return any('image_bytes' in r for p in state.get('_go_history',[]) if isinstance(p,dict)
               for r in p.get('results',[]) if isinstance(r,dict)) if isinstance(state,dict) else False


def go_history_messages(state, protocol, current_ids, *, budget_only=False):
    history=state.get('_go_history',[])
    if type(history) is not list or len(history)>64: raise ValueError('Invalid Go history')
    seen=set();messages=[]
    for pair in history:
        if type(pair) is not dict or set(pair)!={'assistant','results'} or type(pair['results']) is not list: raise ValueError('Invalid Go history')
        ids=_pair_ids(protocol,pair['assistant'])
        results=pair['results']
        if (any(type(x) is not dict or set(x) not in ({'call_id','state','content'},{'call_id','state','content','image_bytes','image_mime'}) or
                type(x['call_id']) is not str or type(x['state']) is not str or type(x['content']) is not str for x in results)
                or len(results)!=len(ids) or {x['call_id'] for x in results}!=set(ids) or seen.intersection(ids)):
            raise ValueError('Invalid Go history')
        seen.update(ids);messages.append(pair['assistant'])
        def saved_image(r,style):
            if 'image_bytes' not in r:return []
            if r['state']!='completed':raise ValueError('Invalid Go image result')
            item=ToolObservation(r['call_id'],r['state'],r['content'],image_bytes=r['image_bytes'],image_mime=r['image_mime'])
            return [_image(item,style,budget_only)]
        if protocol=='openai_chat_completions':
            for r in results:
                messages.append({'role':'tool','tool_call_id':r['call_id'],'content':r['content']})
                images=saved_image(r,'openai')
                if images:messages.append({'role':'user','content':[{'type':'text','text':'Image from preceding tool result.'},*images]})
        else:
            messages.append({'role':'user','content':[{'type':'tool_result','tool_use_id':r['call_id'],
                'content':[{'type':'text','text':r['content']},*saved_image(r,'anthropic')],
                'is_error':r['state']!='completed'} for r in results]})
    if type(current_ids) is not list or any(type(x) is not str or not x for x in current_ids) or len(set(current_ids))!=len(current_ids) or seen.intersection(current_ids):
        raise ValueError('Invalid Go history')
    if '_go_history' in state:
        current=({'role':'assistant','content':state.get('assistant_content')} if protocol=='anthropic_messages' else state.get('assistant'))
        if set(_pair_ids(protocol,current))!=set(current_ids): raise ValueError('Invalid Go history')
    return messages


def validate_go_history(request, state):
    from app.services.providers.base import ProviderFailure
    if '_go_history' not in state: return
    if (state.get('_go_history_scope')!=go_history_scope(request) or type(request.turn_generation) is not int
            or request.turn_generation<1 or type(request.connection.owner_id) is not int or request.connection.owner_id<1):
        raise ProviderFailure('invalid_continuation','Go tool history does not match this execution.')
    try: go_history_messages(state,request.connection.protocol_id,state.get('call_ids'),budget_only=True)
    except (TypeError,ValueError): raise ProviderFailure('invalid_continuation','Go tool history is invalid.') from None


def advance_go_history(request, next_state):
    """Keep this turn's approved tool pairs and images in memory only."""
    from copy import deepcopy
    from app.services.providers.base import ProviderFailure
    if next_state is None or request.turn_generation is None or request.vision is not None:
        return next_state
    if type(request.turn_generation) is not int or request.turn_generation<1 or type(request.connection.owner_id) is not int or request.connection.owner_id<1:
        raise ProviderFailure('invalid_continuation','Go tool history authority is unavailable.')
    prior=request.continuation
    history=[]
    if prior is not None:
        validate_go_history(request,prior)
        history=deepcopy(prior.get('_go_history',[]))
        protocol=request.connection.protocol_id
        assistant=({'role':'assistant','content':prior.get('assistant_content')} if protocol=='anthropic_messages' else prior.get('assistant'))
        try: ids=_pair_ids(protocol,assistant)
        except (TypeError,ValueError): raise ProviderFailure('invalid_continuation','Go tool history is invalid.') from None
        observations=request.observations
        if len(ids)!=len(observations) or set(ids)!={o.call_id for o in observations}:
            raise ProviderFailure('invalid_continuation','Go tool observations do not match.')
        saved=[]
        for o in observations:
            row={'call_id':o.call_id,'state':o.state,'content':observation_text(o)}
            if o.image_bytes is not None:
                if (o.state!='completed' or not isinstance(o.image_bytes,bytes) or not 1<=len(o.image_bytes)<=10000000
                        or o.image_mime not in ('image/png','image/jpeg')
                        or canonical_screenshot_ref(o.screenshot_ref,request.room_id)!=o.screenshot_ref or not o.screenshot_ref):
                    raise ProviderFailure('invalid_continuation','Go image history is invalid.')
                row.update(image_bytes=o.image_bytes,image_mime=o.image_mime)
            saved.append(row)
        history.append({'assistant':deepcopy(assistant),'results':saved})
    result={**next_state,'_go_history':history,'_go_history_scope':go_history_scope(request)}
    validate_go_history(request,result)
    return result


def go_continuation_body(*,protocol,model,system,tools,messages,state,observations,temperature=None,max_tokens=None,budget_only=False):
    if not isinstance(state,dict) or not isinstance(observations,list):raise ValueError('Invalid continuation')
    history=go_history_messages(state,protocol,state.get('call_ids',[]),budget_only=budget_only)
    total=sum(len(r['image_bytes']) for pair in state.get('_go_history',[]) for r in pair['results'] if 'image_bytes' in r)
    total+=sum(len(o.image_bytes) for o in observations if o.image_bytes is not None)
    if total>10000000:raise ValueError('Image budget exceeded')
    if protocol=='anthropic_messages':
        blocks=state.get('assistant_content')
        if not isinstance(blocks,list):raise ValueError('Invalid continuation')
        results=[{'type':'tool_result','tool_use_id':o.call_id,'content':[{'type':'text','text':observation_text(o)},
                  *([_image(o,'anthropic',budget_only)] if o.image_bytes is not None else [])],
                  'is_error':o.state!='completed'} for o in observations]
        body={'model':model,'system':system,'messages':[*messages,*history,{'role':'assistant','content':blocks},
              {'role':'user','content':results}],'max_tokens':max_tokens or 4096}
        if temperature is not None:body['temperature']=temperature
        if tools:body['tools']=[{'name':wire_tool_name(t['name']),'description':t.get('description',''),'input_schema':t['parameters']} for t in tools]
    elif protocol=='openai_chat_completions':
        assistant=state.get('assistant')
        if not isinstance(assistant,dict):raise ValueError('Invalid continuation')
        result=[]
        for o in observations:
            result.append({'role':'tool','tool_call_id':o.call_id,'content':observation_text(o)})
            if o.image_bytes is not None:
                result.append({'role':'user','content':[{'type':'text','text':'Image from preceding tool result.'},_image(o,'openai',budget_only)]})
        body={'model':model,'messages':[{'role':'system','content':system},*messages,*history,assistant,*result]}
        if temperature is not None:body['temperature']=temperature
        if max_tokens is not None:body['max_tokens']=max_tokens
        if tools:body['tools']=[{'type':'function','function':{'name':wire_tool_name(t['name']),
            'description':t.get('description',''),'parameters':t['parameters']}} for t in tools]
    else:raise ValueError('Invalid Go protocol')
    return body

def go_continuation_text_size(**kwargs):
    return len(provider_json_text(go_continuation_body(**kwargs,budget_only=True)))


def go_initial_body(*,protocol,model,system,tools,messages,temperature=None,max_tokens=None):
    if protocol=='anthropic_messages':
        body={'model':model,'system':system,'messages':messages,'max_tokens':max_tokens or 4096}
        if temperature is not None:body['temperature']=temperature
        if tools:body['tools']=[{'name':wire_tool_name(t['name']),'description':t.get('description',''),'input_schema':t['parameters']} for t in tools]
    elif protocol=='openai_chat_completions':
        body={'model':model,'messages':[{'role':'system','content':system},*messages]}
        if temperature is not None:body['temperature']=temperature
        if max_tokens is not None:body['max_tokens']=max_tokens
        if tools:body['tools']=[{'type':'function','function':{'name':wire_tool_name(t['name']),'description':t.get('description',''),'parameters':t['parameters']}} for t in tools]
    else:raise ValueError('Invalid Go protocol')
    return body
