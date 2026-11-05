import type { Project } from '../api/projects'
import type { Conversation, RoomLaunch } from '../api/conversations'
import type { RoomTemplate, RoomTemplateWrite } from '../api/roomTemplates'
import { directoryViewGeneration } from './directoryViewMemory'

export type RoomSetupDraft = {
  title: string; selected: number[]; rounds: string; tools: string; seconds: string
  templatesOpen: boolean; templateId: number | null; templateName: string; templateRounds: string; templateTools: string; templateSeconds: string
  templateBaseline: RoomTemplate | null; intent: string
}
export type RoomSetupKind = 'launch' | 'template-create' | 'template-update' | 'template-delete' | 'template-room'
export type RoomSetupCommand = Readonly<{
  id: string; kind: RoomSetupKind; target: Readonly<{ projectId: number; projectName: string; templateId?: number; name: string; title: string }>
  body?: RoomLaunch | RoomTemplateWrite; key?: string; template?: RoomTemplate; draft: RoomSetupDraft; draftFingerprint: string; roomSignature?: string; retryOf?: string
  phase: 'pending' | 'succeeded' | 'rejected' | 'unknown'; startedAt: string; finishedAt?: string; result?: Conversation | RoomTemplate; code?: string
  acknowledged?: boolean; waitStopped?: boolean
}>
type DraftRecord = Readonly<{ project: Project; value: RoomSetupDraft }>
type Workspace = Readonly<{ drafts: ReadonlyMap<number, DraftRecord>; commands: readonly RoomSetupCommand[]; pending: string | null }>
const initial: Workspace = { drafts: new Map(), commands: [], pending: null }
const rows = new Map<string, Workspace>(), listeners = new Set<() => void>()
let version = 0
const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
const emit = () => { version++; listeners.forEach(listener => listener()) }
export const roomSetupVersion = () => version
export function subscribeRoomSetup(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export const readRoomSetup = (identity: string) => rows.get(identity) || initial
export function clearRoomSetups() { rows.clear(); emit() }
export function defaultRoomSetup(project: Project): RoomSetupDraft {
  return { title: '', selected: (project.agents || []).filter(agent => agent.is_active).map(agent => agent.id).sort((a, b) => a - b), rounds: '2', tools: '8', seconds: '60',
    templatesOpen: false, templateId: null, templateName: '', templateRounds: '2', templateTools: '8', templateSeconds: '60', templateBaseline: null, intent: crypto.randomUUID() }
}
export function initializeRoomSetup(identity: string, generation: number, project: Project) {
  if (generation !== directoryViewGeneration() || readRoomSetup(identity).drafts.has(project.id)) return
  const previous = readRoomSetup(identity), drafts = new Map(previous.drafts)
  drafts.set(project.id, { project: copy(project), value: defaultRoomSetup(project) }); rows.set(identity, { ...previous, drafts }); emit()
}
export function roomSetupFingerprint(draft: RoomSetupDraft) {
  const { templatesOpen: _viewOnly, ...content } = draft
  return JSON.stringify(content)
}
export function patchRoomSetup(identity: string, generation: number, projectId: number, patch: Partial<RoomSetupDraft>) {
  if (generation !== directoryViewGeneration()) return false
  const previous = readRoomSetup(identity), record = previous.drafts.get(projectId)
  if (!record || previous.pending && Object.keys(patch).some(key => key !== 'templatesOpen')) return false
  const value = { ...record.value, ...copy(patch) }
  if (JSON.stringify(value) === JSON.stringify(record.value)) return true
  const drafts = new Map(previous.drafts); drafts.set(projectId, { ...record, value }); rows.set(identity, { ...previous, drafts }); emit(); return true
}
export function roomSetupBlocked(identity: string, except?: string) {
  const value = readRoomSetup(identity)
  return !!value.pending || value.commands.some(command => command.phase === 'unknown' && !command.acknowledged && command.id !== except)
}
export function roomCreationSignature(kind: 'launch' | 'template-room', projectId: number, intent: string, body?: RoomLaunch, template?: RoomTemplate) {
  return JSON.stringify({ kind, projectId, intent, body: kind === 'launch' ? body : { templateId: template?.id, preview: template && { title: template.room_title, agents: [...template.agent_ids].sort((a,b)=>a-b), rounds: template.max_rounds, tools: template.max_tool_calls, seconds: template.max_executor_seconds, updatedAt: template.updated_at } } })
}
export function createdFromRoomSetup(identity: string, signature: string) {
  return [...readRoomSetup(identity).commands].reverse().find(command => command.phase === 'succeeded' && command.roomSignature === signature && command.result && 'title' in command.result)
}
export function beginRoomSetupCommand(identity: string, generation: number, project: Project, kind: RoomSetupKind, body?: RoomLaunch | RoomTemplateWrite, template?: RoomTemplate, retryOf?: string) {
  if (generation !== directoryViewGeneration()) return null
  const previous = readRoomSetup(identity), record = previous.drafts.get(project.id)
  const retry = retryOf ? previous.commands.find(command => command.id === retryOf && command.kind === 'launch' && ['unknown', 'rejected'].includes(command.phase) && command.key && command.body && command.code !== 'room_launch_idempotency_conflict') : undefined
  if (!record || retryOf && (!retry || kind !== 'launch' || retry.target.projectId !== project.id) || roomSetupBlocked(identity, retry?.id)) return null
  const draft = copy(retry?.draft || record.value), payload = copy(retry?.body || body || null) || undefined
  const signature = kind === 'launch' || kind === 'template-room' ? roomCreationSignature(kind, project.id, draft.intent, payload as RoomLaunch | undefined, template) : undefined
  if (!retry && signature && createdFromRoomSetup(identity, signature)) return null
  const original: RoomSetupCommand = { id: crypto.randomUUID(), kind, target: Object.freeze({ projectId: project.id, projectName: project.name, templateId: template?.id,
    name: payload && 'name' in payload ? payload.name : template?.name || '', title: payload && 'title' in payload ? payload.title : payload && 'room_title' in payload ? payload.room_title : template?.room_title || '' }),
    body: payload, key: kind === 'launch' ? retry?.key || crypto.randomUUID() : undefined, template: template && copy(template), draft,
    draftFingerprint: roomSetupFingerprint(draft), roomSignature: signature, retryOf: retry?.id, phase: 'pending', startedAt: new Date().toISOString() }
  const commands = previous.commands.map(command => command.id === retry?.id ? { ...command, acknowledged: true } : command)
  rows.set(identity, { ...previous, pending: original.id, commands: [...commands, original] }); emit(); return original
}
export function settleRoomSetupCommand(identity: string, generation: number, original: RoomSetupCommand, outcome: Pick<RoomSetupCommand, 'phase' | 'result' | 'code'>) {
  if (generation !== directoryViewGeneration()) return false
  const previous = readRoomSetup(identity)
  if (!previous.commands.some(command => command.id === original.id && (command.phase === 'pending' || command.phase === 'unknown' && command.waitStopped))) return false
  rows.set(identity, { ...previous, pending: previous.pending === original.id ? null : previous.pending,
    commands: previous.commands.map(command => command.id === original.id ? { ...command, ...copy(outcome), finishedAt: new Date().toISOString() } : command) }); emit(); return true
}
export function stopRoomSetupWait(identity: string, generation: number, id: string) {
  if (generation !== directoryViewGeneration()) return
  const previous = readRoomSetup(identity)
  if (!previous.commands.some(command => command.id === id && command.phase === 'pending')) return
  rows.set(identity, { ...previous, pending: previous.pending === id ? null : previous.pending,
    commands: previous.commands.map(command => command.id === id ? { ...command, phase: 'unknown' as const, waitStopped: true } : command) }); emit()
}
export function acknowledgeRoomSetupUnknown(identity: string, generation: number, id: string) {
  if (generation !== directoryViewGeneration()) return
  const previous = readRoomSetup(identity)
  rows.set(identity, { ...previous, commands: previous.commands.map(command => command.id === id && command.phase === 'unknown' ? { ...command, acknowledged: true } : command) }); emit()
}
export function applyTemplateToRoomSetup(identity: string, generation: number, template: RoomTemplate) {
  return patchRoomSetup(identity, generation, template.project_id, { title: template.room_title, selected: [...template.agent_ids], templateId: template.id, templateBaseline: template,
    templateName: template.name, templateRounds: String(template.max_rounds), templateTools: String(template.max_tool_calls), templateSeconds: String(template.max_executor_seconds), templatesOpen: true })
}
