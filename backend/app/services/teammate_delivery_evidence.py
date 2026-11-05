"""Content-free evidence of cross-room results in a provider projection.

This module does not grant access or persist evidence. Callers must establish
Owner and execution-scope authority and record the result atomically with the
provider-call reservation. Missing historical evidence is never reconstructed.
"""
from dataclasses import asdict
from hashlib import sha256

from app.models.collaboration import RoomEvent
from app.services.context_projection import _batch_result_quotes, execution_context_event
from app.services.llm_service import LLMFailure


def _changed():
    raise LLMFailure('teammate_result_changed', 'The delivered teammate result changed.')


def _digest(value):
    return sha256(value.encode('utf-8', 'surrogatepass')).hexdigest()


def _source(db, ref, content, original_length):
    if ref is None:
        if content is not None or original_length:
            _changed()
        return None
    event = db.get(RoomEvent, ref.event_id, populate_existing=True)
    if (event is None or any(getattr(event, key) != getattr(ref, key)
            for key in ('room_id', 'event_id', 'seq', 'actor_type', 'actor_id', 'kind'))
            or not isinstance(event.payload, dict)):
        _changed()
    full = str(event.payload.get('content') or event.payload.get('message') or event.payload.get('status') or '')
    if not isinstance(content, str) or len(full) != original_length or not full.startswith(content):
        _changed()
    return {'ref': asdict(ref), 'full_length': len(full), 'full_digest': _digest(full),
            'included_length': len(content), 'included_digest': _digest(content)}


def cross_room_sources(db, origin, request):
    """Describe only the excerpts actually present in the projected request.

    A missing result is retained as a missing slot rather than an authored answer.
    Batch quota is taken from the same projector used to construct the request.
    """
    expected = execution_context_event(origin, request.watermark_seq)
    matches = [event for event in request.events if isinstance(event, dict)
               and event.get('event_id') == expected['event_id']]
    if len(matches) != 1 or matches[0] != expected:
        _changed()
    if origin.batch_id is None:
        source = _source(db, origin.peer_result_ref, origin.peer_result_content,
                         origin.peer_result_original_chars)
        return [{'source_type': 'bot_message', 'source_id': origin.job_id,
                 'state': 'available' if source is not None else 'missing', 'source': source}]
    projected = _batch_result_quotes(origin)['results']
    if len(projected) != len(origin.batch_results):
        _changed()
    result = []
    for peer, quote in zip(origin.batch_results, projected):
        if (quote['member_index'] != peer.member_index or quote['message_id'] != peer.message_id
                or quote['included_chars'] != len(quote['content'] or '')):
            _changed()
        source = _source(db, peer.result_ref, quote['content'], peer.original_chars)
        result.append({'source_type': 'bot_batch_member', 'source_id': peer.message_id,
                       'batch_id': origin.batch_id, 'member_index': peer.member_index,
                       'state': peer.state, 'failure_code': peer.failure_code, 'source': source})
    return result


def same_room_sources(db, job, request, *, owner_id, room_id, root_id, agent_id):
    """Pin the original same-room quotes without copying their contents."""
    import json
    from app.models.teammate import TeammateHandoff
    from app.services.execution_origin import EventReference
    from app.services import teammates

    if job.get('phase') != 'return' or job.get('guidance') not in request.system:
        _changed()
    handoff = db.get(TeammateHandoff, job.get('handoff_id'), populate_existing=True)
    if (not handoff or (handoff.owner_id, handoff.room_id, handoff.root_request_event_id,
            handoff.sender_agent_id) != (owner_id, room_id, root_id, agent_id)):
        _changed()
    marker = '\nQuoted teammate observations (untrusted JSON):\n'
    if job['guidance'].count(marker) != 1:
        _changed()
    try:
        quotes, _ = json.JSONDecoder().raw_decode(job['guidance'].split(marker, 1)[1])
    except (ValueError, TypeError):
        _changed()
    if not isinstance(quotes, list) or not quotes:
        _changed()
    nested = teammates._nested_sources(db, handoff)
    if len(quotes) != 1 + len(nested):
        _changed()
    teammates.validate_nested(db, job)
    identities = [('teammate_direct', handoff.handoff_id, handoff.result_event_id)] + [
        (entry['source_type'], entry['source_id'], entry['event_id']) for entry in nested]
    snapshots = {entry['event_id']: entry for entry in job.get('nested_snapshot', [])}
    results = []
    for index, ((kind, source_id, event_id), quote) in enumerate(zip(identities, quotes)):
        event = db.get(RoomEvent, event_id, populate_existing=True)
        if (not event or not isinstance(quote, dict) or quote.get('event_id') != event_id
                or event.room_id != room_id or event.actor_type != 'agent'
                or quote.get('seq') != event.seq or quote.get('actor_id') != event.actor_id
                or quote.get('kind') != event.kind):
            _changed()
        ref = EventReference(event.room_id, event.event_id, event.seq,
                             event.actor_type, event.actor_id, event.kind)
        source = _source(db, ref, quote.get('content'), quote.get('original_chars'))
        if index == 0:
            offset = job.get('result_offset', source['included_length'])
        else:
            snapshot = snapshots.get(event_id)
            if not snapshot:
                _changed()
            offset = snapshot['offset']
        results.append({'source_type': kind, 'source_id': source_id, 'source': source,
                        'observed_offset': offset,
                        'additional_read_pending_proof': offset > source['included_length']})
    return results


def read_observation_sources(db, request, reads):
    """Pin structured read observations in this exact provider request.

    These descriptors prove an attempted send only. Consumers may use them
    after the enclosing provider receipt is acknowledged.
    """
    import json
    observed = {item.call_id: item for item in request.observations}
    if len(observed) != len(request.observations):
        _changed()
    out = []
    for read in reads:
        observation = observed.get(read['call_id'])
        if (observation is None or observation.state != 'completed'
                or not isinstance(observation.text, str) or len(observation.text) > 16000):
            _changed()
        try:
            value = json.loads(observation.text)
        except (ValueError, TypeError):
            _changed()
        event = db.get(RoomEvent, read['event_id'], populate_existing=True)
        if not event or not isinstance(event.payload, dict) or not isinstance(value, dict):
            _changed()
        full = str(event.payload.get('content') or event.payload.get('message') or event.payload.get('status') or '')
        offset = value.get('offset')
        content = value.get('content')
        total = value.get('total_chars')
        if (type(offset) is not int or offset < 0 or not isinstance(content, str)
                or type(total) is not int or total < offset + len(content)
                or total > len(full) or content != full[offset:offset + len(content)]
                or value.get('digest') != _digest(full[:total])
                or value.get('next_offset') != (offset + len(content) if offset + len(content) < total else None)):
            _changed()
        out.append({'source_type': 'read_chunk', 'source_id': read['event_id'],
                    'call_id': read['call_id'], 'offset': offset, 'length': len(content),
                    'digest': _digest(content), 'total_length': total,
                    'total_digest': value['digest'], 'observation_digest': _digest(observation.text),
                    'inherited': bool(read.get('inherited'))})
    return out


def require_original_receipt(db, snapshot, failure):
    """Require a contemporaneous, acknowledged sender delivery of the snapshot."""
    from app.models.teammate import TeammateDeliveryReceipt
    receipts = db.query(TeammateDeliveryReceipt).filter_by(
        owner_id=snapshot['owner_id'], root_room_id=snapshot['room_id'],
        root_request_event_id=snapshot['original_root_id'], execution_room_id=snapshot['room_id'],
        agent_id=snapshot['agent_id'], job_id=snapshot['handoff_id'], job_kind='teammate',
        phase='return', state='acknowledged').all()
    expected = {(item['source_type'], item['source_id'], item['event_id']): item
                for item in snapshot['sources']}
    for receipt in receipts:
        if (receipt.acknowledged_at is None or failure.created_at is None
                or receipt.acknowledged_at.replace(tzinfo=None) > failure.created_at.replace(tzinfo=None)
                or not isinstance(receipt.sources_json, list)):
            continue
        actual = {}
        for entry in receipt.sources_json:
            if not isinstance(entry, dict) or entry.get('source_type') == 'read_chunk':
                continue
            source = entry.get('source')
            ref = source.get('ref') if isinstance(source, dict) else None
            if not isinstance(ref, dict):
                break
            kind = 'direct' if entry.get('source_type') == 'teammate_direct' else entry.get('source_type')
            key = (kind, entry.get('source_id'), ref.get('event_id'))
            if key in actual:
                break
            actual[key] = (source, ref)
        else:
            if set(actual) != set(expected):
                continue
            if all(source.get('full_length') == expected[key]['length']
                    and source.get('full_digest') == expected[key]['digest']
                    and all(ref.get(field) == expected[key][field] for field in ('event_id', 'seq', 'actor_id', 'kind'))
                    and ref.get('room_id') == snapshot['room_id'] and ref.get('actor_type') == 'agent'
                    for key, (source, ref) in actual.items()):
                return
    _changed()


def verified_receipt_sources(db, receipts, *, inherited_sources=()):
    """Validate acknowledged quote and read-chunk receipts; return content-free ranges.

    Caller must separately prove the receipts belong to an authorized job and
    that every relevant job/receipt has been enumerated. This helper is not an
    authorization decision and is not a historical receipt synthesizer.
    """
    grouped = {}
    chunks = {}
    inherited = {item['event_id']: item for item in inherited_sources}
    for receipt in receipts:
        if receipt.state != 'acknowledged' or receipt.acknowledged_at is None or not isinstance(receipt.sources_json, list):
            _changed()
        for entry in receipt.sources_json:
            if not isinstance(entry, dict):
                _changed()
            if entry.get('source_type') == 'read_chunk':
                event_id = entry.get('source_id')
                if entry.get('inherited'):
                    item = inherited.get(event_id)
                    event = db.get(RoomEvent, event_id, populate_existing=True) if item else None
                    payload = event.payload if event and isinstance(event.payload, dict) else {}
                    content = str(payload.get('content') or payload.get('message') or payload.get('status') or '')
                    offset, length = entry.get('offset'), entry.get('length')
                    if (not item or type(offset) is not int or type(length) is not int
                            or offset < 0 or length <= 0 or offset + length > item['length']
                            or entry.get('total_length') != item['length'] or entry.get('total_digest') != item['digest']
                            or _digest(content[:item['length']]) != item['digest']
                            or entry.get('digest') != _digest(content[offset:offset + length])):
                        _changed()
                    continue
                chunks.setdefault(event_id, []).append(entry)
                continue
            source = entry.get('source')
            if source is None:
                continue
            ref = source.get('ref') if isinstance(source, dict) else None
            if not isinstance(ref, dict):
                _changed()
            event = db.get(RoomEvent, ref.get('event_id'), populate_existing=True)
            if (not event or not isinstance(event.payload, dict) or any(
                    getattr(event, key) != ref.get(key) for key in
                    ('room_id', 'event_id', 'seq', 'actor_type', 'actor_id', 'kind'))):
                _changed()
            full = str(event.payload.get('content') or event.payload.get('message') or event.payload.get('status') or '')
            length = source.get('included_length')
            if (type(length) is not int or not 0 <= length <= len(full)
                    or source.get('full_length') != len(full) or source.get('full_digest') != _digest(full)
                    or source.get('included_digest') != _digest(full[:length])):
                _changed()
            key = event.event_id
            existing = grouped.get(key)
            if existing and (existing['full_digest'] != _digest(full) or existing['ref'] != ref):
                _changed()
            if not existing:
                grouped[key] = {'ref': ref, 'full_digest': _digest(full), 'full_length': len(full),
                                'included_length': length, 'claimed_offset': length}
            else:
                existing['included_length'] = max(existing['included_length'], length)
            offset = entry.get('observed_offset', length)
            if type(offset) is not int or not length <= offset <= len(full):
                _changed()
            grouped[key]['claimed_offset'] = max(grouped[key]['claimed_offset'], offset)
    for event_id, rows in chunks.items():
        item = grouped.get(event_id)
        if item is None:
            _changed()
        event = db.get(RoomEvent, event_id, populate_existing=True)
        full = str(event.payload.get('content') or event.payload.get('message') or event.payload.get('status') or '')
        intervals = []
        for row in rows:
            offset, length, total = row.get('offset'), row.get('length'), row.get('total_length')
            if (type(offset) is not int or type(length) is not int or type(total) is not int
                    or offset < 0 or length <= 0 or offset + length > total or total > len(full)
                    or row.get('digest') != _digest(full[offset:offset + length])
                    or row.get('total_digest') != _digest(full[:total])
                    or not isinstance(row.get('observation_digest'), str) or len(row['observation_digest']) != 64):
                _changed()
            intervals.append((offset, offset + length))
        cursor = item['included_length']
        for start, end in sorted(intervals):
            if start > cursor:
                break
            cursor = max(cursor, end)
        item['included_length'] = cursor
    for item in grouped.values():
        if item['included_length'] < item['claimed_offset']:
            _changed()
    return grouped


def sources_for_root(db, owner_id, room_id, root_id, failure):
    """Return only root sender-visible, proven child excerpts from one attempt.

    Every descendant is validated, but foreign-room descendant text is never
    promoted into the sender's source list. Unsettled or unproven jobs fail closed.
    """
    from app.models.bot_batch import BotBatch
    from app.models.bot_message import BotMessage
    from app.models.collaboration import RoomTrigger
    from app.models.models import Conversation
    from app.models.teammate import TeammateDeliveryReceipt, TeammateHandoff
    from app.services import bot_batches, bot_messages, teammates

    root = db.get(RoomEvent, root_id, populate_existing=True)
    trigger = db.get(RoomTrigger, root_id, populate_existing=True)
    room = db.get(Conversation, room_id, populate_existing=True)
    if (not root or not trigger or not room or room.user_id != owner_id
            or root.room_id != room_id or root.actor_type != 'user' or root.actor_id != owner_id
            or trigger.room_id != room_id or len(trigger.selected_agents or []) != 1
            or not failure or failure.room_id != room_id or failure.seq <= root.seq):
        _changed()
    root_agent = trigger.selected_agents[0]
    if (failure.actor_type != 'agent' or failure.actor_id != root_agent or failure.kind != 'failure'
            or not isinstance(failure.payload, dict) or failure.payload.get('trigger_event_id') != root_id):
        _changed()
    hands = db.query(TeammateHandoff).filter_by(root_request_event_id=root_id).all()
    messages = db.query(BotMessage).filter_by(root_request_event_id=root_id).all()
    batches = db.query(BotBatch).filter_by(root_request_event_id=root_id).all()
    if len(hands) + len(messages) + len(batches) > 128:
        _changed()
    handmap = {row.handoff_id: row for row in hands}
    msgmap = {row.message_id: row for row in messages}
    batchmap = {row.batch_id: row for row in batches}
    if len(set(handmap) | set(msgmap) | set(batchmap)) != len(hands) + len(messages) + len(batches):
        _changed()

    def parent_ok(sender_room, sender_agent, kind, identity):
        if identity is None:
            return kind is None and (sender_room, sender_agent) == (room_id, root_agent)
        parent = handmap.get(identity) if kind == 'teammate' else msgmap.get(identity) if kind == 'bot_message' else None
        if parent is None:
            return False
        expected_room = parent.room_id if kind == 'teammate' else parent.recipient_room_id
        return (sender_room, sender_agent) == (expected_room, parent.recipient_agent_id)

    def depth_ok(row, kind):
        seen = set()
        current = row
        current_kind = kind
        while current is not None:
            identity = current.handoff_id if current_kind == 'teammate' else current.message_id
            if identity in seen or len(seen) >= teammates.MAX_DEPTH:
                return False
            seen.add(identity)
            if type(current.depth) is not int or not 1 <= current.depth <= teammates.MAX_DEPTH:
                return False
            parent_id = current.parent_handoff_id if current_kind == 'teammate' else current.parent_job_id
            parent_kind = ('teammate' if parent_id else None) if current_kind == 'teammate' else current.parent_kind
            if parent_id is None:
                return parent_kind is None and current.depth == 1
            parent = handmap.get(parent_id) if parent_kind == 'teammate' else msgmap.get(parent_id) if parent_kind == 'bot_message' else None
            if parent is None or current.depth != parent.depth + 1:
                return False
            current, current_kind = parent, parent_kind
        return False

    for row in hands:
        parent = handmap.get(row.parent_handoff_id) if row.parent_handoff_id else None
        if (row.owner_id != owner_id or row.room_id != room_id or row.state != 'completed'
                or row.reply_state not in {'delivered', 'failed'} or not row.return_event_id
                or not depth_ok(row, 'teammate')
                or (parent is None and row.parent_handoff_id is not None)
                or (parent is None and row.sender_agent_id != root_agent)
                or (parent is not None and row.sender_agent_id != parent.recipient_agent_id)):
            _changed()
    for row in messages:
        sender_room = db.get(Conversation, row.sender_room_id)
        recipient_room = db.get(Conversation, row.recipient_room_id)
        if (row.owner_id != owner_id or row.root_room_id != room_id or not sender_room or not recipient_room
                or sender_room.user_id != owner_id or recipient_room.user_id != owner_id
                or not parent_ok(row.sender_room_id, row.sender_agent_id, row.parent_kind, row.parent_job_id)
                or not depth_ok(row, 'bot_message')
                or row.state != 'completed' or row.reply_state not in {'delivered', 'failed'}
                or row.batch_id and row.batch_id not in batchmap):
            _changed()
    for row in batches:
        if (row.owner_id != owner_id or row.root_room_id != room_id
                or not parent_ok(row.sender_room_id, row.sender_agent_id, row.parent_kind, row.parent_job_id)
                or row.state != 'completed' or row.reply_state not in {'delivered', 'failed'}
                or not row.return_event_id):
            _changed()
        members = bot_batches.members(db, row)
        if any(member.batch_id != row.batch_id or member.parent_job_id != row.parent_job_id
               or member.parent_kind != row.parent_kind or member.root_room_id != room_id for member in members):
            _changed()
    jobs = {('teammate', row.handoff_id): row for row in hands}
    jobs.update({('bot_message', row.message_id): row for row in messages if not row.batch_id})
    jobs.update({('bot_batch', row.batch_id): row for row in batches})
    receipts = db.query(TeammateDeliveryReceipt).filter_by(root_request_event_id=root_id).all()
    by_job = {}
    attempted_by_job = {}
    inherited_context = root.payload.get('teammate_retry_context') if isinstance(root.payload, dict) else None
    inherited_sources = inherited_context.get('sources', []) if isinstance(inherited_context, dict) else []
    if not isinstance(inherited_sources, list):
        _changed()
    for receipt in receipts:
        if receipt.job_kind == 'retry':
            if (receipt.job_id != root_id or receipt.owner_id != owner_id or receipt.root_room_id != room_id
                    or receipt.execution_room_id != room_id or receipt.agent_id != root_agent
                    or receipt.phase != 'response' or receipt.state not in {'attempted', 'acknowledged'}):
                _changed()
            continue
        key = (receipt.job_kind, receipt.job_id)
        row = jobs.get(key)
        sender_room = row.room_id if receipt.job_kind == 'teammate' and row else getattr(row, 'sender_room_id', None)
        sender_agent = getattr(row, 'sender_agent_id', None)
        if (not row or receipt.owner_id != owner_id or receipt.root_room_id != room_id
                or receipt.execution_room_id != sender_room or receipt.agent_id != sender_agent
                or receipt.phase != 'return' or receipt.state not in {'attempted', 'acknowledged'}
                or failure.created_at is None or receipt.created_at is None
                or receipt.created_at.replace(tzinfo=None) > failure.created_at.replace(tzinfo=None)
                or receipt.state == 'acknowledged' and (not receipt.acknowledged_at
                    or receipt.acknowledged_at.replace(tzinfo=None) > failure.created_at.replace(tzinfo=None))):
            _changed()
        (by_job if receipt.state == 'acknowledged' else attempted_by_job).setdefault(key, []).append(receipt)
    result = {}
    for key, row in jobs.items():
        relevant = by_job.get(key)
        if not relevant:
            _changed()
        if key[0] == 'teammate':
            expected_slots = {('teammate_direct', row.handoff_id): row.result_event_id}
            for item in teammates._nested_sources(db, row):
                slot = (item['source_type'], item['source_id'])
                if slot in expected_slots:
                    _changed()
                expected_slots[slot] = item['event_id']
        elif key[0] == 'bot_message':
            effective = bot_messages.effective_result(db, row)
            expected_slots = {('bot_message', row.message_id): effective.event_id if effective else None}
        else:
            expected_slots = {}
            member_indices = {}
            for member in bot_batches.members(db, row):
                state, code, event = bot_batches.member_value(db, member)
                if state != 'completed' or code or not event:
                    _changed()
                expected_slots[('bot_batch_member', member.message_id)] = event.event_id
                member_indices[member.message_id] = member.batch_index
        expected = set(expected_slots.values())
        for receipt in relevant:
            slots = {}
            for entry in receipt.sources_json:
                if not isinstance(entry, dict):
                    _changed()
                if entry.get('source_type') == 'read_chunk':
                    if not entry.get('inherited') and entry.get('source_id') not in expected:
                        _changed()
                    continue
                slot = (entry.get('source_type'), entry.get('source_id'))
                source = entry.get('source')
                ref = source.get('ref') if isinstance(source, dict) else None
                if slot in slots or not isinstance(ref, dict):
                    _changed()
                slots[slot] = ref.get('event_id')
                if key[0] == 'bot_batch' and (entry.get('batch_id') != row.batch_id
                        or entry.get('member_index') != member_indices.get(entry.get('source_id'))):
                    _changed()
            if slots != expected_slots:
                _changed()
        visible = verified_receipt_sources(db, relevant, inherited_sources=inherited_sources)
        if None in expected or not expected or set(visible) != expected:
            _changed()
        # A later failed provider call cannot invalidate already acknowledged
        # source quotes. Ignore only identical quotes and reads of inherited
        # sources; any new unacknowledged child data remains unproven.
        from types import SimpleNamespace
        canonical_quotes = [[{k: entry.get(k) for k in ('source_type', 'source_id', 'source',
                             'batch_id', 'member_index', 'observed_offset')}
                             for entry in receipt.sources_json if entry.get('source_type') != 'read_chunk']
                            for receipt in relevant]
        for pending in attempted_by_job.get(key, []):
            entries = pending.sources_json
            if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
                _changed()
            quotes = [{k: entry.get(k) for k in ('source_type', 'source_id', 'source',
                       'batch_id', 'member_index', 'observed_offset')}
                      for entry in entries if entry.get('source_type') != 'read_chunk']
            chunks = [entry for entry in entries if entry.get('source_type') == 'read_chunk']
            if quotes not in canonical_quotes or any(not entry.get('inherited') for entry in chunks):
                _changed()
            probe = SimpleNamespace(state='acknowledged', acknowledged_at=pending.created_at, sources_json=chunks)
            verified_receipt_sources(db, [probe], inherited_sources=inherited_sources)
        sender_room = row.room_id if key[0] == 'teammate' else row.sender_room_id
        if sender_room != room_id or row.sender_agent_id != root_agent:
            continue
        for event_id, source in visible.items():
            event = db.get(RoomEvent, event_id)
            length = source['included_length']
            content = str(event.payload.get('content') or event.payload.get('message') or event.payload.get('status') or '')
            item = {'source_type': key[0], 'source_id': key[1], 'event_id': event_id,
                    'seq': event.seq, 'actor_id': event.actor_id, 'kind': event.kind,
                    'length': length, 'digest': _digest(content[:length])}
            if event_id in result and result[event_id] != item:
                _changed()
            result[event_id] = item
    if len(result) > 64:
        _changed()
    return sorted(result.values(), key=lambda item: (item['seq'], item['event_id']))


def safe_activity_ids_for_root(db, owner_id, room_id, root_id, failure):
    """Return only exact, server-created Bot delivery activities for a proven tree."""
    from app.models.bot_batch import BotBatch
    from app.models.bot_message import BotMessage
    from app.models.teammate import TeammateHandoff
    from app.models.collaboration import RoomTrigger
    from app.services import bot_batches, bot_messages
    sources_for_root(db, owner_id, room_id, root_id, failure)
    allowed = set()

    def check(event_id, event_room, category, key, metadata, internal_key):
        event = db.get(RoomEvent, event_id, populate_existing=True) if event_id else None
        payload = event.payload if event and isinstance(event.payload, dict) else {}
        if (not event or event.room_id != event_room or event.kind != 'activity'
                or event.actor_type != 'system' or event.actor_id is not None
                or event.idempotency_key != internal_key or payload.get('category') != category
                or payload.get('trigger_event_id') != root_id or payload.get(key) != metadata):
            _changed()
        allowed.add(event.event_id)
        return payload

    for row in db.query(BotMessage).filter_by(root_request_event_id=root_id).all():
        payload = check(row.delivery_event_id, row.recipient_room_id, 'bot_message', 'bot_message',
            bot_messages.metadata(row, 'delivery'), 'bot-message-delivery:' + row.message_id)
        expected_keys = {'category', 'status', 'content', 'trigger_event_id', 'bot_message', 'source_ref'}
        if row.batch_id:
            expected_keys.add('bot_batch')
        if (set(payload) != expected_keys or payload.get('status') != 'pending' or payload.get('content') != row.content
                or payload.get('source_ref') != bot_messages.event_ref(db, row.request_event_id)):
            _changed()
        if row.batch_id:
            batch = db.get(BotBatch, row.batch_id)
            if not batch or payload.get('bot_batch') != bot_batches.metadata(batch, 'branch_delivery', row.batch_index):
                _changed()
        if row.return_delivery_event_id:
            payload = check(row.return_delivery_event_id, row.sender_room_id, 'bot_message', 'bot_message',
                bot_messages.metadata(row, 'return_delivery'), 'bot-message-return-delivery:' + row.message_id)
            if (set(payload) != {'category', 'status', 'trigger_event_id', 'bot_message', 'result_ref', 'failure_code'}
                    or payload.get('result_ref') != bot_messages.event_ref(db, row.result_event_id)
                    or payload.get('status') != row.state or payload.get('failure_code') != row.failure_code):
                _changed()
        elif not row.batch_id:
            _changed()
    for row in db.query(BotBatch).filter_by(root_request_event_id=root_id).all():
        payload = check(row.return_delivery_event_id, row.sender_room_id, 'bot_batch', 'bot_batch',
            bot_batches.metadata(row, 'return_delivery'), 'bot-batch-return-delivery:' + row.batch_id)
        if set(payload) != {'category', 'status', 'trigger_event_id', 'bot_batch'} or payload.get('status') != row.state:
            _changed()
    import json
    waiting = db.query(RoomEvent).filter(
        RoomEvent.kind == 'activity', RoomEvent.payload['trigger_event_id'].as_string() == root_id,
        RoomEvent.payload['category'].as_string().in_(('bot_message_waiting', 'bot_batch_waiting'))).all()
    for event in waiting:
        payload = event.payload if isinstance(event.payload, dict) else {}
        category = payload.get('category')
        allowed_keys = {'status', 'category', 'trigger_event_id', 'round', 'generation', 'model_use',
                        'message_waiting' if category == 'bot_message_waiting' else 'batch_waiting'}
        allowed_keys |= {'bot_message', 'bot_batch', 'teammate_handoff'}
        if (event.actor_type != 'agent' or type(event.actor_id) is not int or payload.get('status') != 'passed'
                or set(payload) - allowed_keys or not isinstance(payload.get('model_use'), dict)
                or type(payload.get('generation')) is not int or payload['generation'] < 0
                or type(payload.get('round')) is not int or not 1 <= payload['round'] <= 4):
            _changed()
        entries = [payload.get('message_waiting')] if category == 'bot_message_waiting' else payload.get('batch_waiting')
        if not isinstance(entries, list) or not entries:
            _changed()
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get('call_id'), str) or not item['call_id']:
                _changed()
            if category == 'bot_message_waiting':
                if set(item) != {'call_id', 'message_id', 'request_ref'}:
                    _changed()
                row = db.get(BotMessage, item['message_id'])
            else:
                if set(item) != {'call_id', 'family', 'batch_id', 'request_ref'} or item.get('family') != 'message_bots':
                    _changed()
                row = db.get(BotBatch, item['batch_id'])
            if (not row or row.owner_id != owner_id or row.root_room_id != room_id
                    or row.root_request_event_id != root_id or row.sender_room_id != event.room_id
                    or row.sender_agent_id != event.actor_id
                    or item.get('request_ref') != bot_messages.event_ref(db, row.request_event_id)):
                _changed()
            if row.parent_job_id is None:
                root_trigger = db.get(RoomTrigger, root_id)
                if event.room_id != room_id or not root_trigger or root_trigger.selected_agents != [event.actor_id]:
                    _changed()
            else:
                parent_cls = TeammateHandoff if row.parent_kind == 'teammate' else BotMessage if row.parent_kind == 'bot_message' else None
                parent = db.get(parent_cls, row.parent_job_id) if parent_cls else None
                if not parent or parent.return_event_id != event.event_id:
                    _changed()
            delivery_key = sha256(json.dumps([root_id, event.room_id, event.actor_id,
                payload['generation'], item['call_id']], separators=(',', ':')).encode()).hexdigest()
            if row.delivery_key != delivery_key:
                _changed()
        allowed.add(event.event_id)
    return allowed
