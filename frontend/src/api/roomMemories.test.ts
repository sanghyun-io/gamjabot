// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from 'vitest'
import api from '../lib/api'
import { roomMemoriesApi, type MemoryWrite } from './roomMemories'
vi.mock('../lib/api', () => ({ default: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() } }))
beforeEach(() => { vi.clearAllMocks(); for (const fn of [api.get, api.post, api.patch, api.delete]) vi.mocked(fn).mockResolvedValue({ data: {} }) })
const body: MemoryWrite = { expected_room_revision: 7, category: 'fact', content: 'Keep', scope: 'agent', target_agent_id: 8, source: { kind: 'manual' } }
it('uses common Owner API and exact CAS/body/key for add, edit and delete', async () => {
  await roomMemoriesApi.list(501); expect(api.get).toHaveBeenCalledWith('/conversations/501/memories')
  await roomMemoriesApi.add(501, body, 'original'); expect(api.post).toHaveBeenCalledWith('/conversations/501/memories', body, { headers: { 'Idempotency-Key': 'original' } })
  await roomMemoriesApi.edit(501, 'entry', { ...body, expected_revision: 3 }, 'edit-key'); expect(api.patch).toHaveBeenCalledWith('/conversations/501/memories/entry', { ...body, expected_revision: 3 }, { headers: { 'Idempotency-Key': 'edit-key' } })
  await roomMemoriesApi.remove(501, 'entry', { expected_room_revision: 7, expected_revision: 3 }, 'delete-key'); expect(api.delete).toHaveBeenCalledWith('/conversations/501/memories/entry', { data: { expected_room_revision: 7, expected_revision: 3 }, headers: { 'Idempotency-Key': 'delete-key' } })
})
it('keeps legacy digest, target revision and replacement atomic in one command', async () => {
  const value = { expected_room_revision: 7, expected_agent_revision: 2, expected_legacy_digest: 'a'.repeat(64), operation: 'replace' as const, replacement: body }
  await roomMemoriesApi.legacy(501, 8, value, 'legacy-key'); expect(api.post).toHaveBeenCalledWith('/conversations/501/memories/legacy/8', value, { headers: { 'Idempotency-Key': 'legacy-key' } })
})
