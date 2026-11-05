"""Private, owner-root Page commits and canonical receipts."""
from datetime import datetime, timezone
from dataclasses import asdict, dataclass
from hashlib import sha256
from uuid import uuid4
import json
from fastapi import HTTPException
from sqlalchemy import text, update
from app.models.collaboration import RoomEvent
from app.models.room_result import RoomResult, RoomResultVersion, RoomResultCommand
from app.services import bot_creation, execution_scopes, skill_persistence, room_results
from app.services.action_review import ReviewChanged, observe_authority
from app.services.bot_configuration_binding import require_current, fingerprint
from app.services.events import ActorRef, stage_event
from app.services.llm_service import LLMFailure
from app.services.memory_context import snapshot
from app.services.execution_origin import ExecutionOrigin, require_execution_origin_current
from app.models.execution_scope import ExecutionScope
from app.services.page_documents import PageDocumentError, normalize_document, PAGE_DOCUMENT_SCHEMA, safe_row_shape, safe_block_shape, _BLOCK_FIELDS

# Keep the always-offered wire schema compact; normalize_document remains the
# authoritative, strict nested validator after the model returns the document.
_WIRE_DOCUMENT_SCHEMA = {
    'type':'object',
    'properties':{
        'format':{'const':'gamja.page.v1'}, 'title':{'type':'string'},
        'datasets':{'type':'array','items':{'type':'object'}},
        'views':{'type':'array','items':{'type':'object'}},
        'blocks':{'type':'array','minItems':1,'items':{'type':'object'}},
    },
    'required':['format','title','datasets','views','blocks'], 'additionalProperties':False,
}
_WIRE_GUIDANCE = (
    'datasets:[{id,columns:[{key,label,type:text|number|boolean|date}],rows:[{key:scalar|null}]}]; '
    'views:[{id,dataset,filters:[controlId]}]. heading(level 1|2|3,text); markdown(text); '
    'metric(view,aggregate count|sum|avg|min|max,label,field?); table(view,columns,title); '
    'chart(kind bar|line|pie,view,category,aggregate count|sum|avg,title,value?); '
    'control(id,view,field,mode search|select|toggle,label); columns(items:[blocks]); '
    'tabs(tabs:[{label,blocks}]). Set type,view.filters. '
    'search:text; toggle:boolean; count:no field/value; others:numeric. '
    'IDs: ^[a-z][a-z0-9_]{0,63}$ (e.g. task_1).'
)

TOOL_SCHEMAS = [{
    'name':'write_page',
    'description':('Private Page; temporary controls; listed keys only. '+_WIRE_GUIDANCE),
    'parameters':{'type':'object','properties':{'document':_WIRE_DOCUMENT_SCHEMA},
                  'required':['document'],'additionalProperties':False},
}]
NAMES = frozenset({'write_page'})
PAGE_ARGUMENT_ENVELOPE = {'type':'object','properties':{'document':{'type':'object'}},
                          'required':['document'],'additionalProperties':False}

def page_envelope_diagnostic(error, arguments):
    validator=error.validator if error.validator in {'type','required','additionalProperties'} else None
    if validator is None:return None
    path=list(error.absolute_path)
    field='document' if path==['document'] or (validator=='required' and isinstance(arguments,dict) and 'document' not in arguments) else None
    return {'tool':'write_page','validator':validator,'field':field}

def correction_tool_schema():
    from copy import deepcopy
    value=deepcopy(TOOL_SCHEMAS[0])
    document=deepcopy(PAGE_DOCUMENT_SCHEMA)
    document['properties']['datasets']['items']['properties']['rows']['items']={
        'type':'object','maxProperties':40,
        'additionalProperties':{'type':['string','number','boolean','null']}}
    base=document['properties']['blocks']['items']
    fields={
        'heading':(('type','level','text'),()), 'markdown':(('type','text'),()),
        'metric':(('type','view','aggregate','label'),('field',)),
        'table':(('type','view','columns','title'),()),
        'chart':(('type','kind','view','category','aggregate','title'),('value',)),
        'control':(('type','id','view','field','mode','label'),()),
        'columns':(('type','items'),()), 'tabs':(('type','tabs'),()),
    }
    def block_schema(nested):
        branches=[]
        for kind,(required,optional) in fields.items():
            properties={key:deepcopy(base['properties'][key]) for key in (*required,*optional)}
            properties['type']={'type':'string','enum':[kind]}
            if nested and kind=='columns':
                properties['items']={'type':'array','items':block_schema(False)}
            if nested and kind=='tabs':
                properties['tabs']={'type':'array','items':{'type':'object','properties':{
                    'label':{'type':'string'},'blocks':{'type':'array','items':block_schema(False)}},
                    'required':['label','blocks'],'additionalProperties':False}}
            branches.append({'type':'object','properties':properties,'required':list(required),'additionalProperties':False})
        return {'oneOf':branches}
    document['properties']['blocks']['items']=block_schema(True)
    value['parameters']['properties']['document']=document
    value['description'] += (' Rows are flat objects: every row key must exactly match a declared column key; '
        'each value is a scalar or null. Complete example: '
        '{"format":"gamja.page.v1","title":"Sales","datasets":[{"id":"d","columns":['
        '{"key":"group","label":"Group","type":"text"},{"key":"amount","label":"Amount","type":"number"}],'
        '"rows":[{"group":"A","amount":2}]}],"views":[{"id":"v","dataset":"d","filters":["choose"]}],'
        '"blocks":[{"type":"control","id":"choose","view":"v","field":"group","mode":"select","label":"Group"},'
        '{"type":"metric","view":"v","aggregate":"sum","field":"amount","label":"Total"},'
        '{"type":"chart","view":"v","kind":"bar","category":"group","aggregate":"sum","value":"amount","title":"Sales"}]}.')
    return value

class PageValidationFailure(LLMFailure):
    """A document was rejected before the Page transaction began."""
    def __init__(self, error):
        super().__init__(error.code,'The Page document was rejected before saving.')
        self.reason = error.reason
        self.path = error.path
        self.row_shape = safe_row_shape(getattr(error,'row_shape',None))
        self.block_shape = safe_block_shape(getattr(error,'block_shape',None))


def block_shape_guidance(value):
    shape = safe_block_shape(value)
    if shape is None:
        return ''
    kind=shape['block_type'];issue=shape['issue'];field=shape['missing_field']
    if issue=='missing_required':
        message=f" The {kind} block is missing required field {field}."
        if field=='aggregate' and kind in ('metric','chart'):
            message += (' Use aggregate count|sum|avg|min|max; count omits field and other aggregates require numeric field.'
                        if kind=='metric' else
                        ' Use aggregate count|sum|avg; count omits value and other aggregates require numeric value.')
        return message
    if issue=='extra_fields':
        required,optional=_BLOCK_FIELDS[kind]
        return f" The {kind} block has extra fields; use only {','.join(required+optional)}."
    return f" The {kind} block must be an object."


@dataclass(frozen=True)
class PagePermit:
    """Server-derived authority for a delegated new Page only."""
    owner_id: int
    room_id: int
    root_id: str
    agent_id: int
    generation: int
    source_profile_fingerprint: str
    proposer: object
    execution_binding: dict
    origin: ExecutionOrigin


@dataclass(frozen=True)
class TeammatePageOrigin:
    owner_id: int
    room_id: int
    root_id: str
    agent_id: int
    generation: int
    handoff_id: str
    phase: str
    sender_id: int
    recipient_id: int
    parent_id: str | None
    parent_identity: tuple | None
    depth: int
    request_event_id: str
    result_event_id: str | None
    delivery_key: str
    root_payload_hash: str
    request_payload_hash: str
    result_payload_hash: str | None

    @property
    def fingerprint(self):
        return sha256(json.dumps(asdict(self),sort_keys=True,separators=(',',':')).encode()).hexdigest()


@dataclass(frozen=True)
class TeammatePagePermit:
    owner_id: int
    room_id: int
    root_id: str
    agent_id: int
    generation: int
    source_profile_fingerprint: str
    proposer: object
    execution_binding: dict
    origin: TeammatePageOrigin


def load_teammate_origin(db, job, room_id, root_id, agent_id, generation):
    from app.models.teammate import TeammateHandoff
    from app.services import teammates
    if not isinstance(job,dict) or job.get('phase') not in ('response','return') or not isinstance(job.get('handoff_id'),str):
        raise LLMFailure('page_teammate_origin_unavailable','A current teammate task is required to create a Page.')
    row=db.get(TeammateHandoff,job['handoff_id'],populate_existing=True)
    teammates.require_job_current(db,job)
    root,room,_=teammates._root(db,room_id,root_id)
    _,run=teammates._member(db,room,agent_id)
    expected_agent=row.recipient_agent_id if job['phase']=='response' else row.sender_agent_id
    if ((row.room_id,row.root_request_event_id,row.owner_id,expected_agent)!=(room_id,root_id,room.user_id,agent_id)
            or job.get('agent_id')!=agent_id or row.sender_agent_id==row.recipient_agent_id
            or run.state!='running' or run.generation!=generation or run.trigger_event_id!=root_id):
        raise LLMFailure('page_teammate_origin_changed','The teammate task identity changed.')
    request=db.get(RoomEvent,row.request_event_id,populate_existing=True)
    tag=request.payload.get('teammate_handoff') if request and isinstance(request.payload,dict) else None
    if (not request or request.room_id!=room_id or request.actor_type!='agent' or request.actor_id!=row.sender_agent_id
            or request.kind!='message' or request.payload.get('trigger_event_id')!=root_id
            or request.payload.get('content')!=row.content or not isinstance(tag,dict)
            or tag.get('handoff_id')!=row.handoff_id or tag.get('phase')!='request'
            or (tag.get('sender_agent_id'),tag.get('recipient_agent_id'),tag.get('depth')) !=
               (row.sender_agent_id,row.recipient_agent_id,row.depth)):
        raise LLMFailure('page_teammate_origin_changed','The teammate request is unavailable.')
    parent_identity=None
    if row.parent_handoff_id:
        parent=db.get(TeammateHandoff,row.parent_handoff_id,populate_existing=True)
        if not parent or (parent.owner_id,parent.room_id,parent.root_request_event_id,parent.depth+1)!=(row.owner_id,room_id,root_id,row.depth):
            raise LLMFailure('page_teammate_origin_changed','The teammate parent task changed.')
        parent_identity=(parent.handoff_id,parent.owner_id,parent.room_id,parent.root_request_event_id,
            parent.parent_handoff_id,parent.sender_agent_id,parent.recipient_agent_id,parent.depth,parent.request_event_id)
    elif row.depth!=1:
        raise LLMFailure('page_teammate_origin_changed','The teammate task depth changed.')
    result=None
    if job['phase']=='return':
        result=db.get(RoomEvent,row.result_event_id,populate_existing=True) if row.result_event_id else None
        result_tag=result.payload.get('teammate_handoff') if result and isinstance(result.payload,dict) else None
        if (not result or result.room_id!=room_id or result.actor_type!='agent' or result.actor_id!=row.recipient_agent_id
                or result.kind not in ('message','failure') or result.payload.get('trigger_event_id')!=root_id
                or not isinstance(result_tag,dict) or result_tag.get('handoff_id')!=row.handoff_id or result_tag.get('phase')!='response'):
            raise LLMFailure('page_teammate_origin_changed','The teammate result is unavailable.')
    def payload_hash(event):
        try: return sha256(json.dumps(event.payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        except (TypeError,ValueError,UnicodeError):
            raise LLMFailure('page_teammate_origin_changed','The teammate task content is unavailable.') from None
    return TeammatePageOrigin(row.owner_id,room_id,root_id,agent_id,generation,row.handoff_id,job['phase'],
        row.sender_agent_id,row.recipient_agent_id,row.parent_handoff_id,parent_identity,row.depth,row.request_event_id,
        row.result_event_id,row.delivery_key,payload_hash(root),payload_hash(request),payload_hash(result) if result else None)


def require_teammate(db, origin, proof):
    if not isinstance(origin,TeammatePageOrigin) or not isinstance(proof,dict):
        raise LLMFailure('page_teammate_origin_unavailable','The teammate Page authority is unavailable.')
    job={'handoff_id':origin.handoff_id,'phase':origin.phase,'agent_id':origin.agent_id}
    if load_teammate_origin(db,job,origin.room_id,origin.root_id,origin.agent_id,origin.generation)!=origin:
        raise LLMFailure('page_teammate_origin_changed','The teammate Page task changed.')
    expected=(origin.owner_id,origin.room_id,origin.root_id,origin.room_id,origin.agent_id,origin.generation,'user_room',None,None)
    actual=tuple(proof.get(k) for k in ('owner_id','root_room_id','root_request_event_id','execution_room_id',
        'agent_id','run_generation','context_mode','job_id','phase'))
    if expected!=actual:
        raise LLMFailure('page_teammate_scope_changed','The teammate execution identity changed.')
    try:
        execution_scopes.require_binding(db,proof,for_start=True)
        delivery=db.get(ExecutionScope,execution_scopes.identity(origin.root_id,'teammate:'+origin.handoff_id),populate_existing=True)
        scope=delivery if origin.phase=='response' else db.get(ExecutionScope,delivery.parent_scope_id,populate_existing=True) if delivery else None
        if origin.parent_id:
            parent_scope=db.get(ExecutionScope,execution_scopes.identity(origin.root_id,'teammate:'+origin.parent_id),populate_existing=True)
            parent_identity=origin.parent_identity
            if not parent_scope or not parent_identity:
                raise ReviewChanged('Teammate parent scope is unavailable')
            parent_sender,parent_recipient=parent_identity[5],parent_identity[6]
            if origin.sender_id==parent_recipient and origin.sender_id!=parent_sender:
                expected_parent=parent_scope.scope_id
            elif origin.sender_id==parent_sender and origin.sender_id!=parent_recipient:
                expected_parent=parent_scope.parent_scope_id
            else:
                raise ReviewChanged('Teammate parent sender changed')
        else:
            expected_parent=execution_scopes.identity(origin.root_id,f'branch:{origin.room_id}:{origin.sender_id}')
        if (not delivery or delivery.parent_scope_id!=expected_parent or delivery.kind!='delivery' or delivery.job_kind!='teammate' or delivery.job_id!=origin.handoff_id
                or delivery.execution_room_id!=origin.room_id or delivery.agent_id!=origin.recipient_id
                or not scope or scope.scope_id!=proof.get('scope_id')):
            raise ReviewChanged('Teammate task scope changed')
    except ReviewChanged:
        raise LLMFailure('page_teammate_scope_changed','The teammate task is no longer active.') from None


def _direct_page_effects(db, root_id, room_id):
    # Generic scope counters predate Page persistence and do not include direct
    # Owner Page writes. Charge those committed effects separately at admission.
    events=db.query(RoomEvent).filter_by(room_id=room_id,kind='tool_result').filter(
        RoomEvent.payload['trigger_event_id'].as_string()==root_id).all()
    return sum(1 for event in events if event.actor_type=='tool' and isinstance(event.payload,dict)
        and event.payload.get('family')=='write_page' and event.payload.get('status')=='completed'
        and event.payload.get('delegated_origin') is None)


def teammate_eligible(db, origin, proof):
    try:
        require_teammate(db,origin,proof)
        root=db.get(ExecutionScope,proof['root_scope_id'],populate_existing=True)
        return bool(root and root.native_calls + _direct_page_effects(db,origin.root_id,origin.room_id) < root.max_native_calls)
    except LLMFailure:
        return False


def require_delegated(db, origin, proof):
    if not isinstance(origin, ExecutionOrigin) or origin.context_mode != 'delegated' or not isinstance(proof, dict):
        raise LLMFailure('page_delegated_origin_unavailable','A current delegated task is required to create a Page.')
    expected=(origin.owner_id,origin.root_room_id,origin.root_request_event_id,origin.execution_room_id,
              origin.agent_id,origin.generation,origin.job_id,origin.phase,'delegated')
    actual=tuple(proof.get(k) for k in ('owner_id','root_room_id','root_request_event_id','execution_room_id',
                                        'agent_id','run_generation','job_id','phase','context_mode'))
    if actual != expected:
        raise LLMFailure('page_delegated_origin_changed','The delegated Page task identity changed.')
    require_execution_origin_current(db,origin)
    try:
        execution_scopes.require_binding(db,proof,for_start=True)
    except ReviewChanged:
        raise LLMFailure('page_delegated_scope_changed','The delegated Page task is no longer active.') from None
    from app.services.bot_profile import is_personal_conversation
    if not is_personal_conversation(db,origin.owner_id,origin.agent_id,origin.execution_room_id):
        raise LLMFailure('page_delegated_room_unavailable','The delegated Bot private room is unavailable.')
    root=db.get(RoomEvent,origin.root_request_event_id,populate_existing=True)
    if not root or root.kind != 'message':
        raise LLMFailure('page_delegated_root_unavailable','The original Owner request is unavailable.')


def delegated_eligible(db, origin, proof):
    try:
        require_delegated(db,origin,proof)
        root=db.get(ExecutionScope,proof['root_scope_id'],populate_existing=True)
        return bool(root and root.native_calls < root.max_native_calls)
    except LLMFailure:
        return False


def apply(db, permit, call_id, args, *, expected_memory_digest, owner_epochs=None, execution_origin=None,
          model_use=None, round_number=None):
    if (not isinstance(permit,(bot_creation.CreationPermit,PagePermit,TeammatePagePermit)) or not isinstance(call_id,str)
            or not 1 <= len(call_id) <= 200 or not isinstance(args,dict) or set(args) != {'document'}):
        raise LLMFailure('page_invalid','The Page request is unavailable or invalid.')
    try:
        document, canonical, document_hash = normalize_document(args['document'])
    except PageDocumentError as error:
        raise PageValidationFailure(error) from None
    teammate=isinstance(permit,TeammatePagePermit)
    delegated=isinstance(permit,(PagePermit,TeammatePagePermit))
    identity=[permit.root_id,permit.agent_id,call_id]
    if delegated:
        identity += (['teammate',permit.origin.handoff_id,permit.origin.phase] if teammate else
                     [permit.origin.job_id,permit.origin.phase])
    authority_digest=(sha256(json.dumps([permit.origin.fingerprint,permit.execution_binding,
        permit.source_profile_fingerprint,permit.proposer.model_dump(mode='json')],sort_keys=True,
        separators=(',',':')).encode()).hexdigest() if delegated else None)
    command_id = sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    key = 'page-tool:'+command_id
    request_digest = sha256(canonical.encode('utf-8')).hexdigest()
    db.rollback(); db.execute(text('BEGIN IMMEDIATE'))
    try:
        if delegated:
            if teammate:
                if (execution_origin is not None or (permit.owner_id,permit.room_id,permit.root_id,permit.agent_id,permit.generation) !=
                        (permit.origin.owner_id,permit.origin.room_id,permit.origin.root_id,permit.origin.agent_id,permit.origin.generation)):
                    raise LLMFailure('page_teammate_origin_changed','The teammate Page task identity changed.')
                require_teammate(db,permit.origin,permit.execution_binding)
            else:
                if (execution_origin != permit.origin or (permit.owner_id,permit.room_id,permit.root_id,permit.agent_id,permit.generation) !=
                        (permit.origin.owner_id,permit.origin.execution_room_id,permit.origin.root_request_event_id,permit.origin.agent_id,permit.origin.generation)):
                    raise LLMFailure('page_delegated_origin_changed','The delegated Page task identity changed.')
                require_delegated(db,permit.origin,permit.execution_binding)
            if permit.proposer.owner_id != permit.owner_id:
                raise LLMFailure('page_forbidden','The request owner changed.')
            require_current(db,permit.owner_id,permit.agent_id,permit.source_profile_fingerprint)
            try:
                observe_authority(db,room_id=permit.room_id,agent_id=permit.agent_id,session_id='page-persistence',proposer=permit.proposer,owner_epochs=owner_epochs)
            except ReviewChanged:
                raise LLMFailure('page_changed','Owner or execution scope changed; no Page was saved.') from None
            if snapshot(db,permit.room_id,permit.agent_id,execution_origin=execution_origin)['digest'] != expected_memory_digest:
                raise LLMFailure('page_changed','Room context changed; no Page was saved.')
        prior=db.get(RoomResultCommand,(permit.owner_id,key))
        if prior:
            if (prior.room_id != permit.room_id or prior.request_digest != request_digest or prior.response.get('agent_id') != permit.agent_id
                    or delegated and prior.response.get('authority_digest') != authority_digest):
                raise LLMFailure('page_conflict','The Page call identity already has different content.')
            value=dict(prior.response,replayed=True)
            if delegated:
                agent=require_current(db,permit.owner_id,permit.agent_id,permit.source_profile_fingerprint)
                value['_binding']={'profile':fingerprint(agent),'memory':expected_memory_digest}
            db.commit(); return value
        if not delegated and not skill_persistence.eligible(db,permit.room_id,permit.root_id,permit.agent_id,permit.generation):
            raise LLMFailure('page_forbidden','The original Owner request is no longer active.')
        if permit.proposer.owner_id != permit.owner_id:
            raise LLMFailure('page_forbidden','The request owner changed.')
        agent=require_current(db,permit.owner_id,permit.agent_id,permit.source_profile_fingerprint)
        try:
            observe_authority(db,room_id=permit.room_id,agent_id=permit.agent_id,session_id='page-persistence',proposer=permit.proposer,owner_epochs=owner_epochs)
            if not delegated and permit.execution_binding is not None:
                proof=permit.execution_binding
                if (proof.get('context_mode'),proof.get('owner_id'),proof.get('root_room_id'),proof.get('root_request_event_id'),proof.get('execution_room_id'),proof.get('agent_id'),proof.get('run_generation')) != ('user_room',permit.owner_id,permit.room_id,permit.root_id,permit.room_id,permit.agent_id,permit.generation):
                    raise LLMFailure('page_forbidden','The request origin changed.')
                execution_scopes.require_binding(db,proof)
        except ReviewChanged:
            raise LLMFailure('page_changed','Owner or execution scope changed; no Page was saved.') from None
        memory=snapshot(db,permit.room_id,permit.agent_id,execution_origin=execution_origin)
        if memory['digest'] != expected_memory_digest:
            raise LLMFailure('page_changed','Room context changed; no Page was saved.')
        root_event=db.get(RoomEvent,permit.root_id)
        target=(root_event.payload.get('page_target') if not delegated and root_event and isinstance(root_event.payload,dict) else None)
        if delegated:
            root_scope=db.get(ExecutionScope,permit.execution_binding['root_scope_id'])
            prior_direct=_direct_page_effects(db,permit.root_id,permit.room_id) if teammate else 0
            charged=db.execute(update(ExecutionScope).where(ExecutionScope.scope_id==root_scope.scope_id,
                ExecutionScope.native_calls+prior_direct<ExecutionScope.max_native_calls).values(native_calls=ExecutionScope.native_calls+1)).rowcount
            if charged != 1:
                raise LLMFailure('tool_budget_exhausted','The original request shared action budget is exhausted.')
        now=datetime.now(timezone.utc)
        if target is None:
            result_id=str(uuid4()); version=1; base_version=None
            result=RoomResult(result_id=result_id,owner_id=permit.owner_id,room_id=permit.room_id,current_version=version,created_at=now,updated_at=now)
            db.add(result); db.flush()
        else:
            result, old=room_results.page_target(db,permit.owner_id,permit.room_id,target)
            if old.author_agent_id != permit.agent_id:
                raise LLMFailure('page_forbidden','Only the Page author can update this Page.')
            result_id=result.result_id; base_version=old.version; version=base_version+1
            changed=db.execute(update(RoomResult).where(RoomResult.result_id==result_id,RoomResult.owner_id==permit.owner_id,
                RoomResult.room_id==permit.room_id,RoomResult.current_version==base_version).values(current_version=version,updated_at=now)).rowcount
            if changed != 1: raise LLMFailure('page_version_changed','The Page changed; review its latest version.')
        data={'version':1,'room_id':permit.room_id,'result_id':result_id,'page_version':version,
              'base_version':base_version,'title':document['title'],'document_sha256':document_hash,
              'agent_id':permit.agent_id}
        lineage=({'root_room_id':permit.room_id,'root_request_event_id':permit.root_id,'job_kind':'teammate',
                  'job_id':permit.origin.handoff_id,'handoff_id':permit.origin.handoff_id,'phase':permit.origin.phase}
                 if teammate else {'root_room_id':permit.origin.root_room_id,'root_request_event_id':permit.root_id,
                  'job_id':permit.origin.job_id,'phase':permit.origin.phase} if delegated else None)
        payload={'family':'write_page','status':'completed','trigger_event_id':permit.root_id,'page_result':data}
        if lineage is not None: payload['delegated_origin']=lineage
        event=stage_event(db,permit.room_id,ActorRef('tool',None),'tool_result',payload,'page-event:'+command_id)
        from app.services.request_inspector import _model_use
        observed_model=_model_use({'model_use':model_use})
        if observed_model and (observed_model['connection_id'],observed_model['model_id']) != (getattr(permit.proposer,'connection_id',None),getattr(permit.proposer,'model_id',None)):
            observed_model=None
        db.add(RoomResultVersion(result_id=result_id,version=version,room_id=permit.room_id,kind='page',title=document['title'],
            source_event_id=event.event_id,source_seq=event.seq,selector='page:'+document_hash,
            author_agent_id=agent.id,author_name_snapshot=str(agent.name)[:200],
            execution={'trigger_event_id':permit.root_id,'round':round_number if type(round_number) is int and 1<=round_number<=4 else None,
                       'generation':permit.generation,'model_use':observed_model,**({'delegated_origin':lineage} if delegated else {})},
            content=canonical,file_ref=None,evidence=[],state='draft',state_revision=1,link_report='unchecked',created_at=now))
        db.flush()
        receipt={**data,'event_id':event.event_id,'seq':event.seq,'replayed':False}
        if delegated:
            receipt.update(delegated_origin=lineage,authority_digest=authority_digest,
                app_href=f'/chat/{permit.room_id}?result={result_id}&version={version}')
        db.add(RoomResultCommand(owner_id=permit.owner_id,idempotency_key=key,room_id=permit.room_id,request_digest=request_digest,response=receipt,created_at=now));db.flush()
        binding={'profile':fingerprint(agent),'memory':snapshot(db,permit.room_id,permit.agent_id,execution_origin=execution_origin)['digest']}
        db.commit();return {**receipt,'_binding':binding}
    except HTTPException:
        db.rollback();raise LLMFailure('page_changed','The Page changed or is unavailable; nothing was saved.') from None
    except BaseException:
        db.rollback();raise
