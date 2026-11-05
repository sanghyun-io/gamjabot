import { expect, it } from 'vitest'
import { resultHttpUrl, resultSource, resultOpinionCandidates } from './resultSelection'
import type { RoomEvent } from '../api/events'
import type { ResultDetail } from '../api/roomResults'
const id = '11111111-1111-4111-8111-111111111111'
const source = { event_id: id, room_id: 1, seq: 1, actor_type: 'agent', actor_id: 8, kind: 'message', payload: { content: 'Answer' }, device_id: null, actor_name_snapshot: 'Author', created_at: '2026-10-16T00:00:00Z', idempotency_key: null } as RoomEvent
it('offers canonical answers and same-room managed PNGs, never user messages or foreign references', () => {
  expect(resultSource(source, 1)).toBe('text')
  expect(resultSource({ ...source, actor_type: 'user' }, 1)).toBeNull()
  expect(resultSource(source, 2)).toBeNull()
  expect(resultSource({ ...source, payload: { content: '<think>Only hidden</think>' } }, 1)).toBeNull()
  const screenshot = { ...source, actor_type: 'tool', kind: 'tool_result', payload: { state: 'completed', action_id: id, screenshot_ref: `rooms/1/screenshots/${id}.png` } } as RoomEvent
  expect(resultSource(screenshot, 1)).toBe('file')
  expect(resultSource({ ...screenshot, payload: { ...screenshot.payload, screenshot_ref: `/tmp/${id}.png` } }, 1)).toBeNull()
})
it('rejects executable, credential, malformed and control URLs for opening', () => {
  for (const url of ['javascript:alert(1)', 'file:///etc/passwd', 'https://user:pass@example.com', 'https://example.com\\x', 'https://example.com/\n']) expect(resultHttpUrl(url)).toBeNull()
  expect(resultHttpUrl('https://example.com/a?b=1')).toBe('https://example.com/a?b=1')
})
it('offers only another Agent’s provided+declared exact source opinion and no file AI review', () => {
  const target = { room_id: 1, author_agent_id: 8, review_support: 'source_reply_only', source: { event_id: id } } as ResultDetail
  const reply = { ...source, event_id: '22222222-2222-4222-8222-222222222222', seq: 3, actor_id: 9, payload: { content: 'Review', reply_provenance: { version: 1, watermark_seq: 2, declaration_state: 'declared', provided_peer_event_ids: [id], reply_to_event_ids: [id] } } }
  expect(resultOpinionCandidates(target, [source, reply])).toEqual([reply])
  expect(resultOpinionCandidates(target, [source, { ...reply, actor_id: 8 }])).toEqual([])
  expect(resultOpinionCandidates(target, [source, { ...reply, room_id: 2 }])).toEqual([])
  expect(resultOpinionCandidates({ ...target, review_support: 'unsupported' }, [source, reply])).toEqual([])
})
