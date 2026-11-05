import type { AvatarConfig } from './avatarRecipe'
import type { Agent, AgentInput } from '../api/agents'
import { directoryViewGeneration } from './directoryViewMemory'

export type AgentDraft = { avatar?: AvatarConfig; name: string; description: string; systemPrompt: string; connectionId: number; modelId: string; temperature: string; maxTokens: number }
export type AgentDraftRecord = Readonly<{ value: AgentDraft; baseline: Agent | null }>
export type AgentCommand = Readonly<{
  id: string; kind: 'create' | 'update' | 'delete'; target: Readonly<{ id?: number; name: string }>; body?: Readonly<AgentInput>
  draft?: AgentDraft; startedAt: string; finishedAt?: string; snapshotVersion: number
  phase: 'pending' | 'succeeded' | 'rejected' | 'unknown'; result?: Agent; acknowledged?: boolean; dismissed?: boolean; waitStopped?: boolean
}>
type Workspace = Readonly<{ drafts: ReadonlyMap<string, AgentDraftRecord>; commands: readonly AgentCommand[]; pending: string | null }>
const initial: Workspace = { drafts: new Map(), commands: [], pending: null }
const workspaces = new Map<string, Workspace>(), listeners = new Set<() => void>()
let version = 0
const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
const emit = () => { version++; listeners.forEach(listener => listener()) }
export const agentWorkspaceVersion = () => version
export function subscribeAgentWorkspace(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export const readAgentWorkspace = (identity: string) => workspaces.get(identity) || initial
export const agentDraftKey = (id?: number) => id === undefined ? 'new' : String(id)
export const agentCommandBlocked = (identity: string) => {
  const value = readAgentWorkspace(identity)
  return !!value.pending || value.commands.some(command => command.phase === 'unknown' && !command.acknowledged)
}
export function clearAgentWorkspaces() { workspaces.clear(); emit() }
export function keepAgentDraft(identity: string, generation: number, id: number | undefined, value: AgentDraft, baseline: Agent | null, initialValue: AgentDraft) {
  if (generation !== directoryViewGeneration()) return
  const previous = readAgentWorkspace(identity), drafts = new Map(previous.drafts), key = agentDraftKey(id)
  if (JSON.stringify(drafts.get(key)?.value) === JSON.stringify(value)) return
  if (JSON.stringify(value) === JSON.stringify(initialValue)) drafts.delete(key)
  else drafts.set(key, { value: copy(value), baseline: copy(drafts.get(key)?.baseline ?? baseline) })
  workspaces.set(identity, { ...previous, drafts }); emit()
}
export function discardAgentDraft(identity: string, generation: number, id?: number) {
  if (generation !== directoryViewGeneration() || readAgentWorkspace(identity).pending) return
  const previous = readAgentWorkspace(identity), drafts = new Map(previous.drafts)
  drafts.delete(agentDraftKey(id)); workspaces.set(identity, { ...previous, drafts }); emit()
}
export function beginAgentCommand(identity: string, generation: number, command: Pick<AgentCommand, 'kind' | 'target' | 'body' | 'draft' | 'snapshotVersion'>) {
  if (generation !== directoryViewGeneration() || agentCommandBlocked(identity)) return null
  const previous = readAgentWorkspace(identity)
  const original: AgentCommand = { ...copy(command), target: Object.freeze(copy(command.target)), body: command.body && Object.freeze(copy(command.body)), id: crypto.randomUUID(), phase: 'pending', startedAt: new Date().toISOString() }
  workspaces.set(identity, { ...previous, pending: original.id, commands: [...previous.commands, original] }); emit(); return original
}
export function settleAgentCommand(identity: string, generation: number, original: AgentCommand, phase: 'succeeded' | 'rejected' | 'unknown', result?: Agent) {
  if (generation !== directoryViewGeneration()) return false
  const previous = readAgentWorkspace(identity)
  if (!previous.commands.some(command => command.id === original.id && (command.phase === 'pending' || command.phase === 'unknown' && command.waitStopped))) return false
  const drafts = new Map(previous.drafts), key = agentDraftKey(original.target.id), value = drafts.get(key)
  if (phase === 'succeeded' && [...previous.commands].reverse().find(command => agentDraftKey(command.target.id) === key)?.id === original.id && original.kind !== 'delete' && value && JSON.stringify(value.value) === JSON.stringify(original.draft)) drafts.delete(key)
  const commands = previous.commands.map(command => command.id === original.id ? { ...command, phase, result: result && copy(result), finishedAt: new Date().toISOString() } : command)
  workspaces.set(identity, { ...previous, drafts, commands, pending: previous.pending === original.id ? null : previous.pending }); emit(); return true
}
// Acknowledging unknown is permission for a NEW command, never proof of the old result.
export function acknowledgeAgentUnknown(identity: string, generation: number, id: string) {
  if (generation !== directoryViewGeneration()) return
  const previous = readAgentWorkspace(identity)
  workspaces.set(identity, { ...previous, commands: previous.commands.map(command => command.id === id && command.phase === 'unknown' ? { ...command, acknowledged: true } : command) }); emit()
}
export function dismissAgentSaved(identity: string, generation: number, id: string) {
  if (generation !== directoryViewGeneration()) return
  const previous = readAgentWorkspace(identity)
  workspaces.set(identity, { ...previous, commands: previous.commands.map(command => command.id === id ? { ...command, dismissed: true } : command) }); emit()
}

export function stopAgentCommandWait(identity: string, generation: number, id: string) {
  if (generation !== directoryViewGeneration()) return
  const previous = readAgentWorkspace(identity)
  if (!previous.commands.some(command => command.id === id && command.phase === 'pending')) return
  workspaces.set(identity, { ...previous, pending: previous.pending === id ? null : previous.pending,
    commands: previous.commands.map(command => command.id === id ? { ...command, phase: 'unknown' as const, waitStopped: true } : command) })
  emit()
}
