import { useSyncExternalStore } from 'react'
import { useAuthStore } from '../stores/authStore'
import { readHandoffEditor, subscribeHandoffEditors } from '../lib/handoffEditorState'
import { readPlanEditor, subscribePlanEditors } from '../lib/roomPlanEditorState'
import { readScheduleEditor, subscribeScheduleEditors } from '../lib/roomScheduleEditorState'
import { memberChanges, subscribeMemberChanges } from '../lib/pendingMemberChanges'
import { Users } from 'lucide-react'
import type { Agent } from '../api/agents'
import type { RoomMember } from '../api/conversations'
import type { RoomEvent } from '../api/events'
import type { ToolActivity } from '../api/tools'
import { useI18n } from '../i18n'
import { agentStatus, type ConnectionState } from './agentStatus'
import { AgentCharacter } from './AgentCharacter'

export default function RoomMembersSummary({ roomId, available = true, members, agents, actions, events, connection, open, dialogId, disabled, onOpen }: {
  roomId: number; available?: boolean; members: RoomMember[]; agents: Agent[]; actions: ToolActivity[]; events: RoomEvent[]; connection: ConnectionState
  open: boolean; dialogId: string; disabled?: boolean; onOpen: () => void
}) {
  const { t, formatNumber } = useI18n()
  const identity = useAuthStore(state => `${state.user?.id || 0}:${state.epoch}`)
  const records = useSyncExternalStore(subscribeMemberChanges, () => memberChanges(identity, roomId))
  const pending = records.some(row => row.status === 'sending' || row.status === 'refreshing')
  const uncertain = records.some(row => row.needsRead || row.status === 'unknown' && !row.acknowledged)
  const policy = useSyncExternalStore(subscribeScheduleEditors, () => readScheduleEditor(`${roomId}:${identity}`, roomId))
  const memberHint = pending ? t('members.change.pendingShort') : uncertain ? t('members.change.unknownShort') : ''
  const policyHint = policy.phase === 'saving' ? t('chat.policy.savingShort')
    : policy.phase === 'unknown' || policy.phase === 'rejected' ? t('chat.policy.unknownShort')
    : policy.dirty ? t('chat.policy.unsavedShort') : ''
  const plans = useSyncExternalStore(subscribePlanEditors, () => readPlanEditor(identity, roomId))
  const planHint = plans.operation ? t('roomPlans.savingShort') : plans.error ? t('roomPlans.attentionShort')
    : plans.draft || plans.watch ? t('roomPlans.unsavedShort') : ''
  const handoff = useSyncExternalStore(subscribeHandoffEditors, () => readHandoffEditor(identity, roomId))
  const handoffHint = handoff.requests.some(request => request.status === 'sending') ? t('chat.handoff.savingShort')
    : handoff.readingKey || handoff.requests.some(request => (request.status === 'unknown' || request.mismatch) && !request.acknowledged) ? t('chat.handoff.attentionShort')
    : handoff.dirty ? t('chat.handoff.unsavedShort') : ''
  const hint = [memberHint, policyHint, planHint, handoffHint].filter(Boolean).join(' · ')
  const labels = members.map((member) => {
    const agent = agents.find(item => item.id === member.agent_id)
    const name = agent?.name ? `${agent.name} · #${member.agent_id}` : t('members.fallbackName', { id: member.agent_id })
    return `${name} · ${agentStatus(member, actions, events, connection, t).label}`
  })
  return <button type="button" className="button button--ghost room-members-summary" aria-label={t('chat.tools.open')}
    aria-description={[!available ? t('members.unavailable') : '', hint, ...labels].filter(Boolean).join(', ')}
    aria-haspopup="dialog" aria-expanded={open} aria-controls={open ? dialogId : undefined} disabled={disabled} onClick={onOpen}
    title={`${t('members.title')}: ${[hint, ...labels].filter(Boolean).join(', ')}`}>
    <span className="room-members-summary__avatars" aria-hidden="true">
      {members.length ? members.slice(0, 3).map((member) => <AgentCharacter key={member.agent_id}
        state={agentStatus(member, actions, events, connection, t).visual} agentId={member.agent_id} />) : <Users size={20} />}
    </span>
    {hint && <span className="metadata room-members-summary__hint" role="status">{hint}</span>}
    <span className="room-members-summary__count" aria-hidden="true">{available ? formatNumber(members.length) : '—'}</span>
  </button>
}
