import api from '../lib/api'
import type { BotMessage, BotMessageRef } from './botMessages'
import { validBotMessageRef } from './botMessages'
import { canonicalEventId } from '../lib/peerReply'
import { useAuthStore } from '../stores/authStore'
export type BotBatchState = 'pending' | 'running' | 'completed' | 'partial' | 'failed' | 'cancelled' | 'interrupted'
export type BotBatchMember = {
  index: number; message_id: string; recipient_agent_id: number; recipient_name: string; target_room: { room_id: number; title: string }
  content: string; state: BotMessage['state']; failure_code: string | null
  request_ref: BotMessageRef; delivery_ref: BotMessageRef; result_ref: BotMessageRef | null; resolved_result_ref: BotMessageRef | null
}
export type BotBatch = {
  version: 1; batch_id: string; root_ref: BotMessageRef; source_room: { room_id: number; title: string }
  sender_agent_id: number; sender_name: string; parent_job_id: string | null; parent_kind: BotMessage['parent_kind']
  state: BotBatchState; reply_state: BotMessage['reply_state']; failure_code: string | null
  request_ref: BotMessageRef; return_delivery_ref: BotMessageRef | null; return_ref: BotMessageRef | null
  source_scope_id: string; created_at: string; updated_at: string; deadline_at: string; members: BotBatchMember[]
}
export type RoomBotBatches = { version: 1; room_id: number; batches: BotBatch[]; batches_truncated: boolean }
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
const positive = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value > 0
const stamp = (value: unknown) => typeof value === 'string' && Number.isFinite(Date.parse(value))
const failure = (value: unknown) => value === null || typeof value === 'string'
const memberStates = new Set(['pending', 'running', 'completed', 'failed', 'cancelled'])
const batchStates = new Set(['pending', 'running', 'completed', 'partial', 'failed', 'cancelled', 'interrupted'])
const replyStates = new Set(['none', 'pending', 'running', 'delivered', 'failed', 'cancelled'])
const matchesRef = (value: unknown, room: number, actor: number | null, kinds: string[]) => validBotMessageRef(value)
  && value.room_id === room && value.actor_type === (actor === null ? 'system' : 'agent') && value.actor_id === actor && kinds.includes(value.kind)
export function validBotBatch(value: unknown, roomId: number, ownerId: number): value is BotBatch {
  if (!object(value) || value.version !== 1 || !canonicalEventId(value.batch_id) || !canonicalEventId(value.source_scope_id)
    || !validBotMessageRef(value.root_ref) || value.root_ref.actor_type !== 'user' || value.root_ref.actor_id !== ownerId || value.root_ref.kind !== 'message'
    || !object(value.source_room) || !positive(value.source_room.room_id) || typeof value.source_room.title !== 'string'
    || !positive(value.sender_agent_id) || typeof value.sender_name !== 'string'
    || !(value.parent_job_id === null || canonicalEventId(value.parent_job_id)) || ![null, 'bot_message', 'teammate'].includes(value.parent_kind as string | null)
    || !batchStates.has(String(value.state)) || !replyStates.has(String(value.reply_state)) || !failure(value.failure_code)
    || !stamp(value.created_at) || !stamp(value.updated_at) || !stamp(value.deadline_at)
    || !matchesRef(value.request_ref, value.source_room.room_id, value.sender_agent_id, ['message'])
    || !(value.return_delivery_ref === null || matchesRef(value.return_delivery_ref, value.source_room.room_id, null, ['activity']))
    || !(value.return_ref === null || matchesRef(value.return_ref, value.source_room.room_id, value.sender_agent_id, ['message', 'failure', 'activity']))
    || !Array.isArray(value.members) || value.members.length < 2 || value.members.length > 8) return false
  const senderRoom = value.source_room.room_id, senderId = value.sender_agent_id
  if (!value.members.every((member, index) => {
    if (!object(member) || member.index !== index || !canonicalEventId(member.message_id) || !positive(member.recipient_agent_id)
      || member.recipient_agent_id === senderId || typeof member.recipient_name !== 'string' || typeof member.content !== 'string'
      || !object(member.target_room) || !positive(member.target_room.room_id) || typeof member.target_room.title !== 'string'
      || member.target_room.room_id === senderRoom || !memberStates.has(String(member.state)) || !failure(member.failure_code)
      || !matchesRef(member.request_ref, senderRoom, senderId, ['message']) || !matchesRef(member.delivery_ref, member.target_room.room_id, null, ['activity'])
      || !(member.result_ref === null || matchesRef(member.result_ref, member.target_room.room_id, member.recipient_agent_id, ['message', 'failure', 'activity']))
      || !(member.resolved_result_ref === null || matchesRef(member.resolved_result_ref, member.target_room.room_id, member.recipient_agent_id, ['message', 'failure', 'activity']))) return false
    if (member.state === 'completed' && (!validBotMessageRef(member.resolved_result_ref) || member.resolved_result_ref.kind !== 'message')) return false
    // Only compare sequences in the same recipient room.
    if (validBotMessageRef(member.result_ref) && validBotMessageRef(member.delivery_ref) && member.result_ref.seq <= member.delivery_ref.seq) return false
    return true
  }) || new Set(value.members.map(member => member.message_id)).size !== value.members.length
    || new Set(value.members.map(member => member.recipient_agent_id)).size !== value.members.length) return false
  const members = value.members as BotBatchMember[]
  if (![senderRoom, value.root_ref.room_id, ...members.map(member => member.target_room.room_id)].includes(roomId)) return false
  const joined = ['completed', 'partial', 'failed'].includes(String(value.state))
  if (joined && members.some(member => member.state === 'pending' || member.state === 'running')) return false
  if (value.state === 'completed' && (value.failure_code !== null || members.some(member => member.state !== 'completed' || member.failure_code !== null))) return false
  if (value.reply_state === 'delivered' && !validBotMessageRef(value.return_ref)) return false
  // Parent work cannot start from a first sibling result while the join is incomplete.
  if (['pending', 'running'].includes(String(value.state)) && (value.reply_state !== 'none' || value.return_ref !== null || value.return_delivery_ref !== null)) return false
  return true
}
export const botBatchesApi = {
  list: async (roomId: number): Promise<RoomBotBatches> => {
    const before = useAuthStore.getState(), owner = before.user?.id, epoch = before.epoch
    if (!before.token || !positive(owner) || !positive(roomId)) throw new Error('Bot batch owner unavailable')
    const { data } = await api.get<RoomBotBatches>(`/bots/conversations/${roomId}/batches`)
    const after = useAuthStore.getState()
    if (!after.token || after.user?.id !== owner || after.epoch !== epoch || !data || data.version !== 1 || data.room_id !== roomId
      || !Array.isArray(data.batches) || data.batches.length > 100 || !data.batches.every(batch => validBotBatch(batch, roomId, owner))
      || new Set(data.batches.map(batch => batch.batch_id)).size !== data.batches.length || typeof data.batches_truncated !== 'boolean') throw new Error('Bot batch response changed or unavailable')
    return data
  },
}
