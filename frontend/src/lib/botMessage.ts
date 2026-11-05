import type { RoomEvent } from '../api/events'
import type { BotMessage, BotMessageRef } from '../api/botMessages'
import { canonicalEventId } from './peerReply'
export type BotMessagePhase = 'request' | 'delivery' | 'response' | 'return_delivery' | 'return'
export type BotMessageMetadata = {
  message_id: string; phase: BotMessagePhase; sender_agent_id: number; recipient_agent_id: number
  root_room_id: number; root_request_event_id: string
}
export function botMessageMetadata(event: RoomEvent): BotMessageMetadata | null {
  const value = event.payload.bot_message
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const meta = value as Record<string, unknown>
  if (meta.version !== 1 || !canonicalEventId(meta.message_id) || !canonicalEventId(meta.root_request_event_id)
    || event.payload.trigger_event_id !== meta.root_request_event_id
    || !['request', 'delivery', 'response', 'return_delivery', 'return'].includes(String(meta.phase))
    || typeof meta.sender_agent_id !== 'number' || !Number.isSafeInteger(meta.sender_agent_id) || meta.sender_agent_id < 1
    || typeof meta.recipient_agent_id !== 'number' || !Number.isSafeInteger(meta.recipient_agent_id) || meta.recipient_agent_id < 1
    || meta.sender_agent_id === meta.recipient_agent_id || typeof meta.root_room_id !== 'number' || !Number.isSafeInteger(meta.root_room_id) || meta.root_room_id < 1) return null
  const system = meta.phase === 'delivery' || meta.phase === 'return_delivery'
  if (event.actor_type !== (system ? 'system' : 'agent') || event.actor_id !== (system ? null : meta.phase === 'response' ? meta.recipient_agent_id : meta.sender_agent_id)
    || system && (event.kind !== 'activity' || event.payload.category !== 'bot_message')
    || !system && !['message', 'failure', 'activity'].includes(event.kind)) return null
  return meta as BotMessageMetadata
}
export function botMessageReference(row: BotMessage, phase: BotMessagePhase): BotMessageRef | null {
  return phase === 'request' ? row.request_ref : phase === 'delivery' ? row.delivery_ref : phase === 'response' ? row.result_ref
    : phase === 'return_delivery' ? row.return_delivery_ref : row.return_ref
}
export function matchingBotMessage(event: RoomEvent, rows: BotMessage[]): BotMessage | null {
  const meta = botMessageMetadata(event)
  if (!meta) return null
  return rows.find(row => {
    const ref = botMessageReference(row, meta.phase)
    return row.message_id === meta.message_id && row.root_ref.room_id === meta.root_room_id && row.root_request_event_id === meta.root_request_event_id
      && row.sender_agent_id === meta.sender_agent_id && row.recipient_agent_id === meta.recipient_agent_id && !!ref
      && ref.room_id === event.room_id && ref.event_id === event.event_id && ref.seq === event.seq
      && ref.actor_type === event.actor_type && ref.actor_id === event.actor_id && ref.kind === event.kind
      && (meta.phase !== 'request' || row.content === event.payload.content)
  }) || null
}
