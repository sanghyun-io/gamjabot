import { describe, expect, it } from 'vitest'
import type { RoomEvent } from '../api/events'
import { buildResponseComparisons } from './responseComparison'

const event = (seq: number, actorType: RoomEvent['actor_type'], kind: RoomEvent['kind'], payload: Record<string, unknown>, overrides: Partial<RoomEvent> = {}): RoomEvent => ({
  event_id: `event-${seq}`, device_id: null, room_id: 501, seq, actor_type: actorType,
  actor_id: actorType === 'user' ? 1 : actorType === 'agent' ? seq : null,
  actor_name_snapshot: actorType === 'user' ? 'Owner' : actorType === 'agent' ? `Agent ${seq}` : 'System',
  kind, payload, created_at: `2026-10-04T00:00:${String(seq).padStart(2, '0')}Z`, idempotency_key: null,
  ...overrides,
})

const modelUse = (model: string, connection = 10) => ({
  connection_id: connection, model_id: model, provider_kind: 'openai', watermark_seq: 2,
})

describe('buildResponseComparisons', () => {
  it('keeps overlapping requests and multiple rounds separate while preserving historical snapshots', () => {
    const groups = buildResponseComparisons([
      event(1, 'user', 'message', { content: 'First request' }),
      event(2, 'user', 'message', { content: 'Second request' }),
      event(3, 'agent', 'message', { content: 'First/Ada', trigger_event_id: 'event-1', round: 1, model_use: modelUse('gpt-old') }, { actor_name_snapshot: 'Ada before rename' }),
      event(4, 'agent', 'message', { content: 'Second/Bea', trigger_event_id: 'event-2', round: 1, model_use: modelUse('claude-old', 11) }, { actor_name_snapshot: 'Bea before rename' }),
      event(5, 'agent', 'message', { content: 'First/Ada round two', trigger_event_id: 'event-1', round: 2, model_use: modelUse('gpt-old') }, { actor_name_snapshot: 'Ada before rename' }),
      event(6, 'agent', 'message', { content: 'First/Cal round two', trigger_event_id: 'event-1', round: 2, model_use: modelUse('local-old', 12) }, { actor_name_snapshot: 'Cal before rename' }),
    ])

    expect(groups.map((group) => [group.requestSeq, group.requestContent])).toEqual([[2, 'Second request'], [1, 'First request']])
    expect(groups[1].rounds.map((round) => [round.round, round.responses.length])).toEqual([[1, 1], [2, 2]])
    expect(groups[1].rounds[1].responses[0]).toMatchObject({ actorName: 'Ada before rename', modelUse: { modelId: 'gpt-old' } })
  })

  it('excludes legacy, malformed, cross-room, and technical events without inferring adjacency', () => {
    const groups = buildResponseComparisons([
      event(1, 'user', 'message', { content: 'Comparable request' }),
      event(2, 'agent', 'message', { content: 'No metadata' }),
      event(3, 'agent', 'message', { content: 'Bad round', trigger_event_id: 'event-1', round: 0, model_use: modelUse('bad') }),
      event(4, 'agent', 'message', { content: 'Missing model', trigger_event_id: 'event-1', round: 1 }),
      event(5, 'agent', 'tool_result', { content: 'Tool payload', trigger_event_id: 'event-1', round: 1, model_use: modelUse('tool') }),
      event(6, 'system', 'failure', { content: 'Failure', trigger_event_id: 'event-1', round: 1, model_use: modelUse('failure') }),
      event(7, 'agent', 'message', { content: 'Cross room', trigger_event_id: 'event-1', round: 1, model_use: modelUse('cross') }, { room_id: 999 }),
      event(8, 'agent', 'message', { content: 'Valid partial group', trigger_event_id: 'event-1', round: 3, model_use: modelUse('kept') }),
    ])

    expect(groups).toHaveLength(1)
    expect(groups[0].rounds).toHaveLength(1)
    expect(groups[0].rounds[0].round).toBe(3)
    expect(groups[0].rounds[0].responses.map((response) => response.content)).toEqual(['Valid partial group'])
  })
})
