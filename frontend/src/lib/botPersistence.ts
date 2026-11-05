import type { RoomEvent } from '../api/events'
import type { BotPersistenceReceipt } from '../api/botPersistence'
import { canonicalEventId } from './peerReply'
export type BotPersistenceMetadata = { version: 1; receipt_id: string; state: 'saved'; operation: 'add'|'edit'|'delete'|'role'; agent_id: number; root_request_event_id: string; content: string; revision: number | null; memory_id: string | null; entry_revision: number | null; effective: 'next_request'; history_retained: boolean }
export function botPersistenceMetadata(event: RoomEvent): BotPersistenceMetadata | null {
  const raw = event.payload.bot_persistence
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const v = raw as Record<string, unknown>
  if (event.kind !== 'activity' || event.actor_type !== 'system' || event.actor_id !== null || event.payload.category !== 'bot_persistence' || event.payload.status !== 'saved'
    || v.version !== 1 || v.state !== 'saved' || !(typeof v.receipt_id === 'string' && /^[0-9a-f]{64}$/.test(v.receipt_id)) || !canonicalEventId(v.root_request_event_id)
    || event.payload.trigger_event_id !== v.root_request_event_id || typeof v.agent_id !== 'number' || !Number.isSafeInteger(v.agent_id) || v.agent_id < 1
    || !['add','edit','delete','role'].includes(String(v.operation)) || typeof v.content !== 'string' || !v.content.trim()
    || v.effective !== 'next_request' || v.history_retained !== ['edit','delete'].includes(String(v.operation))
    || (v.operation === 'role' ? v.memory_id !== null || v.revision !== null || v.entry_revision !== null
      : !canonicalEventId(v.memory_id) || typeof v.revision !== 'number' || !Number.isSafeInteger(v.revision) || v.revision < 1 || typeof v.entry_revision !== 'number' || !Number.isSafeInteger(v.entry_revision) || v.entry_revision < 1)) return null
  return v as BotPersistenceMetadata
}
export function matchingPersistence(event: RoomEvent, receipt: BotPersistenceReceipt): boolean {
  const m = botPersistenceMetadata(event), ref = receipt.result_ref
  return !!m && receipt.receipt_id === m.receipt_id && receipt.operation === m.operation && receipt.agent_id === m.agent_id
    && receipt.root_request_event_id === m.root_request_event_id && receipt.root_ref.event_id === m.root_request_event_id && receipt.content === m.content && receipt.revision === m.revision
    && receipt.memory_id === m.memory_id && receipt.entry_revision === m.entry_revision && receipt.effective === m.effective && receipt.history_retained === m.history_retained
    && ref.room_id === event.room_id && ref.event_id === event.event_id && ref.seq === event.seq && ref.actor_type === event.actor_type && ref.actor_id === event.actor_id && ref.kind === event.kind
}
