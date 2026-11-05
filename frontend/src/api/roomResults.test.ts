// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from 'vitest'
import api from '../lib/api'
import { roomResultsApi } from './roomResults'
vi.mock('../lib/api', () => ({ default: { get: vi.fn(), post: vi.fn() } }))
beforeEach(() => { vi.clearAllMocks(); vi.mocked(api.get).mockResolvedValue({ data: {} }); vi.mocked(api.post).mockResolvedValue({ data: {} }) })
it('sends exact version, source IDs and original request key through the common API', async () => {
  const value = { source_event_id: 'source-id', kind: 'text' as const, title: 'Plan', evidence: [], expected_current_version: 3 }
  await roomResultsApi.version(501, 'result-id', value, 'original-key')
  expect(api.post).toHaveBeenCalledWith('/conversations/501/results/result-id/versions', value, { headers: { 'Idempotency-Key': 'original-key' } })
  await roomResultsApi.review(501, 'result-id', 2, 'peer-id', 'review-key')
  expect(api.post).toHaveBeenLastCalledWith('/conversations/501/results/result-id/versions/2/reviews', { review_event_id: 'peer-id' }, { headers: { 'Idempotency-Key': 'review-key' } })
})
it('keeps list, version and opinion cursors distinct and bounded', async () => {
  await roomResultsApi.list(501, 'after-id'); expect(api.get).toHaveBeenLastCalledWith('/conversations/501/results', { params: { after: 'after-id', limit: 50 } })
  await roomResultsApi.versions(501, 'result-id', 7); expect(api.get).toHaveBeenLastCalledWith('/conversations/501/results/result-id/versions', { params: { before: 7, limit: 50 } })
})
