import { directoryViewGeneration } from './directoryViewMemory'

export type ContinuationAttempt = Readonly<{
  id: string; identity: string; roomId: number; scope: string; startedAt: string;
  requestKey: string; content: string; replayOf?: string;
  phase: 'sending' | 'unknown' | 'accepted' | 'blocked'; receipt?: Readonly<{ seq: number; eventId: string }>;
}>
const rooms = new Map<number, readonly ContinuationAttempt[]>()
const listeners = new Set<() => void>()
const empty: readonly ContinuationAttempt[] = []
function emit() { listeners.forEach(listener => listener()) }
export function subscribeContinuations(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export function clearContinuations() { rooms.clear(); emit() }
export function roomContinuations(identity: string, roomId: number) {
  const rows = rooms.get(roomId)
  return rows?.[0]?.identity === identity ? rows : empty
}
export function beginContinuation(identity: string, generation: number, roomId: number, scope: string, reviewed: boolean) {
  if (generation !== directoryViewGeneration()) return null
  const rows = roomContinuations(identity, roomId), previous = rows.at(-1)
  if (previous?.phase === 'sending' || previous?.phase === 'unknown' && !reviewed || previous?.phase === 'accepted' && previous.scope === scope || previous?.phase === 'blocked' && previous.scope === scope) return null
  const original = previous?.phase === 'unknown' ? previous : null
  // Never silently turn an old unkeyed unknown request into a new keyed command.
  if (original && (!original.requestKey || typeof original.content !== 'string')) return null
  const attempt: ContinuationAttempt = Object.freeze({ id: crypto.randomUUID(), identity, roomId,
    scope: original?.scope ?? scope, startedAt: new Date().toISOString(), phase: 'sending',
    requestKey: original?.requestKey ?? crypto.randomUUID(),
    content: original?.content ?? 'Continue after reviewing the room activity and current external state.',
    ...(original ? { replayOf: original.id } : {}) })
  rooms.set(roomId, [...rows, attempt]); emit(); return attempt
}
export function settleContinuation(identity: string, generation: number, original: ContinuationAttempt, phase: 'unknown' | 'accepted' | 'blocked', receipt?: ContinuationAttempt['receipt']) {
  if (generation !== directoryViewGeneration()) return
  const rows = roomContinuations(identity, original.roomId)
  if (!rows.some(row => row.id === original.id && row.phase === 'sending')) return
  if (phase === 'accepted' && (!receipt || !Number.isSafeInteger(receipt.seq) || receipt.seq <= 0 || typeof receipt.eventId !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(receipt.eventId))) { phase = 'unknown'; receipt = undefined }
  rooms.set(original.roomId, rows.map(row => row.id === original.id ? Object.freeze({ ...row, phase, receipt: receipt && Object.freeze({ ...receipt }) }) : row)); emit()
}
