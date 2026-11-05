"""Unwired shared-private-memory storage with caller-owned commit and no replay.

For a write, the caller must start its SQLite transaction and enter `stage_change`.
It must commit *inside* the context, or roll back on every failure. The Owner lock
stays held until context exit. The service never commits or rolls back. The
canonical HTTP authority and local SQLite commit cannot be made atomic here.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import UUID, uuid4
from sqlalchemy import text
from app.models.collaboration import RoomAgentRun, RoomEvent, RoomTrigger
from app.models.shared_text_runtime import SharedTextRoom
from app.models.shared_private_memory import SharedPrivateMemoryState, SharedPrivateMemoryEntry, SharedPrivateMemoryCommand
from app.schemas.shared_private_memory import SharedPrivateMemoryChange
from app.services.shared_text_runtime import SharedTurn, require_read, verify
from app.services.llm_service import LLMFailure
from app.services.events import ActorRef, stage_event

MAX_ENTRIES=64
MAX_CONTENT_CHARS=8000
MAX_CONTENT_BYTES=32000


def _fail(code):
    raise LLMFailure('shared_private_memory_'+code,'Shared private memory could not be changed or confirmed.')


def _call_id(value):
    if not isinstance(value,str) or not 1<=len(value)<=200 or not value.isascii() or not value.isprintable():_fail('invalid')
    return value


def _uuid(value):
    try:
        if not isinstance(value,str) or str(UUID(value))!=value:raise ValueError()
    except (ValueError,TypeError):_fail('invalid')
    return value


def _live(db,turn):
    run=db.query(RoomAgentRun).filter_by(room_id=turn.room_id,agent_id=turn.agent_id).populate_existing().first()
    trigger=db.query(RoomTrigger).filter_by(event_id=turn.root_id,room_id=turn.room_id).populate_existing().first()
    root=db.get(RoomEvent,turn.root_id,populate_existing=True)
    if (not run or run.state!='running' or run.paused or run.generation!=turn.generation
            or run.trigger_event_id!=turn.root_id or not trigger or trigger.state!='running'
            or trigger.selected_agents!=[turn.agent_id] or not root or root.room_id!=turn.room_id
            or root.actor_type!='user' or root.actor_id!=turn.owner_id or root.kind!='message'):
        _fail('scope_changed')
    return root


def _snapshot(db,room_id):
    state=db.get(SharedPrivateMemoryState,room_id,populate_existing=True)
    rows=db.query(SharedPrivateMemoryEntry).filter_by(room_id=room_id,deleted_at=None).order_by(SharedPrivateMemoryEntry.memory_id).all()
    entries=[{'memory_id':row.memory_id,'category':row.category,'content':row.content,'revision':row.revision} for row in rows]
    if (len(entries)>MAX_ENTRIES or sum(len(item['content']) for item in entries)>MAX_CONTENT_CHARS
            or sum(len(item['content'].encode('utf-8')) for item in entries)>MAX_CONTENT_BYTES):_fail('budget')
    result={'version':1,'room_id':room_id,'revision':state.revision if state else 0,
            'context_after_seq':state.context_after_seq if state else 0,'entries':entries}
    digest=sha256(json.dumps(result,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
    return {**result,'digest':digest}


def current_snapshot(db,app_state,turn):
    """Return only this live turn's private entries after fresh authority checks."""
    if not isinstance(turn,SharedTurn):_fail('invalid')
    with app_state.owner_recovery_lock:
        verify(app_state,db,turn.room_id,turn.agent_id,turn.root_id,turn.generation,turn)
        _live(db,turn)
        return _snapshot(db,turn.room_id)


@contextmanager
def stage_change(db,app_state,turn,call_id,change,expected_snapshot_digest):
    """Stage exactly one effect and receipt; caller commits inside this context.

    An existing call never re-executes; use `read_receipt` for readback. A missing
    receipt does not prove an in-flight transaction will not subsequently commit.
    """
    if not isinstance(turn,SharedTurn) or not isinstance(change,SharedPrivateMemoryChange):_fail('invalid')
    _call_id(call_id)
    if not isinstance(expected_snapshot_digest,str) or len(expected_snapshot_digest)!=64:_fail('invalid')
    if not db.in_transaction():_fail('transaction_required')
    with app_state.owner_recovery_lock:
        # Acquire SQLite's writer reservation before checking local run/receipt state.
        changed=db.execute(text('UPDATE shared_text_rooms SET room_id=room_id WHERE room_id=:room_id'),{'room_id':turn.room_id}).rowcount
        if changed!=1:_fail('scope_changed')
        verify(app_state,db,turn.room_id,turn.agent_id,turn.root_id,turn.generation,turn)
        root=_live(db,turn)
        request={'version':1,'owner_id':turn.owner_id,'issuer':turn.issuer,'subject':turn.subject,
                 'team_id':turn.team_id,'publication_id':turn.publication_id,'room_id':turn.room_id,
                 'agent_id':turn.agent_id,'root_id':turn.root_id,'generation':turn.generation,
                 'owner_epoch':turn.owner_epoch,'session_generation':turn.session_generation,
                 'authority_revision':turn.authority_revision,'publication_revision':turn.revision,
                 'revocation_epoch':turn.revocation_epoch,'call_id':call_id,'change':change.model_dump(mode='json')}
        digest=sha256(json.dumps(request,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
        prior=db.get(SharedPrivateMemoryCommand,(turn.room_id,turn.root_id,turn.agent_id,call_id))
        if prior is not None:
            _fail('already_committed' if prior.request_digest==digest else 'call_conflict')
        before=_snapshot(db,turn.room_id)
        if before['digest']!=expected_snapshot_digest or before['revision']!=change.expected_scope_revision:_fail('changed')
        state=db.get(SharedPrivateMemoryState,turn.room_id)
        if state is None:
            state=SharedPrivateMemoryState(room_id=turn.room_id,revision=0,context_after_seq=0);db.add(state)
        now=datetime.now(timezone.utc)
        if change.operation=='save':
            row=SharedPrivateMemoryEntry(memory_id=str(uuid4()),room_id=turn.room_id,category=change.category,
                content=change.content.strip(),revision=1,source_event_id=root.event_id,source_seq=root.seq,
                created_at=now,updated_at=now,deleted_at=None)
            db.add(row)
        else:
            row=db.get(SharedPrivateMemoryEntry,change.memory_id)
            if not row or row.room_id!=turn.room_id or row.deleted_at is not None or row.revision!=change.expected_revision:_fail('changed')
            row.revision+=1;row.updated_at=now;row.source_event_id=root.event_id;row.source_seq=root.seq
            if change.operation=='edit':row.category=change.category;row.content=change.content.strip()
            else:row.content='';row.deleted_at=now
            state.context_after_seq=max(state.context_after_seq,root.seq)
        state.revision+=1
        db.flush()
        after=_snapshot(db,turn.room_id)
        response={'version':1,'state':'saved','operation':change.operation,'memory_id':row.memory_id,
                  'scope_revision':state.revision,'entry_revision':row.revision,'context_after_seq':state.context_after_seq,
                  'root_event_id':turn.root_id,'room_id':turn.room_id,'agent_id':turn.agent_id,'call_id':call_id}
        receipt_key=sha256(json.dumps([turn.root_id,turn.agent_id,call_id],separators=(',',':')).encode()).hexdigest()
        stage_event(db,turn.room_id,ActorRef('system',None),'activity',
            {'category':'shared_private_memory','status':'saved','trigger_event_id':turn.root_id,
             'shared_private_memory':response},'shared-private-memory:'+receipt_key)
        db.add(SharedPrivateMemoryCommand(room_id=turn.room_id,root_event_id=turn.root_id,agent_id=turn.agent_id,
            call_id=call_id,request_digest=digest,response=json.dumps(response,sort_keys=True,separators=(',',':')),created_at=now))
        db.flush()
        # Reconfirm immediately before handing the staged transaction to its caller.
        verify(app_state,db,turn.room_id,turn.agent_id,turn.root_id,turn.generation,turn)
        _live(db,turn)
        # This is staged, not a claim of commit. The caller commits before leaving.
        yield {'receipt':response,'snapshot':after}


def read_receipt(db,owner_id,room_id,root_event_id,agent_id,call_id):
    """Read an exact receipt after re-login as the same currently-authorized account."""
    _uuid(root_event_id);_call_id(call_id)
    binding=db.query(SharedTextRoom).filter_by(room_id=room_id,owner_id=owner_id,agent_id=agent_id).first()
    if not binding:_fail('unavailable')
    require_read(db,binding,owner_id)
    root=db.get(RoomEvent,root_event_id)
    if not root or root.room_id!=room_id or root.actor_type!='user' or root.actor_id!=owner_id:_fail('unavailable')
    command=db.get(SharedPrivateMemoryCommand,(room_id,root_event_id,agent_id,call_id))
    return json.loads(command.response) if command else None


def read_snapshot(db,owner_id,room_id,session_generation):
    binding=db.query(SharedTextRoom).filter_by(room_id=room_id,owner_id=owner_id).first()
    if not binding:_fail('unavailable')
    state=db.info.get('app_state')
    if state is None:_fail('unavailable')
    with state.owner_recovery_lock:
        adapter=getattr(state,'team_connection',None)
        if not adapter or adapter.generation!=session_generation:_fail('session_changed')
        require_read(db,binding,owner_id)
        snap=_snapshot(db,room_id)
        if adapter.generation!=session_generation or getattr(state,'team_connection',None) is not adapter:_fail('session_changed')
        return {'version':1,'room_id':room_id,'publication_id':binding.publication_id,'team_id':binding.team_id,
                'account_id':binding.subject,'issuer':binding.issuer,'session_generation':session_generation,
                'scope_revision':snap['revision'],'entries':snap['entries'],'truncated':False}


def change_for_call(snapshot,family,arguments):
    """Resolve model-facing IDs and server-owned revisions against the frozen scope."""
    if not isinstance(snapshot,dict) or not isinstance(arguments,dict):_fail('invalid')
    operation={'save_bot_memory':'save','edit_bot_memory':'edit','forget_bot_memory':'forget'}.get(family)
    if operation is None:_fail('invalid')
    values={'operation':operation,'expected_scope_revision':snapshot['revision']}
    if operation=='save':
        values.update(content=arguments.get('content'),category=arguments.get('category','fact'))
    else:
        memory_id=arguments.get('memory_id')
        matches=[row for row in snapshot['entries'] if row['memory_id']==memory_id]
        if len(matches)!=1:_fail('changed')
        row=matches[0];values.update(memory_id=memory_id,expected_revision=row['revision'])
        if operation=='edit':values.update(content=arguments.get('content'),category=arguments.get('category',row['category']))
    try:return SharedPrivateMemoryChange.model_validate(values)
    except Exception:_fail('invalid')
