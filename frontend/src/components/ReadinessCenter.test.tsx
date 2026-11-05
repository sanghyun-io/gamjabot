// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { MemoryRouter, useLocation } from 'react-router-dom'
import ReadinessCenter from './ReadinessCenter'
import { readinessApi, type ReadinessResponse, type TestHistory } from '../api/readiness'
import { connectionsApi } from '../api/connections'
import { useAuthStore } from '../stores/authStore'

vi.mock('../api/readiness', () => ({ readinessApi: { get: vi.fn(), history: vi.fn() } }))
vi.mock('../api/connections', () => ({ connectionsApi: { test: vi.fn() } }))

const record = { id: 71, connection_id: 1, tested_at: '2026-10-05T01:00:00Z', duration_ms: 245, model_id: 'exact-model', ok: true as const,
  code: 'connected' as const, config_fingerprint: 'a'.repeat(64), config_current: true, credential_observation: 'verified' as const,
  verified_capabilities: { max_tokens: true, temperature: false, tools: true, vision: false } }
const readiness: ReadinessResponse = { connections: [{ connection_id: 1, label: 'Primary', provider_kind: 'openai', enabled: true, auth_required: true, has_secret: true,
  public_config: { base_url: null, default_model_id: 'exact-model', declared_capabilities: { tools: true } }, config_fingerprint: 'a'.repeat(64),
  declared_state: 'declared', credential_observation: 'verified', binding_counts: { active_bound: 1, active_missing_model: 1, inactive: 1, total: 3 },
  agent_bindings: [{ agent_id: 8, agent_name: 'Ada', agent_active: true, model_id: 'exact-model', status: 'bound', observation: 'verified', latest_test_id: 71 },
    { agent_id: 9, agent_name: 'Bea', agent_active: true, model_id: null, status: 'model_missing', observation: 'unknown', latest_test_id: null },
    { agent_id: 10, agent_name: 'Old', agent_active: false, model_id: 'old-model', status: 'inactive', observation: 'unknown', latest_test_id: null }], agent_bindings_truncated: false, latest_test: record }] }

function LocationProbe() {
  const location = useLocation()
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>
}

const view = () => render(<MemoryRouter initialEntries={['/connections?source=readiness']}><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><ReadinessCenter/><LocationProbe/></QueryClientProvider></MemoryRouter>)
beforeEach(() => {
  useAuthStore.getState().setAuth('owner', { id: 1, email: 'owner@example.com', username: 'owner' })
  vi.mocked(readinessApi.get).mockResolvedValue(readiness)
  vi.mocked(readinessApi.history).mockResolvedValue({ connection_id: 1, current_config_fingerprint: 'a'.repeat(64), items: [record] } satisfies TestHistory)
  vi.mocked(connectionsApi.test).mockResolvedValue({ ok: true, code: 'connected', message: '', record })
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('loads declared readiness on demand without testing or reading history automatically', async () => {
  view()
  expect(readinessApi.get).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Readiness and test history' }))
  const dialog = await screen.findByRole('dialog', { name: 'Readiness and test history' })
  expect(await within(dialog).findByText('Primary')).toBeTruthy()
  expect(within(dialog).getByText('Last manual test verified credentials')).toBeTruthy()
  expect(within(dialog).getByText(/1 Agents with an exact model · 1 missing/)).toBeTruthy()
  fireEvent.click(within(dialog).getByText('Agent model bindings'))
  expect(within(dialog).getByText(/Verified for this model/)).toBeTruthy()
  expect(within(dialog).getByText('Model missing')).toBeTruthy()
  expect(connectionsApi.test).not.toHaveBeenCalled()
  expect(readinessApi.history).not.toHaveBeenCalled()
})

it('loads persisted history only on demand and labels a prior configuration stale', async () => {
  vi.mocked(readinessApi.history).mockResolvedValue({ connection_id: 1, current_config_fingerprint: 'b'.repeat(64), items: [
    { ...record, config_current: false }, { ...record, id: 70, model_id: 'other-model', tested_at: '2026-10-04T23:00:00Z', ok: false, code: 'provider_error', verified_capabilities: null },
  ] })
  view(); fireEvent.click(screen.getByRole('button', { name: 'Readiness and test history' }))
  const dialog = await screen.findByRole('dialog')
  fireEvent.click(await within(dialog).findByText('Manual test history'))
  expect(await within(dialog).findByText(/Previous configuration/)).toBeTruthy()
  expect(within(dialog).getByText(/other-model/)).toBeTruthy()
  expect(readinessApi.history).toHaveBeenCalledWith(1)
  expect(connectionsApi.test).not.toHaveBeenCalled()
})

it('shows Go usage exhaustion as a bounded non-success result in readiness history', async () => {
  const exhausted = { ...record, ok: false as const, code: 'quota_exhausted' as const,
    credential_observation: 'unknown' as const, verified_capabilities: null }
  vi.mocked(readinessApi.get).mockResolvedValue({ connections: [{
    ...readiness.connections[0], latest_test: exhausted,
  }] })
  vi.mocked(readinessApi.history).mockResolvedValue({
    connection_id: 1, current_config_fingerprint: 'a'.repeat(64), items: [exhausted],
  })
  view(); fireEvent.click(screen.getByRole('button', { name: 'Readiness and test history' }))
  const dialog = await screen.findByRole('dialog')
  expect(await within(dialog).findByText(/OpenCode Go usage is exhausted/)).toBeTruthy()
  fireEvent.click(within(dialog).getByText('Manual test history'))
  expect((await within(dialog).findAllByText(/OpenCode Go usage is exhausted/)).length).toBeGreaterThan(0)
  expect(within(dialog).queryByText('Connection test succeeded.')).toBeNull()
})

it('keeps readiness read-only and returns to explicit connection-card testing without a provider POST', async () => {
  vi.mocked(readinessApi.get).mockResolvedValue({ connections: [{ ...readiness.connections[0],
    agent_bindings: [{ ...readiness.connections[0].agent_bindings[0], model_id: 'agent-exact' }] }] })
  view(); fireEvent.click(screen.getByRole('button', { name: 'Readiness and test history' }))
  const dialog = await screen.findByRole('dialog')
  await within(dialog).findByText('Primary')
  expect(within(dialog).queryByRole('button', { name: 'Test Primary now' })).toBeNull()
  expect(within(dialog).queryByRole('button', { name: 'Test model agent-exact' })).toBeNull()
  const manage = within(dialog).getByRole('link', { name: 'Manage connections' })
  expect([...dialog.querySelectorAll('button:not(:disabled), a[href], summary')]).toContain(manage)
  fireEvent.click(manage)
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(screen.getByTestId('location').textContent).toBe('/connections')
  expect(connectionsApi.test).not.toHaveBeenCalled()
})

it('clears a prior owner immediately and ignores late readiness', async () => {
  let finish!: (value: ReadinessResponse) => void
  vi.mocked(readinessApi.get).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  view(); fireEvent.click(screen.getByRole('button', { name: 'Readiness and test history' }))
  expect(await screen.findByRole('dialog')).toBeTruthy()
  act(() => useAuthStore.getState().setAuth('other', { id: 2, email: 'other@example.com', username: 'other' }))
  act(() => finish(readiness))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(screen.queryByText('Primary')).toBeNull()
})

it('shows a truthful unavailable state and retries to an empty saved list', async () => {
  vi.mocked(readinessApi.get).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ connections: [] })
  view(); fireEvent.click(screen.getByRole('button', { name: 'Readiness and test history' }))
  expect(await screen.findByText('Readiness is unavailable.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(await screen.findByText('No saved connections.')).toBeTruthy()
  expect(connectionsApi.test).not.toHaveBeenCalled()
})
