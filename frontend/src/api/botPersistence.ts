import api from '../lib/api'
import { useAuthStore } from '../stores/authStore'
import { canonicalEventId } from '../lib/peerReply'
import { validBotMessageRef, type BotMessageRef } from './botMessages'

export type BotPersistenceReceipt = {
  version: 1; receipt_id: string; state: 'saved'; operation: 'add'|'edit'|'delete'|'role'; agent_id: number
  root_request_event_id: string; root_ref: BotMessageRef; result_ref: BotMessageRef; content: string
  revision: number | null; entry_revision: number | null; memory_id: string | null; effective: 'next_request'; history_retained: boolean; replayed: boolean
}
const positive = (v: unknown): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v > 0
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v)
export function validPersistenceReceipt(v: unknown, room: number, owner: number): v is BotPersistenceReceipt {
  return object(v) && v.version === 1 && v.state === 'saved' && typeof v.receipt_id === 'string' && /^[0-9a-f]{64}$/.test(v.receipt_id)
    && ['add','edit','delete','role'].includes(String(v.operation)) && positive(v.agent_id)
    && canonicalEventId(v.root_request_event_id) && validBotMessageRef(v.root_ref) && v.root_ref.event_id === v.root_request_event_id && v.root_ref.room_id === room && v.root_ref.actor_type === 'user' && v.root_ref.actor_id === owner && v.root_ref.kind === 'message'
    && validBotMessageRef(v.result_ref) && v.result_ref.room_id === room && v.result_ref.actor_type === 'system' && v.result_ref.actor_id === null && v.result_ref.kind === 'activity'
    && v.root_ref.seq < v.result_ref.seq && typeof v.content === 'string' && !!v.content.trim()
    && v.effective === 'next_request' && typeof v.replayed === 'boolean' && v.history_retained === ['edit','delete'].includes(String(v.operation))
    && (v.operation === 'role' ? v.memory_id === null && v.revision === null && v.entry_revision === null
      : canonicalEventId(v.memory_id) && typeof v.revision === 'number' && Number.isSafeInteger(v.revision) && v.revision > 0 && typeof v.entry_revision === 'number' && Number.isSafeInteger(v.entry_revision) && v.entry_revision > 0)
}
export const botPersistenceApi = {
  read: async (agent: number, receipt: string, room: number): Promise<BotPersistenceReceipt> => {
    const before = useAuthStore.getState(), owner = before.user?.id, epoch = before.epoch
    if (!before.token || !positive(owner) || !positive(agent) || !positive(room) || !/^[0-9a-f]{64}$/.test(receipt)) throw new Error('Persistence unavailable')
    const { data } = await api.get<unknown>(`/bots/${agent}/persistence/${receipt}`)
    const after = useAuthStore.getState()
    if (!after.token || after.user?.id !== owner || after.epoch !== epoch || !validPersistenceReceipt(data, room, owner)
      || data.agent_id !== agent || data.receipt_id !== receipt) throw new Error('Persistence result unavailable')
    return data
  },
}
