"""ChatGPT plan Responses/SSE adapter with verified files and room collaboration.

A model catalog is availability metadata, not proof of image/audio/tool support.
Unsupported capabilities fail before inference; no alternate billing fallback.
"""
import asyncio
import base64
import json
from hashlib import sha256
import re
import httpx
from .base import (ProviderAdapter, ProviderFailure, ProviderRequest, ProviderResult,
                   UnsupportedOption, attributed_messages, check_options, check_continuation, continuation_scope,
                   normalized_call, observation_text, wire_tool_name, verified, canonical_screenshot_ref, provider_json_bytes, provider_json_text)


def _png_part(item, *, expected=None):
    from app.services.tools.desktop import validate_screenshot_png
    image = item.image_bytes
    if not isinstance(image, bytes) or item.image_mime != 'image/png' or len(image) > 10_000_000:
        raise UnsupportedOption('vision image')
    try:
        validate_screenshot_png(image, expected)
    except Exception:
        raise UnsupportedOption('vision image') from None
    return {'type': 'input_image', 'image_url': 'data:image/png;base64,' + base64.b64encode(image).decode('ascii')}


def _bounded_text_size(value, *, allow_image):
    """Only actual Responses input content can use the separate PNG budget."""
    total = 0
    inputs = value.get('input') if isinstance(value, dict) else value
    locations = set()
    if isinstance(inputs, list):
        for i, entry in enumerate(inputs):
            if not isinstance(entry, dict): continue
            if entry.get('role') == 'user' and isinstance(entry.get('content'), list):
                locations.add((i, 'content'))
            if entry.get('type') == 'function_call_output' and isinstance(entry.get('output'), list):
                locations.add((i, 'output'))
    def visit(item, path=()):
        nonlocal total
        # Paths refer only to a message content part or a function result part.
        p = path[1:] if path and path[0] == 'input' else path
        image_location = len(p) == 3 and p[:2] in locations and type(p[2]) is int
        if isinstance(item, dict):
            in_input = bool(path) and (path[0] == 'input' if isinstance(value,dict) else type(path[0]) is int)
            if in_input and item.get('type') == 'input_image' and not image_location:
                raise UnsupportedOption('vision image')
            if image_location and item.get('type') == 'input_image':
                if not allow_image: raise UnsupportedOption('vision')
                url = item.get('image_url'); prefix = 'data:image/png;base64,'
                if (set(item) != {'type', 'image_url'} or not isinstance(url, str)
                        or not url.startswith(prefix) or len(url) > 13_333_358):
                    raise UnsupportedOption('vision image')
                try:
                    raw = base64.b64decode(url[len(prefix):], validate=True)
                    from app.services.tools.desktop import validate_screenshot_png
                    validate_screenshot_png(raw)
                except Exception:
                    raise UnsupportedOption('vision image') from None
                total += len(raw)
                if total > 10_000_000: raise UnsupportedOption('vision image')
                return {**item, 'image_url': '[bounded PNG]'}
            return {k: visit(v, (*path, k)) for k,v in item.items()}
        if isinstance(item,list): return [visit(v, (*path, i)) for i,v in enumerate(item)]
        return item
    return len(provider_json_text(visit(value)))


def chatgpt_filtered_tools(connection, tools):
    """Apply the adapter tool filter without claiming runtime permission."""
    from app.services.model_features import record
    # Tool support is exact-account/model evidence, not a provider-wide claim.
    proof=record(connection).get('tool_families',{})
    allowed={name for name in ('read_file','write_file') if proof.get(name) is True}
    # check_options already requires exact-model tool support. Collaboration
    # stays within the scheduler's Owner/room boundary and grants no OS access.
    allowed.update(('synthesize_voice_memo', 'browser_screenshot', 'browser_navigate', 'browser_inspect', 'browser_interact', 'list_teammates', 'message_teammate', 'read_teammate_result', 'read_selected_file', 'list_bots', 'message_bot', 'message_bots', 'create_bot', 'react_to_message', 'request_vault_login', 'list_vault_logins', 'list_vault_cards', 'update_vault_card_labels', 'list_vault_contacts', 'create_vault_contact', 'update_vault_contact', 'delete_vault_contact', 'update_vault_login_labels', 'save_bot_memory', 'edit_bot_memory', 'forget_bot_memory', 'change_bot_role', 'set_bot_memory_scope', 'set_memory_group_access', 'save_owner_fact', 'edit_owner_fact', 'forget_owner_fact', 'save_owner_skill', 'edit_owner_skill', 'create_bot_routine', 'edit_bot_routine', 'set_bot_routine', 'test_bot_routine', 'read_bot_routine', 'write_page'))
    return [tool for tool in tools if tool.get('name') in allowed or (isinstance(tool.get('mcp_binding'),dict) and isinstance(tool.get('name'),str) and re.fullmatch(r'mcp_[a-f0-9]{20}',tool['name']))]


def chatgpt_request_parts(request, projected_inputs=None):
    """The exact bounded text portions used by the Responses adapter."""
    tools=chatgpt_filtered_tools(request.connection, request.tools)
    if request.tools and not tools:raise UnsupportedOption('tools')
    inputs=attributed_messages(request) if projected_inputs is None else projected_inputs
    if request.continuation is not None:
        state=request.continuation
        check_continuation(request,state,'chatgpt')
        expected=state.get('call_ids')
        if (not isinstance(expected,list) or len(set(expected))!=len(expected)
                or set(expected)!={o.call_id for o in request.observations}
                or len(expected)!=len(request.observations)
                or not isinstance(state.get('input'),list)):
            raise ProviderFailure('invalid_continuation','Tool observations do not match pending ChatGPT calls.')
        outputs=[]
        for o in request.observations:
            content=observation_text(o)
            if o.image_bytes is not None:
                if (not verified(request, 'vision') or o.state != 'completed'
                        or not isinstance(o.screenshot_ref, str)
                        or canonical_screenshot_ref(o.screenshot_ref, request.room_id) != o.screenshot_ref):
                    raise UnsupportedOption('vision image')
                content=[{'type':'input_text','text':content}, _png_part(o, expected=(o.image_width,o.image_height))]
            outputs.append({'type':'function_call_output','call_id':o.call_id,'output':content})
        inputs=[*state['input'],*outputs]
    elif request.observations:
        raise ProviderFailure('invalid_continuation','Tool results require a pending ChatGPT call.')
    if request.vision is not None:
        if not verified(request, 'vision'):
            raise UnsupportedOption('vision')
        inputs=[*inputs, {'role':'user','content':[{'type':'input_text','text':request.vision.prompt}, _png_part(request.vision)]}]
    if _bounded_text_size(inputs, allow_image=verified(request, 'vision'))>128000:
        raise ProviderFailure('context_too_large','ChatGPT tool history exceeds the bounded context budget.')
    instructions=request.system+'\nUse only offered tools for their matching purposes. Persistent routine, memory and Skill changes require the matching offered tool and a completed receipt; connected apps require an offered app tool. Tool calls propose actions; only completed tool results prove execution. A teammate delivery receipt is not its answer; finish this turn after sending and resume when its actual reply is supplied. Collaboration grants no additional permissions. Use only the image content and tools actually supplied in this request; do not claim unavailable audio or tools.'
    wire_tools=[{'type':'namespace','name':'gamja','description':'Offered tools for files, Bot collaboration, memory, Skills, routines and connected apps; each action retains its existing authority and approval requirements.',
                 'tools':[{'type':'function','name':wire_tool_name(t['name']),'description':t.get('description',''),
                           'parameters':t['parameters'],'strict':False} for t in tools]}] if tools else []
    return {'instructions':instructions,'input':inputs,'tools':wire_tools},tools

def chatgpt_request_body(request, projected_inputs=None):
    parts,tools=chatgpt_request_parts(request, projected_inputs)
    body={'model':request.model_id,'instructions':parts['instructions'],
          'input':parts['input'],'store':False,'stream':True}
    if request.model_id=='gpt-6-luna':body['reasoning']={'effort':'none'}
    if tools:
        body['tools']=parts['tools']
        body['parallel_tool_calls']=False
    return body,tools

# This is local serialization evidence, not proof of provider receipt. It never
# records request instructions, arguments, other tool names, or schema text.
def chatgpt_wire_diagnostic(body):
    allowed = {'create_bot_routine', 'edit_bot_routine', 'set_bot_routine',
               'test_bot_routine', 'read_bot_routine', 'read_file', 'write_file'}
    names = {wire_tool_name(name): name for name in allowed}
    entries = []
    groups = body.get('tools', [])
    if not isinstance(groups, list):
        return None
    count = 0
    for group in groups:
        if not isinstance(group, dict) or group.get('type') != 'namespace' or group.get('name') != 'gamja':
            continue
        tools = group.get('tools')
        if not isinstance(tools, list):
            return None
        count += len(tools)
        for item in tools:
            if not isinstance(item, dict) or item.get('type') != 'function':
                continue
            wire = item.get('name')
            if not isinstance(wire, str) or wire not in names or not isinstance(item.get('parameters'), dict):
                continue
            raw = json.dumps(item['parameters'], sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')
            entries.append({'name': wire, 'schema_sha256': sha256(raw).hexdigest()})
    if count > 1000 or len(entries) > len(allowed) or len({e['name'] for e in entries}) != len(entries):
        return None
    return {'version': 1, 'stage': 'post_returned', 'count': count,
            'tools': sorted(entries, key=lambda e: e['name'])}


def chatgpt_request_text_size(request):
    body,_=chatgpt_request_body(request)
    # Match HTTPX JSON encoding, including escaped non-ASCII and body metadata.
    return _bounded_text_size(body, allow_image=verified(request, 'vision'))


def freeze_history_prefix(request):
    """Identify only unique, exact pre-root wire items, preserving ambiguous inputs."""
    from copy import deepcopy
    from .base import projected_messages, value
    inputs=attributed_messages(request)
    events=sorted(request.events,key=lambda e:value(e,'seq'))
    matches=[e for e in events if value(e,'event_id')==request.trigger_event_id
             and value(e,'actor_type')=='user' and value(e,'kind')=='message'
             and value(e,'seq')<=request.watermark_seq]
    frozen={'prefix':deepcopy(inputs),'removable':[]}
    if len(matches)!=1:return frozen
    active=matches[0]
    projected=projected_messages(events=[active],watermark_seq=request.watermark_seq,agent_id=request.agent_id,
        trigger_event_id=request.trigger_event_id,memory=None,room_id=request.room_id)
    if not projected:return frozen
    # The active message itself must have exactly one position in the whole
    # frozen input. Its generated trailing instructions are never removable.
    root_item=projected[0]
    root_positions=[i for i,item in enumerate(inputs) if item==root_item]
    if len(root_positions)!=1:return frozen
    root_position=root_positions[0];start=1 if request.memory else 0
    if root_position<start:return frozen
    old=[]
    for event in events:
        if value(event,'seq')>=value(active,'seq'):break
        if value(event,'seq')>request.watermark_seq:break
        payload=value(event,'payload')
        if (value(event,'kind')=='tool_result' and
                (not isinstance(payload,dict) or payload.get('state') not in ('completed','failed','denied','cancelled'))):
            continue  # Never compact an uncertain or otherwise unresolved result.
        one=projected_messages(events=[event],watermark_seq=request.watermark_seq,agent_id=request.agent_id,
            trigger_event_id=None,memory=None,room_id=request.room_id)
        if len(one)==1:old.append(one[0])
    candidates=[]
    for item in old:
        # Duplicates in either source events or the entire prefix cannot prove
        # identity. Memory and every input at/after the root remain untouched.
        if sum(other==item for other in old)!=1:continue
        positions=[i for i,value_ in enumerate(inputs) if value_==item]
        if len(positions)!=1 or not start<=positions[0]<root_position:continue
        candidates.append(positions[0])
    # Any reordering invalidates the whole candidate mapping.
    if candidates!=sorted(set(candidates)):return frozen
    frozen['removable']=candidates
    return frozen


def compact_frozen_history(request, frozen, limit):
    """Remove only verified old event inputs, keeping all turn outputs and receipts intact."""
    from copy import deepcopy
    from dataclasses import replace
    from .base import check_continuation
    state=request.continuation
    if not isinstance(state,dict) or not isinstance(frozen,dict):
        raise ProviderFailure('invalid_continuation','Frozen provider context is unavailable.')
    check_continuation(request,state,'chatgpt')
    prefix=frozen.get('prefix');indices=frozen.get('removable');inputs=state.get('input')
    if (not isinstance(prefix,list) or not isinstance(indices,list) or not isinstance(inputs,list)
            or inputs[:len(prefix)]!=prefix or any(type(i) is not int or i<0 or i>=len(prefix) for i in indices)
            or indices!=sorted(set(indices))):
        raise ProviderFailure('invalid_continuation','Frozen provider context changed.')
    removed=[]
    for index in indices:
        if chatgpt_request_text_size(request)<=limit:break
        removed.append(index)
        keep=[item for i,item in enumerate(prefix) if i not in removed]
        updated={**state,'input':[*keep,*inputs[len(prefix):]]}
        request=replace(request,continuation=updated)
    keep=[item for i,item in enumerate(prefix) if i not in removed]
    remaining=[i-sum(j<i for j in removed) for i in indices if i not in removed]
    return request,{'prefix':deepcopy(keep),'removable':remaining}


class ChatGPTAdapter(ProviderAdapter):
    kind = 'chatgpt'
    origin = 'https://api.openai.com/v1'

    async def generate(self,request:ProviderRequest)->ProviderResult:
        check_options(request)
        if request.temperature is not None or request.max_tokens is not None:
            raise UnsupportedOption('temperature/max_tokens')
        from app.services.model_features import permitted, record
        if request.room_id > 0 and not permitted(request.connection,'text_generation'):
            raise ProviderFailure('model_feature_unavailable','Run the exact ChatGPT model text check before using this Agent.')
        body,tools=chatgpt_request_body(request)
        inputs=body['input']
        # The selected account's real catalog is checked before every inference.
        from app.services.connections import discover_models
        items,_,_=await discover_models(request.connection, transport=self.transport)
        if request.model_id not in {item['id'] for item in items}:
            raise ProviderFailure('model_not_found','This model is not available in the selected ChatGPT account. Refresh its model list.')
        data=await self._post(request,'/responses',{'Authorization':'Bearer '+request.connection.api_key},body)
        wire_diagnostic = chatgpt_wire_diagnostic(body)
        try:
            output=data.get('_gamja_stream_output') or data['output']
            text=data.get('_gamja_stream_text') or '\n'.join(part['text'] for item in output if item.get('type')=='message'
                           for part in item.get('content',[]) if part.get('type')=='output_text')
            calls=[]
            for item in output:
                if item.get('type')!='function_call':continue
                if item.get('namespace') not in (None,'gamja'):raise ValueError()
                calls.append(normalized_call(item.get('call_id'),item.get('name'),json.loads(item['arguments']),tools))
            if len({c.call_id for c in calls})!=len(calls) or len(calls)>8:raise ValueError()
            if not text.strip() and not calls:raise ValueError()
        except (KeyError,TypeError,ValueError,AttributeError):
            raise ProviderFailure('provider_invalid_response','ChatGPT returned no valid completed text or allowed tool response.') from None
        continuation=({'provider_kind':self.kind,**continuation_scope(request),'input':[*inputs,*output],
                       'call_ids':[c.call_id for c in calls]} if calls else None)
        return ProviderResult(text=text,tool_calls=calls,continuation=continuation,
                              model_use={'connection_id':request.connection_id,'model_id':request.model_id,'provider_kind':self.kind,'watermark_seq':request.watermark_seq},
                              wire_diagnostic=wire_diagnostic)

    async def _post_unmetered(self,request,path,headers,body):
        from app.services.connections import provider_runtime_origin
        contract,origin=provider_runtime_origin(request.connection)
        if contract.provider_id!=self.kind or origin!=self.origin or path!='/responses':
            raise ProviderFailure('invalid_configuration','ChatGPT Responses origin mismatch.')
        async def receive():
            async with httpx.AsyncClient(transport=self.transport,trust_env=False,follow_redirects=False,timeout=httpx.Timeout(120,connect=10)) as client:
                async with client.stream('POST',origin+path,headers={**headers,'Content-Type':'application/json'},content=provider_json_bytes(body)) as response:
                    if response.status_code in (401,403):raise ProviderFailure('invalid_credential','ChatGPT plan permission or account access was rejected.',http_status=response.status_code)
                    if response.status_code==429:raise ProviderFailure('rate_limited','ChatGPT plan usage is limited. No automatic retry or API-key fallback was used.',http_status=429)
                    if response.status_code>=300:raise ProviderFailure('provider_error','ChatGPT Responses request failed.',http_status=response.status_code)
                    if response.headers.get('content-type') and 'text/event-stream' not in response.headers.get('content-type','').lower():raise ProviderFailure('provider_invalid_response','ChatGPT did not return an event stream.')
                    total=0;event_data=[];text_parts={};output_items={}
                    async for line in response.aiter_lines():
                        total+=len(line)
                        if total>4000000:raise ProviderFailure('provider_invalid_response','ChatGPT response exceeded the bounded stream size.')
                        if line.startswith('data:'):event_data.append(line[5:].lstrip())
                        elif not line and event_data:
                            raw='\n'.join(event_data);event_data=[]
                            if raw=='[DONE]':continue
                            try:event=json.loads(raw)
                            except ValueError:raise ProviderFailure('provider_invalid_response','ChatGPT returned an invalid event.') from None
                            kind=event.get('type')
                            if kind=='response.output_text.delta':
                                delta=event.get('delta')
                                if not isinstance(delta,str):raise ProviderFailure('provider_invalid_response','ChatGPT returned an invalid text delta.')
                                key=(event.get('item_id'),event.get('content_index',0))
                                text_parts[key]=text_parts.get(key,'')+delta
                            if kind=='response.output_item.done':
                                item=event.get('item')
                                if not isinstance(item,dict):raise ProviderFailure('provider_invalid_response','Invalid completed output item.')
                                output_items[event.get('output_index',len(output_items))]=item
                            if kind=='response.completed':
                                value=event.get('response',{})
                                if value.get('status')!='completed':raise ProviderFailure('provider_invalid_response','ChatGPT response is not completed.')
                                if output_items:value['_gamja_stream_output']=[output_items[k] for k in sorted(output_items)]
                                if text_parts:value['_gamja_stream_text']='\n'.join(text_parts.values())
                                return value
                            if kind in {'error','response.failed','response.incomplete','response.cancelled'}:
                                raise ProviderFailure('provider_error','ChatGPT did not complete the request; usage may be unknown.')
                    raise ProviderFailure('provider_invalid_response','ChatGPT stream ended without response.completed.')
        work=None;cancel=None
        try:
            async with asyncio.timeout(120):
                work=asyncio.create_task(receive())
                if request.cancel is None:return await work
                cancel=asyncio.create_task(request.cancel.wait())
                done,_=await asyncio.wait({work,cancel},return_when=asyncio.FIRST_COMPLETED)
                if cancel in done:raise ProviderFailure('cancelled','ChatGPT request cancelled; no retry was sent.')
                return await work
        except (TimeoutError,httpx.HTTPError):raise ProviderFailure('unreachable','ChatGPT request timed out or could not connect; no retry was sent.') from None
        finally:
            tasks=[item for item in (work,cancel) if item is not None]
            for item in tasks:
                if not item.done():item.cancel()
            if tasks:await asyncio.gather(*tasks,return_exceptions=True)
