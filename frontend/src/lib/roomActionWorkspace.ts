import type { Conversation, RoomMember } from '../api/conversations'
import type { RoomSchedule } from '../api/schedules'
import { directoryViewGeneration } from './directoryViewMemory'

export type RoomActionDraft = { title: string; baselineTitle: string; titleVersion: number; cloneTitle: string; targetProject: number; intent: string; open: boolean }
export type RoomClonePreview = { members: RoomMember[]; schedule: RoomSchedule; membersReceivedAt: number; scheduleReceivedAt: number }
export type RoomActionCommand = Readonly<{
  id: string; kind: 'rename' | 'clone'; source: Conversation; body: { title: string; project_id?: number }; targetProjectName?: string
  preview?: RoomClonePreview; rawTitle: string; titleVersion: number; fingerprint: string; signature?: string
  phase: 'pending' | 'succeeded' | 'rejected' | 'unknown'; startedAt: string; finishedAt?: string; result?: Conversation; code?: string; acknowledged?: boolean; waitStopped?: boolean
}>
export type RoomActionRecord = Readonly<{ room: Conversation; draft: RoomActionDraft }>
type Workspace = Readonly<{ drafts: ReadonlyMap<number, RoomActionRecord>; commands: readonly RoomActionCommand[]; pending: string | null }>
const initial: Workspace = { drafts: new Map(), commands: [], pending: null }
const rows = new Map<string, Workspace>(), listeners = new Set<() => void>()
let version = 0
const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
const emit = () => { version++; listeners.forEach(listener => listener()) }
export const roomActionVersion = () => version
export function subscribeRoomActions(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export const readRoomActions = (identity: string) => rows.get(identity) || initial
export function clearRoomActions() { rows.clear(); emit() }
export function defaultRoomAction(room: Conversation): RoomActionDraft { return { title: room.title, baselineTitle: room.title, titleVersion: 0, cloneTitle: '', targetProject: room.project_id, intent: crypto.randomUUID(), open: false } }
export function initializeRoomAction(identity: string, generation: number, room: Conversation) {
  if (generation !== directoryViewGeneration() || readRoomActions(identity).drafts.has(room.id)) return
  const previous = readRoomActions(identity), drafts = new Map(previous.drafts)
  drafts.set(room.id, { room: copy(room), draft: defaultRoomAction(room) }); rows.set(identity, { ...previous, drafts }); emit()
}
export function patchRoomAction(identity: string, generation: number, id: number, patch: Partial<RoomActionDraft>) {
  if (generation !== directoryViewGeneration()) return false
  const previous = readRoomActions(identity), record = previous.drafts.get(id)
  if (!record || previous.pending && Object.keys(patch).some(key => key !== 'open')) return false
  const draft = { ...record.draft, ...copy(patch) }
  if (patch.title !== undefined && patch.title !== record.draft.title) draft.titleVersion = record.draft.titleVersion + 1
  if (JSON.stringify(draft) === JSON.stringify(record.draft)) return true
  const drafts = new Map(previous.drafts); drafts.set(id, { ...record, draft }); rows.set(identity, { ...previous, drafts }); emit(); return true
}
export function roomActionBlocked(identity: string) { const value = readRoomActions(identity); return !!value.pending || value.commands.some(command => command.phase === 'unknown' && !command.acknowledged) }
export function roomActionFingerprint(draft: RoomActionDraft) { const { open: _view, ...content } = draft; return JSON.stringify(content) }
export function roomCloneSignature(roomId: number, draft: RoomActionDraft, preview: RoomClonePreview) { return JSON.stringify({ roomId, projectId: draft.targetProject, title: draft.cloneTitle.trim(), intent: draft.intent, members: preview.members.map(value => value.agent_id).sort((a,b)=>a-b), policy: preview.schedule }) }
export function returnedRoomClone(identity: string, signature: string) { return [...readRoomActions(identity).commands].reverse().find(command => command.kind === 'clone' && command.signature === signature && command.phase === 'succeeded' && command.result) }
export function beginRoomAction(identity: string, generation: number, source: Conversation, kind: 'rename' | 'clone', preview?: RoomClonePreview, targetProjectName?: string) {
  if (generation !== directoryViewGeneration() || roomActionBlocked(identity)) return null
  const previous = readRoomActions(identity), record = previous.drafts.get(source.id)
  if (!record || kind === 'clone' && !preview) return null
  const draft = record.draft, title = (kind === 'rename' ? draft.title : draft.cloneTitle).trim()
  if (!title || title.length > 200) return null
  const signature = kind === 'clone' ? roomCloneSignature(source.id, draft, preview!) : undefined
  if (signature && returnedRoomClone(identity, signature)) return null
  const command: RoomActionCommand = { id: crypto.randomUUID(), kind, source: copy(source), body: Object.freeze({ title, ...(kind === 'clone' ? { project_id: draft.targetProject } : {}) }), targetProjectName,
    preview: preview && copy(preview), rawTitle: draft.title, titleVersion: draft.titleVersion, fingerprint: roomActionFingerprint(draft), signature, phase: 'pending', startedAt: new Date().toISOString() }
  rows.set(identity, { ...previous, pending: command.id, commands: [...previous.commands, command] }); emit(); return command
}
export function settleRoomAction(identity: string, generation: number, original: RoomActionCommand, outcome: Pick<RoomActionCommand, 'phase' | 'result' | 'code'>) {
  if (generation !== directoryViewGeneration()) return false
  const previous = readRoomActions(identity)
  if (!previous.commands.some(command => command.id === original.id && (command.phase === 'pending' || command.phase === 'unknown' && command.waitStopped))) return false
  const drafts = new Map(previous.drafts), record = drafts.get(original.source.id)
  const latestRename = [...previous.commands].reverse().find(command => command.kind === 'rename' && command.source.id === original.source.id)
  if (original.kind === 'rename' && outcome.phase === 'succeeded' && outcome.result && record && latestRename?.id === original.id && record.draft.titleVersion === original.titleVersion && record.draft.title === original.rawTitle) {
    drafts.set(original.source.id, { room: copy(outcome.result), draft: { ...record.draft, title: outcome.result.title, baselineTitle: outcome.result.title } })
  }
  rows.set(identity, { ...previous, drafts, pending: previous.pending === original.id ? null : previous.pending, commands: previous.commands.map(command => command.id === original.id ? { ...command, ...copy(outcome), finishedAt: new Date().toISOString() } : command) }); emit(); return true
}
export function stopRoomActionWait(identity: string, generation: number, id: string) {
  if (generation !== directoryViewGeneration()) return
  const previous = readRoomActions(identity)
  if (!previous.commands.some(command => command.id === id && command.phase === 'pending')) return
  rows.set(identity, { ...previous, pending: previous.pending === id ? null : previous.pending, commands: previous.commands.map(command => command.id === id ? { ...command, phase: 'unknown' as const, waitStopped: true } : command) }); emit()
}
export function acknowledgeRoomAction(identity: string, generation: number, id: string) {
  if (generation !== directoryViewGeneration()) return
  const previous = readRoomActions(identity)
  rows.set(identity, { ...previous, commands: previous.commands.map(command => command.id === id && command.phase === 'unknown' ? { ...command, acknowledged: true } : command) }); emit()
}
