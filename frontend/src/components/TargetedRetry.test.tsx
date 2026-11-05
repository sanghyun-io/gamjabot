// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import TargetedRetry from './TargetedRetry'
import { requestRetriesApi, type RequestRetryPreflight } from '../api/requestRetries'
import { useAuthStore } from '../stores/authStore'

vi.mock('../api/requestRetries', () => ({ requestRetriesApi: { preflight: vi.fn(), start: vi.fn() } }))

const failureId = '33333333-3333-4333-8333-333333333333'
const preflight = (value: Partial<RequestRetryPreflight> = {}): RequestRetryPreflight => ({
  version: 1, room_id: 501, failure_event_id: failureId, failure_seq: 4,
  source_request: { event_id: '11111111-1111-4111-8111-111111111111', seq: 1, preview: 'Prepare the release', href: '/chat/501?seq=1' },
  agent: { agent_id: 8, name_snapshot: 'Ada at failure time', current_name: 'Ada' },
  current_model: { connection_id: 2, model_id: 'current-model', provider_kind: 'openai' },
  limits: { max_rounds: 1, max_tool_calls: 5, max_executor_seconds: 45 },
  eligible: true, blocked_reason: null, plan_hash: 'a'.repeat(64), ...value,
})

beforeEach(() => {
  useAuthStore.getState().setAuth('owner', { id: 1, email: 'owner@example.com', username: 'owner' })
  vi.mocked(requestRetriesApi.preflight).mockResolvedValue(preflight())
  vi.mocked(requestRetriesApi.start).mockResolvedValue({ version: 1, room_id: 501, source_failure_event_id: failureId,
    source_trigger_event_id: '11111111-1111-4111-8111-111111111111', source_request_event_id: '11111111-1111-4111-8111-111111111111',
    retry_event_id: '55555555-5555-4555-8555-555555555555', retry_seq: 7, agent_id: 8, replayed: false, href: '/chat/501?seq=7' })
})
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.restoreAllMocks() })

function view(onCreated = vi.fn()) {
  return { onCreated, ...render(<TargetedRetry roomId={501} failureEventId={failureId} ownerId={1}
    authEpoch={useAuthStore.getState().epoch} onCreated={onCreated} />) }
}

it('shows a long teammate handoff block and does not start a retry', async () => {
  vi.mocked(requestRetriesApi.preflight).mockResolvedValueOnce(preflight({ eligible: false, blocked_reason: 'teammate_result_incomplete', plan_hash: null }))
  view()
  fireEvent.click(screen.getByRole('button', { name: 'Retry only this agent' }))
  expect(await screen.findByText(/This attempt cannot be retried because the teammate response/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Start retry' })).toBeNull()
  expect(requestRetriesApi.start).not.toHaveBeenCalled()
})

it('loads eligibility only on demand and starts one server-derived Agent retry explicitly', async () => {
  const { onCreated } = view()
  expect(requestRetriesApi.preflight).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Retry only this agent' }))
  expect(await screen.findByText('Prepare the release')).toBeTruthy()
  expect(screen.getByText(/Ada will be the only Agent/)).toBeTruthy()
  expect(screen.getByText(/current-model/)).toBeTruthy()
  expect(screen.getByText('The original request and failure stay in the room history. This starts a new attempt with the current room context and model, without automatically replaying earlier tool actions.')).toBeTruthy()
  expect(requestRetriesApi.start).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Start retry' }))
  await waitFor(() => expect(requestRetriesApi.start).toHaveBeenCalledTimes(1))
  expect(requestRetriesApi.start).toHaveBeenCalledWith(501, failureId, 'a'.repeat(64), expect.stringMatching(/^[0-9a-f-]{36}$/))
  expect(await screen.findByText('A new attempt was added for Ada.')).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Start retry' })).toBeNull()
  expect(onCreated).toHaveBeenCalledWith(expect.objectContaining({ retry_seq: 7, agent_id: 8 }))
})

it('keeps one key for an ambiguous failure and refreshes stale plans before a new intent', async () => {
  vi.mocked(requestRetriesApi.start).mockRejectedValueOnce(new Error('offline'))
    .mockRejectedValueOnce(Object.assign(new Error('stale'), { response: { status: 409, data: { detail: { code: 'request_retry_plan_stale' } } } }))
  vi.mocked(requestRetriesApi.preflight).mockResolvedValueOnce(preflight()).mockResolvedValueOnce(preflight({ plan_hash: 'b'.repeat(64) }))
  view()
  fireEvent.click(screen.getByRole('button', { name: 'Retry only this agent' }))
  await screen.findByText('Prepare the release')
  fireEvent.click(screen.getByRole('button', { name: 'Start retry' }))
  expect(await screen.findByText('The new attempt could not be confirmed. Retry uses the same safe request key.')).toBeTruthy()
  const firstKey = vi.mocked(requestRetriesApi.start).mock.calls[0][3]
  fireEvent.click(screen.getByRole('button', { name: 'Retry start' }))
  await waitFor(() => expect(requestRetriesApi.start).toHaveBeenCalledTimes(2))
  expect(vi.mocked(requestRetriesApi.start).mock.calls[1][3]).toBe(firstKey)
  expect(await screen.findByText('The room changed. Review the retry again before starting it.')).toBeTruthy()
  expect(requestRetriesApi.preflight).toHaveBeenCalledTimes(2)
  fireEvent.click(screen.getByRole('button', { name: 'Start retry' }))
  await waitFor(() => expect(requestRetriesApi.start).toHaveBeenCalledTimes(3))
  expect(vi.mocked(requestRetriesApi.start).mock.calls[2][2]).toBe('b'.repeat(64))
  expect(vi.mocked(requestRetriesApi.start).mock.calls[2][3]).not.toBe(firstKey)
})

it('shows finite human blocked reasons and never offers Start', async () => {
  vi.mocked(requestRetriesApi.preflight).mockResolvedValueOnce(preflight({ eligible: false, blocked_reason: 'action_uncertain', plan_hash: null }))
  view()
  fireEvent.click(screen.getByRole('button', { name: 'Retry only this agent' }))
  expect(await screen.findByText('Check the earlier tool action result before retrying this Agent.')).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Start retry' })).toBeNull()
  expect(requestRetriesApi.start).not.toHaveBeenCalled()
})

it('drops late preflight and retry settlements after the owner changes', async () => {
  let finish!: (value: RequestRetryPreflight) => void
  vi.mocked(requestRetriesApi.preflight).mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
  view()
  fireEvent.click(screen.getByRole('button', { name: 'Retry only this agent' }))
  act(() => useAuthStore.getState().setAuth('owner-two', { id: 2, email: 'two@example.com', username: 'two' }))
  act(() => finish(preflight()))
  await act(async () => {})
  expect(screen.queryByText('Prepare the release')).toBeNull()
  expect(requestRetriesApi.start).not.toHaveBeenCalled()
})

it('drops a delayed retry success after the inspector unmounts', async () => {
  let finish!: (value: Awaited<ReturnType<typeof requestRetriesApi.start>>) => void
  vi.mocked(requestRetriesApi.start).mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
  const onCreated = vi.fn()
  const rendered = view(onCreated)
  fireEvent.click(screen.getByRole('button', { name: 'Retry only this agent' }))
  await screen.findByText('Prepare the release')
  fireEvent.click(screen.getByRole('button', { name: 'Start retry' }))
  await waitFor(() => expect(requestRetriesApi.start).toHaveBeenCalledTimes(1))
  rendered.unmount()
  act(() => finish({ version: 1, room_id: 501, source_failure_event_id: failureId,
    source_trigger_event_id: '11111111-1111-4111-8111-111111111111', source_request_event_id: '11111111-1111-4111-8111-111111111111',
    retry_event_id: '55555555-5555-4555-8555-555555555555', retry_seq: 7, agent_id: 8, replayed: false, href: '/chat/501?seq=7' }))
  await act(async () => {})
  expect(onCreated).not.toHaveBeenCalled()
})

it('explains changed memory without offering automatic old-context retry', async () => {
  vi.mocked(requestRetriesApi.preflight).mockResolvedValue(preflight({ eligible: false, blocked_reason: 'memory_context_changed', plan_hash: null }))
  view(); fireEvent.click(screen.getByRole('button', { name: 'Retry only this agent' }))
  await screen.findByText(/Memory context changed/); expect(requestRetriesApi.start).not.toHaveBeenCalled()
})
