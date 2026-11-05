import type { ProjectArchivePreview, ProjectArchiveResult, ProjectRestoreResult } from '../api/projects'
import { directoryViewGeneration } from './directoryViewMemory'

export type LifecyclePlan = Readonly<{ id: string; value: ProjectArchivePreview; receivedAt: string; used: boolean }>
export type ProjectLifecycleCommand = Readonly<{
  id: string; kind: 'archive' | 'restore'; target: Readonly<{ id: number; name: string }>; plan?: ProjectArchivePreview; retryOf?: string
  phase: 'pending' | 'succeeded' | 'rejected' | 'stale' | 'unknown'; startedAt: string; finishedAt?: string
  result?: ProjectArchiveResult | ProjectRestoreResult; code?: string; activeRooms?: number; acknowledged?: boolean; waitStopped?: boolean
}>
type Workspace = Readonly<{ plans: ReadonlyMap<number, LifecyclePlan>; commands: readonly ProjectLifecycleCommand[]; pending: string | null }>
const initial: Workspace = { plans: new Map(), commands: [], pending: null }
const rows = new Map<string, Workspace>(), listeners = new Set<() => void>()
let version = 0
const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
const emit = () => { version++; listeners.forEach(listener => listener()) }
export const projectLifecycleVersion = () => version
export function subscribeProjectLifecycle(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export const readProjectLifecycle = (identity: string) => rows.get(identity) || initial
export function clearProjectLifecycle() { rows.clear(); emit() }
export function projectLifecycleBlocked(identity: string) {
  const value = readProjectLifecycle(identity)
  return !!value.pending || value.commands.some(command => command.phase === 'unknown' && !command.acknowledged)
}
export function receiveLifecyclePlan(identity: string, generation: number, value: ProjectArchivePreview) {
  if (generation !== directoryViewGeneration()) return null
  const previous = readProjectLifecycle(identity), plans = new Map(previous.plans)
  const plan: LifecyclePlan = { id: crypto.randomUUID(), value: copy(value), receivedAt: new Date().toISOString(), used: false }
  plans.set(value.project_id, plan); rows.set(identity, { ...previous, plans }); emit(); return plan
}
export function beginProjectLifecycle(identity: string, generation: number, kind: 'archive' | 'restore', target: { id: number; name: string }, previewId?: string, retryOf?: string) {
  if (generation !== directoryViewGeneration()) return null
  const previous = readProjectLifecycle(identity), retry = retryOf ? previous.commands.find(command => command.id === retryOf && command.kind === 'archive' && command.phase === 'unknown' && command.plan) : undefined
  if (retryOf && (!retry || kind !== 'archive')) return null
  if (previous.pending || previous.commands.some(command => command.phase === 'unknown' && !command.acknowledged && command.id !== retry?.id)) return null
  const preview = previous.plans.get(target.id)
  if (kind === 'archive' && (!retry && (!preview || preview.id !== previewId || preview.used) || retry && retry.target.id !== target.id)) return null
  const original: ProjectLifecycleCommand = { id: crypto.randomUUID(), kind, target: Object.freeze(copy(target)), plan: kind === 'archive' ? copy(retry?.plan || preview!.value) : undefined,
    retryOf: retry?.id, phase: 'pending', startedAt: new Date().toISOString() }
  const plans = new Map(previous.plans)
  if (preview) plans.set(target.id, { ...preview, used: true })
  const commands = previous.commands.map(command => command.id === retry?.id ? { ...command, acknowledged: true } : command)
  rows.set(identity, { ...previous, plans, pending: original.id, commands: [...commands, original] }); emit(); return original
}
export function settleProjectLifecycle(identity: string, generation: number, original: ProjectLifecycleCommand, outcome: Pick<ProjectLifecycleCommand, 'phase' | 'result' | 'code' | 'activeRooms'>) {
  if (generation !== directoryViewGeneration()) return false
  const previous = readProjectLifecycle(identity)
  if (!previous.commands.some(command => command.id === original.id && (command.phase === 'pending' || command.phase === 'unknown' && command.waitStopped))) return false
  const plans = new Map(previous.plans), current = plans.get(original.target.id)
  if (current) plans.set(original.target.id, { ...current, used: true })
  rows.set(identity, { ...previous, plans, pending: previous.pending === original.id ? null : previous.pending,
    commands: previous.commands.map(command => command.id === original.id ? { ...command, ...copy(outcome), finishedAt: new Date().toISOString() } : command) }); emit(); return true
}
export function stopProjectLifecycleWait(identity: string, generation: number, id: string) {
  if (generation !== directoryViewGeneration()) return
  const previous = readProjectLifecycle(identity)
  if (!previous.commands.some(command => command.id === id && command.phase === 'pending')) return
  rows.set(identity, { ...previous, pending: previous.pending === id ? null : previous.pending,
    commands: previous.commands.map(command => command.id === id ? { ...command, phase: 'unknown' as const, waitStopped: true } : command) }); emit()
}
export function acknowledgeProjectLifecycle(identity: string, generation: number, id: string) {
  if (generation !== directoryViewGeneration()) return
  const previous = readProjectLifecycle(identity)
  rows.set(identity, { ...previous, commands: previous.commands.map(command => command.id === id && command.phase === 'unknown' ? { ...command, acknowledged: true } : command) }); emit()
}
