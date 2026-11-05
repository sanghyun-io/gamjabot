// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import RoomMemory from './RoomMemory'
import { roomMemoriesApi, type MemoryList, type MemoryEntry, type MemoryCommand } from '../api/roomMemories'
import type { RoomEvent } from '../api/events'
import type { Agent } from '../api/agents'
import { useAuthStore } from '../stores/authStore'
vi.mock('../api/roomMemories', () => ({ roomMemoriesApi: { list: vi.fn(), add: vi.fn(), edit: vi.fn(), remove: vi.fn(), legacy: vi.fn() } }))
const id = '11111111-1111-4111-8111-111111111111', sourceId = '22222222-2222-4222-8222-222222222222'
const entry: MemoryEntry = { memory_id: id, room_id: 501, category: 'decision', content: 'Original decision', scope: 'agent', target_agent_id: 8, source_kind: 'event', source_event_id: sourceId, source_seq: 2, source_url: null, source_snapshot: { name: 'Past author' }, revision: 3, created_by: 1, updated_by: 1, created_at: '2026-10-16T14:00:01', updated_at: '2026-10-16T14:00:02', deleted_at: null }
const legacy = { agent_id: 8, is_active: true, revision: 2, context_after_seq: 0, legacy_disabled: false, legacy: { prior: 'Legacy original' }, legacy_digest: 'a'.repeat(64) }
const listing = (): MemoryList => ({ room_id: 501, revision: 7, common_context_after_seq: 0, items: [entry], agents: [legacy] })
const command: MemoryCommand = { room_id: 501, memory_id: id, revision: 8, entry_revision: 4, activity_event_id: sourceId, activity_seq: 13, replayed: false }
const agents = [{ id: 8, name: 'Ada', is_active: true }, { id: 9, name: 'Other room Agent', is_active: true }] as Agent[]
const source = { event_id: sourceId, room_id: 501, seq: 2, actor_type: 'agent', actor_id: 8, actor_name_snapshot: 'Past author', kind: 'message', payload: { content: 'Source body' }, created_at: entry.created_at, device_id: 'fixture', idempotency_key: null } as RoomEvent
function deferred<T>() { let resolve!: (value: T) => void; return { promise: new Promise<T>((r) => { resolve = r }), resolve: (value: T) => resolve(value) } }
function reject(code: string, status = 409) { return { response: { status, data: { detail: { code } } } } }
beforeEach(() => {
  localStorage.removeItem('gamja.locale'); useAuthStore.getState().setAuth('owner', { id: 1, username: 'Owner', email: 'owner@example.com' })
  vi.mocked(roomMemoriesApi.list).mockResolvedValue(listing())
  for (const method of ['add', 'edit', 'remove', 'legacy'] as const) vi.mocked(roomMemoriesApi[method]).mockResolvedValue(command)
})
afterEach(() => { cleanup(); vi.clearAllMocks() })
function view(patch: Partial<React.ComponentProps<typeof RoomMemory>> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const props = { roomId: 501, ownerId: 1, authEpoch: useAuthStore.getState().epoch, active: true, visible: true, events: [source], agents, onChanged: vi.fn(), onNavigate: vi.fn(), ...patch }
  return { props, client, ...render(<QueryClientProvider client={client}><RoomMemory {...props} /></QueryClientProvider>) }
}
const edit = async () => fireEvent.click(await screen.findByRole('button', { name: 'Edit memory' }))
const form = () => screen.getByRole('form', { name: 'Memory editor' })
const ack = () => fireEvent.click(screen.getByRole('checkbox'))
const submit = () => fireEvent.submit(form())
it('does not read before its tab opens', () => { view({ visible: false }); expect(roomMemoriesApi.list).not.toHaveBeenCalled() })
it('shows source, historical name, scope, local seconds and exact source navigation', async () => {
  const current = view(); await screen.findByText('Original decision')
  expect(screen.getByText(/Only Ada in this room/)).toBeTruthy(); expect(screen.getByText('Past author')).toBeTruthy()
  expect(document.querySelector('time')?.dateTime).toBe('2026-10-16T14:00:02Z'); expect(document.querySelector('time')?.textContent).toMatch(/\d+:\d+:\d+/)
  fireEvent.click(screen.getByRole('button', { name: 'Open source #2' })); expect(current.props.onNavigate).toHaveBeenCalledWith(2, sourceId)
})
it('adds explicit manual memory without invented provenance and excludes Agents outside membership', async () => {
  view(); await screen.findByText('Original decision'); fireEvent.click(screen.getByRole('button', { name: 'Add memory' }))
  fireEvent.change(screen.getByLabelText('Memory content'), { target: { value: 'Keep this fact' } })
  fireEvent.change(screen.getByLabelText('Shared with'), { target: { value: 'agent' } })
  expect(screen.queryByRole('option', { name: 'Other room Agent' })).toBeNull()
  submit(); await waitFor(() => expect(roomMemoriesApi.add).toHaveBeenCalledWith(501, { expected_room_revision: 7, category: 'fact', content: 'Keep this fact', scope: 'agent', target_agent_id: 8, source: { kind: 'manual' } }, expect.any(String)))
})
it('requires acknowledgement of previous-history loss before edit and keeps exact canonical source CAS', async () => {
  const current = view(); await edit(); expect(screen.getByText(/also removes earlier conversation/)).toBeTruthy()
  fireEvent.change(screen.getByLabelText('Memory content'), { target: { value: 'Revised decision' } }); submit(); expect(roomMemoriesApi.edit).not.toHaveBeenCalled()
  ack(); submit(); await waitFor(() => expect(current.props.onChanged).toHaveBeenCalledTimes(1))
  expect(roomMemoriesApi.edit).toHaveBeenCalledWith(501, id, { expected_room_revision: 7, expected_revision: 3, category: 'decision', content: 'Revised decision', scope: 'agent', target_agent_id: 8, source: { kind: 'event', event_id: sourceId, seq: 2 } }, expect.any(String))
})
it('keeps immutable body and key after ambiguous response, blocks edits and same-tick duplicate, retries only explicitly', async () => {
  vi.mocked(roomMemoriesApi.edit).mockRejectedValueOnce(new Error('connection lost'))
  view(); await edit(); ack(); submit(); submit(); expect(roomMemoriesApi.edit).toHaveBeenCalledTimes(1)
  await screen.findByText(/outcome is unconfirmed/)
  expect(screen.getByLabelText('Memory content').closest('fieldset')?.disabled).toBe(true)
  const original = vi.mocked(roomMemoriesApi.edit).mock.calls[0]; expect(roomMemoriesApi.edit).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByRole('button', { name: 'Retry the same change' })); await screen.findByText('Memory updated.')
  expect(vi.mocked(roomMemoriesApi.edit).mock.calls[1]).toEqual(original)
})
it('preserves proposed input on CAS conflict and requires comparison plus explicit rebase and fresh acknowledgement', async () => {
  vi.mocked(roomMemoriesApi.edit).mockRejectedValueOnce(reject('memory_revision_conflict'))
  view(); await edit(); fireEvent.change(screen.getByLabelText('Memory content'), { target: { value: 'My edit' } }); ack()
  vi.mocked(roomMemoriesApi.list).mockResolvedValue({ ...listing(), revision: 9, items: [{ ...entry, revision: 5, content: 'Concurrent edit' }] })
  submit(); await screen.findByText('Latest saved content'); await waitFor(() => expect(within(form()).getByText('Concurrent edit')).toBeTruthy())
  expect((screen.getByLabelText('Memory content') as HTMLTextAreaElement).value).toBe('My edit')
  submit(); expect(roomMemoriesApi.edit).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByRole('button', { name: 'Use latest revision after comparison' })); submit(); expect(roomMemoriesApi.edit).toHaveBeenCalledTimes(1)
  ack(); submit(); await waitFor(() => expect(roomMemoriesApi.edit).toHaveBeenCalledTimes(2))
  expect(vi.mocked(roomMemoriesApi.edit).mock.calls[1][2]).toMatchObject({ expected_room_revision: 9, expected_revision: 5, content: 'My edit' })
})
it('shows busy and budget refusals without auto retry or bypassing Stop', async () => {
  vi.mocked(roomMemoriesApi.edit).mockRejectedValueOnce(reject('memory_room_busy'))
  view(); await edit(); ack(); submit(); await screen.findByText(/room is busy/)
  expect(roomMemoriesApi.edit).toHaveBeenCalledTimes(1)
  vi.mocked(roomMemoriesApi.edit).mockRejectedValueOnce(reject('memory_context_too_large', 422)); submit(); await screen.findByText(/character budget/)
})
it('deletes with explicit impact confirmation and matching entry/room revision only', async () => {
  view(); fireEvent.click(await screen.findByRole('button', { name: 'Delete memory' })); submit(); expect(roomMemoriesApi.remove).not.toHaveBeenCalled()
  ack(); submit(); await waitFor(() => expect(roomMemoriesApi.remove).toHaveBeenCalledWith(501, id, { expected_room_revision: 7, expected_revision: 3 }, expect.any(String)))
})
it('atomically replaces legacy memory with one scoped explicit entry and does not copy JSON automatically', async () => {
  view(); fireEvent.click(await screen.findByRole('button', { name: 'Replace with an entry' }))
  expect((screen.getByLabelText('Memory content') as HTMLTextAreaElement).value).toBe('')
  expect((screen.getByLabelText('Shared with') as HTMLSelectElement).disabled).toBe(true)
  fireEvent.change(screen.getByLabelText('Memory content'), { target: { value: 'Explicit replacement' } }); ack(); submit()
  await waitFor(() => expect(roomMemoriesApi.legacy).toHaveBeenCalledWith(501, 8, { expected_room_revision: 7, expected_agent_revision: 2, expected_legacy_digest: 'a'.repeat(64), operation: 'replace', replacement: { expected_room_revision: 7, category: 'fact', content: 'Explicit replacement', scope: 'agent', target_agent_id: 8, source: { kind: 'manual' } } }, expect.any(String)))
})
it('disables legacy use while preserving original JSON display', async () => {
  view(); fireEvent.click(await screen.findByRole('button', { name: 'Disable legacy use' })); ack(); submit()
  await waitFor(() => expect(roomMemoriesApi.legacy).toHaveBeenCalledWith(501, 8, expect.objectContaining({ operation: 'disable', replacement: null, expected_legacy_digest: 'a'.repeat(64) }), expect.any(String)))
  expect(screen.getByText(/Legacy original/)).toBeTruthy()
})
it('rejects unsafe source URL without a request and sends only exact selected event reference', async () => {
  view(); await screen.findByText('Original decision'); fireEvent.click(screen.getByRole('button', { name: 'Add memory' })); fireEvent.change(screen.getByLabelText('Memory content'), { target: { value: 'Fact' } })
  fireEvent.change(screen.getByLabelText('Source'), { target: { value: 'link' } }); fireEvent.change(screen.getByLabelText('Source URL'), { target: { value: 'javascript:alert(1)' } }); submit(); expect(roomMemoriesApi.add).not.toHaveBeenCalled()
  fireEvent.change(screen.getByLabelText('Source'), { target: { value: 'event' } }); fireEvent.change(screen.getByLabelText('Source event'), { target: { value: sourceId } }); submit()
  await waitFor(() => expect(roomMemoriesApi.add).toHaveBeenCalledWith(501, expect.objectContaining({ source: { kind: 'event', event_id: sourceId, seq: 2 } }), expect.any(String)))
})
it('ignores callbacks from an unmounted A during A→B→A', async () => {
  const pending = deferred<MemoryCommand>(); vi.mocked(roomMemoriesApi.edit).mockReturnValue(pending.promise)
  const first = view(); await edit(); ack(); submit(); first.unmount()
  const b = view({ roomId: 602 }); b.unmount(); const again = view()
  await act(async () => pending.resolve(command)); expect(first.props.onChanged).not.toHaveBeenCalled(); expect(again.props.onChanged).not.toHaveBeenCalled(); expect(screen.queryByText('Memory updated.')).toBeNull()
})
it('rejects a callback after Owner epoch change but allows token-only renewal', async () => {
  const pending = deferred<MemoryCommand>(); vi.mocked(roomMemoriesApi.edit).mockReturnValue(pending.promise)
  const first = view(); await edit(); ack(); submit()
  act(() => useAuthStore.getState().setAuth('other', { id: 2, username: 'Other', email: 'other@example.com' }))
  await act(async () => pending.resolve(command)); expect(first.props.onChanged).not.toHaveBeenCalled()
})
it('rejects a wrong-room list and disables all archived-room writes', async () => {
  vi.mocked(roomMemoriesApi.list).mockResolvedValue({ ...listing(), room_id: 602 }); const first = view(); await screen.findByText(/Could not load room memory/); expect(screen.queryByText('Original decision')).toBeNull(); first.unmount()
  vi.mocked(roomMemoriesApi.list).mockResolvedValue(listing()); view({ active: false }); await screen.findByText('Original decision'); expect((screen.getByRole('button', { name: 'Edit memory' }) as HTMLButtonElement).disabled).toBe(true)
})

it('accepts token-only renewal without treating it as an Owner change', async () => {
  const pending = deferred<MemoryCommand>(); vi.mocked(roomMemoriesApi.edit).mockReturnValue(pending.promise)
  const first = view(); await edit(); ack(); submit()
  const epoch = useAuthStore.getState().epoch
  act(() => useAuthStore.setState({ token: 'renewed-token' }))
  expect(useAuthStore.getState().epoch).toBe(epoch)
  await act(async () => pending.resolve(command)); expect(first.props.onChanged).toHaveBeenCalledTimes(1)
})
