// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import RequestResult from './RequestResult'
import { requestFinalizeApi, type FinalizePreview } from '../api/requestFinalize'
import { starterApi } from '../api/starter'
import { useAuthStore } from '../stores/authStore'
import { I18nProvider } from '../i18n'

vi.mock('../api/requestFinalize', () => ({ requestFinalizeApi: { preview: vi.fn(), execute: vi.fn() } }))
vi.mock('../api/starter', () => ({ starterApi: { lastResult: vi.fn() } }))

const source = '11111111-1111-4111-8111-111111111111'
const outcome = '22222222-2222-4222-8222-222222222222'
const failedOutcome = '33333333-3333-4333-8333-333333333333'
const preview = (values: Partial<FinalizePreview> = {}): FinalizePreview => ({
  version: 1, room_id: 501, source_request: { event_id: source, seq: 1, preview: 'Choose a launch plan', href: '/chat/501?seq=1' },
  source_completion: { state: 'completed', outcome_status: 'partial', result_available: true },
  outcomes: [{ event_id: outcome, seq: 2, round: 1, agent_id: 8, agent_name_snapshot: 'Proposer then', kind: 'message',
    phase: 'succeeded', summary_code: 'response', model_use: { connection_id: 1, model_id: 'historic-model', provider_kind: 'openai', watermark_seq: 1 }, href: '/chat/501?seq=2' },
    { event_id: failedOutcome, seq: 3, round: 1, agent_id: 9, agent_name_snapshot: 'Reviewer then', kind: 'failure',
      phase: 'failed', summary_code: 'provider_unavailable', model_use: null, href: '/chat/501?seq=3' }],
  outcome_count: 2, outcomes_truncated: false, excluded_agents: [{ agent_id: 9, name_snapshot: 'Reviewer then', reason: 'failed' }],
  agent: { agent_id: 8, name_snapshot: 'Proposer then', current_name: 'Proposer now' },
  current_model: { connection_id: 1, model_id: 'current-model', provider_kind: 'openai' },
  limits: { max_rounds: 1, max_tool_calls: 0, max_executor_seconds: 60 },
  source_coverage: { source_request_event_id: source, source_outcome_event_ids: [outcome, failedOutcome],
    included_message_event_ids: [outcome], omitted_event_ids: [failedOutcome],
    truncations: [{ event_id: outcome, original_chars: 7000, included_chars: 4000, omitted_chars: 3000 }], used_chars: 9000, limit_chars: 32000 },
  eligible: true, blocked_reason: null, plan_hash: 'a'.repeat(64), ...values,
})

beforeEach(() => {
  vi.resetAllMocks()
  vi.spyOn(document, 'hasFocus').mockReturnValue(true)
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue([new DOMRect(0, 0, 600, 100)] as unknown as DOMRectList)
  window.localStorage.removeItem('gamja.locale')
  useAuthStore.getState().setAuth('owner', { id: 1, email: 'owner@example.com', username: 'owner' })
  vi.mocked(starterApi.lastResult).mockResolvedValue({ project_id: 40, room_id: 501, agents: { proposer: 8, reviewer: 9 } })
  vi.mocked(requestFinalizeApi.preview).mockResolvedValue(preview())
  vi.mocked(requestFinalizeApi.execute).mockResolvedValue({ version: 1, room_id: 501, source_request_event_id: source,
    source_outcome_event_ids: [outcome, failedOutcome], finalize_event_id: '44444444-4444-4444-8444-444444444444', finalize_seq: 5,
    agent_id: 8, replayed: false, href: '/chat/501?seq=5' })
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

function view(onCreated = vi.fn()) {
  const onNavigate = vi.fn()
  return { onCreated, onNavigate, ...render(<MemoryRouter><RequestResult roomId={501} sourceRequestId={source} ownerId={1}
    authEpoch={useAuthStore.getState().epoch} selectedAgents={[{ agent_id: 9, name_snapshot: 'Reviewer then' }, { agent_id: 8, name_snapshot: 'Proposer then' }]}
    onCreated={onCreated} onNavigate={onNavigate} /></MemoryRouter>) }
}

it('renders the on-demand action in the selected non-English locale', async () => {
  render(<I18nProvider systemLanguages={() => ['ko']}><MemoryRouter><RequestResult roomId={501} sourceRequestId={source} ownerId={1}
    authEpoch={useAuthStore.getState().epoch} selectedAgents={[{ agent_id: 8, name_snapshot: '기획' }]} onCreated={vi.fn()} />
  </MemoryRouter></I18nProvider>)
  expect(await screen.findByRole('button', { name: '결과 정리' })).toBeTruthy()
})

it('previews only after explicit action, defaults to the starter proposer, and starts one model turn explicitly', async () => {
  const { onCreated } = view()
  expect(requestFinalizeApi.preview).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Prepare final result' }))
  await waitFor(() => expect((screen.getByLabelText('Agent for final result') as HTMLSelectElement).value).toBe('8'))
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('region', { name: 'Final result' })))
  expect(requestFinalizeApi.preview).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Review final result plan' }))
  await waitFor(() => expect(requestFinalizeApi.preview).toHaveBeenCalledWith(501, source, 8))
  expect(await screen.findByText('Partial discussion')).toBeTruthy()
  await waitFor(() => expect(document.activeElement).toBe(screen.getByText('Partial discussion').closest('.request-result__preview')))
  expect(screen.getAllByText(/Reviewer then/).length).toBeGreaterThan(0)
  expect(screen.getByText(/current-model/)).toBeTruthy()
  expect(screen.getByText(/1 round.*0 tool uses/)).toBeTruthy()
  expect(screen.getByText(/one model turn/)).toBeTruthy()
  const transport = screen.getByText('Technical delivery details').closest('details')!
  expect(transport.open).toBe(false)
  expect(transport.textContent).toContain('Provider transport retries are not capped')
  expect(requestFinalizeApi.execute).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Start final result' }))
  await waitFor(() => expect(requestFinalizeApi.execute).toHaveBeenCalledWith(501, source, { agent_id: 8, plan_hash: 'a'.repeat(64) }, expect.stringMatching(/^[0-9a-f-]{36}$/)))
  expect(onCreated).toHaveBeenCalledWith(expect.objectContaining({ finalize_seq: 5, agent_id: 8 }), expect.any(Function))
})

it('keeps one idempotency key after ambiguous failure and requires a new preview when the Agent changes', async () => {
  vi.mocked(requestFinalizeApi.execute).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ version: 1, room_id: 501,
    source_request_event_id: source, source_outcome_event_ids: [outcome, failedOutcome], finalize_event_id: '44444444-4444-4444-8444-444444444444',
    finalize_seq: 5, agent_id: 8, replayed: true, href: '/chat/501?seq=5' })
  view()
  fireEvent.click(screen.getByRole('button', { name: 'Prepare final result' }))
  await screen.findByLabelText('Agent for final result')
  fireEvent.click(screen.getByRole('button', { name: 'Review final result plan' }))
  await screen.findByText('Partial discussion')
  fireEvent.click(screen.getByRole('button', { name: 'Start final result' }))
  expect(await screen.findByText(/has no confirmed response/)).toBeTruthy()
  const key = vi.mocked(requestFinalizeApi.execute).mock.calls[0][3]
  fireEvent.click(screen.getByRole('button', { name: 'Resend original summary key' }))
  await waitFor(() => expect(requestFinalizeApi.execute).toHaveBeenCalledTimes(2))
  expect(vi.mocked(requestFinalizeApi.execute).mock.calls[1][3]).toBe(key)

  fireEvent.change(screen.getByLabelText('Agent for final result'), { target: { value: '9' } })
  expect(screen.queryByRole('button', { name: 'Start final result' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Review final result plan' }))
  await waitFor(() => expect(requestFinalizeApi.preview).toHaveBeenLastCalledWith(501, source, 9))
})

it('reuses the ambiguous key after the inspector closes and reopens', async () => {
  vi.mocked(requestFinalizeApi.execute).mockRejectedValueOnce(new Error('lost response'))
  const first = view(vi.fn())
  fireEvent.click(screen.getByRole('button', { name: 'Prepare final result' }))
  await waitFor(() => expect((screen.getByLabelText('Agent for final result') as HTMLSelectElement).value).toBe('8'))
  fireEvent.click(screen.getByRole('button', { name: 'Review final result plan' }))
  await screen.findByText('Partial discussion')
  fireEvent.click(screen.getByRole('button', { name: 'Start final result' }))
  await screen.findByText(/has no confirmed response/)
  const firstKey = vi.mocked(requestFinalizeApi.execute).mock.calls[0][3]
  first.unmount()

  const onCreated = vi.fn()
  view(onCreated)
  expect(screen.queryByRole('button', { name: 'Prepare final result' })).toBeNull()
  await waitFor(() => expect((screen.getByLabelText('Agent for final result') as HTMLSelectElement).value).toBe('8'))
  fireEvent.click(screen.getByRole('button', { name: 'Resend original summary key' }))
  await waitFor(() => expect(requestFinalizeApi.execute).toHaveBeenCalledTimes(2))
  await screen.findByText('Summary request accepted. A completed or correct summary is separate.')
  expect(onCreated).not.toHaveBeenCalled()
  expect(vi.mocked(requestFinalizeApi.execute).mock.calls[1][3]).toBe(firstKey)
})

it('consumes a changed-intent conflict and requires a fresh preview and key', async () => {
  vi.mocked(requestFinalizeApi.execute).mockRejectedValueOnce({ response: { status: 409, data: { detail: {
    code: 'finalize_idempotency_conflict',
  } } } })
  view()
  fireEvent.click(screen.getByRole('button', { name: 'Prepare final result' }))
  await screen.findByLabelText('Agent for final result')
  fireEvent.click(screen.getByRole('button', { name: 'Review final result plan' }))
  await screen.findByText('Partial discussion')
  fireEvent.click(screen.getByRole('button', { name: 'Start final result' }))
  expect(await screen.findByText(/server rejected this key/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Resend original summary key' })).toBeNull()
  const firstKey = vi.mocked(requestFinalizeApi.execute).mock.calls[0][3]
  fireEvent.click(screen.getByRole('button', { name: 'Review final result plan' }))
  await screen.findByText('Partial discussion')
  fireEvent.click(screen.getByRole('button', { name: 'Start final result' }))
  await waitFor(() => expect(requestFinalizeApi.execute).toHaveBeenCalledTimes(2))
  expect(vi.mocked(requestFinalizeApi.execute).mock.calls[1][3]).not.toBe(firstKey)
})

it('shows canonical omissions and blocked recovery without offering Start', async () => {
  vi.mocked(requestFinalizeApi.preview).mockResolvedValueOnce(preview({ eligible: false, blocked_reason: 'model_unavailable', plan_hash: null }))
  const { onNavigate } = view()
  fireEvent.click(screen.getByRole('button', { name: 'Prepare final result' }))
  await screen.findByLabelText('Agent for final result')
  fireEvent.click(screen.getByRole('button', { name: 'Review final result plan' }))
  expect(await screen.findByText('Set up and explicitly test the current model before preparing a final result.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Open connections' }))
  expect(onNavigate).toHaveBeenCalledWith('/connections')
  expect(screen.queryByRole('button', { name: 'Start final result' })).toBeNull()
})

it('shows the finite reason when an explicit start becomes blocked', async () => {
  vi.mocked(requestFinalizeApi.execute).mockRejectedValueOnce({ response: { status: 409, data: { detail: {
    code: 'finalize_blocked', reason: 'room_busy',
  } } } })
  view()
  fireEvent.click(screen.getByRole('button', { name: 'Prepare final result' }))
  await screen.findByLabelText('Agent for final result')
  fireEvent.click(screen.getByRole('button', { name: 'Review final result plan' }))
  await screen.findByText('Partial discussion')
  fireEvent.click(screen.getByRole('button', { name: 'Start final result' }))
  expect(await screen.findByText('Wait for the room’s current work to finish.')).toBeTruthy()
  expect((screen.getByRole('button', { name: 'Start final result' }) as HTMLButtonElement).disabled).toBe(true)
})

it('drops a delayed preview after the owner changes', async () => {
  let finishPreview!: (value: FinalizePreview) => void
  vi.mocked(requestFinalizeApi.preview).mockReturnValueOnce(new Promise((resolve) => { finishPreview = resolve }))
  const rendered = view()
  fireEvent.click(screen.getByRole('button', { name: 'Prepare final result' }))
  await screen.findByLabelText('Agent for final result')
  fireEvent.click(screen.getByRole('button', { name: 'Review final result plan' }))
  act(() => useAuthStore.getState().setAuth('two', { id: 2, email: 'two@example.com', username: 'two' }))
  act(() => finishPreview(preview()))
  await act(async () => {})
  expect(screen.queryByText('Partial discussion')).toBeNull()
  rendered.unmount()
})

it('drops a delayed execute after unmount', async () => {
  let finishExecute!: (value: Awaited<ReturnType<typeof requestFinalizeApi.execute>>) => void
  vi.mocked(requestFinalizeApi.execute).mockReturnValueOnce(new Promise((resolve) => { finishExecute = resolve }))
  const onCreated = vi.fn()
  const rendered = view(onCreated)
  fireEvent.click(screen.getByRole('button', { name: 'Prepare final result' }))
  await screen.findByLabelText('Agent for final result')
  fireEvent.click(screen.getByRole('button', { name: 'Review final result plan' }))
  await screen.findByText('Partial discussion')
  fireEvent.click(screen.getByRole('button', { name: 'Start final result' }))
  rendered.unmount()
  act(() => finishExecute({ version: 1, room_id: 501, source_request_event_id: source,
    source_outcome_event_ids: [outcome, failedOutcome], finalize_event_id: '44444444-4444-4444-8444-444444444444',
    finalize_seq: 5, agent_id: 8, replayed: false, href: '/chat/501?seq=5' }))
  await act(async () => {})
  expect(onCreated).not.toHaveBeenCalled()
})
