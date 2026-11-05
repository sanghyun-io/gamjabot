import { describe, expect, it } from 'vitest'
import type { Agent } from '../api/agents'
import type { Connection } from '../api/connections'
import type { RoomEvent } from '../api/events'
import type { RoomMember } from '../api/conversations'
import {
  buildRecipientGroups,
  filterRecipientGroups,
  filterTranscriptEvents,
  finalResultSource,
  mentionAtCaret,
  memberReadiness,
  quoteAtCaret,
  replaceMentionAtCaret,
  routableMentionContent,
  transcriptMarkdown,
} from './roomWorkflow'

const agent = (id: number, name: string, connectionId = id, modelId = `model-${id}`): Agent => ({
  id, user_id: 1, name, description: '', system_prompt: '', model: modelId,
  connection_id: connectionId, model_id: modelId, temperature: 'auto', max_tokens: 0,
  is_active: true, created_at: '', updated_at: null,
})
const member = (agentId: number, paused = false): RoomMember => ({
  room_id: 5, agent_id: agentId, is_active: true, joined_at: '', paused, run_state: paused ? 'paused' : 'idle',
})
const connection = (overrides: Partial<Connection> = {}): Connection => ({
  id: 1, owner_id: 1, provider_kind: 'openai', label: 'Primary', base_url: null,
  default_model_id: 'model-1', enabled: true, auth_required: true, has_secret: true, is_local: false,
  capabilities: { declared: {}, verified: { models: { 'model-1': { tools: true } } } }, ...overrides,
})
const roomEvent = (seq: number, actorType: RoomEvent['actor_type'], content: string): RoomEvent => ({
  event_id: `event-${seq}`, device_id: null, room_id: 5, seq, actor_type: actorType,
  actor_id: actorType === 'system' || actorType === 'tool' ? null : seq,
  actor_name_snapshot: actorType === 'user' ? 'DemoUser' : actorType === 'agent' ? '감자' : 'System',
  kind: actorType === 'tool' ? 'tool_result' : 'message', payload: actorType === 'tool'
    ? { text: content, arguments: { token: 'must-not-export' } } : { content },
  created_at: `2026-10-03T0${seq}:00:00Z`, idempotency_key: null,
})

describe('room recipient routing', () => {
  it('matches the backend mention behavior for active members, duplicate names, Korean names, and paused members', () => {
    const agents = [agent(1, 'Ada'), agent(2, 'ada'), agent(3, '감자'), agent(4, 'Paused'), agent(5, 'Broken!'), agent(6, 'Kelvin')]
    const groups = buildRecipientGroups(agents, [member(1), member(2), member(3), member(4, true), member(5), member(6)])
    expect(groups.map((item) => [item.name, item.agentIds])).toEqual([
      ['Ada', [1, 2]], ['감자', [3]], ['Kelvin', [6]],
    ])
    expect(groups.find((item) => item.name === 'Ada')?.matches('@ADA, answer @감자 too')).toBe(true)
    expect(groups.find((item) => item.name === '감자')?.matches('@ADA, answer @감자 too')).toBe(true)
    expect(groups.find((item) => item.name === 'Ada')?.matches('@Ada\u0301 follows the backend boundary')).toBe(true)
    expect(groups.find((item) => item.name === 'Kelvin')?.matches('@kelvin answer')).toBe(true)
    expect(groups.some((item) => item.name === 'Paused')).toBe(false)
  })

  it('finds and replaces the mention token at the caret without changing surrounding text', () => {
    const value = 'Please @Ad review this'
    const token = mentionAtCaret(value, 10)
    expect(token).toEqual({ start: 7, end: 10, query: 'Ad' })
    expect(replaceMentionAtCaret(value, token!, 'Ada')).toEqual({ content: 'Please @Ada review this', caret: 11 })
    const middle = mentionAtCaret('Please @Adam review this', 10)
    expect(middle).toEqual({ start: 7, end: 12, query: 'Ad' })
    expect(replaceMentionAtCaret('Please @Adam review this', middle!, 'Ada')).toEqual({ content: 'Please @Ada review this', caret: 11 })
    expect(mentionAtCaret('email@example.com', 17)).toBeNull()
  })

  it('groups the Unicode case variants matched by the backend regex', () => {
    const groups = buildRecipientGroups([agent(1, 'Σ'), agent(2, 'ς')], [member(1), member(2)])
    expect(groups.map((item) => [item.name, item.agentIds])).toEqual([['Σ', [1, 2]]])
    expect(groups[0].matches('@ς review this')).toBe(true)
  })

  it('narrows autocomplete suggestions with the backend case equivalences', () => {
    const groups = buildRecipientGroups([agent(1, 'Ada'), agent(2, 'Kelvin'), agent(3, '감자')], [member(1), member(2), member(3)])
    expect(filterRecipientGroups(groups, 'ke').map((item) => item.name)).toEqual(['Kelvin'])
    expect(filterRecipientGroups(groups, '감').map((item) => item.name)).toEqual(['감자'])
  })

  it('ignores mentions on Markdown blockquote lines for routing and autocomplete', () => {
    const groups = buildRecipientGroups([agent(1, 'Ada'), agent(2, '감자')], [member(1), member(2)])
    const content = '  > @Ada said this\ninline > @감자 remains routable\n\t>@Ada also quoted'
    expect(routableMentionContent(content)).toBe('inline > @감자 remains routable')
    expect(groups.find((item) => item.name === 'Ada')?.matches(content)).toBe(false)
    expect(groups.find((item) => item.name === '감자')?.matches(content)).toBe(true)
    expect(mentionAtCaret('> @Ad', 5)).toBeNull()
    expect(mentionAtCaret('text > @Ad', 10)?.query).toBe('Ad')
  })

  it('quotes every selected line exactly at the caret and leaves a blank line', () => {
    const result = quoteAtCaret('Before after\n@Ada answer', 'First\n@Ben second', 7)
    expect(result).toEqual({
      content: 'Before \n> First\n> @Ben second\n\nafter\n@Ada answer', caret: 31,
    })
    expect(routableMentionContent(result.content)).toBe('Before \n\nafter\n@Ada answer')
    const groups = buildRecipientGroups([agent(1, 'Ada'), agent(2, 'Ben')], [member(1), member(2)])
    expect(groups.find((item) => item.name === 'Ada')?.matches(result.content)).toBe(true)
    expect(groups.find((item) => item.name === 'Ben')?.matches(result.content)).toBe(false)
  })
})

describe('member readiness', () => {
  it('keeps configuration failures separate from exact-model verification', () => {
    const ada = agent(1, 'Ada')
    expect(memberReadiness(ada, [connection()]).state).toBe('ready')
    expect(memberReadiness(ada, [connection({ capabilities: { declared: {}, verified: { models: {} } } })]).state).toBe('unverified')
    expect(memberReadiness(ada, [connection({ has_secret: false })]).state).toBe('missing_secret')
    expect(memberReadiness(ada, [connection({ enabled: false })]).state).toBe('disabled')
    expect(memberReadiness({ ...ada, connection_id: null }, [connection()]).state).toBe('missing_connection')
    expect(memberReadiness({ ...ada, model_id: null }, [connection()]).state).toBe('missing_model')
    expect(memberReadiness(ada, [connection({ auth_required: false, has_secret: false })]).state).toBe('ready')
  })
})

describe('room transcript', () => {
  it('searches authored messages only and exports the complete safe message transcript', () => {
    const events = [
      roomEvent(1, 'user', '한국어 요청'),
      roomEvent(2, 'agent', 'Answer with `code`'),
      roomEvent(3, 'tool', 'secret tool output'),
      { ...roomEvent(4, 'system', 'checkpoint'), kind: 'summary' as const, payload: { content: 'raw checkpoint' } },
    ]
    expect(filterTranscriptEvents(events, '한국어', 'all').map((item) => item.seq)).toEqual([1])
    expect(filterTranscriptEvents(events, '', 'agents').map((item) => item.seq)).toEqual([2])
    const markdown = transcriptMarkdown('Planning / Room', events, {
      title: 'Conversation transcript', user: 'User', agent: 'Agent', sequence: 'Sequence',
    }, (value) => `DATE:${String(value)}`)
    expect(markdown).toContain('# Conversation transcript: Planning / Room')
    expect(markdown).toContain('DemoUser · User · Sequence 1')
    expect(markdown).toContain('감자 · Agent · Sequence 2')
    expect(markdown).toContain('한국어 요청')
    expect(markdown).toContain('Answer with `code`')
    expect(markdown).not.toContain('secret tool output')
    expect(markdown).not.toContain('must-not-export')
    expect(markdown).not.toContain('raw checkpoint')
    expect(markdown).not.toContain('event-')
  })
})


describe('exact final-result source coverage', () => {
  function fixture() {
    const answer = { ...roomEvent(2, 'agent', 'Original answer'), actor_id: 8 }
    const failed = { ...roomEvent(3, 'agent', ''), kind: 'failure' as const, actor_id: 9,
      payload: { code: 'provider_unavailable', trigger_event_id: 'original' } }
    const request = { ...roomEvent(4, 'user', 'Internal finalize request'), payload: {
      finalize: { version: 1, agent_id: 8, source_request_event_id: 'original',
        source_outcome_event_ids: [answer.event_id, failed.event_id] } } }
    const reply = { ...roomEvent(5, 'agent', 'Remaining uncertainty: None.'), actor_id: 8,
      payload: { content: 'Remaining uncertainty: None.', trigger_event_id: request.event_id } }
    return { answer, failed, request, reply }
  }

  it('keeps a known original failure visible after a separately successful retry', () => {
    const { answer, failed, request, reply } = fixture()
    const retry = { ...roomEvent(6, 'agent', 'Recovered review'), actor_id: 9,
      payload: { content: 'Recovered review', trigger_event_id: 'retry' } }
    expect(finalResultSource(reply, [answer, failed, request, reply, retry]))
      .toEqual({ sourceRequestId: 'original', state: 'partial' })
    expect(reply.payload.content).toBe('Remaining uncertainty: None.')
  })

  it('treats an Agent declining to answer as missing response, not a passed test', () => {
    const { answer, failed, request, reply } = fixture()
    const passed = { ...failed, kind: 'activity' as const, payload: { status: 'passed' } }
    expect(finalResultSource(reply, [answer, passed, request, reply])?.state).toBe('partial')
  })

  it('does not infer success from a missing historical outcome', () => {
    const { answer, request, reply } = fixture()
    expect(finalResultSource(reply, [answer, request, reply])?.state).toBe('unverified')
  })

  it('does not include a failure belonging to another request or room', () => {
    const { answer, failed, request, reply } = fixture()
    request.payload.finalize.source_outcome_event_ids = [answer.event_id]
    expect(finalResultSource(reply, [answer, failed, request, reply])?.state).toBe('ready')
    expect(finalResultSource(reply, [{ ...answer, room_id: 99 }, failed, request, reply])?.state).toBe('unverified')
  })

  it.each([
    { outcome_status: 'partial', omitted_message_count: 0, truncated_event_count: 0 },
    { outcome_status: 'succeeded', omitted_message_count: 1, truncated_event_count: 0 },
    { outcome_status: 'succeeded', omitted_message_count: 0, truncated_event_count: 1 },
  ])('preserves committed status or omitted/truncated coverage: %j', (facts) => {
    const { answer, request, reply } = fixture()
    request.payload.finalize.source_outcome_event_ids = [answer.event_id]
    Object.assign(request.payload.finalize, { source_status: facts })
    expect(finalResultSource(reply, [answer, request, reply])?.state).toBe('partial')
  })

  it('rejects a mismatched finalizer and keeps duplicate bindings unverified', () => {
    const { answer, request, reply } = fixture()
    expect(finalResultSource({ ...reply, actor_id: 9 }, [answer, request, reply])).toBeNull()
    request.payload.finalize.source_outcome_event_ids = [answer.event_id, answer.event_id]
    expect(finalResultSource(reply, [answer, request, reply])?.state).toBe('unverified')
  })
})
