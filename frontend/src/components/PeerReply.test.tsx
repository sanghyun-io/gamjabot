// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import PeerReply from './PeerReply'
import type { RoomEvent } from '../api/events'
const sourceId = '11111111-1111-4111-8111-111111111111'
const secondId = '22222222-2222-4222-8222-222222222222'
const source: RoomEvent = { event_id: sourceId, room_id: 1, seq: 1, actor_type: 'agent', actor_id: 8,
  actor_name_snapshot: 'Original name', kind: 'message', payload: { content: '<think>private reasoning</think>\nVisible peer' },
  device_id: null, created_at: '', idempotency_key: null }
const answer: RoomEvent = { ...source, event_id: '33333333-3333-4333-8333-333333333333', seq: 3, actor_id: 9,
  payload: { content: 'answer', reply_provenance: { version: 1, watermark_seq: 2, declaration_state: 'declared',
    provided_peer_event_ids: [sourceId, secondId], reply_to_event_ids: [sourceId] } } }

afterEach(cleanup)

it('opens an exact immutable source by keyboard and collapses additional replies', async () => {
  const onOpen = vi.fn()
  const both = { ...answer, payload: { reply_provenance: { ...answer.payload.reply_provenance as object,
    reply_to_event_ids: [sourceId, secondId] } } }
  render(<PeerReply answer={both} events={[source, { ...source, event_id: secondId, seq: 2 }]} loading={false} onOpen={onOpen} />)
  const first = screen.getByRole('button', { name: 'Open original: Original name, #1' })
  expect(screen.queryByText('private reasoning')).toBeNull()
  expect(screen.getByText('1 more').closest('details')?.open).toBe(false)
  first.focus(); await userEvent.keyboard('{Enter}')
  expect(onOpen).toHaveBeenCalledWith(1, sourceId)
  fireEvent.click(screen.getByText('1 more'))
  expect(screen.getByText('1 more').closest('details')?.open).toBe(true)
  expect(screen.getAllByText('Visible peer')).toHaveLength(2)
  expect(screen.getByText('About these replies').closest('details')?.open).toBe(false)
})

it('shows no foreign identity/preview or wrong jump when an original is unavailable', () => {
  const onOpen = vi.fn()
  const { rerender } = render(<PeerReply answer={answer} events={[{ ...source, room_id: 2 }]} loading onOpen={onOpen} />)
  expect(screen.getByText('Loading conversation…')).toBeTruthy()
  expect(screen.queryByText('Original name')).toBeNull()
  expect(screen.queryByRole('button')).toBeNull()
  rerender(<PeerReply answer={answer} events={[]} loading={false} onOpen={onOpen} />)
  expect(screen.getByText('Original message unavailable')).toBeTruthy()
  expect(onOpen).not.toHaveBeenCalled()
})
