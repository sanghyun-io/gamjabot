"""Owner-only Bot-private memory. Stored context never grants execution authority."""
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4
import json

from fastapi import HTTPException
from sqlalchemy import or_, text
from app.models.models import Agent, Conversation, Project, User
from app.models.collaboration import RoomEvent
from app.models.bot_memory import BotMemoryEntry, BotMemoryState, BotMemoryCommand
from app.schemas.bot_memory import BotMemoryWrite, BotMemoryEdit, BotMemoryDelete
from app.services.events import ActorRef, stage_event
from app.services.peer_reply import canonical_id
from app.services.result_content import safe_url

SCOPE = "bot_private"
MAX_ENTRIES = 64
MAX_MEMORY_CHARS = 8000


def _digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def fail(code, status=422):
    raise HTTPException(status, {"code": "bot_memory_" + code})


def _agent(db, owner_id, agent_id, *, active=False):
    owner = db.get(User, owner_id)
    agent = db.get(Agent, agent_id)
    if not owner or not owner.is_active or not agent or agent.user_id != owner_id:
        fail("not_found", 404)
    if active and not agent.is_active:
        fail("read_only", 409)
    return agent


def _personal_room(db, owner_id, agent_id):
    from app.services.bot_profile import canonical_conversation
    return canonical_conversation(db, owner_id, agent_id, active_only=False)


def _rows(db, owner_id, agent_id):
    return db.query(BotMemoryEntry).filter_by(owner_id=owner_id, agent_id=agent_id,
        deleted_at=None).order_by(BotMemoryEntry.memory_id).all()


def _entry(db, owner_id, agent_id, memory_id):
    if not canonical_id(memory_id):
        fail("invalid_id")
    row = db.query(BotMemoryEntry).filter_by(owner_id=owner_id, agent_id=agent_id,
        memory_id=memory_id).first()
    if row is None:
        fail("not_found", 404)
    return row


def _stamp(value):
    if value is None:
        return None
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).isoformat()


def dto(row):
    result = {name: getattr(row, name) for name in (
        "memory_id", "agent_id", "category", "content", "group_allowed", "source_kind", "source_room_id",
        "source_event_id", "source_seq", "source_url", "source_snapshot", "revision", "created_by", "updated_by")}
    result.update({name: _stamp(getattr(row, name)) for name in ("created_at", "updated_at", "deleted_at")})
    return {**result, "scope": SCOPE, "authority": "context_only"}


def _project_entry(row):
    imported = (row.source_snapshot or {}).get('memory_import')
    origin = ({'source_label': imported['source_label'][:120], 'authority': 'unverified_reference_only'}
              if isinstance(imported, dict) and isinstance(imported.get('source_label'), str) else None)
    return {"id": row.memory_id, "revision": row.revision, "scope": SCOPE,
        **({'import_origin': origin} if origin else {}),
        "category": row.category, "content": row.content, "group_allowed": bool(row.group_allowed), "source_kind": row.source_kind,
        "source_room_id": row.source_room_id, "source_event_id": row.source_event_id,
        "source_seq": row.source_seq, "source_url": row.source_url,
        "authority": "context_only"}


def projection(db, owner_id, agent_id, room_id):
    """Read only: apply the Owner policy and group exclusions to this Bot."""
    from app.services.memory_policy import eligible
    _agent(db, owner_id, agent_id)
    allowed,group=eligible(db,owner_id,agent_id,room_id)
    if not allowed:
        return {"eligible": False, "scope": SCOPE, "revision": 0, "entries": []}
    state = db.get(BotMemoryState, (owner_id, agent_id))
    return {"eligible": True, "scope": SCOPE, "revision": state.revision if state else 0,
        "entries": [_project_entry(row) for row in _rows(db, owner_id, agent_id) if not group or row.group_allowed]}


def listing(db, owner_id, agent_id):
    _agent(db, owner_id, agent_id)
    state = db.get(BotMemoryState, (owner_id, agent_id))
    room = _personal_room(db, owner_id, agent_id)
    return {"agent_id": agent_id, "scope": SCOPE, "authority": "context_only",
        "revision": state.revision if state else 0,
        "personal_room_id": room.id if room else None,
        "items": [dto(row) for row in _rows(db, owner_id, agent_id)]}


def detail(db, owner_id, agent_id, memory_id):
    _agent(db, owner_id, agent_id)
    return dto(_entry(db, owner_id, agent_id, memory_id))


def _assign(db, owner_id, row, value):
    content = value.content.strip()
    if not content:
        fail("content_empty")
    source = value.source
    source_snapshot = {}
    if source.kind == "event":
        room = db.query(Conversation).join(Project, Project.id == Conversation.project_id).filter(
            Conversation.id == source.room_id, Conversation.user_id == owner_id,
            Project.user_id == owner_id).first()
        event = db.query(RoomEvent).filter_by(room_id=source.room_id,
            event_id=source.event_id, seq=source.seq).first() if room else None
        if event is None:
            fail("source_not_found", 404)
        # This is an explicit Owner promotion of a reference, not new permission
        # from an Agent, summary, web page, or tool result.
        source_snapshot = {"actor_type": event.actor_type, "actor_id": event.actor_id,
            "name": event.actor_name_snapshot, "authority": "reference_only"}
        from app.services.bot_profile import trusted_memory_import
        imported = trusted_memory_import(db, owner_id, room.id, event.event_id, row.agent_id)
        if imported:
            source_snapshot['memory_import'] = dict(imported)
    if source.kind == "link" and not safe_url(source.url):
        fail("source_url_invalid")
    row.category, row.content = value.category, content
    row.source_kind, row.source_room_id = source.kind, source.room_id
    row.source_event_id, row.source_seq, row.source_url = source.event_id, source.seq, source.url
    row.source_snapshot = source_snapshot


def _require_idle(db, room):
    if room is None:
        return
    from app.services.room_memory import require_idle
    try:
        require_idle(db, room.id)
    except HTTPException as error:
        if isinstance(error.detail, dict) and error.detail.get("code") == "memory_room_busy":
            fail("busy", 409)
        raise
    from app.models.teammate import TeammateHandoff
    if db.query(TeammateHandoff).filter(TeammateHandoff.room_id == room.id, or_(
            TeammateHandoff.state.in_(("pending", "running")),
            TeammateHandoff.reply_state.in_(("pending", "running")))).first():
        fail("busy", 409)


def _budget(db, owner_id, agent_id, room):
    items = [_project_entry(row) for row in _rows(db, owner_id, agent_id)]
    if len(items) > MAX_ENTRIES:
        fail("too_many_entries")
    if len(json.dumps({SCOPE: items}, ensure_ascii=False)) > MAX_MEMORY_CHARS:
        fail("context_too_large")
    from app.models.collaboration import RoomMembership
    from app.services.memory_policy import eligible
    from app.services.memory_context import snapshot
    rooms=db.query(RoomMembership.room_id).join(Conversation,Conversation.id==RoomMembership.room_id).filter(
        Conversation.user_id==owner_id,RoomMembership.agent_id==agent_id,RoomMembership.is_active.is_(True)).all()
    for room_id, in rooms:
        if eligible(db,owner_id,agent_id,room_id)[0]:
            memory=snapshot(db,room_id,agent_id)["memory"]
            if len(json.dumps(memory,ensure_ascii=False))>MAX_MEMORY_CHARS:fail("context_too_large")


def mutate(db, owner_id, agent_id, operation, value, key, memory_id=None):
    """One Owner command, CAS, metadata audit, forget boundary and receipt."""
    if not isinstance(key, str) or not 1 <= len(key) <= 200 or not key.isprintable():
        fail("idempotency_invalid")
    expected_types = {"add": BotMemoryWrite, "edit": BotMemoryEdit, "delete": BotMemoryDelete}
    if operation not in expected_types or type(value) is not expected_types[operation]:
        fail("command_invalid")
    request_digest = _digest([agent_id, operation, memory_id, value.model_dump()])
    db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    try:
        _agent(db, owner_id, agent_id)  # Fresh access even for a receipt replay.
        prior = db.get(BotMemoryCommand, (owner_id, key))
        if prior is not None:
            if prior.agent_id != agent_id or prior.request_digest != request_digest:
                fail("idempotency_conflict", 409)
            result = {**prior.response, "replayed": True}
            db.commit()
            return result
        _agent(db, owner_id, agent_id, active=True)
        room = _personal_room(db, owner_id, agent_id)
        _require_idle(db, room)
        state = db.get(BotMemoryState, (owner_id, agent_id))
        current_revision = state.revision if state else 0
        if value.expected_scope_revision != current_revision:
            fail("revision_conflict", 409)
        row = None
        if operation != "add":
            row = _entry(db, owner_id, agent_id, memory_id)
            if row.deleted_at is not None or row.revision != value.expected_revision:
                fail("revision_conflict", 409)
        now = datetime.now(timezone.utc)
        if state is None:
            state = BotMemoryState(owner_id=owner_id, agent_id=agent_id, revision=0)
            db.add(state)
        if operation == "add":
            row = BotMemoryEntry(memory_id=str(uuid4()), owner_id=owner_id, agent_id=agent_id,
                revision=1, created_by=owner_id, updated_by=owner_id, created_at=now, updated_at=now)
            _assign(db, owner_id, row, value)
            db.add(row)
        elif operation == "edit":
            if 'source' not in value.model_fields_set and (row.source_snapshot or {}).get('memory_import'):
                row.category, row.content = value.category, value.content.strip()
                if not row.content: fail('content_empty')
            else:
                _assign(db, owner_id, row, value)
            row.revision += 1
            row.updated_by, row.updated_at = owner_id, now
        else:
            row.deleted_at, row.updated_at, row.updated_by = now, now, owner_id
            row.revision += 1
        state.revision += 1
        db.flush()
        audit = None
        if room is not None:
            audit = stage_event(db, room.id, ActorRef("user", owner_id), "activity",
                {"bot_memory": {"operation": operation, "agent_id": agent_id,
                    "memory_id": row.memory_id, "revision": state.revision, "scope": SCOPE}},
                "bot-memory:" + _digest([owner_id, key]), allow_inactive=True)
        if operation in {"edit", "delete"}:
            from app.services.memory_policy import advance_floors
            advance_floors(db,owner_id,[agent_id])
        db.flush()
        if operation!='delete':_budget(db, owner_id, agent_id, room)
        response = {"agent_id": agent_id, "memory_id": row.memory_id, "scope": SCOPE,
            "revision": state.revision, "entry_revision": row.revision, "replayed": False,
            "personal_room_id": room.id if room else None,
            "activity_event_id": audit.event_id if audit else None,
            "activity_seq": audit.seq if audit else None}
        db.add(BotMemoryCommand(owner_id=owner_id, idempotency_key=key, agent_id=agent_id,
            request_digest=request_digest, response=response, created_at=now))
        db.commit()
        return response
    except BaseException:
        db.rollback()
        raise


def visibility(db,owner_id,agent_id,memory_id,value,key):
    if not isinstance(key,str) or not 1<=len(key)<=200 or not key.isprintable():fail('idempotency_invalid')
    digest=_digest([agent_id,'visibility',memory_id,value.model_dump()])
    db.rollback();db.execute(text('BEGIN IMMEDIATE'))
    try:
        _agent(db,owner_id,agent_id)
        prior=db.get(BotMemoryCommand,(owner_id,key))
        if prior:
            if prior.agent_id!=agent_id or prior.request_digest!=digest:fail('idempotency_conflict',409)
            result={**prior.response,'replayed':True};db.commit();return result
        _agent(db,owner_id,agent_id,active=True)
        state=db.get(BotMemoryState,(owner_id,agent_id));row=_entry(db,owner_id,agent_id,memory_id)
        if not state or state.revision!=value.expected_scope_revision or row.revision!=value.expected_revision or row.deleted_at:
            fail('revision_conflict',409)
        now=datetime.now(timezone.utc);was_allowed=bool(row.group_allowed);row.group_allowed=value.group_allowed;row.revision+=1
        row.updated_at=now;row.updated_by=owner_id;state.revision+=1
        room=_personal_room(db,owner_id,agent_id);audit=None
        if room:
            audit=stage_event(db,room.id,ActorRef('user',owner_id),'activity',{'bot_memory':{'operation':'visibility',
                'agent_id':agent_id,'memory_id':memory_id,'revision':state.revision,'scope':SCOPE}},
                'bot-memory:'+_digest([owner_id,key]),allow_inactive=True)
        from app.services.memory_policy import advance_floors
        if was_allowed and not value.group_allowed:advance_floors(db,owner_id,[agent_id])
        db.flush()
        if value.group_allowed:_budget(db,owner_id,agent_id,room)
        result={'agent_id':agent_id,'memory_id':memory_id,'scope':SCOPE,'revision':state.revision,
            'entry_revision':row.revision,'replayed':False,'personal_room_id':room.id if room else None,
            'activity_event_id':audit.event_id if audit else None,'activity_seq':audit.seq if audit else None}
        db.add(BotMemoryCommand(owner_id=owner_id,idempotency_key=key,agent_id=agent_id,
            request_digest=digest,response=result,created_at=now));db.commit();return result
    except BaseException:db.rollback();raise
