// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import ScreenshotGallery from './ScreenshotGallery'
import { artifactsApi, type ScreenshotArtifactPage } from '../api/artifacts'
import { useAuthStore } from '../stores/authStore'

vi.mock('../api/artifacts', () => ({ artifactsApi: { list: vi.fn() } }))
vi.mock('./ScreenshotArtifact', () => ({ default: ({ actionId }: { actionId: string }) => <span>image:{actionId}</span> }))

const page = (changes: Partial<ScreenshotArtifactPage> = {}): ScreenshotArtifactPage => ({ version: 1, room_id: 501, after_seq: 0,
  scanned_through_seq: 8, next_after_seq: null, items: [
    { room_id: 501, event_seq: 8, action_id: '8d1e0e6c-9eeb-4571-864b-f2592c44db38', created_at: '2026-10-05T01:00:00Z', width: 1280, height: 720, state: 'available' },
    { room_id: 501, event_seq: 4, action_id: '4d1e0e6c-9eeb-4571-864b-f2592c44db38', created_at: '2026-10-05T00:00:00Z', width: 1280, height: 720, state: 'removed' },
  ], ...changes })
const openPanel = (panel: HTMLDetailsElement) => { panel.open = true; fireEvent(panel, new Event('toggle')) }

beforeEach(() => {
  useAuthStore.getState().setAuth('owner', { id: 1, email: 'owner@example.com', username: 'owner' })
  vi.mocked(artifactsApi.list).mockResolvedValue(page())
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('lists lazily, renders only available bytes, and closes before exact-event navigation', async () => {
  const navigate = vi.fn()
  render(<ScreenshotGallery roomId={501} ownerId={1} authEpoch={useAuthStore.getState().epoch} readOnly={false} onNavigateSeq={navigate} />)
  expect(artifactsApi.list).not.toHaveBeenCalled()
  const panel = screen.getByText('Screenshots').closest('details')!
  openPanel(panel)
  expect(await screen.findByText('image:8d1e0e6c-9eeb-4571-864b-f2592c44db38')).toBeTruthy()
  expect(screen.getByText('Image removed')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Message 8' }))
  expect(navigate).toHaveBeenCalledWith(8)
  expect(panel.open).toBe(false)
})

it('forwards an opaque scan cursor even when a page has no screenshots', async () => {
  vi.mocked(artifactsApi.list).mockResolvedValueOnce(page({ scanned_through_seq: 50, next_after_seq: 50, items: [] }))
    .mockResolvedValueOnce(page({ after_seq: 50, scanned_through_seq: 75, next_after_seq: null, items: [page().items[0]] }))
  render(<ScreenshotGallery roomId={501} ownerId={1} authEpoch={useAuthStore.getState().epoch} readOnly={false} onNavigateSeq={vi.fn()} />)
  openPanel(screen.getByText('Screenshots').closest('details')!)
  expect(await screen.findByText('No screenshots in this part of the room. More history is available.')).toBeTruthy()
  fireEvent.click(await screen.findByRole('button', { name: 'Load more' }))
  await waitFor(() => expect(artifactsApi.list).toHaveBeenNthCalledWith(2, 501, 50, 50))
  expect(await screen.findByText(/image:8d1e/)).toBeTruthy()
})

it('drops a late page across an owner epoch change and clears the gallery', async () => {
  let finish!: (value: ScreenshotArtifactPage) => void
  vi.mocked(artifactsApi.list).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  const epoch = useAuthStore.getState().epoch
  const view = render(<ScreenshotGallery roomId={501} ownerId={1} authEpoch={epoch} readOnly={false} onNavigateSeq={vi.fn()} />)
  openPanel(screen.getByText('Screenshots').closest('details')!)
  await waitFor(() => expect(artifactsApi.list).toHaveBeenCalled())
  act(() => useAuthStore.getState().setAuth('other', { id: 2, email: 'other@example.com', username: 'other' }))
  view.rerender(<ScreenshotGallery roomId={501} ownerId={2} authEpoch={useAuthStore.getState().epoch} readOnly={false} onNavigateSeq={vi.fn()} />)
  act(() => finish(page()))
  await waitFor(() => expect(screen.queryByText(/image:8d1e/)).toBeNull())
  expect(screen.getByText('Screenshots').closest('details')?.open).toBe(false)
})

it('does not surface a rejected prior-owner request in the replacement auth state', async () => {
  let fail!: (error: Error) => void
  vi.mocked(artifactsApi.list).mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject }))
  render(<ScreenshotGallery roomId={501} ownerId={1} authEpoch={useAuthStore.getState().epoch} readOnly onNavigateSeq={vi.fn()} />)
  openPanel(screen.getByText('Screenshots').closest('details')!)
  await waitFor(() => expect(artifactsApi.list).toHaveBeenCalled())
  act(() => useAuthStore.getState().setAuth('other', { id: 2, email: 'other@example.com', username: 'other' }))
  act(() => fail(new Error('prior owner failed')))
  await act(async () => {})
  expect(screen.queryByText('Screenshots could not be loaded.')).toBeNull()
})

it('keeps a safe retry after a list error', async () => {
  vi.mocked(artifactsApi.list).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(page())
  render(<ScreenshotGallery roomId={501} ownerId={1} authEpoch={useAuthStore.getState().epoch} readOnly onNavigateSeq={vi.fn()} />)
  openPanel(screen.getByText('Screenshots').closest('details')!)
  expect(await screen.findByText('Screenshots could not be loaded.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(await screen.findByText(/image:8d1e/)).toBeTruthy()
})
