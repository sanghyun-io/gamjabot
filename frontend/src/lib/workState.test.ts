import { expect, it } from 'vitest'
import { validateWorkState, type WorkState } from './workState'
import { agentStatus } from '../components/agentStatus'
import type { Translate } from '../i18n'
import type { RoomMember } from '../api/conversations'
import type { ToolActivity } from '../api/tools'
import type { RoomEvent } from '../api/events'

export const request = { event_id: '550e8400-e29b-41d4-a716-446655440000', seq: 2 }
const result = { event_id: '550e8400-e29b-41d4-a716-446655440001', seq: 3 }
const work: WorkState = { version: 1, room_id: 1, agent_id: 8, scope: 'current', phase: 'thinking',
  trigger_event_id: request.event_id, request, generation: 4, round: 1, peer_agent_ids: [] }
const member: RoomMember = { room_id: 1, agent_id: 8, is_active: true, joined_at: '', run_state: 'idle', work_state: work }
const t = ((key: string) => key) as Translate

it('uses the current server frame despite previous completion and a stale approval', () => {
  const actions = [{ room_id: 1, agent_id: 8, state: 'waiting', trigger_event_id: 'old' }] as ToolActivity[]
  const events = [{ room_id: 1, actor_type: 'agent', actor_id: 8, kind: 'message', payload: {} }] as RoomEvent[]
  expect(agentStatus(member, actions, events, 'live', t)).toMatchObject({ label: 'agentStatus.thinking', visual: 'thinking' })
  expect(agentStatus({ ...member, work_state: { ...work, scope: 'previous', phase: 'completed', result } }, [], [], 'live', t).label)
    .toBe('workState.previous · agentStatus.completed')
})

it('shows two different Agent phases and never infers peer waiting from idle or age', () => {
  const peer: RoomMember = { ...member, agent_id: 9, work_state: { ...work, agent_id: 9, phase: 'peer_waiting', result, peer_agent_ids: [8] } }
  expect(agentStatus(member, [], [], 'live', t).visual).toBe('thinking')
  expect(agentStatus(peer, [], [], 'live', t).visual).toBe('peer_waiting')
  expect(validateWorkState({ ...peer.work_state, peer_agent_ids: [] }, 1, 9)).toBeNull()
})

it('makes explicit malformed/foreign/completion-without-result states unknown, even with legacy success', () => {
  for (const bad of [{ ...work, room_id: 2 }, { ...work, scope: ['current'] }, { ...work, generation: true },
    { ...work, trigger_event_id: request.event_id.toUpperCase() }, { ...work, phase: 'completed' },
    { ...work, phase: 'peer_waiting', result, peer_agent_ids: [9, 9] }]) {
    expect(validateWorkState(bad, 1, 8)).toBeNull()
    expect(agentStatus({ ...member, work_state: bad as unknown as WorkState }, [], [], 'live', t).visual).toBe('unknown')
  }
})

it('does not promote an uncertain effect or offline snapshot to completion', () => {
  const uncertain = { ...work, phase: 'uncertain' as const, action_id: result.event_id }
  expect(agentStatus({ ...member, work_state: uncertain }, [], [], 'live', t)).toMatchObject({ visual: 'error', label: 'agentStatus.needsCheck' })
  expect(agentStatus({ ...member, work_state: { ...work, phase: 'completed', result } }, [], [], 'retrying', t).visual).toBe('unknown')
  expect(validateWorkState(work, 1, 8)).toEqual(validateWorkState(structuredClone(work), 1, 8))
  expect(agentStatus({ ...member, work_state: { ...work, phase: 'tool_waiting' } }, [], [], 'live', t))
    .toMatchObject({ visual: 'tool_waiting', label: 'agentStatus.toolWaiting' })
})
