import api from '../lib/api'

export type DraftValue = string | string[]
export interface DraftField { path: string; label: string; role: string; type: 'string' | 'string_array'; max: number; max_items?: number }
export interface ActionDraft { version: 1; action_id: string; review_id: string | null; revision: number; fields: DraftField[]; values: Record<string, DraftValue>; edits: Record<string, DraftValue>; available: boolean; pending: boolean }
export interface DraftReceipt { version: 1; operation: 'save' | 'apply'; state: 'saved' | 'applied'; command_key: string; action_id: string; review_id: string; revision: number }
const base = (room: number, action: string) => `/tools/rooms/${room}/actions/${action}/draft`
export const mcpDraftApi = {
  read: async (room: number, action: string) => (await api.get<ActionDraft>(base(room, action))).data,
  save: async (room: number, action: string, revision: number, review: string, edits: Record<string, DraftValue>, key: string) => (await api.put<DraftReceipt>(base(room, action), { expected_revision: revision, expected_review_id: review, edits }, { headers: { 'Idempotency-Key': key } })).data,
  apply: async (room: number, action: string, revision: number, review: string, key: string) => (await api.post<DraftReceipt>(`${base(room, action)}/apply`, { expected_revision: revision, expected_review_id: review }, { headers: { 'Idempotency-Key': key } })).data,
  command: async (room: number, action: string, key: string) => (await api.get<DraftReceipt>(`${base(room, action)}/commands/${encodeURIComponent(key)}`)).data,
}
