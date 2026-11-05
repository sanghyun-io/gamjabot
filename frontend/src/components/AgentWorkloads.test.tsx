// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import AgentWorkloads from './AgentWorkloads'
import { agentWorkloadsApi, type AgentWorkload } from '../api/agentWorkloads'
import { useAuthStore } from '../stores/authStore'

vi.mock('../api/agentWorkloads', () => ({ agentWorkloadsApi: { list: vi.fn() } }))
const workload: AgentWorkload = { agent_id: 8, agent_name: 'Ada', agent_active: true, overall_state: 'running',
  counts: { running: 1, waiting: 1, paused: 0, failed: 1, idle: 0, unknown: 0, total_rooms: 3 }, rooms_truncated: false,
  rooms: [
    { room_id: 10, room_title: 'Release', project_id: 2, project_name: 'Desktop', state: 'running', trigger_event_id: 'request-1', trigger_seq: 4,
      failure_event_id: null, failure_seq: null, last_activity_at: '2026-10-05T00:02:00Z', href: '/chat/10?seq=4' },
    { room_id: 11, room_title: 'Review', project_id: 3, project_name: 'Docs', state: 'waiting', trigger_event_id: 'request-2', trigger_seq: 9,
      failure_event_id: null, failure_seq: null, last_activity_at: '2026-10-05T00:01:00Z', href: '/chat/11?seq=9' },
    { room_id: 12, room_title: 'Failure', project_id: 3, project_name: 'Docs', state: 'failed', trigger_event_id: 'request-3', trigger_seq: 1,
      failure_event_id: 'failure-3', failure_seq: 6, last_activity_at: '2026-10-05T00:00:00Z', href: '/chat/12?seq=6&failure=failure-3' },
  ] }
function Location() { const location = useLocation(); return <p>{location.pathname}{location.search}</p> }

beforeEach(() => { useAuthStore.getState().setAuth('owner', { id: 1, email: 'owner@example.com', username: 'owner' }); vi.mocked(agentWorkloadsApi.list).mockResolvedValue({ items: [workload], next_cursor: null }) })
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('loads on demand and shows one Agent working truthfully across several rooms', async () => {
  render(<MemoryRouter><AgentWorkloads/><Routes><Route path="*" element={<Location/>}/></Routes></MemoryRouter>)
  expect(agentWorkloadsApi.list).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Work across rooms' }))
  const dialog = await screen.findByRole('dialog', { name: 'Agent work across rooms' })
  expect(within(dialog).getByText('1 running')).toBeTruthy()
  expect(within(dialog).getByText('1 waiting')).toBeTruthy()
  expect(within(dialog).getByText('1 need attention')).toBeTruthy()
  expect(within(dialog).getAllByRole('link')).toHaveLength(3)
  fireEvent.click(within(dialog).getByRole('link', { name: /Failure/ }))
  expect(await screen.findByText('/chat/12?seq=6&failure=failure-3')).toBeTruthy()
})

it('forwards the opaque cursor and preserves loaded Agents if the next page fails', async () => {
  vi.mocked(agentWorkloadsApi.list).mockResolvedValueOnce({ items: [workload], next_cursor: 'opaque+/cursor==' }).mockRejectedValueOnce(new Error('offline'))
  render(<MemoryRouter><AgentWorkloads/></MemoryRouter>)
  fireEvent.click(screen.getByRole('button', { name: 'Work across rooms' }))
  await screen.findByText('Ada')
  fireEvent.click(screen.getByRole('button', { name: 'Load more' }))
  await screen.findByText('Could not load more agents.')
  expect(agentWorkloadsApi.list).toHaveBeenLastCalledWith('opaque+/cursor==')
  expect(screen.getByText('Release')).toBeTruthy()
})

it('clears the prior owner immediately and ignores a late response', async () => {
  let finish!: (page: { items: AgentWorkload[]; next_cursor: null }) => void
  vi.mocked(agentWorkloadsApi.list).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  render(<MemoryRouter><AgentWorkloads/></MemoryRouter>)
  fireEvent.click(screen.getByRole('button', { name: 'Work across rooms' }))
  expect(await screen.findByRole('dialog')).toBeTruthy()
  act(() => useAuthStore.getState().setAuth('other', { id: 2, email: 'other@example.com', username: 'other' }))
  act(() => finish({ items: [workload], next_cursor: null }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(screen.queryByText('Ada')).toBeNull()
})

it('shows explicit unavailable and empty states without room actions', async () => {
  vi.mocked(agentWorkloadsApi.list).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ items: [], next_cursor: null })
  render(<MemoryRouter><AgentWorkloads/></MemoryRouter>)
  fireEvent.click(screen.getByRole('button', { name: 'Work across rooms' }))
  expect(await screen.findByText('Agent workloads are unavailable.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(await screen.findByText('No agents are available.')).toBeTruthy()
  expect(screen.queryByText(/Approve|Retry failed/)).toBeNull()
})

it('labels paused and unknown inactive work without presenting it as idle', async () => {
  vi.mocked(agentWorkloadsApi.list).mockResolvedValue({ items: [
    { ...workload, agent_id: 9, agent_name: 'Paused agent', overall_state: 'paused',
      counts: { running: 0, waiting: 0, paused: 1, failed: 0, idle: 0, unknown: 0, total_rooms: 1 }, rooms: [{ ...workload.rooms[0], state: 'paused' }] },
    { ...workload, agent_id: 10, agent_name: 'Old agent', agent_active: false, overall_state: 'unknown',
      counts: { running: 0, waiting: 0, paused: 0, failed: 0, idle: 0, unknown: 1, total_rooms: 1 }, rooms: [{ ...workload.rooms[0], state: 'unknown' }] },
  ], next_cursor: null })
  render(<MemoryRouter><AgentWorkloads/></MemoryRouter>)
  fireEvent.click(screen.getByRole('button', { name: 'Work across rooms' }))
  expect(await screen.findByText('Paused agent')).toBeTruthy()
  expect(screen.getAllByText('Paused').length).toBeGreaterThan(0)
  expect(screen.getByText('Inactive agent')).toBeTruthy()
  expect(screen.getAllByText('Status unavailable').length).toBeGreaterThan(0)
  expect(screen.queryByText('1 idle')).toBeNull()
})
