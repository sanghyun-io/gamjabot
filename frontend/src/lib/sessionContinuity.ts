import axios, { CanceledError } from 'axios'
import { useAuthStore } from '../stores/authStore'

type AuthSnapshot = Pick<ReturnType<typeof useAuthStore.getState>, 'epoch' | 'token' | 'user'>
interface Endpoint { base_url: string; session_token: string }
interface Flight { epoch: number; owner: number; token: string; promise: Promise<string> }
let flight: Flight | undefined
let cooldown: { epoch: number; token: string; until: number } | undefined

function sameOwner(snapshot: AuthSnapshot) {
  const current = useAuthStore.getState()
  if (current.epoch !== snapshot.epoch || current.user?.id !== snapshot.user?.id || !current.token) {
    throw new CanceledError('Session changed')
  }
  return current
}

function currentToken(snapshot: AuthSnapshot, token: string) {
  const current = sameOwner(snapshot)
  if (current.token !== token) throw new CanceledError('Session changed')
  return current
}

// These unverified claims decide when to ask the server; they never authenticate a user.
function schedule(token: string): { owner: number; expires: number } | undefined {
  if (token.length > 8192) return undefined
  try {
    const parts = token.split('.')
    if (parts.length !== 3) return undefined
    const base64 = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    const payload = JSON.parse(atob(base64.padEnd(Math.ceil(base64.length / 4) * 4, '=')))
    if (typeof payload.sub !== 'string' || !/^[1-9][0-9]*$/.test(payload.sub)
      || !Number.isSafeInteger(Number(payload.sub)) || typeof payload.exp !== 'number'
      || !Number.isFinite(payload.exp) || payload.exp <= 0) return undefined
    return { owner: Number(payload.sub), expires: payload.exp * 1000 }
  } catch { return undefined }
}

async function renew(endpoint: Endpoint, snapshot: AuthSnapshot, token: string, expires: number): Promise<string> {
  const owner = snapshot.user!.id
  try {
    currentToken(snapshot, token)
    // Use the raw client to avoid intercepting our own restore/verification requests.
    const headers = { 'X-Gamja-Session': endpoint.session_token }
    const restored = await axios.post(`${endpoint.base_url}/api/v1/auth/restore`,
      { expected_owner_id: owner }, { headers, timeout: 5000 })
    currentToken(snapshot, token)
    const fresh = restored.data?.access_token
    const next = typeof fresh === 'string' ? schedule(fresh) : undefined
    if (!next || next.owner !== owner || next.expires <= Date.now()) throw new Error('Saved login unavailable')
    const verified = await axios.get(`${endpoint.base_url}/api/v1/auth/me`, {
      headers: { ...headers, Authorization: `Bearer ${fresh}` }, timeout: 5000,
    })
    currentToken(snapshot, token)
    if (verified.data?.id !== owner || next.expires <= Date.now()) throw new Error('Saved login unavailable')
    if (!useAuthStore.getState().replaceTokenIfCurrent(snapshot.epoch, owner, token, fresh)) {
      throw new CanceledError('Session changed')
    }
    cooldown = undefined
    return fresh
  } catch (error) {
    const current = currentToken(snapshot, token)
    cooldown = { epoch: snapshot.epoch, token, until: performance.now() + 30_000 }
    if (expires > Date.now()) return token
    current.logout()
    throw error
  }
}

export async function ensureSessionContinuity(endpoint: Endpoint, snapshot: AuthSnapshot): Promise<string> {
  const current = sameOwner(snapshot)
  const token = current.token!
  const hint = schedule(token)
  if (!hint || !current.user) return token // Opaque tokens still undergo normal server validation.
  if (hint.owner !== current.user.id) {
    current.logout()
    throw new CanceledError('Session changed')
  }
  if (current.sessionOnly) {
    if (hint.expires > Date.now()) return token
    current.logout()
    throw new CanceledError('Sign in again')
  }
  if (hint.expires > Date.now() + 60_000) return token
  if (cooldown?.epoch === snapshot.epoch && cooldown.token === token && cooldown.until > performance.now()) {
    if (hint.expires > Date.now()) return token
    current.logout()
    throw new CanceledError('Sign in again')
  }
  if (!flight || flight.epoch !== snapshot.epoch || flight.owner !== current.user.id || flight.token !== token) {
    flight = { epoch: snapshot.epoch, owner: current.user.id, token,
      promise: renew(endpoint, snapshot, token, hint.expires) }
  }
  const active = flight
  try {
    await active.promise
    return sameOwner(snapshot).token!
  } finally {
    if (flight === active) flight = undefined
  }
}
