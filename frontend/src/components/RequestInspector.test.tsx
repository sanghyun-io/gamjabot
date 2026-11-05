// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import RequestInspector from './RequestInspector'
import { requestInspectorApi, type RequestInspectorResponse } from '../api/requestInspector'
import { useAuthStore } from '../stores/authStore'
import { requestRetriesApi } from '../api/requestRetries'

vi.mock('../api/requestInspector', () => ({ requestInspectorApi: { get: vi.fn() } }))
vi.mock('../api/requestRetries', () => ({ requestRetriesApi: { preflight: vi.fn(), start: vi.fn() } }))

const triggerA = '11111111-1111-4111-8111-111111111111'
const triggerB = '22222222-2222-4222-8222-222222222222'
const response = (trigger = triggerA): RequestInspectorResponse => ({
  version: 1, room_id: 501, room_title: 'Release room', read_only: false,
  request: { event_id: trigger, seq: trigger === triggerA ? 1 : 9, created_at: '2026-10-05T01:00:00Z', actor_name_snapshot: 'DemoUser',
    preview: trigger === triggerA ? 'First exact request' : 'Overlapping later request', href: `/chat/501?seq=${trigger === triggerA ? 1 : 9}` },
  execution: { phase: 'completed', selected_agent_count: 2, current_round: 2,
    selected_agents: [{ agent_id: 8, name_snapshot: 'Ada at request time', live_state: null }, { agent_id: 9, name_snapshot: null, live_state: 'waiting' }],
    policy: { max_rounds: 3, max_tool_calls: 5, max_executor_seconds: 45 } },
  completion: { state: 'completed', outcome_status: 'partial', result_available: true,
    completed_rounds: 1, pending_agent_count: 0 },
  outcomes: [{ event_id: '33333333-3333-4333-8333-333333333333', seq: 4, round: 1, agent_id: 8,
    agent_name_snapshot: 'Ada at request time', kind: 'message', phase: 'succeeded', summary_code: 'response',
    model_use: { connection_id: 2, model_id: 'historical-model', provider_kind: 'openai', watermark_seq: 3 }, href: '/chat/501?seq=4' }],
  outcome_count: 1, outcomes_truncated: false,
  actions: [{ action_id: '44444444-4444-4444-8444-444444444444', agent_id: 8, state: 'waiting', phase: 'approval', summary_code: 'action_files',
    requested_at: '2026-10-05T01:01:00Z', started_at: null, completed_at: null, result_event_id: null, result_seq: null,
    href: '/chat/501?seq=1&action=44444444-4444-4444-8444-444444444444' }],
  action_count: 1, actions_truncated: false,
})

beforeEach(() => {
  useAuthStore.getState().setAuth('owner', { id: 1, email: 'owner@example.com', username: 'owner' })
  vi.mocked(requestInspectorApi.get).mockResolvedValue(response())
  vi.mocked(requestRetriesApi.preflight).mockResolvedValue({ version: 1, room_id: 501,
    failure_event_id: '33333333-3333-4333-8333-333333333333', failure_seq: 4,
    source_request: null, agent: null, current_model: null, limits: null,
    eligible: false, blocked_reason: 'source_invalid', plan_hash: null })
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue([{ width: 10, height: 10 }] as unknown as DOMRectList)
})
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.restoreAllMocks() })

it('checks teammate retry eligibility only on demand and respects a blocked plan', async () => {
  const item = response(); item.completion.outcome_status = 'failed'; item.outcomes[0] = {...item.outcomes[0], kind:'failure', phase:'failed', summary_code:'failure_teammate_result_incomplete'}
  vi.mocked(requestInspectorApi.get).mockResolvedValue(item)
  vi.mocked(requestRetriesApi.preflight).mockResolvedValueOnce({ version: 1, room_id: 501, failure_event_id: item.outcomes[0].event_id, failure_seq: 4, source_request: null, agent: null, current_model: null, limits: null, eligible: false, blocked_reason: 'teammate_result_incomplete', plan_hash: null })
  render(<RequestInspector roomId={501} triggerEventId={triggerA} ownerId={1} authEpoch={useAuthStore.getState().epoch} onClose={vi.fn()} onNavigate={vi.fn()} />)
  expect(await screen.findByText('Failed')).toBeTruthy()
  expect(requestRetriesApi.preflight).not.toHaveBeenCalled()
  expect(requestRetriesApi.start).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Retry only this agent' }))
  expect(await screen.findByText(/This attempt cannot be retried because the teammate response/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Start retry' })).toBeNull()
  expect(requestRetriesApi.start).not.toHaveBeenCalled()
})

it('offers an explicit teammate retry only after an eligible exact preflight', async () => {
  const item = response(); item.completion.outcome_status = 'failed'; item.outcomes[0] = {...item.outcomes[0], kind:'failure', phase:'failed', summary_code:'failure_teammate_result_incomplete'}
  vi.mocked(requestInspectorApi.get).mockResolvedValue(item)
  vi.mocked(requestRetriesApi.preflight).mockResolvedValueOnce({version:1, room_id:501, failure_event_id:item.outcomes[0].event_id, failure_seq:4, source_request:{event_id:triggerA,seq:1,preview:'First exact request',href:''},agent:{agent_id:8,name_snapshot:'Ada',current_name:'Ada'},current_model:{connection_id:2,model_id:'synthetic',provider_kind:'opencode'},limits:{max_rounds:1,max_tool_calls:8,max_executor_seconds:60},teammate_recovery:{version:1,direct_count:1,nested_count:2,total_chars:4200,single_attempt:true},eligible:true,blocked_reason:null,plan_hash:'a'.repeat(64)})
  render(<RequestInspector roomId={501} triggerEventId={triggerA} ownerId={1} authEpoch={useAuthStore.getState().epoch} onClose={vi.fn()} onNavigate={vi.fn()} />)
  const open = await screen.findByRole('button', {name:'Retry only this agent'})
  expect(requestRetriesApi.preflight).not.toHaveBeenCalled(); expect(requestRetriesApi.start).not.toHaveBeenCalled()
  fireEvent.click(open)
  expect(await screen.findByRole('button', {name:'Start retry'})).toBeTruthy()
  expect(requestRetriesApi.start).not.toHaveBeenCalled()
})

it('offers an explicitly confirmed second teammate attempt only for a valid v2 summary', async () => {
  const item=response(); item.completion.outcome_status='failed'; item.outcomes[0]={...item.outcomes[0],kind:'failure',phase:'failed',summary_code:'failure_teammate_result_incomplete'}
  vi.mocked(requestInspectorApi.get).mockResolvedValue(item)
  vi.mocked(requestRetriesApi.preflight).mockResolvedValueOnce({version:1,room_id:501,failure_event_id:item.outcomes[0].event_id,failure_seq:4,source_request:{event_id:triggerA,seq:1,preview:'First exact request',href:''},agent:{agent_id:8,name_snapshot:'Ada',current_name:'Ada'},current_model:{connection_id:2,model_id:'synthetic',provider_kind:'opencode'},limits:{max_rounds:1,max_tool_calls:8,max_executor_seconds:60},teammate_recovery:{version:2,direct_count:1,nested_count:2,total_chars:4200,attempt_number:2,single_attempt:false},eligible:true,blocked_reason:null,plan_hash:'a'.repeat(64)})
  render(<RequestInspector roomId={501} triggerEventId={triggerA} ownerId={1} authEpoch={useAuthStore.getState().epoch} onClose={vi.fn()} onNavigate={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button',{name:'Retry only this agent'}))
  expect(await screen.findByText(/manual attempt 2/)).toBeTruthy()
  expect(screen.getByRole('button',{name:'Start retry'})).toBeTruthy()
  expect(requestRetriesApi.start).not.toHaveBeenCalled()
})

it('rejects a malformed v2 teammate attempt without starting', async () => {
  const item=response(); item.completion.outcome_status='failed'; item.outcomes[0]={...item.outcomes[0],kind:'failure',phase:'failed',summary_code:'failure_teammate_result_incomplete'}
  vi.mocked(requestInspectorApi.get).mockResolvedValue(item)
  vi.mocked(requestRetriesApi.preflight).mockResolvedValueOnce({version:1,room_id:501,failure_event_id:item.outcomes[0].event_id,failure_seq:4,source_request:{event_id:triggerA,seq:1,preview:'First exact request',href:''},agent:{agent_id:8,name_snapshot:'Ada',current_name:'Ada'},current_model:{connection_id:2,model_id:'synthetic',provider_kind:'opencode'},limits:{max_rounds:1,max_tool_calls:8,max_executor_seconds:60},teammate_recovery:{version:2,direct_count:1,nested_count:2,total_chars:4200,attempt_number:0,single_attempt:false},eligible:true,blocked_reason:null,plan_hash:'a'.repeat(64)})
  render(<RequestInspector roomId={501} triggerEventId={triggerA} ownerId={1} authEpoch={useAuthStore.getState().epoch} onClose={vi.fn()} onNavigate={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button',{name:'Retry only this agent'}))
  expect(await screen.findByRole('alert')).toBeTruthy()
  expect(screen.queryByRole('button',{name:'Start retry'})).toBeNull()
  expect(requestRetriesApi.start).not.toHaveBeenCalled()
})

it('rejects a teammate eligible plan lacking a verified recovery summary', async () => {
  const item=response(); item.completion.outcome_status='failed'; item.outcomes[0]={...item.outcomes[0],kind:'failure',phase:'failed',summary_code:'failure_teammate_result_incomplete'}
  vi.mocked(requestInspectorApi.get).mockResolvedValue(item)
  vi.mocked(requestRetriesApi.preflight).mockResolvedValueOnce({version:1,room_id:501,failure_event_id:item.outcomes[0].event_id,failure_seq:4,source_request:{event_id:triggerA,seq:1,preview:'First exact request',href:''},agent:{agent_id:8,name_snapshot:'Ada',current_name:'Ada'},current_model:{connection_id:2,model_id:'synthetic',provider_kind:'opencode'},limits:{max_rounds:1,max_tool_calls:8,max_executor_seconds:60},eligible:true,blocked_reason:null,plan_hash:'a'.repeat(64)})
  render(<RequestInspector roomId={501} triggerEventId={triggerA} ownerId={1} authEpoch={useAuthStore.getState().epoch} onClose={vi.fn()} onNavigate={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button',{name:'Retry only this agent'}))
  expect(await screen.findByRole('alert')).toBeTruthy()
  expect(screen.queryByRole('button',{name:'Start retry'})).toBeNull()
  expect(requestRetriesApi.start).not.toHaveBeenCalled()
})

it('loads only the exact trigger and keeps historical identity and technical facts collapsed', async () => {
  const navigate = vi.fn()
  render(<RequestInspector roomId={501} triggerEventId={triggerA} ownerId={1} authEpoch={useAuthStore.getState().epoch} onClose={vi.fn()} onNavigate={navigate} />)
  expect(await screen.findByText('First exact request')).toBeTruthy()
  expect(requestInspectorApi.get).toHaveBeenCalledWith(501, triggerA)
  expect(screen.getAllByText('Ada at request time')).toHaveLength(2)
  expect(screen.queryByText('historical-model')).toBeNull()
  const details = screen.getAllByText('Technical details')[0].closest('details')!
  fireEvent.click(within(details).getByText('Technical details'))
  expect(within(details).getByText(/historical-model/)).toBeTruthy()
  fireEvent.click(screen.getAllByRole('button', { name: 'Go to message' })[0])
  expect(navigate).toHaveBeenCalledWith('/chat/501?seq=4')
})

it('drops a late overlapping request response after the exact trigger changes', async () => {
  let finishA!: (value: RequestInspectorResponse) => void
  vi.mocked(requestInspectorApi.get).mockImplementationOnce(() => new Promise((resolve) => { finishA = resolve }))
    .mockResolvedValueOnce(response(triggerB))
  const epoch = useAuthStore.getState().epoch
  const view = render(<RequestInspector roomId={501} triggerEventId={triggerA} ownerId={1} authEpoch={epoch} onClose={vi.fn()} onNavigate={vi.fn()} />)
  view.rerender(<RequestInspector roomId={501} triggerEventId={triggerB} ownerId={1} authEpoch={epoch} onClose={vi.fn()} onNavigate={vi.fn()} />)
  expect(await screen.findByText('Overlapping later request')).toBeTruthy()
  act(() => finishA(response(triggerA)))
  await waitFor(() => expect(screen.queryByText('First exact request')).toBeNull())
})

it('preserves an explicit retry after an ordinary load failure', async () => {
  vi.mocked(requestInspectorApi.get).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(response())
  render(<RequestInspector roomId={501} triggerEventId={triggerA} ownerId={1} authEpoch={useAuthStore.getState().epoch} onClose={vi.fn()} onNavigate={vi.fn()} />)
  expect((await screen.findByRole('alert')).textContent).toBe('Request progress could not be loaded.')
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(await screen.findByText('First exact request')).toBeTruthy()
})

it('drops a deferred rejection after the auth owner changes', async () => {
  let fail!: (error: Error) => void
  vi.mocked(requestInspectorApi.get).mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject }))
  render(<RequestInspector roomId={501} triggerEventId={triggerA} ownerId={1} authEpoch={useAuthStore.getState().epoch} onClose={vi.fn()} onNavigate={vi.fn()} />)
  await waitFor(() => expect(requestInspectorApi.get).toHaveBeenCalled())
  act(() => useAuthStore.getState().setAuth('owner-two', { id: 2, email: 'two@example.com', username: 'two' }))
  act(() => fail(new Error('prior owner failed')))
  await act(async () => {})
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.queryByText('First exact request')).toBeNull()
})

it('traps focus among visible controls and skips descendants of closed technical details', async () => {
  render(<RequestInspector roomId={501} triggerEventId={triggerA} ownerId={1} authEpoch={useAuthStore.getState().epoch} onClose={vi.fn()} onNavigate={vi.fn()} />)
  await screen.findByText('First exact request')
  const closed = screen.getAllByText('Technical details')[0].closest('details')!
  const hidden = document.createElement('button'); hidden.textContent = 'hidden detail control'; closed.append(hidden)
  const last = screen.getByText('Settings used for this request') as HTMLElement
  last.focus(); fireEvent.keyDown(document, { key: 'Tab' })
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Close' }))
  fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
  expect(document.activeElement).toBe(last)
  expect(document.activeElement).not.toBe(hidden)
})

it('states when historical model and action time were not recorded', async () => {
  const legacy = response()
  legacy.outcomes[0] = { ...legacy.outcomes[0], model_use: null }
  legacy.actions[0] = { ...legacy.actions[0], requested_at: null }
  vi.mocked(requestInspectorApi.get).mockResolvedValueOnce(legacy)
  render(<RequestInspector roomId={501} triggerEventId={triggerA} ownerId={1} authEpoch={useAuthStore.getState().epoch} onClose={vi.fn()} onNavigate={vi.fn()} />)
  await screen.findByText('First exact request')
  for (const summary of screen.getAllByText('Technical details')) fireEvent.click(summary)
  expect(screen.getAllByText('Not recorded')).toHaveLength(2)
})

it('shows canonical completion separately from execution records', async () => {
  render(<RequestInspector roomId={501} triggerEventId={triggerA} ownerId={1}
    authEpoch={useAuthStore.getState().epoch} onClose={vi.fn()} onNavigate={vi.fn()} />)
  expect(await screen.findByText('Partial result: some selected Agent work did not succeed.')).toBeTruthy()
  expect(screen.getByText('1 round completed')).toBeTruthy()
})

it('does not celebrate an all-pass completion without a useful result', async () => {
  const allPass = response()
  allPass.completion = { state: 'completed', outcome_status: 'succeeded', result_available: false,
    completed_rounds: 1, pending_agent_count: 0 }
  allPass.outcomes = allPass.outcomes.map((outcome) => ({ ...outcome, kind: 'passed', phase: 'passed', summary_code: 'passed' }))
  vi.mocked(requestInspectorApi.get).mockResolvedValueOnce(allPass)
  render(<RequestInspector roomId={501} triggerEventId={triggerA} ownerId={1}
    authEpoch={useAuthStore.getState().epoch} onClose={vi.fn()} onNavigate={vi.fn()} />)
  expect(await screen.findByText('Completed without a usable Agent response.')).toBeTruthy()
  expect(screen.queryByText('Request succeeded')).toBeNull()
})

it('offers lazy targeted retry only for active failed outcomes', async () => {
  const failed = response()
  failed.outcomes[0] = { ...failed.outcomes[0], kind: 'failure', phase: 'failed', summary_code: 'failure_tool' }
  vi.mocked(requestInspectorApi.get).mockResolvedValueOnce(failed)
  const rendered = render(<RequestInspector roomId={501} triggerEventId={triggerA} ownerId={1}
    authEpoch={useAuthStore.getState().epoch} onClose={vi.fn()} onNavigate={vi.fn()} />)
  const retry = await screen.findByRole('button', { name: 'Retry only this agent' })
  expect(requestRetriesApi.preflight).not.toHaveBeenCalled()
  fireEvent.click(retry)
  await waitFor(() => expect(requestRetriesApi.preflight).toHaveBeenCalledWith(501, failed.outcomes[0].event_id))
  expect(requestRetriesApi.start).not.toHaveBeenCalled()
  rendered.unmount()

  const archived = { ...failed, read_only: true }
  vi.mocked(requestInspectorApi.get).mockResolvedValueOnce(archived)
  render(<RequestInspector roomId={501} triggerEventId={triggerA} ownerId={1}
    authEpoch={useAuthStore.getState().epoch} onClose={vi.fn()} onNavigate={vi.fn()} />)
  await screen.findByText('First exact request')
  expect(screen.queryByRole('button', { name: 'Retry only this agent' })).toBeNull()
})
