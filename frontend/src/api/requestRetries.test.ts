// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import type { AxiosAdapter } from 'axios'
import api from '../lib/api'
import { requestRetriesApi } from './requestRetries'
import { useAuthStore } from '../stores/authStore'

vi.mock('../lib/localHost', () => ({ getLocalEndpoint: vi.fn().mockResolvedValue({
  base_url: 'http://127.0.0.1:9000', session_token: 'local-test-session',
}) }))

const original = api.defaults.adapter
afterEach(() => { api.defaults.adapter = original; useAuthStore.getState().logout(); vi.restoreAllMocks() })

it('preflights and starts one exact failure without client content or Agent overrides', async () => {
  const failure = '33333333-3333-4333-8333-333333333333'
  useAuthStore.getState().setAuth('owner-token', { id: 1, email: 'owner@example.com', username: 'owner' })
  const adapter = vi.fn<AxiosAdapter>().mockImplementation(async (config) => ({
    data: config.method === 'get' ? { version: 1, eligible: true, plan_hash: 'a'.repeat(64) } : { version: 1, retry_event_id: 'retry' },
    status: config.method === 'get' ? 200 : 201, statusText: 'OK', headers: {}, config,
  }))
  api.defaults.adapter = adapter
  await requestRetriesApi.preflight(501, failure)
  await requestRetriesApi.start(501, failure, 'a'.repeat(64), 'retry-key')
  const [read, write] = adapter.mock.calls.map((call) => call[0])
  expect(read.method).toBe('get')
  expect(read.url).toBe(`/conversations/501/failures/${failure}/retry`)
  expect(read.params).toBeUndefined()
  expect(read.data).toBeUndefined()
  expect(write.method).toBe('post')
  expect(write.url).toBe(`/conversations/501/failures/${failure}/retry`)
  expect(JSON.parse(String(write.data))).toEqual({ plan_hash: 'a'.repeat(64) })
  expect(write.headers?.['Idempotency-Key']).toBe('retry-key')
})
