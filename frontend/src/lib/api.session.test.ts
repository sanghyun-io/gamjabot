// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from 'axios'
import api from './api'
import { getLocalEndpoint } from './localHost'
import { useAuthStore } from '../stores/authStore'

vi.mock('./localHost', () => ({ getLocalEndpoint: vi.fn(async () => ({
  base_url: 'http://127.0.0.1:9000', session_token: 'synthetic-local-session',
})) }))

const previousAdapter = api.defaults.adapter
const user = { id: 1, email: 'owner@example.test', username: 'Owner' }
const endpoint = { base_url: 'http://127.0.0.1:9000', session_token: 'synthetic-local-session' }
afterEach(() => {
  api.defaults.adapter = previousAdapter
  useAuthStore.getState().logout()
  vi.mocked(getLocalEndpoint).mockResolvedValue(endpoint)
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

function unauthorized(config: InternalAxiosRequestConfig) {
  const response = { config, data: {}, status: 401, statusText: 'Unauthorized', headers: {} } as AxiosResponse
  return new AxiosError('Unauthorized', 'ERR_BAD_REQUEST', config, {}, response)
}

it('expires the current session in place without reloading the desktop or resending a mutation', async () => {
  const navigations: unknown[] = []
  vi.stubGlobal('window', new Proxy(window, { get(target, property, receiver) {
    if (property === 'location') return { set href(value: unknown) { navigations.push(value) } }
    return Reflect.get(target, property, receiver)
  } }))
  useAuthStore.getState().setAuth('old-token', user)
  const epoch = useAuthStore.getState().epoch
  const adapter = vi.fn(async (config: InternalAxiosRequestConfig) => { throw unauthorized(config) })
  api.defaults.adapter = adapter

  await expect(api.post('/conversations/1/messages', { content: 'one request' })).rejects.toBeInstanceOf(AxiosError)
  expect(useAuthStore.getState().token).toBeNull()
  expect(useAuthStore.getState().epoch).toBe(epoch + 1)
  expect(adapter).toHaveBeenCalledTimes(1)
  expect(navigations).toEqual([])
})

it('keeps the newer owner logged in when the previous owner receives a late 401', async () => {
  useAuthStore.getState().setAuth('old-token', user)
  let rejectOld!: (error: AxiosError) => void
  let oldConfig!: InternalAxiosRequestConfig
  api.defaults.adapter = async (config) => {
    oldConfig = config
    return new Promise<AxiosResponse>((_, reject) => { rejectOld = reject })
  }
  const oldRequest = api.get('/dashboard')
  const rejected = expect(oldRequest).rejects.toBeInstanceOf(AxiosError)
  await vi.waitFor(() => expect(rejectOld).toBeDefined())
  useAuthStore.getState().setAuth('new-token', { ...user, id: 2 })
  const epoch = useAuthStore.getState().epoch
  rejectOld(unauthorized(oldConfig))
  await rejected
  expect(useAuthStore.getState().token).toBe('new-token')
  expect(useAuthStore.getState().epoch).toBe(epoch)
})

it('does not overwrite an explicit restored token or log out the current owner for its failure', async () => {
  useAuthStore.getState().setAuth('current-token', user)
  api.defaults.adapter = async (config) => {
    expect(config.headers.Authorization).toBe('Bearer restored-token')
    throw unauthorized(config)
  }
  await expect(api.get('/auth/me', { headers: { Authorization: 'Bearer restored-token' } })).rejects.toBeInstanceOf(AxiosError)
  expect(useAuthStore.getState().token).toBe('current-token')
})

it('cancels a mutation waiting for the desktop endpoint when the owner changes', async () => {
  let ready!: (value: typeof endpoint) => void
  vi.mocked(getLocalEndpoint).mockReturnValue(new Promise((resolve) => { ready = resolve }))
  useAuthStore.getState().setAuth('old-token', user)
  const adapter = vi.fn()
  api.defaults.adapter = adapter
  const request = api.post('/conversations/1/messages', { content: 'old owner draft' })
  const rejected = expect(request).rejects.toMatchObject({ code: 'ERR_CANCELED' })
  await vi.waitFor(() => expect(getLocalEndpoint).toHaveBeenCalled())
  useAuthStore.getState().setAuth('new-token', { ...user, id: 2 })
  ready(endpoint)
  await rejected
  expect(adapter).not.toHaveBeenCalled()
  expect(useAuthStore.getState().token).toBe('new-token')
})

it('does not invalidate a signed-in owner for an anonymous login request that fails', async () => {
  api.defaults.adapter = async (config) => {
    useAuthStore.getState().setAuth('new-token', user)
    throw unauthorized(config)
  }
  await expect(api.post('/auth/login', { email: 'wrong@example.test', password: 'synthetic' })).rejects.toBeInstanceOf(AxiosError)
  expect(useAuthStore.getState().token).toBe('new-token')
})
