import type { RoomEvent } from '../api/events'
import type { BotCreationReceipt } from '../api/botCreations'
import { canonicalEventId } from './peerReply'
export type BotCreationMetadata = { version: 1; creation_id: string; phase: 'request' | 'created'; root_room_id: number; root_request_event_id: string; source_agent_id: number }
export function botCreationMetadata(event: RoomEvent): BotCreationMetadata | null {
  const raw = event.payload.bot_creation
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const value = raw as Record<string, unknown>
  if (value.version !== 1 || !canonicalEventId(value.creation_id) || !canonicalEventId(value.root_request_event_id)
    || !Number.isSafeInteger(value.root_room_id) || value.root_room_id !== event.room_id
    || typeof value.source_agent_id !== 'number' || !Number.isSafeInteger(value.source_agent_id) || value.source_agent_id < 1
    || !['request','created'].includes(String(value.phase)) || event.kind !== 'activity' || event.payload.category !== 'bot_creation'
    || event.payload.trigger_event_id !== value.root_request_event_id
    || value.phase === 'created' && (!(typeof event.payload.created_agent_id === 'number' && Number.isSafeInteger(event.payload.created_agent_id) && event.payload.created_agent_id > 0)
      || !(typeof event.payload.created_room_id === 'number' && Number.isSafeInteger(event.payload.created_room_id) && event.payload.created_room_id > 0))
    || (value.phase === 'request' ? event.actor_type !== 'agent' || event.actor_id !== value.source_agent_id || event.payload.status !== 'requested'
      : event.actor_type !== 'system' || event.actor_id !== null || event.payload.status !== 'created')) return null
  return value as BotCreationMetadata
}
export function matchingBotCreation(event: RoomEvent, rows: BotCreationReceipt[]): BotCreationReceipt | null {
  const meta = botCreationMetadata(event)
  if (!meta) return null
  return rows.find(row => {
    const ref = meta.phase === 'request' ? row.request_ref : row.result_ref
    return row.creation_id === meta.creation_id && row.root_ref.room_id === meta.root_room_id && row.root_ref.event_id === meta.root_request_event_id
      && row.source_agent_id === meta.source_agent_id && ref.room_id === event.room_id && ref.event_id === event.event_id && ref.seq === event.seq
      && ref.actor_type === event.actor_type && ref.actor_id === event.actor_id && ref.kind === event.kind
      && (meta.phase !== 'created' || event.payload.created_agent_id === row.created_agent.id && event.payload.created_room_id === row.conversation.room_id)
  }) || null
}
export function duplicateCreationRequest(event: RoomEvent, events: RoomEvent[]): boolean {
  const meta = botCreationMetadata(event)
  return meta?.phase === 'request' && events.some(item => {
    const result = botCreationMetadata(item)
    return result?.phase === 'created' && result.creation_id === meta.creation_id && result.root_request_event_id === meta.root_request_event_id
      && result.source_agent_id === meta.source_agent_id && item.room_id === event.room_id && item.seq > event.seq
  })
}
