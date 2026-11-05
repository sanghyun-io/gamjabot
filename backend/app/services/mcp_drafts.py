"""Owner edits for a pending MCP proposal; saving never grants execution."""
import copy,json,re
from datetime import datetime,timezone
from hashlib import sha256
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import text
from app.models.collaboration import ToolAction
from app.models.mcp import MCPOwnerDraft,MCPCommand,MCPTool
from app.services import mcp_registry,room_service
from app.services.action_review import ReviewChanged
from app.services.review_snapshot import capture_review,stage_initial_review,same_review_observations
from app.services.events import ActorRef,stage_event

SENSITIVE=re.compile(r'(?i)(password|secret|token|api[_-]?key|authorization|credential|private[_-]?key)')
MAX_EDITS=32000

def fail(code='mcp_draft_changed',status=409):raise HTTPException(status,{'code':code})
def key_for(room,action,key):
    if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',key):fail('mcp_draft_key_invalid',422)
    return 'mcpdraft_'+sha256(json.dumps([room,action,key],separators=(',',':')).encode()).hexdigest()
def pointer(parts):return '/'+ '/'.join(str(v).replace('~','~0').replace('/','~1') for v in parts)
def _hidden(schema,key):
    return (bool(SENSITIVE.search(key)) or schema.get('writeOnly') is True or schema.get('readOnly') is True or schema.get('x-sensitive') is True
            or schema.get('format') in ('password','secret') or schema.get('x-hidden') is True)
def fields(schema,raw):
    """Expose schema-declared values and missing optional fields, never hidden keys."""
    result=[];values={};visits=0;missing=object()
    def add(path,typ,value,maximum,max_items=None):
        if not path:return
        entry={'path':pointer(path),'label':str(path[-1])[:100],'role':'text','type':typ,'max':max(0,maximum)}
        if max_items is not None:entry['max_items']=max(0,max_items)
        result.append(entry)
        if value is not missing:values[entry['path']]=copy.deepcopy(value)
    def walk(node,value,path,depth,seen):
        nonlocal visits
        visits+=1
        if visits>4096 or depth>32 or len(result)>=128 or not isinstance(node,dict):return
        if node.get('writeOnly') is True or node.get('readOnly') is True or node.get('x-sensitive') is True or node.get('x-hidden') is True or node.get('format') in ('password','secret'):return
        ref=node.get('$ref')
        if ref is not None:
            if not isinstance(ref,str) or not ref.startswith('#/') or ref in seen:return
            target=schema
            try:
                for part in ref[2:].split('/'):
                    if re.search(r'~(?![01])',part):return
                    part=part.replace('~1','/').replace('~0','~');target=target[part]
            except (KeyError,TypeError):return
            if any(k in node for k in ('allOf','anyOf','oneOf')):return
            walk(target,value,path,depth+1,seen|{ref});return
        if any(k in node for k in ('allOf','anyOf','oneOf')):return
        typ=node.get('type')
        if typ=='object' and isinstance(value,dict) and isinstance(node.get('properties'),dict):
            required=node.get('required',[])
            for k,sub in node['properties'].items():
                if (isinstance(k,str) and isinstance(sub,dict) and not _hidden(sub,k)
                        and (k in value or k not in required)):
                    walk(sub,value.get(k,missing),path+[k],depth+1,seen)
        elif typ=='array' and (value is missing or isinstance(value,list)):
            item=node.get('items')
            if isinstance(item,dict) and item.get('type')=='string' and not _hidden(item,''):
                if value is not missing and (len(value)>100 or any(not isinstance(v,str) or len(v)>16000 or re.search(r'(?i)bearer\s+\S+',v) for v in value)):return
                maximum=min(item.get('maxLength',16000) if type(item.get('maxLength')) is int else 16000,16000)
                max_items=min(node.get('maxItems',100) if type(node.get('maxItems')) is int else 100,100)
                add(path,'string_array',value,maximum,max_items)
            elif isinstance(item,dict) and value is not missing:
                for index,v in enumerate(value[:100]):walk(item,v,path+[str(index)],depth+1,seen)
        elif typ=='string' and (value is missing or isinstance(value,str)):
            if value is not missing and (len(value)>16000 or re.search(r'(?i)bearer\s+\S+',value)):return
            maximum=min(node.get('maxLength',16000) if type(node.get('maxLength')) is int else 16000,16000)
            add(path,'string',value,maximum)
    walk(schema,raw,[],0,set())
    return result,values

def _action(db,owner,room,action,*,active=True):
    room_service.require_room(db,room,owner,active=active)
    row=db.get(ToolAction,action)
    if not row or row.room_id!=room or row.family!='mcp_tool_call':fail('mcp_draft_not_found',404)
    return row

def _live(db,broker,row,owner):
    if (row.state!='waiting' or row.started_at is not None or row.approved_at is not None
            or row.approval_owner_id is not None or row.grant_id is not None or row.approval_scope is not None
            or row.session_id!=broker.session_id):raise ReviewChanged('Draft action unavailable')
    original,latest,prepared=broker._review_observations(db,row,owner)
    if not same_review_observations(original,latest):raise ReviewChanged('Draft review changed')
    tool=db.get(MCPTool,prepared.mcp_binding['tool_id'])
    if not tool:raise ReviewChanged('Draft schema unavailable')
    return prepared,tool

def read(db,broker,owner,room,action):
    row=_action(db,owner,room,action,active=False);draft=db.get(MCPOwnerDraft,action)
    if draft and (draft.owner_id!=owner or draft.room_id!=room):fail('mcp_draft_not_found',404)
    descriptors=[];values={};available=False
    if broker is not None and hasattr(broker,'_previews'):
        try:
            prepared,tool=_live(db,broker,row,owner);descriptors,values=fields(tool.input_schema,prepared.raw_arguments);available=bool(descriptors)
        except (ReviewChanged,ValueError,TypeError,KeyError):pass
    summary=draft.original_summary if draft and isinstance(draft.original_summary,dict) else {}
    arguments=summary.get('arguments') if isinstance(summary.get('arguments'),dict) else {}
    binding=summary.get('mcp_binding') if isinstance(summary.get('mcp_binding'),dict) else {}
    def safe_hash(value):return value if isinstance(value,str) and re.fullmatch(r'[a-f0-9]{64}',value) else None
    original={'payload_digest':safe_hash(summary.get('payload_digest')),
              'arguments_sha256':safe_hash(arguments.get('arguments_sha256')),
              'schema_sha256':safe_hash(binding.get('schema_sha256'))} if draft else None
    return {'version':1,'action_id':action,'review_id':row.review_id,'revision':draft.revision if draft else 0,
            'fields':descriptors,'values':values,'edits':copy.deepcopy(draft.edits) if draft else {},
            'pending':bool(draft.pending) if draft else False,'available':available,
            'original_review_id':draft.original_review_id if draft else None,'original_summary':original}

def _prior(db,owner,room,action,key,operation,signature):
    command=db.get(MCPCommand,(owner,key_for(room,action,key)))
    if not command:return None
    if command.request_digest!=signature or command.response.get('operation')!=operation:fail('mcp_draft_idempotency_conflict')
    return command.response

def command(db,owner,room,action,key):
    _action(db,owner,room,action,active=False)
    row=db.get(MCPCommand,(owner,key_for(room,action,key)))
    if not row:fail('mcp_draft_command_not_found',404)
    return row.response

def _validate_edits(edits,descriptors):
    if not isinstance(edits,dict):fail('mcp_draft_invalid',422)
    try:encoded=json.dumps(edits,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
    except (TypeError,ValueError):fail('mcp_draft_invalid',422)
    if len(encoded)>MAX_EDITS:fail('mcp_draft_invalid',422)
    allowed={d['path']:d for d in descriptors}
    for path,value in edits.items():
        d=allowed.get(path)
        if not d:fail('mcp_draft_invalid',422)
        if d['type']=='string':
            if not isinstance(value,str) or len(value)>d['max']:fail('mcp_draft_invalid',422)
        elif not isinstance(value,list) or len(value)>d['max_items'] or any(not isinstance(v,str) or len(v)>d['max'] for v in value):fail('mcp_draft_invalid',422)
    return copy.deepcopy(edits)

def save(db,broker,owner,room,action,revision,review,edits,key):
    try:
        if len(json.dumps(edits,ensure_ascii=False,allow_nan=False).encode())>MAX_EDITS:fail('mcp_draft_invalid',422)
        signature=mcp_registry.digest({'operation':'save','action_id':action,'revision':revision,'review_id':review,'edits':edits})
    except (ValueError,TypeError,UnicodeEncodeError):fail('mcp_draft_invalid',422)
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        row=_action(db,owner,room,action);prior=_prior(db,owner,room,action,key,'save',signature)
        if prior:db.commit();return prior
        draft=db.get(MCPOwnerDraft,action)
        if (row.review_id!=review or (draft.revision if draft else 0)!=revision
                or draft and (draft.owner_id!=owner or draft.room_id!=room or draft.base_review_id!=review)):fail()
        try:prepared,tool=_live(db,broker,row,owner)
        except (ReviewChanged,ValueError,TypeError,KeyError):fail()
        descriptors,_=fields(tool.input_schema,prepared.raw_arguments);validated=_validate_edits(edits,descriptors)
        if not draft:
            draft=MCPOwnerDraft(action_id=action,owner_id=owner,room_id=room,revision=0,base_review_id=review,
                edits={},pending=False,original_review_id=review,original_summary={'arguments':row.arguments,'payload_digest':row.payload_digest,'mcp_binding':row.mcp_binding},updated_at=datetime.now(timezone.utc))
            db.add(draft)
        draft.edits=validated;draft.pending=bool(validated);draft.revision+=1;draft.updated_at=datetime.now(timezone.utc)
        result={'version':1,'operation':'save','state':'saved','command_key':key,'action_id':action,'review_id':review,'revision':draft.revision}
        db.add(MCPCommand(owner_id=owner,command_key=key_for(room,action,key),request_digest=signature,response=result));db.commit();return result
    except BaseException:db.rollback();raise

def _merge(raw,edits):
    value=copy.deepcopy(raw)
    for pointer_text,new in edits.items():
        parts=[p.replace('~1','/').replace('~0','~') for p in pointer_text[1:].split('/')]
        cursor=value
        for part in parts[:-1]:cursor=cursor[int(part)] if isinstance(cursor,list) else cursor[part]
        last=parts[-1]
        if isinstance(cursor,list):cursor[int(last)]=copy.deepcopy(new)
        else:cursor[last]=copy.deepcopy(new)
    return value

def apply(db,broker,owner,room,action,revision,review,key):
    signature=mcp_registry.digest({'operation':'apply','action_id':action,'revision':revision,'review_id':review})
    db.rollback();db.execute(text('BEGIN IMMEDIATE'));old=None;swapped=False;old_marker=broker._draft_applied.get(action)
    try:
        row=_action(db,owner,room,action);prior=_prior(db,owner,room,action,key,'apply',signature)
        if prior:db.commit();return prior
        draft=db.get(MCPOwnerDraft,action)
        if (not draft or draft.owner_id!=owner or draft.room_id!=room or draft.revision!=revision
                or draft.base_review_id!=review or row.review_id!=review or not draft.pending or not draft.edits):fail()
        try:
            old,tool=_live(db,broker,row,owner);descriptors,_=fields(tool.input_schema,old.raw_arguments)
            edits=_validate_edits(draft.edits,descriptors);raw=_merge(old.raw_arguments,edits)
            proof=old.mcp_binding;meta={k:proof[k] for k in ('server_id','config_revision','binding_revision','tool_id','tool_name','schema_sha256')}
            prepared=broker.prepare_mcp(db,owner_id=owner,agent_id=row.agent_id,room_id=room,root_id=row.trigger_event_id,
                generation=proof['generation'],meta=meta,arguments=raw,execution_binding=row.execution_binding,teammate_binding=proof.get('teammate_binding'))
            original,_,_=broker._review_observations(db,row,owner)
            row.arguments=prepared.public_arguments;row.payload_digest=prepared.payload_digest;row.mcp_binding=prepared.mcp_binding
            row.target=prepared.target;row.scope_target=prepared.scope_target;row.expected_effect=prepared.expected_effect
            row.review_id=None;row.review_snapshot=None
            stage_initial_review(row,capture_review(row,prepared,original.authority,broker.executor))
        except (ReviewChanged,ValueError,TypeError,KeyError,IndexError):fail()
        new_review=row.review_id;draft.base_review_id=new_review;draft.applied_review_id=new_review;draft.revision+=1;draft.pending=False;draft.updated_at=datetime.now(timezone.utc)
        result={'version':1,'operation':'apply','state':'applied','command_key':key,'action_id':action,'review_id':new_review,'revision':draft.revision}
        db.add(MCPCommand(owner_id=owner,command_key=key_for(room,action,key),request_digest=signature,response=result))
        stage_event(db,room,ActorRef('user',owner),'activity',{'category':'mcp_owner_draft','status':'applied','action_id':action,'review_id':new_review,'original_review_id':draft.original_review_id},'mcp-draft-apply:'+new_review)
        broker._previews[action]=prepared;broker._draft_applied[action]=(new_review,prepared.payload_digest);swapped=True
        db.commit();return result
    except BaseException:
        db.rollback()
        if swapped:
            broker._previews[action]=old
            if old_marker is None:broker._draft_applied.pop(action,None)
            else:broker._draft_applied[action]=old_marker
        raise
