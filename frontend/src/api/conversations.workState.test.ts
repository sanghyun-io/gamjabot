// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from 'vitest'
import api from '../lib/api'
import { conversationsApi } from './conversations'
import { useAuthStore } from '../stores/authStore'
vi.mock('../lib/api', () => ({ default: { get: vi.fn() } }))
beforeEach(() => { vi.clearAllMocks(); useAuthStore.getState().setAuth('synthetic', { id: 1, username: 'one', email: 'one@test.invalid' }) })

it('cancels the actual members API success after Owner or session changes', async () => {
  let resolve!: (response: unknown) => void
  vi.mocked(api.get).mockImplementation(() => new Promise((done) => { resolve = done }) as never)
  const pending = conversationsApi.listMembers(1)
  const assertion = expect(pending).rejects.toMatchObject({ code: 'ERR_CANCELED' })
  useAuthStore.getState().setAuth('synthetic-two', { id: 2, username: 'two', email: 'two@test.invalid' })
  resolve({ data: [{ room_id: 1, agent_id: 8, is_active: true }] })
  await assertion
})

it('rejects another room and accepts a fresh response for exactly the requested room', async () => {
  vi.mocked(api.get).mockResolvedValueOnce({ data: [{ room_id: 2, agent_id: 8, is_active: true }] })
  await expect(conversationsApi.listMembers(1)).rejects.toMatchObject({ code: 'ERR_CANCELED' })
  vi.mocked(api.get).mockResolvedValueOnce({ data: [{ room_id: 1, agent_id: 8, is_active: true }] })
  await expect(conversationsApi.listMembers(1)).resolves.toHaveLength(1)
})
