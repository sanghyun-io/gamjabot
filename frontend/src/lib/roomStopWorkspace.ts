import { directoryViewGeneration } from './directoryViewMemory'
export type RoomStopState = 'sending' | 'accepted' | 'unknown' | 'notSent' | null
export type RoomStopRecord = Readonly<{ revision: number; id: string | null; phase: RoomStopState; scopeStopId: string | null }>
const initial: RoomStopRecord = Object.freeze({ revision: 0, id: null, phase: null, scopeStopId: null })
const rooms = new Map<number, { identity: string; record: RoomStopRecord }>(), listeners = new Set<() => void>()
function emit() { listeners.forEach(listener => listener()) }
export function subscribeRoomStops(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export function readRoomStop(identity: string, roomId: number) { const row = rooms.get(roomId); return row?.identity === identity ? row.record : initial }
export function clearRoomStops() { rooms.clear(); emit() }
export function beginRoomStop(identity: string, generation: number, roomId: number) {
  if (generation !== directoryViewGeneration()) return null
  const previous = readRoomStop(identity, roomId)
  if (previous.phase === 'sending') return null
  const record: RoomStopRecord = Object.freeze({ revision: previous.revision + 1, id: crypto.randomUUID(), phase: 'sending', scopeStopId: null })
  rooms.set(roomId, { identity, record }); emit(); return record
}
export function settleRoomStop(identity: string, generation: number, roomId: number, id: string, phase: Exclude<RoomStopState, 'sending' | null>) {
  if (generation !== directoryViewGeneration()) return
  const previous = readRoomStop(identity, roomId)
  if (previous.id !== id || previous.phase !== 'sending') return
  rooms.set(roomId, { identity, record: Object.freeze({ ...previous, phase }) }); emit()
}
export function roomSendAllowed(identity: string, roomId: number, revision: number) {
  const current = readRoomStop(identity, roomId)
  return current.phase !== 'sending' && current.revision === revision
}

export function attachScopeStop(identity: string, generation: number, roomId: number, requestId: string, scopeStopId: string) {
  if (generation !== directoryViewGeneration()) return
  const previous = readRoomStop(identity, roomId)
  if (previous.id !== requestId || previous.scopeStopId && previous.scopeStopId !== scopeStopId) return
  rooms.set(roomId, { identity, record: Object.freeze({ ...previous, scopeStopId }) }); emit()
}
