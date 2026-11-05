import type { Project } from '../api/projects'
type ProjectInput = { name: string; description: string }
import { directoryViewGeneration } from './directoryViewMemory'

export type ProjectDraft = { name: string; description: string }
export type ProjectDraftRecord = Readonly<{ value: ProjectDraft; baseline: Project | null }>
export type ProjectCommand = Readonly<{
  id: string; kind: 'create' | 'update'; target: Readonly<{ id?: number; name: string }>; body?: Readonly<ProjectInput>
  draft?: ProjectDraft; startedAt: string; finishedAt?: string; snapshotVersion: number
  phase: 'pending' | 'succeeded' | 'rejected' | 'unknown'; result?: Project; acknowledged?: boolean; dismissed?: boolean; waitStopped?: boolean
}>
type Workspace = Readonly<{ drafts: ReadonlyMap<string, ProjectDraftRecord>; commands: readonly ProjectCommand[]; pending: string | null }>
const initial: Workspace = { drafts: new Map(), commands: [], pending: null }
const workspaces = new Map<string, Workspace>(), listeners = new Set<() => void>()
let version = 0
const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
const emit = () => { version++; listeners.forEach(listener => listener()) }
export const projectWorkspaceVersion = () => version
export function subscribeProjectWorkspace(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export const readProjectWorkspace = (identity: string) => workspaces.get(identity) || initial
export const projectDraftKey = (id?: number) => id === undefined ? 'new' : String(id)
export const projectCommandBlocked = (identity: string) => {
  const value = readProjectWorkspace(identity)
  return !!value.pending || value.commands.some(command => command.phase === 'unknown' && !command.acknowledged)
}
export function clearProjectWorkspaces() { workspaces.clear(); emit() }
export function keepProjectDraft(identity: string, generation: number, id: number | undefined, value: ProjectDraft, baseline: Project | null, initialValue: ProjectDraft) {
  if (generation !== directoryViewGeneration()) return
  const previous = readProjectWorkspace(identity), drafts = new Map(previous.drafts), key = projectDraftKey(id)
  if (JSON.stringify(drafts.get(key)?.value) === JSON.stringify(value)) return
  if (JSON.stringify(value) === JSON.stringify(initialValue)) drafts.delete(key)
  else drafts.set(key, { value: copy(value), baseline: copy(drafts.get(key)?.baseline ?? baseline) })
  workspaces.set(identity, { ...previous, drafts }); emit()
}
export function discardProjectDraft(identity: string, generation: number, id?: number) {
  if (generation !== directoryViewGeneration() || readProjectWorkspace(identity).pending) return
  const previous = readProjectWorkspace(identity), drafts = new Map(previous.drafts)
  drafts.delete(projectDraftKey(id)); workspaces.set(identity, { ...previous, drafts }); emit()
}
export function beginProjectCommand(identity: string, generation: number, command: Pick<ProjectCommand, 'kind' | 'target' | 'body' | 'draft' | 'snapshotVersion'>) {
  if (generation !== directoryViewGeneration() || projectCommandBlocked(identity)) return null
  const previous = readProjectWorkspace(identity)
  const original: ProjectCommand = { ...copy(command), target: Object.freeze(copy(command.target)), body: command.body && Object.freeze(copy(command.body)), id: crypto.randomUUID(), phase: 'pending', startedAt: new Date().toISOString() }
  workspaces.set(identity, { ...previous, pending: original.id, commands: [...previous.commands, original] }); emit(); return original
}
export function settleProjectCommand(identity: string, generation: number, original: ProjectCommand, phase: 'succeeded' | 'rejected' | 'unknown', result?: Project) {
  if (generation !== directoryViewGeneration()) return false
  const previous = readProjectWorkspace(identity)
  if (!previous.commands.some(command => command.id === original.id && (command.phase === 'pending' || command.phase === 'unknown' && command.waitStopped))) return false
  const drafts = new Map(previous.drafts), key = projectDraftKey(original.target.id), value = drafts.get(key)
  if (phase === 'succeeded' && [...previous.commands].reverse().find(command => projectDraftKey(command.target.id) === key)?.id === original.id && value && JSON.stringify(value.value) === JSON.stringify(original.draft)) drafts.delete(key)
  const commands = previous.commands.map(command => command.id === original.id ? { ...command, phase, result: result && copy(result), finishedAt: new Date().toISOString() } : command)
  workspaces.set(identity, { ...previous, drafts, commands, pending: previous.pending === original.id ? null : previous.pending }); emit(); return true
}
// Acknowledging unknown is permission for a NEW command, never proof of the old result.
export function acknowledgeProjectUnknown(identity: string, generation: number, id: string) {
  if (generation !== directoryViewGeneration()) return
  const previous = readProjectWorkspace(identity)
  workspaces.set(identity, { ...previous, commands: previous.commands.map(command => command.id === id && command.phase === 'unknown' ? { ...command, acknowledged: true } : command) }); emit()
}
export function dismissProjectSaved(identity: string, generation: number, id: string) {
  if (generation !== directoryViewGeneration()) return
  const previous = readProjectWorkspace(identity)
  workspaces.set(identity, { ...previous, commands: previous.commands.map(command => command.id === id ? { ...command, dismissed: true } : command) }); emit()
}

export function stopProjectCommandWait(identity: string, generation: number, id: string) {
  if (generation !== directoryViewGeneration()) return
  const previous = readProjectWorkspace(identity)
  if (!previous.commands.some(command => command.id === id && command.phase === 'pending')) return
  workspaces.set(identity, { ...previous, pending: previous.pending === id ? null : previous.pending,
    commands: previous.commands.map(command => command.id === id ? { ...command, phase: 'unknown' as const, waitStopped: true } : command) })
  emit()
}
