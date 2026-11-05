import { describe, expect, it } from 'vitest'
import type { RoomEvent } from '../api/events'
import { peerReplies, replyExcerpt } from './peerReply'

export const sourceId = '11111111-1111-4111-8111-111111111111'
export const secondId = '22222222-2222-4222-8222-222222222222'
export const source: RoomEvent = { event_id: sourceId, room_id: 1, seq: 1, actor_type: 'agent', actor_id: 8,
  actor_name_snapshot: 'Original name', kind: 'message', payload: { content: '<think>private reasoning</think>\nVisible peer' },
  device_id: null, created_at: '', idempotency_key: null }
export const answer: RoomEvent = { ...source, event_id: '33333333-3333-4333-8333-333333333333', seq: 3, actor_id: 9,
  payload: { content: 'answer', reply_provenance: { version: 1, watermark_seq: 2, declaration_state: 'declared',
    provided_peer_event_ids: [sourceId, secondId], reply_to_event_ids: [sourceId] } } }

describe('explicit canonical peer replies', () => {
  it('resolves immutable identity and snapshot while hiding reasoning from excerpts', () => {
    expect(peerReplies(answer, [source])?.references[0].source).toBe(source)
    expect(replyExcerpt(String(source.payload.content))).toBe('Visible peer')
    expect(replyExcerpt('**Long** ' + 'a'.repeat(150))).toHaveLength(121)
  })
  it('never backfills legacy, availability-only, malformed or unsupported declarations', () => {
    expect(peerReplies({ ...answer, payload: { content: 'Original name said' } }, [source])).toBeNull()
    for (const changed of [{ version: 2 }, { watermark_seq: 3 }, { declaration_state: 'absent' },
      { reply_to_event_ids: [sourceId, sourceId] }, { reply_to_event_ids: [secondId], provided_peer_event_ids: [sourceId] },
      { reply_to_event_ids: ['bad'] }]) {
      expect(peerReplies({ ...answer, payload: { reply_provenance: { ...answer.payload.reply_provenance as object, ...changed } } }, [source])).toBeNull()
    }
  })
  it('does not preview foreign-room, future, self, tool or mismatched-identity messages', () => {
    for (const changed of [{ room_id: 2 }, { seq: 4 }, { actor_id: 9 }, { actor_type: 'tool' as const },
      { event_id: secondId }, { kind: 'summary' as const }]) {
      expect(peerReplies(answer, [{ ...source, ...changed }])?.references[0].source).toBeUndefined()
    }
  })
})
