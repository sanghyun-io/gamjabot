// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render as renderComponent, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import RoomMembersSummary from './RoomMembersSummary'
import type { RoomMember } from '../api/conversations'
import type { Agent } from '../api/agents'

const clients: QueryClient[] = []
function render(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(client)
  return renderComponent(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}
afterEach(() => { cleanup(); clients.splice(0).forEach(client => client.clear()) })
const members: RoomMember[] = [8,9,10,11].map((id) => ({ room_id: 501, agent_id: id, is_active: true, joined_at: '', run_state: id===8 ? 'running' : 'idle' }))
const agents = [{ id:8,name:'Ada' }, { id:9,name:'Bea' }] as Agent[]

it('shows a bounded character group and total count using actual member states', () => {
  const open = vi.fn()
  const view=render(<RoomMembersSummary roomId={501} members={members} agents={agents} actions={[]} events={[]} connection="live" open={false} dialogId="management501" onOpen={open} />)
  expect(view.container.querySelectorAll('.agent-character')).toHaveLength(3)
  expect(view.container.querySelectorAll('.agent-character--thinking')).toHaveLength(1)
  const trigger=screen.getByRole('button',{ name:'Conversation options' })
  expect(trigger.title).toContain('Ada')
  expect(trigger.title).toContain('Agent #10')
  expect(trigger.textContent).toBe('4')
  expect(trigger.getAttribute('aria-expanded')).toBe('false')
  expect(trigger.getAttribute('aria-haspopup')).toBe('dialog')
  fireEvent.click(trigger);expect(open).toHaveBeenCalledTimes(1)
})

it('does not present a stale running state as current when the room connection is unavailable', () => {
  const view=render(<RoomMembersSummary roomId={501} members={members} agents={agents} actions={[]} events={[]} connection="retrying" open dialogId="management501" onOpen={()=>{}} />)
  expect(view.container.querySelector('.agent-character--thinking')).toBeNull()
  expect(view.container.querySelectorAll('.agent-character--unknown')).toHaveLength(3)
  expect(screen.getByRole('button').getAttribute('aria-controls')).toBe('management501')
})

it('retains a discoverable options button for an empty room without inventing members', () => {
  render(<RoomMembersSummary roomId={501} members={[]} agents={[]} actions={[]} events={[]} connection="live" open={false} dialogId="management501" onOpen={()=>{}} />)
  expect(screen.getByRole('button',{name:'Conversation options'}).textContent).toBe('0')
  expect(document.querySelector('.agent-character')).toBeNull()
})
