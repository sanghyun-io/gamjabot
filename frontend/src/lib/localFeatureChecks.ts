import type { ModelFeatureReport } from '../api/connections'
import type { MessageKey } from '../i18n/locales/en'
import { directoryViewGeneration } from './directoryViewMemory'

export type FeatureCheckTarget = Readonly<{ connectionId: number; modelId: string; revision: string; baseUrl: string }>
export type FeatureCheckRecord = Readonly<{ id: string; target: FeatureCheckTarget; status: 'preparing' | 'checking' | 'done' | 'unknown' | 'not_sent'; report?: ModelFeatureReport; notice?: MessageKey }>
const logs = new Map<string, readonly FeatureCheckRecord[]>(), active = new Map<string, string>(), listeners = new Set<() => void>()
const empty: readonly FeatureCheckRecord[] = []
let version = 0
const scope = (identity: string, connectionId: number, modelId: string, revision: string) => JSON.stringify([identity, connectionId, modelId, revision])
function emit() { version++; listeners.forEach(listener => listener()) }
export const featureChecksVersion = () => version
export function subscribeFeatureChecks(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export const featureCheckBusy = (identity: string) => active.has(identity)
export const readFeatureChecks = (identity: string, connectionId: number, modelId: string, revision: string) => logs.get(scope(identity, connectionId, modelId, revision)) || empty
export function clearFeatureChecks() { logs.clear(); active.clear(); emit() }
// Shared synchronous admission mirrors the local probe's existing one-Owner-at-a-time contract.
// UI memory is not a distributed lock or a server receipt; backend admission remains authoritative.
export function startFeatureCheck(identity: string, generation: number, target: FeatureCheckTarget) {
  if (generation !== directoryViewGeneration() || active.has(identity)) return null
  const id = crypto.randomUUID(), key = scope(identity, target.connectionId, target.modelId, target.revision)
  const record: FeatureCheckRecord = { id, target: Object.freeze({ ...target }), status: 'preparing' }
  active.set(identity, id); logs.set(key, [...(logs.get(key) || empty), record]); emit(); return record
}
export function updateFeatureCheck(identity: string, generation: number, record: FeatureCheckRecord, status: FeatureCheckRecord['status'], report?: ModelFeatureReport, notice?: MessageKey) {
  if (generation !== directoryViewGeneration()) return false
  const key = scope(identity, record.target.connectionId, record.target.modelId, record.target.revision), previous = logs.get(key)
  if (!previous?.some(value => value.id === record.id)) return false
  logs.set(key, previous.map(value => value.id === record.id ? { ...value, status, report: report ? JSON.parse(JSON.stringify(report)) as ModelFeatureReport : undefined, notice } : value))
  if (status !== 'preparing' && status !== 'checking' && active.get(identity) === record.id) active.delete(identity)
  emit(); return true
}
