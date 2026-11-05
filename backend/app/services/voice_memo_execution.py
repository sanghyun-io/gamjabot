"""Execute one reviewed speech proposal via the Core-owned speech channel."""
import asyncio
import base64
import hashlib
import json
import re
from datetime import datetime, timezone

from app.services.voice_memo_artifacts import MAX_BYTES, MAX_DURATION_MS, VoiceMemoUnavailable


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


async def execute(bridge, action, ref, prepared, review, cancel, active):
    """Caller has consumed a once grant. Never replay uncertain issue/start/read."""
    if (bridge is None or not getattr(bridge, 'available', False) or ref.scope != 'once'
            or ref.grant_id is None or ref.run_generation is None or not isinstance(ref.expires_at, datetime)):
        raise VoiceMemoUnavailable()
    proposer = review.authority.proposer
    if not proposer.owner_epoch or not re.fullmatch(r'[0-9a-f]{64}', proposer.owner_epoch):
        raise VoiceMemoUnavailable()
    expiry = ref.expires_at
    expiry = expiry.replace(tzinfo=timezone.utc) if expiry.tzinfo is None else expiry.astimezone(timezone.utc)
    args = prepared.raw_arguments
    raw_text = args['text'].encode('utf-8')
    # Use the same shape persisted by the existing review snapshot contract.
    from app.services.review_snapshot import snapshot_value
    review_dict = snapshot_value(review)
    review_canonical = canonical(review_dict)
    action_canonical = canonical({'family':action.family, 'workflow_id':action.workflow_id, 'arguments':args})
    if hashlib.sha256(action_canonical.encode()).hexdigest() != prepared.payload_digest:
        raise VoiceMemoUnavailable()
    binding = {'owner_id':proposer.owner_id, 'owner_epoch':proposer.owner_epoch, 'core_epoch':bridge.core_epoch,
               'room_id':action.room_id, 'agent_id':action.agent_id, 'root_event_id':action.trigger_event_id,
               'generation':ref.run_generation, 'action_id':action.action_id, 'review_id':ref.review_id,
               'grant_id':ref.grant_id, 'lease_expires_at':expiry.isoformat().replace('+00:00','Z'),
               'payload_digest':prepared.payload_digest,
               'review_sha256':hashlib.sha256(review_canonical.encode()).hexdigest(),
               'text_sha256':hashlib.sha256(raw_text).hexdigest(), 'text_bytes':len(raw_text),
               'voice_id':args['voice_id'], 'locale':args['locale'], 'rate_milli':args['rate_milli']}
    if action.execution_binding is not None:
        binding['execution_binding'] = action.execution_binding
    payload = {'version':1, 'binding':binding, 'arguments':args, 'text':args['text'],
               'review':review_dict, 'review_canonical':review_canonical,
               'action_payload_canonical':action_canonical}
    cancel_payload = {'version':1, **{k:binding[k] for k in ('owner_id','owner_epoch','core_epoch','room_id','agent_id','root_event_id','generation','action_id')}}
    try:
        issued = await asyncio.wait_for(bridge.speech_issue(payload), 5)
        nonce = issued.get('nonce') if isinstance(issued, dict) and issued.get('state') == 'issued' else None
        if not isinstance(nonce, str) or not re.fullmatch(r'[0-9a-f]{64}', nonce): raise VoiceMemoUnavailable()
        if cancel.is_set() or not active(): raise VoiceMemoUnavailable()
        response = await asyncio.wait_for(bridge.speech_start({**payload, 'nonce':nonce}), 305)
        result = response.get('result') if isinstance(response, dict) and response.get('state') == 'completed' else None
        if not isinstance(result, dict): raise VoiceMemoUnavailable()
        size, duration = result.get('size_bytes'), result.get('duration_ms')
        artifact = result.get('artifact_id')
        if (type(size) is not int or not 1 <= size <= MAX_BYTES or type(duration) is not int
                or not 1 <= duration <= MAX_DURATION_MS or result.get('media_type') != 'audio/wav'
                or not isinstance(artifact, str) or not re.fullmatch(r'[0-9a-f]{32}', artifact)
                or not isinstance(result.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', result['sha256'])
                or result.get('text_sha256') != binding['text_sha256']
                or any(result.get(k) != args[k] for k in ('voice_id','locale','rate_milli'))):
            raise VoiceMemoUnavailable()
        read_binding = {k:binding[k] for k in ('owner_id','owner_epoch','core_epoch','room_id','agent_id','root_event_id','action_id')}
        data = bytearray()
        while len(data) < size:
            if cancel.is_set() or not active(): raise VoiceMemoUnavailable()
            response = await asyncio.wait_for(bridge.speech_read({'version':1, **read_binding, 'artifact_id':artifact, 'offset':len(data)}), 5)
            chunk = response.get('result') if isinstance(response, dict) and response.get('state') == 'completed' else None
            if not isinstance(chunk, dict) or chunk.get('offset') != len(data): raise VoiceMemoUnavailable()
            try: part = base64.b64decode(chunk.get('data_base64'), validate=True)
            except (TypeError, ValueError): raise VoiceMemoUnavailable() from None
            if not 1 <= len(part) <= 32768 or len(data) + len(part) > size: raise VoiceMemoUnavailable()
            data.extend(part)
            if type(chunk.get('eof')) is not bool or chunk['eof'] != (len(data) == size): raise VoiceMemoUnavailable()
        if hashlib.sha256(data).hexdigest() != result['sha256'] or cancel.is_set() or not active():
            raise VoiceMemoUnavailable()
        meta = {'version':1, 'owner_id':proposer.owner_id, 'room_id':action.room_id, 'agent_id':action.agent_id,
                'trigger_event_id':action.trigger_event_id, 'action_id':action.action_id,
                'review_id':ref.review_id, 'grant_id':ref.grant_id, 'generation':ref.run_generation,
                'media_type':'audio/wav', 'size_bytes':size, 'sha256':result['sha256'], 'duration_ms':duration,
                'transcript':args['text'], 'text_sha256':binding['text_sha256'], 'voice_id':args['voice_id'],
                'locale':args['locale'], 'rate_milli':args['rate_milli'], 'deleted':False}
        return meta, bytes(data)
    finally:
        try:
            cleanup = await asyncio.wait_for(bridge.speech_cancel(cancel_payload), 3)
            if not isinstance(cleanup, dict) or cleanup.get('state') != 'completed':
                raise VoiceMemoUnavailable()
        except Exception:
            raise VoiceMemoUnavailable() from None
