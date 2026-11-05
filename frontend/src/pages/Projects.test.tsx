// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom'
import Projects from './Projects'

vi.mock('../api/projects', () => ({ projectsApi: { list: vi.fn(), create: vi.fn(), update: vi.fn(),
  addAgent: vi.fn(), removeAgent: vi.fn(), previewArchive: vi.fn(), archive: vi.fn(), restore: vi.fn() } }))
vi.mock('../api/agents', () => ({ agentsApi: { list: vi.fn() } }))
vi.mock('../api/connections', () => ({ connectionsApi: { list: vi.fn() } }))
vi.mock('../api/conversations', () => ({ conversationsApi: { listByProject: vi.fn(), create: vi.fn(), launch: vi.fn(), addMember: vi.fn(),
  listMembers: vi.fn(), update: vi.fn(), clone: vi.fn() } }))
vi.mock('../api/schedules', () => ({ schedulesApi: { get: vi.fn() } }))
vi.mock('../api/roomTemplates', () => ({ roomTemplatesApi: {
  list: vi.fn(), create: vi.fn(), update: vi.fn(), delete: vi.fn(), createRoom: vi.fn(),
} }))
import { projectsApi } from '../api/projects'
import { agentsApi } from '../api/agents'
import { connectionsApi } from '../api/connections'
import { conversationsApi } from '../api/conversations'
import { schedulesApi } from '../api/schedules'
import { roomTemplatesApi } from '../api/roomTemplates'
import { useAuthStore } from '../stores/authStore'

beforeEach(() => {
  vi.mocked(projectsApi.list).mockResolvedValue([{ id: 20, name: 'Research', is_active: true, agents: [] }] as never)
  vi.mocked(projectsApi.previewArchive).mockResolvedValue({ project_id: 20, plan_hash: 'a'.repeat(64), counts: {
    active_rooms: 2, pending_triggers: 1, running_triggers: 0, running_runs: 1, waiting_runs: 0,
    waiting_approvals: 1, started_actions_uncertain: 0, open_actions_cancelled: 2, grants_revoked: 1,
  }, rooms: [{ id: 10, title: 'Earlier', last_event_seq: 8, active_work_count: 2 }], rooms_truncated: false })
  vi.mocked(projectsApi.archive).mockResolvedValue({ project_id: 20, is_active: false, archived_at: '2026-10-05T00:00:00Z',
    plan_hash: 'a'.repeat(64), counts: { active_rooms: 2, pending_triggers: 1, running_triggers: 0, running_runs: 1,
      waiting_runs: 0, waiting_approvals: 1, started_actions_uncertain: 0, open_actions_cancelled: 2, grants_revoked: 1 },
    cleanup_unconfirmed: [] })
  vi.mocked(projectsApi.restore).mockResolvedValue({ project_id: 20, is_active: true, closed_rooms: 2 })
  vi.mocked(agentsApi.list).mockResolvedValue([{ id: 7, name: 'Reusable', connection_id: 4, model_id: 'exact', is_active: true }] as never)
  vi.mocked(connectionsApi.list).mockResolvedValue([{ id: 4, label: 'Saved', enabled: true, capabilities: { verified: { models: {} } } }] as never)
  vi.mocked(conversationsApi.listByProject).mockImplementation(async (_project, status) => status === 'closed'
    ? [{ id: 11, title: 'Closed topic', project_id: 20, is_active: false }] as never
    : [{ id: 10, title: 'Earlier', project_id: 20, is_active: true }] as never)
  vi.mocked(conversationsApi.create).mockResolvedValue({ id: 30, title: 'New topic', project_id: 20 } as never)
  vi.mocked(conversationsApi.launch).mockResolvedValue({ id: 30, title: 'New topic', project_id: 20, is_active: true } as never)
  vi.mocked(conversationsApi.addMember).mockResolvedValue({ room_id: 30, agent_id: 7, is_active: true, joined_at: '' } as never)
  vi.mocked(conversationsApi.listMembers).mockResolvedValue([{ room_id: 10, agent_id: 7, is_active: true, joined_at: '' }] as never)
  vi.mocked(schedulesApi.get).mockResolvedValue({ max_rounds: 3, max_tool_calls: 9, max_executor_seconds: 120 })
  vi.mocked(conversationsApi.update).mockResolvedValue({ id: 10, title: 'Renamed', project_id: 20, is_active: true } as never)
  vi.mocked(conversationsApi.clone).mockResolvedValue({ id: 40, title: 'Fresh room', project_id: 20, is_active: true } as never)
  vi.mocked(roomTemplatesApi.list).mockResolvedValue([])
  vi.mocked(roomTemplatesApi.create).mockResolvedValue({ id: 51, user_id: 1, project_id: 20, name: 'Launch team', room_title: 'Launch room',
    agent_ids: [7], max_rounds: 2, max_tool_calls: 8, max_executor_seconds: 60, created_at: '', updated_at: '' })
  vi.mocked(roomTemplatesApi.update).mockResolvedValue({ id: 51, user_id: 1, project_id: 20, name: 'Launch team', room_title: 'Launch room',
    agent_ids: [7], max_rounds: 2, max_tool_calls: 8, max_executor_seconds: 60, created_at: '', updated_at: '' })
  vi.mocked(roomTemplatesApi.delete).mockResolvedValue(undefined)
  vi.mocked(roomTemplatesApi.createRoom).mockResolvedValue({ id: 61, user_id: 1, project_id: 20, title: 'Launch room', is_active: true, created_at: '', updated_at: '' })
})

it('allows an active owned Agent even when its saved connection is currently disabled', async () => {
  vi.mocked(connectionsApi.list).mockResolvedValue([{ id: 4, label: 'Saved', enabled: false }] as never)
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Projects /></MemoryRouter>
  </QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: /New room/ }))
  expect((screen.getByRole('checkbox', { name: /Reusable/ }) as HTMLInputElement).disabled).toBe(false)
})

it('starts with assigned Agents selected and allows another active owned Agent', async () => {
  vi.mocked(projectsApi.list).mockResolvedValue([{ id: 20, name: 'Research', is_active: true,
    agents: [{ id: 7, name: 'Assigned', model_id: 'exact', is_active: true }] }] as never)
  vi.mocked(agentsApi.list).mockResolvedValue([
    { id: 7, name: 'Assigned', model_id: 'exact', is_active: true },
    { id: 8, name: 'Available', model_id: 'other', is_active: true },
  ] as never)
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Projects /></MemoryRouter>
  </QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: /New room/ }))
  expect((screen.getByRole('checkbox', { name: /Assigned/ }) as HTMLInputElement).checked).toBe(true)
  const available = screen.getByRole('checkbox', { name: /Available/ }) as HTMLInputElement
  expect(available.checked).toBe(false)
  fireEvent.click(available)
  fireEvent.change(screen.getByRole('textbox', { name: 'Room title' }), { target: { value: 'Two agents' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create room' }))
  await waitFor(() => expect(conversationsApi.launch).toHaveBeenCalledWith(expect.objectContaining({ agent_ids: [7, 8] }), expect.any(String)))
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('previews the exact project archive impact before applying its plan hash', async () => {
  vi.mocked(projectsApi.archive).mockResolvedValueOnce({ project_id: 20, is_active: false, archived_at: '2026-10-05T00:00:00Z',
    plan_hash: 'a'.repeat(64), counts: { active_rooms: 2, pending_triggers: 1, running_triggers: 0, running_runs: 1,
      waiting_runs: 0, waiting_approvals: 1, started_actions_uncertain: 0, open_actions_cancelled: 2, grants_revoked: 1 },
    cleanup_unconfirmed: [10] })
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Projects /></MemoryRouter>
  </QueryClientProvider>)
  expect(await screen.findByText('Research')).toBeTruthy()
  expect(projectsApi.list).toHaveBeenCalledWith('active')
  const archiveTrigger = screen.getByRole('button', { name: 'Archive Research' })
  fireEvent.click(archiveTrigger)
  expect(await screen.findByRole('heading', { name: 'Archive Research?' })).toBeTruthy()
  expect(projectsApi.previewArchive).toHaveBeenCalledWith(20)
  expect(await screen.findByText('2 active rooms will close')).toBeTruthy()
  const dialog = screen.getByRole('dialog', { name: 'Archive Research?' })
  const cancel = screen.getByRole('button', { name: 'Cancel' })
  await waitFor(() => expect(document.activeElement).toBe(cancel))
  const apply = screen.getByRole('button', { name: 'Archive project' })
  apply.focus()
  fireEvent.keyDown(dialog, { key: 'Tab' })
  expect(document.activeElement).toBe(cancel)
  fireEvent.keyDown(dialog, { key: 'Escape' })
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Archive Research?' })).toBeNull())
  expect(document.activeElement).toBe(archiveTrigger)
  fireEvent.click(archiveTrigger)
  expect(await screen.findByRole('dialog', { name: 'Archive Research?' })).toBeTruthy()
  expect(await screen.findByText('1 queued or running request will stop')).toBeTruthy()
  expect(screen.getByText('1 agent run will stop')).toBeTruthy()
  expect(screen.getByText('0 started actions will need review')).toBeTruthy()
  expect(screen.getAllByText('Earlier')).toHaveLength(2)
  expect(screen.getByText('2 active work items')).toBeTruthy()
  expect(projectsApi.archive).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Archive project' }))
  await waitFor(() => expect(projectsApi.archive).toHaveBeenCalledWith(20, 'a'.repeat(64)))
  expect(await screen.findByText(/archived, but cleanup could not be confirmed for 1 room/)).toBeTruthy()
})

it('describes an empty active library without implying archived projects are gone', async () => {
  vi.mocked(projectsApi.list).mockResolvedValue([])
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Projects /></MemoryRouter>
  </QueryClientProvider>)
  expect(await screen.findByText('No active projects.')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Archived projects' })).toBeTruthy()
})

it('keeps the archive preview open and requires a fresh preview when its plan is stale', async () => {
  vi.mocked(projectsApi.archive).mockRejectedValueOnce(Object.assign(new Error('stale'), {
    response: { status: 409, data: { detail: { code: 'project_archive_plan_stale' } } },
  }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Projects /></MemoryRouter>
  </QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Archive Research' }))
  await screen.findByText('2 active rooms will close')
  fireEvent.click(await screen.findByRole('button', { name: 'Archive project' }))
  expect(await screen.findByText('This project changed. Review the current impact before archiving.')).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Archive project' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Review current impact' }))
  await waitFor(() => expect(projectsApi.previewArchive).toHaveBeenCalledTimes(2))
})

it('browses archived projects and restores only the project container', async () => {
  vi.mocked(projectsApi.list).mockImplementation(async (status) => status === 'archived'
    ? [{ id: 21, user_id: 1, name: 'Past work', description: null, is_active: false, archived_at: '2026-10-04T00:00:00Z', agents: [] }] as never
    : [{ id: 20, name: 'Research', is_active: true, agents: [] }] as never)
  vi.mocked(conversationsApi.listByProject).mockResolvedValue([])
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Projects /></MemoryRouter>
  </QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Archived projects' }))
  expect(await screen.findByText('Past work')).toBeTruthy()
  expect(projectsApi.list).toHaveBeenCalledWith('archived')
  expect(conversationsApi.listByProject).toHaveBeenCalledWith(21, 'all')
  expect(screen.queryByRole('button', { name: /New room/ })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Restore Past work' }))
  await waitFor(() => expect(projectsApi.restore).toHaveBeenCalledWith(21))
  expect(await screen.findByText('Project restored. Its 2 rooms remain closed.')).toBeTruthy()
})

it('guides a legacy archived project with active rooms through archive preview before restore', async () => {
  vi.mocked(projectsApi.list).mockImplementation(async (status) => status === 'archived'
    ? [{ id: 21, user_id: 1, name: 'Legacy work', description: null, is_active: false, archived_at: null, agents: [] }] as never
    : [] as never)
  vi.mocked(projectsApi.previewArchive).mockResolvedValueOnce({ project_id: 21, plan_hash: 'c'.repeat(64), counts: {
    active_rooms: 1, pending_triggers: 0, running_triggers: 0, running_runs: 0, waiting_runs: 0,
    waiting_approvals: 0, started_actions_uncertain: 0, open_actions_cancelled: 0, grants_revoked: 0,
  }, rooms: [{ id: 10, title: 'Older active room', last_event_seq: 1, active_work_count: 0 }], rooms_truncated: false })
  vi.mocked(projectsApi.restore).mockRejectedValueOnce(Object.assign(new Error('active children'), {
    response: { status: 409, data: { detail: { code: 'project_restore_active_children', active_rooms: 1 } } },
  }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Projects /></MemoryRouter>
  </QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Archived projects' }))
  expect(await screen.findByText('1 older room is still active and was not changed automatically.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Restore Legacy work' }))
  expect(await screen.findByText('Close the 1 active room with a reviewed archive, then restore the project.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Review remaining rooms' }))
  expect(await screen.findByRole('heading', { name: 'Archive Legacy work?' })).toBeTruthy()
  expect(projectsApi.previewArchive).toHaveBeenCalledWith(21)
  fireEvent.click(await screen.findByRole('button', { name: 'Archive project' }))
  await waitFor(() => expect(projectsApi.archive).toHaveBeenCalledWith(21, 'c'.repeat(64)))
  expect(screen.queryByText('Close the 1 active room with a reviewed archive, then restore the project.')).toBeNull()
})

it('ignores a late archive result after the owner changes', async () => {
  act(() => useAuthStore.getState().setAuth('owner-one', { id: 1, email: 'one@example.com', username: 'one' }))
  let finish!: (value: unknown) => void
  vi.mocked(projectsApi.archive).mockReturnValue(new Promise((resolve) => { finish = resolve }) as never)
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Projects /></MemoryRouter>
  </QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Archive Research' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Archive project' }))
  act(() => useAuthStore.getState().setAuth('owner-two', { id: 2, email: 'two@example.com', username: 'two' }))
  await act(async () => finish({ project_id: 20, is_active: false, archived_at: '2026-10-05T00:00:00Z',
    plan_hash: 'a'.repeat(64), counts: {}, cleanup_unconfirmed: [] }))
  expect(screen.queryByText(/Project archived/)).toBeNull()
  act(() => useAuthStore.getState().logout())
})

it('ignores a late restore result after the owner changes', async () => {
  act(() => useAuthStore.getState().setAuth('owner-one', { id: 1, email: 'one@example.com', username: 'one' }))
  vi.mocked(projectsApi.list).mockImplementation(async (status) => status === 'archived'
    ? [{ id: 21, user_id: 1, name: 'Private archive', is_active: false, archived_at: '2026-10-04T00:00:00Z', agents: [] }] as never
    : [] as never)
  vi.mocked(conversationsApi.listByProject).mockResolvedValue([])
  let finish!: (value: unknown) => void
  vi.mocked(projectsApi.restore).mockReturnValue(new Promise((resolve) => { finish = resolve }) as never)
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Projects /></MemoryRouter>
  </QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Archived projects' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Restore Private archive' }))
  act(() => useAuthStore.getState().setAuth('owner-two', { id: 2, email: 'two@example.com', username: 'two' }))
  await act(async () => finish({ project_id: 21, is_active: true, closed_rooms: 3 }))
  expect(screen.queryByText('Project restored. Its 3 rooms remain closed.')).toBeNull()
  act(() => useAuthStore.getState().logout())
})

it('reopens an existing room and launches a custom room with one atomic request', async () => {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Routes><Route path="/projects" element={<Projects />} />
      <Route path="/chat/:conversationId" element={<><p>New room opened</p><Link to="/projects">Back to projects</Link></>} /></Routes></MemoryRouter>
  </QueryClientProvider>)
  expect((await screen.findByRole('link', { name: 'Earlier' })).getAttribute('href')).toBe('/chat/10')
  fireEvent.click(screen.getByRole('button', { name: /New room/ }))
  fireEvent.change(screen.getByRole('textbox', { name: 'Room title' }), { target: { value: 'New topic' } })
  fireEvent.click(screen.getByRole('checkbox', { name: /Reusable/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Create room' }))
  await screen.findByText('New room opened')
  expect(conversationsApi.launch).toHaveBeenCalledWith({ project_id: 20, title: 'New topic', agent_ids: [7],
    max_rounds: 2, max_tool_calls: 8, max_executor_seconds: 60 }, expect.stringMatching(/^[0-9a-f-]{36}$/))
  expect(conversationsApi.create).not.toHaveBeenCalled()
  expect(conversationsApi.addMember).not.toHaveBeenCalled()
  vi.mocked(conversationsApi.launch).mockResolvedValueOnce({ id: 31, title: 'Another topic', project_id: 20, is_active: true } as never)
  fireEvent.click(screen.getByRole('link', { name: 'Back to projects' }))
  fireEvent.click(await screen.findByRole('button', { name: /New room/ }))
  fireEvent.change(screen.getByRole('textbox', { name: 'Room title' }), { target: { value: 'Another topic' } })
  fireEvent.click(screen.getByRole('checkbox', { name: /Reusable/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Create room' }))
  await waitFor(() => expect(conversationsApi.launch).toHaveBeenCalledTimes(2))
})

it('allows an intentional empty team and includes the complete policy in one launch', async () => {
  vi.mocked(agentsApi.list).mockResolvedValue([])
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Projects /></MemoryRouter>
  </QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: /New room/ }))
  expect(screen.getByText('No Agents selected. You can add them after opening the room.')).toBeTruthy()
  fireEvent.change(screen.getByRole('textbox', { name: 'Room title' }), { target: { value: '  Empty planning  ' } })
  fireEvent.change(screen.getByRole('spinbutton', { name: 'Collaboration rounds' }), { target: { value: '4' } })
  fireEvent.change(screen.getByRole('spinbutton', { name: 'Tool uses' }), { target: { value: '0' } })
  fireEvent.change(screen.getByRole('spinbutton', { name: 'Tool work time (seconds)' }), { target: { value: '90' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create room' }))
  await waitFor(() => expect(conversationsApi.launch).toHaveBeenCalledWith({ project_id: 20, title: 'Empty planning', agent_ids: [],
    max_rounds: 4, max_tool_calls: 0, max_executor_seconds: 90 }, expect.any(String)))
})

it('preserves a failed launch and reuses its key only while the exact body is unchanged', async () => {
  vi.mocked(conversationsApi.launch).mockRejectedValue(new Error('offline'))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Projects /></MemoryRouter>
  </QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: /New room/ }))
  const title = screen.getByRole('textbox', { name: 'Room title' }) as HTMLInputElement
  fireEvent.change(title, { target: { value: 'Keep this launch' } })
  fireEvent.click(screen.getByRole('checkbox', { name: /Reusable/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Create room' }))
  expect(await screen.findByText('Could not create the room. Your choices are still here.')).toBeTruthy()
  const firstKey = vi.mocked(conversationsApi.launch).mock.calls[0][1]
  fireEvent.click(screen.getByRole('button', { name: 'Retry creating room' }))
  await waitFor(() => expect(conversationsApi.launch).toHaveBeenCalledTimes(2))
  expect(vi.mocked(conversationsApi.launch).mock.calls[1][1]).toBe(firstKey)
  fireEvent.change(title, { target: { value: 'Changed launch' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create room' }))
  await waitFor(() => expect(conversationsApi.launch).toHaveBeenCalledTimes(3))
  expect(vi.mocked(conversationsApi.launch).mock.calls[2][1]).not.toBe(firstKey)
  expect(title.value).toBe('Changed launch')
})

it('renews a server-rejected conflicting key before retrying the preserved launch', async () => {
  vi.mocked(conversationsApi.launch).mockRejectedValueOnce(Object.assign(new Error('conflict'), {
    response: { status: 409, data: { detail: { code: 'room_launch_idempotency_conflict' } } },
  })).mockResolvedValueOnce({ id: 32, user_id: 1, title: 'Recovered launch', project_id: 20, is_active: true,
    created_at: '', updated_at: '' })
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Projects /></MemoryRouter>
  </QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: /New room/ }))
  fireEvent.change(screen.getByRole('textbox', { name: 'Room title' }), { target: { value: 'Recovered launch' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create room' }))
  expect(await screen.findByText('Could not create the room. Your choices are still here.')).toBeTruthy()
  const rejectedKey = vi.mocked(conversationsApi.launch).mock.calls[0][1]
  fireEvent.click(screen.getByRole('button', { name: 'Retry creating room' }))
  await waitFor(() => expect(conversationsApi.launch).toHaveBeenCalledTimes(2))
  expect(vi.mocked(conversationsApi.launch).mock.calls[1][1]).not.toBe(rejectedKey)
})

it('coalesces duplicate launch clicks and ignores a late result after the owner changes', async () => {
  act(() => useAuthStore.getState().setAuth('owner-one', { id: 1, email: 'one@example.com', username: 'one' }))
  let resolveLaunch!: (value: Awaited<ReturnType<typeof conversationsApi.launch>>) => void
  vi.mocked(conversationsApi.launch).mockImplementation(() => new Promise((resolve) => { resolveLaunch = resolve }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Routes><Route path="/projects" element={<Projects />} />
      <Route path="/chat/:conversationId" element={<p>Prior owner room</p>} /></Routes></MemoryRouter>
  </QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: /New room/ }))
  fireEvent.change(screen.getByRole('textbox', { name: 'Room title' }), { target: { value: 'One intent' } })
  const create = screen.getByRole('button', { name: 'Create room' })
  fireEvent.click(create)
  fireEvent.click(create)
  expect(conversationsApi.launch).toHaveBeenCalledTimes(1)
  act(() => useAuthStore.getState().setAuth('owner-two', { id: 2, email: 'two@example.com', username: 'two' }))
  await act(async () => resolveLaunch({ id: 99, user_id: 1, title: 'One intent', project_id: 20, is_active: true,
    created_at: '', updated_at: '' }))
  expect(screen.queryByText('Prior owner room')).toBeNull()
})

it('shows active rooms by default, browses closed rooms, and preserves a failed rename edit', async () => {
  vi.mocked(conversationsApi.update).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ id: 10, title: 'Renamed room', project_id: 20, is_active: true } as never)
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Projects /></MemoryRouter>
  </QueryClientProvider>)
  expect(await screen.findByRole('link', { name: 'Earlier' })).toBeTruthy()
  expect(conversationsApi.listByProject).toHaveBeenCalledWith(20, 'active')
  fireEvent.click(screen.getByText('Room options'))
  const title = screen.getByRole('textbox', { name: 'Room title' }) as HTMLInputElement
  fireEvent.change(title, { target: { value: 'Renamed room' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save room name' }))
  expect(await screen.findByText('Could not rename the room. Your edit is still here.')).toBeTruthy()
  expect(title.value).toBe('Renamed room')
  fireEvent.click(screen.getByRole('button', { name: 'Save room name' }))
  await waitFor(() => expect(conversationsApi.update).toHaveBeenCalledWith(10, 'Renamed room'))
  fireEvent.click(screen.getByRole('button', { name: 'Closed' }))
  expect(await screen.findByRole('link', { name: 'Closed topic' })).toBeTruthy()
  expect(screen.getByText('Closed room')).toBeTruthy()
  expect(conversationsApi.listByProject).toHaveBeenCalledWith(20, 'closed')
})

it('distinguishes an empty active-room view from an empty room history', async () => {
  vi.mocked(conversationsApi.listByProject).mockImplementation(async (_project, status) => status === 'closed'
    ? [{ id: 11, title: 'Preserved closed room', project_id: 20, is_active: false }] as never
    : [] as never)
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Projects /></MemoryRouter>
  </QueryClientProvider>)
  expect(await screen.findByText('No active rooms.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Closed' }))
  expect(await screen.findByRole('link', { name: 'Preserved closed room' })).toBeTruthy()
})

it('previews the source team and policy and creates from it with one atomic API call', async () => {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Routes><Route path="/projects" element={<Projects />} />
      <Route path="/chat/:conversationId" element={<p>Fresh room opened</p>} /></Routes></MemoryRouter>
  </QueryClientProvider>)
  await screen.findByRole('link', { name: 'Earlier' })
  fireEvent.click(screen.getByText('Room options'))
  expect(await screen.findByText('1 team member')).toBeTruthy()
  expect(screen.getByText('3 collaboration rounds · 9 tool uses · 120 seconds')).toBeTruthy()
  fireEvent.change(screen.getByRole('textbox', { name: 'New room name' }), { target: { value: 'Fresh room' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create new room' }))
  await screen.findByText('Fresh room opened')
  expect(conversationsApi.clone).toHaveBeenCalledWith(10, { project_id: 20, title: 'Fresh room' })
  expect(conversationsApi.create).not.toHaveBeenCalled()
  expect(conversationsApi.addMember).not.toHaveBeenCalled()
})

it('states when a historical room has no preserved team before creating one empty room', async () => {
  vi.mocked(conversationsApi.listMembers).mockResolvedValue([])
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Projects /></MemoryRouter>
  </QueryClientProvider>)
  await screen.findByRole('link', { name: 'Earlier' })
  fireEvent.click(screen.getByText('Room options'))
  expect(await screen.findByText('No preserved team members')).toBeTruthy()
  fireEvent.change(screen.getByRole('textbox', { name: 'New room name' }), { target: { value: 'Empty team room' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create new room' }))
  await waitFor(() => expect(conversationsApi.clone).toHaveBeenCalledWith(10, { project_id: 20, title: 'Empty team room' }))
  expect(conversationsApi.clone).toHaveBeenCalledTimes(1)
})

it('previews a named saved team and creates one fresh room with one atomic template call', async () => {
  vi.mocked(roomTemplatesApi.list).mockResolvedValue([{ id: 51, user_id: 1, project_id: 20, name: 'Launch team', room_title: 'Launch room',
    agent_ids: [7], max_rounds: 4, max_tool_calls: 12, max_executor_seconds: 180, created_at: '', updated_at: '' }])
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Routes><Route path="/projects" element={<Projects />} />
      <Route path="/chat/:conversationId" element={<p>Template room opened</p>} /></Routes></MemoryRouter>
  </QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: /New room/ }))
  fireEvent.click(screen.getByText('Saved team setups'))
  const saved = await screen.findByRole('combobox', { name: 'Saved setup' })
  fireEvent.change(saved, { target: { value: '51' } })
  expect((screen.getByRole('textbox', { name: 'Room title' }) as HTMLInputElement).value).toBe('Launch room')
  expect(screen.getByText('1 team member')).toBeTruthy()
  expect(screen.getByText('4 collaboration rounds · 12 tool uses · 180 seconds')).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Create room' })).toBeNull()
  expect(screen.queryByText(/Create uses the saved setup/)).toBeNull()
  fireEvent.change(screen.getByRole('textbox', { name: 'Room title' }), { target: { value: 'Unsaved title' } })
  expect(screen.getByText(/Create uses the saved setup/)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Create room from setup' }))
  await screen.findByText('Template room opened')
  expect(roomTemplatesApi.createRoom).toHaveBeenCalledWith(20, 51)
  expect(roomTemplatesApi.createRoom).toHaveBeenCalledTimes(1)
  expect(conversationsApi.create).not.toHaveBeenCalled()
  expect(conversationsApi.addMember).not.toHaveBeenCalled()
})

it('allows an explicit empty-team template even when no agents exist', async () => {
  vi.mocked(agentsApi.list).mockResolvedValue([])
  vi.mocked(roomTemplatesApi.list).mockResolvedValue([{ id: 52, user_id: 1, project_id: 20, name: 'Solo planning', room_title: 'Planning room',
    agent_ids: [], max_rounds: 2, max_tool_calls: 0, max_executor_seconds: 60, created_at: '', updated_at: '' }])
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Projects /></MemoryRouter>
  </QueryClientProvider>)
  const open = await screen.findByRole('button', { name: /New room/ })
  expect((open as HTMLButtonElement).disabled).toBe(false)
  fireEvent.click(open)
  fireEvent.click(screen.getByText('Saved team setups'))
  fireEvent.change(await screen.findByRole('combobox', { name: 'Saved setup' }), { target: { value: '52' } })
  expect(screen.getAllByText('No team members').length).toBeGreaterThan(0)
  fireEvent.click(screen.getByRole('button', { name: 'Create room from setup' }))
  await waitFor(() => expect(roomTemplatesApi.createRoom).toHaveBeenCalledWith(20, 52))
})

it('saves and updates the current team, title, and complete policy without mutating rooms', async () => {
  vi.mocked(roomTemplatesApi.list).mockResolvedValue([{ id: 51, user_id: 1, project_id: 20, name: 'Launch team', room_title: 'Launch room',
    agent_ids: [7], max_rounds: 2, max_tool_calls: 8, max_executor_seconds: 60, created_at: '', updated_at: '' }])
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Projects /></MemoryRouter>
  </QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: /New room/ }))
  fireEvent.change(screen.getByRole('textbox', { name: 'Room title' }), { target: { value: 'Reusable room' } })
  fireEvent.click(screen.getByRole('checkbox', { name: /Reusable/ }))
  fireEvent.click(screen.getByText('Saved team setups'))
  await screen.findByRole('combobox', { name: 'Saved setup' })
  fireEvent.change(screen.getByRole('textbox', { name: 'Setup name' }), { target: { value: 'Reusable team' } })
  fireEvent.click(screen.getByText('Room policy'))
  fireEvent.change(screen.getByRole('textbox', { name: 'Collaboration rounds' }), { target: { value: '3' } })
  fireEvent.change(screen.getByRole('textbox', { name: 'Tool uses' }), { target: { value: '16' } })
  fireEvent.change(screen.getByRole('textbox', { name: 'Tool work time (seconds)' }), { target: { value: '240' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save as new setup' }))
  await waitFor(() => expect(roomTemplatesApi.create).toHaveBeenCalledWith(20, {
    name: 'Reusable team', room_title: 'Reusable room', agent_ids: [7], max_rounds: 3, max_tool_calls: 16, max_executor_seconds: 240,
  }))
  fireEvent.change(screen.getByRole('combobox', { name: 'Saved setup' }), { target: { value: '51' } })
  fireEvent.change(screen.getByRole('textbox', { name: 'Setup name' }), { target: { value: 'Launch team revised' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
  await waitFor(() => expect(roomTemplatesApi.update).toHaveBeenCalledWith(20, 51, {
    name: 'Launch team revised', room_title: 'Launch room', agent_ids: [7], max_rounds: 2, max_tool_calls: 8, max_executor_seconds: 60,
  }))
  fireEvent.click(screen.getByRole('button', { name: 'Delete setup' }))
  await waitFor(() => expect(roomTemplatesApi.delete).toHaveBeenCalledWith(20, 51))
  expect(conversationsApi.update).not.toHaveBeenCalled()
  expect(conversationsApi.clone).not.toHaveBeenCalled()
})

it('keeps template choices after an unavailable-member create failure', async () => {
  vi.mocked(roomTemplatesApi.list).mockResolvedValue([{ id: 51, user_id: 1, project_id: 20, name: 'Old team', room_title: 'Keep this title',
    agent_ids: [999], max_rounds: 2, max_tool_calls: 8, max_executor_seconds: 60, created_at: '', updated_at: '' }])
  vi.mocked(roomTemplatesApi.createRoom).mockRejectedValue(Object.assign(new Error('unavailable'), {
    response: { status: 409, data: { detail: { code: 'room_template_members_unavailable', agent_ids: [999] } } },
  }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Projects /></MemoryRouter>
  </QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: /New room/ }))
  fireEvent.click(screen.getByText('Saved team setups'))
  fireEvent.change(await screen.findByRole('combobox', { name: 'Saved setup' }), { target: { value: '51' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create room from setup' }))
  expect(await screen.findByText(/Some team members are unavailable/)).toBeTruthy()
  expect((screen.getByRole('textbox', { name: 'Room title' }) as HTMLInputElement).value).toBe('Keep this title')
  expect((screen.getByRole('combobox', { name: 'Saved setup' }) as HTMLSelectElement).value).toBe('51')
})

it('ignores a late template room result after the owner epoch changes', async () => {
  act(() => useAuthStore.getState().setAuth('owner-one', { id: 1, email: 'one@example.com', username: 'one' }))
  vi.mocked(roomTemplatesApi.list).mockResolvedValue([{ id: 51, user_id: 1, project_id: 20, name: 'Owner one team', room_title: 'Private room',
    agent_ids: [7], max_rounds: 2, max_tool_calls: 8, max_executor_seconds: 60, created_at: '', updated_at: '' }])
  let finish!: (value: unknown) => void
  vi.mocked(roomTemplatesApi.createRoom).mockReturnValue(new Promise((resolve) => { finish = resolve }) as never)
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Routes><Route path="/projects" element={<Projects />} />
      <Route path="/chat/:conversationId" element={<p>Prior owner template room</p>} /></Routes></MemoryRouter>
  </QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: /New room/ }))
  fireEvent.click(screen.getByText('Saved team setups'))
  fireEvent.change(await screen.findByRole('combobox', { name: 'Saved setup' }), { target: { value: '51' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create room from setup' }))
  await waitFor(() => expect(roomTemplatesApi.createRoom).toHaveBeenCalledWith(20, 51))
  act(() => useAuthStore.getState().setAuth('owner-two', { id: 2, email: 'two@example.com', username: 'two' }))
  await act(async () => finish({ id: 61, user_id: 1, project_id: 20, title: 'Private room', is_active: true }))
  expect(screen.queryByText('Prior owner template room')).toBeNull()
  act(() => useAuthStore.getState().logout())
})

it('shows an unavailable-team conflict without losing the new room choices', async () => {
  vi.mocked(conversationsApi.clone).mockRejectedValue(Object.assign(new Error('invalid team'), {
    response: { status: 409, data: { detail: { code: 'room_clone_members_unavailable', agent_ids: [7] } } },
  }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Projects /></MemoryRouter>
  </QueryClientProvider>)
  await screen.findByRole('link', { name: 'Earlier' })
  fireEvent.click(screen.getByText('Room options'))
  await screen.findByText('1 team member')
  const name = screen.getByRole('textbox', { name: 'New room name' }) as HTMLInputElement
  fireEvent.change(name, { target: { value: 'Keep this name' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create new room' }))
  expect(await screen.findByText(/Some team members are no longer available/)).toBeTruthy()
  expect(name.value).toBe('Keep this name')
})

it('ignores a late clone result after the owner epoch changes', async () => {
  act(() => useAuthStore.getState().setAuth('owner-one', { id: 1, email: 'one@example.com', username: 'one' }))
  let finish!: (value: unknown) => void
  vi.mocked(conversationsApi.clone).mockReturnValue(new Promise((resolve) => { finish = resolve }) as never)
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Routes><Route path="/projects" element={<Projects />} />
      <Route path="/chat/:conversationId" element={<p>Fresh room opened</p>} /></Routes></MemoryRouter>
  </QueryClientProvider>)
  await screen.findByRole('link', { name: 'Earlier' })
  fireEvent.click(screen.getByText('Room options'))
  await screen.findByText('1 team member')
  fireEvent.change(screen.getByRole('textbox', { name: 'New room name' }), { target: { value: 'Owner one room' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create new room' }))
  await waitFor(() => expect(conversationsApi.clone).toHaveBeenCalledTimes(1))
  act(() => useAuthStore.getState().setAuth('owner-two', { id: 2, email: 'two@example.com', username: 'two' }))
  finish({ id: 40, title: 'Owner one room', project_id: 20, is_active: true })
  await waitFor(() => expect(screen.queryByText('Fresh room opened')).toBeNull())
  act(() => useAuthStore.getState().logout())
})

it('restores focus to trigger after closing room launch modal via Escape', async () => {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Projects /></MemoryRouter>
  </QueryClientProvider>)
  const launchBtn = await screen.findByRole('button', { name: /New room/ })
  launchBtn.focus()
  fireEvent.click(launchBtn)
  const dialog = await screen.findByRole('dialog', { name: /Create room/i })
  const titleInput = screen.getByRole('textbox', { name: 'Room title' })
  expect(document.activeElement).toBe(titleInput)
  fireEvent.keyDown(dialog, { key: 'Escape' })
  await waitFor(() => expect(screen.queryByRole('dialog', { name: /Create room/i })).toBeNull())
  expect(document.activeElement).toBe(launchBtn)
})

it('cycles Tab focus within room launch modal and excludes disabled/hidden controls', async () => {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Projects /></MemoryRouter>
  </QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: /New room/ }))
  await screen.findByRole('dialog', { name: /Create room/i })
  // Find the actual last enabled/visible control (not assuming Cancel is last)
  const createBtn = screen.getByRole('button', { name: 'Create room' })
  createBtn.focus()
  // Tab from last control wraps to first input
  fireEvent.keyDown(createBtn, { key: 'Tab' })
  const titleInput = screen.getByRole('textbox', { name: 'Room title' })
  await waitFor(() => expect(document.activeElement).toBe(titleInput))
  // Shift+Tab from first input wraps back to last control
  fireEvent.keyDown(titleInput, { key: 'Tab', shiftKey: true })
  await waitFor(() => expect(document.activeElement).toBe(createBtn))
})

it('restores focus after closing Agent assignment modal', async () => {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Projects /></MemoryRouter>
  </QueryClientProvider>)
  const manageBtn = await screen.findByRole('button', { name: 'Manage agents' })
  manageBtn.focus()
  fireEvent.click(manageBtn)
  const dialog = await screen.findByRole('dialog', { name: /Manage agents/i })
  expect(dialog).toBeTruthy()
  fireEvent.keyDown(dialog, { key: 'Escape' })
  await waitFor(() => expect(screen.queryByRole('dialog', { name: /Manage agents/i })).toBeNull())
  expect(document.activeElement).toBe(manageBtn)
})

it('updates the open assignment modal after add and blocks duplicate pending requests', async () => {
  let resolveAdd!: () => void
  let resolveRemove!: () => void
  vi.mocked(projectsApi.addAgent).mockImplementation(() => new Promise<void>((resolve) => { resolveAdd = resolve }))
  vi.mocked(projectsApi.removeAgent).mockImplementation(() => new Promise<void>((resolve) => { resolveRemove = resolve }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Projects /></MemoryRouter>
  </QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Manage agents' }))
  const add = screen.getByRole('button', { name: 'Add' })
  fireEvent.click(add)
  await waitFor(() => expect(projectsApi.addAgent).toHaveBeenCalledTimes(1))
  fireEvent.click(add)
  expect(projectsApi.addAgent).toHaveBeenCalledTimes(1)
  resolveAdd()
  await waitFor(() => expect(screen.getByRole('button', { name: 'Remove' })).toBeTruthy())
  expect(screen.queryByRole('button', { name: 'Add' })).toBeNull()
  const remove = screen.getByRole('button', { name: 'Remove' })
  fireEvent.click(remove)
  await waitFor(() => expect(projectsApi.removeAgent).toHaveBeenCalledTimes(1))
  fireEvent.click(remove)
  expect(projectsApi.removeAgent).toHaveBeenCalledTimes(1)
  resolveRemove()
  await waitFor(() => expect(screen.getByRole('button', { name: 'Add' })).toBeTruthy())
})

it('keeps assignment state authoritative after failure and allows a successful retry', async () => {
  vi.mocked(projectsApi.addAgent).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(undefined)
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/projects']}><Projects /></MemoryRouter>
  </QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Manage agents' }))
  fireEvent.click(screen.getByRole('button', { name: 'Add' }))
  expect((await screen.findByRole('alert')).textContent).toBe('Could not change the project assignment. Try again.')
  expect(screen.getByRole('button', { name: 'Add' })).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Remove' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Add' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Remove' })).toBeTruthy())
  expect(screen.queryByRole('alert')).toBeNull()
  expect(projectsApi.addAgent).toHaveBeenCalledTimes(2)
})
