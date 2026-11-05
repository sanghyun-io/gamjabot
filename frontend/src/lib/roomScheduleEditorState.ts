import type { RoomSchedule } from '../api/schedules'
import { directoryViewGeneration } from './directoryViewMemory'

export type DraftSchedule = Record<keyof RoomSchedule, string>
export type ScheduleEditor = Readonly<{
  draft: DraftSchedule | null; revision: number; dirty: boolean;
  phase: 'idle' | 'saving' | 'unknown' | 'rejected'; operation: string | null;
  inspecting: boolean; saved: boolean; validationError: boolean;
}>
const initial: ScheduleEditor = Object.freeze({ draft: null, revision: 0, dirty: false,
  phase: 'idle', operation: null, inspecting: false, saved: false, validationError: false })
const editors = new Map<number, { identity: string; state: ScheduleEditor }>()
const listeners = new Set<() => void>()
export function readScheduleEditor(identity: string, roomId: number) {
  const editor = editors.get(roomId)
  return editor?.identity === identity ? editor.state : initial
}
export function subscribeScheduleEditors(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export function clearScheduleEditors() { editors.clear(); listeners.forEach(listener => listener()) }
export function updateScheduleEditor(identity: string, roomId: number, generation: number,
  update: (state: ScheduleEditor) => ScheduleEditor) {
  if (generation !== directoryViewGeneration()) return false
  const previous = readScheduleEditor(identity, roomId), next = update(previous)
  if (previous === next) return false
  editors.set(roomId, { identity, state: Object.freeze(next) })
  listeners.forEach(listener => listener())
  return true
}
