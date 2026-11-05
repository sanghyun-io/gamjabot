import type { Connection, TestStatus } from '../api/connections'
import type { MessageKey } from '../i18n/locales/en'
import { directoryViewGeneration } from './directoryViewMemory'

export type ConnectionTestTarget = Readonly<{ connectionId: number; label: string; modelId: string; profile: 'text' | 'full'; configRevision: number; authGrantRevision: number; maxRequests: number; startedAt: string }>
export type ConnectionTestRequest = Readonly<{ id: string; target: ConnectionTestTarget; phase: 'preparing' | 'sending' | 'received' | 'unknown' | 'not_sent'; response?: TestStatus; receivedAt?: string; notice?: MessageKey }>
export type ConnectionTestWorkspace = Readonly<{ model: string | null; requests: readonly ConnectionTestRequest[] }>
const initial: ConnectionTestWorkspace = { model: null, requests: [] }
const workspaces = new Map<number, { identity: string; value: ConnectionTestWorkspace }>(), pending = new Map<string, { id: string; connectionId: number }>(), listeners = new Set<() => void>()
let version = 0
const emit = () => { version++; listeners.forEach(listener => listener()) }
export const connectionTestsVersion = () => version
export function subscribeConnectionTests(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export const readConnectionTests = (identity: string, connectionId: number) => { const row = workspaces.get(connectionId); return row?.identity === identity ? row.value : initial }
export const connectionTestsBusy = (identity: string) => pending.has(identity)
export const connectionTestActive = (identity: string, connectionId: number) => pending.get(identity)?.connectionId === connectionId
export const effectiveTestModel = (connection: Connection, value: ConnectionTestWorkspace) => value.model?.trim() || connection.default_model_id
export function hasUnknownConnectionTest(identity: string) { return [...workspaces.values()].some(row => row.identity === identity && row.value.requests.some(request => request.phase === 'unknown')) }
export function clearConnectionTests() { workspaces.clear(); pending.clear(); emit() }
export function selectConnectionTestModel(identity: string, connectionId: number, generation: number, model: string) {
  if (generation !== directoryViewGeneration()) return
  const previous = readConnectionTests(identity, connectionId)
  workspaces.set(connectionId, { identity, value: { ...previous, model } }); emit()
}
export function beginConnectionTest(identity: string, generation: number, target: ConnectionTestTarget) {
  if (generation !== directoryViewGeneration() || pending.has(identity)) return null
  const id = crypto.randomUUID(), request: ConnectionTestRequest = { id, target: Object.freeze({ ...target }), phase: 'preparing' }
  const previous = readConnectionTests(identity, target.connectionId)
  pending.set(identity, { id, connectionId: target.connectionId })
  workspaces.set(target.connectionId, { identity, value: { ...previous, requests: [...previous.requests, request] } }); emit(); return request
}
export function updateConnectionTest(identity: string, generation: number, original: ConnectionTestRequest, phase: ConnectionTestRequest['phase'], response?: TestStatus, notice?: MessageKey) {
  if (generation !== directoryViewGeneration()) return false
  const previous = readConnectionTests(identity, original.target.connectionId)
  if (!previous.requests.some(request => request.id === original.id)) return false
  const copy = response && JSON.parse(JSON.stringify(response)) as TestStatus | undefined
  const requests = previous.requests.map(request => request.id === original.id ? { ...request, phase, response: copy, notice, receivedAt: copy ? new Date().toISOString() : request.receivedAt } : request)
  workspaces.set(original.target.connectionId, { identity, value: { ...previous, requests } })
  if (!['preparing', 'sending'].includes(phase) && pending.get(identity)?.id === original.id) pending.delete(identity)
  emit(); return true
}
// Stop only the UI wait. An already-dispatched provider test has no cancellation/idempotency API.
// Its late response may still settle this original record, never a newer request or current model.
export function stopConnectionTestWait(identity: string, generation: number, original: ConnectionTestRequest) {
  const request = readConnectionTests(identity, original.target.connectionId).requests.find(value => value.id === original.id)
  if (!request || !['preparing', 'sending'].includes(request.phase)) return false
  return updateConnectionTest(identity, generation, request, request.phase === 'preparing' ? 'not_sent' : 'unknown', undefined,
    request.phase === 'preparing' ? 'connections.testSession.notSent' : 'connections.testSession.waitStopped')
}
