// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import type { AxiosAdapter } from 'axios'
import api from '../lib/api'
import { requestFinalizeApi } from './requestFinalize'
import { useAuthStore } from '../stores/authStore'

vi.mock('../lib/localHost', () => ({ getLocalEndpoint: vi.fn().mockResolvedValue({
  base_url: 'http://127.0.0.1:9000', session_token: 'local-test-session',
}) }))

const original = api.defaults.adapter
afterEach(() => { api.defaults.adapter = original; useAuthStore.getState().logout(); vi.restoreAllMocks() })

it('previews and explicitly starts one exact finalizer intent without client content or outcome overrides', async () => {
  const source = '11111111-1111-4111-8111-111111111111'
  useAuthStore.getState().setAuth('owner-token', { id: 1, email: 'owner@example.com', username: 'owner' })
  const adapter = vi.fn<AxiosAdapter>().mockImplementation(async (config) => ({
    data: config.url?.endsWith('/preview') ? { version: 1, eligible: true, plan_hash: 'a'.repeat(64) }
      : { version: 1, finalize_event_id: '44444444-4444-4444-8444-444444444444' },
    status: config.url?.endsWith('/preview') ? 200 : 201, statusText: 'OK', headers: {}, config,
  }))
  api.defaults.adapter = adapter
  await requestFinalizeApi.preview(501, source, 8)
  await requestFinalizeApi.execute(501, source, { agent_id: 8, plan_hash: 'a'.repeat(64) }, 'finalize-key')
  const [preview, execute] = adapter.mock.calls.map((call) => call[0])
  expect(preview.method).toBe('post')
  expect(preview.url).toBe(`/conversations/501/requests/${source}/finalize/preview`)
  expect(JSON.parse(String(preview.data))).toEqual({ agent_id: 8 })
  expect(execute.method).toBe('post')
  expect(execute.url).toBe(`/conversations/501/requests/${source}/finalize`)
  expect(JSON.parse(String(execute.data))).toEqual({ agent_id: 8, plan_hash: 'a'.repeat(64) })
  expect(execute.headers?.['Idempotency-Key']).toBe('finalize-key')
})
