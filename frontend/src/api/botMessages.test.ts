// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from 'vitest'
import api from '../lib/api'
import { botMessagesApi, botMessageRetriesApi, type BotMessageRetryPlan, type BotMessage, type BotMessageRef, type RoomBotMessages } from './botMessages'
import type { RoomEvent } from './events'
import { matchingBotMessage } from '../lib/botMessage'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { botMessageRetryKey, readBotMessageRetry, readBotMessageRetryPlan, startBotMessageRetry, resendBotMessageRetry } from '../lib/botMessageRetryWorkspace'
import { useAuthStore } from '../stores/authStore'
vi.mock('../lib/api', () => ({ default: { get: vi.fn(), post: vi.fn() } }))
const root = 'bc012000-0000-4000-8000-000000000001', id = 'bc012000-0000-4000-8000-000000000002'
const uuid = (n: number) => `bc012000-0000-4000-8000-${String(n).padStart(12, '0')}`
const ref = (room_id: number, seq: number, actor_id: number | null, event_id: string, actor_type: BotMessageRef['actor_type'], kind: BotMessageRef['kind']): BotMessageRef => ({ room_id, seq, actor_id, event_id, actor_type, kind })
const message = (): BotMessage => ({ version: 1, capabilities: { text_collaboration: true, delegated_os_execution: false }, message_id: id,
  root_request_event_id: root, parent_job_id: null, parent_kind: null, depth: 1, sender_agent_id: 8, sender_name: 'Ada', recipient_agent_id: 9, recipient_name: 'Bea', content: 'Review only this request',
  state: 'completed', reply_state: 'delivered', source_room: { room_id: 501, title: 'Ada DM' }, target_room: { room_id: 701, title: 'Bea inbox' },
  root_ref: ref(501, 17, 1, root, 'user', 'message'), request_ref: ref(501, 18, 8, uuid(3), 'agent', 'message'),
  delivery_ref: ref(701, 1, null, uuid(4), 'system', 'activity'), result_ref: ref(701, 2, 9, uuid(5), 'agent', 'message'),
  return_delivery_ref: ref(501, 19, null, uuid(6), 'system', 'activity'), return_ref: ref(501, 20, 8, uuid(7), 'agent', 'message'),
  failure_code: null, deadline_at: '2026-10-29T01:05:00Z', created_at: '2026-10-29T01:00:00Z', updated_at: '2026-10-29T01:01:00Z' })
const response = (row = message(), roomId = 501): RoomBotMessages => ({ version: 1, room_id: roomId, capabilities: row.capabilities, bots: [], messages: [row], messages_truncated: false })
const event = (row: BotMessage, phase: 'request' | 'delivery', reference: BotMessageRef): RoomEvent => ({ ...reference,
  device_id: null, actor_name_snapshot: '', idempotency_key: null, created_at: row.created_at,
  payload: { ...(phase === 'request' ? { content: row.content } : { category: 'bot_message' }), trigger_event_id: root,
    bot_message: { version: 1, message_id: id, phase, sender_agent_id: 8, recipient_agent_id: 9, root_room_id: 501, root_request_event_id: root } } })
beforeEach(() => { vi.resetAllMocks(); useAuthStore.getState().setAuth('synthetic', { id: 1, email: 'test@example.invalid', username: 'test' }) })
it('accepts separate room sequences and reads only the requested owner room', async () => {
  vi.mocked(api.get).mockResolvedValue({ data: response() })
  expect((await botMessagesApi.list(501)).messages[0].result_ref?.seq).toBe(2)
  expect(api.get).toHaveBeenCalledWith('/bots/conversations/501/messages')
})
it('matches the foreign-root delivery by exact target room, UUID, sequence and actor', () => {
  const row = message(), delivery = event(row, 'delivery', row.delivery_ref)
  expect(matchingBotMessage(delivery, [row])).toBe(row)
  expect(matchingBotMessage({ ...delivery, room_id: 501 }, [row])).toBeNull()
  expect(matchingBotMessage({ ...delivery, seq: 2 }, [row])).toBeNull()
  expect(matchingBotMessage({ ...delivery, actor_type: 'agent', actor_id: 9 }, [row])).toBeNull()
})
it('does not infer a request from names or changed content', () => {
  const row = message(), request = event(row, 'request', row.request_ref)
  expect(matchingBotMessage(request, [row])).toBe(row)
  expect(matchingBotMessage({ ...request, payload: { ...request.payload, content: 'different' } }, [row])).toBeNull()
  expect(matchingBotMessage({ ...request, event_id: uuid(90) }, [row])).toBeNull()
})
it('rejects a result reference from a third room instead of building a source-room link', async () => {
  const row = message(); row.result_ref = { ...row.result_ref!, room_id: 999 }
  vi.mocked(api.get).mockResolvedValue({ data: response(row) })
  await expect(botMessagesApi.list(501)).rejects.toThrow('unavailable')
})
it('rejects impossible ordering within the target room', async () => {
  const row = message(); row.result_ref = { ...row.result_ref!, seq: 1 }
  vi.mocked(api.get).mockResolvedValue({ data: response(row) })
  await expect(botMessagesApi.list(501)).rejects.toThrow('unavailable')
})
it('accepts a final nested result or an older server without it, and rejects changed scope', async () => {
  const row = message(); row.resolved_result_ref = ref(701, 4, 9, uuid(80), 'agent', 'message')
  vi.mocked(api.get).mockResolvedValue({ data: response(row) })
  expect((await botMessagesApi.list(501)).messages[0].resolved_result_ref?.seq).toBe(4)
  for (const changed of [{ room_id: 999 }, { actor_id: 10 }, { seq: 1 }, { kind: 'activity' }, { actor_type: 'system', actor_id: null }]) {
    row.resolved_result_ref = { ...ref(701, 4, 9, uuid(80), 'agent', 'message'), ...changed } as BotMessageRef
    await expect(botMessagesApi.list(501)).rejects.toThrow('unavailable')
  }
  delete row.resolved_result_ref
  expect((await botMessagesApi.list(501)).messages[0].resolved_result_ref).toBeUndefined()
})

it('keeps interrupted and busy-expired jobs without inventing a Bot answer', async () => {
  for (const code of ['interrupted', 'target_busy_timeout']) {
    const row = message(); Object.assign(row, { state: 'failed', reply_state: 'failed', failure_code: code, result_ref: null, return_delivery_ref: null, return_ref: null })
    vi.mocked(api.get).mockResolvedValue({ data: response(row) })
    const [record] = (await botMessagesApi.list(501)).messages
    expect(record.failure_code).toBe(code); expect(record.result_ref).toBeNull()
  }
})
it('drops a delayed response after the login scope changes', async () => {
  let resolve!: (value: { data: RoomBotMessages }) => void
  vi.mocked(api.get).mockReturnValue(new Promise(done => { resolve = done }))
  const read = botMessagesApi.list(501)
  useAuthStore.getState().setAuth('second', { id: 2, email: 'second@example.invalid', username: 'second' })
  resolve({ data: response() })
  await expect(read).rejects.toThrow('unavailable')
})

it('rejects a completed status with no canonical answer reference', async () => {
  const row = message(); row.result_ref = null
  vi.mocked(api.get).mockResolvedValue({ data: response(row) })
  await expect(botMessagesApi.list(501)).rejects.toThrow('unavailable')
})

const retryPlan = (): BotMessageRetryPlan => ({ version: 1, message_id: id, expected_updated_at: '2026-10-29T01:01:00Z',
  root_ref: message().root_ref, eligible: true, blocked_reason: null, source_agent_id: 10, cost: 'new_model_work', plan_hash: 'a'.repeat(64) })
const retryReceipt = () => ({ version: 1 as const, message_id: id, root_room_id: 501, retry_event_id: uuid(99), retry_seq: 21,
  href: 'https://untrusted.example/redirect', source_agent_id: 10, root_ref: message().root_ref, replayed: false, cost: 'new_model_work' as const, plan_hash: 'a'.repeat(64) })
it('reads retry eligibility only explicitly and accepts a current root-selected Bot different from the nested sender', async () => {
  vi.mocked(api.get).mockResolvedValue({ data: retryPlan() })
  const plan = await botMessageRetriesApi.preview(message())
  expect(plan.source_agent_id).toBe(10)
  expect(api.get).toHaveBeenCalledWith(`/bots/messages/${id}/retry`)
  expect(api.post).not.toHaveBeenCalled()
})
it('sends only the frozen CAS body and key, then builds the exact internal new-root link', async () => {
  vi.mocked(api.post).mockResolvedValue({ data: retryReceipt() })
  const plan = retryPlan(), body = Object.freeze({ expected_updated_at: plan.expected_updated_at, plan_hash: plan.plan_hash! })
  const accepted = await botMessageRetriesApi.start(plan, body, uuid(80))
  expect(api.post).toHaveBeenCalledWith(`/bots/messages/${id}/retry`, body, { headers: { 'Idempotency-Key': uuid(80) } })
  expect(accepted.href).toBe(`/chat/501?seq=21&event=${uuid(99)}`)
})
it('does not confirm a receipt for a different selected source Bot', async () => {
  vi.mocked(api.post).mockResolvedValue({ data: { ...retryReceipt(), source_agent_id: 8 } })
  const plan = retryPlan()
  await expect(botMessageRetriesApi.start(plan, { expected_updated_at: plan.expected_updated_at, plan_hash: plan.plan_hash! }, uuid(80))).rejects.toThrow('unconfirmed')
})
it('rejects a preview that points at a different original root', async () => {
  vi.mocked(api.get).mockResolvedValue({ data: { ...retryPlan(), root_ref: { ...message().root_ref, room_id: 702 } } })
  await expect(botMessageRetriesApi.preview(message())).rejects.toThrow('unavailable')
})

it('retains an uncertain frozen CAS and key, blocks duplicate sends, and replays only explicitly', async () => {
  vi.mocked(api.get).mockResolvedValue({ data: retryPlan() })
  vi.mocked(api.post).mockRejectedValueOnce(new Error('receipt lost')).mockResolvedValueOnce({ data: { ...retryReceipt(), replayed: true } })
  const key = botMessageRetryKey(`1:${useAuthStore.getState().epoch}`, directoryViewGeneration(), id)
  readBotMessageRetryPlan(key, message())
  await vi.waitFor(() => expect(readBotMessageRetry(key).readPhase).toBe('ready'))
  const readId = readBotMessageRetry(key).readId
  startBotMessageRetry(key, readId); startBotMessageRetry(key, readId)
  await vi.waitFor(() => expect(readBotMessageRetry(key).operation?.phase).toBe('unknown'))
  expect(api.post).toHaveBeenCalledTimes(1)
  const original = readBotMessageRetry(key).operation!
  expect(Object.isFrozen(original.body)).toBe(true)
  readBotMessageRetryPlan(key, message())
  expect(api.get).toHaveBeenCalledTimes(1)
  expect(readBotMessageRetry(key).operation?.key).toBe(original.key)
  resendBotMessageRetry(key)
  await vi.waitFor(() => expect(readBotMessageRetry(key).operation?.phase).toBe('accepted'))
  expect(api.post).toHaveBeenCalledTimes(2)
  expect(vi.mocked(api.post).mock.calls[1][1]).toBe(original.body)
  expect(vi.mocked(api.post).mock.calls[1][2]).toEqual({ headers: { 'Idempotency-Key': original.key } })
  expect(readBotMessageRetry(key).operation?.receipt?.replayed).toBe(true)
})
it('clears the retry workspace on login change and ignores its late acceptance', async () => {
  vi.mocked(api.get).mockResolvedValue({ data: retryPlan() })
  let finish!: (value: { data: ReturnType<typeof retryReceipt> }) => void
  const delayed = new Promise<{ data: ReturnType<typeof retryReceipt> }>(resolve => { finish = resolve })
  vi.mocked(api.post).mockReturnValue(delayed)
  const key = botMessageRetryKey(`1:${useAuthStore.getState().epoch}`, directoryViewGeneration(), id)
  readBotMessageRetryPlan(key, message())
  await vi.waitFor(() => expect(readBotMessageRetry(key).readPhase).toBe('ready'))
  startBotMessageRetry(key, readBotMessageRetry(key).readId)
  expect(readBotMessageRetry(key).operation?.phase).toBe('sending')
  useAuthStore.getState().setAuth('second', { id: 2, email: 'second@example.invalid', username: 'second' })
  finish({ data: retryReceipt() }); await delayed; await Promise.resolve(); await Promise.resolve()
  expect(readBotMessageRetry(key).operation).toBeNull()
})

it('requires a fresh preview and key after a bound plan is rejected', async () => {
  vi.mocked(api.get).mockResolvedValueOnce({ data: retryPlan() }).mockResolvedValueOnce({ data: { ...retryPlan(), source_agent_id: 8, plan_hash: 'b'.repeat(64) } })
  vi.mocked(api.post).mockRejectedValueOnce({ response: { status: 409, data: { detail: { code: 'bot_message_retry_changed' } } } })
  const key = botMessageRetryKey(`1:${useAuthStore.getState().epoch}`, directoryViewGeneration(), id)
  readBotMessageRetryPlan(key, message())
  await vi.waitFor(() => expect(readBotMessageRetry(key).readPhase).toBe('ready'))
  startBotMessageRetry(key, readBotMessageRetry(key).readId)
  await vi.waitFor(() => expect(readBotMessageRetry(key).operation?.phase).toBe('rejected'))
  const prior = readBotMessageRetry(key).operation!
  expect(prior.body.plan_hash).toBe('a'.repeat(64))
  expect(prior.receipt).toBeUndefined()
  readBotMessageRetryPlan(key, message())
  await vi.waitFor(() => expect(readBotMessageRetry(key).plan?.plan_hash).toBe('b'.repeat(64)))
  expect(readBotMessageRetry(key).operation).toBeNull()
  expect(api.post).toHaveBeenCalledTimes(1)
})
it('does not confirm an acceptance for a different plan hash', async () => {
  vi.mocked(api.post).mockResolvedValue({ data: { ...retryReceipt(), plan_hash: 'b'.repeat(64) } })
  const plan = retryPlan()
  await expect(botMessageRetriesApi.start(plan, { expected_updated_at: plan.expected_updated_at, plan_hash: plan.plan_hash! }, uuid(80))).rejects.toThrow('unconfirmed')
})
