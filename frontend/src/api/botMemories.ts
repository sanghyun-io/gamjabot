import api from '../lib/api'
import { useAuthStore } from '../stores/authStore'
import { canonicalEventId } from '../lib/peerReply'

export type BotMemoryCategory = 'fact' | 'preference' | 'role'
export type BotMemorySource = { kind: 'manual' } | { kind: 'event'; room_id: number; event_id: string; seq: number } | { kind: 'link'; url: string }
export type BotMemoryEntry = {
  memory_id: string; agent_id: number; group_allowed: boolean; scope: 'bot_private'; authority: 'context_only'; category: BotMemoryCategory; content: string
  source_kind: 'manual' | 'event' | 'link'; source_room_id: number | null; source_event_id: string | null; source_seq: number | null
  source_url: string | null; source_snapshot: Record<string, unknown>; revision: number; created_by: number; updated_by: number
  created_at: string; updated_at: string; deleted_at: string | null
}
export type BotMemoryList = { agent_id: number; scope: 'bot_private'; authority: 'context_only'; revision: number; personal_room_id: number | null; items: BotMemoryEntry[] }
export type BotMemoryWrite = { expected_scope_revision: number; category: BotMemoryCategory; content: string; source: BotMemorySource }
export type BotMemoryEdit = BotMemoryWrite & { expected_revision: number }
export type BotMemoryDelete = { expected_scope_revision: number; expected_revision: number }
export type BotMemoryReceipt = { agent_id: number; memory_id: string; scope: 'bot_private'; revision: number; entry_revision: number; replayed: boolean
  personal_room_id: number | null; activity_event_id: string | null; activity_seq: number | null }
const positive = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value > 0
const before = () => { const auth = useAuthStore.getState(); return `${auth.user?.id || 0}:${auth.epoch}` }
function requireOwner(identity: string) { const auth = useAuthStore.getState(); if (!auth.token || identity !== `${auth.user?.id || 0}:${auth.epoch}`) throw new Error('Bot memory owner changed') }
function receipt(value: BotMemoryReceipt, agentId: number, memoryId?: string) {
  if (!value || value.agent_id !== agentId || value.scope !== 'bot_private' || !canonicalEventId(value.memory_id)
    || memoryId !== undefined && value.memory_id !== memoryId || !positive(value.revision) || !positive(value.entry_revision)
    || typeof value.replayed !== 'boolean') throw new Error('Bot memory receipt unavailable')
  return value
}
const headers = (key: string) => ({ headers: { 'Idempotency-Key': key } })
const base = (agent: number) => `/bots/${agent}/memories`
export const botMemoriesApi = {
  list: async (agentId: number): Promise<BotMemoryList> => {
    const identity = before(), { data } = await api.get<BotMemoryList>(base(agentId)); requireOwner(identity)
    if (!data || data.agent_id !== agentId || data.scope !== 'bot_private' || data.authority !== 'context_only'
      || !Number.isSafeInteger(data.revision) || data.revision < 0 || data.personal_room_id !== null && !positive(data.personal_room_id)
      || !Array.isArray(data.items) || data.items.some(row => row.agent_id !== agentId || row.scope !== 'bot_private' || row.authority !== 'context_only'
        || !canonicalEventId(row.memory_id) || !positive(row.revision) || !['fact', 'preference', 'role'].includes(row.category)
        || typeof row.content !== 'string' || typeof row.group_allowed !== 'boolean' || !['manual', 'event', 'link'].includes(row.source_kind))) throw new Error('Bot memory response unavailable')
    return data
  },
  add: async (agent: number, body: BotMemoryWrite, key: string) => {
    const identity = before(), { data } = await api.post<BotMemoryReceipt>(base(agent), body, headers(key)); requireOwner(identity); return receipt(data, agent)
  },
  edit: async (agent: number, id: string, body: BotMemoryEdit, key: string) => {
    const identity = before(), { data } = await api.patch<BotMemoryReceipt>(`${base(agent)}/${id}`, body, headers(key)); requireOwner(identity); return receipt(data, agent, id)
  },
  remove: async (agent: number, id: string, body: BotMemoryDelete, key: string) => {
    const identity = before(), { data } = await api.delete<BotMemoryReceipt>(`${base(agent)}/${id}`, { ...headers(key), data: body }); requireOwner(identity); return receipt(data, agent, id)
  },
}
