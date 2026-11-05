// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import ReportSettings from './ReportSettings'
import * as reporting from '../lib/reporting'

vi.mock('../lib/reporting', async (importOriginal) => ({
  ...await importOriginal<typeof import('../lib/reporting')>(),
  listReports: vi.fn(), getReportStatus: vi.fn(), setAutoReporting: vi.fn(),
}))

beforeEach(() => {
  vi.mocked(reporting.getReportStatus).mockResolvedValue({ auto_reporting_enabled: false, consent_generation: 0, submission_available: false })
  vi.mocked(reporting.listReports).mockResolvedValue({ events: [], reports: [], auto_reporting_enabled: false, consent_generation: 0, discarded: 0, discarded_expired: 0, discarded_capacity: 0, auto_attempt_times: [] })
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('shows default-off consent, destination, retention, and unavailable sender without opting in on open', async () => {
  render(<MemoryRouter><ReportSettings /></MemoryRouter>)
  expect(await screen.findByLabelText('Send diagnostic errors automatically')).toHaveProperty('checked', false)
  expect(screen.getAllByText(/gamja.top\/api\/gamja-bot\/reports/).length).toBeGreaterThan(0)
  expect(screen.getByText(/30 days/)).toBeTruthy()
  expect(screen.getByText(/7 days/)).toBeTruthy()
  expect(screen.getAllByText(/Submission unavailable/).length).toBeGreaterThan(0)
  expect(screen.getByText(/during this app session/)).toBeTruthy()
  expect(screen.getByText(/restart.*enable it again/i)).toBeTruthy()
  expect(reporting.setAutoReporting).not.toHaveBeenCalled()
})

it('refreshes host consent after a failed opt-out without claiming durable deletion', async () => {
  vi.mocked(reporting.getReportStatus).mockResolvedValue({ auto_reporting_enabled: true, consent_generation: 1, submission_available: true })
  vi.mocked(reporting.setAutoReporting).mockImplementation(() => {
    vi.mocked(reporting.getReportStatus).mockResolvedValue({ auto_reporting_enabled: false, consent_generation: 2, submission_available: true })
    return Promise.reject('Reporting storage unavailable')
  })
  render(<MemoryRouter><ReportSettings /></MemoryRouter>)
  fireEvent.click(await screen.findByLabelText('Send diagnostic errors automatically'))
  await waitFor(() => expect(reporting.getReportStatus).toHaveBeenCalledTimes(3))
  expect(screen.getByLabelText('Send diagnostic errors automatically')).toHaveProperty('checked', false)
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('Local deletion is not confirmed'))
  expect(screen.queryByText(/Unsent automatic reports were removed/)).toBeNull()
})

it('describes a failed opt-in as remaining off without mentioning opt-out or deletion', async () => {
  vi.mocked(reporting.getReportStatus).mockResolvedValue({ auto_reporting_enabled: false, consent_generation: 0, submission_available: true })
  vi.mocked(reporting.setAutoReporting).mockRejectedValue('Reporting storage unavailable')
  render(<MemoryRouter><ReportSettings /></MemoryRouter>)
  fireEvent.click(await screen.findByLabelText('Send diagnostic errors automatically'))
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('Could not enable automatic reporting'))
  expect(screen.getByLabelText('Send diagnostic errors automatically')).toHaveProperty('checked', false)
  expect(screen.queryByText(/Could not save the opt-out|Local deletion is not confirmed/)).toBeNull()
  await waitFor(() => expect(reporting.getReportStatus).toHaveBeenCalledTimes(3))
})

it('opt-in is explicit and revocation removes unsent automatic reports', async () => {
  vi.mocked(reporting.getReportStatus).mockResolvedValue({ auto_reporting_enabled: true, consent_generation: 1, submission_available: true })
  vi.mocked(reporting.setAutoReporting).mockResolvedValueOnce([false, 2])
  render(<MemoryRouter><ReportSettings /></MemoryRouter>)
  fireEvent.click(await screen.findByLabelText('Send diagnostic errors automatically'))
  await waitFor(() => expect(reporting.setAutoReporting).toHaveBeenCalledWith(false))
  expect(await screen.findByText(/Unsent automatic reports were removed/)).toBeTruthy()
})
