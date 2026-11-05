// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import RoomResults from './RoomResults'
import { roomResultsApi, type ResultDetail, type ResultSummary } from '../api/roomResults'
import type { RoomEvent } from '../api/events'
import { useAuthStore } from '../stores/authStore'

vi.mock('../api/roomResults', () => ({ roomResultsApi: { list: vi.fn(), versions: vi.fn(), detail: vi.fn(), reviews: vi.fn(), save: vi.fn(), version: vi.fn(), state: vi.fn(), review: vi.fn(), linkReport: vi.fn() } }))
vi.mock('./ScreenshotArtifact', () => ({ default: (props: { roomId: number; actionId: string; eventSeq: number; readOnly: boolean }) => <div data-testid="managed-viewer">{JSON.stringify(props)}</div> }))
const sourceId = '11111111-1111-4111-8111-111111111111'
const resultId = '22222222-2222-4222-8222-222222222222'
const source: RoomEvent = { event_id: sourceId, room_id: 501, seq: 2, device_id: null, actor_type: 'agent', actor_id: 8,
  actor_name_snapshot: 'Author then', kind: 'message', payload: { content: 'Original answer' }, created_at: '2026-10-16T00:00:00Z', idempotency_key: null }
const summary: ResultSummary = { room_id: 501, result_id: resultId, version: 2, title: 'Plan', kind: 'text', author_agent_id: 8, author_name_snapshot: 'Author then', state: 'draft', state_revision: 1, linked_opinion_count: 0, created_at: source.created_at }
const detail = (patch: Partial<ResultDetail> = {}): ResultDetail => ({ ...summary, current_version: 2, source: { event_id: sourceId, seq: 2, href: '/chat/501?seq=2' }, execution: { trigger_event_id: null, round: 1, generation: 7, model_use: null }, content: 'Version two', file_ref: null, file_status: null, evidence: [], evidence_status: 'none', review_support: 'source_reply_only', link_report: null, state_owner_id: null, state_changed_at: null, link_report_owner_id: null, link_reported_at: null, ...patch })
const command = { room_id: 501, result_id: resultId, result_version: 2, state_revision: 1, state: 'draft' as const, operation: 'save', replayed: false, activity_event_id: sourceId, activity_seq: 9 }
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((r) => { resolve = r }); return { resolve, promise } }
beforeEach(() => {
  localStorage.removeItem('gamja.locale')
  useAuthStore.getState().setAuth('owner', { id: 1, username: 'Owner', email: 'owner@example.com' })
  vi.mocked(roomResultsApi.list).mockResolvedValue({ room_id: 501, items: [summary], next_cursor: null })
  vi.mocked(roomResultsApi.versions).mockResolvedValue({ room_id: 501, result_id: resultId, current_version: 2, items: [summary, { ...summary, version: 1 }], next_before_version: null })
  vi.mocked(roomResultsApi.detail).mockImplementation(async (_room, _id, version) => detail({ version, content: version === 1 ? 'Version one' : 'Version two' }))
  vi.mocked(roomResultsApi.reviews).mockResolvedValue({ items: [], next_cursor: null })
  for (const method of ['save', 'version', 'state', 'review', 'linkReport'] as const) vi.mocked(roomResultsApi[method]).mockResolvedValue(command)
})
afterEach(() => { cleanup(); vi.clearAllMocks() })
function view(patch: Partial<React.ComponentProps<typeof RoomResults>> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const props = { roomId: 501, ownerId: 1, authEpoch: useAuthStore.getState().epoch, active: true, visible: true, events: [source], source: null, onClearSource: vi.fn(), onNavigate: vi.fn(), ...patch }
  return { props, client, ...render(<QueryClientProvider client={client}><RoomResults {...props} /></QueryClientProvider>) }
}
async function openDetail() { fireEvent.click(await screen.findByRole('button', { name: /Plan.*Text/ })); await screen.findByText('Version two') }

it('does not fetch or mutate before its visible tab is opened', () => {
  view({ visible: false })
  expect(roomResultsApi.list).not.toHaveBeenCalled(); expect(roomResultsApi.save).not.toHaveBeenCalled()
})
it('loads immutable versions, compares the previous content and keeps no-evidence and opinion limits explicit', async () => {
  const { props } = view(); await openDetail()
  expect(screen.getByText('No additional evidence recorded.')).toBeTruthy()
  const time = document.querySelector('time')!; expect(time.dateTime).toBe(source.created_at); expect(time.textContent).toMatch(/\d+:\d+:\d+/)
  expect(screen.getByText(/do not prove full content review/)).toBeTruthy()
  expect((screen.getByText('Source and evidence').closest('details') as HTMLDetailsElement).open).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: 'Compare with previous version' }))
  expect(await screen.findByText('Version one')).toBeTruthy()
  fireEvent.click(screen.getAllByRole('button', { name: 'Open source #2' })[0])
  expect(props.onNavigate).toHaveBeenCalledWith(2, sourceId)
  expect(roomResultsApi.state).not.toHaveBeenCalled()
})
it('submits only selected canonical references and prevents same-tick duplicates', async () => {
  const pending = deferred<typeof command>(); vi.mocked(roomResultsApi.save).mockReturnValue(pending.promise)
  const { props } = view({ source })
  const input = screen.getByLabelText('Result title'); expect(document.activeElement).toBe(input)
  fireEvent.change(input, { target: { value: 'Saved answer' } })
  const form = screen.getByRole('form', { name: 'Save result' })
  fireEvent.submit(form); fireEvent.submit(form)
  expect(roomResultsApi.save).toHaveBeenCalledTimes(1)
  expect(roomResultsApi.save).toHaveBeenCalledWith(501, { source_event_id: sourceId, kind: 'text', title: 'Saved answer', evidence: [] }, expect.any(String))
  await act(async () => pending.resolve(command))
  expect(props.onClearSource).toHaveBeenCalledTimes(1)
})
it('retains the exact command key after an ambiguous failure and never auto-retries a write', async () => {
  vi.mocked(roomResultsApi.save).mockRejectedValueOnce(new Error('connection lost'))
  view({ source }); fireEvent.change(screen.getByLabelText('Result title'), { target: { value: 'Saved answer' } })
  fireEvent.submit(screen.getByRole('form', { name: 'Save result' }))
  await screen.findByText(/change is unconfirmed/)
  expect(roomResultsApi.save).toHaveBeenCalledTimes(1)
  const key = vi.mocked(roomResultsApi.save).mock.calls[0][2]
  fireEvent.submit(screen.getByRole('form', { name: 'Save result' }))
  await waitFor(() => expect(roomResultsApi.save).toHaveBeenCalledTimes(2))
  expect(vi.mocked(roomResultsApi.save).mock.calls[1][2]).toBe(key)
})
it('rejects late callbacks from an unmounted A during A→B→A', async () => {
  const pending = deferred<typeof command>(); vi.mocked(roomResultsApi.save).mockReturnValue(pending.promise)
  const first = view({ source }); fireEvent.change(screen.getByLabelText('Result title'), { target: { value: 'Old A' } })
  fireEvent.submit(screen.getByRole('form', { name: 'Save result' })); first.unmount()
  const b = view({ roomId: 602 }); b.unmount()
  const again = view()
  await act(async () => pending.resolve(command))
  expect(first.props.onClearSource).not.toHaveBeenCalled(); expect(again.props.onClearSource).not.toHaveBeenCalled()
  expect(screen.queryByText('Result updated.')).toBeNull()
})
it('ignores a pending mutation after the authentication epoch changes', async () => {
  const pending = deferred<typeof command>(); vi.mocked(roomResultsApi.save).mockReturnValue(pending.promise)
  const current = view({ source }); fireEvent.change(screen.getByLabelText('Result title'), { target: { value: 'A' } })
  fireEvent.submit(screen.getByRole('form', { name: 'Save result' }))
  act(() => useAuthStore.getState().setAuth('other', { id: 2, username: 'Other', email: 'other@example.com' }))
  await act(async () => pending.resolve(command)); expect(current.props.onClearSource).not.toHaveBeenCalled()
})
it('creates a version with the selected current-version CAS and no carried review metadata', async () => {
  view({ source }); await screen.findByRole('button', { name: /Plan.*Text/ })
  fireEvent.change(screen.getByLabelText('Result title'), { target: { value: 'Next revision' } })
  fireEvent.change(screen.getByLabelText('Result or new version'), { target: { value: resultId } })
  fireEvent.submit(screen.getByRole('form', { name: 'Save result' }))
  await waitFor(() => expect(roomResultsApi.version).toHaveBeenCalledWith(501, resultId, { source_event_id: sourceId, kind: 'text', title: 'Next revision', evidence: [], expected_current_version: 2 }, expect.any(String)))
})
it('keeps Owner link report separate from workflow-state authority and binds state revision', async () => {
  vi.mocked(roomResultsApi.detail).mockResolvedValue(detail({ kind: 'link', content: 'https://example.com/a', link_report: 'reported_bad', state: 'final', state_owner_id: 1, state_changed_at: source.created_at, link_report_owner_id: 2, link_reported_at: source.created_at }))
  view(); fireEvent.click(await screen.findByRole('button', { name: /Plan.*Text/ }))
  await screen.findByText('Link reported unavailable by the Owner.')
  expect(screen.getAllByText(/Owner #1/)).toHaveLength(1); expect(screen.getAllByText(/Owner #2/)).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: 'Clear unavailable report' }))
  await waitFor(() => expect(roomResultsApi.linkReport).toHaveBeenCalledWith(501, resultId, 2, { report: 'unchecked', expected_state_revision: 1 }, expect.any(String)))
  expect(roomResultsApi.state).not.toHaveBeenCalled()
})
it('uses only the existing managed screenshot viewer and never labels metadata as verified bytes', async () => {
  vi.mocked(roomResultsApi.detail).mockResolvedValue(detail({ kind: 'file', content: '', author_name_snapshot: '', file_status: 'unchecked', review_support: 'unsupported', file_ref: { adapter: 'screenshot', action_id: sourceId, event_seq: 2, image_width: 640, image_height: 480 } }))
  view(); fireEvent.click(await screen.findByRole('button', { name: /Plan.*Text/ }))
  const viewer = await screen.findByTestId('managed-viewer')
  expect(JSON.parse(viewer.textContent!)).toMatchObject({ roomId: 501, actionId: sourceId, eventSeq: 2, readOnly: true })
  expect(screen.getByText('AI review of screenshot contents is not supported here.')).toBeTruthy()
  expect(screen.getAllByText(/Not recorded/).length).toBeGreaterThan(0)
})
it('keeps archived result reading available while all state changes are disabled', async () => {
  view({ active: false }); await openDetail()
  expect((screen.getByRole('button', { name: 'In review' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'In review' }))
  expect(roomResultsApi.state).not.toHaveBeenCalled()
})
it('links a peer opinion only to the selected source/version and preserves old opinions on their version', async () => {
  const review: RoomEvent = { ...source, event_id: '33333333-3333-4333-8333-333333333333', seq: 4, actor_id: 9, actor_name_snapshot: 'Peer then', payload: { content: 'Consider this option', reply_provenance: { version: 1, watermark_seq: 3, declaration_state: 'declared', provided_peer_event_ids: [sourceId], reply_to_event_ids: [sourceId] } } }
  view({ events: [source, review] }); await openDetail()
  fireEvent.click(screen.getByRole('button', { name: 'Link this opinion' }))
  await waitFor(() => expect(roomResultsApi.review).toHaveBeenCalledWith(501, resultId, 2, review.event_id, expect.any(String)))
})
it('does not render a cross-room list response', async () => {
  vi.mocked(roomResultsApi.list).mockResolvedValue({ room_id: 602, items: [{ ...summary, room_id: 602, title: 'Other room private' }], next_cursor: null })
  view(); await screen.findByRole('alert')
  expect(screen.queryByText('Other room private')).toBeNull()
})
it('loads further bounded result pages without replacing the earlier page', async () => {
  vi.mocked(roomResultsApi.list).mockResolvedValueOnce({ room_id: 501, items: [summary], next_cursor: resultId }).mockResolvedValueOnce({ room_id: 501, items: [{ ...summary, result_id: sourceId, title: 'Second result' }], next_cursor: null })
  view(); fireEvent.click(await screen.findByRole('button', { name: 'Load more' }))
  await screen.findByRole('button', { name: /Second result/ }); expect(screen.getByRole('button', { name: /Plan.*Text/ })).toBeTruthy()
  expect(roomResultsApi.list).toHaveBeenLastCalledWith(501, resultId)
})
