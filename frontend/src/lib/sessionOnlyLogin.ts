// This non-secret flag lives only in the current WebView's session storage.
// An unavailable or malformed store suppresses automatic restoration.
const key = 'gamja.session-only-login.v1'
export type SessionOnlyStore = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>
export function suppressAutomaticRestore(): boolean {
  try { const value = sessionStorage.getItem(key); return value !== null } catch { return true }
}
export function markSessionOnly(store?: SessionOnlyStore): boolean {
  try { const target = store ?? sessionStorage; target.setItem(key, '1'); return target.getItem(key) === '1' } catch { return false }
}
export function clearSessionOnly(store?: SessionOnlyStore): void {
  try { (store ?? sessionStorage).removeItem(key) } catch { /* leaving suppression in place is safe */ }
}
