import { directoryViewGeneration } from './directoryViewMemory'
export type DraftIndexRow = Readonly<{ room: number; chars: number; preview: string; version: number; observedAt: string }>
const owners = new Map<string, Map<number, DraftIndexRow>>(), reads = new Map<string, number>(), names = new Map<string, Map<number, string>>()
const listeners = new Set<() => void>(); let revision = 0, request = 0
const emit = () => { revision++; listeners.forEach(listener => listener()) }
export const draftIndexRevision = () => revision
export function subscribeDraftIndex(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export function clearDraftIndex() { owners.clear(); reads.clear(); names.clear(); emit() }
export function beginDraftObservation(identity: string, generation: number, room: number) {
  if (generation !== directoryViewGeneration()) return 0
  const id = ++request, key = `${identity}:${room}`; reads.delete(key); reads.set(key, id)
  while (reads.size > 256) reads.delete(reads.keys().next().value!)
  return id
}
export function observeDraft(identity: string, generation: number, room: number, requestId: number, content: string, version: number) {
  if (generation !== directoryViewGeneration() || !requestId || reads.get(`${identity}:${room}`) !== requestId || !Number.isSafeInteger(room) || room < 1 || typeof content !== 'string' || !Number.isSafeInteger(version) || version < 0) return
  const rows = owners.get(identity) || new Map<number, DraftIndexRow>()
  rows.delete(room)
  rows.set(room, Object.freeze({ room, version, chars: content.trim() ? content.length : 0, preview: Array.from(content.replace(/\s+/g, ' ').slice(0, 201)).slice(0, 100).join(''), observedAt: new Date().toISOString() }))
  while (rows.size > 128) rows.delete(rows.keys().next().value!)
  owners.set(identity, rows); emit()
}
export function observedDraftRows(identity: string): readonly DraftIndexRow[] { return [...(owners.get(identity)?.values() || [])] }
export function rememberDraftRoomName(identity: string, generation: number, room: number, name: string) {
  if (generation !== directoryViewGeneration() || typeof name !== 'string') return
  const rows = names.get(identity) || new Map<number, string>(), next = name.slice(0, 200)
  if (rows.get(room) === next) return
  rows.delete(room); rows.set(room, next); while (rows.size > 128) rows.delete(rows.keys().next().value!); names.set(identity, rows); emit()
}
export const draftRoomName = (identity: string, room: number) => names.get(identity)?.get(room) || ''
