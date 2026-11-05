import type { MemoryCategory, MemoryCommand, MemoryEntry, MemoryLegacyCommand, MemorySource, MemoryWrite, LegacyMemory } from '../api/roomMemories'
import type { MessageKey } from '../i18n/locales/en'
import { directoryViewGeneration } from './directoryViewMemory'

export type MemoryEditorBase = Readonly<{ mode: 'add' | 'edit' | 'remove' | 'replace' | 'disable'; roomRevision: number; entry?: MemoryEntry; legacy?: LegacyMemory }>
export type MemoryDraft = Readonly<{
  content: string; category: MemoryCategory; scope: 'room' | 'agent'; agent: string;
  source: MemorySource['kind']; url: string; eventKey: string;
  event: Readonly<{ event_id: string; seq: number; name?: string }> | null;
}>
export type MemoryEditor = MemoryEditorBase & Readonly<{ id: string; draft: MemoryDraft; initialDraft?: MemoryDraft }>
export type MemoryOperation =
  | Readonly<{ operation: 'add'; body: MemoryWrite }>
  | Readonly<{ operation: 'edit'; memoryId: string; body: MemoryWrite & { expected_revision: number } }>
  | Readonly<{ operation: 'remove'; memoryId: string; body: { expected_room_revision: number; expected_revision: number } }>
  | Readonly<{ operation: 'legacy'; agentId: number; body: MemoryLegacyCommand }>
export type MemoryRequest = Readonly<{
  key: string; editorId: string; intent: MemoryOperation; mode: MemoryEditorBase['mode'];
  beforeContent: string | null; agentName: string | null; targetAgentId?: number | null;
  status: 'sending' | 'accepted' | 'unknown' | 'rejected'; receipt?: MemoryCommand; mismatch?: boolean;
}>
export type MemoryFilters = Readonly<{ query: string; category: MemoryCategory | 'all'; scope: 'all' | 'room' | 'agent' }>
export type MemoryWorkspace = Readonly<{
  filters: MemoryFilters;
  editor: MemoryEditor | null; request: MemoryRequest | null;
  notice: MessageKey | null; conflicted: boolean; changeId: string;
}>
const initial: MemoryWorkspace = Object.freeze({ editor: null, request: null, notice: null, conflicted: false, changeId: '', filters: Object.freeze({ query: '', category: 'all', scope: 'all' }) })
const workspaces = new Map<number, { identity: string; state: MemoryWorkspace }>()
const listeners = new Set<() => void>()
export function readMemoryWorkspace(identity: string, roomId: number) {
  const value = workspaces.get(roomId)
  return value?.identity === identity ? value.state : initial
}
export function subscribeMemoryWorkspaces(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export function clearMemoryWorkspaces() { workspaces.clear(); listeners.forEach(listener => listener()) }
export function updateMemoryWorkspace(identity: string, roomId: number, generation: number, update: (previous: MemoryWorkspace) => MemoryWorkspace) {
  if (generation !== directoryViewGeneration()) return false
  const previous = readMemoryWorkspace(identity, roomId), next = update(previous)
  if (previous === next) return false
  workspaces.set(roomId, { identity, state: Object.freeze(next) }); listeners.forEach(listener => listener()); return true
}
export function memoryWorkspaceLocked(value: MemoryWorkspace) { return value.request?.status === 'sending' || value.request?.status === 'unknown' }
function freezeTree(value: unknown) {
  if (!value || typeof value !== 'object') return
  Object.values(value).forEach(freezeTree); Object.freeze(value)
}
// Detached copies keep original request bodies independent of current form/query objects.
export function snapshotMemory<T extends object>(value: T): T {
  const copy = JSON.parse(JSON.stringify(value)) as T
  freezeTree(copy); return copy
}
export function createMemoryEditor(base: MemoryEditorBase, firstAgent?: number): MemoryEditor {
  const entry = base.entry
  const draft: MemoryDraft = {
    content: entry?.content || '', category: entry?.category || 'fact', scope: entry?.scope || (base.legacy ? 'agent' : 'room'),
    agent: String(entry?.target_agent_id || base.legacy?.agent_id || firstAgent || ''), source: entry?.source_kind || 'manual', url: entry?.source_url || '',
    eventKey: entry?.source_event_id || '', event: entry?.source_event_id && entry.source_seq ? { event_id: entry.source_event_id, seq: entry.source_seq, name: entry.source_snapshot.name } : null,
  }
  return snapshotMemory({ ...base, id: crypto.randomUUID(), draft, initialDraft: draft })
}
