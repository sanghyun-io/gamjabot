import { expect, it } from 'vitest'
import { agentStatus } from './agentStatus'
import type { RoomMember } from '../api/conversations'
import type { RoomEvent } from '../api/events'
import type { ToolActivity } from '../api/tools'
import type { Translate } from '../i18n'

const t = ((key: string) => key) as Translate

const member = { room_id: 1, agent_id: 8, is_active: true, joined_at: '', run_state: 'idle' } as RoomMember
const action = (state: string) => ({ action_id: 'a', room_id: 1, agent_id: 8, state } as ToolActivity)
const event = (kind: RoomEvent['kind'], seq: number, payload: Record<string, unknown> = {}) =>
  ({ event_id: `e${seq}`, room_id: 1, seq, actor_type: 'agent', actor_id: 8, kind, payload } as RoomEvent)

it('uses backend run and action state for working, tool waiting, approval and pause', () => {
  expect(agentStatus({ ...member, run_state: 'running' }, [], [], 'live', t)).toMatchObject({ label: 'agentStatus.thinking', visual: 'thinking' })
  expect(agentStatus({ ...member, run_state: 'waiting' }, [], [], 'live', t)).toMatchObject({ label: 'agentStatus.toolWaiting', visual: 'tool_running' })
  expect(agentStatus({ ...member, run_state: 'waiting' }, [action('started')], [], 'live', t)).toMatchObject({ label: 'agentStatus.toolRunning', visual: 'tool_running' })
  expect(agentStatus({ ...member, run_state: 'waiting' }, [action('waiting')], [], 'live', t)).toMatchObject({ label: 'agentStatus.approval', visual: 'approval_waiting' })
  expect(agentStatus({ ...member, paused: true, run_state: 'paused' }, [action('waiting')], [], 'live', t)).toMatchObject({ label: 'agentStatus.paused', visual: 'paused' })
})

it('shows last authoritative completion, cancellation and provider failure without a fake busy timer', () => {
  expect(agentStatus(member, [], [event('message', 3, { content: 'Done' })], 'live', t).label).toBe('agentStatus.completed')
  expect(agentStatus(member, [], [event('failure', 4, { code: 'provider_unavailable', message: 'Turn failed.' })], 'live', t))
    .toMatchObject({ label: 'agentStatus.failed', visual: 'error' })
  expect(agentStatus(member, [action('cancelled')], [event('tool_result', 5, { agent_id: 8, state: 'cancelled' })], 'live', t))
    .toMatchObject({ label: 'agentStatus.needsCheck', visual: 'error' })
})

it('does not claim idle or working during reconnect, missing state or another room event', () => {
  expect(agentStatus(member, [], [], 'retrying', t)).toMatchObject({ label: 'agentStatus.connection', visual: 'unknown' })
  expect(agentStatus({ ...member, run_state: undefined }, [], [], 'live', t)).toMatchObject({ label: 'agentStatus.unknown', visual: 'unknown' })
  expect(agentStatus(member, [], [event('failure', 3, { code: 'old' }), { ...event('message', 4), actor_id: 9 }], 'live', t))
    .toMatchObject({ label: 'agentStatus.failed', visual: 'error' })
})

it('keeps a settled previous cancellation visible without counting an idle Bot as paused', () => {
  const id = 'af040000-0000-4000-8000-000000000001'
  const previous = { version: 1 as const, room_id: 1, agent_id: 8, scope: 'previous' as const, phase: 'cancelled' as const, trigger_event_id: id, request: { event_id: id, seq: 1 }, peer_agent_ids: [] }
  const stopped = { ...member, paused: false, run_state: 'idle' as const, work_state: previous }
  expect(agentStatus(stopped, [], [], 'live', t)).toMatchObject({ label: 'workState.previous · agentStatus.cancelled', visual: 'idle' })
  expect(agentStatus({ ...stopped, paused: true }, [], [], 'live', t).visual).toBe('paused')
  expect(agentStatus({ ...stopped, run_state: 'stopped' }, [], [], 'live', t).visual).toBe('paused')
  expect(agentStatus({ ...stopped, work_state: { ...previous, scope: 'current' } }, [], [], 'live', t).visual).toBe('paused')
  expect(agentStatus({ ...stopped, work_state: { ...previous, phase: 'interrupted' } }, [], [], 'live', t).visual).toBe('paused')
})
