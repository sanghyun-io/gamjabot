import { describe, expect, it } from 'vitest'
import { validPersistenceReceipt, type BotPersistenceReceipt } from '../api/botPersistence'
import type { RoomEvent } from '../api/events'
import { botPersistenceMetadata, matchingPersistence } from './botPersistence'

const id = (n: number) => `af037000-0000-4000-8000-${String(n).padStart(12, '0')}`
const meta = { version: 1 as const, receipt_id: 'a'.repeat(64), state: 'saved' as const, operation: 'add' as const, agent_id: 8,
  root_request_event_id: id(1), content: 'Preference', memory_id: id(4), revision: 1, entry_revision: 1, effective: 'next_request' as const, history_retained: false }
const event: RoomEvent = { room_id: 501, event_id: id(2), seq: 2, actor_type: 'system', actor_id: null, kind: 'activity',
  actor_name_snapshot: 'System', device_id: null, idempotency_key: null, created_at: '2026-10-29T00:00:00Z',
  payload: { category: 'bot_persistence', status: 'saved', trigger_event_id: id(1), bot_persistence: meta } }
const receipt: BotPersistenceReceipt = { ...meta, root_ref: { room_id: 501, event_id: id(1), seq: 1, actor_type: 'user', actor_id: 1, kind: 'message' },
  result_ref: { room_id: 501, event_id: id(2), seq: 2, actor_type: 'system', actor_id: null, kind: 'activity' }, replayed: true }
describe('conversational persistence evidence', () => {
  it('accepts the exact owner and durable event receipt', () => {
    expect(validPersistenceReceipt(receipt, 501, 1)).toBe(true)
    expect(matchingPersistence(event, receipt)).toBe(true)
  })
  it('rejects another owner and any mismatched result or revision', () => {
    expect(validPersistenceReceipt(receipt, 501, 2)).toBe(false)
    expect(matchingPersistence(event, { ...receipt, revision: 2 })).toBe(false)
    expect(matchingPersistence(event, { ...receipt, result_ref: { ...receipt.result_ref, event_id: id(9) } })).toBe(false)
    expect(botPersistenceMetadata({ ...event, actor_type: 'agent', actor_id: 8 })).toBeNull()
  })
  it('keeps role settings distinct from role-category memory', () => {
    expect(validPersistenceReceipt({ ...receipt, operation: 'role' }, 501, 1)).toBe(false)
    expect(validPersistenceReceipt({ ...receipt, operation: 'role', memory_id: null, revision: null, entry_revision: null }, 501, 1)).toBe(true)
  })
})
