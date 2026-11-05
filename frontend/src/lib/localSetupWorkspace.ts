import type { Connection, ModelCatalog, ModelFeatureReport, VersionedConnectionInput } from '../api/connections'
import type { MessageKey } from '../i18n/locales/en'
import { directoryViewGeneration } from './directoryViewMemory'

export const localPresets = { ollama: 'http://127.0.0.1:11434/v1', 'lm-studio': 'http://127.0.0.1:1234/v1', 'local-openai': 'http://127.0.0.1:8080/v1' }
export type LocalProvider = keyof typeof localPresets
export type LocalSetupForm = { provider: LocalProvider; url: string; model: string; name: string; serverRevision: number }
export type LocalCheck = { id: string; status: 'preparing' | 'checking' | 'done' | 'unknown' | 'not_sent'; report?: ModelFeatureReport; notice?: MessageKey }
export type LocalSetupAttempt = {
  id: string; body: VersionedConnectionInput; model: string; url: string;
  status: 'creating' | 'saved' | 'unknown' | 'rejected'; saved?: Connection;
  inspected?: boolean; candidates?: { id: number; label: string }[];
  checks: LocalCheck[]; storedReport?: ModelFeatureReport;
}
export type LocalSetupWorkspace = {
  form: LocalSetupForm; catalog: { serverRevision: number; value: ModelCatalog } | null;
  attempts: LocalSetupAttempt[]; currentId: string | null;
  operation: { id: string; kind: 'list' | 'create' | 'check' | 'inspect' | 'report' } | null;
  notice: MessageKey | null;
}
const initial: LocalSetupWorkspace = { form: { provider: 'ollama', url: localPresets.ollama, model: '', name: '', serverRevision: 0 }, catalog: null, attempts: [], currentId: null, operation: null, notice: null }
const workspaces = new Map<string, LocalSetupWorkspace>(), listeners = new Set<() => void>()
export const readLocalSetup = (identity: string) => workspaces.get(identity) || initial
export function subscribeLocalSetup(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export function clearLocalSetups() { workspaces.clear(); listeners.forEach(listener => listener()) }
export function updateLocalSetup(identity: string, generation: number, update: (value: LocalSetupWorkspace) => LocalSetupWorkspace) {
  if (generation !== directoryViewGeneration()) return false
  const previous = readLocalSetup(identity), next = update(previous)
  if (previous === next) return false
  workspaces.set(identity, next); listeners.forEach(listener => listener()); return true
}
export function localSetupHasWork(value: LocalSetupWorkspace) {
  return !!(value.attempts.length || value.operation || value.catalog || value.form.model || value.form.name || value.form.url !== localPresets[value.form.provider] || value.form.provider !== 'ollama')
}
// Copies contain public local-none configuration only. Never a credential or browser storage.
export const snapshotLocalSetup = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T
export function normalizeLocalUrl(value: string) {
  try {
    const url = new URL(value.trim())
    if (!['http:', 'https:'].includes(url.protocol) || !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname.toLowerCase()) || url.username || url.password || url.search || url.hash || value.length > 2048) return null
    return url.href.replace(/\/$/, '')
  } catch { return null }
}
