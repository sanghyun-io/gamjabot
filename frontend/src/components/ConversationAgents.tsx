import type { RoomMember } from '../api/conversations'
import type { Agent } from '../api/agents'
import type { RoomEvent } from '../api/events'
import type { ToolActivity } from '../api/tools'
import { validateWorkState, type WorkEventRef } from '../lib/workState'
import { useI18n } from '../i18n'
import type { MessageKey } from '../i18n/locales/en'
import { AgentCharacter } from './AgentCharacter'
import { agentStatus, type ConnectionState } from './agentStatus'
import { StatusText } from './ui'

/** Uses the existing room query. No new polling, synthetic completion or cross-room inference. */
export default function ConversationAgents({ roomId, active, members, agents, actions, events, connection, onAction, onNavigate }: {
  roomId: number; active: boolean; members: RoomMember[]; agents: Agent[]; actions: ToolActivity[]; events: RoomEvent[]
  connection: ConnectionState; onAction: (id: string) => void; onNavigate: (seq: number, eventId: string) => void
}) {
  const { t } = useI18n()
  if (!active) return null
  if (connection !== 'live') return <p role="status" className="conversation-agents__connection">{t('chat.activityChecking')}</p>
  const rows = members.filter((member) => member.room_id === roomId && member.is_active).flatMap((member) => {
    const work = validateWorkState(member.work_state, roomId, member.agent_id)
    // Old successful runs stay in history. A previous failure is explicitly labelled as previous.
    if (work?.scope === 'none' || work?.scope === 'previous' && !['failed', 'uncertain', 'interrupted', 'paused', 'cancelled'].includes(work.phase)) return []
    const status = work ? agentStatus(member, actions, events, connection, t)
      : { label: t('agentStatus.unknown'), tone: 'neutral' as const, visual: 'unknown' as const }
    const name = agents.find((agent) => agent.id === member.agent_id)?.name || t('members.fallbackName', { id: member.agent_id })
    const action = work?.scope === 'current' ? actions.find(item => item.room_id === roomId && item.agent_id === member.agent_id && item.action_id === work.action_id) : undefined
    const approval = work?.phase === 'approval_waiting' && action?.state === 'waiting' ? action : undefined
    const inspect = !approval && action && work && ['uncertain', 'tool_running', 'tool_waiting', 'failed', 'interrupted', 'cancelled', 'paused'].includes(work.phase) ? action : undefined
    const candidates: Array<{ ref: WorkEventRef | null | undefined; label: MessageKey }> = [
      { ref: work?.request, label: 'workState.request' }, { ref: work?.event, label: 'workState.cause' },
      { ref: !approval ? work?.result : null, label: 'workState.result' },
    ]
    const links = candidates.flatMap((item, index) => {
      const ref = item.ref
      if (!ref || candidates.slice(index + 1).some(next => next.ref?.event_id === ref.event_id && next.ref?.seq === ref.seq)) return []
      return [{ ref, label: item.label }]
    })
    return [{ member, work, status, name, approval, inspect, links }]
  })
  if (!rows.length) return null
  return <section className="conversation-agents" aria-label={t('chat.roomActivity')}>
    <ul aria-live="polite" aria-relevant="text additions">{rows.map(({ member, work, status, name, approval, inspect, links }) => <li key={member.agent_id} className="conversation-agent">
      <AgentCharacter agentId={member.agent_id} state={work?.scope === 'previous' ? 'idle' : status.visual} />
      <div className="conversation-agent__body"><strong>{name}</strong>{work?.scope === 'current' && <span className="metadata">{t('workState.current')}</span>}<StatusText tone={status.tone}>{status.label}</StatusText></div>
      {(approval || inspect || links.length > 0) && <div className="conversation-agent__links">
        {approval && <button type="button" className="text-link" aria-label={`${name} #${member.agent_id} · ${t('workState.approval')}`} onClick={() => onAction(approval.action_id)}>{t('workState.approval')}</button>}
        {inspect && <button type="button" className="text-link" aria-label={`${name} #${member.agent_id} · ${t('workState.inspectAction')}`} onClick={() => onAction(inspect.action_id)}>{t('workState.inspectAction')}</button>}
        {links.map(item => <button key={item.label} type="button" className="text-link" aria-label={`${name} #${member.agent_id} · ${t(item.label)}`}
          onClick={() => onNavigate(item.ref.seq, item.ref.event_id)}>{t(item.label)}</button>)}
      </div>}
    </li>)}</ul>
  </section>
}
