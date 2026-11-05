// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import RoomMembers from './RoomMembers'

vi.mock('../api/conversations', () => ({ conversationsApi: {
  listMembers: vi.fn(), addMember: vi.fn(), removeMember: vi.fn(), pauseMember: vi.fn(), resumeMember: vi.fn(),
} }))
vi.mock('../api/connections', () => ({ connectionsApi: { list: vi.fn(), catalog: vi.fn() } }))
import { conversationsApi } from '../api/conversations'
import { connectionsApi } from '../api/connections'

const agents = [
  { id: 7, name: 'Missing key', connection_id: 70, is_active: true },
  { id: 8, name: 'Disabled key', connection_id: 80, is_active: true },
  { id: 9, name: 'Ready agent', connection_id: 90, model_id: 'openrouter/test', is_active: true },
] as never

function renderMembers() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter><RoomMembers roomId={501} agents={agents} actions={[]} events={[]} connection="live" /></MemoryRouter>
  </QueryClientProvider>)
}

beforeEach(() => {
  vi.mocked(conversationsApi.listMembers).mockResolvedValue([])
  vi.mocked(conversationsApi.addMember).mockResolvedValue({ room_id: 501, agent_id: 9 } as never)
  vi.mocked(connectionsApi.list).mockResolvedValue([{ id: 80, enabled: false }, { id: 90, enabled: true }] as never)
  vi.mocked(connectionsApi.catalog).mockResolvedValue({ version: 1, providers: [{
    provider_id: 'openrouter', label_key: 'connections.providers.openrouter', icon: 'openrouter',
    protocols: ['openai_chat_completions'], fields: [], model_source: { strategy: 'configured', manual_id: true, static_model_ids: [] },
    methods: [], technical_compatibility: 'supported', usage_classification: 'verified',
  }] } as never)
})

afterEach(() => { cleanup(); vi.clearAllMocks() })

it('keeps late old-room work cards out of the newly selected room', async () => {
  const request = { event_id: '550e8400-e29b-41d4-a716-446655440000', seq: 2 }
  let resolveOld!: (data: never) => void
  const work = (room: number, phase: string) => [{ room_id: room, agent_id: 9, is_active: true, joined_at: '',
    work_state: { version: 1, room_id: room, agent_id: 9, phase, scope: 'current', trigger_event_id: request.event_id,
      request, generation: 4, round: 1, peer_agent_ids: [] } }] as never
  vi.mocked(conversationsApi.listMembers).mockImplementation(async (room) => room === 501
    ? new Promise((resolve) => { resolveOld = resolve }) : work(602, 'thinking'))
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrap = (room: number) => <QueryClientProvider client={cache}><MemoryRouter>
    <RoomMembers roomId={room} agents={agents} actions={[]} events={[]} connection="live" />
  </MemoryRouter></QueryClientProvider>
  const view = render(wrap(501))
  await waitFor(() => expect(conversationsApi.listMembers).toHaveBeenCalledWith(501))
  view.rerender(wrap(602))
  expect(await screen.findByText('Thinking')).toBeTruthy()
  resolveOld(work(501, 'queued'))
  await waitFor(() => expect(cache.getQueriesData({ queryKey: ['members', 501] }).some(([, value]) => value !== undefined)).toBe(true))
  expect(screen.queryByText('Queued')).toBeNull()
  expect(screen.getByText('Thinking')).toBeTruthy()
  view.unmount(); cache.clear()
})

it('uses the catalog label key for typed provider identity', async () => {
  vi.mocked(conversationsApi.listMembers).mockResolvedValue([{ room_id: 501, agent_id: 9, is_active: true, joined_at: '' }] as never)
  vi.mocked(connectionsApi.list).mockResolvedValue([{ id: 90, enabled: true, label: 'Router',
    provider_kind: 'compatible', provider_id: 'openrouter' }] as never)
  renderMembers()
  expect(await screen.findByText(/Router · OpenRouter/)).toBeTruthy()
  expect(connectionsApi.catalog).toHaveBeenCalledTimes(1)
})

it('uses the localized unknown provider label for an invalid catalog label key', async () => {
  vi.mocked(conversationsApi.listMembers).mockResolvedValue([{ room_id: 501, agent_id: 9, is_active: true, joined_at: '' }] as never)
  vi.mocked(connectionsApi.list).mockResolvedValue([{ id: 90, enabled: true, label: 'Router',
    provider_kind: 'compatible', provider_id: 'future-provider' }] as never)
  vi.mocked(connectionsApi.catalog).mockResolvedValue({ version: 1, providers: [{
    provider_id: 'future-provider', label_key: 'connections.providers.missing-provider-label', icon: 'compatible',
    protocols: ['openai_chat_completions'], fields: [], model_source: { strategy: 'configured', manual_id: true, static_model_ids: [] },
    methods: [], technical_compatibility: 'supported', usage_classification: 'verified',
  }] } as never)
  renderMembers()
  expect(await screen.findByText(/Router · Other provider/)).toBeTruthy()
  expect(screen.queryByText(/connections\.providers\.missing-provider-label/)).toBeNull()
  expect(screen.queryByText(/future-provider/)).toBeNull()
})

it('shows why agents are ineligible and never adds them without an enabled saved connection', async () => {
  renderMembers()
  const select = await screen.findByRole('combobox', { name: 'Agent to add' })
  await waitFor(() => expect((screen.getByRole('option', { name: /Ready agent/ }) as HTMLOptionElement).disabled).toBe(false))
  const options = Array.from((select as HTMLSelectElement).options)
  expect(options.find((option) => option.textContent?.includes('Missing key'))?.disabled).toBe(true)
  expect(options.find((option) => option.textContent?.includes('Disabled key'))?.disabled).toBe(true)
  expect(options.find((option) => option.textContent?.includes('Ready agent'))?.disabled).toBe(false)
  fireEvent.change(select, { target: { value: '7' } })
  expect((screen.getByRole('button', { name: 'Add to room' }) as HTMLButtonElement).disabled).toBe(true)
  expect(conversationsApi.addMember).not.toHaveBeenCalled()
})

it('recovers connection eligibility through explicit retry without hiding existing members', async () => {
  vi.mocked(connectionsApi.list).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce([{ id: 90, enabled: true }] as never)
  vi.mocked(conversationsApi.listMembers).mockResolvedValue([{ room_id: 501, agent_id: 7, is_active: true, joined_at: '' }] as never)
  renderMembers()
  expect(await screen.findByText('Missing key')).toBeTruthy()
  expect(await screen.findByText('Could not load saved connections.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  await waitFor(() => expect((screen.getByRole('option', { name: /Ready agent/ }) as HTMLOptionElement).disabled).toBe(false))
})
