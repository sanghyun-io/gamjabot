// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import type { AxiosAdapter } from 'axios'
import api from '../lib/api'
import { useAuthStore } from '../stores/authStore'
import { toolsApi } from './tools'

vi.mock('../lib/localHost', () => ({ getLocalEndpoint: vi.fn().mockResolvedValue({
  base_url: 'http://127.0.0.1:9000', session_token: 'local-test-session',
}) }))

const originalAdapter = api.defaults.adapter
afterEach(() => { api.defaults.adapter = originalAdapter; useAuthStore.getState().logout() })

it('requests the exact approval preview with session credentials and no extra CORS header', async () => {
  useAuthStore.getState().setAuth('test-owner-token', { id: 1, email: 'owner@test.invalid', username: 'owner' })
  api.defaults.adapter = (async (request) => {
    expect(request.url).toBe('/tools/rooms/2/actions/action-1/preview')
    expect(request.headers.get('X-Gamja-Session')).toBe('local-test-session')
    expect(request.headers.get('Authorization')).toBe('Bearer test-owner-token')
    expect(request.headers.has('Cache-Control')).toBe(false)
    return { data: { action_id: 'action-1', arguments: { path: '/tmp/disposable.txt' } }, status: 200,
      statusText: 'OK', headers: { 'cache-control': 'no-store' }, config: request }
  }) satisfies AxiosAdapter

  const preview = await toolsApi.preview(2, 'action-1')
  expect(preview.arguments.path).toBe('/tmp/disposable.txt')
})

it('posts the inspected review ID without fetching a new preview', async () => {
  useAuthStore.getState().setAuth('test-owner-token', { id: 1, email: 'owner@test.invalid', username: 'owner' })
  const seen: string[] = []
  api.defaults.adapter = (async (request) => {
    seen.push(request.url!)
    expect(request.method).toBe('post')
    expect(JSON.parse(request.data)).toEqual({ decision: 'approve', scope: 'once',
      review_id: '550e8400-e29b-41d4-a716-446655440000', acknowledge_unverified_context: true })
    return { data: {}, status: 200, statusText: 'OK', headers: {}, config: request }
  }) satisfies AxiosAdapter
  await toolsApi.decide(2, 'action-1', 'approve', 'once', '550e8400-e29b-41d4-a716-446655440000', true)
  expect(seen).toEqual(['/tools/rooms/2/actions/action-1/decision'])
})
