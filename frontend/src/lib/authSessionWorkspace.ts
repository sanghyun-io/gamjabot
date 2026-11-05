import type { AuthMethodId, AuthOperationResponse, ConnectionRevisions } from '../api/connections'
import type { MessageKey } from '../i18n/locales/en'
import { directoryViewGeneration } from './directoryViewMemory'

export type AuthStartAttempt = Readonly<{
  id: string; key: string; connectionId: number; label: string; method: AuthMethodId; body: Readonly<ConnectionRevisions>;
  status: 'preparing' | 'sending' | 'unknown' | 'accepted' | 'rejected' | 'not_sent';
  response?: AuthOperationResponse; receivedAt?: string; retryRejected?: boolean; statusFailed?: boolean;
  cancellation?: Readonly<{ operationId: string; status: 'sending' | 'unknown' | 'response' | 'rejected' }>;
}>
export type AuthSessionWorkspace = Readonly<{
  selected: AuthMethodId | null; attempts: readonly AuthStartAttempt[]; currentId: string | null;
  operation: Readonly<{ token: string; kind: 'preflight' | 'start' | 'status' | 'cancel'; attemptId: string }> | null;
  notice: MessageKey | null;
}>
const initial: AuthSessionWorkspace = { selected: null, attempts: [], currentId: null, operation: null, notice: null }
const workspaces = new Map<number, { identity: string; value: AuthSessionWorkspace }>(), listeners = new Set<() => void>()
let version = 0
const emit = () => { version++; listeners.forEach(listener => listener()) }
export const authSessionsVersion = () => version
export function subscribeAuthSessions(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export const readAuthSession = (identity: string, connectionId: number) => { const row = workspaces.get(connectionId); return row?.identity === identity ? row.value : initial }
export function clearAuthSessions() { workspaces.clear(); emit() }
export function updateAuthSession(identity: string, connectionId: number, generation: number, update: (value: AuthSessionWorkspace) => AuthSessionWorkspace) {
  if (generation !== directoryViewGeneration()) return false
  const previous = readAuthSession(identity, connectionId), next = update(previous)
  if (previous === next) return false
  workspaces.set(connectionId, { identity, value: next }); emit(); return true
}
export const authTerminal = (response?: AuthOperationResponse) => !!response && ['succeeded', 'cancelled', 'expired', 'interrupted', 'failed'].includes(response.state)
export const currentAuthAttempt = (value: AuthSessionWorkspace) => value.attempts.find(attempt => attempt.id === value.currentId)
export function authSessionLocked(value: AuthSessionWorkspace) {
  const attempt = currentAuthAttempt(value)
  return !!value.operation || !!attempt && (['preparing', 'sending', 'unknown'].includes(attempt.status) || !!attempt.response && !authTerminal(attempt.response))
}
export function authSessionHint(value: AuthSessionWorkspace): MessageKey | null {
  const attempt = currentAuthAttempt(value)
  if (!attempt) return null
  if (['not_sent', 'rejected', 'unknown'].includes(attempt.status) || attempt.cancellation?.status === 'unknown' || attempt.statusFailed) return 'connections.authSession.hintUnknown'
  if (value.operation || ['preparing', 'sending'].includes(attempt.status) || attempt.response && !authTerminal(attempt.response)) return 'connections.authSession.hintPending'
  return 'connections.authSession.hintResult'
}
// Login memory only. Never export these ephemeral public auth URLs/codes or command keys to disk/logs.
export const snapshotAuth = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T
