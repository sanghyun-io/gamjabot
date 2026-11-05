// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import type { AxiosAdapter } from 'axios'
import api from '../lib/api'
import { requestInspectorApi } from './requestInspector'
import { useAuthStore } from '../stores/authStore'

vi.mock('../lib/localHost', () => ({ getLocalEndpoint: vi.fn().mockResolvedValue({
  base_url: 'http://127.0.0.1:9000', session_token: 'local-test-session',
}) }))

const original = api.defaults.adapter
afterEach(() => { api.defaults.adapter = original; useAuthStore.getState().logout(); vi.restoreAllMocks() })

it('reads one exact room request without query or mutation data', async () => {
  const trigger = '11111111-1111-4111-8111-111111111111'
  useAuthStore.getState().setAuth('owner-token', { id: 1, email: 'owner@example.com', username: 'owner' })
  const adapter = vi.fn<AxiosAdapter>().mockImplementation(async (config) => ({
    data: { version: 1, room_id: 501, request: { event_id: trigger }, execution: {}, outcomes: [], actions: [] },
    status: 200, statusText: 'OK', headers: {}, config,
  }))
  api.defaults.adapter = adapter
  await requestInspectorApi.get(501, trigger)
  expect(adapter).toHaveBeenCalledTimes(1)
  const config = adapter.mock.calls[0][0]
  expect(config.method).toBe('get')
  expect(config.url).toBe(`/conversations/501/requests/${trigger}`)
  expect(config.params).toBeUndefined()
  expect(config.data).toBeUndefined()
})
