import type { RoomEvent } from '../api/events'
import type { HandoffNoteWrite } from '../api/summaries'
import { directoryViewGeneration } from './directoryViewMemory'

export type HandoffEndpoint = Readonly<{ seq: number; eventId: string }>
export type HandoffSelection = Readonly<{ start: HandoffEndpoint; end: HandoffEndpoint }>
export type HandoffRequest = Readonly<{
  key: string; body: Readonly<HandoffNoteWrite>; selection: HandoffSelection;
  fingerprint: string; revision: number; afterSeq: number;
  status: 'sending' | 'unknown' | 'busy' | 'rejected' | 'confirmed';
  receipt?: RoomEvent; mismatch?: boolean; acknowledged?: boolean; lookup?: '' | 'absent' | 'failure';
}>
export type HandoffEditor = Readonly<{
  content: string; selection: HandoffSelection | null; revision: number; dirty: boolean;
  validationError: boolean; readingKey: string | null; requests: readonly HandoffRequest[];
}>
const initial: HandoffEditor = Object.freeze({ content: '', selection: null, revision: 0,
  dirty: false, validationError: false, readingKey: null, requests: [] })
const editors = new Map<number, { identity: string; state: HandoffEditor }>()
const listeners = new Set<() => void>()
export function readHandoffEditor(identity: string, roomId: number) {
  const editor = editors.get(roomId)
  return editor?.identity === identity ? editor.state : initial
}
export function subscribeHandoffEditors(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export function clearHandoffEditors() { editors.clear(); listeners.forEach(listener => listener()) }
export function updateHandoffEditor(identity: string, roomId: number, generation: number,
  update: (state: HandoffEditor) => HandoffEditor) {
  if (generation !== directoryViewGeneration()) return false
  const previous = readHandoffEditor(identity, roomId), next = update(previous)
  if (previous === next) return false
  editors.set(roomId, { identity, state: Object.freeze(next) })
  listeners.forEach(listener => listener())
  return true
}
export function handoffFingerprint(selection: HandoffSelection, content: string) {
  return JSON.stringify([selection.start.seq, selection.start.eventId, selection.end.seq, selection.end.eventId, content])
}
export function matchesHandoffReceipt(request: HandoffRequest, event: RoomEvent, roomId: number) {
  return event.room_id === roomId && Number.isSafeInteger(event.seq) && event.seq > 0
    && typeof event.event_id === 'string' && !!event.event_id && event.actor_type === 'system'
    && event.actor_id === null && event.kind === 'summary' && event.idempotency_key === request.key
    && event.payload.owner_checkpoint === true && event.payload.content === request.body.content
    && event.payload.source_start_seq === request.body.source_start_seq
    && event.payload.source_end_seq === request.body.source_end_seq
}
