type CleanupStatus = 'pending' | 'cleared' | 'unavailable'
type CleanupState = Readonly<{ status: CleanupStatus; attempts: number; dismissed: boolean }>
let state: CleanupState = { status: 'pending', attempts: 0, dismissed: false }
const listeners = new Set<() => void>()
const update = (next: CleanupState) => { state = next; for (const listener of listeners) listener() }
export const subscribeLegacyAuthCleanup = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener) } }
export const legacyAuthCleanupSnapshot = () => state
export const dismissLegacyAuthCleanup = () => update({ ...state, dismissed: true })

// Keep the original exact legacy-key removal, but do not let a throwing
// localStorage getter/removeItem abort eager module loading before React exists.
// Never read the key, fall back to it, log an exception, or clear other storage.
export function clearLegacyAuthStorage() {
  let status: CleanupStatus = 'unavailable'
  try {
    const storage = typeof window === 'undefined' ? null : window.localStorage
    if (storage) {
      storage.removeItem('auth-storage')
      status = 'cleared'
    }
  } catch { /* Keep cleanup explicitly unconfirmed; current auth stays in memory. */ }
  update({ status, attempts: state.attempts + 1, dismissed: false })
  return status
}
