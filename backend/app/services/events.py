"""Single room event allocator and canonical message writer."""

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import and_,func,or_,select,text,update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models.collaboration import RoomEvent, RoomMembership, RoomDraft, RoomTrigger, RoomAgentRun, ToolAction
from app.models.models import Agent, Conversation, Message, User


@dataclass(frozen=True)
class ActorRef:
    actor_type: str
    actor_id: int | None

def reply_target(db, room_id, owner_id, value, *, before_seq=None):
    """Resolve only an exact, same-room authored message, without accepting forged labels."""
    if not isinstance(value,dict) or set(value)!={'event_id','seq'} or type(value.get('seq')) is not int or value['seq']<1:
        raise HTTPException(422,{'code':'reply_invalid'})
    try:
        if not isinstance(value['event_id'],str) or str(UUID(value['event_id']))!=value['event_id']:raise ValueError()
    except (ValueError,AttributeError):raise HTTPException(422,{'code':'reply_invalid'}) from None
    row=db.get(RoomEvent,value['event_id'])
    if (not row or row.room_id!=room_id or row.seq!=value['seq'] or (before_seq is not None and row.seq>=before_seq)
            or row.kind!='message' or not isinstance(row.payload,dict) or not isinstance(row.payload.get('content'),str)):
        raise HTTPException(404,{'code':'reply_target_unavailable'})
    if row.actor_type=='user' and row.actor_id==owner_id:return row
    if row.actor_type=='agent' and row.actor_id is not None:
        agent=db.get(Agent,row.actor_id)
        if agent and agent.user_id==owner_id:return row
    raise HTTPException(404,{'code':'reply_target_unavailable'})

def reply_thread_page(db,room_id,owner_id,root_id,root_seq,*,after_seq=0,upper_seq=None,limit=50):
    """Read one immutable upper-bounded page of direct replies to an exact message."""
    root=reply_target(db,room_id,owner_id,{'event_id':root_id,'seq':root_seq})
    if (type(after_seq) is not int or after_seq<0 or type(limit) is not int or not 1<=limit<=100
            or (upper_seq is None and after_seq!=0)
            or (upper_seq is not None and (type(upper_seq) is not int or upper_seq<root.seq or after_seq>upper_seq))):
        raise HTTPException(422,{'code':'reply_cursor_invalid'})
    high=db.query(func.max(RoomEvent.seq)).filter(RoomEvent.room_id==room_id).scalar() or root.seq
    if upper_seq is None:upper_seq=high
    entries=func.json_each(RoomEvent.payload,'$.reply_to').table_valued('key','value')
    field_count=select(func.count()).select_from(entries).correlate(RoomEvent).scalar_subquery()
    rows=(db.query(RoomEvent).outerjoin(Agent,Agent.id==RoomEvent.actor_id)
        .filter(RoomEvent.room_id==room_id,RoomEvent.seq>root.seq,RoomEvent.seq>after_seq,RoomEvent.seq<=upper_seq,
            RoomEvent.kind=='message',func.json_type(RoomEvent.payload,'$.content')=='text',
            func.json_type(RoomEvent.payload,'$.reply_to')=='object',
            func.json_type(RoomEvent.payload,'$.reply_to.seq')=='integer',
            RoomEvent.payload['reply_to']['event_id'].as_string()==root.event_id,
            RoomEvent.payload['reply_to']['seq'].as_integer()==root.seq,field_count==2,
            or_(and_(RoomEvent.actor_type=='user',RoomEvent.actor_id==owner_id),
                and_(RoomEvent.actor_type=='agent',Agent.user_id==owner_id)))
        .order_by(RoomEvent.seq.asc()).limit(limit+1).all())
    page=rows[:limit];more=len(rows)>limit
    return {'version':1,'root':{'event_id':root.event_id,'seq':root.seq},'upper_seq':upper_seq,
            'items':page,'has_more':more,'next_after_seq':page[-1].seq if more else None}

MAX_THREAD_DEPTH = 64

def descendant_thread_page(db,room_id,owner_id,root_id,root_seq,*,after_seq=0,upper_seq=None,limit=50):
    """Read an exact-root descendant page under a fixed room high-water mark."""
    root=reply_target(db,room_id,owner_id,{'event_id':root_id,'seq':root_seq})
    if (type(after_seq) is not int or after_seq<0 or type(limit) is not int or not 1<=limit<=100
            or (upper_seq is None and after_seq!=0)
            or (upper_seq is not None and (type(upper_seq) is not int or upper_seq<root.seq or after_seq>upper_seq))):
        raise HTTPException(422,{'code':'reply_cursor_invalid'})
    high=db.query(func.max(RoomEvent.seq)).filter(RoomEvent.room_id==room_id).scalar() or root.seq
    if upper_seq is None:upper_seq=high
    elif upper_seq>high:raise HTTPException(422,{'code':'reply_cursor_invalid'})
    # Materialize only this room's eligible messages, then follow exact parent
    # identities. Increasing sequence numbers prevent cycles; depth is bounded.
    rows=db.execute(text('''
        WITH RECURSIVE source AS MATERIALIZED (
          SELECT e.event_id, e.seq,
                 CASE WHEN json_valid(e.payload) THEN e.payload ELSE '{}' END AS payload
          FROM room_events e LEFT JOIN agents a ON a.id=e.actor_id
          WHERE e.room_id=:room_id AND e.seq>:root_seq AND e.seq<=:upper_seq
            AND e.kind='message'
            AND ((e.actor_type='user' AND e.actor_id=:owner_id)
                 OR (e.actor_type='agent' AND a.user_id=:owner_id))
        ), valid AS MATERIALIZED (
          SELECT event_id, seq, json_extract(payload,'$.reply_to.event_id') AS parent_id,
                 json_extract(payload,'$.reply_to.seq') AS parent_seq
          FROM source
          WHERE json_type(payload,'$.content')='text'
            AND json_type(payload,'$.reply_to')='object'
            AND json_type(payload,'$.reply_to.event_id')='text'
            AND json_type(payload,'$.reply_to.seq')='integer'
            AND (SELECT count(*) FROM json_each(payload,'$.reply_to'))=2
            AND (SELECT count(DISTINCT key) FROM json_each(payload,'$.reply_to')
                 WHERE key IN ('event_id','seq'))=2
            AND length(event_id)=36 AND event_id=lower(event_id)
            AND substr(event_id,9,1)='-' AND substr(event_id,14,1)='-'
            AND substr(event_id,19,1)='-' AND substr(event_id,24,1)='-'
            AND length(replace(event_id,'-',''))=32
            AND replace(event_id,'-','') NOT GLOB '*[^0-9a-f]*'
        ), tree(event_id,seq,parent_id,parent_seq,depth) AS (
          SELECT event_id,seq,parent_id,parent_seq,1 FROM valid
          WHERE parent_id=:root_id AND parent_seq=:root_seq AND seq>:root_seq
          UNION ALL
          SELECT child.event_id,child.seq,child.parent_id,child.parent_seq,parent.depth+1
          FROM tree parent JOIN valid child ON child.parent_id=parent.event_id
               AND child.parent_seq=parent.seq AND child.seq>parent.seq
          WHERE parent.depth<:max_depth
        ), flag AS (
          SELECT EXISTS(SELECT 1 FROM tree parent JOIN valid child
                        ON child.parent_id=parent.event_id AND child.parent_seq=parent.seq
                           AND child.seq>parent.seq WHERE parent.depth=:max_depth) AS limited
        ), page AS (
          SELECT * FROM tree WHERE seq>:after_seq ORDER BY seq LIMIT :page_limit
        )
        SELECT page.event_id,page.seq,page.parent_id,page.parent_seq,page.depth,flag.limited
        FROM flag LEFT JOIN page ON 1=1 ORDER BY page.seq
    '''),{'room_id':room_id,'owner_id':owner_id,'root_id':root.event_id,'root_seq':root.seq,
          'upper_seq':upper_seq,'after_seq':after_seq,'max_depth':MAX_THREAD_DEPTH,'page_limit':limit+1}).mappings().all()
    limited=bool(rows[0]['limited'])
    page=[row for row in rows if row['event_id'] is not None]
    more=len(page)>limit;page=page[:limit]
    ids=[row['event_id'] for row in page]
    found={event.event_id:event for event in db.query(RoomEvent).filter(
        RoomEvent.room_id==room_id,RoomEvent.event_id.in_(ids)).all()} if ids else {}
    if len(found)!=len(ids):raise HTTPException(409,{'code':'reply_thread_changed'})
    return {'version':2,'root':{'event_id':root.event_id,'seq':root.seq},'upper_seq':upper_seq,
            'items':[{'event':found[row['event_id']],
                      'parent':{'event_id':row['parent_id'],'seq':row['parent_seq']},'depth':row['depth']} for row in page],
            'has_more':more,'next_after_seq':page[-1]['seq'] if more else None,'depth_limited':limited}

def canonical_reply_scope_failure(db, event, owner_id):
    """Validate a system-recorded admission failure without inventing Agent speech."""
    p=event.payload if event and isinstance(event.payload,dict) else {}
    trigger_id=p.get('trigger_event_id');agent_id=p.get('agent_id');generation=p.get('generation')
    if (not event or event.actor_type!='system' or event.actor_id is not None or event.kind!='failure'
        or p.get('category')!='reply_target_unavailable' or p.get('code')!='reply_target_unavailable'
        or type(p.get('version')) is not int or p['version']!=1 or type(p.get('round')) is not int or p['round']!=1 or type(agent_id) is not int or agent_id<=0
        or type(generation) is not int or generation<0
        or event.idempotency_key!=f'reply-scope:{trigger_id}:{agent_id}:{generation}'):
        return None
    try:
        if not isinstance(trigger_id,str) or str(UUID(trigger_id))!=trigger_id or str(UUID(event.event_id))!=event.event_id:return None
    except (ValueError,TypeError,AttributeError):return None
    root=db.get(RoomEvent,trigger_id);trigger=db.get(RoomTrigger,trigger_id)
    room=db.get(Conversation,event.room_id);agent=db.get(Agent,agent_id);run=db.get(RoomAgentRun,(event.room_id,agent_id))
    if (not room or room.user_id!=owner_id or not agent or agent.user_id!=owner_id or not run or run.generation<generation
        or not root or root.room_id!=event.room_id or root.actor_type!='user' or root.actor_id!=owner_id
        or root.kind!='message' or not isinstance(root.payload,dict) or root.seq>=event.seq or not trigger or trigger.room_id!=event.room_id
        or trigger.state!='cancelled' or trigger.current_round!=1 or trigger.selected_agents!=[agent_id]
        or db.query(ToolAction.action_id).filter_by(trigger_event_id=trigger_id).first()
        or db.query(RoomEvent.event_id).filter(RoomEvent.room_id==event.room_id,RoomEvent.actor_type=='agent',
            RoomEvent.payload['trigger_event_id'].as_string()==trigger_id,
            RoomEvent.kind.in_(('message','failure','activity'))).first()):return None
    try:source=reply_target(db,event.room_id,owner_id,root.payload.get('reply_to'),before_seq=root.seq)
    except HTTPException:return None
    if source.actor_type!='agent' or source.actor_id!=agent_id or p.get('source_event_id')!=source.event_id:
        return None
    return root,agent_id,generation,source


def local_device_id() -> str:
    root = Path(os.environ.get("GAMJA_APP_DATA_DIR", Path.cwd()))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / "device-id"
    if path.exists():
        return str(UUID(path.read_text().strip()))
    created = str(uuid4())
    temporary = root / f".device-id-{uuid4().hex}"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(created + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)  # Publish a fully written file, never a partial UUID.
            return created
        except FileExistsError:
            return str(UUID(path.read_text().strip()))
    finally:
        temporary.unlink(missing_ok=True)


def _actor_name(db: Session, room: Conversation, actor: ActorRef) -> str:
    if actor.actor_type == "user" and actor.actor_id == room.user_id:
        user = db.get(User, actor.actor_id)
        if user and user.is_active:
            return user.username
    if actor.actor_type == "agent" and actor.actor_id is not None:
        agent = db.get(Agent, actor.actor_id)
        member = db.get(RoomMembership, (room.id, actor.actor_id))
        if agent and agent.is_active and agent.user_id == room.user_id and member and member.is_active:
            return agent.name
    if actor.actor_type == "system" and actor.actor_id is None:
        return "System"
    if actor.actor_type == "tool" and actor.actor_id is None:
        return "Tool"
    raise HTTPException(403, "Actor is not authorized in this room")


def stage_event(db: Session, room_id: int, actor: ActorRef, kind: str,
                payload: dict, idempotency_key: str | None = None,
                *, allow_inactive: bool = False) -> RoomEvent:
    """Stage one canonical event inside the caller's transaction."""
    if kind not in {"message", "tool_result", "failure", "summary", "activity"}:
        raise ValueError("Unsupported room event kind")
    if not isinstance(payload, dict):
        raise ValueError("Event payload must be an object")
    if idempotency_key is not None and (not idempotency_key or len(idempotency_key) > 200):
        raise ValueError("Invalid idempotency key")
    room_filter = [Conversation.id == room_id]
    if not allow_inactive:
        room_filter.append(Conversation.is_active.is_(True))
    live = db.execute(update(Conversation).where(*room_filter).values(
        updated_at=Conversation.updated_at)).rowcount
    if not live:
        raise HTTPException(404, "Room not found")
    room = db.get(Conversation, room_id)
    name = _actor_name(db, room, actor)
    if idempotency_key:
        prior = db.query(RoomEvent).filter_by(
            room_id=room_id, idempotency_key=idempotency_key).first()
        if prior:
            original_payload = dict(prior.payload)
            if prior.kind == "message":
                original_payload.pop("legacy_message_id", None)
            if (prior.actor_type != actor.actor_type or prior.actor_id != actor.actor_id
                    or prior.kind != kind or original_payload != payload):
                raise HTTPException(409, "Idempotency key already used for a different event")
            prior._duplicate = True
            return prior
    if kind=='message' and actor.actor_type=='user':
        if idempotency_key:
            from app.services.routine_reference_fence import existing,rejection_status
            code=existing(db,actor.actor_id,room_id,idempotency_key,payload)
            if code:raise HTTPException(rejection_status(code),{'code':code,'fenced':True})
        target=payload.get('page_target')
        if target is not None:
            from app.services.room_results import page_context_content
            page_context_content(db,actor.actor_id,room_id,target,payload.get('content',''),selection=payload.get('page_selection'))
        routine_ref=payload.get('routine_ref')
        if routine_ref is not None:
            from app.services.routine_persistence import validate_reference
            validate_reference(db,actor.actor_id,room_id,routine_ref)
        ref=payload.get('reply_to')
        if ref is not None:
            source=reply_target(db,room_id,actor.actor_id,ref)
            if source.actor_type=='agent':
                from app.services.room_service import eligible_agents
                from app.services.context_projection import selected_agent_ids
                try:selected_agent_ids(eligible_agents(db,room_id),str(payload.get('content','')),source.actor_id)
                except ValueError:raise HTTPException(409,{'code':'reply_target_unavailable'}) from None
        if idempotency_key:
            draft=db.get(RoomDraft,(actor.actor_id,room_id))
            if draft and draft.idempotency_key==idempotency_key:
                if draft.reply_to!=ref or ((draft.reply_to is not None or ref is not None) and draft.content!=payload.get('content')):
                    raise HTTPException(409,{'code':'reply_draft_changed'})
    seq = (db.scalar(select(func.max(RoomEvent.seq)).where(RoomEvent.room_id == room_id)) or 0) + 1
    event_payload = dict(payload)
    if kind == "message":
        content = event_payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Message content required")
        message = Message(conversation_id=room_id,
                          agent_id=actor.actor_id if actor.actor_type == "agent" else None,
                          role="assistant" if actor.actor_type == "agent" else "user" if actor.actor_type == "user" else "system",
                          content=content)
        db.add(message)
        db.flush()
        event_payload["legacy_message_id"] = message.id
    event = RoomEvent(event_id=str(uuid4()), device_id=local_device_id(), room_id=room_id, seq=seq,
                      actor_type=actor.actor_type, actor_id=actor.actor_id, actor_name_snapshot=name,
                      kind=kind, payload=event_payload, created_at=datetime.now(timezone.utc),
                      idempotency_key=idempotency_key)
    db.add(event)
    db.flush()
    event._duplicate = False
    return event


def append_event(db: Session, room_id: int, actor: ActorRef, kind: str,
                 payload: dict, idempotency_key: str | None = None,
                 *, allow_inactive: bool = False, retry: bool = True) -> RoomEvent:
    """Commit one event. SQLite's first write serializes seq allocation per room."""
    for attempt in range(8):
        try:
            event = stage_event(db, room_id, actor, kind, payload, idempotency_key,
                                allow_inactive=allow_inactive)
            db.commit()
            return event
        except (OperationalError, IntegrityError) as exc:
            db.rollback()
            if not retry or attempt == 7 or not any(text in str(exc).lower() for text in ("locked", "busy", "unique")):
                raise
            time.sleep(0.01 * (attempt + 1))
        except BaseException:
            db.rollback()
            raise
    raise RuntimeError("Event allocation retry exhausted")


def events_after(db: Session, room_id: int, after_seq: int, limit: int) -> list[RoomEvent]:
    if after_seq < 0 or not 1 <= limit <= 500:
        raise ValueError("Invalid event cursor or limit")
    return db.query(RoomEvent).filter(RoomEvent.room_id == room_id, RoomEvent.seq > after_seq).order_by(RoomEvent.seq).limit(limit).all()
