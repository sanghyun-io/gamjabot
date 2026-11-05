// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Agents from './Agents'

vi.mock('../api/agents', () => ({ agentsApi: { list: vi.fn(), update: vi.fn(), create: vi.fn(), delete: vi.fn() } }))
vi.mock('../api/connections', () => ({ connectionsApi: { list: vi.fn() } }))
import { agentsApi } from '../api/agents'
import { connectionsApi } from '../api/connections'

beforeEach(() => {
  vi.mocked(agentsApi.list).mockResolvedValue([{ id: 7, name: 'Ada', description: 'd', system_prompt: 'p', connection_id: 1,
    model_id: 'old-exact', model: 'old-exact', temperature: 'auto', max_tokens: 0, is_active: true }] as never)
  vi.mocked(connectionsApi.list).mockResolvedValue([
    { id: 1, label: 'Vendor account A', provider_kind: 'openai', default_model_id: 'old-exact', enabled: false, has_secret: true, auth_required: true, capabilities: { verified: { models: {} } } },
    { id: 2, label: 'Vendor account B', provider_kind: 'openai', default_model_id: 'new-exact', enabled: true, has_secret: true, auth_required: true, capabilities: { verified: { models: { 'new-exact': {} } } } },
  ] as never)
  vi.mocked(agentsApi.update).mockResolvedValue({} as never)
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('keeps two same-vendor connections distinct and requires an explicit model on rebinding', async () => {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter><Agents /></MemoryRouter></QueryClientProvider>)
  await screen.findByText(/Vendor account A.*Not in use.*Unverified model/)
  fireEvent.click(screen.getByRole('button', { name: 'Edit Ada' }))
  const select = screen.getByRole('combobox') as HTMLSelectElement
  fireEvent.change(select, { target: { value: '2' } })
  const model = screen.getByRole('textbox', { name: 'Exact model ID' }) as HTMLInputElement
  expect(model.value).toBe('')
  expect(screen.getByText(/This model has not been verified yet/)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: /Use the connection default model: new-exact/ }))
  expect(model.value).toBe('new-exact')
  fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
  await waitFor(() => expect(agentsApi.update).toHaveBeenCalledWith(7, expect.objectContaining({ connection_id: 2, model_id: 'new-exact' })))
})

it('shows a failed connection list as unavailable while preserving the saved binding', async () => {
  vi.mocked(connectionsApi.list).mockRejectedValueOnce(new Error('offline'))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter><Agents /></MemoryRouter></QueryClientProvider>)
  await screen.findByText(/Connection list unavailable/)
  expect(screen.queryByText(/missing saved connection/)).toBeNull()
  expect(screen.getByText(/Saved connection #1.*Error/)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Edit Ada' }))
  expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('1')
  expect((screen.getByRole('textbox', { name: 'Exact model ID' }) as HTMLInputElement).value).toBe('old-exact')
  expect((screen.getByRole('button', { name: 'Edit' }) as HTMLButtonElement).disabled).toBe(true)
  expect(agentsApi.update).not.toHaveBeenCalled()
})

it('keeps a saved binding visible while connections are still loading', async () => {
  vi.mocked(connectionsApi.list).mockImplementationOnce(() => new Promise(() => {}))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter><Agents /></MemoryRouter></QueryClientProvider>)
  await screen.findByText(/Saved connection #1.*Loading/)
  expect(screen.queryByText(/missing saved connection/)).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Edit Ada' }))
  expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('1')
  expect((screen.getByRole('button', { name: 'Edit' }) as HTMLButtonElement).disabled).toBe(true)
})

it('labels a no-auth connection without implying that a credential was stored', async () => {
  vi.mocked(agentsApi.list).mockResolvedValue([{ id: 9, name: 'Local', description: 'd', system_prompt: 'p', connection_id: 3,
    model_id: 'local-model', model: 'local-model', temperature: 'auto', max_tokens: 0, is_active: true }] as never)
  vi.mocked(connectionsApi.list).mockResolvedValue([
    { id: 3, label: 'Local runtime', provider_kind: 'compatible', default_model_id: 'local-model', enabled: true,
      has_secret: false, auth_required: false, capabilities: { verified: { models: { 'local-model': {} } } } },
  ] as never)

  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter><Agents /></MemoryRouter></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: 'Edit Local' }))

  expect(screen.getByText(/#3.*Local runtime.*No key required/)).toBeTruthy()
  expect(screen.getByText('local-model · Verified model')).toBeTruthy()
  expect(screen.queryByText(/Credential configured/)).toBeNull()
})

it('keeps advanced Agent fields collapsed and returns focus when Escape closes the dialog', async () => {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter><Agents /></MemoryRouter></QueryClientProvider>)
  const open = await screen.findByRole('button', { name: 'New agent' })
  open.focus()
  fireEvent.click(open)
  const dialog = screen.getByRole('dialog', { name: 'Create agent' })
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'Name' })))
  const advanced = screen.getByText('Details').closest('details')!
  expect(advanced.open).toBe(false)
  fireEvent.keyDown(dialog, { key: 'Escape' })
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Create agent' })).toBeNull())
  expect(document.activeElement).toBe(open)
})
