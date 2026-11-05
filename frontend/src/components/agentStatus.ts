import type { RoomMember } from '../api/conversations'
import type { RoomEvent } from '../api/events'
import type { ToolActivity } from '../api/tools'
import type { Translate } from '../i18n'
import type { MessageKey } from '../i18n/locales/en'
import { validateWorkState, type WorkPhase } from '../lib/workState'

export type ConnectionState = 'connecting' | 'live' | 'retrying'
export type AgentVisualState = 'idle' | 'queued' | 'thinking' | 'peer_waiting' | 'tool_waiting' | 'tool_running' | 'approval_waiting' | 'error' | 'paused' | 'unknown'
export type AgentStatus = { label: string; tone: 'neutral' | 'busy' | 'warning' | 'error'; visual: AgentVisualState }

export function agentStatus(member: RoomMember, actions: ToolActivity[], events: RoomEvent[], connection: ConnectionState, t: Translate): AgentStatus {
  if (connection !== 'live') return { label: t('agentStatus.connection'), tone: 'warning', visual: 'unknown' }
  if (member.work_state != null) {
    const work = validateWorkState(member.work_state, member.room_id, member.agent_id)
    if (!work) return { label: t('agentStatus.unknown'), tone: 'neutral', visual: 'unknown' }
    const keys: Record<WorkPhase, MessageKey> = {
      idle: 'agentStatus.idle', queued: 'agentStatus.queued', thinking: 'agentStatus.thinking',
      peer_waiting: 'agentStatus.peerWaiting', approval_waiting: 'agentStatus.approval',
      tool_waiting: 'agentStatus.toolWaiting', tool_running: 'agentStatus.toolRunning',
      completed: 'agentStatus.completed', passed: 'agentStatus.passed', failed: 'agentStatus.failed',
      uncertain: 'agentStatus.needsCheck', paused: 'agentStatus.paused', stopped: 'agentStatus.stopped',
      cancelled: 'agentStatus.cancelled', interrupted: 'agentStatus.interrupted', unknown: 'agentStatus.unknown',
    }
    const settledPreviousCancellation = work.scope === 'previous' && work.phase === 'cancelled' && member.paused === false && member.run_state === 'idle'
    const visual: AgentVisualState = settledPreviousCancellation ? 'idle' : work.phase === 'failed' || work.phase === 'uncertain' ? 'error' :
      ['stopped', 'cancelled', 'interrupted'].includes(work.phase) ? 'paused' :
      work.phase === 'completed' || work.phase === 'passed' ? 'idle' : work.phase as AgentVisualState
    const tone = ['thinking', 'tool_running'].includes(work.phase) ? 'busy' :
      work.phase === 'failed' ? 'error' : ['approval_waiting', 'tool_waiting', 'uncertain'].includes(work.phase) ? 'warning' : 'neutral'
    const label = t(keys[work.phase])
    return { label: work.scope === 'previous' ? `${t('workState.previous')} · ${label}` : label, tone, visual }
  }
  if (!member.run_state) return { label: t('agentStatus.unknown'), tone: 'neutral', visual: 'unknown' }
  if (member.paused || member.run_state === 'paused') return { label: t('agentStatus.paused'), tone: 'neutral', visual: 'paused' }
  const ownActions = actions.filter((action) => action.room_id === member.room_id && action.agent_id === member.agent_id)
  if (ownActions.some((action) => action.state === 'waiting')) return { label: t('agentStatus.approval'), tone: 'warning', visual: 'approval_waiting' }
  if (member.run_state === 'running') return { label: t('agentStatus.thinking'), tone: 'busy', visual: 'thinking' }
  if (member.run_state === 'waiting') {
    const toolStarted = ownActions.some((action) => action.state === 'started')
    return { label: t(toolStarted ? 'agentStatus.toolRunning' : 'agentStatus.toolWaiting'), tone: toolStarted ? 'busy' : 'warning', visual: toolStarted ? 'tool_running' : 'tool_waiting' }
  }
  if (member.run_state === 'stopped') return { label: t('agentStatus.stopped'), tone: 'neutral', visual: 'paused' }

  const last = events.filter((event) => event.room_id === member.room_id && ((event.actor_type === 'agent' && event.actor_id === member.agent_id) ||
    (event.kind === 'tool_result' && event.payload.agent_id === member.agent_id))).at(-1)
  if (last?.kind === 'failure') {
    return { label: t('agentStatus.failed'), tone: 'error', visual: 'error' }
  }
  if (last?.kind === 'tool_result') {
    const state = last.payload.state
    if (state === 'cancelled' || state === 'uncertain') return { label: t('agentStatus.needsCheck'), tone: 'warning', visual: 'error' }
    if (state === 'failed' || state === 'denied') return { label: t('agentStatus.toolFailed'), tone: 'error', visual: 'error' }
    if (state === 'completed') return { label: t('agentStatus.completed'), tone: 'neutral', visual: 'idle' }
  }
  if (last?.kind === 'message') return { label: t('agentStatus.completed'), tone: 'neutral', visual: 'idle' }
  const lastAction = ownActions.at(-1)
  if (lastAction?.state === 'cancelled' || lastAction?.state === 'uncertain') return { label: t('agentStatus.needsCheck'), tone: 'warning', visual: 'error' }
  if (lastAction?.state === 'failed' || lastAction?.state === 'denied') return { label: t('agentStatus.toolFailed'), tone: 'error', visual: 'error' }
  return { label: t('agentStatus.idle'), tone: 'neutral', visual: 'idle' }
}
