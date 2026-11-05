"""Owner-local cross-room messages. Routing and authority always come from the DB.

Recipients execute only the explicitly sent task; their personal conversation
history and memory are handled by a separate trusted delegated-context route.
"""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from uuid import uuid4

from sqlalchemy import func, or_, text
from fastapi import HTTPException
from app.models.bot_message import BotMessage, BotMessageRetry
from app.models.collaboration import RoomAgentRun, RoomEvent, RoomMembership, RoomTrigger, ToolAction
from app.models.models import Agent, Conversation, Project
from app.models.teammate import TeammateBudget, TeammateHandoff
from app.schemas.bot_profile import BotConversationResolve
from app.services import bot_profile, teammates
from app.services.events import ActorRef, stage_event
from app.services.llm_service import LLMFailure

MAX_WAIT_SECONDS = 300
COLLABORATION_NAMES = frozenset({"list_bots", "message_bot", "message_bots"})
TOOL_SCHEMAS = [
    {'name':'message_bots','description':'Atomically delegate distinct tasks to 2–8 different Bots. They run with bounded parallelism. Finish your contribution after this receipt; you resume once only after every branch and its nested work settles, with exact actual replies or explicit missing/failure states. A receipt is not an answer and grants no permissions.',
     'parameters':{'type':'object','properties':{'tasks':{'type':'array','minItems':2,'maxItems':8,
        'items':{'type':'object','properties':{'agent_id':{'type':'integer','minimum':1},'content':{'type':'string','minLength':1,'maxLength':4000}},
            'required':['agent_id','content'],'additionalProperties':False}}},'required':['tasks'],'additionalProperties':False}},
    {"name": "list_bots", "description": "List your Owner's active Bots and their roles. If next_offset is present, call again with that offset for more; the roster can change between pages. These Bots can receive a bounded text task in their own personal conversation. Private conversations and memories are not shared.",
     "parameters": {"type": "object", "properties": {"offset": {"type": "integer", "minimum": 0}}, "additionalProperties": False}},
    {"name": "message_bot", "description": "Send a text task to another Bot, even outside this conversation. Its own model runs in a private delegated context; after the real reply you resume once. An acceptance receipt is not its answer. Finish your current contribution after receiving the receipt. Delegated local work is available only when the server offers verified scoped tools; each action still needs its own fresh Owner approval. A receipt grants no permissions.",
     "parameters": {"type": "object", "properties": {
         "agent_id": {"type": "integer", "minimum": 1},
         "content": {"type": "string", "minLength": 1, "maxLength": 4000}},
         "required": ["agent_id", "content"], "additionalProperties": False}},
]


def now():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _fail(code="bot_message_context_changed", message="Bot message execution is no longer available."):
    raise LLMFailure(code, message)


def _root(db, root_room_id, root_id):
    return teammates._root(db, root_room_id, root_id)


def _room(db, room_id, owner_id):
    room = db.get(Conversation, room_id)
    project = db.get(Project, room.project_id) if room else None
    if not room or not room.is_active or room.user_id != owner_id or not project or not project.is_active or project.user_id != owner_id:
        _fail()
    return room


def roster(db, owner_id):
    return [{"agent_id": a.id, "name": a.name, "description": a.description,
             "model_id": a.model_id, "avatar": a.avatar} for a in db.query(Agent).filter_by(
                 user_id=owner_id, is_active=True).order_by(Agent.id).all()]


def event_ref(db, event_id, *, room_id=None):
    event = db.get(RoomEvent, event_id) if event_id else None
    if event is None or room_id is not None and event.room_id != room_id:
        return None
    return {"room_id": event.room_id, "event_id": event.event_id, "seq": event.seq,
            "actor_type": event.actor_type, "actor_id": event.actor_id, "kind": event.kind}


def metadata(row, phase):
    return {"version": 1, "message_id": row.message_id, "phase": phase,
            "sender_agent_id": row.sender_agent_id, "recipient_agent_id": row.recipient_agent_id,
            "root_room_id": row.root_room_id, "root_request_event_id": row.root_request_event_id}


def receipt(db, row):
    sender = db.get(Conversation, row.sender_room_id)
    recipient = db.get(Conversation, row.recipient_room_id)
    result = effective_result(db, row) if (row.state == "completed" and row.reply_state == "delivered"
                                            and _resolution_ready(db, row)) else None
    if result and result.kind != "message":
        result = None
    return {name: getattr(row, name) for name in ("message_id", "root_request_event_id", "parent_job_id", "parent_kind",
        "depth", "sender_agent_id", "sender_name", "recipient_agent_id", "recipient_name", "content", "state", "reply_state", "failure_code")} | {
        "version": 1, "cross_room": True,
        "capabilities": {"text_collaboration": True, "delegated_os_execution": False},
        "source_room": {"room_id": row.sender_room_id, "title": sender.title if sender else ""},
        "target_room": {"room_id": row.recipient_room_id, "title": recipient.title if recipient else ""},
        "root_ref": event_ref(db, row.root_request_event_id, room_id=row.root_room_id),
        "request_ref": event_ref(db, row.request_event_id, room_id=row.sender_room_id),
        "delivery_ref": event_ref(db, row.delivery_event_id, room_id=row.recipient_room_id),
        "result_ref": event_ref(db, row.result_event_id, room_id=row.recipient_room_id),
        "resolved_result_ref": event_ref(db, result.event_id, room_id=row.recipient_room_id) if result else None,
        "return_delivery_ref": event_ref(db, row.return_delivery_event_id, room_id=row.sender_room_id),
        "return_ref": event_ref(db, row.return_event_id, room_id=row.sender_room_id),
        "created_at": aware(row.created_at).isoformat(), "updated_at": aware(row.updated_at).isoformat(),
        "deadline_at": aware(row.deadline_at).isoformat()}


def list_room(db, room_id, owner_id):
    from app.services.room_service import require_room
    require_room(db, room_id, owner_id, active=False)
    rows = db.query(BotMessage).filter(BotMessage.owner_id == owner_id, BotMessage.batch_id.is_(None), or_(BotMessage.root_room_id == room_id,
        BotMessage.sender_room_id == room_id, BotMessage.recipient_room_id == room_id)).order_by(
            BotMessage.created_at.desc(), BotMessage.message_id).limit(101).all()
    return {"version": 1, "room_id": room_id, "bots": roster(db, owner_id),
            "messages": [receipt(db, row) for row in rows[:100]], "messages_truncated": len(rows) > 100,
            "capabilities": {"text_collaboration": True, "delegated_os_execution": False}}


def root_rows(db, root_id):
    return db.query(BotMessage).filter_by(root_request_event_id=root_id).order_by(BotMessage.created_at, BotMessage.message_id).all()


def _parent(db, parent_job_id, parent_kind, root_id):
    if parent_job_id is None:
        return None
    cls = BotMessage if parent_kind == "bot_message" else TeammateHandoff if parent_kind == "teammate" else None
    row = db.get(cls, parent_job_id) if cls else None
    if row is None or row.root_request_event_id != root_id:
        _fail()
    return row


def _send_guard(db, sender_room_id, root_room_id, root_id, sender_id, generation, retry_binding=None):
    root, root_room, trigger = _root(db, root_room_id, root_id)
    if isinstance(root.payload, dict) and 'teammate_retry_context' in root.payload:
        if not retry_binding:
            _fail('bot_message_context_changed', 'The retry authority is unavailable.')
        teammates.require_retry_binding(db, retry_binding, owner_id=root_room.user_id, root_room_id=root_room_id,
            root_id=root_id, room_id=sender_room_id, agent_id=sender_id, generation=generation)
    sender_room = _room(db, sender_room_id, root_room.user_id)
    sender, run = teammates._member(db, sender_room, sender_id)
    if run.generation != generation or run.state != "running" or run.trigger_event_id != root_id:
        _fail()
    return root, root_room, sender_room, sender


def send(db, sender_room_id, root_room_id, root_id, sender_id, generation, call_id, arguments,
         *, parent_job_id=None, parent_kind=None, provider_calls=0, expected_memory_digest=None, execution_origin=None, retry_binding=None):
    target_id, content = arguments.get("agent_id"), arguments.get("content")
    if (set(arguments) != {"agent_id", "content"} or type(target_id) is not int or target_id <= 0 or target_id == sender_id
            or not isinstance(content, str) or not content.strip() or len(content) > 4000):
        _fail("bot_message_invalid_request", "Choose another active Bot and a text task of at most 4000 characters.")
    root, root_room, sender_room, sender = _send_guard(db, sender_room_id, root_room_id, root_id, sender_id, generation, retry_binding)
    target = bot_profile.owned_agent(db, root_room.user_id, target_id)
    owner_id = root_room.user_id
    # Canonical-room creation is idempotent and dispatch-free. Revalidate the
    # original root in the following queue transaction before accepting work.
    target_room = bot_profile.canonical_conversation(db, owner_id, target_id, active_only=True)
    if target_room is None:
        result = bot_profile.resolve_conversation(db, owner_id, target_id, BotConversationResolve(), retry_binding=retry_binding)
        target_room_id = result["room_id"]
    else:
        target_room_id = target_room.id
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        from app.services.memory_context import require_locked
        require_locked(db,sender_room_id,sender_id,expected_memory_digest,execution_origin=execution_origin)
        root, root_room, sender_room, sender = _send_guard(db, sender_room_id, root_room_id, root_id, sender_id, generation, retry_binding)
        if not bot_profile.is_personal_conversation(db, owner_id, target_id, target_room_id):
            _fail()
        target = bot_profile.owned_agent(db, owner_id, target_id)
        key = sha256(json.dumps([root_id, sender_room_id, sender_id, generation, call_id], separators=(",", ":")).encode()).hexdigest()
        prior = db.query(BotMessage).filter_by(root_request_event_id=root_id, delivery_key=key).first()
        if prior:
            if prior.content != content or prior.recipient_agent_id != target_id:
                _fail("bot_message_delivery_changed", "A delivery identity cannot carry different work.")
            value = receipt(db, prior)
            db.commit()
            return value
        parent = _parent(db, parent_job_id, parent_kind, root_id)
        depth = parent.depth + 1 if parent else 1
        existing = root_rows(db, root_id)
        same_room_count = db.query(func.count(TeammateHandoff.handoff_id)).filter_by(root_request_event_id=root_id).scalar()
        if depth > teammates.MAX_DEPTH or len(existing) + same_room_count >= teammates.MAX_HANDOFFS or provider_calls >= teammates.MAX_PROVIDER_CALLS:
            _fail("teammate_budget_exhausted", "This request reached its shared Bot collaboration limit.")
        if db.get(TeammateBudget, root_id) is None:
            db.add(TeammateBudget(root_request_event_id=root_id, provider_calls=provider_calls))
        at = now()
        row = BotMessage(message_id=str(uuid4()), owner_id=owner_id, root_room_id=root_room_id,
            root_request_event_id=root_id, sender_room_id=sender_room_id, recipient_room_id=target_room_id,
            sender_agent_id=sender_id, recipient_agent_id=target_id, sender_name=sender.name, recipient_name=target.name,
            content=content, delivery_key=key, parent_job_id=parent_job_id, parent_kind=parent_kind,
            depth=depth, state="pending", reply_state="none", created_at=at, updated_at=at,
            deadline_at=min([aware(r.deadline_at) for r in existing], default=at + timedelta(seconds=MAX_WAIT_SECONDS)))
        request = stage_event(db, sender_room_id, ActorRef("agent", sender_id), "message",
            {"content": content, "trigger_event_id": root_id, "bot_message": metadata(row, "request")}, "bot-message-request:" + row.message_id)
        row.request_event_id = request.event_id
        delivery = stage_event(db, target_room_id, ActorRef("system", None), "activity",
            {"category": "bot_message", "status": "pending", "content": content,
             "trigger_event_id": root_id, "bot_message": metadata(row, "delivery"),
             "source_ref": event_ref(db, request.event_id)}, "bot-message-delivery:" + row.message_id)
        row.delivery_event_id = delivery.event_id
        db.add(row)
        from app.services.execution_scopes import job_scope
        job_scope(db, row)
        db.commit()
        return receipt(db, row)
    except BaseException:
        db.rollback()
        raise


def resolve_execution_origin(db, job_id, phase, execution_room_id, agent_id, generation=None):
    """Only this DB resolver may authorize a cross-room execution context."""
    row = db.get(BotMessage, job_id, populate_existing=True)
    from app.models.bot_batch import BotBatch
    batch = db.get(BotBatch, job_id)
    if row is not None and batch is not None:
        _fail('bot_batch_context_changed')
    if batch is not None:
        from app.services.bot_batches import resolve_execution_origin as resolve_batch
        return resolve_batch(db,job_id,phase,execution_room_id,agent_id,generation)
    if row is None or phase not in {"response", "return"}:
        _fail()
    root, root_room, trigger = _root(db, row.root_room_id, row.root_request_event_id)
    if row.owner_id != root_room.user_id:
        _fail()
    expected_room = row.recipient_room_id if phase == "response" else row.sender_room_id
    expected_agent = row.recipient_agent_id if phase == "response" else row.sender_agent_id
    if execution_room_id != expected_room or agent_id != expected_agent:
        _fail()
    sender_room = _room(db, row.sender_room_id, row.owner_id)
    teammates._member(db, sender_room, row.sender_agent_id)
    if phase == 'response':
        target_room = _room(db, row.recipient_room_id, row.owner_id)
        if not bot_profile.is_personal_conversation(db, row.owner_id, row.recipient_agent_id, target_room.id):
            _fail()
    else:
        # A stopped recipient cannot invalidate a reply already received by
        # its parent. Historical result ownership remains exact and checked.
        target_room = db.get(Conversation, row.recipient_room_id)
        if not target_room or target_room.user_id != row.owner_id:
            _fail()
    _, run = teammates._member(db, target_room if phase == "response" else sender_room, agent_id)
    from app.services.execution_scopes import job_scope, lineage
    delivery = job_scope(db, row)
    current_scope = delivery if phase == 'response' else db.get(type(delivery), delivery.parent_scope_id)
    if (any(s.state != 'active' or aware(s.deadline_at) <= now() for s in lineage(db, current_scope))
            or (row.state if phase == "response" else row.reply_state) != "running"
            or generation is not None and (run.generation != generation or run.state != "running"
                or run.trigger_event_id != row.root_request_event_id)):
        _fail()
    parent = _parent(db, row.parent_job_id, row.parent_kind, row.root_request_event_id)
    delegated = phase == "response" or row.parent_kind == "bot_message"
    source_task = None
    task_content = row.content
    if phase == "return" and parent is not None:
        task_content = parent.content
        source_task = event_ref(db, parent.request_event_id)
    result = effective_result(db,row) if phase=='return' else db.get(RoomEvent,row.result_event_id) if row.result_event_id else None
    if result and (result.room_id != row.recipient_room_id or result.actor_id != row.recipient_agent_id):
        _fail()
    peer_text = str(result.payload.get("content") or result.payload.get("message") or result.payload.get("status") or "") if phase == "return" and result else ""
    return {"owner_id": row.owner_id, "root_room_id": row.root_room_id,
        "root_request_event_id": row.root_request_event_id, "execution_room_id": execution_room_id,
        "agent_id": agent_id, "job_id": job_id, "phase": phase, "generation": generation,
        "context_mode": "delegated" if delegated else "user_room", "task_content": task_content,
        "task_ref": event_ref(db, row.request_event_id, room_id=row.sender_room_id),
        "source_task_ref": source_task, "root_ref": event_ref(db, row.root_request_event_id, room_id=row.root_room_id),
        "peer_result_ref": event_ref(db, result.event_id, room_id=row.recipient_room_id) if phase == "return" and result else None,
        "peer_result_content": peer_text[:4000] if phase == "return" and result else None,
        "peer_result_original_chars": len(peer_text), "peer_result_included_chars": min(len(peer_text), 4000),
        "peer_result_omitted_chars": max(0, len(peer_text) - 4000),
        "failure_code": row.failure_code}


def _return_delivery(db, row):
    if row.return_delivery_event_id is None:
        event = stage_event(db, row.sender_room_id, ActorRef("system", None), "activity",
            {"category": "bot_message", "status": row.state, "trigger_event_id": row.root_request_event_id,
             "bot_message": metadata(row, "return_delivery"), "result_ref": event_ref(db, row.result_event_id),
             "failure_code": row.failure_code}, "bot-message-return-delivery:" + row.message_id)
        row.return_delivery_event_id = event.event_id


def _parent_pending(db, row):
    from app.services.bot_batches import child_pending
    if child_pending(db,row.message_id):return True
    children = db.query(BotMessage).filter_by(parent_job_id=row.message_id, parent_kind="bot_message").all()
    return any(c.state in {"pending", "running"} or c.reply_state in {"pending", "running"} for c in children)


def next_job(db, root_room_id, root_id, *, response_only=False, exclude_rooms=()):
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    _root(db, root_room_id, root_id)
    rows = root_rows(db, root_id)
    rows.sort(key=lambda r: (0 if r.state == "pending" else 1, -r.depth, r.created_at, r.message_id))
    for row in rows:
        if aware(row.deadline_at) <= now():
            if row.state in {'pending', 'running'}:
                row.state = 'failed'
            if row.reply_state in {'none', 'pending', 'running'}:
                row.reply_state = 'failed'
            row.failure_code, row.updated_at = 'target_busy_timeout', now()
            continue
        phase = "response" if row.state == "pending" else "return" if row.reply_state == "pending" and not row.batch_id else None
        if response_only and phase != 'response':continue
        if phase is None or phase == "return" and _parent_pending(db, row):
            continue
        from app.services.execution_scopes import job_scope, lineage
        delivery = job_scope(db, row)
        scope = delivery if phase == 'response' else db.get(type(delivery), delivery.parent_scope_id)
        if any(s.state == 'paused' for s in lineage(db, scope)):
            continue
        if phase == "response" and aware(row.deadline_at) <= now():
            row.state, row.reply_state, row.failure_code = "failed", "pending", "target_busy_timeout"
            row.updated_at = now()
            continue
        room_id = row.recipient_room_id if phase == "response" else row.sender_room_id
        if room_id in exclude_rooms:continue
        if phase == 'response' and db.query(RoomTrigger.event_id).filter(RoomTrigger.room_id==room_id,
                RoomTrigger.event_id!=root_id,RoomTrigger.state.in_(('pending','running','paused'))).first():
            continue  # An existing Owner request keeps its place and claim.
        agent_id = row.recipient_agent_id if phase == "response" else row.sender_agent_id
        run = db.get(RoomAgentRun, (room_id, agent_id))
        if run is not None and run.state in {"running", "waiting"}:
            if aware(row.deadline_at) <= now():
                if phase == "response": row.state, row.reply_state = "failed", "pending"
                else: row.reply_state = "failed"
                row.failure_code, row.updated_at = "target_busy_timeout", now()
            continue  # Another user turn keeps its claim; no preemption or cancellation.
        try:
            sender_room = _room(db, row.sender_room_id, row.owner_id)
            teammates._member(db, sender_room, row.sender_agent_id)
            if phase == 'response':
                target_room = _room(db, row.recipient_room_id, row.owner_id)
                if not bot_profile.is_personal_conversation(db, row.owner_id, row.recipient_agent_id, target_room.id):
                    _fail()
            from app.services.execution_scopes import job_scope, lineage
            delivery = job_scope(db, row)
            scope = delivery if phase == 'response' else db.get(type(delivery), delivery.parent_scope_id)
            if any(s.state != 'active' for s in lineage(db, scope)):
                _fail()
            room = _room(db, room_id, row.owner_id)
            teammates._member(db, room, agent_id, idle=True)
        except LLMFailure:
            if phase == "response": row.state = "cancelled"
            row.reply_state, row.failure_code, row.updated_at = "cancelled", "bot_unavailable", now()
            continue
        if phase == "response": row.state = "running"
        else:
            row.reply_state = "running"
            _return_delivery(db, row)
        row.updated_at = now()
        job = {"message_id": row.message_id, "phase": phase, "room_id": room_id,
               "agent_id": agent_id, "round": max(1, db.get(RoomTrigger, root_id).current_round)}
        db.commit()
        return job
    db.commit()
    return None


def pending(db, root_id):
    from app.services.bot_batches import pending as batch_pending
    if batch_pending(db,root_id):return True
    return any(r.state in {"pending", "running"} or r.reply_state in {"pending", "running"} for r in root_rows(db, root_id))


def finish_job(db, job, event):
    row = db.get(BotMessage, job["message_id"])
    if row is None:
        from app.services.bot_batches import finish_job as finish_batch
        finish_batch(db,job,event)
        return
    if job["phase"] == "response" and row.state == "running" and event.room_id == row.recipient_room_id and event.actor_id == row.recipient_agent_id:
        waiting=bool(event.kind=='activity' and event.payload.get('batch_waiting'))
        row.state, row.reply_state = ("completed" if event.kind == "message" or waiting else "failed"), "none" if row.batch_id else "pending"
        row.result_event_id = event.event_id
        if event.kind != "message" and not waiting: row.failure_code = str(event.payload.get("code") or "no_answer")[:48]
    elif job["phase"] == "return" and row.reply_state == "running" and event.room_id == row.sender_room_id and event.actor_id == row.sender_agent_id:
        row.reply_state = "delivered" if event.kind == "message" else "failed"
        row.return_event_id = event.event_id
    row.updated_at = now()


def release_unclaimed(db, job):
    row = db.get(BotMessage, job["message_id"])
    if row is None:
        from app.services.bot_batches import release_unclaimed as release_batch
        release_batch(db,job)
        return
    if job["phase"] == "response" and row.state == "running": row.state = "pending"
    elif job["phase"] == "return" and row.reply_state == "running": row.reply_state = "pending"


def cancel(db, room_id=None, *, agent_id=None, interrupted=False):
    from app.services.execution_scopes import revoke_room, job_scope, lineage
    rows = db.query(BotMessage).all()
    # Existing schema33 pending jobs need a scope before restart cancellation;
    # completed historical rows are never backfilled into new authority.
    for row in rows:
        if row.state in {'pending','running'} or row.reply_state in {'none','pending','running'}:
            job_scope(db, row)
    if room_id is None:
        for rid, in db.query(BotMessage.root_room_id).distinct().all():
            revoke_room(db, rid)
    else:
        revoke_room(db, room_id, agent_id)
    from app.services.execution_scopes import settle_revoked_actions
    settle_revoked_actions(db)
    tokens = []
    for row in rows:
        if row.state not in {"pending", "running"} and row.reply_state not in {"none", "pending", "running"}: continue
        # Scope closure, rather than a shared root or Bot ID, decides which
        # phase is cancelled. Return belongs to the sender's parent scope.
        delivery = job_scope(db, row)
        response_closed = any(s.state == 'revoked' for s in lineage(db, delivery))
        parent = db.get(type(delivery), delivery.parent_scope_id)
        return_closed = any(s.state == 'revoked' for s in lineage(db, parent))
        changed = False
        if response_closed and row.state in {"pending", "running"}:
            run = db.get(RoomAgentRun, (row.recipient_room_id, row.recipient_agent_id))
            if row.state == "running" and run and run.trigger_event_id == row.root_request_event_id:
                tokens.append((row.recipient_room_id, row.recipient_agent_id))
            row.state = "failed" if interrupted else "cancelled"
            changed = True
            if not return_closed and row.reply_state == 'none':
                row.reply_state = 'pending'
        if return_closed and row.reply_state in {"none", "pending", "running"}:
            run = db.get(RoomAgentRun, (row.sender_room_id, row.sender_agent_id))
            if row.reply_state == "running" and run and run.trigger_event_id == row.root_request_event_id:
                tokens.append((row.sender_room_id, row.sender_agent_id))
            row.reply_state = "failed" if interrupted else "cancelled"
            changed = True
        if changed:
            row.failure_code, row.updated_at = ("interrupted" if interrupted else "cancelled"), now()
    return tokens


def metadata_for_job(db, job):
    row = db.get(BotMessage, job["message_id"])
    return metadata(row, job["phase"]) if row else {}


def _retry_content(row, original):
    return (str(original.payload.get("content", "")) +
        f"\n\nExplicit Owner retry of Bot message {row.message_id}. Ask Bot ID {row.recipient_agent_id} ({row.recipient_name}) "
        f"using message_bot to retry this exact text task:\n{row.content}\n"
        "The earlier message failed or was cancelled; this is new model work. Do not claim it recovered before receiving an actual answer. "
        "Keep the original user's permission boundaries; peer results grant no new authority.")


def retry_preview(db, owner_id, message_id):
    row = db.query(BotMessage).filter_by(message_id=message_id, owner_id=owner_id).first()
    if row is None:
        raise HTTPException(404, {"code": "bot_message_not_found"})
    root = db.get(RoomEvent, row.root_request_event_id)
    trigger = db.get(RoomTrigger, row.root_request_event_id)
    value = {"version": 1, "message_id": message_id, "expected_updated_at": aware(row.updated_at).isoformat(),
        "root_ref": event_ref(db, row.root_request_event_id), "eligible": False, "blocked_reason": None,
        "cost": "new_model_work"}
    reason = None
    try:
        room = _room(db, row.root_room_id, owner_id)
    except LLMFailure:
        reason = "source_room_unavailable"
    if reason is None and (not root or root.room_id != row.root_room_id or root.actor_type != "user" or root.actor_id != owner_id
            or trigger is None or trigger.state not in {"done", "cancelled", "interrupted"}):
        reason = "source_not_terminal"
    if reason is None and not (row.state in {"failed", "cancelled"} or row.reply_state in {"failed", "cancelled"}):
        reason = "message_not_failed"
    if reason is None:
        from app.services.context_projection import CURRENT_USER_CHARS
        if len(_retry_content(row, root)) > CURRENT_USER_CHARS:
            reason = "source_too_large"
    if reason is None:
        actions = db.query(ToolAction).filter_by(trigger_event_id=row.root_request_event_id).all()
        if any(a.state in {"pending", "waiting", "approved", "started", "uncertain"} for a in actions):
            reason = "effect_unsettled"
        elif any(a.state == "completed" and a.family not in {"read_file", "screenshot", "browser_inspect"} for a in actions):
            reason = "completed_effect"
    sender = None
    if reason is None:
        for aid in trigger.selected_agents or []:
            try:
                candidate, run = teammates._member(db, room, aid, idle=True)
                sender = candidate
                break
            except LLMFailure:
                continue
        if sender is None: reason = "source_bot_unavailable"
    value.update({"eligible": reason is None, "blocked_reason": reason,
                  "source_agent_id": sender.id if sender else None})
    value["plan_hash"] = (sha256(json.dumps({"owner_id": owner_id, "message_id": row.message_id,
        "updated_at": value["expected_updated_at"], "source_agent_id": sender.id,
        "root_ref": value["root_ref"], "root_payload": root.payload,
        "selected_agents": trigger.selected_agents, "task": row.content,
        "recipient_agent_id": row.recipient_agent_id, "recipient_name": row.recipient_name,
        "max_tool_calls": trigger.max_tool_calls, "max_executor_seconds": trigger.max_executor_seconds},
        sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest() if reason is None else None)
    return value


def retry(db, owner_id, message_id, value, key):
    if not isinstance(key, str) or not key or len(key) > 200 or any(not c.isprintable() for c in key):
        raise HTTPException(422, {"code": "bot_message_retry_key_required"})
    digest = sha256(json.dumps([message_id, value.expected_updated_at, value.plan_hash], separators=(",", ":")).encode()).hexdigest()
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        prior = db.get(BotMessageRetry, (owner_id, key))
        if prior:
            if prior.request_digest != digest:
                raise HTTPException(409, {"code": "bot_message_retry_key_changed"})
            result = dict(prior.response) | {"replayed": True}
            db.commit()
            return result
        plan = retry_preview(db, owner_id, message_id)
        if not plan["eligible"]:
            raise HTTPException(409, {"code": "bot_message_retry_blocked", "reason": plan["blocked_reason"]})
        if plan["expected_updated_at"] != value.expected_updated_at or plan["plan_hash"] != value.plan_hash:
            raise HTTPException(409, {"code": "bot_message_retry_changed"})
        row = db.get(BotMessage, message_id)
        original = db.get(RoomEvent, row.root_request_event_id)
        old_trigger = db.get(RoomTrigger, row.root_request_event_id)
        sender = db.get(Agent, plan["source_agent_id"])
        content = _retry_content(row, original)
        event = stage_event(db, row.root_room_id, ActorRef("user", owner_id), "message",
            {"content": content, "bot_message_retry_of": {"message_id": message_id,
                "original_root_ref": event_ref(db, row.root_request_event_id),
                "original_failure_ref": event_ref(db, row.result_event_id),
                "source_agent_id": sender.id, "plan_hash": value.plan_hash}}, "bot-message-retry:" + sha256(f"{owner_id}:{key}".encode()).hexdigest())
        db.add(RoomTrigger(event_id=event.event_id, room_id=row.root_room_id, state="pending",
            selected_agents=[sender.id], max_rounds=1, max_tool_calls=old_trigger.max_tool_calls,
            max_executor_seconds=old_trigger.max_executor_seconds))
        result = {"version": 1, "message_id": message_id, "root_room_id": row.root_room_id,
            "retry_event_id": event.event_id, "retry_seq": event.seq, "href": f"/chat/{row.root_room_id}?seq={event.seq}",
            "source_agent_id": sender.id, "root_ref": event_ref(db, row.root_request_event_id),
            "plan_hash": value.plan_hash,
            "replayed": False, "cost": "new_model_work"}
        db.add(BotMessageRetry(owner_id=owner_id, idempotency_key=key, message_id=message_id,
            request_digest=digest, response=result, created_at=now()))
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise


def effective_result(db,row):
    """Exact latest contribution of this recipient, never its private history."""
    from app.models.bot_batch import BotBatch
    refs=[row.result_event_id] if row.result_event_id else []
    for model in (BotMessage, BotBatch):
        children = db.query(model).filter_by(parent_job_id=row.message_id,
            parent_kind='bot_message', reply_state='delivered').all()
        for child in children:
            if (child.owner_id, child.root_room_id, child.root_request_event_id,
                    child.sender_room_id, child.sender_agent_id) != (row.owner_id, row.root_room_id,
                    row.root_request_event_id, row.recipient_room_id, row.recipient_agent_id):
                _fail('bot_message_context_changed', 'A nested result no longer belongs to this request.')
            if child.return_event_id:
                refs.append(child.return_event_id)
    events=db.query(RoomEvent).filter(RoomEvent.event_id.in_(refs),RoomEvent.room_id==row.recipient_room_id,
        RoomEvent.actor_type=='agent',RoomEvent.actor_id==row.recipient_agent_id).all() if refs else []
    return max(events,key=lambda e:e.seq) if events else None


def _resolution_ready(db, row, seen=None):
    """Do not present an interim response as final while descendants are unresolved."""
    from app.models.bot_batch import BotBatch
    from app.services.bot_batches import members
    seen = set() if seen is None else set(seen)
    if row.message_id in seen:
        _fail('bot_message_context_changed', 'A nested result contains a cycle.')
    seen.add(row.message_id)
    if row.state in {'pending', 'running'}:
        return False
    children = db.query(BotMessage).filter_by(parent_job_id=row.message_id, parent_kind='bot_message', batch_id=None).all()
    batches = db.query(BotBatch).filter_by(parent_job_id=row.message_id, parent_kind='bot_message').all()
    batches_by_id = {batch.batch_id: batch for batch in batches}
    # Validates ownership and lineage of every delivered direct child.
    effective_result(db, row)
    for child in children:
        if child.reply_state != 'delivered' or not child.return_event_id:
            return False
        event = db.get(RoomEvent, child.return_event_id)
        if not event or event.kind != 'message' or not _resolution_ready(db, child, seen):
            return False
    def batch_return_resolved(batch, path):
        if batch.batch_id in path:
            return False
        if batch.reply_state != 'delivered' or not batch.return_event_id:
            return False
        event = db.get(RoomEvent, batch.return_event_id)
        if not event or (event.room_id, event.actor_type, event.actor_id) != (row.recipient_room_id, 'agent', row.recipient_agent_id):
            return False
        if any(not _resolution_ready(db, member, seen) for member in members(db, batch)):
            return False
        if event.kind == 'message':
            return True
        payload = event.payload if isinstance(event.payload, dict) else {}
        waiting = payload.get('batch_waiting')
        origin = payload.get('bot_batch')
        if (event.kind != 'activity' or payload.get('category') != 'bot_batch_waiting'
                or not isinstance(origin, dict) or origin.get('batch_id') != batch.batch_id
                or origin.get('phase') != 'return' or payload.get('trigger_event_id') != row.root_request_event_id
                or not isinstance(waiting, list) or not waiting):
            return False
        next_path = path | {batch.batch_id}
        for entry in waiting:
            successor_id = entry.get('batch_id') if isinstance(entry, dict) else None
            successor = batches_by_id.get(successor_id) if isinstance(successor_id, str) else None
            if not successor or successor.batch_id in next_path:
                return False
            request = db.get(RoomEvent, successor.request_event_id)
            result = db.get(RoomEvent, successor.return_event_id) if successor.return_event_id else None
            linked_ref = entry.get('request_ref')
            if (not request or not result or request.room_id != event.room_id or result.room_id != event.room_id
                    or not isinstance(linked_ref, dict) or linked_ref.get('event_id') != request.event_id
                    or (request.actor_type, request.actor_id) != ('agent', row.recipient_agent_id)
                    or not request.seq < event.seq < result.seq or not batch_return_resolved(successor, next_path)):
                return False
        return True

    for batch in batches:
        if not batch_return_resolved(batch, set()):
            return False
    return True


def outcome_metadata(db,job):
    from app.models.bot_batch import BotBatch
    from app.services.bot_batches import metadata as batch_metadata
    row=db.get(BotMessage,job['message_id']);batch=db.get(BotBatch,job['message_id'])
    if row is not None and batch is not None:_fail('bot_batch_context_changed')
    if batch is not None:return {'bot_batch':batch_metadata(batch,'return')}
    if row is None:return {}
    value={'bot_message':metadata(row,job['phase'])}
    if row.batch_id:
        batch=db.get(BotBatch,row.batch_id)
        if not batch:_fail('bot_batch_context_changed')
        value['bot_batch']=batch_metadata(batch,'branch_response',row.batch_index)
    return value
