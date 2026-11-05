import api from '../lib/api'
import { useAuthStore } from '../stores/authStore'
import { canonicalEventId } from '../lib/peerReply'
import { type BotMessageRef } from './botMessages'
import type { BotMemoryReceipt } from './botMemories'

export type MemoryPolicy = { version: 1; agent_id: number; revision: number; mode: 'canonical_only'|'owner_rooms' }
export type CommonFact = { fact_id: string; kind: 'name'|'timezone'; content: string; revision: number; origin: { agent_id: number; source_kind: 'manual'|'event'; room_id: number | null; event_id: string | null; seq: number | null }; visibility: 'owner_bots'; created_at: string; updated_at: string }
export type CommonList = { version: 1; scope: 'owner_common'; revision: number; items: CommonFact[] }
export type CommonReceipt = { version: 1; scope: 'owner_common'; fact_id: string; kind: 'name'|'timezone'; revision: number; entry_revision: number; origin: CommonFact['origin']; operation: string; root_ref: BotMessageRef | null; result_ref: BotMessageRef | null; replayed: boolean }
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v)
const positive = (v: unknown): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v > 0
const revision = (v: unknown) => typeof v === 'number' && Number.isSafeInteger(v) && v >= 0
const origin = (v: unknown): v is CommonFact['origin'] => object(v) && positive(v.agent_id) && (v.source_kind === 'manual' ? v.room_id === null && v.event_id === null && v.seq === null : v.source_kind === 'event' && positive(v.room_id) && canonicalEventId(v.event_id) && positive(v.seq))
const timestamp = (v: unknown) => typeof v === 'string' && Number.isFinite(Date.parse(v))
const owner = () => { const s = useAuthStore.getState(); if (!s.token || !positive(s.user?.id)) throw new Error('Memory owner unavailable'); return { id: s.user.id, epoch: s.epoch } }
const unchanged = (before: ReturnType<typeof owner>) => { const s = useAuthStore.getState(); if (!s.token || s.user?.id !== before.id || s.epoch !== before.epoch) throw new Error('Memory owner changed') }
export function validMemoryPolicy(v: unknown, agent: number): v is MemoryPolicy { return object(v) && v.version === 1 && v.agent_id === agent && revision(v.revision) && ['canonical_only','owner_rooms'].includes(String(v.mode)) }
export function validCommonList(v: unknown): v is CommonList {
  return object(v) && v.version === 1 && v.scope === 'owner_common' && revision(v.revision) && Array.isArray(v.items) && v.items.length <= 100 && v.items.every((x: unknown) => object(x) && canonicalEventId(x.fact_id) && ['name','timezone'].includes(String(x.kind)) && typeof x.content === 'string' && positive(x.revision) && origin(x.origin) && x.visibility === 'owner_bots' && timestamp(x.created_at) && timestamp(x.updated_at))
}
export const memorySharingApi = {
  readChange: async (agent: number, receipt: string): Promise<unknown> => { const auth = owner(), { data } = await api.get<unknown>(`/bots/${agent}/persistence/${receipt}`); unchanged(auth); return data },
  policy: async (agent: number): Promise<MemoryPolicy> => { const auth = owner(), { data } = await api.get<unknown>(`/bots/${agent}/memory-policy`); unchanged(auth); if (!validMemoryPolicy(data, agent)) throw new Error('Memory policy unavailable'); return data },
  setPolicy: async (agent: number, expected_revision: number, mode: MemoryPolicy['mode']): Promise<MemoryPolicy> => { const auth = owner(), { data } = await api.put<unknown>(`/bots/${agent}/memory-policy`, { expected_revision, mode }); unchanged(auth); if (!validMemoryPolicy(data, agent) || data.mode !== mode || data.revision <= expected_revision) throw new Error('Memory policy result unavailable'); return data },
  visibility: async (agent: number, memoryId: string, body: { expected_scope_revision: number; expected_revision: number; group_allowed: boolean }, key: string): Promise<BotMemoryReceipt> => {
    const auth = owner(), { data } = await api.patch<BotMemoryReceipt>(`/bots/${agent}/memories/${memoryId}/visibility`, body, { headers: { 'Idempotency-Key': key } }); unchanged(auth)
    if (!data || data.agent_id !== agent || data.memory_id !== memoryId || data.scope !== 'bot_private' || data.revision <= body.expected_scope_revision || data.entry_revision <= body.expected_revision || typeof data.replayed !== 'boolean') throw new Error('Memory visibility result unavailable'); return data
  },
  common: async (): Promise<CommonList> => { const auth = owner(), { data } = await api.get<unknown>('/bots/memory/common'); unchanged(auth); if (!validCommonList(data)) throw new Error('Common memory unavailable'); return data },
  addCommon: async (agent: number, scopeRevision: number, kind: CommonFact['kind'], content: string, key: string): Promise<unknown> => {
    const auth = owner(), { data } = await api.post<unknown>('/bots/memory/common', { expected_scope_revision: scopeRevision, kind, content, origin: { agent_id: agent, source_kind: 'manual' } }, { headers: { 'Idempotency-Key': key } }); unchanged(auth)
    if (!object(data) || data.version !== 1 || data.scope !== 'owner_common' || data.kind !== kind || !canonicalEventId(data.fact_id) || !positive(data.revision) || data.revision <= scopeRevision || !positive(data.entry_revision) || !origin(data.origin) || data.origin.agent_id !== agent || data.origin.source_kind !== 'manual' || data.root_ref !== null || data.result_ref !== null || data.operation !== 'add' || typeof data.replayed !== 'boolean') throw new Error('Common memory result unavailable')
    return data
  },
  changeCommon: async (fact: CommonFact, scopeRevision: number, content: string | null, key: string): Promise<CommonReceipt> => {
    const auth = owner(), url = `/bots/memory/common/${fact.fact_id}`, body = { expected_scope_revision: scopeRevision, expected_revision: fact.revision }
    const response = content === null ? await api.delete<unknown>(url, { headers: { 'Idempotency-Key': key }, data: body }) : await api.patch<unknown>(url, { ...body, content }, { headers: { 'Idempotency-Key': key } })
    unchanged(auth); const v = response.data
    if (!object(v) || v.version !== 1 || v.scope !== 'owner_common' || v.fact_id !== fact.fact_id || v.kind !== fact.kind || !positive(v.revision) || !positive(v.entry_revision) || v.revision <= scopeRevision || v.entry_revision <= fact.revision || !origin(v.origin) || (v.origin.agent_id !== fact.origin.agent_id || v.origin.source_kind !== fact.origin.source_kind || v.origin.room_id !== fact.origin.room_id || v.origin.event_id !== fact.origin.event_id || v.origin.seq !== fact.origin.seq) || v.root_ref !== null || v.result_ref !== null || v.operation !== (content === null ? 'delete' : 'edit') || typeof v.replayed !== 'boolean') throw new Error('Common memory result unavailable')
    return v as CommonReceipt
  },
}
