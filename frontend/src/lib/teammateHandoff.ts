import type { RoomEvent } from '../api/events'
import type { TeammateHandoff } from '../api/teammates'
import { canonicalEventId } from './peerReply'

export type TeammateMessage = {
  handoff_id: string; phase: 'request' | 'response' | 'return'
  sender_agent_id: number; recipient_agent_id: number; depth: number
}

export function teammateMessage(event: RoomEvent): TeammateMessage | null {
  const value = event.payload.teammate_handoff
  if (event.actor_type !== 'agent' || !['message', 'failure'].includes(event.kind)
    || !value || typeof value !== 'object' || Array.isArray(value)) return null
  const meta = value as Record<string, unknown>
  if (meta.version !== 1 || !canonicalEventId(meta.handoff_id) || !canonicalEventId(event.payload.trigger_event_id)
    || !['request', 'response', 'return'].includes(String(meta.phase))
    || typeof meta.sender_agent_id !== 'number' || !Number.isSafeInteger(meta.sender_agent_id) || meta.sender_agent_id < 1
    || typeof meta.recipient_agent_id !== 'number' || !Number.isSafeInteger(meta.recipient_agent_id) || meta.recipient_agent_id < 1
    || meta.sender_agent_id === meta.recipient_agent_id || typeof meta.depth !== 'number' || !Number.isSafeInteger(meta.depth) || meta.depth < 1
    || event.actor_id !== (meta.phase === 'response' ? meta.recipient_agent_id : meta.sender_agent_id)) return null
  return { handoff_id: meta.handoff_id, phase: meta.phase as TeammateMessage['phase'],
    sender_agent_id: meta.sender_agent_id, recipient_agent_id: meta.recipient_agent_id, depth: meta.depth }
}

export function matchingTeammateHandoff(event: RoomEvent, handoffs: TeammateHandoff[]): TeammateHandoff | null {
  const meta = teammateMessage(event)
  if (!meta) return null
  return handoffs.find(row => row.handoff_id === meta.handoff_id && row.sender_agent_id === meta.sender_agent_id
    && row.recipient_agent_id === meta.recipient_agent_id && row.depth === meta.depth
    && row.root_request_event_id === event.payload.trigger_event_id
    && (meta.phase === 'request' ? row.request_event_id === event.event_id && row.request_seq === event.seq
      && row.content === event.payload.content
      : meta.phase === 'response' ? row.result_event_id === event.event_id && row.result_seq === event.seq
        : row.return_event_id === event.event_id && row.return_seq === event.seq)) || null
}
