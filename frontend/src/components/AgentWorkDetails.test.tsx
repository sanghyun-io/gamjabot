// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import AgentWorkDetails from './AgentWorkDetails'
import type { WorkState } from '../lib/workState'

afterEach(cleanup)
const request = { event_id: '550e8400-e29b-41d4-a716-446655440000', seq: 2 }
const result = { event_id: '550e8400-e29b-41d4-a716-446655440001', seq: 4 }
const state: WorkState = { version: 1, room_id: 1, agent_id: 8, scope: 'current', phase: 'approval_waiting',
  trigger_event_id: request.event_id, generation: 4, round: 1, peer_agent_ids: [], request, result, action_id: result.event_id }

it('moves by exact seq and UUID with keyboard and pointer, while technical information starts folded', async () => {
  const onNavigate = vi.fn(), onAction = vi.fn(), user = userEvent.setup()
  const view = render(<AgentWorkDetails state={state} onNavigate={onNavigate} onAction={onAction} />)
  expect(view.container.querySelector('details')?.open).toBe(false)
  await user.tab(); await user.keyboard('{Enter}')
  expect(onNavigate).toHaveBeenLastCalledWith(2, request.event_id)
  await user.click(screen.getByRole('button', { name: 'Result' }))
  expect(onNavigate).toHaveBeenLastCalledWith(4, result.event_id)
  await user.click(screen.getByRole('button', { name: 'View approval' }))
  expect(onAction).toHaveBeenCalledWith(result.event_id)
})

it('does not offer approval for thinking, previous completion, or unavailable navigation', () => {
  const view = render(<AgentWorkDetails state={{ ...state, phase: 'completed', scope: 'previous' }} onAction={vi.fn()} />)
  expect(screen.queryByRole('button', { name: 'View approval' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Request' })).toBeNull()
  view.rerender(<AgentWorkDetails state={{ ...state, scope: 'none', phase: 'idle' }} />)
  expect(view.container.firstChild).toBeNull()
})
