// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import api from './api'
import { botBatchesApi, validBotBatch, type BotBatch } from '../api/botBatches'
import type { BotMessageRef } from '../api/botMessages'
import type { RoomEvent } from '../api/events'
import { matchingBotBatch, summarizedBatchRequest, summarizedBatchRequestRoot, isBatchMemberMessage } from './botBatch'
import RootBotBatches from '../components/RootBotBatches'
import BotBatchEvent from '../components/BotBatchEvent'
import { useAuthStore } from '../stores/authStore'
vi.mock('./api', () => ({ default: { get: vi.fn() } }))
const id = (n: number) => `af035000-0000-4000-8000-${String(n).padStart(12, '0')}`
const ref = (room: number, n: number, seq: number, actor: number | null, kind: BotMessageRef['kind'] = 'message'): BotMessageRef => ({
  room_id: room, event_id: id(n), seq, actor_type: actor === null ? 'system' : 'agent', actor_id: actor, kind,
})
const batch = (): BotBatch => ({ version: 1, batch_id: id(100), root_ref: { ...ref(501, 1, 1, 1), actor_type: 'user' },
  source_room: { room_id: 501, title: 'Ada DM' }, sender_agent_id: 8, sender_name: 'Ada', parent_job_id: null, parent_kind: null,
  state: 'running', reply_state: 'none', failure_code: null, request_ref: ref(501, 2, 2, 8), return_delivery_ref: null, return_ref: null,
  source_scope_id: id(200), created_at: '2026-10-29T01:00:00Z', updated_at: '2026-10-29T01:01:00Z', deadline_at: '2026-10-29T01:30:00Z',
  members: [9,12,10].map((actor, index) => ({ index, message_id: id(300 + index), recipient_agent_id: actor, recipient_name: ['Bea','Cory','Dee'][index],
    target_room: { room_id: 701 + index, title: ['Bea DM','Cory DM','Dee DM'][index] }, content: `Task ${index + 1}`, state: index === 0 ? 'completed' : 'running', failure_code: null,
    request_ref: ref(501, 10 + index, 3 + index, 8), delivery_ref: ref(701 + index, 20 + index, 1, null, 'activity'),
    result_ref: index === 0 ? ref(701, 30, 2, actor) : null, resolved_result_ref: index === 0 ? ref(701, 30, 2, actor) : null,
  })),
})
const view = (value: BotBatch) => <MemoryRouter><RootBotBatches batches={[value]} current fetching={false} failed={false} truncated={false} canUse={() => true} onRead={vi.fn()} onOpen={vi.fn()} /></MemoryRouter>
const memberEvent = (value: BotBatch, phase: 'delivery'|'response'): RoomEvent => {
  const member = value.members[0], r = phase === 'delivery' ? member.delivery_ref : member.result_ref!
  return { ...r, device_id: null, actor_name_snapshot: phase === 'delivery' ? 'System' : 'Bea', created_at: value.updated_at, idempotency_key: null,
    payload: { category: 'bot_message', content: phase === 'delivery' ? member.content : 'Actual answer', trigger_event_id: value.root_ref.event_id,
      bot_message: { version: 1, message_id: member.message_id, phase, sender_agent_id: value.sender_agent_id, recipient_agent_id: member.recipient_agent_id,
        root_room_id: value.root_ref.room_id, root_request_event_id: value.root_ref.event_id },
      bot_batch: { version: 1, batch_id: value.batch_id, phase: phase === 'delivery' ? 'branch_delivery' : 'branch_response', member_index: 0,
        root_room_id: value.root_ref.room_id, root_request_event_id: value.root_ref.event_id } } }
}
beforeEach(() => { vi.resetAllMocks(); useAuthStore.getState().setAuth('synthetic', { id: 1, email: 'owner@example.invalid', username: 'Owner' }) })
afterEach(() => { cleanup(); vi.restoreAllMocks() })
it('rejects premature completion and incomplete or reordered members instead of presenting a successful join', () => {
  const value = batch(); expect(validBotBatch(value, 501, 1)).toBe(true)
  expect(validBotBatch({ ...value, state: 'completed' }, 501, 1)).toBe(false)
  expect(validBotBatch({ ...value, reply_state: 'pending' }, 501, 1)).toBe(false)
  expect(validBotBatch({ ...value, members: value.members.slice(0, 1) }, 501, 1)).toBe(false)
  expect(validBotBatch({ ...value, members: [value.members[1], value.members[0], value.members[2]] }, 501, 1)).toBe(false)
  const missing = structuredClone(value); missing.members[0].resolved_result_ref = null
  expect(validBotBatch(missing, 501, 1)).toBe(false)
})
it('matches each canonical branch by its actual recipient room, actor, event and input index', () => {
  const value = batch(), r = value.members[0].result_ref!
  const event: RoomEvent = { ...r, device_id: null, actor_name_snapshot: 'Bea', payload: { content: 'Answer', trigger_event_id: value.root_ref.event_id,
    bot_batch: { version: 1, batch_id: value.batch_id, phase: 'branch_response', member_index: 0, root_room_id: 501, root_request_event_id: value.root_ref.event_id } }, created_at: value.updated_at, idempotency_key: null }
  expect(matchingBotBatch(event, [value])).toBe(value)
  const root: RoomEvent = { ...event, ...value.root_ref, payload: { content: 'Original request' } }
  const request: RoomEvent = { ...event, ...value.members[0].request_ref, payload: { content: value.members[0].content, trigger_event_id: value.root_ref.event_id,
    bot_batch: { version: 1, batch_id: value.batch_id, phase: 'branch_request', member_index: 0, root_room_id: 501, root_request_event_id: value.root_ref.event_id } } }
  expect(summarizedBatchRequest(request, [value], [root, request], 1)).toBe(true)
  expect(summarizedBatchRequestRoot(request, [value], [root, request], 1)).toEqual(value.root_ref)
  expect(summarizedBatchRequest(request, [], [root, request], 1)).toBe(false)
  expect(summarizedBatchRequest(request, [value], [request], 1)).toBe(false)
  expect(summarizedBatchRequest(request, [value], [{ ...root, actor_id: 2 }, request], 1)).toBe(false)
  expect(summarizedBatchRequest(event, [value], [root, event], 1)).toBe(false)
  render(<MemoryRouter><BotBatchEvent event={event} batches={[value]} canUse={() => true} onOpen={vi.fn()} /></MemoryRouter>)
  expect(screen.getByRole('region', { name: 'Teammate reply' }).textContent).toContain('Bea → Ada')
  for (const changed of [{ ...event, room_id: 702 }, { ...event, actor_id: 12 }, { ...event, seq: 3 }, { ...event, event_id: id(99) },
    { ...event, payload: { ...event.payload, bot_batch: { ...event.payload.bot_batch as object, member_index: 1 } } }]) expect(matchingBotBatch(changed, [value])).toBeNull()
  const nested = structuredClone(value); nested.members[0].resolved_result_ref = ref(701, 98, 9, 9)
  expect(validBotBatch(nested, 701, 1)).toBe(true)
  nested.members[0].resolved_result_ref!.actor_id = 12
  expect(validBotBatch(nested, 701, 1)).toBe(false)
})
it('keeps the first answer separate from overall completion and links the actual final contribution', () => {
  const value = batch(); value.members[0].resolved_result_ref = ref(701, 98, 9, 9)
  render(view(value))
  expect(screen.getByText('1 of 3 answers received')).toBeTruthy()
  expect(screen.getByText('Working together')).toBeTruthy()
  expect(screen.queryByText('All teammate answers received')).toBeNull()
  expect(screen.getByRole('link', { name: 'Bea · open answer' }).getAttribute('href')).toBe(`/chat/701?seq=9&event=${id(98)}`)
  expect(screen.queryByRole('link', { name: /summary answer/ })).toBeNull()
})
it('keeps partial failure and root Stop distinct from a received sender summary', () => {
  const value = batch(); value.members.forEach((member, index) => { member.state = index === 1 ? 'failed' : 'completed'; member.failure_code = index === 1 ? 'provider_unavailable' : null
    member.result_ref = ref(member.target_room.room_id, 30 + index, 2, member.recipient_agent_id, index === 1 ? 'failure' : 'message'); member.resolved_result_ref = member.result_ref })
  value.state = 'partial'; value.failure_code = 'partial_members'; value.reply_state = 'pending'
  expect(validBotBatch(value, 501, 1)).toBe(true)
  const rendered = render(view(value))
  expect(screen.getByText('2 of 3 answers received')).toBeTruthy(); expect(screen.getByText('Some work could not be finished')).toBeTruthy()
  expect(screen.getByRole('link', { name: 'Cory · open error' }).getAttribute('href')).toContain('/chat/702?seq=2&event=')
  expect(screen.getByText('Ada · waiting to prepare the summary')).toBeTruthy()
  value.state = 'cancelled'; value.reply_state = 'cancelled'; value.failure_code = 'cancelled'; rendered.rerender(view(value))
  expect(screen.getByRole('link', { name: 'Bea · open answer' })).toBeTruthy(); expect(screen.queryByRole('link', { name: /summary answer/ })).toBeNull()
  value.state = 'partial'; value.reply_state = 'delivered'; value.failure_code = 'partial_members'; value.return_ref = ref(501, 70, 7, 8)
  rendered.rerender(view(value))
  expect(screen.getByRole('link', { name: 'Ada · open summary answer' }).getAttribute('href')).toBe(`/chat/501?seq=7&event=${id(70)}`)
})
it('rejects delayed responses after Owner epoch changes and requires the exact truncation flag', async () => {
  let finish!: (value: unknown) => void
  vi.mocked(api.get).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }) as ReturnType<typeof api.get>)
  const reading = botBatchesApi.list(501)
  useAuthStore.getState().setAuth('other-synthetic', { id: 2, email: 'other@example.invalid', username: 'Other' })
  finish({ data: { version: 1, room_id: 501, batches: [batch()], batches_truncated: false } })
  await expect(reading).rejects.toThrow('unavailable')
  useAuthStore.getState().setAuth('synthetic', { id: 1, email: 'owner@example.invalid', username: 'Owner' })
  vi.mocked(api.get).mockResolvedValueOnce({ data: { version: 1, room_id: 501, batches: [batch()], truncated: false } })
  await expect(botBatchesApi.list(501)).rejects.toThrow('unavailable')
})

it('shows only the exact incoming member as replied while siblings still work, with both canonical metadata families', () => {
  const value = batch(), delivery = memberEvent(value, 'delivery'), response = memberEvent(value, 'response')
  expect(validBotBatch(value, 701, 1)).toBe(true); expect(validBotBatch(value, 701, 2)).toBe(false)
  expect(isBatchMemberMessage(delivery)).toBe(true); expect(isBatchMemberMessage(response)).toBe(true)
  expect(matchingBotBatch(delivery, [value])).toBe(value); expect(matchingBotBatch(response, [value])).toBe(value)
  render(<MemoryRouter>{[delivery,response].map(event => <BotBatchEvent key={event.event_id} event={event} batches={[value]} current canUse={() => true} onOpen={vi.fn()} />)}</MemoryRouter>)
  expect(screen.getAllByText('Teammate replied')).toHaveLength(2)
  expect(screen.queryByText('Current status unavailable')).toBeNull(); expect(screen.queryByText('All teammate answers received')).toBeNull()
  expect(screen.queryByText('Cory')).toBeNull(); expect(screen.queryByText('Dee')).toBeNull()
  expect(screen.getByRole('link', { name: 'Bea · open answer' }).getAttribute('href')).toBe(`/chat/701?seq=2&event=${id(30)}`)
  expect(screen.getAllByRole('link', { name: 'View the answers together' }).every(node => node.getAttribute('href') === `/chat/501?seq=1&event=${id(1)}`)).toBe(true)
  for (const broken of [{ ...response, payload: { ...response.payload, bot_message: { ...(response.payload.bot_message as object), message_id: value.members[1].message_id } } },
    { ...response, payload: { ...response.payload, bot_message: { ...(response.payload.bot_message as object), sender_agent_id: 10 } } },
    { ...response, payload: { ...response.payload, bot_batch: { ...(response.payload.bot_batch as object), member_index: 1 } } }]) expect(matchingBotBatch(broken, [value])).toBeNull()
})
it('does not turn an early answer, failed member or unavailable batch into a completed sibling result', () => {
  const value = batch(); value.members[0].state = 'running'
  const read = vi.fn(), renderMember = (rows: BotBatch[], current: boolean) => <MemoryRouter><BotBatchEvent event={memberEvent(value, 'response')} batches={rows} current={current} failed={!current} onRead={read} canUse={() => true} onOpen={vi.fn()} /></MemoryRouter>
  const rendered = render(renderMember([value], true))
  expect(screen.getByText('Waiting for follow-up work')).toBeTruthy(); expect(screen.queryByText('Teammate replied')).toBeNull()
  value.members[0].state = 'failed'; value.members[0].failure_code = 'provider_unavailable'; value.members[0].result_ref = { ...value.members[0].result_ref!, kind: 'failure' }; value.members[0].resolved_result_ref = null
  rendered.rerender(renderMember([value], true))
  expect(screen.getByText('Teammate request failed')).toBeTruthy(); expect(screen.queryByText('Teammate replied')).toBeNull()
  rendered.rerender(renderMember([], false))
  expect(screen.getByText('Current status unavailable')).toBeTruthy(); expect(screen.queryByText('Teammate replied')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Retry' })); expect(read).toHaveBeenCalledTimes(1)
})
