import type { RoomEvent } from '../api/events'
import type { BotMessageRef } from '../api/botMessages'
import type { BotBatch } from '../api/botBatches'
import { canonicalEventId } from './peerReply'
import { botMessageMetadata } from './botMessage'
import type { BotBatchMember } from '../api/botBatches'
import type { MessageKey } from '../i18n/locales/en'
export type BotBatchPhase = 'request' | 'branch_request' | 'branch_delivery' | 'branch_response' | 'return_delivery' | 'return'
export type BotBatchMetadata = { batch_id: string; phase: BotBatchPhase; member_index?: number; root_room_id: number; root_request_event_id: string }
export function botBatchMetadata(event: RoomEvent): BotBatchMetadata | null {
  const value = event.payload.bot_batch
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const meta = value as Record<string, unknown>
  if (meta.version !== 1 || !canonicalEventId(meta.batch_id) || !canonicalEventId(meta.root_request_event_id)
    || event.payload.trigger_event_id !== meta.root_request_event_id || !['request', 'branch_request', 'branch_delivery', 'branch_response', 'return_delivery', 'return'].includes(String(meta.phase))
    || typeof meta.root_room_id !== 'number' || !Number.isSafeInteger(meta.root_room_id) || meta.root_room_id < 1
    || String(meta.phase).startsWith('branch_') && (typeof meta.member_index !== 'number' || !Number.isInteger(meta.member_index) || meta.member_index < 0 || meta.member_index > 7)) return null
  const system = meta.phase === 'branch_delivery' || meta.phase === 'return_delivery'
  if (system ? event.actor_type !== 'system' || event.actor_id !== null || event.kind !== 'activity'
    : event.actor_type !== 'agent' || !['message', 'failure', 'activity'].includes(event.kind)) return null
  return meta as BotBatchMetadata
}
export function isBatchMemberMessage(event: RoomEvent): boolean {
  const batch = botBatchMetadata(event), message = botMessageMetadata(event)
  if (!batch || !message) return false
  const phase = batch.phase === 'branch_request' ? 'request' : batch.phase === 'branch_delivery' ? 'delivery' : batch.phase === 'branch_response' ? 'response' : null
  return phase !== null && message.phase === phase && batch.root_room_id === message.root_room_id && batch.root_request_event_id === message.root_request_event_id
}
export function botBatchMemberStatus(member: BotBatchMember): MessageKey {
  if (member.failure_code === 'interrupted') return 'botBatches.interrupted'
  if (member.failure_code === 'target_busy_timeout') return 'botMessages.busyTimeout'
  if (member.failure_code === 'no_answer') return 'botMessages.passed'
  if (member.failure_code && member.state === 'completed') return 'botBatches.nestedIssue'
  return member.state === 'pending' ? 'teammates.pending' : member.state === 'running' ? member.result_ref ? 'botBatches.nestedWaiting' : 'teammates.running'
    : member.state === 'completed' ? 'teammates.completed' : member.state === 'failed' ? 'teammates.failed' : 'teammates.cancelled'
}
export function batchEventRef(batch: BotBatch, meta: BotBatchMetadata): BotMessageRef | null {
  const member = meta.member_index === undefined ? null : batch.members[meta.member_index]
  return meta.phase === 'request' ? batch.request_ref : meta.phase === 'return_delivery' ? batch.return_delivery_ref : meta.phase === 'return' ? batch.return_ref
    : meta.phase === 'branch_request' ? member?.request_ref || null : meta.phase === 'branch_delivery' ? member?.delivery_ref || null : member?.result_ref || null
}
export function matchingBotBatch(event: RoomEvent, batches: BotBatch[]): BotBatch | null {
  const meta = botBatchMetadata(event)
  if (!meta) return null
  return batches.find(batch => {
    const ref = batchEventRef(batch, meta), member = meta.member_index === undefined ? null : batch.members[meta.member_index]
    if (meta.phase.startsWith('branch_') && event.payload.bot_message != null) {
      const message = botMessageMetadata(event)
      if (!isBatchMemberMessage(event) || !message || !member || message.message_id !== member.message_id
        || message.sender_agent_id !== batch.sender_agent_id || message.recipient_agent_id !== member.recipient_agent_id) return false
    }
    if (meta.phase === 'branch_request' && member?.content !== event.payload.content) return false
    return batch.root_ref.actor_type === 'user' && batch.root_ref.kind === 'message' && batch.batch_id === meta.batch_id && batch.root_ref.room_id === meta.root_room_id && batch.root_ref.event_id === meta.root_request_event_id
      && !!ref && ref.room_id === event.room_id && ref.event_id === event.event_id && ref.seq === event.seq && ref.actor_type === event.actor_type && ref.actor_id === event.actor_id && ref.kind === event.kind
  }) || null
}

// Hide only duplicated sending records whose exact authoritative summary has
// an original Owner message in the rendered feed. Explicit searches/deep links
// and failed reads are handled by the caller and retain the full event.
export function summarizedBatchRequestRoot(event: RoomEvent, batches: BotBatch[], visibleEvents: RoomEvent[], ownerId: number): BotMessageRef | null {
  const meta = botBatchMetadata(event)
  if (!meta || !['request', 'branch_request'].includes(meta.phase)) return null
  const batch = matchingBotBatch(event, batches)
  return batch && visibleEvents.some(root => root.room_id === batch.root_ref.room_id && root.event_id === batch.root_ref.event_id
    && root.seq === batch.root_ref.seq && root.actor_type === 'user' && root.actor_id === ownerId && root.kind === 'message') ? batch.root_ref : null
}

export function summarizedBatchRequest(event: RoomEvent, batches: BotBatch[], visibleEvents: RoomEvent[], ownerId: number): boolean {
  return summarizedBatchRequestRoot(event, batches, visibleEvents, ownerId) !== null
}
