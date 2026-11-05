"""DB-derived provenance for cross-room turns; never an API/model argument."""

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re
from uuid import UUID

from app.services.llm_service import LLMFailure


def _changed():
    raise LLMFailure("execution_context_changed", "Bot task execution context changed; start a new task.")


def _id(value):
    if type(value) is not int or value < 1:
        _changed()
    return value


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            _changed()
    except (ValueError, TypeError, AttributeError):
        _changed()
    return value


@dataclass(frozen=True)
class EventReference:
    room_id: int
    event_id: str
    seq: int
    actor_type: str
    actor_id: int | None
    kind: str


def _ref(value, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, dict):
        _changed()
    actor = value.get("actor_type")
    actor_id = value.get("actor_id")
    if actor not in {"user", "agent", "system", "tool"}:
        _changed()
    if actor_id is not None:
        _id(actor_id)
    if actor in {"user", "agent"} and actor_id is None:
        _changed()
    kind = value.get("kind")
    if not isinstance(kind, str) or not re.fullmatch(r"[a-z_]{1,40}", kind):
        _changed()
    return EventReference(_id(value.get("room_id")), _uuid(value.get("event_id")),
                          _id(value.get("seq")), actor, actor_id, kind)


@dataclass(frozen=True)
class BatchPeerResult:
    member_index: int
    message_id: str
    agent_id: int
    name: str
    request_ref: EventReference
    delivery_ref: EventReference
    result_ref: EventReference | None
    content: str | None
    original_chars: int
    included_chars: int
    omitted_chars: int
    failure_code: str | None
    state: str


def _batch_peers(values, *, batch_id, execution_room_id, agent_id):
    if not isinstance(values,list) or not 2<=len(values)<=8:
        _changed()
    peers=[]
    for item in values:
        if not isinstance(item,dict):_changed()
        index=item.get('member_index')
        if type(index) is not int or not 0<=index<len(values):_changed()
        message_id=_uuid(item.get('message_id'));target=_id(item.get('agent_id'))
        name=item.get('name');body=item.get('content');failure=item.get('failure_code')
        request=_ref(item.get('request_ref'));delivery=_ref(item.get('delivery_ref'))
        result=_ref(item.get('result_ref'),optional=True)
        counts=tuple(item.get(key+'_chars') for key in ('original','included','omitted'))
        if (not isinstance(name,str) or not name or len(name)>200
                or body is not None and not isinstance(body,str)
                or any(type(count) is not int or count<0 for count in counts)
                or counts[1]!=len(body or '') or counts[0]!=counts[1]+counts[2] or counts[1]>4000
                or (result is None)!=(body is None) or result is None and any(counts)
                or message_id==batch_id or target==agent_id
                or request.room_id!=execution_room_id or request.actor_type!='agent' or request.actor_id!=agent_id or request.kind!='message'
                or delivery.actor_type!='system' or delivery.actor_id is not None or delivery.kind!='activity'
                or result is not None and (result.room_id!=delivery.room_id or result.actor_type!='agent' or result.actor_id!=target)
                or item.get('state') not in {'completed','failed','cancelled'}
                or failure is not None and (not isinstance(failure,str) or not re.fullmatch(r'[a-z_]{1,64}',failure))):
            _changed()
        peers.append(BatchPeerResult(index,message_id,target,name,request,delivery,result,body,*counts,failure,item['state']))
    if (len({p.member_index for p in peers})!=len(peers) or len({p.message_id for p in peers})!=len(peers)
            or len({p.agent_id for p in peers})!=len(peers)):
        _changed()
    return tuple(sorted(peers,key=lambda peer:peer.member_index))


@dataclass(frozen=True)
class ExecutionOrigin:
    owner_id: int
    root_room_id: int
    root_request_event_id: str
    execution_room_id: int
    agent_id: int
    job_id: str
    phase: str
    generation: int | None
    context_mode: str
    task_content: str
    task_ref: EventReference
    root_ref: EventReference
    source_task_ref: EventReference | None
    peer_result_ref: EventReference | None
    peer_result_content: str | None
    failure_code: str | None
    peer_result_original_chars: int
    peer_result_included_chars: int
    peer_result_omitted_chars: int
    batch_results: tuple[BatchPeerResult, ...] = ()
    batch_id: str | None = None

    @property
    def fingerprint(self):
        value=asdict(self)
        if self.batch_id is None:
            # Preserve the scalar-origin continuation binding byte-for-byte.
            value.pop('batch_results');value.pop('batch_id')
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode()
        return sha256(encoded).hexdigest()


def load_execution_origin(db, *, job_id, phase, execution_room_id, agent_id, generation=None):
    """Resolve persisted queue facts, membership, Owner and run before projection.

    Construction alone grants nothing: consumers re-resolve and compare the full
    immutable value, including the purpose and generation, after every await.
    """
    from app.models.bot_message import BotMessage
    from app.models.bot_batch import BotBatch

    _uuid(job_id)
    _id(execution_room_id)
    _id(agent_id)
    if phase not in {"response", "return"} or (generation is not None and
            (type(generation) is not int or generation < 0)):
        _changed()
    single,batch=db.get(BotMessage,job_id),db.get(BotBatch,job_id)
    if single is not None and batch is not None:
        raise LLMFailure('bot_batch_context_changed','Bot job identity is ambiguous; start a new task.')
    if single is None and batch is None:_changed()
    if batch is not None:
        if phase!='return':_changed()
        from app.services.bot_batches import resolve_execution_origin
    else:
        from app.services.bot_messages import resolve_execution_origin
    data = resolve_execution_origin(db, job_id, phase, execution_room_id, agent_id, generation)
    if not isinstance(data, dict):
        _changed()
    for key, expected in (("job_id", job_id), ("phase", phase),
                          ("execution_room_id", execution_room_id), ("agent_id", agent_id),
                          ("generation", generation)):
        if data.get(key) != expected:
            _changed()
    owner, root_room = _id(data.get("owner_id")), _id(data.get("root_room_id"))
    root_id = _uuid(data.get("root_request_event_id"))
    root = _ref(data.get("root_ref"))
    task = _ref(data.get("task_ref"))
    source_task = _ref(data.get("source_task_ref"), optional=True)
    peer = _ref(data.get("peer_result_ref"), optional=True)
    mode, content, peer_content = data.get("context_mode"), data.get("task_content"), data.get("peer_result_content")
    failure = data.get("failure_code")
    if peer_content is not None and not isinstance(peer_content,str):
        _changed()
    counts=tuple(data.get('peer_result_'+name+'_chars') for name in ('original','included','omitted'))
    if (any(type(count) is not int or count < 0 for count in counts)
            or counts[1] != len(peer_content or '') or counts[0] != counts[1]+counts[2]
            or counts[1] > 4000 or peer is None and any(counts)):
        _changed()
    if (root.room_id != root_room or root.event_id != root_id
            or root.actor_type != "user" or root.actor_id != owner or root.kind != "message"
            or task.actor_type != "agent" or task.kind != "message"
            or source_task is not None and (source_task.actor_type != "agent" or source_task.kind != "message")
            or mode not in {"delegated", "user_room"}
            or phase == "response" and mode != "delegated"
            or mode == "user_room" and execution_room_id != root_room
            or not isinstance(content, str) or not content.strip()
            or peer_content is not None and not isinstance(peer_content, str)
            or (peer is None) != (peer_content is None)
            or peer is not None and peer.actor_type != "agent"
            or failure is not None and (not isinstance(failure, str) or not re.fullmatch(r"[a-z_]{1,64}", failure))):
        _changed()
    batch_id=job_id if batch is not None else None
    if batch_id is not None:
        if (data.get('batch_id',batch_id)!=batch_id or peer is not None
                or task.room_id!=execution_room_id or task.actor_id!=agent_id):_changed()
        batch_results=_batch_peers(data.get('batch_results'),batch_id=batch_id,
            execution_room_id=execution_room_id,agent_id=agent_id)
    else:
        if data.get('batch_id') is not None or data.get('batch_results') not in (None,[]):_changed()
        batch_results=()
    return ExecutionOrigin(owner, root_room, root_id, execution_room_id, agent_id,
                           job_id, phase, generation, mode, content, task, root,
                           source_task, peer, peer_content, failure, *counts,batch_results,batch_id)


def require_execution_origin_current(db, origin):
    if not isinstance(origin, ExecutionOrigin):
        _changed()
    current = load_execution_origin(db, job_id=origin.job_id, phase=origin.phase,
        execution_room_id=origin.execution_room_id, agent_id=origin.agent_id,
        generation=origin.generation)
    if current != origin:
        _changed()
    return current
