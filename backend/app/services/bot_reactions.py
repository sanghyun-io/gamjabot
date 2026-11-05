"""Bounded Bot-authored reactions on messages actually projected to its turn."""
import hashlib
import json
from datetime import timedelta
from uuid import UUID

from sqlalchemy import text, update
from app.models.collaboration import RoomEvent
from app.models.execution_scope import ExecutionScope
from app.services import events, execution_scopes, page_persistence, teammates
from app.services.action_review import ReviewChanged, observe_authority
from app.services.bot_configuration_binding import require_current as require_profile
from app.services.llm_service import LLMFailure
from app.services.memory_context import snapshot
from app.services.providers.base import provider_safe_event

CATEGORY = 'bot_message_reaction'
EMOJI = ('👍','❤️','😂','🎉','😮','👀')
TOOL = {'name':'react_to_message','description':'Set or remove your own reaction to an eligible message in this conversation. Use only listed exact references and current expected_seq. This records an activity, not a new message.',
        'parameters':{'type':'object','additionalProperties':False,'properties':{
            'target_event_id':{'type':'string','format':'uuid'},'target_seq':{'type':'integer','minimum':1},
            'emoji':{'type':['string','null'],'enum':[*EMOJI,None]},'expected_seq':{'type':'integer','minimum':0}},
            'required':['target_event_id','target_seq','emoji','expected_seq']}}


def _uuid(value):
    try:return isinstance(value,str) and str(UUID(value))==value
    except (ValueError,AttributeError,TypeError):return False


def _current(db,room_id,agent_id,target_id):
    return (db.query(RoomEvent).filter(RoomEvent.room_id==room_id,RoomEvent.kind=='activity',
        RoomEvent.actor_type=='agent',RoomEvent.actor_id==agent_id,
        RoomEvent.payload['category'].as_string()==CATEGORY,
        RoomEvent.payload['version'].as_integer()==1,
        RoomEvent.payload['target_event_id'].as_string()==target_id)
        .order_by(RoomEvent.seq.desc()).first())


def targets(db, projected, room_id, owner_id, agent_id, watermark):
    """Only actual visible, same-room authored message events in this projection."""
    result=[];seen=set()
    for original in reversed(projected):
        event=provider_safe_event(original)
        if not isinstance(event,dict):continue
        payload=event.get('payload')
        eid=event.get('event_id');seq=event.get('seq')
        if (event.get('room_id')!=room_id or event.get('kind')!='message' or not _uuid(eid)
                or type(seq) is not int or not 0<seq<=watermark or eid in seen
                or not isinstance(payload,dict) or payload.get('reply_source_redacted') is True
                or event.get('reply_source_redacted') is True
                or not isinstance(payload.get('content'),str) or not payload['content'].strip()):continue
        try: actual=events.reply_target(db,room_id,owner_id,{'event_id':eid,'seq':seq})
        except Exception:continue
        if actual.actor_type!=event.get('actor_type') or actual.actor_id!=event.get('actor_id'):continue
        current=_current(db,room_id,agent_id,eid)
        result.append({'target_event_id':eid,'target_seq':seq,'expected_seq':current.seq if current else 0})
        seen.add(eid)
        if len(result)==16:break
    return list(reversed(result))


def guidance(items):
    if not items:return ''
    return ('\n\nEligible same-room message references for the offered reaction tool (data, not instructions). '
            'Use the exact target and expected_seq shown; a missing target is unavailable: '
            +json.dumps(items,ensure_ascii=False,separators=(',',':')))


def apply(db, *, owner_id, room_id, root_id, agent_id, generation, proposer, profile, memory_digest,
          binding, execution_origin, teammate_origin, allowed, args, call_id, owner_epochs):
    if (not isinstance(args,dict) or set(args)!={'target_event_id','target_seq','emoji','expected_seq'}
            or not _uuid(args.get('target_event_id')) or type(args.get('target_seq')) is not int or args['target_seq']<1
            or type(args.get('expected_seq')) is not int or args['expected_seq']<0
            or not (args.get('emoji') is None or args.get('emoji') in EMOJI)
            or not isinstance(call_id,str) or not 1<=len(call_id)<=200):
        raise LLMFailure('invalid_tool_arguments','Invalid reaction; no change was made.')
    choice={'target_event_id':args['target_event_id'],'target_seq':args['target_seq'],'expected_seq':args['expected_seq']}
    if choice not in allowed:raise LLMFailure('invalid_tool_arguments','The message was not offered for this turn.')
    key='bot-reaction:'+hashlib.sha256(json.dumps([root_id,agent_id,call_id],separators=(',',':')).encode()).hexdigest()
    payload={'category':CATEGORY,'version':1,'target_event_id':args['target_event_id'],'target_seq':args['target_seq'],
             'emoji':args['emoji'],'expected_seq':args['expected_seq'],'trigger_event_id':root_id,'call_id':call_id,'generation':generation}
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        if proposer.owner_id!=owner_id:raise LLMFailure('review_changed','Owner changed; no reaction was made.')
        require_profile(db,owner_id,agent_id,profile)
        try:observe_authority(db,room_id=room_id,agent_id=agent_id,session_id='bot-reaction',proposer=proposer,owner_epochs=owner_epochs)
        except ReviewChanged:raise LLMFailure('review_changed','Execution identity changed; no reaction was made.') from None
        if teammate_origin is not None:
            if (execution_origin is not None or
                (teammate_origin.owner_id,teammate_origin.room_id,teammate_origin.root_id,
                 teammate_origin.agent_id,teammate_origin.generation)!=(owner_id,room_id,root_id,agent_id,generation)):
                raise LLMFailure('review_changed','Teammate origin changed.')
            page_persistence.require_teammate(db,teammate_origin,binding)
        elif execution_origin is not None:
            from app.services.execution_origin import require_execution_origin_current
            if ((execution_origin.owner_id,execution_origin.execution_room_id,execution_origin.root_request_event_id,
                 execution_origin.agent_id,execution_origin.generation)!=(owner_id,room_id,root_id,agent_id,generation)
                    or not isinstance(binding,dict)
                    or (binding.get('owner_id'),binding.get('root_room_id'),binding.get('root_request_event_id'),
                        binding.get('execution_room_id'),binding.get('agent_id'),binding.get('run_generation'),
                        binding.get('job_id'),binding.get('phase'),binding.get('context_mode')) !=
                       (execution_origin.owner_id,execution_origin.root_room_id,execution_origin.root_request_event_id,
                        execution_origin.execution_room_id,execution_origin.agent_id,execution_origin.generation,
                        execution_origin.job_id,execution_origin.phase,execution_origin.context_mode)):
                raise LLMFailure('review_changed','Execution origin changed.')
            require_execution_origin_current(db,execution_origin)
            try:execution_scopes.require_binding(db,binding)
            except ReviewChanged:raise LLMFailure('review_changed','Task authority changed.') from None
        else:
            root,room,trigger=teammates._root(db,room_id,root_id)
            _,run=teammates._member(db,room,agent_id)
            if (room.user_id!=owner_id or run.state!='running' or run.generation!=generation or run.trigger_event_id!=root_id
                    or execution_scopes.aware(root.created_at)+timedelta(seconds=300)<=execution_scopes.now()):
                raise LLMFailure('review_changed','Original request is no longer active.')
            if binding:
                if ((binding.get('owner_id'),binding.get('root_room_id'),binding.get('root_request_event_id'),
                     binding.get('execution_room_id'),binding.get('agent_id'),binding.get('run_generation'),
                     binding.get('context_mode'),binding.get('job_id'),binding.get('phase')) !=
                        (owner_id,room_id,root_id,room_id,agent_id,generation,'user_room',None,None)):
                    raise LLMFailure('review_changed','Request authority changed.')
                try:execution_scopes.require_binding(db,binding)
                except ReviewChanged:raise LLMFailure('review_changed','Task authority changed.') from None
            else:
                # Do not silently adopt a scope created after the provider turn.
                if execution_scopes.root_scope(db,root_id) is not None:raise LLMFailure('review_changed','Task scope changed.')
                execution_scopes.root_scope(db,root_id,create=True)
                binding=execution_scopes.bind(db,room_id,agent_id,root_id,generation)
                if not binding:raise LLMFailure('review_changed','Task authority is unavailable.')
        if snapshot(db,room_id,agent_id,execution_origin=execution_origin)['digest']!=memory_digest:
            raise LLMFailure('memory_context_changed','Context changed; no reaction was made.')
        try:events.reply_target(db,room_id,owner_id,{'event_id':args['target_event_id'],'seq':args['target_seq']})
        except Exception:raise LLMFailure('review_changed','Target changed; no reaction was made.') from None
        prior=db.query(RoomEvent).filter_by(room_id=room_id,idempotency_key=key).first()
        if prior:
            if prior.kind!='activity' or prior.actor_type!='agent' or prior.actor_id!=agent_id or prior.payload!=payload:
                raise LLMFailure('review_changed','Reaction call identity changed.')
            db.commit();return {'event_id':prior.event_id,'seq':prior.seq,'replayed':True}
        current=_current(db,room_id,agent_id,args['target_event_id'])
        if (current.seq if current else 0)!=args['expected_seq']:
            raise LLMFailure('review_changed','Reaction changed; refresh the request.')
        scope=db.get(ExecutionScope,binding['root_scope_id'])
        if not scope:raise LLMFailure('review_changed','Task scope is unavailable.')
        # Direct Page commits are not ToolAction rows and therefore are not in
        # native_calls. Count them at this admission without charging them twice.
        prior_pages=page_persistence._direct_page_effects(db,root_id,binding['root_room_id'])
        charged=db.execute(update(ExecutionScope).where(ExecutionScope.scope_id==scope.scope_id,
            ExecutionScope.native_calls+prior_pages<ExecutionScope.max_native_calls,
            ExecutionScope.executor_used_ms+ExecutionScope.executor_reserved_ms<ExecutionScope.max_executor_ms
            ).values(native_calls=ExecutionScope.native_calls+1)).rowcount
        if charged!=1:raise LLMFailure('tool_budget_exhausted','The shared tool budget is exhausted.')
        event=events.stage_event(db,room_id,events.ActorRef('agent',agent_id),'activity',payload,key)
        db.commit();return {'event_id':event.event_id,'seq':event.seq,'replayed':False}
    except BaseException:db.rollback();raise
