// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import ReportProblem from './ReportProblem'
import { useReportingReads } from '../lib/useReportingReads'
import { useAuthStore } from '../stores/authStore'

function ProblemView() {
  const identity = useAuthStore(state => `${state.user?.id ?? 0}:${state.epoch}`)
  const reads = useReportingReads()
  return <ReportProblem identity={identity} reads={reads} />
}
import * as reporting from '../lib/reporting'
import type { SubmissionDraft } from '../lib/reporting'

vi.mock('../lib/reporting', async (importOriginal) => ({
  ...await importOriginal<typeof import('../lib/reporting')>(),
  listReports: vi.fn(), getReportStatus: vi.fn(), createManualDraft: vi.fn(),
  updateManualDraft: vi.fn(), submitManualReport: vi.fn(), deleteReport: vi.fn(), exportReport: vi.fn(),
}))

const event = { event_id: 'e1', occurred_at: '2026-09-29T00:00:00Z', component: 'backend', stage: 'startup', code: 'backend_init_failed', app_version: '1', build_id: 'build', os: 'macos', os_version: '15.0', arch: 'aarch64' } as const
const envelope: SubmissionDraft['envelope'] = { schema_version: 1, report_id: 'a'.repeat(32), mode: 'manual', app_version: '1', build_id: 'build', os: 'macos', os_version: '15.0', arch: 'aarch64', occurred_at: event.occurred_at, component: event.component, stage: event.stage, code: event.code, occurrences: 1, transitions: [] }
const draft: SubmissionDraft = { envelope, body: JSON.stringify(envelope) }
const snapshot: reporting.ReportSnapshot = { events: [event], reports: [], auto_reporting_enabled: false, consent_generation: 0, discarded: 0, discarded_expired: 0, discarded_capacity: 0, auto_attempt_times: [] }

let storedSnapshot: reporting.ReportSnapshot
function persistDraft(value: SubmissionDraft) {
  storedSnapshot = { ...storedSnapshot, reports: [...storedSnapshot.reports.filter(report => report.draft.envelope.report_id !== value.envelope.report_id), { draft: value, state: 'pending', attempts: 0, attempt_times: [], next_attempt_at: 0, receipt: null }] }
  return value
}
beforeEach(() => {
  vi.resetAllMocks()
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  storedSnapshot = { ...snapshot, events: [...snapshot.events], reports: [] }
  useAuthStore.getState().setAuth('synthetic-owner', { id: 1, email: 'owner@example.invalid', username: 'owner' })
  vi.mocked(reporting.listReports).mockImplementation(async () => storedSnapshot)
  vi.mocked(reporting.getReportStatus).mockResolvedValue({ auto_reporting_enabled: false, consent_generation: 0, submission_available: false })
  vi.mocked(reporting.createManualDraft).mockImplementation(async (_ids, description) => {
    const next = { ...envelope, ...(description ? { description } : {}) }
    return persistDraft({ envelope: next, body: JSON.stringify(next) })
  })
  vi.mocked(reporting.updateManualDraft).mockResolvedValue(draft)
  vi.mocked(reporting.exportReport).mockResolvedValue('/private/report.json')
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('previews the full allowed payload and excludes a removed diagnostic without submitting on open', async () => {
  render(<ProblemView />)
  expect((await screen.findAllByText(/backend_init_failed/)).length).toBeGreaterThan(0)
  expect(reporting.submitManualReport).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: /Remove diagnostic 1/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Preview report' }))
  await waitFor(() => expect(reporting.createManualDraft).toHaveBeenCalledWith([], undefined))
  expect(screen.getByText(/"schema_version": 1/)).toBeTruthy()
  expect(screen.getByText(/Submission unavailable/)).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Submit report' }).hasAttribute('disabled')).toBe(true)
})

it('shows the exact sanitized diagnostic in the local entry and manual wire preview', async () => {
  const diagnostic = { version: 1 as const, correlation_id: 'a'.repeat(32), occurred_at: event.occurred_at,
    stage: 'tool_execution', failure_code: 'provider_unavailable', reason: 'unexpected_error', error_class: 'value_error',
    room_id: 4, agent_id: 7, connection_id: 3, trigger_event_id: null, action_id: null,
    exception_chain: [{ module: 'builtins', class: 'ValueError' }],
    frames: [{ file: 'app/services/scheduler.py', function: '_run', line: 382 }] }
  vi.mocked(reporting.listReports).mockResolvedValue({ ...snapshot, events: [{ ...event, diagnostic }] })
  vi.mocked(reporting.createManualDraft).mockImplementation(async () => {
    const next = { ...envelope, diagnostics: [diagnostic] }
    return persistDraft({ envelope: next, body: JSON.stringify(next) })
  })
  render(<ProblemView />)
  expect((await screen.findAllByText(/"correlation_id": "a{32}"/)).length).toBe(1)
  fireEvent.click(screen.getByRole('button', { name: 'Preview report' }))
  await waitFor(() => expect(screen.getAllByText(/"correlation_id": "a{32}"/)).toHaveLength(2))
  expect(screen.getAllByText(/app\/services\/scheduler.py/)).toHaveLength(2)
  expect(reporting.submitManualReport).not.toHaveBeenCalled()
})

it('names each diagnostic control with a safe ordinal and fixed failure details', async () => {
  vi.mocked(reporting.listReports).mockResolvedValue({ ...snapshot, events: [event, { ...event, event_id: 'e2' }] })
  render(<ProblemView />)
  expect(await screen.findByRole('checkbox', { name: /Include diagnostic 1: backend \/ startup \/ backend_init_failed at 2026-09-29T00:00:00Z/ })).toBeTruthy()
  expect(screen.getByRole('checkbox', { name: /Include diagnostic 2: backend \/ startup \/ backend_init_failed at 2026-09-29T00:00:00Z/ })).toBeTruthy()
  expect(screen.getByRole('button', { name: /Remove diagnostic 1: backend \/ startup \/ backend_init_failed/ })).toBeTruthy()
  expect(screen.getByRole('button', { name: /Remove diagnostic 2: backend \/ startup \/ backend_init_failed/ })).toBeTruthy()
})

it('offers Submit report for a saved unattempted draft and Retry only after an attempt', async () => {
  vi.mocked(reporting.getReportStatus).mockResolvedValue({ auto_reporting_enabled: false, consent_generation: 0, submission_available: true })
  vi.mocked(reporting.listReports).mockResolvedValue({ ...snapshot, reports: [
    { draft, state: 'pending' as const, attempts: 0, attempt_times: [], next_attempt_at: 0, receipt: null },
    { draft: { envelope: { ...envelope, report_id: 'd'.repeat(32) }, body: JSON.stringify({ ...envelope, report_id: 'd'.repeat(32) }) }, state: 'pending' as const, attempts: 1, attempt_times: [], next_attempt_at: 0, receipt: null },
  ] })
  render(<ProblemView />)
  await screen.findByRole('button', { name: `Export report · ${envelope.report_id}` })
  document.querySelectorAll('details').forEach(details => { details.open = true })
  expect(screen.getByRole('button', { name: `Submit report · ${envelope.report_id}` })).toBeTruthy()
  expect(screen.getByRole('button', { name: `Retry the original report unchanged · ${'d'.repeat(32)}` })).toBeTruthy()
})

it('rejects a description above 1000 characters without silently shortening it', async () => {
  render(<ProblemView />)
  fireEvent.change(await screen.findByLabelText('Optional description'), { target: { value: '😀'.repeat(1001) } })
  fireEvent.click(screen.getByRole('button', { name: 'Preview report' }))
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('1,000 characters'))
  expect(reporting.createManualDraft).not.toHaveBeenCalled()
})

it('keeps a failed send pending and allows explicit retry and export', async () => {
  vi.mocked(reporting.getReportStatus).mockResolvedValue({ auto_reporting_enabled: false, consent_generation: 0, submission_available: true })
  vi.mocked(reporting.submitManualReport).mockRejectedValueOnce('Submission pending').mockResolvedValueOnce({ report_id: envelope.report_id, receipt_id: 'b'.repeat(32), accepted_at: '2026-09-29T01:00:00Z' })
  render(<ProblemView />)
  const diagnosticControl = await screen.findByRole('button', { name: /Remove diagnostic 1/ })
  await waitFor(() => expect((diagnosticControl as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(screen.getByRole('button', { name: 'Preview report' }))
  const submit = await screen.findByRole('button', { name: 'Submit report' })
  fireEvent.click(screen.getByRole('button', { name: 'Read local records again' }))
  await waitFor(() => expect((submit as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(submit)
  expect(await screen.findByText(/Pending manual report/)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Export report' }))
  await waitFor(() => expect(reporting.exportReport).toHaveBeenCalledWith(envelope.report_id))
  fireEvent.click(screen.getByRole('button', { name: 'Retry the original report unchanged' }))
  expect((await screen.findAllByText(/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/)).length).toBeGreaterThan(0)
  expect(reporting.submitManualReport).toHaveBeenCalledTimes(2)
})

it('does not call an invalid receipt accepted and supports delete', async () => {
  vi.mocked(reporting.getReportStatus).mockResolvedValue({ auto_reporting_enabled: false, consent_generation: 0, submission_available: true })
  vi.mocked(reporting.submitManualReport).mockResolvedValue({ report_id: envelope.report_id, receipt_id: '', accepted_at: '' })
  render(<ProblemView />)
  const diagnosticControl = await screen.findByRole('button', { name: /Remove diagnostic 1/ })
  await waitFor(() => expect((diagnosticControl as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(screen.getByRole('button', { name: 'Preview report' }))
  const submit = await screen.findByRole('button', { name: 'Submit report' })
  fireEvent.click(screen.getByRole('button', { name: 'Read local records again' }))
  await waitFor(() => expect((submit as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(submit)
  expect(await screen.findByText(/Pending manual report/)).toBeTruthy()
  expect(screen.queryByText(/Submitted/)).toBeNull()
  fireEvent.click(screen.getAllByRole('button', { name: `Delete report · ${envelope.report_id}` })[0])
  await waitFor(() => expect(reporting.deleteReport).toHaveBeenCalledWith(envelope.report_id))
})

it('keeps a mismatched receipt pending instead of showing Submitted', async () => {
  vi.mocked(reporting.getReportStatus).mockResolvedValue({ auto_reporting_enabled: false, consent_generation: 0, submission_available: true })
  vi.mocked(reporting.submitManualReport).mockResolvedValue({ report_id: 'c'.repeat(32), receipt_id: 'b'.repeat(32), accepted_at: '2026-09-29T01:00:00Z' })
  render(<ProblemView />)
  const diagnosticControl = await screen.findByRole('button', { name: /Remove diagnostic 1/ })
  await waitFor(() => expect((diagnosticControl as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(screen.getByRole('button', { name: 'Preview report' }))
  const submit = await screen.findByRole('button', { name: 'Submit report' })
  fireEvent.click(screen.getByRole('button', { name: 'Read local records again' }))
  await waitFor(() => expect((submit as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(submit)
  expect(await screen.findByText(/Pending manual report/)).toBeTruthy()
  expect(screen.queryByText(/Submitted/)).toBeNull()
})

it('does not label a saved accepted entry Submitted when its receipt belongs to another report', async () => {
  vi.mocked(reporting.listReports).mockResolvedValue({ ...snapshot, reports: [{ draft, state: 'accepted' as const, attempts: 1, attempt_times: [], next_attempt_at: 0, receipt: { report_id: 'c'.repeat(32), receipt_id: 'b'.repeat(32), accepted_at: '2026-09-29T01:00:00Z' } }] })
  render(<ProblemView />)
  expect(await screen.findByText(/Submission pending/)).toBeTruthy()
  expect(screen.queryByText(/Submitted/)).toBeNull()
})

it('shows wire-size failures for Unicode without shortening the description', async () => {
  vi.mocked(reporting.createManualDraft).mockRejectedValue('Report exceeds wire limit')
  render(<ProblemView />)
  await screen.findByRole('checkbox', { name: /Include diagnostic 1/ })
  fireEvent.change(screen.getByLabelText('Optional description'), { target: { value: '😀'.repeat(1000) } })
  fireEvent.click(screen.getByRole('button', { name: 'Preview report' }))
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('4 KiB'))
  expect(reporting.createManualDraft).toHaveBeenCalledWith([event.event_id], '😀'.repeat(1000))
})

it('does not offer blind retry after receiver rejection', async () => {
  vi.mocked(reporting.getReportStatus).mockResolvedValue({ auto_reporting_enabled: false, consent_generation: 0, submission_available: true })
  vi.mocked(reporting.submitManualReport).mockRejectedValue('Report rejected')
  render(<ProblemView />)
  const diagnosticControl = await screen.findByRole('button', { name: /Remove diagnostic 1/ })
  await waitFor(() => expect((diagnosticControl as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(screen.getByRole('button', { name: 'Preview report' }))
  const submit = await screen.findByRole('button', { name: 'Submit report' })
  fireEvent.click(screen.getByRole('button', { name: 'Read local records again' }))
  await waitFor(() => expect((submit as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(submit)
  expect(await screen.findByText(/Report rejected. It cannot be retried unchanged/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Retry the original report unchanged' })).toBeNull()
})
