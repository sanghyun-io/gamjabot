import api from '../lib/api'
import type { RoomEvent } from './events'
import type { TeammateHandoffState, TeammateReplyState } from './teammates'
import { useAuthStore } from '../stores/authStore'
import { canonicalEventId } from '../lib/peerReply'

export type BotMessageCapabilities = { text_collaboration: true; delegated_os_execution: boolean }
export type BotMessageRef = Pick<RoomEvent, 'room_id' | 'event_id' | 'seq' | 'actor_type' | 'actor_id' | 'kind'>
export type BotMessage = {
  version: 1; capabilities: BotMessageCapabilities; message_id: string; root_request_event_id: string; parent_job_id: string | null; parent_kind: 'bot_message' | 'teammate' | null
  depth: number; sender_agent_id: number; sender_name: string; recipient_agent_id: number; recipient_name: string
  content: string; state: TeammateHandoffState; reply_state: TeammateReplyState
  source_room: { room_id: number; title: string }; target_room: { room_id: number; title: string }
  root_ref: BotMessageRef; request_ref: BotMessageRef; delivery_ref: BotMessageRef
  result_ref: BotMessageRef | null; resolved_result_ref?: BotMessageRef | null; return_delivery_ref: BotMessageRef | null; return_ref: BotMessageRef | null
  failure_code: string | null; deadline_at: string; created_at: string; updated_at: string
}
export type RoomBotMessages = {
  version: 1; room_id: number; capabilities: BotMessageCapabilities
  bots: Array<{ agent_id: number; name: string; description: string; model_id: string | null; avatar: unknown }>
  messages: BotMessage[]; messages_truncated: boolean
}
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
const positive = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value > 0
const timestamp = (value: unknown) => typeof value === 'string' && Number.isFinite(Date.parse(value))
const states = new Set(['pending', 'running', 'completed', 'failed', 'cancelled'])
const replyStates = new Set(['none', 'pending', 'running', 'delivered', 'failed', 'cancelled'])
export function validBotMessageRef(value: unknown): value is BotMessageRef {
  return record(value) && positive(value.room_id) && canonicalEventId(value.event_id) && positive(value.seq)
    && ['user', 'agent', 'tool', 'system'].includes(String(value.actor_type))
    && (value.actor_id === null || positive(value.actor_id)) && ['message', 'failure', 'activity', 'tool_result', 'summary'].includes(String(value.kind))
}
const capabilities = (value: unknown) => record(value) && value.text_collaboration === true && typeof value.delegated_os_execution === 'boolean'
function validMessage(value: unknown, roomId: number, ownerId: number): value is BotMessage {
  if (!record(value) || value.version !== 1 || !capabilities(value.capabilities) || !canonicalEventId(value.message_id) || !canonicalEventId(value.root_request_event_id)
    || !positive(value.sender_agent_id) || !positive(value.recipient_agent_id) || value.sender_agent_id === value.recipient_agent_id
    || typeof value.sender_name !== 'string' || typeof value.recipient_name !== 'string' || typeof value.content !== 'string'
    || !positive(value.depth) || !(value.parent_job_id === null || canonicalEventId(value.parent_job_id))
    || !(value.parent_kind === null || value.parent_kind === 'bot_message' || value.parent_kind === 'teammate')
    || !states.has(String(value.state)) || !replyStates.has(String(value.reply_state))
    || !(value.failure_code === null || typeof value.failure_code === 'string')
    || !timestamp(value.created_at) || !timestamp(value.updated_at) || !timestamp(value.deadline_at)
    || !record(value.source_room) || !positive(value.source_room.room_id) || typeof value.source_room.title !== 'string'
    || !record(value.target_room) || !positive(value.target_room.room_id) || typeof value.target_room.title !== 'string'
    || value.source_room.room_id === value.target_room.room_id
    || !validBotMessageRef(value.root_ref) || !validBotMessageRef(value.request_ref) || !validBotMessageRef(value.delivery_ref)
    || value.root_ref.event_id !== value.root_request_event_id || value.root_ref.actor_type !== 'user' || value.root_ref.actor_id !== ownerId || value.root_ref.kind !== 'message'
    || ![value.source_room.room_id, value.target_room.room_id, value.root_ref.room_id].includes(roomId)
    || value.request_ref.room_id !== value.source_room.room_id || value.request_ref.actor_type !== 'agent' || value.request_ref.actor_id !== value.sender_agent_id || value.request_ref.kind !== 'message'
    || value.delivery_ref.room_id !== value.target_room.room_id || value.delivery_ref.actor_type !== 'system' || value.delivery_ref.actor_id !== null || value.delivery_ref.kind !== 'activity') return false
  const matches = (ref: unknown, room: number, actor: number | null) => ref === null || validBotMessageRef(ref) && ref.room_id === room
    && ref.actor_type === (actor === null ? 'system' : 'agent') && ref.actor_id === actor
  if (!matches(value.result_ref, value.target_room.room_id, value.recipient_agent_id)
    || !matches(value.return_delivery_ref, value.source_room.room_id, null) || !matches(value.return_ref, value.source_room.room_id, value.sender_agent_id)) return false
  if (value.resolved_result_ref !== undefined && value.resolved_result_ref !== null && (!matches(value.resolved_result_ref, value.target_room.room_id, value.recipient_agent_id)
    || !validBotMessageRef(value.resolved_result_ref) || value.resolved_result_ref.kind !== 'message'
    || value.state !== 'completed' || value.reply_state !== 'delivered' || !validBotMessageRef(value.result_ref)
    || value.resolved_result_ref.seq < value.result_ref.seq)) return false
  if (value.state === 'completed' && value.result_ref === null || value.reply_state === 'delivered' && value.return_ref === null) return false
  // Sequences order events only within their own room, never between conversations.
  if (value.root_ref.room_id === value.request_ref.room_id && value.root_ref.seq >= value.request_ref.seq) return false
  if (validBotMessageRef(value.result_ref) && value.result_ref.seq <= value.delivery_ref.seq) return false
  if (validBotMessageRef(value.return_delivery_ref) && value.return_delivery_ref.seq <= value.request_ref.seq) return false
  if (validBotMessageRef(value.return_ref) && value.return_ref.seq <= (validBotMessageRef(value.return_delivery_ref) ? value.return_delivery_ref.seq : value.request_ref.seq)) return false
  return true
}
export const botMessagesApi = {
  list: async (roomId: number): Promise<RoomBotMessages> => {
    const before = useAuthStore.getState(), owner = before.user?.id, epoch = before.epoch
    if (!before.token || !positive(owner) || !positive(roomId)) throw new Error('Bot messages unavailable')
    const { data } = await api.get<RoomBotMessages>(`/bots/conversations/${roomId}/messages`)
    const after = useAuthStore.getState()
    if (after.user?.id !== owner || after.epoch !== epoch || !data || data.version !== 1 || data.room_id !== roomId
      || !capabilities(data.capabilities) || !Array.isArray(data.messages) || data.messages.length > 100 || !data.messages.every(row => validMessage(row, roomId, owner))
      || new Set(data.messages.map(row => row.message_id)).size !== data.messages.length || typeof data.messages_truncated !== 'boolean'
      || !Array.isArray(data.bots) || !data.bots.every(row => positive(row.agent_id) && typeof row.name === 'string'
        && typeof row.description === 'string' && (row.model_id === null || typeof row.model_id === 'string'))) throw new Error('Bot message response changed or unavailable')
    return data
  },
}

export type BotMessageRetryPlan = {
  version: 1; message_id: string; expected_updated_at: string; root_ref: BotMessageRef
  eligible: boolean; blocked_reason: string | null; cost: 'new_model_work'; source_agent_id: number | null; plan_hash: string | null
}
export type BotMessageRetryReceipt = {
  version: 1; message_id: string; root_room_id: number; retry_event_id: string; retry_seq: number
  href: string; source_agent_id: number; root_ref: BotMessageRef; replayed: boolean; cost: 'new_model_work'; plan_hash: string
}
const sameRef = (a: BotMessageRef, b: BotMessageRef) => a.room_id === b.room_id && a.event_id === b.event_id && a.seq === b.seq
  && a.actor_type === b.actor_type && a.actor_id === b.actor_id && a.kind === b.kind
const retryScope = () => {
  const auth = useAuthStore.getState(), identity = `${auth.user?.id || 0}:${auth.epoch}`
  if (!auth.token || !positive(auth.user?.id)) throw new Error('Bot retry unavailable')
  return () => { const current = useAuthStore.getState(); return !!current.token && `${current.user?.id || 0}:${current.epoch}` === identity }
}
export const botMessageRetriesApi = {
  preview: async (message: BotMessage): Promise<BotMessageRetryPlan> => {
    const current = retryScope()
    if (message.root_ref.actor_id !== useAuthStore.getState().user?.id) throw new Error('Bot retry owner changed')
    const { data } = await api.get<BotMessageRetryPlan>(`/bots/messages/${message.message_id}/retry`)
    if (!current() || !data || data.version !== 1 || data.message_id !== message.message_id || !timestamp(data.expected_updated_at)
      || !validBotMessageRef(data.root_ref) || !sameRef(data.root_ref, message.root_ref) || data.cost !== 'new_model_work'
      || typeof data.eligible !== 'boolean' || !(data.source_agent_id === null || positive(data.source_agent_id))
      || (data.eligible ? data.blocked_reason !== null || !positive(data.source_agent_id) || typeof data.plan_hash !== 'string' || !/^[a-f0-9]{64}$/.test(data.plan_hash) : typeof data.blocked_reason !== 'string' || !data.blocked_reason || data.plan_hash !== null)) throw new Error('Bot retry preview changed or unavailable')
    return { ...data, root_ref: { ...data.root_ref } }
  },
  start: async (plan: BotMessageRetryPlan, body: Readonly<{ expected_updated_at: string; plan_hash: string }>, key: string): Promise<BotMessageRetryReceipt> => {
    const current = retryScope()
    if (!plan.eligible || plan.root_ref.actor_id !== useAuthStore.getState().user?.id || body.expected_updated_at !== plan.expected_updated_at || body.plan_hash !== plan.plan_hash || !key) throw new Error('Bot retry request changed')
    const { data } = await api.post<BotMessageRetryReceipt>(`/bots/messages/${plan.message_id}/retry`, body, { headers: { 'Idempotency-Key': key } })
    if (!current() || !data || data.version !== 1 || data.message_id !== plan.message_id || data.root_room_id !== plan.root_ref.room_id
      || !canonicalEventId(data.retry_event_id) || data.retry_event_id === plan.root_ref.event_id || !positive(data.retry_seq) || data.retry_seq <= plan.root_ref.seq
      || data.plan_hash !== plan.plan_hash || data.source_agent_id !== plan.source_agent_id || !validBotMessageRef(data.root_ref) || !sameRef(data.root_ref, plan.root_ref)
      || typeof data.replayed !== 'boolean' || data.cost !== 'new_model_work') throw new Error('Bot retry acceptance unconfirmed')
    return { ...data, href: `/chat/${data.root_room_id}?${new URLSearchParams({ seq: String(data.retry_seq), event: data.retry_event_id })}` }
  },
}
