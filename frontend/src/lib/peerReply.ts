import type { RoomEvent } from '../api/events'
import { assistantVisibleMarkdown } from './assistantContent'

export const canonicalEventId = (value: unknown): value is string => typeof value === 'string'
  && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value)

function ids(value: unknown, limit: number): value is string[] {
  return Array.isArray(value) && value.length <= limit && value.every(canonicalEventId)
    && new Set(value).size === value.length
}

/** Canonical IDs only. Availability/name/adjacency never implies a reply. */
export function peerReplies(answer: RoomEvent, events: RoomEvent[]) {
  const value = answer.payload.reply_provenance
  if (answer.kind !== 'message' || answer.actor_type !== 'agent' || !value || typeof value !== 'object') return null
  const meta = value as Record<string, unknown>
  const watermark = meta.watermark_seq
  if (meta.version !== 1 || typeof watermark !== 'number' || !Number.isSafeInteger(watermark)
    || watermark < 1 || watermark >= answer.seq || meta.declaration_state !== 'declared'
    || !ids(meta.provided_peer_event_ids, 64) || !ids(meta.reply_to_event_ids, 8)) return null
  const provided = meta.provided_peer_event_ids
  if (meta.reply_to_event_ids.some((id) => !provided.includes(id))) return null
  const references = meta.reply_to_event_ids.map((id) => {
    const source = events.find((event) => event.event_id === id && event.room_id === answer.room_id
      && event.seq <= watermark && event.seq < answer.seq && event.kind === 'message'
      && event.actor_type === 'agent' && event.actor_id !== answer.actor_id
      && typeof event.payload.content === 'string')
    return { id, source }
  })
  return { providedCount: provided.length, references }
}

export function replyExcerpt(source: string) {
  const visible = assistantVisibleMarkdown(source)
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1').replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/<[^>]*>/g, '').replace(/^[ \t]*[#>*]+[ \t]*/gm, '').replace(/[`*_~]/g, '')
    .replace(/\s+/g, ' ').trim()
  const chars = Array.from(visible)
  return chars.slice(0, 120).join('') + (chars.length > 120 ? '…' : '')
}
