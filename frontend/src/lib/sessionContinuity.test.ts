// @vitest-environment jsdom
import axios, { AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from 'axios'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { invoke } from '@tauri-apps/api/core'
import api from './api'
import { ensureSessionContinuity } from './sessionContinuity'
import { getLocalEndpoint, openRoomSocket } from './localHost'
import { useAuthStore } from '../stores/authStore'
import { queryClient } from './queryClient'
import { roomEventCursor } from './eventCursor'
import type { RoomEvent } from '../api/events'

vi.mock('@tauri-apps/api/core', () => ({ isTauri: () => true, invoke: vi.fn() }))
const endpoint = { base_url: 'http://127.0.0.1:9000', session_token: 'synthetic-local-session' }
const user = { id: 1, email: 'owner@example.test', username: 'Owner' }
const originalApiAdapter = api.defaults.adapter
const originalRawAdapter = axios.defaults.adapter

function token(seconds: number, owner = 1) {
  return `synthetic.${btoa(JSON.stringify({ sub: String(owner), exp: Date.now() / 1000 + seconds }))}.signature`
}
function response(config: InternalAxiosRequestConfig, data: unknown): AxiosResponse {
  return { config, data, status: 200, statusText: 'OK', headers: {} }
}
function denied(config: InternalAxiosRequestConfig) {
  return new AxiosError('Unauthorized', 'ERR_BAD_REQUEST', config, {},
    { ...response(config, {}), status: 401 })
}
function authTransport(owner = 1) {
  const fresh = token(1800)
  const adapter = vi.fn(async (config: InternalAxiosRequestConfig) => response(config,
    config.url?.endsWith('/restore') ? { access_token: fresh } : { id: owner }))
  axios.defaults.adapter = adapter
  return { fresh, adapter }
}

beforeEach(async () => {
  vi.useFakeTimers({ toFake: ['Date', 'performance'] })
  vi.setSystemTime(new Date('2026-10-12T00:00:00Z'))
  vi.mocked(invoke).mockResolvedValue(endpoint)
  await getLocalEndpoint({ refresh: true })
  useAuthStore.getState().logout()
})
afterEach(() => {
  useAuthStore.getState().logout()
  api.defaults.adapter = originalApiAdapter
  axios.defaults.adapter = originalRawAdapter
  vi.clearAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

it('renews after sleep without clearing the same owner draft, cursor, or session epoch', async () => {
  useAuthStore.getState().setAuth(token(1800), user)
  const captured = useAuthStore.getState()
  queryClient.setQueryData(['draft', 1], 'keep my draft')
  roomEventCursor.add(1, [{ room_id: 1, seq: 1, event_id: 'synthetic-event' } as RoomEvent])
  vi.setSystemTime(new Date('2026-10-12T00:31:00Z'))
  const { fresh, adapter } = authTransport()
  expect(await ensureSessionContinuity(endpoint, captured)).toBe(fresh)
  expect(adapter).toHaveBeenCalledTimes(2)
  expect(JSON.parse(adapter.mock.calls[0][0].data)).toEqual({ expected_owner_id: 1 })
  expect(adapter.mock.calls[1][0].headers.Authorization).toBe(`Bearer ${fresh}`)
  expect(useAuthStore.getState().epoch).toBe(captured.epoch)
  expect(queryClient.getQueryData(['draft', 1])).toBe('keep my draft')
  expect(roomEventCursor.afterSeq(1)).toBe(1)
})

it('shares one renewal and sends each queued mutation once', async () => {
  useAuthStore.getState().setAuth(token(-1), user)
  const fresh = token(1800)
  let release!: (value: AxiosResponse) => void
  let restoreConfig!: InternalAxiosRequestConfig
  const raw = vi.fn(async (config: InternalAxiosRequestConfig) => {
    if (config.url?.endsWith('/restore')) {
      restoreConfig = config
      return new Promise<AxiosResponse>((resolve) => { release = resolve })
    }
    return response(config, { id: 1 })
  })
  axios.defaults.adapter = raw
  const business = vi.fn(async (config: InternalAxiosRequestConfig) => response(config, {}))
  api.defaults.adapter = business
  const first = api.post('/conversations/1/messages', { content: 'first' })
  const second = api.post('/conversations/2/messages', { content: 'second' })
  await vi.waitFor(() => expect(release).toBeDefined())
  expect(business).not.toHaveBeenCalled()
  release(response(restoreConfig, { access_token: fresh }))
  await Promise.all([first, second])
  expect(raw).toHaveBeenCalledTimes(2)
  expect(business).toHaveBeenCalledTimes(2)
  expect(business.mock.calls.map(([config]) => config.url).sort()).toEqual([
    '/conversations/1/messages', '/conversations/2/messages',
  ])
  expect(business.mock.calls.every(([config]) => config.headers.Authorization === `Bearer ${fresh}`)).toBe(true)
})

it('ignores a late old-token 401 after same-owner refresh', async () => {
  useAuthStore.getState().setAuth(token(120), user)
  const captured = useAuthStore.getState()
  let reject!: (error: AxiosError) => void
  let oldConfig!: InternalAxiosRequestConfig
  api.defaults.adapter = async (config) => {
    oldConfig = config
    return new Promise<AxiosResponse>((_, fail) => { reject = fail })
  }
  const original = api.get('/dashboard')
  const rejected = expect(original).rejects.toBeInstanceOf(AxiosError)
  await vi.waitFor(() => expect(reject).toBeDefined())
  vi.setSystemTime(new Date('2026-10-12T00:01:05Z'))
  const { fresh } = authTransport()
  await ensureSessionContinuity(endpoint, captured)
  reject(denied(oldConfig))
  await rejected
  expect(useAuthStore.getState().token).toBe(fresh)
  expect(useAuthStore.getState().epoch).toBe(captured.epoch)
})

it.each(['revoked', 'foreign'])('does not dispatch an expired %s session', async (kind) => {
  useAuthStore.getState().setAuth(token(-1), user)
  if (kind === 'foreign') authTransport(2)
  else axios.defaults.adapter = async (config) => { throw denied(config) }
  const business = vi.fn()
  api.defaults.adapter = business
  await expect(api.post('/conversations/1/messages', {})).rejects.toBeDefined()
  expect(business).not.toHaveBeenCalled()
  expect(useAuthStore.getState().token).toBeNull()
})

it.each(['logout', 'owner-change'])('cancels queued work after %s during renewal', async (kind) => {
  useAuthStore.getState().setAuth(token(-1), user)
  const fresh = token(1800)
  let release!: (value: AxiosResponse) => void
  let restoreConfig!: InternalAxiosRequestConfig
  axios.defaults.adapter = async (config) => {
    restoreConfig = config
    return new Promise<AxiosResponse>((resolve) => { release = resolve })
  }
  const business = vi.fn()
  api.defaults.adapter = business
  const request = api.post('/conversations/1/messages', {})
  const rejected = expect(request).rejects.toMatchObject({ code: 'ERR_CANCELED' })
  await vi.waitFor(() => expect(release).toBeDefined())
  if (kind === 'logout') useAuthStore.getState().logout()
  else useAuthStore.getState().setAuth('other-owner-token', { ...user, id: 2 })
  const epoch = useAuthStore.getState().epoch
  release(response(restoreConfig, { access_token: fresh }))
  await rejected
  expect(business).not.toHaveBeenCalled()
  expect(useAuthStore.getState().epoch).toBe(epoch)
  expect(useAuthStore.getState().token).toBe(kind === 'logout' ? null : 'other-owner-token')
})

it('allows a still-valid nonremembered token briefly without repeated restore calls', async () => {
  useAuthStore.getState().setAuth(token(60), user)
  const raw = vi.fn(async (config: InternalAxiosRequestConfig) => { throw denied(config) })
  axios.defaults.adapter = raw
  const business = vi.fn(async (config: InternalAxiosRequestConfig) => response(config, {}))
  api.defaults.adapter = business
  await api.post('/conversations/1/messages', {})
  await api.post('/conversations/1/messages', {})
  expect(raw).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(31_000)
  await api.post('/conversations/1/messages', {})
  expect(raw).toHaveBeenCalledTimes(2)
  await vi.advanceTimersByTimeAsync(40_000)
  await expect(api.post('/conversations/1/messages', {})).rejects.toBeDefined()
  expect(business).toHaveBeenCalledTimes(3)
})

it('renews the socket using subprotocol credentials and preserves its cursor', async () => {
  useAuthStore.getState().setAuth(token(-1), user)
  const { fresh } = authTransport()
  const Socket = vi.fn(function (this: object, _url: string, _protocols: string[]) {})
  vi.stubGlobal('WebSocket', Socket)
  await openRoomSocket(3, 5)
  expect(Socket).toHaveBeenCalledOnce()
  const [url, protocols] = Socket.mock.calls[0]
  expect(String(url)).toBe('ws://127.0.0.1:9000/api/v1/conversations/3/ws?after_seq=5')
  expect(protocols).toEqual(['gamja.chat', 'gamja.local.synthetic-local-session', `gamja.user.${fresh}`])
})

it('cancels a socket awaiting the endpoint when the owner changes', async () => {
  useAuthStore.getState().setAuth(token(1800), user)
  let release!: (value: typeof endpoint) => void
  vi.mocked(invoke).mockReturnValue(new Promise((resolve) => { release = resolve }))
  const pendingEndpoint = getLocalEndpoint({ refresh: true })
  const Socket = vi.fn()
  vi.stubGlobal('WebSocket', Socket)
  const pendingSocket = openRoomSocket(3)
  const rejected = expect(pendingSocket).rejects.toMatchObject({ code: 'ERR_CANCELED' })
  useAuthStore.getState().setAuth('other-owner-token', { ...user, id: 2 })
  release(endpoint)
  await pendingEndpoint
  await rejected
  expect(Socket).not.toHaveBeenCalled()
})
