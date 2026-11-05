// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import App, { ReportingErrorBoundary, ReportingRoot } from './App'
import { getLocalEndpoint } from './lib/localHost'
import { recordFrontendRenderFailure } from './lib/reporting'

vi.mock('./lib/localHost', () => ({ getLocalEndpoint: vi.fn() }))
vi.mock('./lib/api', () => ({ default: { post: vi.fn().mockRejectedValue(new Error('no stored login')) } }))
vi.mock('./lib/reporting', () => ({
  recordFrontendRenderFailure: vi.fn().mockResolvedValue(undefined),
  getReportStatus: vi.fn().mockResolvedValue({ auto_reporting_enabled: false, consent_generation: 0, submission_available: false }),
  listReports: vi.fn().mockResolvedValue({ events: [], reports: [], auto_reporting_enabled: false, consent_generation: 0, discarded: 0, discarded_expired: 0, discarded_capacity: 0, auto_attempt_times: [] }),
}))
vi.mock('./stores/authStore', () => {
  const state = { token: null, setAuth: vi.fn(), epoch: 0 }
  return { useAuthStore: Object.assign(
    (selector?: (value: typeof state) => unknown) => selector ? selector(state) : state,
    { getState: () => state },
  ) }
})
vi.mock('./pages/Login', () => ({ default: () => { throw new Error('secret in exception') } }))

afterEach(() => { cleanup(); vi.clearAllMocks() })

it('opens reporting without backend startup or login', async () => {
  vi.mocked(getLocalEndpoint).mockRejectedValue(new Error('backend failed'))
  render(<MemoryRouter initialEntries={['/reports']}><App /></MemoryRouter>)
  expect(await screen.findByRole('heading', { name: 'Reporting settings' })).toBeTruthy()
  expect(getLocalEndpoint).not.toHaveBeenCalled()
})

it('offers the reporting route when local startup fails', async () => {
  vi.mocked(getLocalEndpoint).mockRejectedValue(new Error('backend failed'))
  render(<MemoryRouter initialEntries={['/']}><App /></MemoryRouter>)
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('Local service could not start'))
  fireEvent.click(screen.getByRole('link', { name: 'Report a problem' }))
  expect(await screen.findByRole('heading', { name: 'Reporting settings' })).toBeTruthy()
})

it('opens the real report screen after a render failure without sending exception data', async () => {
  vi.mocked(getLocalEndpoint).mockResolvedValue({ base_url: 'http://127.0.0.1:8000', session_token: 'unused' })
  const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
  try {
    function LocationProbe() { const location = useLocation(); return <output data-testid="path">{location.pathname}</output> }
    render(<MemoryRouter initialEntries={['/login']}><LocationProbe /><ReportingRoot /></MemoryRouter>)
    expect(screen.getByRole('combobox', { name: 'Language' }).closest('.locale-toolbar')).toBeTruthy()
    expect(document.querySelector('.app-frame__content')).toBeTruthy()
    fireEvent.click(await screen.findByRole('button', { name: 'Report a problem' }))
    expect(screen.getByTestId('path')).toHaveProperty('textContent', '/reports')
    expect(await screen.findByRole('heading', { name: 'Reporting settings' })).toBeTruthy()
    await waitFor(() => expect(recordFrontendRenderFailure).toHaveBeenCalledWith())
  } finally { consoleError.mockRestore() }
})

it('records a fixed frontend render code without forwarding the thrown error', async () => {
  function Broken(): never { throw new Error('secret in exception') }
  const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
  render(<MemoryRouter><ReportingErrorBoundary><Broken /></ReportingErrorBoundary></MemoryRouter>)
  expect(screen.getByRole('link', { name: 'Report a problem' })).toBeTruthy()
  await waitFor(() => expect(recordFrontendRenderFailure).toHaveBeenCalledWith())
  consoleError.mockRestore()
})

it('points to outside-webview instructions when the reports screen crashes', async () => {
  function Broken(): never { throw new Error('report screen failed') }
  const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
  try {
    render(<MemoryRouter><ReportingErrorBoundary reportScreen><Broken /></ReportingErrorBoundary></MemoryRouter>)
    expect(screen.getByRole('alert')).toHaveProperty('textContent', expect.stringContaining('project README'))
    expect(screen.queryByRole('button', { name: 'Report a problem' })).toBeNull()
    await waitFor(() => expect(recordFrontendRenderFailure).toHaveBeenCalledWith())
  } finally { consoleError.mockRestore() }
})
