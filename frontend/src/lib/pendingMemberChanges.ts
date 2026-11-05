import { directoryViewGeneration } from './directoryViewMemory'

export type MemberAction = 'add' | 'remove' | 'pause' | 'resume'
export type MemberChange = Readonly<{
  id: string; identity: string; roomId: number; agentId: number; agentName: string; action: MemberAction;
  status: 'sending' | 'refreshing' | 'accepted' | 'rejected' | 'unknown';
  needsRead: boolean; inspected: boolean; acknowledged: boolean; reason?: string;
}>
const rooms = new Map<number, readonly MemberChange[]>()
const listeners = new Set<() => void>()
const empty: readonly MemberChange[] = []
const emit = () => listeners.forEach(listener => listener())
export function subscribeMemberChanges(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export function memberChanges(identity: string, roomId: number): readonly MemberChange[] {
  const rows = rooms.get(roomId)
  return rows?.[0]?.identity === identity ? rows : empty
}
export function clearMemberChanges() { rooms.clear(); emit() }
export function blocksMemberChanges(rows: readonly MemberChange[]) {
  return rows.some(row => row.status === 'sending' || row.status === 'refreshing' || row.needsRead || row.status === 'unknown' && !row.acknowledged)
}
export function startMemberChange(row: MemberChange, generation: number) {
  if (directoryViewGeneration() !== generation || blocksMemberChanges(memberChanges(row.identity, row.roomId))) return false
  // Uncertain outcomes stay visible for this login session, even after explicit review.
  const earlier = memberChanges(row.identity, row.roomId).filter(item => item.status === 'unknown')
  rooms.set(row.roomId, [...earlier, Object.freeze(row)]); emit(); return true
}
export function updateMemberChange(identity: string, roomId: number, id: string,
  patch: Partial<Pick<MemberChange, 'status' | 'needsRead' | 'inspected' | 'acknowledged' | 'reason'>>, generation: number) {
  if (directoryViewGeneration() !== generation) return
  const rows = memberChanges(identity, roomId)
  if (!rows.some(row => row.id === id)) return
  rooms.set(roomId, rows.map(row => row.id === id ? Object.freeze({ ...row, ...patch }) : row)); emit()
}
