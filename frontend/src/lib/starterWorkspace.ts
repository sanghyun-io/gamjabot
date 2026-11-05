import type { StarterIntent, StarterPreview, StarterResult } from '../api/starter'
import { directoryViewGeneration } from './directoryViewMemory'

export type StarterForm = Readonly<{
  goal: string; projectTitle: string; roomTitle: string; proposerName: string; reviewerName: string;
  proposerBinding: string; reviewerBinding: string; revision: number; dirty: boolean;
}>
export type StarterPlan = Readonly<{ intent: StarterIntent; value: StarterPreview; goal: string; revision: number; receivedAt?: number }>
export type StarterRequest = Readonly<{
  key: string; fingerprint: string; body: StarterIntent & { plan_hash: string }; goal: string;
  preview: StarterPreview;
  title: string; revision: number; status: 'sending' | 'accepted' | 'unknown' | 'rejected' | 'stale' | 'conflict';
  result?: StarterResult; mismatch?: boolean; acknowledged?: boolean;
  seed?: Readonly<{ status: 'reading' | 'saving' | 'saved' | 'kept' | 'read-error' | 'unknown' | 'conflict' | 'rejected'; version?: number; mismatch?: boolean }>;
}>
export type StarterOperation = Readonly<{ token: string; kind: 'preview' | 'apply' | 'seed'; requestKey?: string }>
export type StarterWorkspace = Readonly<{
  form: StarterForm; plan: StarterPlan | null; requests: readonly StarterRequest[];
  operation: StarterOperation | null; error: '' | 'preview' | 'changed';
}>
const initial: StarterWorkspace = Object.freeze({ form: Object.freeze({ goal: '', projectTitle: '', roomTitle: '', proposerName: '', reviewerName: '',
  proposerBinding: '', reviewerBinding: '', revision: 0, dirty: false }), plan: null, requests: [], operation: null, error: '' })
let current: { identity: string; state: StarterWorkspace } | null = null
const listeners = new Set<() => void>()
export function readStarterWorkspace(identity: string) { return current?.identity === identity ? current.state : initial }
export function subscribeStarterWorkspace(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export function clearStarterWorkspace() { current = null; listeners.forEach(listener => listener()) }
export function updateStarterWorkspace(identity: string, generation: number, update: (previous: StarterWorkspace) => StarterWorkspace) {
  if (generation !== directoryViewGeneration()) return false
  const previous = readStarterWorkspace(identity), next = update(previous)
  if (previous === next) return false
  current = { identity, state: Object.freeze(next) }; listeners.forEach(listener => listener()); return true
}
export function starterHasWork(value: StarterWorkspace) { return value.form.dirty || !!value.operation || value.requests.length > 0 }
export function freezeStarterIntent(value: StarterIntent): StarterIntent {
  const agents: StarterIntent['agents'] = [Object.freeze({ ...value.agents[0] }), Object.freeze({ ...value.agents[1] })]
  Object.freeze(agents)
  return Object.freeze({ ...value, agents })
}
