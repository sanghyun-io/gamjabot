// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from 'vitest'
import { invoke, isTauri } from '@tauri-apps/api/core'
import { useAuthStore } from '../stores/authStore'
import { artifactsApi } from './artifacts'

vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn(), isTauri: vi.fn() }))

beforeEach(() => {
  vi.clearAllMocks()
  useAuthStore.getState().setAuth('private-owner-token', { id: 1, email: 'owner@example.com', username: 'owner' })
})

it('uses only the private native List command with the exact room scan cursor', async () => {
  vi.mocked(isTauri).mockReturnValue(true)
  vi.mocked(invoke).mockResolvedValue({ version: 1, room_id: 501, after_seq: 40, scanned_through_seq: 52,
    next_after_seq: 52, items: [] })
  await expect(artifactsApi.list(501, 40, 50)).resolves.toMatchObject({ next_after_seq: 52 })
  expect(invoke).toHaveBeenCalledWith('list_screenshot_artifacts', {
    roomId: 501, afterSeq: 40, limit: 50, bearerToken: 'private-owner-token',
  })
})

it('does not expose a public CLI or HTTP fallback outside the fixed native runtime', async () => {
  vi.mocked(isTauri).mockReturnValue(false)
  await expect(artifactsApi.list(501)).rejects.toThrow('artifact_unavailable')
  expect(invoke).not.toHaveBeenCalled()
})
