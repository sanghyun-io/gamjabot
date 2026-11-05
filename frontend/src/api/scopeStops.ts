import api from '../lib/api'
import { useAuthStore } from '../stores/authStore'
export type ScopeRevocationRef = { scope_id: string; epoch: number }
export type ScopeStopReceipt = {
  version: 1; stop_id: string; room_id: number; created_at: string; state: 'pending' | 'settled' | 'unknown'
  revocations: ScopeRevocationRef[]; receipts: Array<ScopeRevocationRef & { state: 'settled' | 'unknown' }>
}
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
const uuid = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(value)
const ref = (value: unknown): value is ScopeRevocationRef => object(value) && uuid(value.scope_id) && typeof value.epoch === 'number' && Number.isSafeInteger(value.epoch) && value.epoch >= 0
export const revocationSetKey = (refs: ScopeRevocationRef[]) => JSON.stringify(refs.map(item => [item.scope_id, item.epoch]).sort((a, b) => String(a).localeCompare(String(b))))
function validate(value: unknown, roomId: number): value is ScopeStopReceipt {
  if (!object(value) || value.version !== 1 || !uuid(value.stop_id) || value.room_id !== roomId || typeof value.created_at !== 'string' || !Number.isFinite(Date.parse(value.created_at))
    || !['pending', 'settled', 'unknown'].includes(String(value.state)) || !Array.isArray(value.revocations) || !value.revocations.every(ref)
    || !Array.isArray(value.receipts) || !value.receipts.every(item => object(item) && ['settled', 'unknown'].includes(String(item.state)) && ref(item))) return false
  const targets = value.revocations as ScopeRevocationRef[], receipts = value.receipts as ScopeStopReceipt['receipts']
  if (new Set(targets.map(item => `${item.scope_id}:${item.epoch}`)).size !== targets.length
    || new Set(receipts.map(item => `${item.scope_id}:${item.epoch}`)).size !== receipts.length
    || !receipts.every(item => targets.some(target => item.scope_id === target.scope_id && item.epoch === target.epoch))) return false
  return value.state !== 'settled' || receipts.length === targets.length && receipts.every(item => item.state === 'settled')
}
function scope() {
  const auth = useAuthStore.getState(), identity = `${auth.user?.id || 0}:${auth.epoch}`
  if (!auth.token || !auth.user?.id) throw new Error('Stop receipt owner unavailable')
  return () => { const latest = useAuthStore.getState(); return !!latest.token && `${latest.user?.id || 0}:${latest.epoch}` === identity }
}
export const scopeStopsApi = {
  latest: async (roomId: number): Promise<ScopeStopReceipt | null> => {
    const current = scope(), { data } = await api.get<{ version: 1; room_id: number; latest: ScopeStopReceipt | null }>(`/bots/conversations/${roomId}/scope-stops`)
    if (!current() || !data || data.version !== 1 || data.room_id !== roomId || data.latest !== null && !validate(data.latest, roomId)) throw new Error('Stop receipt unavailable')
    return data.latest
  },
  read: async (stopId: string, roomId: number): Promise<ScopeStopReceipt> => {
    const current = scope(), { data } = await api.get<ScopeStopReceipt>(`/bots/scope-stops/${stopId}`)
    if (!current() || !validate(data, roomId) || data.stop_id !== stopId) throw new Error('Stop receipt unavailable')
    return data
  },
  confirm: async (original: ScopeStopReceipt): Promise<ScopeStopReceipt> => {
    const current = scope(), frozenTargets = revocationSetKey(original.revocations)
    // Reconfirm only the durable receipt. Never resend room cancellation or select new scopes.
    const { data } = await api.post<ScopeStopReceipt>(`/bots/scope-stops/${original.stop_id}/confirm`)
    if (!current() || !validate(data, original.room_id) || data.stop_id !== original.stop_id || data.created_at !== original.created_at
      || revocationSetKey(data.revocations) !== frozenTargets) throw new Error('Stop target confirmation unavailable')
    return data
  },
}
