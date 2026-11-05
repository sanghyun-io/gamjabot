// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import api from './api'
import { botCreationsApi, validBotCreation, type BotCreationReceipt } from '../api/botCreations'
import type { RoomEvent } from '../api/events'
import { botCreationMetadata, matchingBotCreation, duplicateCreationRequest } from './botCreation'
import BotCreation from '../components/BotCreation'
import { avatarFromSeed } from './avatarRecipe'
import { useAuthStore } from '../stores/authStore'
vi.mock('./api', () => ({ default: { get: vi.fn(), post: vi.fn() } }))
const id = (n: number) => `af036000-0000-4000-8000-${String(n).padStart(12, '0')}`
const receipt = (): BotCreationReceipt => ({ version: 1, creation_id: id(100), state: 'created', source_agent_id: 8, source_room_id: 501,
  root_ref: { room_id: 501, event_id: id(1), seq: 1, actor_type: 'user', actor_id: 1, kind: 'message' },
  request_ref: { room_id: 501, event_id: id(2), seq: 2, actor_type: 'agent', actor_id: 8, kind: 'activity' },
  result_ref: { room_id: 501, event_id: id(3), seq: 3, actor_type: 'system', actor_id: null, kind: 'activity' },
  created_agent: { id: 13, name: 'Statistics', description: 'Checks numbers and assumptions.', system_prompt: 'Explain the source and uncertainty of every conclusion.',
    connection_id: 1, model_id: 'qwen3.7-plus', temperature: 'auto', max_tokens: 0, avatar: avatarFromSeed(13) },
  conversation: { room_id: 713, project_id: 40, href: '/chat/713' }, model_binding: { connection_id: 1, model_id: 'qwen3.7-plus', config_revision: 1, auth_grant_revision: 1 },
  readback: { agent_matches: true, conversation_matches: true, available: true }, created_at: '2026-10-29T01:00:00Z', replayed: false })
const event = (phase: 'request'|'created' = 'created'): RoomEvent => {
  const row = receipt(), ref = phase === 'request' ? row.request_ref : row.result_ref
  return { ...ref, device_id: null, actor_name_snapshot: phase === 'request' ? 'Planner' : 'System', created_at: row.created_at, idempotency_key: null,
    payload: { category: 'bot_creation', status: phase === 'request' ? 'requested' : 'created', trigger_event_id: row.root_ref.event_id,
      ...(phase === 'created' ? { created_agent_id: 13, created_room_id: 713 } : {}),
      bot_creation: { version: 1, creation_id: row.creation_id, phase, root_room_id: 501, root_request_event_id: row.root_ref.event_id, source_agent_id: 8 } } }
}
const clients: QueryClient[] = []
function Location() { return <output data-testid="location">{useLocation().pathname}</output> }
function view(rows: BotCreationReceipt[], current = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } }); clients.push(client)
  return <QueryClientProvider client={client}><MemoryRouter initialEntries={['/chat/501']}><BotCreation event={event()} receipts={rows} receivedAt={0} current={current} fetching={false} canUse={() => true} /><Location /></MemoryRouter></QueryClientProvider>
}
beforeEach(() => { vi.resetAllMocks(); useAuthStore.getState().setAuth('synthetic', { id: 1, email: 'owner@example.invalid', username: 'Owner' }) })
afterEach(() => { cleanup(); clients.splice(0).forEach(client => client.clear()); vi.restoreAllMocks() })
it('requires the exact Owner root, inherited model binding and canonical creation references', () => {
  const row = receipt(); expect(validBotCreation(row, 501, 1)).toBe(true)
  expect(validBotCreation(row, 701, 1)).toBe(false); expect(validBotCreation(row, 501, 2)).toBe(false)
  expect(validBotCreation({ ...row, state: 'pending' }, 501, 1)).toBe(false)
  expect(validBotCreation({ ...row, model_binding: { ...row.model_binding, model_id: 'other-model' } }, 501, 1)).toBe(false)
  expect(validBotCreation({ ...row, conversation: { ...row.conversation, href: '/chat/999' } }, 501, 1)).toBe(false)
  expect(validBotCreation({ ...row, result_ref: { ...row.result_ref, actor_type: 'agent', actor_id: 8 } }, 501, 1)).toBe(false)
})
it('keeps a receipt exactly once while preserving a sending-record fallback when the created record is absent', () => {
  const request = event('request'), result = event(), row = receipt()
  expect(matchingBotCreation(result, [row])).toBe(row)
  expect(duplicateCreationRequest(request, [request, result])).toBe(true)
  expect(duplicateCreationRequest(request, [request])).toBe(false)
  expect(matchingBotCreation({ ...result, seq: 4 }, [row])).toBeNull()
  expect(matchingBotCreation({ ...result, payload: { ...result.payload, created_agent_id: 14 } }, [row])).toBeNull()
  expect(botCreationMetadata({ ...result, actor_type: 'agent', actor_id: 8 })).toBeNull()
})
it('shows the stored role/model and exact conversation only when the current readback matches', () => {
  const row = receipt(); render(view([row]))
  expect(screen.getByText('Bot created')).toBeTruthy(); expect(screen.getByText('Model · qwen3.7-plus')).toBeTruthy()
  expect(screen.getByText(row.created_agent.system_prompt)).toBeTruthy()
  expect(screen.getByRole('link', { name: 'Chat with Statistics' }).getAttribute('href')).toBe('/chat/713')
  expect(api.post).not.toHaveBeenCalled()
  cleanup(); row.readback = { agent_matches: false, conversation_matches: true, available: false }; render(view([row]))
  expect(screen.queryByRole('link')).toBeNull(); expect(screen.getByText(/This is the creation record/)).toBeTruthy()
})
it('recovers an unknown creation by reading its existing operation only and never automatically navigates or writes', async () => {
  render(view([], false))
  expect(screen.queryByText('Bot created')).toBeNull(); expect(screen.queryByRole('link')).toBeNull()
  vi.mocked(api.get).mockResolvedValueOnce({ data: receipt() })
  fireEvent.click(screen.getByRole('button', { name: 'Check creation result again' }))
  expect(await screen.findByText('Bot created')).toBeTruthy()
  expect(api.get).toHaveBeenCalledExactlyOnceWith(`/bots/creations/${id(100)}`)
  expect(api.post).not.toHaveBeenCalled(); expect(screen.getByTestId('location').textContent).toBe('/chat/501')
  expect(screen.getByRole('link', { name: 'Chat with Statistics' }).getAttribute('href')).toBe('/chat/713')
})
it('rejects an operation result arriving after the Owner session changes', async () => {
  let finish!: (value: unknown) => void
  vi.mocked(api.get).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }) as ReturnType<typeof api.get>)
  const reading = botCreationsApi.read(id(100), 501, id(1))
  useAuthStore.getState().setAuth('other-synthetic', { id: 2, email: 'other@example.invalid', username: 'Other' })
  finish({ data: receipt() }); await expect(reading).rejects.toThrow('owner changed')
})
