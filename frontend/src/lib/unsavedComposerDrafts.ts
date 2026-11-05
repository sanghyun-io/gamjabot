import { directoryViewGeneration } from './directoryViewMemory'

// Unsaved input is not a regenerable view preference: do not evict it with the UI LRU.
// Keep it only in this app process until acknowledged by the draft API or logout.
const drafts = new Map<number, { identity: string; content: string; editId: string; updatedAt: string }>()
const listeners = new Set<() => void>(); let revision = 0
const emit = () => { revision++; listeners.forEach(listener => listener()) }
export const unsavedDraftRevision = () => revision
export function subscribeUnsavedDrafts(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export function listUnsavedDrafts(identity: string) { return [...drafts].filter(([,row]) => row.identity === identity).map(([room,row]) => ({ room, content: row.content, editId: row.editId, updatedAt: row.updatedAt })) }
export function clearUnsavedComposerDrafts() { drafts.clear(); emit() }
export function readUnsavedComposerDraft(identity: string, roomId: number): string | null {
  const draft = drafts.get(roomId)
  return draft?.identity === identity ? draft.content : null
}
export function readUnsavedComposerEditId(identity: string, roomId: number) {
  const draft = drafts.get(roomId)
  return draft?.identity === identity ? draft.editId : null
}
export function keepUnsavedComposerDraft(identity: string, roomId: number, content: string, generation: number, editId?: string) {
  const prior = drafts.get(roomId)
  if (directoryViewGeneration() !== generation) return
  const nextId = editId ?? (prior?.identity === identity && prior.content === content ? prior.editId : crypto.randomUUID())
  if (prior?.identity === identity && prior.content === content && prior.editId === nextId) return
  drafts.set(roomId, { identity, content, editId: nextId, updatedAt: new Date().toISOString() }); emit()
}
export function acknowledgeComposerDraft(identity: string, roomId: number, content: string, generation: number, editId?: string) {
  if (directoryViewGeneration() !== generation) return
  const draft = drafts.get(roomId)
  if (draft?.identity === identity && draft.content === content && (!editId || draft.editId === editId)) { drafts.delete(roomId); emit() }
}
