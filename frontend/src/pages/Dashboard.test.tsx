// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Dashboard from './Dashboard'
import { dashboardApi, type DashboardSummary } from '../api/dashboard'

vi.mock('../api/dashboard', () => ({ dashboardApi: { get: vi.fn() } }))
vi.mock('../components/FirstSuccessGuide', () => ({ default: () => null }))
let authState = { user: { id: 1, username: 'Owner', email: 'owner@example.test' }, logout: vi.fn(), epoch: 1 }
vi.mock('../stores/authStore', () => ({
  useAuthStore: Object.assign(() => authState, { getState: () => authState }),
}))

afterEach(() => {
  cleanup(); vi.clearAllMocks(); vi.useRealTimers()
  authState = { user: { id: 1, username: 'Owner', email: 'owner@example.test' }, logout: vi.fn(), epoch: 1 }
})

it('updates a CLI-started and completed job while the dashboard stays open, then stops polling on unmount', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  const room = { ...completeSummary.rooms[0], work_status: 'running' as const, waiting_approvals: 0, active_failures: 0 }
  vi.mocked(dashboardApi.get)
    .mockResolvedValueOnce({ ...completeSummary, rooms: [room] })
    .mockResolvedValue({ ...completeSummary, rooms: [{ ...room, work_status: 'idle', last_authored_preview: 'The job completed.' }] })
  const view = renderDashboard()
  await screen.findByText('Working')
  await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
  expect(await screen.findByText('The job completed.')).toBeTruthy()
  expect(screen.queryByText('Working')).toBeNull()
  view.unmount()
  const calls = vi.mocked(dashboardApi.get).mock.calls.length
  await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
  expect(dashboardApi.get).toHaveBeenCalledTimes(calls)
})

const completeSummary: DashboardSummary = {
  setup: { next_action: null },
  counts: {
    enabled_connections: 2, ready_agents: 4, ready_projects: 2, active_rooms: 3,
    unread_authored: 5, waiting_approvals: 2, active_failures: 1,
  },
  rooms: [
    {
      room_id: 22, room_title: 'Release room', project_id: 4, project_name: 'Desktop',
      last_event_seq: 18, last_activity_at: '2026-10-03T10:00:00Z', last_authored_seq: 17,
      last_authored_at: '2026-10-03T09:59:00Z', last_authored_preview: 'Review is waiting for approval.',
      unread_authored: 3, waiting_approvals: 2, active_failures: 0, work_status: 'waiting',
    },
    {
      room_id: 11, room_title: 'Fix room', project_id: 3, project_name: 'Core',
      last_event_seq: 9, last_activity_at: '2026-10-03T09:00:00Z', last_authored_seq: 8,
      last_authored_at: '2026-10-03T08:59:00Z', last_authored_preview: 'The latest run needs attention.',
      unread_authored: 2, waiting_approvals: 0, active_failures: 1, work_status: 'paused',
    },
  ],
}

function renderDashboard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<MemoryRouter><QueryClientProvider client={client}><Dashboard /></QueryClientProvider></MemoryRouter>)
}

it.each([
  ['connection', 'Set up a connection', '/connections'],
  ['agent', 'Create a ready agent', '/agents'],
  ['project', 'Create a ready project', '/projects'],
  ['room', 'Start your first room', '/projects'],
] as const)('shows only the %s setup action with the correct destination', async (nextAction, label, href) => {
  vi.mocked(dashboardApi.get).mockResolvedValue({ ...completeSummary, setup: { next_action: nextAction }, rooms: [] })
  renderDashboard()

  const guidance = await screen.findByRole('region', { name: 'Next action' })
  const cta = within(guidance).getByRole('link', { name: label })
  expect(cta.getAttribute('href')).toBe(href)
  expect(within(guidance).getAllByRole('link')).toHaveLength(1)
})

it('uses one aggregate request and renders the server-ordered active work queue with resumable links', async () => {
  vi.mocked(dashboardApi.get).mockResolvedValue(completeSummary)
  renderDashboard()

  const queue = await screen.findByRole('region', { name: 'Recent active work' })
  const rows = within(queue).getAllByRole('article')
  expect(screen.getByText('Ready agents')).toBeTruthy()
  expect(screen.getByText('Ready projects')).toBeTruthy()
  expect(dashboardApi.get).toHaveBeenCalledTimes(1)
  expect(dashboardApi.get).toHaveBeenCalledWith(6)
  expect(rows).toHaveLength(2)
  expect(rows[0].textContent).toContain('Release room')
  expect(rows[0].textContent).toContain('3 unread')
  expect(rows[0].textContent).toContain('2 awaiting approval')
  expect(within(rows[0]).getByRole('link', { name: 'Resume Release room' }).getAttribute('href')).toBe('/chat/22')
  expect(rows[1].textContent).toContain('Fix room')
  expect(rows[1].textContent).toContain('Needs attention')
  expect(screen.getByText('5 unread messages')).toBeTruthy()
})

it('keeps loading, failed, and healthy empty dashboard states distinct', async () => {
  let settle!: (value: DashboardSummary) => void
  vi.mocked(dashboardApi.get).mockReturnValue(new Promise((resolve) => { settle = resolve }))
  const view = renderDashboard()
  expect(screen.getByRole('status').textContent).toContain('Loading workspace activity')
  expect(screen.queryByText('No active rooms yet.')).toBeNull()

  settle({ ...completeSummary, setup: { next_action: 'room' }, rooms: [], counts: { ...completeSummary.counts, active_rooms: 0 } })
  expect(await screen.findByRole('link', { name: 'Start your first room' })).toBeTruthy()
  expect(screen.queryByText('No active rooms yet.')).toBeNull()
  view.unmount()

  vi.mocked(dashboardApi.get).mockRejectedValue(new Error('offline'))
  renderDashboard()
  const alert = await screen.findByRole('alert')
  expect(alert.textContent).toContain('Workspace activity is unavailable.')
  expect(screen.queryByText('No active rooms yet.')).toBeNull()
})

it('retries a failed aggregate request from the unavailable state', async () => {
  vi.mocked(dashboardApi.get).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(completeSummary)
  renderDashboard()
  await userEvent.click(await screen.findByRole('button', { name: 'Retry' }))
  expect(await screen.findByText('Release room')).toBeTruthy()
  expect(dashboardApi.get).toHaveBeenCalledTimes(2)
})

it('does not render a late dashboard response from a previous owner epoch', async () => {
  let settleOld!: (value: DashboardSummary) => void
  vi.mocked(dashboardApi.get)
    .mockReturnValueOnce(new Promise((resolve) => { settleOld = resolve }))
    .mockResolvedValueOnce({ ...completeSummary, rooms: [{ ...completeSummary.rooms[0], room_title: 'New owner room' }] })
  const view = renderDashboard()

  authState = { user: { id: 2, username: 'New owner', email: 'new@example.test' }, logout: vi.fn(), epoch: 2 }
  view.rerender(<MemoryRouter><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><Dashboard /></QueryClientProvider></MemoryRouter>)
  expect(await screen.findByText('New owner room')).toBeTruthy()

  settleOld({ ...completeSummary, rooms: [{ ...completeSummary.rooms[0], room_title: 'Old owner room' }] })
  await waitFor(() => expect(screen.queryByText('Old owner room')).toBeNull())
})
