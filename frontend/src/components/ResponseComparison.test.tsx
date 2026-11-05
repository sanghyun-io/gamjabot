// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import type { RoomEvent } from '../api/events'
import ResponseComparison from './ResponseComparison'

afterEach(cleanup)

const user = (seq: number, content: string): RoomEvent => ({ event_id: `u${seq}`, device_id: null, room_id: 501, seq,
  actor_type: 'user', actor_id: 1, actor_name_snapshot: 'Owner', kind: 'message', payload: { content },
  created_at: '2026-10-04T00:00:00Z', idempotency_key: null })
const reply = (seq: number, trigger: string, round: number, actor: string, model: string): RoomEvent => ({
  event_id: `a${seq}`, device_id: null, room_id: 501, seq, actor_type: 'agent', actor_id: seq,
  actor_name_snapshot: actor, kind: 'message', payload: { content: `${actor} response`, trigger_event_id: trigger, round,
    model_use: { connection_id: seq, model_id: model, provider_kind: 'openai', watermark_seq: seq - 1 } },
  created_at: '2026-10-04T00:00:00Z', idempotency_key: null,
})

const events = [user(1, 'Plan the launch'), reply(2, 'u1', 1, 'Ada then', 'historic-a'), reply(3, 'u1', 1, 'Bea then', 'historic-b'),
  reply(4, 'u1', 2, 'Ada then', 'historic-a')]

it('is collapsed by default and distinguishes incomplete history from no comparable responses', () => {
  const { rerender } = render(<ResponseComparison events={events} historyState="loading" onNavigateSeq={vi.fn()} />)
  const panel = screen.getByText('Compare responses').closest('details') as HTMLDetailsElement
  expect(panel.open).toBe(false)
  expect(panel.getAttribute('name')).toBe('room-panel')
  fireEvent.click(screen.getByText('Compare responses'))
  expect(screen.getByText('Loading complete room history…')).toBeTruthy()
  rerender(<ResponseComparison events={events} historyState="error" onNavigateSeq={vi.fn()} />)
  expect(screen.getByText('Complete room history is unavailable, so responses cannot be compared safely.')).toBeTruthy()
  rerender(<ResponseComparison events={[user(1, 'Only request')]} historyState="complete" onNavigateSeq={vi.fn()} />)
  expect(screen.getByText('No comparable agent responses are available in this room history.')).toBeTruthy()
})

it('shows exact request rounds, partial counts, historical model snapshots, and sequence navigation', () => {
  const navigate = vi.fn()
  render(<ResponseComparison events={events} historyState="complete" onNavigateSeq={navigate} />)
  fireEvent.click(screen.getByText('Compare responses'))
  expect(screen.getByText('Plan the launch')).toBeTruthy()
  expect(screen.getByText('1 response in round 2')).toBeTruthy()
  expect(screen.getAllByText('Ada then')).toHaveLength(1)
  expect(screen.queryByText('Bea then')).toBeNull()

  fireEvent.change(screen.getByRole('combobox', { name: 'Round' }), { target: { value: '1' } })
  expect(screen.getByText('2 responses in round 1')).toBeTruthy()
  expect(screen.getByText('Bea then')).toBeTruthy()
  const model = screen.getAllByText('Historical model details')[0].closest('details') as HTMLDetailsElement
  expect(model.open).toBe(false)
  fireEvent.click(within(model).getByText('Historical model details'))
  expect(within(model).getByText('historic-a')).toBeTruthy()

  fireEvent.click(screen.getByRole('button', { name: 'Go to request event 1' }))
  expect((screen.getByText('Compare responses').closest('details') as HTMLDetailsElement).open).toBe(false)
  fireEvent.click(screen.getByText('Compare responses'))
  fireEvent.click(screen.getByRole('button', { name: 'Go to response event 2' }))
  expect(navigate.mock.calls).toEqual([[1], [2]])
})

it('closes another top-level room panel on toggle without relying on native details grouping', () => {
  const rendered = render(<div className="room-column"><details name="room-panel" data-testid="other"><summary>Other</summary></details>
    <ResponseComparison events={events} historyState="complete" onNavigateSeq={vi.fn()} /></div>)
  const other = screen.getByTestId('other') as HTMLDetailsElement
  const comparison = rendered.container.querySelector('.response-comparison') as HTMLDetailsElement
  other.open = true
  comparison.open = true
  comparison.dispatchEvent(new Event('toggle', { bubbles: true }))
  expect(other.open).toBe(false)
  expect(comparison.open).toBe(true)
})
