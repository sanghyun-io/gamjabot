// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { RoomEvent } from '../api/events'
import HandoffNote from './HandoffNote'
import { summariesApi } from '../api/summaries'

vi.mock('../api/summaries', () => ({ summariesApi: { create: vi.fn() } }))
let authState = { user: { id: 1 }, epoch: 1 }
vi.mock('../stores/authStore', () => ({ useAuthStore: Object.assign(() => authState, { getState: () => authState }) }))
afterEach(() => { cleanup(); vi.clearAllMocks(); authState = { user: { id: 1 }, epoch: 1 } })

const row = (seq: number, actorType: RoomEvent['actor_type'], kind: RoomEvent['kind'], content: string, payload: Record<string, unknown> = {}): RoomEvent => ({
  event_id: `e${seq}`, device_id: null, room_id: 501, seq, actor_type: actorType,
  actor_id: actorType === 'user' ? 1 : actorType === 'agent' ? 8 : null,
  actor_name_snapshot: actorType === 'user' ? 'Owner' : actorType === 'agent' ? 'Ada' : 'System', kind,
  payload: { content, ...payload }, created_at: `2026-10-04T00:0${seq}:00Z`, idempotency_key: null,
})
const events = [row(1, 'user', 'message', 'Start here'), row(2, 'tool', 'tool_result', 'private tool output'),
  row(3, 'agent', 'message', 'Decision from Ada'), row(4, 'system', 'summary', 'Older note', { owner_checkpoint: true, source_start_seq: 1, source_end_seq: 3 }),
  row(5, 'user', 'message', 'Latest request')]
const saved = row(6, 'system', 'summary', 'Ship with staged rollout', { owner_checkpoint: true, source_start_seq: 3, source_end_seq: 5 })

function view(props: Partial<React.ComponentProps<typeof HandoffNote>> = {}) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  const wrap = (node: React.ReactNode) => <QueryClientProvider client={client}>{node}</QueryClientProvider>
  const rendered = render(wrap(<div className="room-column"><HandoffNote roomId={501} active authEpoch={1} events={events}
    historyState="complete" onSaved={vi.fn()} {...props} /></div>))
  return { ...rendered, wrap }
}

it('stays hidden by default and gates creation on complete history and active rooms', () => {
  const rendered = view({ historyState: 'loading' })
  const panel = screen.getByText('Handoff note').closest('details') as HTMLDetailsElement
  expect(panel.open).toBe(false)
  expect(screen.queryByText('Start here')).toBeNull()
  fireEvent.click(screen.getByText('Handoff note'))
  expect(screen.getByText('Loading complete room history…')).toBeTruthy()
  rendered.rerender(rendered.wrap(<div className="room-column"><HandoffNote roomId={501} active authEpoch={1} events={events}
    historyState="error" onSaved={vi.fn()} /></div>))
  expect(screen.getByText('Complete room history is required before saving a handoff note.')).toBeTruthy()
  rendered.rerender(rendered.wrap(<div className="room-column"><HandoffNote roomId={501} active={false} authEpoch={1} events={events}
    historyState="complete" onSaved={vi.fn()} /></div>))
  expect(screen.getByText('This closed room’s handoff note is read-only.')).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Save handoff note' })).toBeNull()
})

it('shows the latest note, authored-only range choices, exact preview, and full contiguous sequence body', async () => {
  vi.mocked(summariesApi.create).mockResolvedValue(saved)
  const onSaved = vi.fn()
  view({ onSaved })
  fireEvent.click(screen.getAllByText('Handoff note')[0])
  expect(screen.getByText('Older note')).toBeTruthy()
  expect(screen.queryByText('private tool output')).toBeNull()
  const current = screen.getByRole('region', { name: 'Current handoff note' })
  fireEvent.click(within(current).getByText('Included conversation'))
  expect(within(current).getByText(/Owner · Start here/)).toBeTruthy()
  expect(within(current).getByText(/Ada · Decision from Ada/)).toBeTruthy()
  expect(within(current).queryByText(/Latest request/)).toBeNull()
  expect(within(current).queryByText(/private tool output/)).toBeNull()
  fireEvent.change(screen.getByRole('combobox', { name: 'First message' }), { target: { value: '3' } })
  fireEvent.change(screen.getByRole('textbox', { name: 'Note for the next request' }), { target: { value: 'Ship with staged rollout' } })
  expect(within(screen.getByRole('region', { name: 'Preview' })).getByText('Ship with staged rollout')).toBeTruthy()
  expect(within(screen.getByRole('region', { name: 'Preview' })).getByText('Includes 3 conversation records.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Save handoff note' }))
  await waitFor(() => expect(summariesApi.create).toHaveBeenCalledWith(501, {
    source_start_seq: 3, source_end_seq: 5, content: 'Ship with staged rollout',
  }, expect.stringMatching(/^[0-9a-f-]{36}$/)))
  expect(onSaved).toHaveBeenCalledWith(saved)
})

it('keeps the draft and retry key on failure, renews the key after edits, and explains busy work', async () => {
  vi.mocked(summariesApi.create).mockRejectedValueOnce(new Error('offline')).mockRejectedValueOnce(new Error('offline'))
    .mockRejectedValueOnce(Object.assign(new Error('busy'), { response: { status: 409, data: { detail: { code: 'checkpoint_source_busy' } } } }))
  view()
  fireEvent.click(screen.getAllByText('Handoff note')[0])
  const note = screen.getByRole('textbox', { name: 'Note for the next request' }) as HTMLTextAreaElement
  fireEvent.change(note, { target: { value: 'Keep exact wording' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save handoff note' }))
  expect(await screen.findByText('Could not save the handoff note. Your draft is still here.')).toBeTruthy()
  const firstKey = vi.mocked(summariesApi.create).mock.calls[0][2]
  fireEvent.click(screen.getByRole('button', { name: 'Retry saving handoff note' }))
  await waitFor(() => expect(summariesApi.create).toHaveBeenCalledTimes(2))
  expect(vi.mocked(summariesApi.create).mock.calls[1][2]).toBe(firstKey)
  fireEvent.change(note, { target: { value: 'Edited wording' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save handoff note' }))
  expect(await screen.findByText('Wait for current work in this range to finish, then retry.')).toBeTruthy()
  expect(vi.mocked(summariesApi.create).mock.calls[2][2]).not.toBe(firstKey)
  expect(note.value).toBe('Edited wording')
})

it('rejects notes over 8000 characters and ignores a late result after the owner epoch changes', async () => {
  let finish!: (value: RoomEvent) => void
  vi.mocked(summariesApi.create).mockReturnValue(new Promise((resolve) => { finish = resolve }))
  const onSaved = vi.fn()
  const rendered = view({ onSaved })
  fireEvent.click(screen.getAllByText('Handoff note')[0])
  const note = screen.getByRole('textbox', { name: 'Note for the next request' })
  fireEvent.change(note, { target: { value: 'x'.repeat(8001) } })
  fireEvent.click(screen.getByRole('button', { name: 'Save handoff note' }))
  expect(screen.getByText('Enter a note of 1 to 8,000 characters.')).toBeTruthy()
  expect(summariesApi.create).not.toHaveBeenCalled()
  fireEvent.change(note, { target: { value: 'Valid note' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save handoff note' }))
  await waitFor(() => expect(summariesApi.create).toHaveBeenCalledTimes(1))
  authState = { user: { id: 2 }, epoch: 2 }
  rendered.rerender(rendered.wrap(<div className="room-column"><HandoffNote roomId={602} active authEpoch={2} events={[]}
    historyState="complete" onSaved={onSaved} /></div>))
  finish(saved)
  await waitFor(() => expect(onSaved).not.toHaveBeenCalled())
})
