"""Bounded, explicit retry lineage for previously admitted teammate results.

A later attempt inherits earlier results and only the proven sender-visible
parts of newly delegated work. Historical attempts without complete evidence
fail closed; this module never grants permission to repeat external effects.
"""
from app.models.collaboration import RequestRetryRecord, RoomEvent, RoomTrigger
from app.services.llm_service import LLMFailure

MAX_CHAIN = 32


def _changed():
    raise LLMFailure('teammate_result_changed', 'The earlier retry evidence is unavailable or changed.')


def next_context(db, failure, request, owner_id, *, _existing=None):
    """Derive a subsequent retry context without inferring unseen peer results."""
    from app.services import teammates
    from app.services.request_retry import _canonical_failure, teammate_retry_effects_safe

    payload = request.payload if request and isinstance(request.payload, dict) else {}
    old = payload.get('teammate_retry_context')
    if not isinstance(old, dict) or old.get('version') not in (1, 2):
        _changed()
    room_id = request.room_id
    if old.get('version') == 2:
        chain = old.get('chain')
        if not isinstance(chain, list) or not chain or len(chain) >= MAX_CHAIN:
            _changed()
    if (_canonical_failure(failure, room_id) is None or failure.actor_id != old.get('agent_id')
            or failure.payload.get('trigger_event_id') != request.event_id
            or failure.seq <= request.seq or failure.payload.get('code') in {'teammate_result_changed', 'cancelled'}):
        _changed()
    trigger = db.get(RoomTrigger, request.event_id, populate_existing=True)
    if not trigger or trigger.room_id != room_id or trigger.state != 'done':
        _changed()
    if db.query(RoomEvent.event_id).filter(
            RoomEvent.room_id == room_id, RoomEvent.seq > failure.seq,
            RoomEvent.actor_type == 'agent', RoomEvent.actor_id == failure.actor_id,
            RoomEvent.kind.in_(('message', 'failure', 'activity')),
            RoomEvent.payload['trigger_event_id'].as_string() == request.event_id).first():
        _changed()
    siblings = db.query(RequestRetryRecord).filter_by(owner_id=owner_id, room_id=room_id,
            source_failure_event_id=failure.event_id).all()
    if siblings and (len(siblings) != 1 or _existing is None
                     or siblings[0].retry_event_id != _existing.retry_event_id):
        _changed()
    teammates.retry_source_current(db, old, room_id=room_id, retry_id=request.event_id, owner_id=owner_id)
    from app.services.teammate_delivery_evidence import sources_for_root, safe_activity_ids_for_root
    new_sources = sources_for_root(db, owner_id, room_id, request.event_id, failure)
    delivery_ids = safe_activity_ids_for_root(db, owner_id, room_id, request.event_id, failure)
    if not teammate_retry_effects_safe(db, owner_id, room_id, request.event_id, delivery_activity_ids=delivery_ids):
        _changed()
    sources = list(old.get('sources', []))
    by_event = {item.get('event_id'): item for item in sources if isinstance(item, dict)}
    if len(by_event) != len(sources):
        _changed()
    for item in new_sources:
        prior = by_event.get(item['event_id'])
        if prior is not None and prior != item:
            _changed()
        if prior is None:
            sources.append(item)
            by_event[item['event_id']] = item
    if len(sources) > 64:
        _changed()
    chain = list(old.get('chain', [])) if old.get('version') == 2 else []
    if len(chain) >= MAX_CHAIN or any(not isinstance(item, dict) for item in chain):
        _changed()
    chain.append({'retry_id': request.event_id, 'failure_id': failure.event_id})
    result = {key: value for key, value in old.items() if key not in {'admission_digest', 'chain'}}
    result.update(version=2, failure_id=failure.event_id, chain=chain, sources=sources)
    return result


def validate_current(db, context, *, room_id, retry_id, owner_id):
    """Recompute the chain from server records, never from a user-supplied claim."""
    from app.services.request_retry import _hash
    if not isinstance(context, dict) or context.get('version') != 2:
        _changed()
    records = db.query(RequestRetryRecord).filter_by(retry_event_id=retry_id).all()
    if len(records) != 1:
        _changed()
    record = records[0]
    root = db.get(RoomEvent, retry_id, populate_existing=True)
    trigger = db.get(RoomTrigger, retry_id, populate_existing=True)
    if (record.owner_id != owner_id or record.room_id != room_id or not root or not trigger
            or root.room_id != room_id or root.seq != record.retry_seq
            or root.actor_type != 'user' or root.actor_id != owner_id or root.kind != 'message'
            or not isinstance(root.payload, dict) or root.payload.get('teammate_retry_context') != context
            or root.idempotency_key != 'request-retry:' + _hash([owner_id, record.idempotency_key])
            or record.request_digest != _hash({'room_id': room_id, 'failure_event_id': record.source_failure_event_id,
                                               'plan_hash': record.plan_hash})
            or record.source_request_event_id != record.source_trigger_event_id
            or root.payload.get('retry_of_failure_event_id') != record.source_failure_event_id
            or root.payload.get('retry_of_trigger_event_id') != record.source_trigger_event_id
            or root.payload.get('retry_agent_id') != record.agent_id
            or context.get('agent_id') != record.agent_id or context.get('owner_id') != owner_id
            or context.get('room_id') != room_id or context.get('failure_id') != record.source_failure_event_id
            or trigger.room_id != room_id or trigger.selected_agents != [record.agent_id]):
        _changed()
    source = db.get(RoomEvent, record.source_request_event_id, populate_existing=True)
    failure = db.get(RoomEvent, record.source_failure_event_id, populate_existing=True)
    if (not source or not failure or not isinstance(source.payload, dict)
            or root.payload.get('content') != source.payload.get('content')
            or source.seq >= root.seq or failure.seq >= root.seq):
        _changed()
    # The new record itself makes the usual superseded check true. Its presence
    # must be unique and is admitted only for this exact validation.
    siblings = db.query(RequestRetryRecord).filter_by(owner_id=owner_id, room_id=room_id,
        source_failure_event_id=failure.event_id).all()
    if len(siblings) != 1 or siblings[0].retry_event_id != retry_id:
        _changed()
    derived = _derive_existing(db, failure, source, owner_id, record)
    if derived != {key: value for key, value in context.items() if key != 'admission_digest'}:
        _changed()


def _derive_existing(db, failure, source, owner_id, record):
    # next_context's duplicate guard must remain enforced for preflight, while
    # validation of the already-created single record excludes itself.
    return next_context(db, failure, source, owner_id, _existing=record)
