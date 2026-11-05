// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import ContextPreview from './ContextPreview'
import { contextPreviewsApi, type ContextPreviewResponse } from '../api/contextPreviews'
import { useAuthStore } from '../stores/authStore'

vi.mock('../api/contextPreviews', () => ({ contextPreviewsApi: { create: vi.fn() } }))

const preview: ContextPreviewResponse = {
  room_id: 501, projected_seq: 7, selected_agent_ids: [8, 9], fingerprint: 'a'.repeat(64),
  agents: [
    { agent_id: 8, agent_name: 'Ada', model_id: 'exact-one', projection_status: 'ready', error_code: null,
      system_text: 'Help with the release.', messages: [{ role: 'user', content: 'Earlier question' }, { role: 'assistant', content: 'Earlier answer' }, { role: 'user', content: '@Ada check this' }],
      source_coverage: { committed_high_water: 6, included_event_seqs: [1, 2, 6], summarized_ranges: [{ kind: 'owner_checkpoint', source_start_seq: 1, source_end_seq: 4 }], draft_included: true },
      truncations: [{ event_id: 'event-2', seq: 2, original_chars: 120, included_chars: 80, omitted_chars: 40 }],
      non_text: [{ event_id: 'shot-1', seq: 5, kind: 'screenshot_reference', supplied: false, image_mime: 'image/png', width: 800, height: 600 }],
      budget: { unit: 'characters', limit_chars: 12000, used_chars: 430, remaining_chars: 11570 } },
    { agent_id: 9, agent_name: 'Bea', model_id: 'exact-two', projection_status: 'unavailable', error_code: 'context_too_large',
      system_text: '', messages: [], source_coverage: { committed_high_water: 6, included_event_seqs: [], summarized_ranges: [], draft_included: false }, truncations: [], non_text: [],
      budget: { unit: 'characters', limit_chars: 12000, used_chars: 0, remaining_chars: 12000 } },
  ],
}

beforeEach(() => {
  useAuthStore.getState().setAuth('owner', { id: 1, email: 'owner@example.com', username: 'owner' })
  vi.mocked(contextPreviewsApi.create).mockResolvedValue(preview)
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('loads only on demand and shows each selected Agent exact safe projected conversation', async () => {
  render(<ContextPreview roomId={501} ownerId={1} authEpoch={useAuthStore.getState().epoch} content="@Ada check this  " sourceVersion="6:e6" recipientIdentity="8:exact-one,9:exact-two" active />)
  expect(contextPreviewsApi.create).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Preview what Agents will see' }))
  const dialog = await screen.findByRole('dialog', { name: 'Conversation shown to Agents' })
  expect(contextPreviewsApi.create).toHaveBeenCalledWith(501, '@Ada check this')
  expect(within(dialog).getByText('Ada')).toBeTruthy()
  expect(within(dialog).getByText('Earlier question')).toBeTruthy()
  expect(within(dialog).getByText('@Ada check this')).toBeTruthy()
  expect(within(dialog).getByText(/conversation is too large/i)).toBeTruthy()
  expect(within(dialog).getByText(/some earlier text was shortened/i)).toBeTruthy()
  expect(within(dialog).getByText(/image reference is not supplied/i)).toBeTruthy()
  expect(within(dialog).getByText('Projection details').closest('details')?.open).toBe(false)
  fireEvent.click(within(dialog).getByText('Technical details'))
  expect(within(dialog).getByText('#1, #2, #6')).toBeTruthy()
  expect(within(dialog).getByText('Handoff note')).toBeTruthy()
  expect(within(dialog).getByText('#1–#4')).toBeTruthy()
  expect(within(dialog).getByText('Shortened room record #2')).toBeTruthy()
  expect(within(dialog).getByText('80 of 120 characters included · 40 omitted')).toBeTruthy()
})

it('drops an open preview when draft or projection identity changes without sending', async () => {
  const view = render(<ContextPreview roomId={501} ownerId={1} authEpoch={useAuthStore.getState().epoch} content="First draft" sourceVersion="6:e6" recipientIdentity="8:exact-one" active />)
  fireEvent.click(screen.getByRole('button', { name: 'Preview what Agents will see' }))
  await screen.findByText('Earlier question')
  view.rerender(<ContextPreview roomId={501} ownerId={1} authEpoch={useAuthStore.getState().epoch} content="Edited draft" sourceVersion="6:e6" recipientIdentity="8:exact-one" active />)
  expect(screen.queryByRole('dialog')).toBeNull()
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Preview what Agents will see' })))
  vi.mocked(contextPreviewsApi.create).mockResolvedValueOnce({ ...preview, selected_agent_ids: [9], agents: [preview.agents[1]] })
  fireEvent.click(screen.getByRole('button', { name: 'Preview what Agents will see' }))
  expect(await screen.findByText('Bea')).toBeTruthy()
  expect(screen.queryByText('Ada')).toBeNull()
  view.rerender(<ContextPreview roomId={501} ownerId={1} authEpoch={useAuthStore.getState().epoch} content="Edited draft" sourceVersion="7:e7" recipientIdentity="8:changed-model" active />)
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(contextPreviewsApi.create).toHaveBeenCalledTimes(2)
})

it('ignores a late prior-owner preview and clears the modal', async () => {
  let finish!: (value: ContextPreviewResponse) => void
  vi.mocked(contextPreviewsApi.create).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  render(<ContextPreview roomId={501} ownerId={1} authEpoch={useAuthStore.getState().epoch} content="Draft" sourceVersion="6:e6" recipientIdentity="8:exact-one" active />)
  fireEvent.click(screen.getByRole('button', { name: 'Preview what Agents will see' }))
  expect(await screen.findByRole('dialog')).toBeTruthy()
  act(() => useAuthStore.getState().setAuth('other', { id: 2, email: 'other@example.com', username: 'other' }))
  act(() => finish(preview))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(screen.queryByText('Earlier question')).toBeNull()
})

it('preserves the draft and offers retry when projection is unavailable', async () => {
  vi.mocked(contextPreviewsApi.create).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(preview)
  render(<ContextPreview roomId={501} ownerId={1} authEpoch={useAuthStore.getState().epoch} content="Keep this draft" sourceVersion="6:e6" recipientIdentity="8:exact-one" active />)
  fireEvent.click(screen.getByRole('button', { name: 'Preview what Agents will see' }))
  expect(await screen.findByText('The Agent conversation preview is unavailable.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(await screen.findByText('Earlier question')).toBeTruthy()
  expect(contextPreviewsApi.create).toHaveBeenNthCalledWith(2, 501, 'Keep this draft')
})

it('shows exact memory recipient, revision, floor, selected scope and serialized characters and invalidates on change', async () => {
  vi.mocked(contextPreviewsApi.create).mockResolvedValue({ ...preview, agents: [{ ...preview.agents[0], memory_projection: { agent_id: 8, room_revision: 7, agent_revision: 3, context_after_seq: 12, legacy_disabled: true, serialized_chars: 456, entries: [{ id: 'memory-id', revision: 2, scope: 'agent', target_agent_id: 8 }] } }] })
  const props = { roomId: 501, ownerId: 1, authEpoch: useAuthStore.getState().epoch, content: 'Next question', sourceVersion: 'before-memory', recipientIdentity: '8', active: true }
  const result = render(<ContextPreview {...props} />)
  fireEvent.click(screen.getByRole('button', { name: 'Preview what Agents will see' }))
  await screen.findByText(/Recipient Agent #8/); expect(screen.getByText(/456 serialized characters/)).toBeTruthy(); expect(screen.getByText(/memory-id/)).toBeTruthy()
  result.rerender(<ContextPreview {...props} sourceVersion="memory-updated" />)
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(contextPreviewsApi.create).toHaveBeenCalledTimes(1)
})
