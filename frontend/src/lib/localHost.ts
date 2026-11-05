import { invoke, isTauri } from '@tauri-apps/api/core'
import { useAuthStore } from '../stores/authStore'
import { ensureSessionContinuity } from './sessionContinuity'

interface LocalEndpoint {
  base_url: string
  session_token: string
}

let endpoint: Promise<LocalEndpoint> | undefined

export function getLocalEndpoint(options: { refresh?: boolean } = {}): Promise<LocalEndpoint> {
  if (!isTauri()) return Promise.reject(new Error('Desktop host is unavailable'))
  if (options.refresh || !endpoint) {
    const exchange = invoke<LocalEndpoint>('get_local_endpoint').catch((error) => {
      // An older failed exchange must not discard a more recent refresh.
      if (endpoint === exchange) endpoint = undefined
      throw error
    })
    endpoint = exchange
  }
  return endpoint
}

export async function openRoomSocket(roomId: number, afterSeq?: number): Promise<WebSocket> {
  const auth = useAuthStore.getState()
  if (!auth.token) throw new Error('User login is required')
  const endpoint = await getLocalEndpoint()
  const { base_url, session_token } = endpoint
  const userToken = await ensureSessionContinuity(endpoint, auth)
  if (useAuthStore.getState().epoch !== auth.epoch) throw new Error('Session changed')
  const url = new URL(`/api/v1/conversations/${roomId}/ws`, base_url)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  if (afterSeq !== undefined) url.searchParams.set('after_seq', String(afterSeq))
  return new WebSocket(url, ['gamja.chat', `gamja.local.${session_token}`, `gamja.user.${userToken}`])
}
