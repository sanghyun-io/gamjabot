import api from '../lib/api'
export type MemorySource = { kind: 'manual' } | { kind: 'event'; event_id: string; seq: number } | { kind: 'link'; url: string }
export type MemoryCategory = 'fact' | 'decision' | 'requirement'
export interface MemoryWrite { expected_room_revision: number; category: MemoryCategory; content: string; scope: 'room' | 'agent'; target_agent_id: number | null; source: MemorySource }
export interface MemoryEntry {
  memory_id: string; room_id: number; category: MemoryCategory; content: string; scope: 'room' | 'agent'; target_agent_id: number | null
  source_kind: MemorySource['kind']; source_event_id: string | null; source_seq: number | null; source_url: string | null
  source_snapshot: { actor_type?: string; actor_id?: number; name?: string }; revision: number
  created_by: number; updated_by: number; created_at: string; updated_at: string; deleted_at: string | null
}
export interface LegacyMemory { agent_id: number; is_active: boolean; revision: number; context_after_seq: number; legacy_disabled: boolean; legacy: Record<string, unknown>; legacy_digest: string }
export interface MemoryList { room_id: number; revision: number; common_context_after_seq: number; items: MemoryEntry[]; agents: LegacyMemory[] }
export interface MemoryCommand { room_id: number; memory_id: string | null; revision: number; entry_revision: number | null; activity_event_id: string; activity_seq: number; replayed: boolean }
export interface MemoryLegacyCommand { expected_room_revision: number; expected_agent_revision: number; expected_legacy_digest: string; operation: 'disable' | 'replace'; replacement: MemoryWrite | null }
const base = (room: number) => `/conversations/${room}/memories`
const headers = (key: string) => ({ headers: { 'Idempotency-Key': key } })
export const roomMemoriesApi = {
  list: async (room: number) => (await api.get<MemoryList>(base(room))).data,
  add: async (room: number, body: MemoryWrite, key: string) => (await api.post<MemoryCommand>(base(room), body, headers(key))).data,
  edit: async (room: number, id: string, body: MemoryWrite & { expected_revision: number }, key: string) => (await api.patch<MemoryCommand>(`${base(room)}/${id}`, body, headers(key))).data,
  remove: async (room: number, id: string, body: { expected_room_revision: number; expected_revision: number }, key: string) => (await api.delete<MemoryCommand>(`${base(room)}/${id}`, { ...headers(key), data: body })).data,
  legacy: async (room: number, agent: number, body: MemoryLegacyCommand, key: string) => (await api.post<MemoryCommand>(`${base(room)}/legacy/${agent}`, body, headers(key))).data,
}
