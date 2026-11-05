type Entry = { identity: string; value: unknown }
const entries = new Map<string, Entry>()
let generation = 0

// UI context only: never persisted, and cleared with the existing Owner data boundary.
export function clearDirectoryViewMemory() { generation += 1; entries.clear() }
export function directoryViewGeneration() { return generation }
export function readDirectoryView<T>(identity: string, key: string, fallback: T): T {
  const entry = entries.get(key)
  return entry?.identity === identity ? entry.value as T : fallback
}
export function writeDirectoryView(identity: string, key: string, value: unknown, expectedGeneration: number) {
  if (generation !== expectedGeneration) return false
  entries.delete(key)
  entries.set(key, { identity, value })
  // Returning to a recent screen is useful; retaining unlimited UI history is not.
  while (entries.size > 128) entries.delete(entries.keys().next().value!)
  return true
}
