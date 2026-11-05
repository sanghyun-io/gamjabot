// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import FirstSuccessGuide from './FirstSuccessGuide'
import { starterApi } from '../api/starter'
import type { StarterPreview } from '../api/starter'
import { connectionsApi } from '../api/connections'
import { readinessApi } from '../api/readiness'
import { draftsApi } from '../api/drafts'
import { useAuthStore } from '../stores/authStore'
import { I18nProvider, LocaleSelector } from '../i18n'

vi.mock('../api/starter', () => ({ starterApi: { preview: vi.fn(), apply: vi.fn(), lastResult: vi.fn() } }))
vi.mock('../api/connections', () => ({ connectionsApi: { list: vi.fn() } }))
vi.mock('../api/readiness', () => ({ readinessApi: { get: vi.fn() } }))
vi.mock('../api/drafts', () => ({ draftsApi: { get: vi.fn(), put: vi.fn() } }))

const connection = {
  id: 4, owner_id: 1, provider_kind: 'openai' as const, label: 'Work model', base_url: null,
  default_model_id: 'exact-model', enabled: true, auth_required: true, has_secret: true, is_local: false,
  capabilities: { declared: {}, verified: { models: { 'exact-model': { max_tokens: true } } } },
}
const readiness = {
  connections: [{
    connection_id: 4, label: 'Work model', provider_kind: 'openai' as const, enabled: true,
    auth_required: true, has_secret: true,
    public_config: { base_url: null, default_model_id: 'exact-model', declared_capabilities: {} },
    config_fingerprint: 'f'.repeat(64), declared_state: 'declared' as const,
    credential_observation: 'verified' as const,
    binding_counts: { active_bound: 0, active_missing_model: 0, inactive: 0, total: 0 },
    agent_bindings: [], agent_bindings_truncated: false,
    latest_test: { id: 8, connection_id: 4, tested_at: '2026-10-06T00:00:00Z', duration_ms: 50,
      model_id: 'exact-model', ok: true, code: 'connected' as const, config_fingerprint: 'f'.repeat(64),
      config_current: true, credential_observation: 'verified' as const,
      verified_capabilities: { max_tokens: true, temperature: false, tools: false, vision: false },
      profile: 'text' as const, actual_request_count: 1 },
  }],
}
const scrollIntoView = vi.fn()
const previewResult = (hash: string): StarterPreview => ({
  plan_hash: hash, project_title: 'Launch', room_title: 'Launch decision',
  agents: [
    { role: 'proposer' as const, requested_name: 'Proposer', name: 'Proposer', connection_id: 4, model_id: 'exact-model', test_history_id: 8, config_revision: 1, config_fingerprint: 'f'.repeat(64) },
    { role: 'reviewer' as const, requested_name: 'Reviewer', name: 'Reviewer', connection_id: 4, model_id: 'exact-model', test_history_id: 8, config_revision: 1, config_fingerprint: 'f'.repeat(64) },
  ], policy: { max_rounds: 2 as const, max_tool_calls: 0 as const, max_executor_seconds: 60 as const },
})

function renderGuide(showLocale = false) {
  return render(<I18nProvider systemLanguages={() => ['en']}><MemoryRouter><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    {showLocale && <LocaleSelector />}
    <FirstSuccessGuide />
  </QueryClientProvider></MemoryRouter></I18nProvider>)
}

beforeEach(() => {
  window.localStorage.removeItem('gamja.locale')
  scrollIntoView.mockReset()
  HTMLElement.prototype.scrollIntoView = scrollIntoView
  useAuthStore.getState().setAuth('owner-token', { id: 1, email: 'owner@example.test', username: 'owner' })
  vi.mocked(starterApi.lastResult).mockResolvedValue(null)
  vi.mocked(connectionsApi.list).mockResolvedValue([connection])
  vi.mocked(readinessApi.get).mockResolvedValue(readiness)
  vi.mocked(draftsApi.get).mockResolvedValue({ content: '', version: 0, idempotency_key: null, updated_at: null })
  vi.mocked(draftsApi.put).mockResolvedValue({ content: 'saved', version: 1, idempotency_key: 'draft-key', updated_at: '2026-10-06T00:00:00Z' })
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('previews and applies one exact two-role starter, then saves the prompt only as a room draft', async () => {
  vi.mocked(starterApi.preview).mockResolvedValue(previewResult('a'.repeat(64)))
  vi.mocked(starterApi.apply).mockResolvedValue({ project_id: 31, room_id: 41,
    agents: { proposer: 51, reviewer: 52 }, replayed: false })
  renderGuide()

  const guide = await screen.findByRole('region', { name: 'Start your first collaboration' })
  expect((await within(guide).findAllByText(/Work model/)).length).toBeGreaterThan(0)
  fireEvent.change(await within(guide).findByLabelText('Project name'), { target: { value: 'Launch' } })
  fireEvent.change(within(guide).getByLabelText('Room name'), { target: { value: 'Launch decision' } })
  fireEvent.click(within(guide).getByRole('button', { name: 'Review team' }))
  await waitFor(() => expect(starterApi.preview).toHaveBeenCalledWith({
    project_title: 'Launch', room_title: 'Launch decision', agents: [
      { role: 'proposer', name: 'Proposer', connection_id: 4, model_id: 'exact-model' },
      { role: 'reviewer', name: 'Reviewer', connection_id: 4, model_id: 'exact-model' },
    ],
  }))

  fireEvent.click(await within(guide).findByRole('button', { name: 'Create team and room' }))
  await waitFor(() => expect(starterApi.apply).toHaveBeenCalledTimes(1))
  expect(vi.mocked(starterApi.apply).mock.calls[0][0]).toMatchObject({ project_title: 'Launch', room_title: 'Launch decision', plan_hash: 'a'.repeat(64) })
  expect(vi.mocked(starterApi.apply).mock.calls[0][1]).toMatch(/^[0-9a-f-]{36}$/)
  await waitFor(() => expect(draftsApi.put).toHaveBeenCalledWith(41, expect.stringContaining('constraints'), 0))
  const openRoom = await within(guide).findByRole('link', { name: 'Open Launch decision' })
  expect(openRoom.getAttribute('href')).toBe('/chat/41')
  await waitFor(() => expect(document.activeElement).toBe(openRoom.closest('.first-success__result')))
  expect(scrollIntoView).toHaveBeenCalledWith({ block: 'start' })
})

it('recovers a committed lost apply response from owner last-result without creating again', async () => {
  vi.mocked(starterApi.preview).mockResolvedValue(previewResult('b'.repeat(64)))
  vi.mocked(starterApi.apply).mockRejectedValue(new Error('connection lost'))
  vi.mocked(starterApi.lastResult)
    .mockResolvedValueOnce(null)
    .mockResolvedValue({ project_id: 31, room_id: 77, agents: { proposer: 51, reviewer: 52 } })
  renderGuide()
  const guide = await screen.findByRole('region', { name: 'Start your first collaboration' })
  fireEvent.change(await within(guide).findByLabelText('Project name'), { target: { value: 'Launch' } })
  fireEvent.change(within(guide).getByLabelText('Room name'), { target: { value: 'Recovered room' } })
  fireEvent.click(within(guide).getByRole('button', { name: 'Review team' }))
  fireEvent.click(await within(guide).findByRole('button', { name: 'Create team and room' }))

  expect((await within(guide).findByRole('link', { name: 'Open Recovered room' })).getAttribute('href')).toBe('/chat/77')
  expect(starterApi.apply).toHaveBeenCalledTimes(1)
  expect(draftsApi.put).toHaveBeenCalledWith(77, expect.any(String), 0)
})

it('does not expose a prior owner response or preserve identity-bound setup fields', async () => {
  let finish!: (value: StarterPreview) => void
  vi.mocked(starterApi.preview).mockReturnValue(new Promise((resolve) => { finish = resolve }))
  const view = renderGuide()
  const guide = await screen.findByRole('region', { name: 'Start your first collaboration' })
  fireEvent.change(await within(guide).findByLabelText('Project name'), { target: { value: 'Prior owner private title' } })
  fireEvent.change(within(guide).getByLabelText('Room name'), { target: { value: 'Prior room' } })
  fireEvent.click(within(guide).getByRole('button', { name: 'Review team' }))

  useAuthStore.getState().setAuth('next-token', { id: 2, email: 'next@example.test', username: 'next' })
  vi.mocked(starterApi.lastResult).mockResolvedValue(null)
  vi.mocked(connectionsApi.list).mockResolvedValue([])
  vi.mocked(readinessApi.get).mockResolvedValue({ connections: [] })
  view.rerender(<I18nProvider systemLanguages={() => ['en']}><MemoryRouter><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <FirstSuccessGuide />
  </QueryClientProvider></MemoryRouter></I18nProvider>)
  finish(previewResult('c'.repeat(64)))

  await waitFor(() => expect(screen.queryByText('Prior owner private title')).toBeNull())
  expect(screen.queryByRole('button', { name: 'Create team and room' })).toBeNull()
  expect((await screen.findByRole('link', { name: 'Set up a connection' })).getAttribute('href')).toBe('/connections?first=1')
})

it('never previews, applies, or probes a provider until the owner explicitly acts', async () => {
  renderGuide()
  await screen.findByRole('region', { name: 'Start your first collaboration' })
  expect(starterApi.preview).not.toHaveBeenCalled()
  expect(starterApi.apply).not.toHaveBeenCalled()
  expect(connectionsApi.test).toBeUndefined()
  expect(draftsApi.put).not.toHaveBeenCalled()
})

it('renders the selected locale and preserves same-owner setup input across a language change', async () => {
  renderGuide(true)
  const guide = await screen.findByRole('region', { name: 'Start your first collaboration' })
  fireEvent.change(await within(guide).findByLabelText('Project name'), { target: { value: '다크 모드 입력' } })
  fireEvent.change(within(guide).getByLabelText('Room name'), { target: { value: 'Locale room' } })
  const agentNames = within(guide).getAllByLabelText('Agent name')
  fireEvent.change(agentNames[0], { target: { value: 'Eigener Vorschlag' } })
  fireEvent.change(agentNames[1], { target: { value: 'Eigene Prüfung' } })

  fireEvent.change(screen.getByRole('combobox', { name: 'Language' }), { target: { value: 'de' } })

  const localized = await screen.findByRole('region', { name: 'Starte deine erste Zusammenarbeit' })
  expect((within(localized).getByLabelText('Projektname') as HTMLInputElement).value).toBe('다크 모드 입력')
  expect((within(localized).getByLabelText('Raumname') as HTMLInputElement).value).toBe('Locale room')
  const localizedNames = within(localized).getAllByLabelText('Agentenname')
  expect((localizedNames[0] as HTMLInputElement).value).toBe('Eigener Vorschlag')
  expect((localizedNames[1] as HTMLInputElement).value).toBe('Eigene Prüfung')
  expect(within(localized).getByRole('button', { name: 'Team prüfen' })).toBeTruthy()
  expect(within(localized).getAllByText(/Work model · exact-model/)).toHaveLength(2)
  expect(within(localized).getAllByLabelText('Geprüfte Verbindung und genaues Modell').map((field) => (field as HTMLSelectElement).value))
    .toEqual(['4:exact-model', '4:exact-model'])
})
