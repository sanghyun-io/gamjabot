import api from '../lib/api'
import { useAuthStore } from '../stores/authStore'
import { validBotMessageRef, type BotMessageRef } from './botMessages'

export type CheckinState = 'running'|'completed'|'failed'|'cancelled'|'interrupted'|'unknown'
export type PrimaryCheckin = { version: 1; revision: number; primary_revision: number; enabled: boolean; prompt: string; interval_minutes: number; next_run_at: string | null; last_run_id: string | null; last_state: CheckinState | null; primary_agent_id: number | null; availability: 'unset'|'available'|'primary_changed'|'room_unavailable'|'agent_inactive'|'profile_changed'|'owner_inactive'|'disabled' }
export type CheckinRun = { run_id: string; state: CheckinState; scheduled_at: string; started_at: string | null; completed_at: string | null; root_ref: BotMessageRef | null; result_ref: BotMessageRef | null; failure_code: string | null; primary_agent_id: number; room_id: number; connection_id: number | null; model_id: string | null }
export type CheckinRuns = { version: 1; runs: CheckinRun[] }
export type CheckinDraftBase = { revision: number; primary_revision: number; agent_id: number | null }
export function checkinDraftCurrent(base: CheckinDraftBase, value: PrimaryCheckin, primary: { revision: number; agent_id: number | null }): boolean {
  return base.revision === value.revision && base.primary_revision === value.primary_revision && base.primary_revision === primary.revision && base.agent_id === primary.agent_id
}
export type CheckinWrite = { expected_revision: number; expected_primary_revision: number; enabled: boolean; prompt: string; interval_minutes: number }
const object = (x: unknown): x is Record<string, unknown> => !!x && typeof x === 'object' && !Array.isArray(x)
const positive = (x: unknown): x is number => typeof x === 'number' && Number.isSafeInteger(x) && x > 0
const revision = (x: unknown) => typeof x === 'number' && Number.isSafeInteger(x) && x >= 0
const date = (x: unknown) => typeof x === 'string' && Number.isFinite(Date.parse(x))
const optionalDate = (x: unknown) => x === null || date(x)
const states = ['running','completed','failed','cancelled','interrupted','unknown']
const id = (x: unknown) => typeof x === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(x)
const owner = () => { const s = useAuthStore.getState(); if (!s.token || !positive(s.user?.id)) throw new Error('Check-in owner unavailable'); return { id: s.user.id, epoch: s.epoch } }
const unchanged = (before: ReturnType<typeof owner>) => { const s = useAuthStore.getState(); if (!s.token || s.user?.id !== before.id || s.epoch !== before.epoch) throw new Error('Check-in owner changed') }
export function validCheckin(x: unknown): x is PrimaryCheckin {
  return object(x) && x.version === 1 && revision(x.revision) && revision(x.primary_revision) && typeof x.enabled === 'boolean' && typeof x.prompt === 'string'
    && typeof x.interval_minutes === 'number' && Number.isSafeInteger(x.interval_minutes) && x.interval_minutes >= 1 && x.interval_minutes <= 10080
    && optionalDate(x.next_run_at) && (x.last_run_id === null || id(x.last_run_id)) && (x.last_state === null || states.includes(String(x.last_state)))
    && (x.primary_agent_id === null || positive(x.primary_agent_id)) && ['unset','available','primary_changed','room_unavailable','agent_inactive','profile_changed','owner_inactive','disabled'].includes(String(x.availability))
}
export function validCheckinRuns(x: unknown): x is CheckinRuns {
  if (!object(x) || x.version !== 1 || !Array.isArray(x.runs) || x.runs.length > 20) return false
  return x.runs.every((r: unknown) => object(r) && id(r.run_id) && states.includes(String(r.state)) && date(r.scheduled_at)
    && optionalDate(r.started_at) && optionalDate(r.completed_at) && positive(r.primary_agent_id) && positive(r.room_id)
    && (r.connection_id === null || positive(r.connection_id)) && (r.model_id === null || typeof r.model_id === 'string')
    && (r.failure_code === null || typeof r.failure_code === 'string')
    && (r.root_ref === null || validBotMessageRef(r.root_ref) && r.root_ref.room_id === r.room_id && r.root_ref.actor_type === 'system' && r.root_ref.actor_id === null && r.root_ref.kind === 'activity')
    && (r.result_ref === null || validBotMessageRef(r.result_ref) && r.result_ref.room_id === r.room_id && r.result_ref.actor_type === 'agent' && r.result_ref.actor_id === r.primary_agent_id && r.result_ref.kind === 'message'))
}
export const primaryCheckinsApi = {
  get: async (): Promise<PrimaryCheckin> => { const before = owner(), { data } = await api.get<unknown>('/bots/primary/check-ins'); unchanged(before); if (!validCheckin(data)) throw new Error('Check-in read unavailable'); return data },
  runs: async (): Promise<CheckinRuns> => { const before = owner(), { data } = await api.get<unknown>('/bots/primary/check-ins/runs'); unchanged(before); if (!validCheckinRuns(data)) throw new Error('Check-in history unavailable'); return data },
  put: async (value: CheckinWrite): Promise<PrimaryCheckin> => { const before = owner(), { data } = await api.put<unknown>('/bots/primary/check-ins', value); unchanged(before); if (!validCheckin(data) || data.enabled !== value.enabled || data.revision <= value.expected_revision || data.primary_revision !== value.expected_primary_revision || data.prompt !== value.prompt || data.interval_minutes !== value.interval_minutes) throw new Error('Check-in save unavailable'); return data },
}
