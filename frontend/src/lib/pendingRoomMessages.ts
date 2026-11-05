import { sameReply, validReply, sameRoutineReference, type RoutineReference, type ReplyReference, type RoomEvent } from '../api/events'
import { directoryViewGeneration } from './directoryViewMemory'

export type PendingRoomMessage = Readonly<{
  identity: string; roomId: number; key: string; content: string; sourceContent: string; sourceEditId: string;
  outputKind: 'auto' | 'text' | 'image_generation' | 'speech_generation'; draftVersion: number;
  replyTo?: ReplyReference | null; routineRef?:RoutineReference|null; stopRevision?: number; afterSeq: number; status: 'sending' | 'unknown' | 'confirmed'; parked: boolean;
  receipt?: RoomEvent; mismatch?: boolean; settlementFailed?: boolean;
}>
const rooms = new Map<number, readonly PendingRoomMessage[]>()
const listeners = new Set<() => void>()
const empty: readonly PendingRoomMessage[] = []
function emit() { listeners.forEach(listener => listener()) }
export function subscribePendingMessages(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export function clearPendingMessages() { rooms.clear(); emit() }
export function pendingRoomMessages(identity: string, roomId: number): readonly PendingRoomMessage[] {
  const items = rooms.get(roomId)
  return items?.[0]?.identity === identity ? items : empty
}
export function blocksNewMessage(items: readonly PendingRoomMessage[]) {
  return items.some(item => item.status === 'sending' || !item.parked)
}
export function freezePendingMessage(item: PendingRoomMessage, generation: number) {
  if (directoryViewGeneration() !== generation) throw new Error('Owner changed')
  const items = pendingRoomMessages(item.identity, item.roomId)
  if (blocksNewMessage(items) || items.some(prior => prior.key === item.key)) throw new Error('Resolve the existing send first')
  rooms.set(item.roomId, [...items, Object.freeze(item)]); emit()
}
export function updatePendingMessage(identity: string, roomId: number, key: string,
  changes: Partial<Pick<PendingRoomMessage, 'status' | 'parked' | 'receipt' | 'afterSeq' | 'mismatch' | 'settlementFailed'>>, generation: number) {
  if (directoryViewGeneration() !== generation) return
  const items = pendingRoomMessages(identity, roomId)
  if (!items.some(item => item.key === key)) return
  rooms.set(roomId, items.map(item => item.key === key ? Object.freeze({ ...item, ...changes }) : item)); emit()
}
export function consumeConfirmedMessage(identity: string, roomId: number, key: string, generation: number) {
  if (directoryViewGeneration() !== generation) return
  const items = pendingRoomMessages(identity, roomId)
  rooms.set(roomId, items.filter(item => item.key !== key || item.status !== 'confirmed')); emit()
}
export function rejectPendingMessage(identity:string,roomId:number,key:string,generation:number) {
  if(directoryViewGeneration()!==generation)return
  const items=pendingRoomMessages(identity,roomId)
  rooms.set(roomId,items.filter(item=>item.key!==key||item.status==='confirmed'));emit()
}
export function matchesPendingMessage(item: PendingRoomMessage, event: RoomEvent, ownerId: number) {
  return event.room_id === item.roomId && event.kind === 'message' && event.actor_type === 'user' && event.actor_id === ownerId
    && event.idempotency_key === item.key && event.payload.content === item.content
    && sameRoutineReference(item.routineRef,event.payload.routine_ref)
    && (event.payload.output_kind ?? 'auto') === item.outputKind
    && (event.payload.reply_to == null || validReply(event.payload.reply_to)) && sameReply(item.replyTo, event.payload.reply_to as ReplyReference | null | undefined)
}
