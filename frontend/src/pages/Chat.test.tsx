import { readPastedChatReference } from '../lib/resultLink'
// @vitest-environment jsdom
import { roomMemoriesApi } from '../api/roomMemories'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom'
import Chat from './Chat'
import { roomEventCursor } from '../lib/eventCursor'
import { useAuthStore } from '../stores/authStore'
import { queryClient } from '../lib/queryClient'
import { invoke, isTauri } from '@tauri-apps/api/core'
import type { AxiosAdapter } from 'axios'
import api from '../lib/api'

const room = { id: 501, project_id: 40, user_id: 1, title: 'Topic A', is_active: true }
const ACTION_ID = '1d1e0e6c-9eeb-4571-864b-f2592c44db38'
const peers = [{ id: 8, name: 'Ada', connection_id: 1, model_id: 'exact-one', is_active: true }, { id: 9, name: 'Bea', connection_id: 2, model_id: 'exact-two', is_active: true }]
const event = (seq: number, actor_name_snapshot: string) => ({ event_id: `e${seq}`, room_id: 501, seq, device_id: null,
  actor_type: 'agent', actor_id: seq, actor_name_snapshot, kind: 'message', payload: { content: `Reply ${seq}` },
  created_at: '2026-09-28T00:00:00Z', idempotency_key: null })
let socket: { onmessage: ((event: { data: string }) => void) | null; onopen: (() => void) | null;
  onerror: (() => void) | null; onclose: (() => void) | null; close: ReturnType<typeof vi.fn> }

vi.mock('../api/conversations', () => ({ conversationsApi: {
  get: vi.fn(), listMembers: vi.fn(), cancel: vi.fn(), continue: vi.fn(), stop: vi.fn(), delete: vi.fn(),
} }))
vi.mock('../api/projects', () => ({ projectsApi: { get: vi.fn() } }))
vi.mock('../api/agents', () => ({ agentsApi: { list: vi.fn() } }))
vi.mock('../api/connections', () => ({ connectionsApi: { list: vi.fn(), catalog: vi.fn() } }))
vi.mock('../api/events', async (importOriginal) => ({ ...await importOriginal<typeof import('../api/events')>(), eventsApi: { list: vi.fn(), send: vi.fn() } }))
vi.mock('../api/drafts', () => ({ draftsApi: { get: vi.fn(), put: vi.fn(), clear: vi.fn() } }))
vi.mock('../api/readStates', () => ({ readStatesApi: { get: vi.fn(), put: vi.fn() } }))
vi.mock('../api/schedules', () => ({ schedulesApi: { get: vi.fn(), put: vi.fn() } }))
vi.mock('../api/summaries', () => ({ summariesApi: { create: vi.fn() } }))
vi.mock('../api/contextPreviews', () => ({ contextPreviewsApi: { create: vi.fn() } }))
vi.mock('../api/requestInspector', () => ({ requestInspectorApi: { get: vi.fn() } }))
vi.mock('../api/requestRetries', () => ({ requestRetriesApi: { preflight: vi.fn(), start: vi.fn() } }))
vi.mock('../api/requestFinalize', () => ({ requestFinalizeApi: { preview: vi.fn(), execute: vi.fn() } }))
vi.mock('../api/starter', () => ({ starterApi: { lastResult: vi.fn() } }))
vi.mock('../api/roomResults', () => ({ roomResultsApi: { list: vi.fn() } }))
vi.mock('../api/tools', () => ({ toolsApi: { activity: vi.fn(), grants: vi.fn(), broadMode: vi.fn(), setBroadMode: vi.fn(), lease: vi.fn(), preview: vi.fn(), decide: vi.fn(), screenshot: vi.fn(), deleteScreenshot: vi.fn() } }))
vi.mock('../lib/localHost', () => ({
  openRoomSocket: vi.fn(),
  getLocalEndpoint: vi.fn().mockResolvedValue({ base_url: 'http://127.0.0.1:9000', session_token: 'local-test-session' }),
}))
vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn(), isTauri: vi.fn() }))

let createdObjectUrl = 0
const revokedObjectUrls: string[] = []
const NativeURL = URL
const originalApiAdapter = api.defaults.adapter

class VisibleObserver {
  static latest: VisibleObserver | null = null
  private callback: IntersectionObserverCallback
  private target: Element | null = null
  constructor(callback: IntersectionObserverCallback) { this.callback = callback; VisibleObserver.latest = this }
  observe(target: Element) { this.target = target; this.emit(true) }
  emit(isIntersecting: boolean) {
    if (this.target) this.callback([{ isIntersecting, target: this.target } as IntersectionObserverEntry], this as unknown as IntersectionObserver)
  }
  disconnect() {}
  unobserve() {}
  takeRecords() { return [] }
  root = null
  rootMargin = ''
  thresholds = [0]
}

import { conversationsApi } from '../api/conversations'
import { projectsApi } from '../api/projects'
import { agentsApi } from '../api/agents'
import { connectionsApi } from '../api/connections'
import { eventsApi } from '../api/events'
import { draftsApi } from '../api/drafts'
import { readStatesApi } from '../api/readStates'
import { schedulesApi } from '../api/schedules'
import { summariesApi } from '../api/summaries'
import { contextPreviewsApi } from '../api/contextPreviews'
import { requestInspectorApi, type RequestInspectorResponse } from '../api/requestInspector'
import { requestRetriesApi } from '../api/requestRetries'
import { requestFinalizeApi } from '../api/requestFinalize'
import { starterApi } from '../api/starter'
import { toolsApi } from '../api/tools'
import { roomResultsApi } from '../api/roomResults'
import { openRoomSocket } from '../lib/localHost'

beforeEach(() => {
  useAuthStore.getState().setAuth('owner', { id: 1, email: 'owner@example.invalid', username: 'Owner' })
  createdObjectUrl = 0
  VisibleObserver.latest = null
  Element.prototype.scrollIntoView = vi.fn()
  roomEventCursor.clear()
  socket = { onmessage: null, onopen: null, onerror: null, onclose: null, close: vi.fn() }
  vi.mocked(openRoomSocket).mockResolvedValue(socket as unknown as WebSocket)
  vi.mocked(conversationsApi.get).mockResolvedValue(room as never)
  vi.mocked(conversationsApi.delete).mockResolvedValue(undefined)
  vi.mocked(conversationsApi.listMembers).mockResolvedValue([{ room_id: 501, agent_id: 8, is_active: true, joined_at: '' },
    { room_id: 501, agent_id: 9, is_active: true, joined_at: '' }] as never)
  vi.mocked(projectsApi.get).mockResolvedValue({ id: 40, user_id: 1, name: 'Project', is_active: true } as never)
  vi.mocked(agentsApi.list).mockResolvedValue(peers as never)
  vi.mocked(connectionsApi.list).mockResolvedValue([
    { id: 1, enabled: true, provider_kind: 'openai', label: 'Primary OpenAI', auth_required: true, has_secret: true,
      capabilities: { declared: {}, verified: { models: { 'exact-one': { tools: true } } } } },
    { id: 2, enabled: true, provider_kind: 'anthropic', label: 'Primary Anthropic', auth_required: true, has_secret: true,
      capabilities: { declared: {}, verified: { models: {} } } },
  ] as never)
  vi.mocked(connectionsApi.catalog).mockResolvedValue({ version: 1, providers: [
    { provider_id: 'openai', label_key: 'connections.providers.openai', icon: 'openai', protocols: ['openai_chat_completions'],
      fields: [], model_source: { strategy: 'configured', manual_id: true, static_model_ids: [] }, methods: [],
      technical_compatibility: 'supported', usage_classification: 'verified', default_base_url: 'https://api.openai.com/v1' },
    { provider_id: 'anthropic', label_key: 'connections.providers.anthropic', icon: 'anthropic', protocols: ['anthropic_messages'],
      fields: [], model_source: { strategy: 'configured', manual_id: true, static_model_ids: [] }, methods: [],
      technical_compatibility: 'supported', usage_classification: 'verified', default_base_url: 'https://api.anthropic.com' },
  ] } as never)
  vi.mocked(eventsApi.list).mockImplementation(async (_roomId, after) => after === 0 ? [event(1, 'Ada'), event(2, 'Bea')] as never : [])
  vi.mocked(eventsApi.send).mockResolvedValue({ ...event(3, 'Me'), actor_type: 'user', payload: { content: 'Hello' } } as never)
  vi.mocked(draftsApi.get).mockResolvedValue({ content: '', version: 0, idempotency_key: null, updated_at: null })
  vi.mocked(draftsApi.put).mockImplementation(async (_roomId, content, expectedVersion) => ({
    content, version: expectedVersion + 1, idempotency_key: content ? '123e4567-e89b-42d3-a456-426614174000' : null,
    updated_at: content ? '2026-10-03T00:00:00Z' : null,
  }))
  vi.mocked(draftsApi.clear).mockResolvedValue(undefined)
  vi.mocked(readStatesApi.get).mockResolvedValue({ last_read_seq: 0, updated_at: null })
  vi.mocked(readStatesApi.put).mockImplementation(async (_roomId, lastReadSeq) => ({ last_read_seq: lastReadSeq, updated_at: '2026-10-03T00:00:00Z' }))
  vi.mocked(schedulesApi.get).mockResolvedValue({ max_rounds: 2, max_tool_calls: 8, max_executor_seconds: 60 })
  vi.mocked(schedulesApi.put).mockImplementation(async (_roomId, value) => value)
  vi.mocked(summariesApi.create).mockRejectedValue(new Error('unexpected summary write'))
  vi.mocked(contextPreviewsApi.create).mockResolvedValue({ room_id: 501, projected_seq: 3, selected_agent_ids: [8], fingerprint: 'b'.repeat(64), agents: [{
    agent_id: 8, agent_name: 'Ada', model_id: 'exact-one', projection_status: 'ready', error_code: null, system_text: 'Be helpful.',
    messages: [{ role: 'user', content: '@Ada inspect this draft' }], source_coverage: { committed_high_water: 2, included_event_seqs: [1, 2], summarized_ranges: [], draft_included: true },
    truncations: [], non_text: [], budget: { unit: 'characters', limit_chars: 12000, used_chars: 50, remaining_chars: 11950 },
  }] })
  vi.mocked(requestInspectorApi.get).mockResolvedValue({ version: 1, room_id: 501, room_title: 'Topic A', read_only: false,
    request: { event_id: '11111111-1111-4111-8111-111111111111', seq: 1, created_at: '2026-10-05T00:00:00Z', actor_name_snapshot: 'DemoUser', preview: 'Inspect this request', href: '/chat/501?seq=1' },
    execution: { phase: 'completed', selected_agents: [{ agent_id: 8, name_snapshot: 'Ada', live_state: null }], selected_agent_count: 1,
      policy: { max_rounds: 2, max_tool_calls: 8, max_executor_seconds: 60 }, current_round: 1 },
    completion: { state: 'completed', outcome_status: 'succeeded', result_available: false, completed_rounds: 1, pending_agent_count: 0 },
    outcomes: [], outcome_count: 0, outcomes_truncated: false, actions: [], action_count: 0, actions_truncated: false })
  vi.mocked(requestRetriesApi.preflight).mockRejectedValue(new Error('unexpected retry preflight'))
  vi.mocked(requestRetriesApi.start).mockRejectedValue(new Error('unexpected retry start'))
  vi.mocked(starterApi.lastResult).mockResolvedValue(null)
  vi.mocked(requestFinalizeApi.preview).mockRejectedValue(new Error('unexpected finalize preview'))
  vi.mocked(requestFinalizeApi.execute).mockRejectedValue(new Error('unexpected finalize execute'))
  vi.mocked(toolsApi.activity).mockResolvedValue([])
  vi.mocked(toolsApi.grants).mockResolvedValue([])
  vi.mocked(toolsApi.broadMode).mockResolvedValue({ enabled: false, scope: null, expires_at: null })
  vi.mocked(toolsApi.setBroadMode).mockResolvedValue({ enabled: false, scope: null, expires_at: null })
  vi.mocked(toolsApi.lease).mockResolvedValue({ active: false, owner: null, another_profile_or_room: false })
  vi.mocked(toolsApi.preview).mockResolvedValue({ review_id: '550e8400-e29b-41d4-a716-446655440000', approval_policy: { scopes: ['once', 'room_session'], unverified_context_required: false, start_within_seconds: null }, action_id: 'pending-a', family: 'write_file', target: '/tmp/fixture',
    expected_effect: 'Write 7 bytes to file', arguments: { path: '/tmp/fixture' }, scope_description: 'one room session',
    scope_target: '/tmp/fixture', workflow_id: null, payload_digest: 'digest' })
  vi.mocked(toolsApi.decide).mockResolvedValue({})
  vi.mocked(toolsApi.screenshot).mockResolvedValue(new Blob(['synthetic-png'], { type: 'image/png' }))
  vi.mocked(toolsApi.deleteScreenshot).mockResolvedValue(undefined)
  vi.mocked(invoke).mockResolvedValue(undefined)
  vi.mocked(isTauri).mockReturnValue(false)
  vi.stubGlobal('IntersectionObserver', VisibleObserver)
  class TestURL extends NativeURL {
    static createObjectURL = vi.fn(() => `blob:screen-${++createdObjectUrl}`)
    static revokeObjectURL = vi.fn((url: string) => revokedObjectUrls.push(url))
  }
  vi.stubGlobal('URL', TestURL)
})

it('opens an explicitly declared exact peer after loading multiple chunks without changing the draft', async () => {
  const id = '11111111-1111-4111-8111-111111111111'
  const history = Array.from({ length: 101 }, (_, index) => event(index + 1, 'Ada'))
  history[0] = { ...event(1, 'Historical Ada'), event_id: id, actor_id: 8, payload: { content: 'Original proposal' } }
  history[100] = { ...event(101, 'Bea'), event_id: '33333333-3333-4333-8333-333333333333', actor_id: 9,
    payload: { content: 'A specific reply', reply_provenance: { version: 1, watermark_seq: 100, declaration_state: 'declared',
      provided_peer_event_ids: [id], reply_to_event_ids: [id] } } } as never
  vi.mocked(eventsApi.list).mockImplementation(async (_id, after) => after === 0 ? history.slice(0, 100) as never
    : after === 100 ? history.slice(100) as never : [])
  view(501, '?seq=101')
  const open = await screen.findByRole('button', { name: 'Open original: Historical Ada, #1' })
  fireEvent.change(screen.getByRole('combobox', { name: 'Message' }), { target: { value: 'Unsent draft' } })
  fireEvent.click(open)
  await waitFor(() => expect(document.querySelector('[data-event-seq="1"]')?.classList.contains('conversation-event--target')).toBe(true))
  expect(eventsApi.list).toHaveBeenCalledWith(501, 100, 100)
  expect((screen.getByRole('combobox', { name: 'Message' }) as HTMLTextAreaElement).value).toBe('Unsent draft')
  expect(eventsApi.send).not.toHaveBeenCalled()
  const target = document.querySelector('[data-event-seq="1"]') as HTMLElement
  vi.mocked(target.scrollIntoView).mockClear()
  fireEvent.click(open)
  await waitFor(() => expect(target.scrollIntoView).toHaveBeenCalledWith({ block: 'center' }))
  vi.mocked(target.scrollIntoView).mockClear()
  await waitFor(() => expect(socket.onmessage).toBeTruthy())
  act(() => socket.onmessage!({ data: JSON.stringify({ type: 'event', ...event(102, 'Bea') }) }))
  await screen.findByText('Reply 102')
  expect(target.scrollIntoView).not.toHaveBeenCalled()
  expect((screen.getByRole('combobox', { name: 'Message' }) as HTMLTextAreaElement).value).toBe('Unsent draft')
})

it('does not jump to a different event at an expected source sequence', async () => {
  view(501, '?seq=1&event=11111111-1111-4111-8111-111111111111')
  await screen.findByText('This message is not available in the room history.')
  expect(document.querySelector('.conversation-event--target')).toBeNull()
  expect(readStatesApi.put).not.toHaveBeenCalled()
})

it('removes a visible old-room feed and failed draft before the next room can send', async () => {
  vi.mocked(conversationsApi.get).mockImplementation(async (id) => ({ ...room, id, title: id === 501 ? 'Topic A' : 'Topic B' } as never))
  vi.mocked(eventsApi.list).mockImplementation(async (id, after) => id === 501 && after === 0 ? [event(1, 'Ada'), event(2, 'Bea')] as never : [])
  vi.mocked(eventsApi.send).mockRejectedValueOnce(new Error('network'))
  view(501)
  await screen.findByText('Reply 1')
  fireEvent.change(screen.getByRole('combobox', { name: 'Message' }), { target: { value: 'A private draft' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send message' }))
  await screen.findByText(/message could not be confirmed/i)
  fireEvent.click(screen.getByRole('link', { name: 'Switch room' }))
  expect(screen.queryByText('Reply 1')).toBeNull()
  expect(screen.queryByText(/message could not be confirmed/i)).toBeNull()
  expect((await screen.findByRole('combobox', { name: 'Message' }) as HTMLInputElement).value).toBe('')
  expect((screen.getByRole('button', { name: 'Send message' }) as HTMLButtonElement).disabled).toBe(true)
  expect(eventsApi.send).toHaveBeenCalledTimes(1)
  expect(vi.mocked(eventsApi.send).mock.calls[0][0]).toBe(501)
})

it('flushes a recent draft when leaving the room without exposing it in the next room', async () => {
  vi.mocked(conversationsApi.get).mockImplementation(async (id) => ({ ...room, id } as never))
  vi.mocked(draftsApi.get).mockImplementation(async (id) => id === 501
    ? { content: '', version: 0, idempotency_key: null, updated_at: null }
    : { content: '', version: 3, idempotency_key: null, updated_at: null })
  view(501)
  const composer = await screen.findByRole('combobox', { name: 'Message' })
  await waitFor(() => expect(draftsApi.get).toHaveBeenCalledWith(501))
  fireEvent.change(composer, { target: { value: 'Leave safely', selectionStart: 12 } })
  fireEvent.click(screen.getByRole('link', { name: 'Switch room' }))
  await waitFor(() => expect(draftsApi.put).toHaveBeenCalledWith(501, 'Leave safely', 0))
  await waitFor(() => expect(draftsApi.get).toHaveBeenCalledWith(602))
  expect((screen.getByRole('combobox', { name: 'Message' }) as HTMLTextAreaElement).value).toBe('')
})

it('drops visible feed and failed draft on direct owner replacement without logout', async () => {
  vi.mocked(conversationsApi.get).mockImplementation(async () => useAuthStore.getState().user?.id === 1 ?
    { ...room, id: 701, title: 'Owner one room' } as never : Promise.reject(new Error('foreign room')))
  vi.mocked(eventsApi.list).mockImplementation(async (_id, after) => useAuthStore.getState().user?.id === 1 && after === 0 ?
    [{ ...event(1, 'Ada'), event_id: 'owner-one-1', room_id: 701, payload: { content: 'Old owner event' } }] as never : [])
  vi.mocked(eventsApi.send).mockRejectedValueOnce(new Error('network'))
  act(() => useAuthStore.getState().setAuth('owner-one', { id: 1, email: 'one@example.com', username: 'one' }))
  render(<QueryClientProvider client={queryClient}><MemoryRouter initialEntries={['/chat/701']}>
    <Routes><Route path="/chat/:conversationId" element={<Chat />} /></Routes>
  </MemoryRouter></QueryClientProvider>)
  await screen.findByText('Old owner event')
  fireEvent.change(screen.getByRole('combobox', { name: 'Message' }), { target: { value: 'Old owner draft' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send message' }))
  await screen.findByText(/message could not be confirmed/i)
  act(() => useAuthStore.getState().setAuth('owner-two', { id: 2, email: 'two@example.com', username: 'two' }))
  expect(screen.queryByText('Old owner event')).toBeNull()
  expect(screen.queryByText('Owner one room')).toBeNull()
  expect(screen.queryByText(/message could not be confirmed/i)).toBeNull()
  expect(screen.queryByRole('combobox', { name: 'Message' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Send message' })).toBeNull()
  act(() => useAuthStore.getState().logout())
})
afterEach(() => { cleanup(); useAuthStore.getState().logout(); vi.clearAllMocks(); vi.unstubAllGlobals(); revokedObjectUrls.length = 0; api.defaults.adapter = originalApiAdapter })

function view(roomId = 501, search = '', queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return render(<QueryClientProvider client={queryClient}>
    <MemoryRouter initialEntries={[`/chat/${roomId}${search}`]}><Link to="/chat/602">Switch room</Link><Routes><Route path="/chat/:conversationId" element={<Chat />} /></Routes></MemoryRouter>
  </QueryClientProvider>)
}

it.each(['teammate_result_incomplete', 'teammate_result_changed'])('explains teammate failure %s without rerunning or claiming success', async (code) => {
  useAuthStore.getState().setAuth('synthetic-owner', { id: 1, email: 'owner@example.invalid', username: 'Owner' })
  vi.mocked(conversationsApi.get).mockResolvedValue({ ...room, user_id: 1 } as never)
  vi.mocked(projectsApi.get).mockResolvedValue({ id: 40, user_id: 1, name: 'Project', is_active: true } as never)
  api.defaults.adapter = (async (request) => {
    if (request.url === '/conversations/501/shared-binding') return { data: { kind: 'ordinary' }, status: 200, statusText: 'OK', headers: {}, config: request }
    throw new Error('No live network in this test')
  }) satisfies AxiosAdapter
  const failed = { ...event(2, 'Ada'), event_id: 'failure-incomplete', kind: 'failure' as const,
    payload: { code, message: 'Synthetic incomplete response' } }
  vi.mocked(eventsApi.list).mockImplementation(async (_room, after) => after === 0 ? [
    { ...event(1, 'Owner'), actor_type: 'user' as const, actor_id: null }, failed,
  ] as never : [])
  view()
  expect((await screen.findAllByText(/The teammate response could not be read in full/)).length).toBeGreaterThan(0)
  expect(screen.getAllByText(/Nothing is rerun automatically/).length).toBeGreaterThan(0)
  expect(eventsApi.send).not.toHaveBeenCalled()
  expect(requestRetriesApi.start).not.toHaveBeenCalled()
})

it('opens the exact work source from an attention URL without keeping its old attention target or sending the draft', async () => {
  const id = '550e8400-e29b-41d4-a716-446655440000'
  vi.mocked(eventsApi.list).mockImplementation(async (_room, after) => after === 0
    ? [{ ...event(1, 'Owner'), event_id: id, actor_type: 'user', payload: { content: 'Synthetic request' } }] as never : [])
  vi.mocked(conversationsApi.listMembers).mockResolvedValue([{ room_id: 501, agent_id: 8, is_active: true, joined_at: '',
    work_state: { version: 1, room_id: 501, agent_id: 8, phase: 'thinking', scope: 'current', trigger_event_id: id,
      generation: 4, round: 1, request: { event_id: id, seq: 1 }, peer_agent_ids: [] } }] as never)
  view(501, `?action=${ACTION_ID}`)
  await screen.findByText('Synthetic request')
  await waitFor(() => expect(socket.onopen).toBeTruthy())
  act(() => socket.onopen?.())
  fireEvent.change(screen.getByRole('combobox', { name: 'Message' }), { target: { value: 'Unsent source draft' } })
  fireEvent.click(screen.getByRole('button', { name: 'Conversation options' }))
  const dialog = await screen.findByRole('dialog', { name: 'Conversation options' })
  fireEvent.click(await within(dialog).findByRole('button', { name: 'Request' }))
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Conversation options' })).toBeNull())
  await waitFor(() => expect(document.querySelector('[data-event-seq="1"]')?.classList.contains('conversation-event--target')).toBe(true))
  expect((screen.getByRole('combobox', { name: 'Message' }) as HTMLTextAreaElement).value).toBe('Unsent source draft')
  expect(eventsApi.send).not.toHaveBeenCalled()
  expect(toolsApi.decide).not.toHaveBeenCalled()
})

it('previews the current unsent draft without dispatching and clears it after an edit', async () => {
  view()
  await screen.findByText('Reply 1')
  const composer = screen.getByRole('combobox', { name: 'Message' })
  fireEvent.change(composer, { target: { value: '@Ada inspect this draft', selectionStart: 23 } })
  fireEvent.click(screen.getByRole('button', { name: 'Preview what Agents will see' }))
  const dialog = await screen.findByRole('dialog', { name: 'Conversation shown to Agents' })
  expect(within(dialog).getByText('@Ada inspect this draft')).toBeTruthy()
  expect(contextPreviewsApi.create).toHaveBeenCalledWith(501, '@Ada inspect this draft')
  expect(eventsApi.send).not.toHaveBeenCalled()
  fireEvent.change(composer, { target: { value: '@Ada changed', selectionStart: 12 } })
  expect(screen.queryByRole('dialog', { name: 'Conversation shown to Agents' })).toBeNull()
})

async function openRoomTools() {
  let panel = screen.queryByRole('dialog', { name: 'Conversation options' })
  if (!panel) {
    const trigger = screen.getByRole('button', { name: 'Conversation options' })
    await waitFor(() => expect(trigger).toHaveProperty('disabled', false))
    trigger.focus(); fireEvent.click(trigger)
    panel = screen.getByRole('dialog', { name: 'Conversation options' })
  }
  return panel
}

describe('room view', () => {
  it('renders a closed room read-only without draft, dispatch, approval, or room mutation calls', async () => {
    vi.mocked(conversationsApi.get).mockResolvedValue({ ...room, is_active: false } as never)
    vi.mocked(eventsApi.list).mockImplementation(async (_roomId, after) => after === 0 ? [
      event(1, 'Ada'),
      { ...event(2, 'System'), actor_type: 'system', actor_id: null, kind: 'summary', payload: {
        owner_checkpoint: true, source_start_seq: 1, source_end_seq: 1, content: 'Preserved handoff',
      } },
    ] as never : [])
    view()
    await screen.findByText('Reply 1')
    expect(screen.getAllByText('Closed room').length).toBeGreaterThan(0)
    expect(screen.getByText(/This room is read-only/)).toBeTruthy()
    expect(screen.queryByRole('combobox', { name: 'Message' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Stop work' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Close room' })).toBeNull()
    expect(screen.queryByRole('button', { name: /Pause Ada/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /Remove Ada/ })).toBeNull()
    expect(draftsApi.get).not.toHaveBeenCalled()
    expect(toolsApi.activity).not.toHaveBeenCalled()
    expect(openRoomSocket).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Message actions for Ada' }))
    expect(screen.queryByRole('menuitem', { name: 'Quote message' })).toBeNull()
    expect(screen.getByRole('menuitem', { name: 'Copy message' })).toBeTruthy()
    await openRoomTools()
    const handoff = within(screen.getByRole('dialog', { name: 'Conversation options' })).getAllByText('Handoff note')[0].closest('details') as HTMLDetailsElement
    fireEvent.click(within(handoff).getByText('Handoff note'))
    expect(within(handoff).getByText('Preserved handoff')).toBeTruthy()
    expect(await within(handoff).findByText(/closed room’s handoff note is read-only/i)).toBeTruthy()
    expect(schedulesApi.get).toHaveBeenCalledWith(501)
    expect(eventsApi.send).not.toHaveBeenCalled()
    expect(conversationsApi.cancel).not.toHaveBeenCalled()
    expect(conversationsApi.delete).not.toHaveBeenCalled()
  })

  it('renders an active legacy child read-only when its project is archived', async () => {
    vi.mocked(projectsApi.get).mockResolvedValue({ id: 40, name: 'Archived project', is_active: false } as never)
    view()
    await screen.findByText('Reply 1')
    expect(screen.getAllByText('Closed room').length).toBeGreaterThan(0)
    expect(screen.getByText(/This room is read-only/)).toBeTruthy()
    expect(screen.queryByRole('combobox', { name: 'Message' })).toBeNull()
    expect(draftsApi.get).not.toHaveBeenCalled()
    expect(toolsApi.activity).not.toHaveBeenCalled()
    expect(toolsApi.broadMode).not.toHaveBeenCalled()
    expect(openRoomSocket).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Stop work' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Close room' })).toBeNull()
  })

  it('does not navigate a replacement owner when an earlier room close finishes late', async () => {
    act(() => useAuthStore.getState().setAuth('owner-one', { id: 1, email: 'one@example.com', username: 'one' }))
    let finish!: () => void
    vi.mocked(conversationsApi.delete).mockReturnValue(new Promise<void>((resolve) => { finish = resolve }))
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={['/chat/501']}><Routes>
        <Route path="/chat/:conversationId" element={<Chat />} />
        <Route path="/projects" element={<p>Projects destination</p>} />
      </Routes></MemoryRouter>
    </QueryClientProvider>)
    await screen.findByText('Reply 2')
    fireEvent.click(screen.getByRole('button', { name: 'Close room' }))
    await waitFor(() => expect(conversationsApi.delete).toHaveBeenCalledWith(501))
    act(() => useAuthStore.getState().setAuth('owner-two', { id: 2, email: 'two@example.com', username: 'two' }))
    await act(async () => finish())
    expect(screen.queryByText('Projects destination')).toBeNull()
    expect(await screen.findByText('Reply 2')).toBeTruthy()
    act(() => useAuthStore.getState().logout())
  })

  it('shows ordered peer snapshots once after socket replay and sends by one idempotent HTTP path', async () => {
    view()
    await screen.findByText('Reply 2')
    expect(screen.getAllByText(/Reply [12]/).map((node) => node.textContent)).toEqual(['Reply 1', 'Reply 2'])
    await waitFor(() => expect(socket.onmessage).toBeTruthy())
    socket.onmessage!({ data: JSON.stringify({ type: 'event', ...event(2, 'Bea') }) })
    expect(screen.getAllByText('Reply 2')).toHaveLength(1)
    fireEvent.change(screen.getByRole('combobox', { name: 'Message' }), { target: { value: 'Hello' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    await waitFor(() => expect(eventsApi.send).toHaveBeenCalledTimes(1))
    expect(vi.mocked(eventsApi.send).mock.calls[0][2]).toMatch(/^[0-9a-f-]{36}$/)
    expect(socket).not.toHaveProperty('send')
    expect(roomEventCursor.afterSeq(501)).toBe(3)
  })

  it('keeps long history inside the desktop room scroller while header and composer stay outside it', async () => {
    const longHistory = Array.from({ length: 72 }, (_, index) => event(index + 1, index % 2 === 0 ? 'Ada' : 'Bea'))
    vi.mocked(eventsApi.list).mockImplementation(async (_roomId, after) => after === 0 ? longHistory as never : [])
    const result = render(<div className="app-frame"><div className="locale-toolbar" />
      <div className="app-frame__content"><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={['/chat/501']}><Routes><Route path="/chat/:conversationId" element={<Chat />} /></Routes></MemoryRouter>
      </QueryClientProvider></div></div>)
    await screen.findByText('Reply 72')
    const room = result.container.querySelector('.room-shell') as HTMLElement
    const scroller = result.container.querySelector('.room-scroll') as HTMLElement
    const composer = screen.getByRole('combobox', { name: 'Message' }).closest('.room-composer')!
    expect(scroller.contains(screen.getByText('Reply 72'))).toBe(true)
    expect(result.container.querySelector('.room-rail')).toBeNull()
    expect(result.container.querySelector('.room-tools')).toBeNull()
    const panel = await openRoomTools()
    expect(panel.querySelector('.room-tools')?.contains(screen.getByText('Compare responses'))).toBe(true)
    expect(room.contains(panel)).toBe(false)
    expect(scroller.contains(room.querySelector('.room-header'))).toBe(false)
    expect(scroller.contains(composer)).toBe(false)
  })

  it('opens canonical response comparison on demand, excludes technical rows, and navigates without acknowledging the panel', async () => {
    const model_use = { connection_id: 11, model_id: 'historic-model', provider_kind: 'openai', watermark_seq: 1 }
    vi.mocked(eventsApi.list).mockImplementation(async (_roomId, after) => after === 0 ? [
      { ...event(1, 'Owner'), actor_type: 'user', actor_id: 1, event_id: 'request-1', payload: { content: 'Compare this request' } },
      { ...event(2, 'Ada before rename'), actor_id: 8, event_id: 'reply-2', payload: { content: 'Ada answer', trigger_event_id: 'request-1', round: 1, model_use } },
      { ...event(3, 'Tool'), actor_type: 'tool', actor_id: null, kind: 'tool_result', event_id: 'tool-3', payload: { content: 'secret technical output', trigger_event_id: 'request-1', round: 1, model_use } },
    ] as never : [])
    view()
    await screen.findAllByText('Ada answer')
    vi.mocked(readStatesApi.put).mockClear()
    await openRoomTools()
    const comparison = screen.getByText('Compare responses').closest('details') as HTMLDetailsElement
    expect(comparison.open).toBe(false)
    fireEvent.click(within(comparison).getByText('Compare responses'))
    expect(within(comparison).getByText('Ada before rename')).toBeTruthy()
    expect(within(comparison).queryByText('secret technical output')).toBeNull()
    expect(readStatesApi.put).not.toHaveBeenCalled()

    fireEvent.click(within(comparison).getByRole('button', { name: 'Go to response event 2' }))
    await waitFor(() => expect(document.querySelector('[data-authored-seq="2"]')?.classList.contains('conversation-event--target')).toBe(true))
    expect(readStatesApi.put).not.toHaveBeenCalled()
  })

  it('adds a saved handoff note to the auxiliary timeline without acknowledging unseen messages', async () => {
    const handoff = { ...event(3, 'System'), actor_type: 'system', actor_id: null, kind: 'summary', event_id: 'handoff-3',
      payload: { source_start_seq: 1, source_end_seq: 2, owner_checkpoint: true, content: 'Carry the rollout decision forward.' } }
    vi.mocked(summariesApi.create).mockResolvedValue(handoff as never)
    view()
    await screen.findByText('Reply 2')
    vi.mocked(readStatesApi.put).mockClear()
    await openRoomTools()
    const panel = screen.getByText('Handoff note').closest('details') as HTMLDetailsElement
    fireEvent.click(within(panel).getByText('Handoff note'))
    fireEvent.change(within(panel).getByRole('textbox', { name: 'Note for the next request' }),
      { target: { value: 'Carry the rollout decision forward.' } })
    fireEvent.click(within(panel).getByRole('button', { name: 'Save handoff note' }))
    await waitFor(() => expect(summariesApi.create).toHaveBeenCalledWith(501, {
      source_start_seq: 1, source_end_seq: 2, content: 'Carry the rollout decision forward.',
    }, expect.stringMatching(/^[0-9a-f-]{36}$/)))
    expect(screen.getAllByText('Handoff note').length).toBeGreaterThan(1)
    expect(roomEventCursor.afterSeq(501)).toBe(3)
    expect(readStatesApi.put).not.toHaveBeenCalled()
  })

  it('keeps an away-from-bottom position and jumps to acknowledge only the visible new authored message', async () => {
    vi.mocked(readStatesApi.get).mockResolvedValue({ last_read_seq: 2, updated_at: '2026-10-03T00:00:00Z' })
    const result = view()
    await screen.findByText('Reply 2')
    await waitFor(() => expect(readStatesApi.get).toHaveBeenCalledWith(501))
    const scroller = result.container.querySelector('.room-scroll') as HTMLElement
    Object.defineProperties(scroller, {
      scrollTop: { value: 100, writable: true, configurable: true },
      clientHeight: { value: 400, configurable: true },
      scrollHeight: { value: 1000, configurable: true },
    })
    scroller.getBoundingClientRect = () => ({ top: 0, bottom: 400, left: 0, right: 800, width: 800, height: 400, x: 0, y: 0, toJSON: () => ({}) })
    fireEvent.scroll(scroller)
    vi.mocked(Element.prototype.scrollIntoView).mockClear()
    await waitFor(() => expect(socket.onmessage).toBeTruthy())
    socket.onmessage!({ data: JSON.stringify({ type: 'event', ...event(3, 'Ada') }) })
    const latest = await screen.findByText('Reply 3')
    const latestArticle = latest.closest('[data-authored-seq]') as HTMLElement
    latestArticle.getBoundingClientRect = () => ({ top: 180, bottom: 260, left: 0, right: 800, width: 800, height: 80, x: 0, y: 180, toJSON: () => ({}) })
    expect(await screen.findByRole('button', { name: 'Jump to 1 new message' })).toBeTruthy()
    expect(Element.prototype.scrollIntoView).not.toHaveBeenCalled()
    expect(readStatesApi.put).not.toHaveBeenCalledWith(501, 3)

    vi.mocked(Element.prototype.scrollIntoView).mockImplementation(function (this: Element) {
      if (this === latestArticle || this.classList.contains('room-feed-end')) (scroller as HTMLElement & { scrollTop: number }).scrollTop = 600
    })
    fireEvent.click(screen.getByRole('button', { name: 'Jump to 1 new message' }))
    await waitFor(() => expect(readStatesApi.put).toHaveBeenCalledWith(501, 3))
  })

  it('does not acknowledge visible messages while the page is hidden and retries a failed acknowledgement', async () => {
    vi.mocked(readStatesApi.get).mockResolvedValue({ last_read_seq: 1, updated_at: '2026-10-03T00:00:00Z' })
    vi.mocked(readStatesApi.put).mockRejectedValue(new Error('offline'))
    const result = view()
    const message = await screen.findByText('Reply 2')
    const scroller = result.container.querySelector('.room-scroll') as HTMLElement
    const article = message.closest('[data-authored-seq]') as HTMLElement
    Object.defineProperties(scroller, {
      scrollTop: { value: 600, writable: true, configurable: true }, clientHeight: { value: 400, configurable: true },
      scrollHeight: { value: 1000, configurable: true },
    })
    scroller.getBoundingClientRect = () => ({ top: 0, bottom: 400, left: 0, right: 800, width: 800, height: 400, x: 0, y: 0, toJSON: () => ({}) })
    article.getBoundingClientRect = () => ({ top: 100, bottom: 180, left: 0, right: 800, width: 800, height: 80, x: 0, y: 100, toJSON: () => ({}) })
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true })
    fireEvent.scroll(scroller)
    expect(readStatesApi.put).not.toHaveBeenCalled()
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
    expect(await screen.findByText('Could not save your reading position.')).toBeTruthy()
    vi.mocked(readStatesApi.put).mockResolvedValue({ last_read_seq: 2, updated_at: '2026-10-03T00:00:01Z' })
    fireEvent.click(screen.getByRole('button', { name: 'Retry saving reading position' }))
    await waitFor(() => expect(readStatesApi.put).toHaveBeenLastCalledWith(501, 2))
    await waitFor(() => expect(screen.queryByText('Could not save your reading position.')).toBeNull())
  })

  it('does not acknowledge fetched messages while contiguous history is unavailable', async () => {
    vi.mocked(eventsApi.list).mockRejectedValue(new Error('offline'))
    const result = view()
    await screen.findByText('Live room connection interrupted. Retrying from the saved room cursor…')
    const scroller = result.container.querySelector('.room-scroll') as HTMLElement
    fireEvent.scroll(scroller)
    expect(readStatesApi.put).not.toHaveBeenCalled()
  })

  it('opens an exact approval target without acknowledging the request message', async () => {
    const actionId = '7d1e0e6c-9eeb-4571-864b-f2592c44db38'
    vi.mocked(toolsApi.activity).mockResolvedValue([{ action_id: actionId, room_id: 501, agent_id: 8, trigger_event_id: 'e1',
      family: 'browser_navigate', target: 'https://example.invalid', expected_effect: 'Navigate browser', state: 'waiting', scope: null,
      grant_id: null, requested_at: '2026-10-03T00:00:00Z', started_at: null, completed_at: null }])
    view(501, `?seq=1&action=${actionId}`)
    expect((await screen.findAllByText('Approval required · Ada')).length).toBeGreaterThan(0)
    await waitFor(() => expect(document.getElementById(`room-action-${actionId}`)?.classList.contains('attention-target')).toBe(true))
    expect(readStatesApi.put).not.toHaveBeenCalled()
  })

  it('opens the exact pending approval from an action-only inspector link', async () => {
    useAuthStore.getState().setAuth('synthetic-owner', { id: 1, email: 'owner@example.invalid', username: 'Owner' })
    vi.mocked(conversationsApi.get).mockResolvedValue({ ...room, user_id: 1 } as never)
    vi.mocked(projectsApi.get).mockResolvedValue({ id: 40, user_id: 1, name: 'Project', is_active: true } as never)
    api.defaults.adapter = (async (request) => {
      if (request.url === '/conversations/501/shared-binding') return { data: { kind: 'ordinary' }, status: 200, statusText: 'OK', headers: {}, config: request }
      throw new Error('No live network in this test')
    }) satisfies AxiosAdapter
    const targetId = '7d1e0e6c-9eeb-4571-864b-f2592c44db38'
    const otherId = '8d1e0e6c-9eeb-4571-864b-f2592c44db38'
    const action = { room_id: 501, agent_id: 8, trigger_event_id: 'e1', family: 'browser_navigate',
      target: 'https://example.invalid', expected_effect: 'Navigate browser', state: 'waiting' as const, scope: null,
      grant_id: null, requested_at: '2026-10-03T00:00:00Z', started_at: null, completed_at: null }
    vi.mocked(toolsApi.activity).mockResolvedValue([{ ...action, action_id: otherId }, { ...action, action_id: targetId }])
    view(501, `?action=${targetId}`)
    await waitFor(() => expect(document.getElementById(`room-action-${targetId}`)?.classList.contains('attention-target')).toBe(true))
    const target = document.getElementById(`room-action-${targetId}`)!
    const other = document.getElementById(`room-action-${otherId}`)!
    expect(target).toBeTruthy()
    expect(other).toBeTruthy()
    expect(other.classList.contains('attention-target')).toBe(false)
    expect(within(target).getByRole('article', { name: 'Approval for Ada' })).toBeTruthy()
    await waitFor(() => expect(target.scrollIntoView).toHaveBeenCalledWith({ block: 'center' }))
    expect(toolsApi.decide).not.toHaveBeenCalled()
  })

  it('highlights only the exact current failure and reports a stale attention link', async () => {
    const failed = { ...event(2, 'Ada'), event_id: 'failure-2', kind: 'failure' as const,
      payload: { code: 'provider_unavailable', message: 'private diagnostic excluded from inbox' } }
    vi.mocked(eventsApi.list).mockImplementation(async (_roomId, after) => after === 0 ? [
      { ...event(1, 'Owner'), actor_type: 'user' as const, actor_id: null }, failed,
    ] as never : [])
    const current = view(501, '?seq=2&failure=failure-2')
    await waitFor(() => expect(current.container.querySelector('#room-failure-failure-2')?.classList.contains('attention-target')).toBe(true))
    expect(readStatesApi.put).not.toHaveBeenCalled()
    cleanup()
    view(501, '?seq=2&failure=settled-failure')
    expect(await screen.findByText('This item changed. Refresh the inbox for its current state.')).toBeTruthy()
  })

  it('does not advance past earlier unread authored messages that were never visible', async () => {
    vi.mocked(readStatesApi.get).mockResolvedValue({ last_read_seq: 1, updated_at: '2026-10-03T00:00:00Z' })
    vi.mocked(eventsApi.list).mockImplementation(async (_roomId, after) => after === 0 ? [1, 2, 3, 4].map((seq) => event(seq, seq === 1 ? 'Seen' : `Unread ${seq}`)) as never : [])
    const result = view()
    await screen.findByText('Reply 4')
    const scroller = result.container.querySelector('.room-scroll') as HTMLElement
    Object.defineProperties(scroller, {
      scrollTop: { value: 600, writable: true, configurable: true }, clientHeight: { value: 400, configurable: true },
      scrollHeight: { value: 1000, configurable: true },
    })
    scroller.getBoundingClientRect = () => ({ top: 0, bottom: 400, left: 0, right: 800, width: 800, height: 400, x: 0, y: 0, toJSON: () => ({}) })
    for (const seq of [2, 3, 4]) {
      const article = result.container.querySelector(`[data-authored-seq="${seq}"]`) as HTMLElement
      article.getBoundingClientRect = () => ({ top: seq === 4 ? 100 : -300, bottom: seq === 4 ? 180 : -220,
        left: 0, right: 800, width: 800, height: 80, x: 0, y: seq === 4 ? 100 : -300, toJSON: () => ({}) })
    }
    fireEvent.scroll(scroller)
    expect(readStatesApi.put).not.toHaveBeenCalledWith(501, 4)
    expect(screen.getByRole('button', { name: 'Jump to 3 new messages' })).toBeTruthy()

    for (const seq of [2, 3, 4]) {
      const article = result.container.querySelector(`[data-authored-seq="${seq}"]`) as HTMLElement
      article.getBoundingClientRect = () => ({ top: seq * 70 - 100, bottom: seq * 70 - 30,
        left: 0, right: 800, width: 800, height: 70, x: 0, y: seq * 70 - 100, toJSON: () => ({}) })
    }
    fireEvent.scroll(scroller)
    await waitFor(() => expect(readStatesApi.put).toHaveBeenCalledWith(501, 4))
  })

  it('restores the first unread authored message after both history and room read state load', async () => {
    let finishRead!: (value: { last_read_seq: number; updated_at: string | null }) => void
    vi.mocked(readStatesApi.get).mockImplementationOnce(() => new Promise((resolve) => { finishRead = resolve }))
    view()
    const unread = (await screen.findByText('Reply 2')).closest('[data-authored-seq]') as HTMLElement
    unread.scrollIntoView = vi.fn()
    finishRead({ last_read_seq: 1, updated_at: '2026-10-03T00:00:00Z' })
    await waitFor(() => expect(unread.scrollIntoView).toHaveBeenCalledWith({ block: 'start' }))
  })

  it('ignores a late read-state response from the previous room', async () => {
    let finishOld!: (value: { last_read_seq: number; updated_at: string | null }) => void
    vi.mocked(conversationsApi.get).mockImplementation(async (id) => ({ ...room, id } as never))
    vi.mocked(readStatesApi.get).mockImplementation((id) => id === 501
      ? new Promise((resolve) => { finishOld = resolve })
      : Promise.resolve({ last_read_seq: 0, updated_at: null }))
    view(501)
    await waitFor(() => expect(finishOld).toBeTypeOf('function'))
    fireEvent.click(screen.getByRole('link', { name: 'Switch room' }))
    await waitFor(() => expect(readStatesApi.get).toHaveBeenCalledWith(602))
    finishOld({ last_read_seq: 999, updated_at: '2026-10-03T00:00:00Z' })
    await act(async () => {})
    expect(readStatesApi.put).not.toHaveBeenCalledWith(602, 999)
  })

  it('offers only active unpaused members from inline mention autocomplete', async () => {
    vi.mocked(conversationsApi.listMembers).mockResolvedValue([
      { room_id: 501, agent_id: 8, is_active: true, joined_at: '', paused: false },
      { room_id: 501, agent_id: 9, is_active: true, joined_at: '', paused: true },
    ] as never)
    view()
    const composer = await screen.findByRole('combobox', { name: 'Message' })
    fireEvent.focus(composer)
    fireEvent.change(composer, { target: { value: '@', selectionStart: 1 } })
    const suggestions = await screen.findByRole('listbox', { name: 'Initial response mentions' })
    expect(within(suggestions).getByRole('option', { name: 'Mention Ada' })).toBeTruthy()
    expect(within(suggestions).queryByRole('option', { name: 'Mention Bea' })).toBeNull()
    fireEvent.pointerDown(within(suggestions).getByRole('option', { name: 'Mention Ada' }))
    expect((composer as HTMLInputElement).value).toBe('@Ada ')
    await waitFor(() => expect(document.activeElement).toBe(composer))
    expect(screen.queryByRole('listbox')).toBeNull()
    fireEvent.change(composer, { target: { value: '@Ada Review this', selectionStart: 16 } })
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }))
    await waitFor(() => expect(eventsApi.send).toHaveBeenCalledWith(501, '@Ada Review this', expect.any(String)))
  })

  it('inserts a narrowed mention at the caret and supports keyboard selection and dismissal', async () => {
    view()
    const composer = await screen.findByRole('combobox', { name: 'Message' })
    fireEvent.focus(composer)
    fireEvent.change(composer, { target: { value: 'Please @Ad review', selectionStart: 10 } })
    expect(await screen.findByRole('option', { name: 'Mention Ada' })).toBeTruthy()
    fireEvent.keyDown(composer, { key: 'Enter' })
    expect((composer as HTMLInputElement).value).toBe('Please @Ada review')
    expect(eventsApi.send).not.toHaveBeenCalled()
    await waitFor(() => expect(document.activeElement).toBe(composer))
    fireEvent.select(composer)
    expect(screen.queryByRole('listbox')).toBeNull()

    fireEvent.change(composer, { target: { value: '@', selectionStart: 1 } })
    await screen.findByRole('listbox')
    vi.mocked(Element.prototype.scrollIntoView).mockClear()
    fireEvent.keyDown(composer, { key: 'ArrowDown' })
    await waitFor(() => expect(screen.getByRole('option', { name: 'Mention Bea' }).getAttribute('aria-selected')).toBe('true'))
    expect(Element.prototype.scrollIntoView).toHaveBeenCalledWith({ block: 'nearest' })
    fireEvent.keyDown(composer, { key: 'Tab' })
    expect((composer as HTMLInputElement).value).toBe('@Bea ')
    fireEvent.change(composer, { target: { value: '@', selectionStart: 1 } })
    expect(await screen.findByRole('listbox')).toBeTruthy()
    fireEvent.keyDown(composer, { key: 'Escape' })
    expect(screen.queryByRole('listbox')).toBeNull()
  })

  it('uses a multiline composer and sends only with Ctrl or Command Enter', async () => {
    view()
    const composer = await screen.findByRole('combobox', { name: 'Message' })
    expect(composer.tagName).toBe('TEXTAREA')
    fireEvent.change(composer, { target: { value: 'First line\nSecond line', selectionStart: 22 } })
    fireEvent.keyDown(composer, { key: 'Enter' })
    expect(eventsApi.send).not.toHaveBeenCalled()
    fireEvent.keyDown(composer, { key: 'Enter', ctrlKey: true })
    await waitFor(() => expect(eventsApi.send).toHaveBeenCalledWith(501, 'First line\nSecond line', expect.any(String)))

    fireEvent.change(composer, { target: { value: 'Command send', selectionStart: 12 } })
    fireEvent.keyDown(composer, { key: 'Enter', metaKey: true })
    await waitFor(() => expect(eventsApi.send).toHaveBeenCalledWith(501, 'Command send', expect.any(String)))
    expect(screen.getByText('Enter for a new line · Ctrl or Command + Enter to send')).toBeTruthy()
    const routingHelp = screen.getByText('How mentions work').closest('details') as HTMLDetailsElement
    expect(routingHelp.open).toBe(false)
    fireEvent.click(screen.getByText('How mentions work'))
    expect(routingHelp.open).toBe(true)
    expect(screen.getByText(/Mentions guide the initial response/)).toBeTruthy()
  })

  it('restores the owner room draft and saves edits through the canonical draft API', async () => {
    vi.mocked(draftsApi.get).mockResolvedValue({ content: 'Restored\ndraft', version: 7,
      idempotency_key: 'demo-key', updated_at: '2026-10-03T00:00:00Z' })
    view()
    const composer = await screen.findByRole('combobox', { name: 'Message' })
    await waitFor(() => expect((composer as HTMLTextAreaElement).value).toBe('Restored\ndraft'))
    expect(draftsApi.get).toHaveBeenCalledWith(501)
    fireEvent.change(composer, { target: { value: 'Edited draft', selectionStart: 12 } })
    await waitFor(() => expect(draftsApi.put).toHaveBeenCalledWith(501, 'Edited draft', 7), { timeout: 1500 })
  })

  it('persists a return to the loaded draft while the preceding save is still in flight', async () => {
    let finishFirstSave!: (value: { content: string; version: number; idempotency_key: string; updated_at: string }) => void
    vi.mocked(draftsApi.put).mockImplementation((_roomId, content, expectedVersion) => {
      if (content === 'First edit') return new Promise((resolve) => { finishFirstSave = resolve })
      return Promise.resolve({ content, version: expectedVersion + 1, idempotency_key: 'returned-draft', updated_at: '2026-10-03T00:00:01Z' })
    })
    view()
    const composer = await screen.findByRole('combobox', { name: 'Message' })
    await waitFor(() => expect(draftsApi.get).toHaveBeenCalledWith(501))
    fireEvent.change(composer, { target: { value: 'First edit', selectionStart: 10 } })
    await waitFor(() => expect(draftsApi.put).toHaveBeenCalledWith(501, 'First edit', 0), { timeout: 1500 })
    fireEvent.change(composer, { target: { value: '', selectionStart: 0 } })
    await act(async () => { finishFirstSave({ content: 'First edit', version: 1,
      idempotency_key: 'first-edit', updated_at: '2026-10-03T00:00:00Z' }) })
    expect((composer as HTMLTextAreaElement).value).toBe('')
    await waitFor(() => expect(draftsApi.put).toHaveBeenLastCalledWith(501, '', 1), { timeout: 1500 })
  })

  it('preserves local text and requires an explicit choice after a draft version conflict', async () => {
    vi.mocked(draftsApi.get).mockResolvedValueOnce({ content: '', version: 1, idempotency_key: null, updated_at: null })
      .mockResolvedValueOnce({ content: 'Saved elsewhere', version: 2, idempotency_key: 'remote-key', updated_at: '2026-10-03T00:00:00Z' })
    vi.mocked(draftsApi.put).mockRejectedValueOnce({ response: { status: 409 } }).mockResolvedValueOnce({
      content: 'My local draft', version: 3, idempotency_key: 'local-key', updated_at: '2026-10-03T00:00:01Z',
    })
    view()
    const composer = await screen.findByRole('combobox', { name: 'Message' })
    await waitFor(() => expect(draftsApi.get).toHaveBeenCalledWith(501))
    fireEvent.change(composer, { target: { value: 'My local draft', selectionStart: 14 } })
    expect(await screen.findByText('This draft changed in another window. Choose which version to keep.')).toBeTruthy()
    expect((composer as HTMLTextAreaElement).value).toBe('My local draft')
    expect(draftsApi.put).toHaveBeenCalledTimes(1)
    expect((screen.getByRole('button', { name: 'Send message' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Save my draft' }))
    await waitFor(() => expect(draftsApi.put).toHaveBeenLastCalledWith(501, 'My local draft', 2))
    await waitFor(() => expect(screen.queryByText('This draft changed in another window. Choose which version to keep.')).toBeNull())
  })

  it('keeps local text visible and offers retry when draft saving is unavailable', async () => {
    vi.mocked(draftsApi.put).mockRejectedValueOnce(new Error('offline'))
    view()
    const composer = await screen.findByRole('combobox', { name: 'Message' })
    await waitFor(() => expect(draftsApi.get).toHaveBeenCalledWith(501))
    fireEvent.change(composer, { target: { value: 'Keep locally', selectionStart: 12 } })
    expect(await screen.findByText('Draft could not be saved. It remains here while this room is open.')).toBeTruthy()
    expect((composer as HTMLTextAreaElement).value).toBe('Keep locally')
    fireEvent.click(screen.getByRole('button', { name: 'Retry saving draft' }))
    await waitFor(() => expect(draftsApi.put).toHaveBeenLastCalledWith(501, 'Keep locally', 0))
    await waitFor(() => expect(screen.queryByText('Draft could not be saved. It remains here while this room is open.')).toBeNull())
  })

  it('uses the server draft key and preserves a newer edit when send confirmation arrives late', async () => {
    let finishSend!: (value: never) => void
    vi.mocked(eventsApi.send).mockImplementationOnce(() => new Promise((resolve) => { finishSend = resolve }))
    vi.mocked(draftsApi.put).mockImplementation(async (_roomId, content, expectedVersion) => ({ content,
      version: expectedVersion + 1, idempotency_key: `server-key-${content}`, updated_at: '2026-10-03T00:00:00Z' }))
    view()
    const composer = await screen.findByRole('combobox', { name: 'Message' })
    await waitFor(() => expect(draftsApi.get).toHaveBeenCalledWith(501))
    fireEvent.change(composer, { target: { value: 'Send this', selectionStart: 9 } })
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }))
    await waitFor(() => expect(eventsApi.send).toHaveBeenCalledWith(501, 'Send this', 'server-key-Send this'))
    fireEvent.change(composer, { target: { value: 'Send this\n', selectionStart: 10 } })
    finishSend({ ...event(3, 'Me'), actor_type: 'user', payload: { content: 'Send this' } } as never)
    await waitFor(() => expect(draftsApi.clear).toHaveBeenCalledWith(501, 1))
    expect((composer as HTMLTextAreaElement).value).toBe('Send this\n')
    await waitFor(() => expect(draftsApi.put).toHaveBeenLastCalledWith(501, 'Send this\n', 2))
  })

  it('keeps mention selection ahead of the multiline send shortcut', async () => {
    view()
    const composer = await screen.findByRole('combobox', { name: 'Message' })
    fireEvent.change(composer, { target: { value: '@Ad', selectionStart: 3 } })
    await screen.findByRole('option', { name: 'Mention Ada' })
    fireEvent.keyDown(composer, { key: 'Enter', ctrlKey: true })
    expect((composer as HTMLTextAreaElement).value).toBe('@Ada ')
    expect(eventsApi.send).not.toHaveBeenCalled()
  })

  it('does not select or send on IME Enter and fails closed for an unmatched mention', async () => {
    view()
    const composer = await screen.findByRole('combobox', { name: 'Message' })
    fireEvent.change(composer, { target: { value: '@없는', selectionStart: 3 } })
    expect(await screen.findByText('This mention does not match an active member. Remove it or choose a member before sending.')).toBeTruthy()
    fireEvent.keyDown(composer, { key: 'Enter', isComposing: true })
    expect(eventsApi.send).not.toHaveBeenCalled()
    expect((composer as HTMLInputElement).value).toBe('@없는')
    expect((screen.getByRole('button', { name: 'Send message' }) as HTMLButtonElement).disabled).toBe(true)

    fireEvent.change(composer, { target: { value: '@', selectionStart: 1 } })
    expect(await screen.findByRole('listbox')).toBeTruthy()
    expect(fireEvent.keyDown(composer, { key: 'ArrowDown', isComposing: true })).toBe(true)
    expect(screen.getByRole('option', { name: 'Mention Ada' }).getAttribute('aria-selected')).toBe('true')

    fireEvent.change(composer, { target: { value: 'Normal message', selectionStart: 14 } })
    expect(fireEvent.keyDown(composer, { key: 'Enter' })).toBe(true)
  })

  it('hides all room tools behind one compact conversation-options control by default', async () => {
    view()
    await screen.findByText('Reply 2')
    const trigger = screen.getByRole('button', { name: 'Conversation options' })
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByRole('combobox', { name: 'Agent to add' })).toBeNull()
    expect(screen.queryByRole('searchbox', { name: 'Search conversation' })).toBeNull()
    await openRoomTools()
    expect(trigger.getAttribute('aria-expanded')).toBe('true')
    const tools = screen.getByText('Search and export').closest('details') as HTMLDetailsElement
    expect(tools.open).toBe(false)
    expect(tools.hasAttribute('open')).toBe(false)
    expect(screen.getByText('Room options')).toBeTruthy()
    expect(screen.getByText('Compare responses')).toBeTruthy()
    expect(screen.getAllByText('Handoff note').length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: 'Mention Ada' })).toBeNull()
  })

  it('copies a current event link accepted by the room switcher parser', async () => {
    useAuthStore.getState().setAuth('owner', { id: 1, email: 'owner@example.com', username: 'owner' })
    vi.mocked(conversationsApi.get).mockResolvedValue({ ...room, user_id: 1 } as never)
    const rects = vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue({ length: 1 } as DOMRectList)
    try {
      vi.mocked(eventsApi.list).mockImplementation(async (_roomId, after) => after === 0 ? [
        { ...event(1, 'DemoUser'), event_id: '929d363e-c8b1-466c-a4fc-f7e24a3adb8c', actor_type: 'user', actor_id: 1, payload: { content: 'Original message' } },
      ] as never : [])
      const writeText = vi.fn().mockResolvedValue(undefined)
      vi.stubGlobal('navigator', Object.assign(Object.create(navigator), { clipboard: { writeText } }))
      view()
      const article = (await screen.findByText('Original message')).closest('article')!
      fireEvent.click(within(article).getByRole('button', { name: 'Message actions for DemoUser' }))
      fireEvent.click(screen.getByRole('menuitem', { name: 'Copy link to message' }))
      const href = '/chat/501?seq=1&event=929d363e-c8b1-466c-a4fc-f7e24a3adb8c'
      await waitFor(() => expect(writeText).toHaveBeenCalledWith(href))
      expect(readPastedChatReference(String(writeText.mock.calls.at(-1)?.[0]))).toMatchObject({ state: 'valid', kind: 'event', room: 501, seq: 1 })
    } finally { rects.mockRestore(); useAuthStore.getState().logout() }
  })

  it('copies exact authored text and a sequence permalink, then quotes at the composer caret', async () => {
    vi.mocked(eventsApi.list).mockImplementation(async (_roomId, after) => after === 0 ? [
      { ...event(1, 'DemoUser'), event_id: '929d363e-c8b1-466c-a4fc-f7e24a3adb8c', actor_type: 'user', payload: { content: 'First line\n@Other quoted name' } },
      { ...event(2, 'Tool'), actor_type: 'tool', kind: 'tool_result', payload: { text: 'private tool output' } },
    ] as never : [])
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', Object.assign(Object.create(navigator), { clipboard: { writeText } }))
    view()
    const authored = (await screen.findByText((_text, element) => element?.classList.contains('conversation-event__body') === true
      && element.textContent === 'First line\n@Other quoted name')).closest('article')!
    await waitFor(() => expect(draftsApi.get).toHaveBeenCalledWith(501))
    await act(async () => { await new Promise((resolve) => window.setTimeout(resolve, 550)) })
    expect(draftsApi.put).not.toHaveBeenCalled()
    fireEvent.click(within(authored).getByRole('button', { name: 'Message actions for DemoUser' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Copy message' }))
    await waitFor(() => expect(writeText).toHaveBeenLastCalledWith('First line\n@Other quoted name'))
    expect(document.activeElement).toBe(within(authored).getByRole('button', { name: 'Message actions for DemoUser' }))
    expect(await within(authored).findByText('Message copied.')).toBeTruthy()

    fireEvent.click(within(authored).getByRole('button', { name: 'Message actions for DemoUser' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Copy link to message' }))
    await waitFor(() => expect(writeText).toHaveBeenLastCalledWith('/chat/501?seq=1&event=929d363e-c8b1-466c-a4fc-f7e24a3adb8c'))
    expect(readPastedChatReference(String(writeText.mock.calls.at(-1)?.[0]))).toMatchObject({ state: 'valid', kind: 'event', room: 501, seq: 1 })
    expect(await within(authored).findByText('Message link copied.')).toBeTruthy()

    const composer = screen.getByRole('combobox', { name: 'Message' }) as HTMLTextAreaElement
    fireEvent.change(composer, { target: { value: 'Before after', selectionStart: 7, selectionEnd: 7 } })
    fireEvent.select(composer, { target: { selectionStart: 7, selectionEnd: 7 } })
    fireEvent.click(within(authored).getByRole('button', { name: 'Message actions for DemoUser' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Quote message' }))
    expect(composer.value).toBe('Before \n> First line\n> @Other quoted name\n\nafter')
    expect(screen.queryByText('This mention does not match an active member. Remove it or choose a member before sending.')).toBeNull()
    await waitFor(() => expect(draftsApi.put).toHaveBeenCalledWith(501, composer.value, 0), { timeout: 1500 })
    expect(within(screen.getByText('private tool output').closest('article')!).queryByRole('button', { name: /Message actions/ })).toBeNull()
  })

  it('copies and quotes the visible Agent answer while preserving its canonical event', async () => {
    const raw = '<think>\n\nPrivate reasoning\n\n</think>\n\nVisible answer'
    const original = { ...event(1, 'Ada'), payload: { content: raw } }
    vi.mocked(eventsApi.list).mockImplementation(async (_roomId, after) => after === 0 ? [original] as never : [])
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', Object.assign(Object.create(navigator), { clipboard: { writeText } }))
    view()
    const authored = (await screen.findByText('Visible answer')).closest('article')!
    expect(screen.queryByText('Private reasoning')).toBeNull()
    fireEvent.click(within(authored).getByRole('button', { name: 'Message actions for Ada' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Copy message' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledOnce())
    expect(String(writeText.mock.calls[0][0]).trim()).toBe('Visible answer')
    fireEvent.click(within(authored).getByRole('button', { name: 'Message actions for Ada' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Quote message' }))
    const composer = screen.getByRole('combobox', { name: 'Message' }) as HTMLTextAreaElement
    expect(composer.value).toContain('> Visible answer')
    expect(composer.value).not.toContain('Private reasoning')
    expect(original.payload.content).toBe(raw)
  })

  it('reports a message clipboard failure without claiming success', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('blocked'))
    vi.stubGlobal('navigator', Object.assign(Object.create(navigator), { clipboard: { writeText } }))
    view()
    const authored = (await screen.findByText('Reply 1')).closest('article')!
    expect(screen.queryByRole('menuitem', { name: 'Copy message' })).toBeNull()
    fireEvent.click(within(authored).getByRole('button', { name: 'Message actions for Ada' }))
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('menuitem', { name: 'Copy message' })).toBeNull()
    fireEvent.click(within(authored).getByRole('button', { name: 'Message actions for Ada' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Copy message' }))
    expect((await within(authored).findByRole('alert')).textContent).toBe('Could not copy. Try again.')
    expect(within(authored).queryByText('Message copied.')).toBeNull()
  })

  it('opens the inspector only from an authored user request without dispatching or acknowledging', async () => {
    const triggerId = '11111111-1111-4111-8111-111111111111'
    vi.mocked(eventsApi.list).mockImplementation(async (_roomId, after) => after === 0 ? [{ ...event(1, 'DemoUser'),
      event_id: triggerId, actor_type: 'user', actor_id: null, payload: { content: 'Inspect this request' } }] as never : [])
    view()
    const authored = (await screen.findByText('Inspect this request')).closest('article')!
    fireEvent.click(within(authored).getByRole('button', { name: 'Message actions for DemoUser' }))
    const inspectorButton = screen.getByRole('menuitem', { name: 'View request progress' })
    fireEvent.click(inspectorButton)
    expect(await screen.findByRole('dialog', { name: 'Request progress' })).toBeTruthy()
    expect(requestInspectorApi.get).toHaveBeenCalledWith(501, triggerId)
    expect(eventsApi.send).not.toHaveBeenCalled()
    expect(readStatesApi.put).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(document.activeElement).toBe(within(authored).getByRole('button', { name: 'Message actions for DemoUser' })))
  })

  it('labels an exact finalizer response and copies only its authored content', async () => {
    const finalizeId = '11111111-1111-4111-8111-111111111111'
    const responseId = '22222222-2222-4222-8222-222222222222'
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', Object.assign(Object.create(navigator), { clipboard: { writeText } }))
    vi.mocked(eventsApi.list).mockImplementation(async (_roomId, after) => after === 0 ? [
      { ...event(1, 'DemoUser'), event_id: finalizeId, actor_type: 'user', actor_id: null,
        payload: { content: 'Create the final result.', finalize: { version: 1, source_request_event_id: '33333333-3333-4333-8333-333333333333', source_outcome_event_ids: [], agent_id: 8 } } },
      { ...event(2, 'Ada'), event_id: responseId, actor_id: 8,
        payload: { content: 'The exact authored final answer.', trigger_event_id: finalizeId } },
      { ...event(3, 'Bea'), actor_id: 9, payload: { content: 'A mismatched response.', trigger_event_id: finalizeId } },
    ] as never : [])
    view()
    const answer = (await screen.findByText('The exact authored final answer.')).closest('article')!
    expect(within(answer).getByText('Final result')).toBeTruthy()
    expect(within(screen.getByText('A mismatched response.').closest('article')!).queryByText('Final result')).toBeNull()
    fireEvent.click(within(answer).getByRole('button', { name: 'Message actions for Ada' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Copy message' }))
    expect(writeText).toHaveBeenCalledWith('The exact authored final answer.')
    expect(eventsApi.send).not.toHaveBeenCalled()
  })

  it('keeps an ambiguous finalizer key when the inspector closes and reopens', async () => {
    act(() => useAuthStore.getState().setAuth('owner', { id: 1, email: 'owner@example.com', username: 'owner' }))
    const triggerId = '11111111-1111-4111-8111-111111111111'
    vi.mocked(eventsApi.list).mockImplementation(async (_roomId, after) => after === 0 ? [{ ...event(1, 'DemoUser'),
      event_id: triggerId, actor_type: 'user', actor_id: null, payload: { content: 'Inspect this request' } }] as never : [])
    vi.mocked(requestFinalizeApi.preview).mockResolvedValue({ version: 1, room_id: 501,
      source_request: { event_id: triggerId, seq: 1, preview: 'Inspect this request', href: '/chat/501?seq=1' },
      source_completion: { state: 'completed', outcome_status: 'succeeded', result_available: true }, outcomes: [],
      outcome_count: 1, outcomes_truncated: false, excluded_agents: [], agent: { agent_id: 8, name_snapshot: 'Ada', current_name: 'Ada' },
      current_model: { connection_id: 1, model_id: 'exact-one', provider_kind: 'openai' },
      limits: { max_rounds: 1, max_tool_calls: 0, max_executor_seconds: 60 },
      source_coverage: { source_request_event_id: triggerId, source_outcome_event_ids: [], included_message_event_ids: [],
        omitted_event_ids: [], truncations: [], used_chars: 100, limit_chars: 1000 }, eligible: true, blocked_reason: null, plan_hash: 'a'.repeat(64) })
    vi.mocked(requestFinalizeApi.execute).mockRejectedValueOnce(new Error('lost response')).mockRejectedValueOnce(new Error('still unconfirmed')).mockResolvedValue({ version: 1, room_id: 501,
      source_request_event_id: triggerId, source_outcome_event_ids: [], finalize_event_id: '22222222-2222-4222-8222-222222222222',
      finalize_seq: 2, agent_id: 8, replayed: true, href: '/chat/501?seq=2' })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    view(501, '', queryClient)
    const sourceRow = (await screen.findByText('Inspect this request')).closest('article')!
    const openInspector = () => {
      if (!screen.queryByRole('menuitem', { name: 'View request progress' })) {
        fireEvent.click(within(sourceRow).getByRole('button', { name: 'Message actions for DemoUser' }))
      }
      fireEvent.click(screen.getByRole('menuitem', { name: 'View request progress' }))
    }
    openInspector()
    fireEvent.click(await screen.findByRole('button', { name: 'Prepare final result' }))
    await waitFor(() => expect((screen.getByLabelText('Agent for final result') as HTMLSelectElement).disabled).toBe(false))
    fireEvent.click(screen.getByRole('button', { name: 'Review final result plan' }))
    await screen.findByText('Completed discussion')
    fireEvent.click(screen.getByRole('button', { name: 'Start final result' }))
    await screen.findByText(/could not be confirmed/)
    const key = vi.mocked(requestFinalizeApi.execute).mock.calls[0][3]
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    vi.mocked(projectsApi.get).mockRejectedValueOnce(new Error('temporary project read failure'))
    await queryClient.invalidateQueries({ queryKey: ['project'] })
    await waitFor(() => expect(screen.queryByRole('combobox', { name: 'Message' })).toBeNull())
    await queryClient.invalidateQueries({ queryKey: ['project'] })
    await screen.findByRole('combobox', { name: 'Message' })
    openInspector()
    const reopened = await screen.findByRole('dialog')
    fireEvent.click(await within(reopened).findByRole('button', { name: 'Prepare final result' }))
    await waitFor(() => expect((within(reopened).getByLabelText('Agent for final result') as HTMLSelectElement).disabled).toBe(false))
    fireEvent.click(within(reopened).getByRole('button', { name: 'Review final result plan' }))
    await within(reopened).findByText('Completed discussion')
    fireEvent.click(within(reopened).getByRole('button', { name: 'Start final result' }))
    await waitFor(() => expect(requestFinalizeApi.execute).toHaveBeenCalledTimes(2))
    expect(vi.mocked(requestFinalizeApi.execute).mock.calls[1][3]).toBe(key)
    await within(reopened).findByText(/could not be confirmed/)
    fireEvent.click(within(reopened).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    vi.mocked(projectsApi.get).mockResolvedValueOnce({ id: 40, name: 'Project', is_active: false } as never)
    await queryClient.invalidateQueries({ queryKey: ['project'] })
    await waitFor(() => expect(screen.queryByRole('combobox', { name: 'Message' })).toBeNull())
    await queryClient.invalidateQueries({ queryKey: ['project'] })
    await screen.findByRole('combobox', { name: 'Message' })
    openInspector()
    const afterConfirmedArchive = await screen.findByRole('dialog')
    fireEvent.click(await within(afterConfirmedArchive).findByRole('button', { name: 'Prepare final result' }))
    await waitFor(() => expect((within(afterConfirmedArchive).getByLabelText('Agent for final result') as HTMLSelectElement).disabled).toBe(false))
    fireEvent.click(within(afterConfirmedArchive).getByRole('button', { name: 'Review final result plan' }))
    await within(afterConfirmedArchive).findByText('Completed discussion')
    fireEvent.click(within(afterConfirmedArchive).getByRole('button', { name: 'Start final result' }))
    await waitFor(() => expect(requestFinalizeApi.execute).toHaveBeenCalledTimes(3))
    expect(vi.mocked(requestFinalizeApi.execute).mock.calls[2][3]).not.toBe(key)
    expect(eventsApi.send).not.toHaveBeenCalled()
    act(() => useAuthStore.getState().logout())
  })

  it('refreshes canonical room history after one explicit targeted retry without sending composer content', async () => {
    act(() => useAuthStore.getState().setAuth('owner', { id: 1, email: 'owner@example.com', username: 'owner' }))
    const triggerId = '11111111-1111-4111-8111-111111111111'
    const failureId = '33333333-3333-4333-8333-333333333333'
    let started = false
    const initial = [{ ...event(1, 'DemoUser'), event_id: triggerId, actor_type: 'user', actor_id: null, payload: { content: 'Inspect this request' } },
      { ...event(2, 'Ada'), event_id: failureId, kind: 'failure', payload: { code: 'tool_failed' } }]
    const retried = { ...event(3, 'DemoUser'), event_id: '55555555-5555-4555-8555-555555555555', actor_type: 'user', actor_id: null,
      payload: { content: 'Retrying Ada for the original request', source_failure_event_id: failureId } }
    vi.mocked(eventsApi.list).mockImplementation(async (_roomId, after) => after === 0 ? initial as never : after === 2 && started ? [retried] as never : [])
    const sourceInspection: RequestInspectorResponse = { version: 1, room_id: 501, room_title: 'Topic A', read_only: false,
      request: { event_id: triggerId, seq: 1, created_at: '2026-10-05T00:00:00Z', actor_name_snapshot: 'DemoUser', preview: 'Inspect this request', href: '/chat/501?seq=1' },
      execution: { phase: 'completed', selected_agents: [{ agent_id: 8, name_snapshot: 'Ada', live_state: null }], selected_agent_count: 1,
        policy: { max_rounds: 2, max_tool_calls: 8, max_executor_seconds: 60 }, current_round: 1 },
      completion: { state: 'completed', outcome_status: 'failed', result_available: false, completed_rounds: 1, pending_agent_count: 0 },
      outcomes: [{ event_id: failureId, seq: 2, round: 1, agent_id: 8, agent_name_snapshot: 'Ada', kind: 'failure', phase: 'failed',
        summary_code: 'failure_tool', model_use: null, href: '/chat/501?seq=2' }], outcome_count: 1, outcomes_truncated: false,
      actions: [], action_count: 0, actions_truncated: false }
    const retryInspection: RequestInspectorResponse = { version: 1, room_id: 501, room_title: 'Topic A', read_only: false,
      request: { event_id: retried.event_id, seq: 3, created_at: retried.created_at, actor_name_snapshot: 'DemoUser',
        preview: 'Retrying Ada for the original request', href: '/chat/501?seq=3' },
      execution: { phase: 'running', selected_agents: [{ agent_id: 8, name_snapshot: 'Ada', live_state: 'running' as const }], selected_agent_count: 1,
        policy: { max_rounds: 1, max_tool_calls: 8, max_executor_seconds: 60 }, current_round: 1 },
      completion: { state: 'running', outcome_status: 'pending', result_available: false, completed_rounds: 0, pending_agent_count: 1 },
      outcomes: [], outcome_count: 0, outcomes_truncated: false, actions: [], action_count: 0, actions_truncated: false }
    vi.mocked(requestInspectorApi.get).mockImplementation(async (_roomId, eventId) => eventId === retried.event_id ? retryInspection : sourceInspection)
    vi.mocked(requestRetriesApi.preflight).mockResolvedValue({ version: 1, room_id: 501, failure_event_id: failureId, failure_seq: 2,
      source_request: { event_id: triggerId, seq: 1, preview: 'Inspect this request', href: '/chat/501?seq=1' },
      agent: { agent_id: 8, name_snapshot: 'Ada', current_name: 'Ada' }, current_model: { connection_id: 1, model_id: 'exact-one', provider_kind: 'openai' },
      limits: { max_rounds: 1, max_tool_calls: 8, max_executor_seconds: 60 }, eligible: true, blocked_reason: null, plan_hash: 'a'.repeat(64) })
    vi.mocked(requestRetriesApi.start).mockImplementation(async () => { started = true; return { version: 1, room_id: 501,
      source_failure_event_id: failureId, source_trigger_event_id: triggerId, source_request_event_id: triggerId,
      retry_event_id: retried.event_id, retry_seq: 3, agent_id: 8, replayed: false, href: '/chat/501?seq=3' } })
    view()
    const authored = (await screen.findByText('Inspect this request')).closest('article')!
    fireEvent.click(within(authored).getByRole('button', { name: 'Message actions for DemoUser' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'View request progress' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Retry only this agent' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Start retry' }))
    expect(await screen.findByText('A new attempt was added for Ada.')).toBeTruthy()
    expect(await screen.findByText('Retrying Ada for the original request')).toBeTruthy()
    expect(eventsApi.send).not.toHaveBeenCalled()
    expect(requestRetriesApi.start).toHaveBeenCalledWith(501, failureId, 'a'.repeat(64), expect.any(String))
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    const retryMessage = screen.getByText('Retrying Ada for the original request').closest('article')!
    fireEvent.click(within(retryMessage).getByRole('button', { name: 'Message actions for DemoUser' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'View request progress' }))
    const retryDialog = await screen.findByRole('dialog', { name: 'Request progress' })
    expect(within(retryDialog).getByText('Ada')).toBeTruthy()
    fireEvent.click(within(retryDialog).getByText('Settings used for this request'))
    expect(within(retryDialog).getByText('1')).toBeTruthy()
    expect(within(retryDialog).getByText('8')).toBeTruthy()
    expect(within(retryDialog).getByText('60')).toBeTruthy()
    expect(within(retryDialog).queryByText('Could not load request progress.')).toBeNull()
    expect(requestInspectorApi.get).toHaveBeenCalledWith(501, retried.event_id)
    act(() => useAuthStore.getState().logout())
  })

  it('loads, scrolls to, and highlights an authored sequence without acknowledging unseen messages', async () => {
    const history = Array.from({ length: 101 }, (_, index) => ({ ...event(index + 1, 'Ada'), payload: { content: `Message ${index + 1}` } }))
    vi.mocked(eventsApi.list).mockImplementation(async (_roomId, after) => after === 0 ? history.slice(0, 100) as never
      : after === 100 ? history.slice(100) as never : [])
    view(501, '?seq=101')
    const target = (await screen.findByText('Message 101')).closest('article')!
    await waitFor(() => expect(target.classList.contains('conversation-event--target')).toBe(true))
    expect(target.scrollIntoView).toHaveBeenCalledWith({ block: 'center' })
    expect(eventsApi.list).toHaveBeenCalledWith(501, 100, 100)
    expect(readStatesApi.put).not.toHaveBeenCalled()
  })

  it('scrolls to an exact auxiliary sequence without falsely marking authored messages read', async () => {
    vi.mocked(eventsApi.list).mockImplementation(async (_roomId, after) => after === 0 ? [
      { ...event(1, 'Tool'), actor_type: 'tool', kind: 'tool_result', payload: { text: 'technical' } },
    ] as never : [])
    view(501, '?seq=1')
    const target = (await screen.findByText('technical')).closest('article')!
    await waitFor(() => expect(target.classList.contains('attention-target')).toBe(true))
    expect(target.scrollIntoView).toHaveBeenCalledWith({ block: 'center' })
    expect(screen.queryByText('This message is not available in the room history.')).toBeNull()
    expect(readStatesApi.put).not.toHaveBeenCalled()
  })

  it('opens a historical failure by exact sequence after a newer request without an attention-stale claim', async () => {
    const failureId = '44444444-4444-4444-8444-444444444444'
    vi.mocked(eventsApi.list).mockImplementation(async (_roomId, after) => after === 0 ? [
      { ...event(1, 'DemoUser'), actor_type: 'user', actor_id: null, payload: { content: 'Earlier request' } },
      { ...event(2, 'Ada'), event_id: failureId, kind: 'failure', payload: { code: 'provider_unavailable', trigger_event_id: 'e1' } },
      { ...event(3, 'DemoUser'), actor_type: 'user', actor_id: null, payload: { content: 'Newer request' } },
    ] as never : [])
    view(501, '?seq=2')
    const target = (await screen.findByText('Could not reach the model provider. Check the connection, then try again.')).closest('article')!
    await waitFor(() => expect(target.classList.contains('attention-target')).toBe(true))
    expect(screen.queryByText('This item changed. Refresh the inbox for its current state.')).toBeNull()
    expect(readStatesApi.put).not.toHaveBeenCalled()
  })

  it('reports a missing sequence without falsely marking it read', async () => {
    vi.mocked(eventsApi.list).mockResolvedValue([])
    view(501, '?seq=99')
    expect(await screen.findByText('This message is not available in the room history.')).toBeTruthy()
    expect(readStatesApi.put).not.toHaveBeenCalled()
  })

  it('shows compact multi-provider readiness and keeps exact model details on demand', async () => {
    view()
    await screen.findByText('Reply 2')
    await openRoomTools()
    const rail = screen.getByRole('region', { name: 'Room members' })
    const ada = (await within(rail).findByText('Ada')).closest('.member-row') as HTMLElement
    const details = (await within(ada).findByText('Provider and model')).closest('details')!
    expect(details.open).toBe(false)
    fireEvent.click(within(details).getByText('Provider and model'))
    expect(within(details).getByText('Ready')).toBeTruthy()
    expect(within(details).getByText(/Primary OpenAI · OpenAI/)).toBeTruthy()
    expect(within(details).getByText('exact-one')).toBeTruthy()
    const bea = within(rail).getByText('Bea').closest('.member-row') as HTMLElement
    const beaDetails = within(bea).getByText('Provider and model').closest('details')!
    expect(beaDetails.open).toBe(false)
    fireEvent.click(within(beaDetails).getByText('Provider and model'))
    expect(within(bea).getByText('Model not verified')).toBeTruthy()
  })

  it('searches complete authored history and exports without technical events', async () => {
    vi.mocked(eventsApi.list).mockImplementation(async (_roomId, after) => after === 0 ? [
      { ...event(1, 'DemoUser'), actor_type: 'user', payload: { content: '한국어 요청' } },
      { ...event(2, 'Ada'), payload: { content: 'Visible answer' } },
      { ...event(3, 'Tool'), actor_type: 'tool', kind: 'tool_result', payload: { text: 'private output', arguments: { token: 'secret' } } },
    ] as never : [])
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    view()
    await screen.findByText('Visible answer')
    await openRoomTools()
    fireEvent.click(screen.getByText('Search and export'))
    const search = screen.getByRole('searchbox', { name: 'Search conversation' })
    fireEvent.change(search, { target: { value: '한국어' } })
    expect(screen.getByText('한국어 요청')).toBeTruthy()
    expect(screen.queryByText('Visible answer')).toBeNull()
    const exportButton = screen.getByRole('button', { name: 'Export Markdown' })
    expect((exportButton as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(exportButton)
    expect(click).toHaveBeenCalledTimes(1)
    click.mockRestore()
  })

  it('copies the same safe authored transcript from a user gesture', async () => {
    vi.mocked(eventsApi.list).mockImplementation(async (_roomId, after) => after === 0 ? [
      { ...event(1, 'DemoUser'), actor_type: 'user', payload: { content: 'Keep this request' } },
      { ...event(2, 'Tool'), actor_type: 'tool', kind: 'tool_result', payload: { text: 'private output', arguments: { token: 'secret' } } },
    ] as never : [])
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', Object.assign(Object.create(navigator), { clipboard: { writeText } }))
    view()
    await screen.findByText('Keep this request')
    await openRoomTools()
    fireEvent.click(screen.getByText('Search and export'))
    fireEvent.click(screen.getByRole('button', { name: 'Copy conversation' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
    expect(writeText.mock.calls[0][0]).toContain('Keep this request')
    expect(writeText.mock.calls[0][0]).not.toContain('private output')
    expect(await screen.findByText('Conversation copied.')).toBeTruthy()
  })

  it('reports clipboard failure without claiming that the transcript was copied', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('clipboard blocked'))
    vi.stubGlobal('navigator', Object.assign(Object.create(navigator), { clipboard: { writeText } }))
    view()
    await screen.findByText('Reply 2')
    await openRoomTools()
    fireEvent.click(screen.getByText('Search and export'))
    fireEvent.click(screen.getByRole('button', { name: 'Copy conversation' }))
    expect((await screen.findByRole('alert')).textContent).toContain('Could not copy the conversation. Use Markdown export in a browser instead.')
    expect(screen.queryByText('Conversation copied.')).toBeNull()
  })

  it('uses copy as the native transcript action instead of an unsupported browser download', async () => {
    vi.mocked(isTauri).mockReturnValue(true)
    view()
    await screen.findByText('Reply 2')
    await openRoomTools()
    fireEvent.click(screen.getByText('Search and export'))
    expect(screen.queryByRole('button', { name: 'Export Markdown' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Copy conversation' })).toBeTruthy()
  })

  it('does not label or export a partial history after catch-up fails', async () => {
    vi.mocked(eventsApi.list).mockRejectedValue(new Error('offline'))
    view()
    await openRoomTools()
    fireEvent.click(screen.getByText('Search and export'))
    expect(await screen.findByText('Full history is unavailable. Reconnect before exporting.')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Export Markdown' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('loads and exports an inactive room without opening a live socket', async () => {
    vi.mocked(conversationsApi.get).mockResolvedValue({ ...room, is_active: false } as never)
    view()
    await screen.findByText('Reply 2')
    await openRoomTools()
    fireEvent.click(screen.getByText('Search and export'))
    expect((screen.getByRole('button', { name: 'Export Markdown' }) as HTMLButtonElement).disabled).toBe(false)
    expect(openRoomSocket).not.toHaveBeenCalled()
  })
})

it('retains a failed message and reuses its key on explicit retry', async () => {
  vi.mocked(conversationsApi.get).mockResolvedValue({ ...room, id: 503 } as never)
  vi.mocked(eventsApi.list).mockResolvedValue([])
  vi.mocked(eventsApi.send).mockRejectedValueOnce(new Error('network'))
  view(503)
  fireEvent.change(await screen.findByRole('combobox', { name: 'Message' }), { target: { value: 'Keep this' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send message' }))
  await screen.findByText(/message could not be confirmed/i)
  expect((screen.getByRole('combobox', { name: 'Message' }) as HTMLInputElement).value).toBe('Keep this')
  fireEvent.click(screen.getByRole('button', { name: 'Retry sending message' }))
  await waitFor(() => expect(eventsApi.send).toHaveBeenCalledTimes(2))
  expect(vi.mocked(eventsApi.send).mock.calls[0][2]).toBe(vi.mocked(eventsApi.send).mock.calls[1][2])
})

it('ignores a late old-room fetch and renders the typed OS permission path', async () => {
  let finishOld!: (events: never) => void
  vi.mocked(conversationsApi.get).mockImplementation(async (id) => ({ ...room, id } as never))
  vi.mocked(eventsApi.list).mockImplementation(async (id) => id === 601 ? new Promise((resolve) => { finishOld = resolve }) : id === 602 ? [
    { ...event(1, 'System'), event_id: '602-1', room_id: 602, kind: 'tool_result', payload: { state: 'failed', text: 'Denied', error_code: 'screen_recording_denied' } },
  ] as never : [])
  view(601)
  await waitFor(() => expect(finishOld).toBeTypeOf('function'))
  fireEvent.click(screen.getByRole('link', { name: 'Switch room' }))
  finishOld([{ ...event(1, 'Old'), room_id: 601, event_id: '601-1', payload: { content: 'Late old' } }] as never)
  const permissionEvent = (await screen.findByText('Screen access permission is required.')).closest('details')!
  expect(permissionEvent.open).toBe(false)
  fireEvent.click(within(permissionEvent).getByText('Screen access permission is required.'))
  expect(permissionEvent.open).toBe(true)
  await within(permissionEvent).findByText(/Screen & System Audio Recording/)
  expect(screen.queryByText('Late old')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Stop work' }))
  await waitFor(() => expect(conversationsApi.cancel).toHaveBeenCalledWith(602))
  expect(conversationsApi.stop).not.toHaveBeenCalled()
})

it('opens screen recording settings only for the typed permission failure and shows opener errors', async () => {
  vi.mocked(eventsApi.list).mockResolvedValue([
    { ...event(1, 'System'), kind: 'tool_result', payload: { state: 'failed', text: 'Denied', error_code: 'screen_recording_denied' } },
    { ...event(2, 'System'), kind: 'tool_result', payload: { state: 'failed', text: 'screen_recording_denied' } },
  ] as never)
  let rejectOpen!: (error: Error) => void
  vi.mocked(invoke).mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectOpen = reject }))
  view()
  const permissionEvent = (await screen.findAllByText('Screen access permission is required.'))[0].closest('details')!
  expect(permissionEvent.open).toBe(false)
  fireEvent.click(within(permissionEvent).getByText('Screen access permission is required.'))
  expect(permissionEvent.open).toBe(true)
  const button = await within(permissionEvent).findByRole('button', { name: 'Open screen recording settings' })
  expect(screen.getAllByRole('button', { name: 'Open screen recording settings' })).toHaveLength(1)
  expect(invoke).not.toHaveBeenCalled()
  fireEvent.click(button)
  await waitFor(() => expect(invoke).toHaveBeenCalledExactlyOnceWith('open_screen_recording_settings'))
  expect((button as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(button)
  expect(invoke).toHaveBeenCalledTimes(1)
  await act(async () => rejectOpen(new Error('open failed')))
  expect((await screen.findByRole('alert', { name: 'Settings error' })).textContent).toContain('Could not open System Settings')
})

it('checks native screen permission only on request and reports not-granted without requesting access', async () => {
  vi.mocked(eventsApi.list).mockResolvedValueOnce([
    { ...event(1, 'System'), kind: 'tool_result', payload: { state: 'failed', error_code: 'screen_recording_denied' } },
  ] as never).mockResolvedValue([])
  vi.mocked(invoke).mockResolvedValue('not_granted')
  view(501)
  const permissionEvent = (await screen.findByText('Screen access permission is required.')).closest('details')!
  fireEvent.click(within(permissionEvent).getByText('Screen access permission is required.'))
  const check = await within(permissionEvent).findByRole('button', { name: 'Check permission status again' })
  expect(invoke).not.toHaveBeenCalled()
  fireEvent.click(check)
  await waitFor(() => expect(invoke).toHaveBeenCalledExactlyOnceWith('get_screen_recording_permission_status'))
  expect(await within(permissionEvent).findByText(/Screen recording permission is not enabled for this app/)).toBeTruthy()
  expect(invoke).toHaveBeenCalledExactlyOnceWith('get_screen_recording_permission_status')
})

it('fills a socket gap and reconnects from the contiguous room cursor', async () => {
  vi.mocked(conversationsApi.get).mockResolvedValue({ ...room, id: 504 } as never)
  const first = { ...event(1, 'Ada'), room_id: 504, event_id: '504-1' }
  const second = { ...event(2, 'Bea'), room_id: 504, event_id: '504-2' }
  let initialRead = true
  vi.mocked(eventsApi.list).mockImplementation(async (_roomId, after) => {
    if (initialRead) { initialRead = false; return [] }
    return after === 0 ? [first, second] as never : []
  })
  view(504)
  await waitFor(() => expect(socket.onmessage).toBeTruthy())
  socket.onmessage!({ data: JSON.stringify({ type: 'event', ...second }) })
  await screen.findByText('Reply 1')
  expect(screen.getAllByText('Reply 2')).toHaveLength(1)
  socket.onclose!()
  await waitFor(() => expect(openRoomSocket).toHaveBeenCalledWith(504, 2), { timeout: 2500 })
})

it('disables transcript export when socket gap recovery fails', async () => {
  vi.mocked(eventsApi.list).mockResolvedValueOnce([]).mockRejectedValue(new Error('gap fetch failed'))
  view(501)
  await waitFor(() => expect(socket.onmessage).toBeTruthy())
  await openRoomTools()
  fireEvent.click(screen.getByText('Search and export'))
  expect((screen.getByRole('button', { name: 'Export Markdown' }) as HTMLButtonElement).disabled).toBe(false)
  socket.onmessage!({ data: JSON.stringify({ type: 'event', ...event(2, 'Bea') }) })
  expect(await screen.findByText('Full history is unavailable. Reconnect before exporting.')).toBeTruthy()
  expect((screen.getByRole('button', { name: 'Export Markdown' }) as HTMLButtonElement).disabled).toBe(true)
})

it('keeps always-approve state and its off switch visible in the sticky room header', async () => {
  vi.mocked(toolsApi.broadMode).mockResolvedValue({ enabled: true, scope: 'broad', expires_at: null })
  view(501)
  expect(await screen.findByText('Always approve is on')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Turn off' }))
  await waitFor(() => expect(toolsApi.setBroadMode).toHaveBeenCalledWith(false))
})

it('puts the exact pending approval after its triggering event and decides only through that inline card', async () => {
  vi.mocked(conversationsApi.listMembers).mockResolvedValue([{ room_id: 501, agent_id: 8, is_active: true,
    run_state: 'waiting', joined_at: '' }] as never)
  vi.mocked(toolsApi.activity).mockResolvedValueOnce([{ action_id: 'pending-a', room_id: 501, agent_id: 8,
    trigger_event_id: 'e1', family: 'write_file', target: '/tmp/fixture', expected_effect: 'Write fixture',
    state: 'waiting', scope: null, grant_id: null, requested_at: '', started_at: null, completed_at: null }])
    .mockResolvedValue([])
  view(501)
  const feed = await screen.findByRole('region', { name: 'Room events' })
  await waitFor(() => expect(socket.onopen).toBeTruthy())
  act(() => socket.onopen?.())
  await waitFor(() => expect(screen.getByRole('button', { name: 'Conversation options' }).title).toContain('Ada · Awaiting approval'))
  await within(feed).findByRole('article', { name: 'Approval for Ada' })
  expect(toolsApi.preview).toHaveBeenCalledWith(501, 'pending-a')
  const approve = await within(feed).findByRole('button', { name: 'Allow Modify file once for Ada' })
  const prompt = within(feed).getByRole('article', { name: 'Approval for Ada' })
  expect(within(prompt).getByText('Modify file')).toBeTruthy()
  expect(within(prompt).getByText((_text, element) => element?.tagName === 'P' && element.textContent === 'Target: /tmp/fixture')).toBeTruthy()
  expect(within(prompt).getByText('Writes 7 bytes of changes to this file.')).toBeTruthy()
  expect(within(prompt).getByText('Allow continued work on this file in this room. Target changes require another review.')).toBeTruthy()
  const technical = within(prompt).getByText('Details').closest('details')
  expect(technical?.open).toBe(false)
  expect(technical?.textContent).toContain('Write 7 bytes to file')
  expect(technical?.textContent).toContain('one room session')
  fireEvent.click(approve)
  await waitFor(() => expect(toolsApi.decide).toHaveBeenCalledWith(501, 'pending-a', 'approve', 'once', '550e8400-e29b-41d4-a716-446655440000', false))
  await waitFor(() => expect(within(feed).queryByRole('article', { name: 'Approval for Ada' })).toBeNull())
})

it('shows provider failure from room events and connection loss as unknown, never as busy', async () => {
  vi.mocked(conversationsApi.listMembers).mockResolvedValue([{ room_id: 501, agent_id: 8, is_active: true,
    run_state: 'idle', joined_at: '' }] as never)
  vi.mocked(eventsApi.list).mockResolvedValueOnce([{ ...event(1, 'Ada'), actor_id: 8, kind: 'failure',
    payload: { code: 'provider_unavailable', message: 'Turn failed.',
      diagnostic: { stage: 'provider_request', correlation_id: 'a'.repeat(32) } } }] as never).mockResolvedValue([])
  view(501)
  await waitFor(() => expect(socket.onopen).toBeTruthy())
  act(() => socket.onopen?.())
  await waitFor(() => expect(screen.getByRole('button', { name: 'Conversation options' }).title).toContain('Ada · Work failed'))
  expect(screen.getAllByText('Could not reach the model provider. Check the connection, then try again.')).toHaveLength(2)
  const technical = screen.getAllByText('Could not reach the model provider. Check the connection, then try again.')
    .map((node) => node.closest('details')).find(Boolean)
  expect(technical?.open).toBe(false)
  expect(technical?.closest('.conversation-auxiliary')?.querySelector('.avatar')).toBeNull()
  expect(technical?.textContent).toContain('provider_unavailable')
  expect(technical?.textContent).toContain('provider_request')
  expect(technical?.textContent).toContain('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')
  fireEvent.click(within(technical as HTMLElement).getByText('Could not reach the model provider. Check the connection, then try again.'))
  expect(technical?.open).toBe(true)
  socket.onclose?.()
  await waitFor(() => expect(screen.getByRole('button', { name: 'Conversation options' }).title).toContain('Ada · Checking connection'))
})

it('compacts system summaries while keeping authored agent summaries as messages', async () => {
  const checkpoint = 'Extractive room checkpoint with the exact technical summary retained for inspection.'
  const authored = '에이전트가 작성한 사용자용 요약입니다.'
  vi.mocked(eventsApi.list).mockResolvedValueOnce([
    { ...event(1, 'System'), actor_type: 'system', actor_id: null, kind: 'summary', payload: { content: checkpoint } },
    { ...event(2, 'Ada'), actor_id: 8, kind: 'summary', payload: { content: authored } },
  ] as never).mockResolvedValue([])

  view(501)

  const compact = (await screen.findByText('Conversation summary recorded.')).closest('details')
  expect(compact?.open).toBe(false)
  expect(compact?.closest('.conversation-auxiliary')?.querySelector('.avatar')).toBeNull()
  expect(compact?.textContent).toContain(checkpoint)
  fireEvent.click(within(compact as HTMLElement).getByText('Conversation summary recorded.'))
  expect(compact?.open).toBe(true)

  const agentSummary = screen.getByText(authored).closest('.conversation-event')
  expect(agentSummary?.querySelector('.avatar')).not.toBeNull()
  expect(agentSummary?.querySelector('details')).toBeNull()
})

it('keeps an uncertain external effect concise while preserving explicit inspection and expandable target details', async () => {
  vi.mocked(eventsApi.list).mockResolvedValue([])
  vi.mocked(toolsApi.activity).mockResolvedValue([{ action_id: 'uncertain-a', room_id: 501, agent_id: 8,
    trigger_event_id: null, family: 'write_file', target: '/tmp/fixture', expected_effect: 'Write fixture',
    state: 'uncertain', scope: null, grant_id: null, requested_at: '', started_at: '', completed_at: null }])
  view(501)

  expect(await screen.findByText('An external action may already have run. Check the actual result before continuing.')).toBeTruthy()
  expect(screen.getByRole('checkbox', { name: 'I checked the actual result for the current target.' })).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Continue room after inspection' })).toHaveProperty('disabled', true)
  const details = screen.getByText('Targets to check · 1 target').closest('details')
  expect(details?.open).toBe(false)
  expect(details?.textContent).toContain('/tmp/fixture')
  fireEvent.click(screen.getByRole('checkbox', { name: 'I checked the actual result for the current target.' }))
  expect(screen.getByRole('button', { name: 'Continue room after inspection' })).toHaveProperty('disabled', false)
})

it('links missing-secret recovery to the diagnostic connection without retrying the uncertain turn', async () => {
  vi.mocked(eventsApi.list).mockResolvedValueOnce([{ ...event(1, 'Ada'), actor_id: 8, kind: 'failure',
    payload: { code: 'missing_secret', message: 'Re-enter the API key for this saved connection.',
      diagnostic: { stage: 'provider_request', correlation_id: 'b'.repeat(32), connection_id: 2 } } }] as never).mockResolvedValue([])
  view(501)

  const link = await screen.findByRole('link', { name: /Re-enter the API key for connection #2/ })
  expect(link.getAttribute('href')).toBe('/connections?edit=2')
  expect(eventsApi.send).not.toHaveBeenCalled()
  expect(conversationsApi.continue).not.toHaveBeenCalled()
  expect(screen.queryByRole('button', { name: 'Retry sending message' })).toBeNull()
})

it('keeps agent state unknown until the room socket actually opens, including a failed handshake', async () => {
  vi.mocked(conversationsApi.listMembers).mockResolvedValue([{ room_id: 501, agent_id: 8, is_active: true,
    run_state: 'running', joined_at: '' }] as never)
  view(501)
  await waitFor(() => expect(socket.onopen).toBeTruthy())
  const status = screen.getByRole('button', { name: 'Conversation options' })
  await waitFor(() => expect(status.title).toContain('Ada · Checking connection'))
  const character = status.querySelector('.agent-character')
  expect(status.title).toContain('Ada · Checking connection')
  expect(screen.getByRole('status', { name: 'Room work status' }).textContent).toBe('Checking agent status')
  expect(character?.classList.contains('agent-character--unknown')).toBe(true)
  act(() => socket.onopen?.())
  expect(status.title).toContain('Ada · Thinking')
  expect(screen.getByRole('status', { name: 'Room work status' }).textContent).toBe('1 thinking')
  expect(character?.classList.contains('agent-character--thinking')).toBe(true)
  expect(character?.getAttribute('aria-hidden')).toBe('true')
  act(() => socket.onerror?.())
  expect(status.title).toContain('Ada · Checking connection')
  expect(character?.classList.contains('agent-character--unknown')).toBe(true)
  act(() => socket.onopen?.())
  expect(status.title).toContain('Ada · Checking connection')
})

it('reports missing and paused member state truthfully in the room header', async () => {
  vi.mocked(conversationsApi.listMembers).mockResolvedValue([{ room_id: 501, agent_id: 8, is_active: true,
    joined_at: '' }] as never)
  const rendered = view(501)
  await waitFor(() => expect(socket.onopen).toBeTruthy())
  act(() => socket.onopen?.())
  await waitFor(() => expect(screen.getByRole('status', { name: 'Room work status' }).textContent).toBe('1 status unknown'))

  rendered.unmount()
  roomEventCursor.clear()
  socket = { onmessage: null, onopen: null, onerror: null, onclose: null, close: vi.fn() }
  vi.mocked(openRoomSocket).mockResolvedValue(socket as unknown as WebSocket)
  vi.mocked(conversationsApi.listMembers).mockResolvedValue([{ room_id: 501, agent_id: 8, is_active: true,
    paused: true, run_state: 'running', joined_at: '' }] as never)
  view(501)
  await waitFor(() => expect(socket.onopen).toBeTruthy())
  act(() => socket.onopen?.())
  await waitFor(() => expect(screen.getByRole('status', { name: 'Room work status' }).textContent).toBe('1 paused'))
})

it('never renders prior-owner room data or a late response after an owner switch', async () => {
  let finishOld!: (events: never) => void
  vi.mocked(conversationsApi.get).mockImplementation(async () => {
    if (useAuthStore.getState().user?.id === 1) return { ...room, id: 901, title: 'Owner one room' } as never
    throw { response: { status: 404 } }
  })
  vi.mocked(eventsApi.list).mockImplementation(async () => useAuthStore.getState().user?.id === 1 ?
    new Promise((resolve) => { finishOld = resolve }) : Promise.reject(new Error('foreign room')))
  act(() => useAuthStore.getState().setAuth('owner-one', { id: 1, email: 'one@example.com', username: 'one' }))
  function OwnerGate() {
    const token = useAuthStore((state) => state.token)
    return token ? <Chat /> : <p>Logged out</p>
  }
  render(<QueryClientProvider client={queryClient}><MemoryRouter initialEntries={['/chat/901']}>
    <Routes><Route path="/chat/:conversationId" element={<OwnerGate />} /></Routes>
  </MemoryRouter></QueryClientProvider>)
  await screen.findByText('Owner one room')
  await waitFor(() => expect(finishOld).toBeTypeOf('function'))
  act(() => useAuthStore.getState().logout())
  await screen.findByText('Logged out')
  act(() => useAuthStore.getState().setAuth('owner-two', { id: 2, email: 'two@example.com', username: 'two' }))
  await act(async () => finishOld([{ ...event(1, 'Owner one'), event_id: 'owner-one-event', room_id: 901, payload: { content: 'Old private event' } }] as never))
  await waitFor(() => expect(vi.mocked(conversationsApi.get).mock.calls.length).toBeGreaterThan(1))
  expect(screen.queryByText('Owner one room')).toBeNull()
  expect(screen.queryByText('Old private event')).toBeNull()
  expect(roomEventCursor.events(901)).toEqual([])
  act(() => useAuthStore.getState().logout())
})

it('loads a visible screenshot privately and opens one accessible viewer', async () => {
  const screenshot = { ...event(1, 'Tool'), actor_type: 'tool', actor_id: null, kind: 'tool_result',
    created_at: '2026-10-02T12:34:56Z', payload: { state: 'completed', text: 'Screen captured',
      action_id: '1d1e0e6c-9eeb-4571-864b-f2592c44db38', screenshot_ref: 'rooms/501/screenshots/1d1e0e6c-9eeb-4571-864b-f2592c44db38.png',
      image_mime: 'image/png', image_width: 2560, image_height: 1440 } }
  vi.mocked(eventsApi.list).mockResolvedValueOnce([screenshot] as never).mockResolvedValue([])
  view()

  const thumbnail = await screen.findByRole('button', { name: 'Screenshot viewer · 2,560×1,440' })
  expect(toolsApi.screenshot).toHaveBeenCalledWith(501, screenshot.payload.action_id, screenshot.seq, expect.any(AbortSignal))
  expect(within(thumbnail).getByRole('img').getAttribute('src')).toBe('blob:screen-1')
  expect(screen.getByText(new Intl.DateTimeFormat('en').format(new Date(screenshot.created_at)))).toBeTruthy()
  fireEvent.click(thumbnail)

  const viewer = screen.getByRole('dialog', { name: 'Screenshot viewer' })
  expect(within(viewer).getByRole('img').getAttribute('src')).toBe('blob:screen-1')
  fireEvent.click(within(viewer).getByRole('button', { name: 'Zoom in' }))
  expect(within(viewer).getByRole('img').getAttribute('style')).toContain('scale(1.25)')
  expect(within(viewer).getByRole('link', { name: 'Save image' }).getAttribute('download')).toMatch(/\.png$/)
  fireEvent.keyDown(viewer, { key: 'Escape' })
  expect(screen.queryByRole('dialog', { name: 'Screenshot viewer' })).toBeNull()
})

it('keeps the private screenshot request alive while loading', async () => {
  const screenshot = { ...event(1, 'Tool'), actor_type: 'tool', actor_id: null, kind: 'tool_result',
    payload: { state: 'completed', text: 'Screen captured', action_id: '3d1e0e6c-9eeb-4571-864b-f2592c44db38',
      screenshot_ref: 'rooms/501/screenshots/3d1e0e6c-9eeb-4571-864b-f2592c44db38.png', image_mime: 'image/png', image_width: 2, image_height: 2 } }
  let finish!: (blob: Blob) => void
  let requestSignal!: AbortSignal
  vi.mocked(toolsApi.screenshot).mockImplementation(async (_room, _action, _seq, signal) => {
    requestSignal = signal!
    return new Promise((resolve) => { finish = resolve })
  })
  vi.mocked(eventsApi.list).mockResolvedValueOnce([screenshot] as never).mockResolvedValue([])
  view()

  await screen.findByText('Loading screenshot…')
  expect(requestSignal.aborted).toBe(false)
  await act(async () => finish(new Blob(['synthetic-png'], { type: 'image/png' })))
  expect(await screen.findByRole('button', { name: 'Screenshot viewer · 2×2' })).toBeTruthy()
})

it('releases an offscreen screenshot and reloads it when visible again', async () => {
  const screenshot = { ...event(1, 'Tool'), actor_type: 'tool', actor_id: null, kind: 'tool_result',
    payload: { state: 'completed', text: 'Screen captured', action_id: '5d1e0e6c-9eeb-4571-864b-f2592c44db38',
      screenshot_ref: 'rooms/501/screenshots/5d1e0e6c-9eeb-4571-864b-f2592c44db38.png', image_mime: 'image/png', image_width: 5, image_height: 5 } }
  vi.mocked(eventsApi.list).mockResolvedValueOnce([screenshot] as never).mockResolvedValue([])
  view()
  const thumbnail = await screen.findByRole('button', { name: 'Screenshot viewer · 5×5' })
  const firstUrl = within(thumbnail).getByRole('img').getAttribute('src')!

  act(() => VisibleObserver.latest?.emit(false))
  await waitFor(() => expect(revokedObjectUrls).toContain(firstUrl))
  expect(screen.queryByRole('button', { name: 'Screenshot viewer · 5×5' })).toBeNull()

  act(() => VisibleObserver.latest?.emit(true))
  expect(await screen.findByRole('button', { name: 'Screenshot viewer · 5×5' })).toBeTruthy()
  expect(toolsApi.screenshot).toHaveBeenCalledTimes(2)
})

it('keeps the screenshot blob while its viewer is open offscreen, then releases it on close', async () => {
  const screenshot = { ...event(1, 'Tool'), actor_type: 'tool', actor_id: null, kind: 'tool_result',
    payload: { state: 'completed', text: 'Screen captured', action_id: '6d1e0e6c-9eeb-4571-864b-f2592c44db38',
      screenshot_ref: 'rooms/501/screenshots/6d1e0e6c-9eeb-4571-864b-f2592c44db38.png', image_mime: 'image/png', image_width: 6, image_height: 6 } }
  vi.mocked(eventsApi.list).mockResolvedValueOnce([screenshot] as never).mockResolvedValue([])
  view()
  const thumbnail = await screen.findByRole('button', { name: 'Screenshot viewer · 6×6' })
  const privateUrl = within(thumbnail).getByRole('img').getAttribute('src')!
  fireEvent.click(thumbnail)

  act(() => VisibleObserver.latest?.emit(false))
  expect(screen.getByRole('dialog', { name: 'Screenshot viewer' })).toBeTruthy()
  expect(revokedObjectUrls).not.toContain(privateUrl)
  fireEvent.click(screen.getByRole('button', { name: 'Close screenshot viewer' }))

  await waitFor(() => expect(revokedObjectUrls).toContain(privateUrl))
  expect(screen.queryByRole('button', { name: 'Screenshot viewer · 6×6' })).toBeNull()
})

it('aborts an offscreen screenshot fetch and ignores its late response', async () => {
  const screenshot = { ...event(1, 'Tool'), actor_type: 'tool', actor_id: null, kind: 'tool_result',
    payload: { state: 'completed', text: 'Screen captured', action_id: '7d1e0e6c-9eeb-4571-864b-f2592c44db38',
      screenshot_ref: 'rooms/501/screenshots/7d1e0e6c-9eeb-4571-864b-f2592c44db38.png', image_mime: 'image/png', image_width: 7, image_height: 7 } }
  let finish!: (blob: Blob) => void
  let requestSignal!: AbortSignal
  vi.mocked(toolsApi.screenshot).mockImplementation(async (_room, _action, _seq, signal) => {
    requestSignal = signal!
    return new Promise((resolve) => { finish = resolve })
  })
  vi.mocked(eventsApi.list).mockResolvedValueOnce([screenshot] as never).mockResolvedValue([])
  view()
  await screen.findByText('Loading screenshot…')

  act(() => VisibleObserver.latest?.emit(false))
  await waitFor(() => expect(requestSignal.aborted).toBe(true))
  await act(async () => finish(new Blob(['late-synthetic-png'], { type: 'image/png' })))
  expect(URL.createObjectURL).not.toHaveBeenCalled()
  expect(screen.queryByRole('img', { name: 'Screenshot viewer · 7×7' })).toBeNull()
})

it('aborts a prior-owner screenshot fetch and ignores its late response after an owner switch', async () => {
  const screenshot = { ...event(1, 'Tool'), actor_type: 'tool', actor_id: null, kind: 'tool_result',
    payload: { state: 'completed', text: 'Screen captured', action_id: '8d1e0e6c-9eeb-4571-864b-f2592c44db38',
      screenshot_ref: 'rooms/501/screenshots/8d1e0e6c-9eeb-4571-864b-f2592c44db38.png', image_mime: 'image/png', image_width: 8, image_height: 8 } }
  let finish!: (blob: Blob) => void
  let requestSignal!: AbortSignal
  vi.mocked(toolsApi.screenshot).mockImplementation(async (_room, _action, _seq, signal) => {
    requestSignal = signal!
    return new Promise((resolve) => { finish = resolve })
  })
  vi.mocked(eventsApi.list).mockImplementation(async (_roomId, after) =>
    useAuthStore.getState().user?.id === 1 && after === 0 ? [screenshot] as never : [])
  act(() => useAuthStore.getState().setAuth('owner-one', { id: 1, email: 'one@example.com', username: 'one' }))
  view()
  await screen.findByText('Loading screenshot…')

  act(() => useAuthStore.getState().setAuth('owner-two', { id: 2, email: 'two@example.com', username: 'two' }))
  await waitFor(() => expect(requestSignal.aborted).toBe(true))
  await act(async () => finish(new Blob(['late-owner-one-png'], { type: 'image/png' })))
  expect(URL.createObjectURL).not.toHaveBeenCalled()
  expect(screen.queryByRole('img', { name: 'Screenshot viewer · 8×8' })).toBeNull()
  act(() => useAuthStore.getState().logout())
})

it('deletes a screenshot explicitly, preserves its message, and revokes the private blob', async () => {
  const screenshot = { ...event(1, 'Tool'), actor_type: 'tool', actor_id: null, kind: 'tool_result',
    payload: { state: 'completed', text: 'Screen captured', action_id: '2d1e0e6c-9eeb-4571-864b-f2592c44db38',
      screenshot_ref: 'rooms/501/screenshots/2d1e0e6c-9eeb-4571-864b-f2592c44db38.png', image_mime: 'image/png', image_width: 1, image_height: 1 } }
  vi.mocked(eventsApi.list).mockResolvedValueOnce([screenshot] as never).mockResolvedValue([])
  view()
  const thumbnail = await screen.findByRole('button', { name: 'Screenshot viewer · 1×1' })
  const privateUrl = within(thumbnail).getByRole('img').getAttribute('src')!
  fireEvent.click(thumbnail)
  fireEvent.click(screen.getByRole('button', { name: 'Delete image' }))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm image deletion' }))

  await screen.findByText('Image removed')
  expect(screen.getByText('Screenshot ready.')).toBeTruthy()
  const details = screen.getByText('Screenshot ready.').closest('details')
  expect(details?.open).toBe(false)
  expect(details?.textContent).toContain('completed')
  expect(details?.textContent).toContain('Screen captured')
  expect(toolsApi.deleteScreenshot).toHaveBeenCalledWith(501, screenshot.payload.action_id, screenshot.seq)
  expect(revokedObjectUrls).toContain(privateUrl)
})

it('traps viewer focus and keeps confirmation keyboard handling inside the modal', async () => {
  const screenshot = { ...event(1, 'Tool'), actor_type: 'tool', actor_id: null, kind: 'tool_result',
    payload: { state: 'completed', text: 'Screen captured', action_id: '4d1e0e6c-9eeb-4571-864b-f2592c44db38',
      screenshot_ref: 'rooms/501/screenshots/4d1e0e6c-9eeb-4571-864b-f2592c44db38.png', image_mime: 'image/png', image_width: 4, image_height: 4 } }
  vi.mocked(eventsApi.list).mockResolvedValueOnce([screenshot] as never).mockResolvedValue([])
  const rendered = view()
  fireEvent.click(await screen.findByRole('button', { name: 'Screenshot viewer · 4×4' }))
  const viewer = screen.getByRole('dialog', { name: 'Screenshot viewer' })
  await waitFor(() => expect(document.activeElement).toBe(viewer))
  expect(rendered.container.hasAttribute('inert')).toBe(true)

  const first = within(viewer).getByRole('button', { name: 'Zoom out' })
  const last = within(viewer).getByRole('button', { name: 'Close screenshot viewer' })
  fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
  expect(document.activeElement).toBe(last)
  last.focus()
  fireEvent.keyDown(document, { key: 'Tab' })
  expect(document.activeElement).toBe(first)
  first.focus()
  fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
  expect(document.activeElement).toBe(last)

  const remove = within(viewer).getByRole('button', { name: 'Delete image' })
  fireEvent.click(remove)
  const confirm = screen.getByRole('button', { name: 'Confirm image deletion' })
  await waitFor(() => expect(document.activeElement).toBe(confirm))
  fireEvent.keyDown(document, { key: 'Escape' })
  expect(screen.queryByRole('alertdialog', { name: 'Delete screenshot' })).toBeNull()
  expect(screen.getByRole('dialog', { name: 'Screenshot viewer' })).toBeTruthy()
  expect(document.activeElement).toBe(remove)

  fireEvent.keyDown(document, { key: 'Escape' })
  expect(screen.queryByRole('dialog', { name: 'Screenshot viewer' })).toBeNull()
  expect(rendered.container.hasAttribute('inert')).toBe(false)
})

it('routes fixed-runtime screenshot bytes and deletes through native IPC with the exact event sequence', async () => {
  const { toolsApi: realToolsApi } = await vi.importActual<typeof import('../api/tools')>('../api/tools')
  vi.mocked(isTauri).mockReturnValue(true)
  useAuthStore.getState().setAuth('private-owner-token', { id: 1, email: 'owner@test.invalid', username: 'owner' })
  let httpCalls = 0
  api.defaults.adapter = (async () => { httpCalls += 1; throw new Error('HTTP must not be used') }) satisfies AxiosAdapter
  vi.mocked(invoke).mockImplementation(async (command) => {
    if (command === 'screenshot_artifact_transport') return 'native'
    if (command === 'read_screenshot_artifact') return new Uint8Array([1, 2, 3]).buffer
    if (command === 'delete_screenshot_artifact') return undefined
    throw new Error('unexpected command')
  })

  const blob = await realToolsApi.screenshot(501, ACTION_ID, 73)
  expect(blob.type).toBe('image/png')
  expect(blob.size).toBe(3)
  expect(invoke).toHaveBeenCalledWith('read_screenshot_artifact', {
    roomId: 501, actionId: ACTION_ID, eventSeq: 73, bearerToken: 'private-owner-token',
  })
  await realToolsApi.deleteScreenshot(501, ACTION_ID, 73)
  expect(invoke).toHaveBeenCalledWith('delete_screenshot_artifact', {
    roomId: 501, actionId: ACTION_ID, eventSeq: 73, bearerToken: 'private-owner-token',
  })
  expect(httpCalls).toBe(0)
})

it('normalizes the macOS Tauri raw callback byte array into a screenshot blob', async () => {
  const { toolsApi: realToolsApi } = await vi.importActual<typeof import('../api/tools')>('../api/tools')
  vi.mocked(isTauri).mockReturnValue(true)
  useAuthStore.getState().setAuth('private-owner-token', { id: 1, email: 'owner@test.invalid', username: 'owner' })
  vi.mocked(invoke).mockImplementation(async (command) => {
    if (command === 'screenshot_artifact_transport') return 'native'
    if (command === 'read_screenshot_artifact') return [137, 80, 78, 71]
    throw new Error('unexpected command')
  })

  const blob = await realToolsApi.screenshot(501, ACTION_ID, 73)
  expect(blob.type).toBe('image/png')
  expect(blob.size).toBe(4)
})

it('rejects malformed and oversized macOS Tauri raw callback arrays', async () => {
  const { toolsApi: realToolsApi } = await vi.importActual<typeof import('../api/tools')>('../api/tools')
  vi.mocked(isTauri).mockReturnValue(true)
  useAuthStore.getState().setAuth('private-owner-token', { id: 1, email: 'owner@test.invalid', username: 'owner' })
  const sparse: number[] = []
  sparse.length = 4
  const oversized: number[] = []
  oversized.length = 10_000_001
  for (const payload of [[137, 256, -1, 1.5, Number.NaN], sparse, oversized]) {
    vi.mocked(invoke).mockImplementation(async (command) => {
      if (command === 'screenshot_artifact_transport') return 'native'
      if (command === 'read_screenshot_artifact') return payload
      throw new Error('unexpected command')
    })
    await expect(realToolsApi.screenshot(501, ACTION_ID, 73)).rejects.toThrow('Screenshot unavailable')
  }
})

it('uses existing HTTP screenshot routes outside native fixed runtime', async () => {
  const { toolsApi: realToolsApi } = await vi.importActual<typeof import('../api/tools')>('../api/tools')
  vi.mocked(isTauri).mockReturnValue(false)
  const requests: string[] = []
  api.defaults.adapter = (async (request) => {
    requests.push(`${request.method}:${request.url}`)
    return { data: request.method === 'get' ? new Blob(['http-png'], { type: 'image/png' }) : undefined,
      status: 200, statusText: 'OK', headers: {}, config: request }
  }) satisfies AxiosAdapter

  expect((await realToolsApi.screenshot(501, ACTION_ID, 73)).type).toBe('image/png')
  await realToolsApi.deleteScreenshot(501, ACTION_ID, 73)
  expect(requests).toEqual([
    `get:/tools/rooms/501/actions/${ACTION_ID}/screenshot`,
    `delete:/tools/rooms/501/actions/${ACTION_ID}/screenshot`,
  ])
  expect(invoke).not.toHaveBeenCalled()
})

it('maps native removal to 410 and never falls back to HTTP after native authorization fails', async () => {
  const { toolsApi: realToolsApi } = await vi.importActual<typeof import('../api/tools')>('../api/tools')
  vi.mocked(isTauri).mockReturnValue(true)
  useAuthStore.getState().setAuth('private-owner-token', { id: 1, email: 'owner@test.invalid', username: 'owner' })
  let httpCalls = 0
  api.defaults.adapter = (async () => { httpCalls += 1; throw new Error('fallback') }) satisfies AxiosAdapter
  vi.mocked(invoke).mockImplementation(async (command) => {
    if (command === 'screenshot_artifact_transport') return 'native'
    throw 'artifact_removed'
  })

  await expect(realToolsApi.screenshot(501, ACTION_ID, 73)).rejects.toMatchObject({ response: { status: 410 } })
  expect(httpCalls).toBe(0)
})

it('renders only a fixed delete error when a native failure contains private text', async () => {
  const screenshot = { ...event(1, 'Tool'), actor_type: 'tool', actor_id: null, kind: 'tool_result',
    payload: { state: 'completed', text: 'Screen captured', action_id: ACTION_ID,
      screenshot_ref: `rooms/501/screenshots/${ACTION_ID}.png`, image_mime: 'image/png', image_width: 1, image_height: 1 } }
  vi.mocked(eventsApi.list).mockResolvedValueOnce([screenshot] as never).mockResolvedValue([])
  vi.mocked(toolsApi.deleteScreenshot).mockRejectedValue(new Error('private-owner-token /private/path'))
  view()
  fireEvent.click(await screen.findByRole('button', { name: 'Screenshot viewer · 1×1' }))
  fireEvent.click(screen.getByRole('button', { name: 'Delete image' }))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm image deletion' }))

  expect((await screen.findByRole('alert')).textContent).toBe('Image could not be removed. Try again.')
  expect(document.body.textContent).not.toContain('private-owner-token')
  expect(document.body.textContent).not.toContain('/private/path')
})


it('compacts generated final-result requests without hiding identical authored text or losing request inspection', async () => {
  const raw = 'Create a final result from the linked request and peer responses.'
  const sourceId = '11111111-1111-4111-8111-111111111111'
  const finalizeId = '22222222-2222-4222-8222-222222222222'
  const generated = { ...event(2, 'DemoUser'), event_id: finalizeId, actor_type: 'user', actor_id: null,
    payload: { content: raw, finalize: { version: 1, source_request_event_id: sourceId,
      source_outcome_event_ids: ['peer-one'], agent_id: 8 } } }
  vi.mocked(eventsApi.list).mockResolvedValueOnce([
    { ...event(1, 'DemoUser'), event_id: sourceId, actor_type: 'user', actor_id: null, payload: { content: raw } },
    generated,
    { ...event(3, 'Ada'), actor_id: 8, payload: { content: 'Authored final answer', trigger_event_id: finalizeId } },
  ] as never).mockResolvedValue([])
  view(501)
  const summary = await screen.findByText('Final result requested.')
  const details = summary.closest('details')!
  expect(details.open).toBe(false)
  expect(details.textContent).toContain(raw)
  expect(details.closest('article')?.querySelector('.avatar')).toBeNull()
  const authored = screen.getAllByText(raw).find((node) => node.classList.contains('conversation-event__body'))!
  expect(authored.closest('article')?.querySelector('.avatar')).not.toBeNull()
  const result = screen.getByText('Authored final answer').closest('article')!
  expect(within(result).getByText('Final result')).toBeTruthy()
  fireEvent.click(summary)
  expect(details.open).toBe(true)
  fireEvent.click(within(details).getByRole('button', { name: 'View request progress' }))
  expect(await screen.findByRole('dialog', { name: 'Request progress' })).toBeTruthy()
  expect(requestInspectorApi.get).toHaveBeenCalledWith(501, finalizeId)
  expect(generated.payload.content).toBe(raw)
  expect(eventsApi.send).not.toHaveBeenCalled()
  expect(requestFinalizeApi.execute).not.toHaveBeenCalled()
})

it.each([
  { name: 'unsupported version', actor: 'user', finalize: { version: 0, source_request_event_id: 'source', source_outcome_event_ids: [], agent_id: 8 } },
  { name: 'missing source request', actor: 'user', finalize: { version: 1, source_outcome_event_ids: [], agent_id: 8 } },
  { name: 'invalid outcome collection', actor: 'user', finalize: { version: 1, source_request_event_id: 'source', source_outcome_event_ids: 'wrong', agent_id: 8 } },
  { name: 'authored agent metadata', actor: 'agent', finalize: { version: 1, source_request_event_id: 'source', source_outcome_event_ids: [], agent_id: 8 } },
])('keeps $name as an authored message', async ({ name, actor, finalize }) => {
  const content = `Keep this authored text: ${name}`
  vi.mocked(eventsApi.list).mockResolvedValueOnce([{ ...event(1, 'Ada'), actor_type: actor,
    payload: { content, finalize } }] as never).mockResolvedValue([])
  view(501)
  const authored = (await screen.findByText(content)).closest('article')!
  expect(authored.querySelector('.avatar')).not.toBeNull()
  expect(authored.querySelector('details')).toBeNull()
  expect(screen.queryByText('Final result requested.')).toBeNull()
})


it('shows a compact original-partial note beside overconfident final prose and opens only the original request', async () => {
  const sourceId = '11111111-1111-4111-8111-111111111111'
  const finalId = '22222222-2222-4222-8222-222222222222'
  const answer = { ...event(2, 'Ada'), actor_id: 8, payload: { content: 'Original useful answer', trigger_event_id: sourceId } }
  const failed = { ...event(3, 'Bea'), actor_id: 9, kind: 'failure' as const,
    payload: { code: 'provider_unavailable', message: 'PRIVATE_DIAGNOSTIC', trigger_event_id: sourceId } }
  vi.mocked(eventsApi.list).mockImplementation(async (_roomId, after) => after === 0 ? [
    { ...event(1, 'DemoUser'), actor_type: 'user', event_id: sourceId, payload: { content: 'Original question' } },
    answer, failed,
    { ...event(4, 'DemoUser'), actor_type: 'user', event_id: finalId, payload: { content: 'Internal finalize',
      finalize: { version: 1, source_request_event_id: sourceId,
        source_outcome_event_ids: [answer.event_id, failed.event_id], agent_id: 8 } } },
    { ...event(5, 'Ada'), actor_id: 8, payload: { content: 'Remaining uncertainty: None.', trigger_event_id: finalId } },
    { ...event(6, 'Bea'), actor_id: 9, payload: { content: 'Separately recovered', trigger_event_id: 'retry' } },
  ] as never : [])
  view()
  const article = (await screen.findByText('Remaining uncertainty: None.')).closest('article')!
  expect(within(article).getByText('Some responses or source text are missing from this summary.')).toBeTruthy()
  expect(within(article).queryByText('PRIVATE_DIAGNOSTIC')).toBeNull()
  fireEvent.click(within(article).getByRole('button', { name: 'Review original discussion' }))
  await waitFor(() => expect(requestInspectorApi.get).toHaveBeenCalledWith(501, sourceId))
  expect(requestFinalizeApi.execute).not.toHaveBeenCalled()
  expect(eventsApi.send).not.toHaveBeenCalled()
})


it('preserves the reading position and an unsent draft while settings are opened and closed', async () => {
  const result = view()
  const composer = await screen.findByRole('combobox', { name: 'Message' }) as HTMLTextAreaElement
  await screen.findByText('Reply 2')
  fireEvent.change(composer, { target: { value: 'Only a draft' } })
  composer.setSelectionRange(4, 7)
  const scroller = result.container.querySelector('.room-scroll') as HTMLElement
  scroller.scrollTop = 173
  const trigger = screen.getByRole('button', { name: 'Conversation options' })
  trigger.focus(); fireEvent.click(trigger)
  const panel = screen.getByRole('dialog', { name: 'Conversation options' })
  fireEvent.click(within(panel).getByText('Room settings'))
  expect(composer.isConnected).toBe(true)
  expect(scroller.scrollTop).toBe(173)
  expect(eventsApi.send).not.toHaveBeenCalled()
  expect(toolsApi.setBroadMode).not.toHaveBeenCalled()
  expect(toolsApi.decide).not.toHaveBeenCalled()
  fireEvent.keyDown(document, { key: 'Escape' })
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(document.activeElement).toBe(trigger)
  expect(composer.value).toBe('Only a draft')
  expect([composer.selectionStart, composer.selectionEnd]).toEqual([4, 7])
  expect(scroller.scrollTop).toBe(173)
})

it('does not scroll or acknowledge new peer messages behind an open management dialog', async () => {
  const result=view()
  await screen.findByText('Reply 2')
  await waitFor(()=>expect(socket.onmessage).toBeTruthy())
  await openRoomTools()
  const scroller=result.container.querySelector('.room-scroll') as HTMLElement
  scroller.scrollTop=91
  vi.mocked(readStatesApi.put).mockClear()
  vi.mocked(Element.prototype.scrollIntoView).mockClear()
  act(()=>socket.onmessage!({data:JSON.stringify({type:'event',...event(3,'Ada')})}))
  await waitFor(()=>expect(scroller.textContent).toContain('Reply 3'))
  expect(scroller.scrollTop).toBe(91)
  expect(Element.prototype.scrollIntoView).not.toHaveBeenCalled()
  expect(readStatesApi.put).not.toHaveBeenCalled()
  expect(eventsApi.send).not.toHaveBeenCalled()
})

it('retains room-specific Stop work inside the management modal', async () => {
  view();await screen.findByText('Reply 2')
  const panel=await openRoomTools()
  expect(conversationsApi.cancel).not.toHaveBeenCalled()
  fireEvent.click(within(panel).getByRole('button',{name:'Stop work'}))
  await waitFor(()=>expect(conversationsApi.cancel).toHaveBeenCalledWith(501))
  expect(conversationsApi.cancel).toHaveBeenCalledTimes(1)
  expect(conversationsApi.delete).not.toHaveBeenCalled()
  expect(toolsApi.setBroadMode).not.toHaveBeenCalled()
})

it('links a collapsed canonical tool result to its safe action/review IDs without dumping audit payload fields', async () => {
  const review = '550e8400-e29b-41d4-a716-446655440000'
  vi.mocked(eventsApi.list).mockImplementation(async (_id, after) => after === 0 ? [{ ...event(1, 'Tool'), actor_type: 'tool', kind: 'tool_result',
    payload: { state: 'completed', text: 'Owned fixture done', action_id: ACTION_ID, review_id: review, credential_ref: 'NEVER_RENDER_AUDIT_SECRET' } }] as never : [])
  view(501)
  const summary = await screen.findByText('Tool work finished.')
  const details = summary.closest('details')!
  expect(details.open).toBe(false)
  fireEvent.click(summary)
  expect(within(details).getByText(ACTION_ID)).toBeTruthy()
  expect(within(details).getByText(review)).toBeTruthy()
  expect(document.body.textContent).not.toContain('NEVER_RENDER_AUDIT_SECRET')
})


it('keeps the conversation DOM, draft, selection and reading position across the results tab', async () => {
  useAuthStore.getState().setAuth('owner', { id: 1, username: 'Owner', email: 'owner@example.com' })
  vi.mocked(roomResultsApi.list).mockResolvedValue({ room_id: 501, items: [], next_cursor: null })
  const result = view(); const composer = await screen.findByRole('combobox', { name: 'Message' }) as HTMLTextAreaElement
  await screen.findByText('Reply 2'); await waitFor(() => expect(socket.onmessage).toBeTruthy())
  fireEvent.change(composer, { target: { value: 'Keep my draft' } }); composer.setSelectionRange(2, 6)
  const feed = result.container.querySelector('.room-scroll') as HTMLElement; feed.scrollTop = 123
  fireEvent.click(screen.getByRole('button', { name: 'Results' }))
  await screen.findByText(/No saved results/); expect(composer.isConnected).toBe(true)
  vi.mocked(readStatesApi.put).mockClear(); vi.mocked(Element.prototype.scrollIntoView).mockClear()
  act(() => socket.onmessage!({ data: JSON.stringify({ type: 'event', ...event(3, 'Ada') }) }))
  expect(feed.scrollTop).toBe(123); expect(readStatesApi.put).not.toHaveBeenCalled(); expect(Element.prototype.scrollIntoView).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Conversation' }))
  await waitFor(() => expect(feed.scrollTop).toBe(123))
  expect(composer.value).toBe('Keep my draft'); expect([composer.selectionStart, composer.selectionEnd]).toEqual([2, 6])
  expect(eventsApi.send).not.toHaveBeenCalled(); expect(toolsApi.decide).not.toHaveBeenCalled()
})

it('opens a source-bound result form from an Agent menu without sending or granting tools', async () => {
  useAuthStore.getState().setAuth('owner', { id: 1, username: 'Owner', email: 'owner@example.com' })
  const id = '11111111-1111-4111-8111-111111111111'
  vi.mocked(eventsApi.list).mockImplementation(async (_room, after) => after === 0 ? [{ ...event(1, 'Ada'), event_id: id }] as never : [])
  vi.mocked(roomResultsApi.list).mockResolvedValue({ room_id: 501, items: [], next_cursor: null })
  view(); const article = (await screen.findByText('Reply 1')).closest('article')!
  fireEvent.click(within(article).getByRole('button', { name: /Message actions/ }))
  fireEvent.click(screen.getByRole('menuitem', { name: 'Save result' }))
  await screen.findByRole('form', { name: 'Save result' }); expect(screen.getByText('Save result · #1')).toBeTruthy()
  expect(document.activeElement).toBe(screen.getByLabelText('Result title'))
  expect(eventsApi.send).not.toHaveBeenCalled(); expect(toolsApi.decide).not.toHaveBeenCalled()
})


it('labels missing screenshot dimensions as unrecorded while keeping the authenticated viewer', async () => {
  const id = '11111111-1111-4111-8111-111111111111'
  vi.mocked(eventsApi.list).mockImplementation(async (_room, after) => after === 0 ? [{ ...event(1, 'Tool'), event_id: id,
    actor_type: 'tool', actor_id: null, kind: 'tool_result', payload: { state: 'completed', action_id: id,
    screenshot_ref: `rooms/501/screenshots/${id}.png`, image_width: 0, image_height: 0 } }] as never : [])
  view(); const thumb = await screen.findByRole('button', { name: 'Screenshot viewer · Not recorded or unavailable' })
  expect(toolsApi.screenshot).toHaveBeenCalledWith(501, id, 1, expect.any(AbortSignal))
  fireEvent.click(thumb); expect(await screen.findByRole('dialog', { name: 'Screenshot viewer' })).toBeTruthy()
  expect(screen.queryByText(/0×0/)).toBeNull()
})


it('returns to the mounted conversation when a same-room exact source link is opened from results', async () => {
  useAuthStore.getState().setAuth('owner', { id: 1, username: 'Owner', email: 'owner@example.com' })
  const id = '11111111-1111-4111-8111-111111111111'
  vi.mocked(eventsApi.list).mockImplementation(async (_room, after) => after === 0 ? [{ ...event(1, 'Ada'), event_id: id }] as never : [])
  vi.mocked(roomResultsApi.list).mockResolvedValue({ room_id: 501, items: [], next_cursor: null })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const result = render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/chat/501']}>
    <Link to={`/chat/501?seq=1&event=${id}`}>Exact external source</Link><Routes><Route path="/chat/:conversationId" element={<Chat />} /></Routes>
  </MemoryRouter></QueryClientProvider>)
  await screen.findByText('Reply 1'); fireEvent.click(screen.getByRole('button', { name: 'Results' })); await screen.findByText(/No saved results/)
  fireEvent.click(screen.getByRole('link', { name: 'Exact external source' }))
  await waitFor(() => expect((result.container.querySelector('.room-conversation') as HTMLElement).hidden).toBe(false))
  await waitFor(() => expect(result.container.querySelector('[data-event-seq="1"]')?.classList.contains('conversation-event--target')).toBe(true))
  expect(eventsApi.send).not.toHaveBeenCalled()
})

vi.mock('../api/roomMemories', () => ({ roomMemoriesApi: { list: vi.fn(), add: vi.fn(), edit: vi.fn(), remove: vi.fn(), legacy: vi.fn() } }))

it('preserves conversation DOM, draft, selection, reading position and Stop across the memory tab', async () => {
  useAuthStore.getState().setAuth('owner', { id: 1, username: 'Owner', email: 'owner@example.com' })
  vi.mocked(roomMemoriesApi.list).mockResolvedValue({ room_id: 501, revision: 0, common_context_after_seq: 0, items: [], agents: [] })
  const result = view(); const composer = await screen.findByRole('combobox', { name: 'Message' }) as HTMLTextAreaElement
  await screen.findByText('Reply 2'); await waitFor(() => expect(socket.onmessage).toBeTruthy())
  fireEvent.change(composer, { target: { value: 'Only a memory-tab draft' } }); composer.setSelectionRange(2, 6)
  const feed = result.container.querySelector('.room-scroll') as HTMLElement; feed.scrollTop = 123
  fireEvent.click(screen.getByRole('button', { name: 'Room memory' })); await screen.findByText('No memory entries yet.')
  expect(composer.isConnected).toBe(true)
  vi.mocked(readStatesApi.put).mockClear(); vi.mocked(Element.prototype.scrollIntoView).mockClear()
  act(() => socket.onmessage!({ data: JSON.stringify({ type: 'event', ...event(3, 'Ada') }) }))
  expect(feed.scrollTop).toBe(123); expect(readStatesApi.put).not.toHaveBeenCalled(); expect(Element.prototype.scrollIntoView).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Conversation' })); await waitFor(() => expect(feed.scrollTop).toBe(123))
  expect(composer.value).toBe('Only a memory-tab draft'); expect([composer.selectionStart, composer.selectionEnd]).toEqual([2, 6])
  expect(eventsApi.send).not.toHaveBeenCalled(); expect(toolsApi.decide).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Room memory' })); fireEvent.click(screen.getByRole('button', { name: 'Stop work' }))
  await waitFor(() => expect(conversationsApi.cancel).toHaveBeenCalledWith(501))
})
