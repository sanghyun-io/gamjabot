import { ArrowRight } from 'lucide-react'
import type { RoomEvent } from '../api/events'
import type { TeammateHandoff as Handoff, TeammateHandoffState, TeammateReplyState } from '../api/teammates'
import type { Agent } from '../api/agents'
import type { MessageKey } from '../i18n/locales/en'
import { useI18n } from '../i18n'
import { teammateMessage, matchingTeammateHandoff } from '../lib/teammateHandoff'
import { StatusText } from './ui'

const stateKeys: Record<TeammateHandoffState, MessageKey> = {
  pending: 'teammates.pending', running: 'teammates.running', completed: 'teammates.completed',
  failed: 'teammates.failed', cancelled: 'teammates.cancelled',
}
const returnKeys: Partial<Record<TeammateReplyState, MessageKey>> = {
  pending: 'teammates.returnPending', running: 'teammates.returnRunning', delivered: 'teammates.returnDelivered',
  failed: 'teammates.returnFailed', cancelled: 'teammates.returnCancelled',
}

export default function TeammateHandoff({ event, handoffs, agents, current, fetching, failed, onRead, onOpen }: {
  event: RoomEvent; handoffs: Handoff[]; agents: Agent[]; current: boolean; fetching: boolean; failed: boolean
  onRead: () => void; onOpen: (seq: number, eventId: string) => void
}) {
  const { t } = useI18n()
  const meta = teammateMessage(event)
  if (!meta) return null
  const handoff = matchingTeammateHandoff(event, handoffs)
  const name = (id: number) => agents.find(agent => agent.id === id)?.name || t('members.fallbackName', { id })
  const sender = handoff?.sender_name || name(meta.sender_agent_id)
  const recipient = handoff?.recipient_name || name(meta.recipient_agent_id)
  const request = meta.phase === 'request'
  const outgoing = meta.phase !== 'response'
  const status = handoff && current ? stateKeys[handoff.state] : 'teammates.statusUnknown'
  const tone = !current || !handoff ? 'warning' : handoff.state === 'failed' ? 'error'
    : handoff.state === 'running' ? 'busy' : 'neutral'
  return <section className={`teammate-handoff${request ? '' : ' teammate-handoff--reply'}`} aria-label={t(request ? 'teammates.request' : meta.phase === 'response' ? 'teammates.response' : 'teammates.return')}>
    <div className="teammate-handoff__heading">
      <span className="metadata">{t(request ? 'teammates.request' : meta.phase === 'response' ? 'teammates.response' : 'teammates.return')}</span>
      <span className="teammate-handoff__people"><strong>{outgoing ? sender : recipient}</strong>{meta.phase !== 'return' && <><ArrowRight size={14} aria-hidden="true"/><strong>{outgoing ? recipient : sender}</strong></>}</span>
      {request && <StatusText tone={tone}>{t(status)}</StatusText>}
    </div>
    {request && <>
      {handoff && current && returnKeys[handoff.reply_state] && <p className="metadata" role="status">{t(returnKeys[handoff.reply_state]!)}</p>}
      {(!current || !handoff) && <p className="metadata" role="status">{t('teammates.recordedOnly')}{failed && <>{' '}<button type="button" className="text-link" disabled={fetching} onClick={onRead}>{t('common.retry')}</button></>}</p>}
    </>}
    {handoff && <div className="teammate-handoff__links">
      <button type="button" className="text-link" onClick={() => onOpen(handoff.root_request_seq, handoff.root_request_event_id)}>{t('teammates.original')}</button>
      {!request && <button type="button" className="text-link" onClick={() => onOpen(handoff.request_seq, handoff.request_event_id)}>{t('teammates.openRequest')}</button>}
      {request && handoff.result_event_id && handoff.result_seq && <button type="button" className="text-link" onClick={() => onOpen(handoff.result_seq!, handoff.result_event_id!)}>{t(handoff.state === 'failed' ? 'teammates.openFailure' : 'teammates.openResponse')}</button>}
      {request && handoff.return_event_id && handoff.return_seq && <button type="button" className="text-link" onClick={() => onOpen(handoff.return_seq!, handoff.return_event_id!)}>{t('teammates.openReturn')}</button>}
    </div>}
  </section>
}
