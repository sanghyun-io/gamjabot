import { directoryViewGeneration } from './directoryViewMemory'
import { resultVersionHref } from './resultLink'
export type RecentResultView = Readonly<{ key: string; room: number; id: string; version: number; base: number | null; title: string; openedAt: string; href: string }>
const empty: readonly RecentResultView[] = Object.freeze([]), owners = new Map<string, readonly RecentResultView[]>(), listeners = new Set<() => void>()
const removed = new Map<string, { generation: number; rows: readonly RecentResultView[] }>()
let revision = 0
const emit = () => { revision++; listeners.forEach(listener => listener()) }
export const recentResultRevision = () => revision
export const readRecentResults = (identity: string) => owners.get(identity) || empty
export function subscribeRecentResults(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export function clearRecentResults() { owners.clear(); removed.clear(); emit() }
export function rememberResultView(identity: string, generation: number, value: Pick<RecentResultView, 'room' | 'id' | 'version' | 'base' | 'title'>) {
  if (generation !== directoryViewGeneration() || typeof value.title !== 'string') return
  const href = resultVersionHref(value.room, value.id, value.version, value.base)
  if (!href) return
  const key = `${value.room}:${value.id}:${value.version}`
  const row = Object.freeze({ ...value, title: value.title.slice(0, 200), key, href, openedAt: new Date().toISOString() })
  owners.set(identity, Object.freeze([row, ...readRecentResults(identity).filter(item => item.key !== key)].slice(0, 12))); emit()
}
export function recentResultUndoCount(identity: string, generation: number) {
  const batch = removed.get(identity)
  return generation === directoryViewGeneration() && batch?.generation === generation ? batch.rows.length : 0
}
export function restoreForgottenRecentResults(identity: string, generation: number) {
  const batch = removed.get(identity)
  if (generation !== directoryViewGeneration() || !batch || batch.generation !== generation) return
  // Current entries win over the old snapshot, including newer comparison/context.
  const current = readRecentResults(identity), known = new Set(current.map(row => row.key))
  const merged = [...current, ...batch.rows.filter(row => !known.has(row.key))]
  merged.sort((a, b) => b.openedAt.localeCompare(a.openedAt))
  owners.set(identity, Object.freeze(merged.slice(0, 12)))
  removed.delete(identity); emit()
}
export function forgetRecentResult(identity: string, generation: number, key?: string) {
  if (generation !== directoryViewGeneration()) return
  const current = readRecentResults(identity)
  const batch = key === undefined ? current : current.filter(item => item.key === key)
  if (!batch.length) return
  // One bounded undo batch per Owner; another explicit removal replaces it.
  removed.set(identity, { generation, rows: Object.freeze([...batch]) })
  if (key === undefined) owners.delete(identity)
  else owners.set(identity, Object.freeze(current.filter(item => item.key !== key)))
  emit()
}
