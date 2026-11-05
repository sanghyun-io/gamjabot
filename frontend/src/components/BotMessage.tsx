import { ArrowRight } from 'lucide-react'
import type { RoomEvent } from '../api/events'
import type { BotMessage as Message, BotMessageRef } from '../api/botMessages'
import type { Agent } from '../api/agents'
import type { MessageKey } from '../i18n/locales/en'
import { useI18n } from '../i18n'
import { botMessageMetadata, matchingBotMessage } from '../lib/botMessage'
import HistoryEventLink from './HistoryEventLink'
import BotMessageRetry from './BotMessageRetry'
import { StatusText } from './ui'
const workKeys: Record<Message['state'], MessageKey> = {
  pending: 'teammates.pending', running: 'teammates.running', completed: 'teammates.completed', failed: 'teammates.failed', cancelled: 'teammates.cancelled',
}
const replyKeys: Partial<Record<Message['reply_state'], MessageKey>> = {
  pending: 'teammates.returnPending', running: 'teammates.returnRunning', delivered: 'teammates.returnDelivered', failed: 'teammates.returnFailed', cancelled: 'teammates.returnCancelled',
}
export default function BotMessage({ event, messages, agents, current, fetching, failed, onRead, canUse, onOpen }: {
  event: RoomEvent; messages: Message[]; agents: Agent[]; current: boolean; fetching: boolean; failed: boolean
  onRead: () => void; canUse: () => boolean; onOpen: (reference: BotMessageRef) => void
}) {
  const { t } = useI18n(), meta = botMessageMetadata(event)
  if (!meta) return null
  const row = matchingBotMessage(event, messages), known = !!row && current
  const sender = row?.sender_name || agents.find(agent => agent.id === meta.sender_agent_id)?.name || t('members.fallbackName', { id: meta.sender_agent_id })
  const recipient = row?.recipient_name || agents.find(agent => agent.id === meta.recipient_agent_id)?.name || t('members.fallbackName', { id: meta.recipient_agent_id })
  const request = meta.phase === 'request' || meta.phase === 'delivery', returning = meta.phase === 'return_delivery' || meta.phase === 'return'
  const title = meta.phase === 'request' ? 'botMessages.request' : meta.phase === 'delivery' ? 'botMessages.received'
    : meta.phase === 'response' ? 'teammates.response' : meta.phase === 'return_delivery' ? 'botMessages.returnDelivery' : 'teammates.return'
  const interrupted = known && row.failure_code === 'interrupted' && row.result_ref === null
  const busyTimeout = known && row.failure_code === 'target_busy_timeout' && row.result_ref === null
  const returnInterrupted = known && row.failure_code === 'interrupted' && row.result_ref !== null && row.reply_state === 'failed'
  const returnBusy = known && row.failure_code === 'target_busy_timeout' && row.result_ref !== null && row.reply_state === 'failed'
  const replyStatus: MessageKey | undefined = row ? returnInterrupted ? 'botMessages.returnInterrupted' : returnBusy ? 'botMessages.returnBusyTimeout' : replyKeys[row.reply_state] : undefined
  const status: MessageKey = !known ? 'teammates.statusUnknown' : interrupted ? 'botMessages.interrupted' : busyTimeout ? 'botMessages.busyTimeout'
    : returning ? row.return_ref?.kind === 'activity' ? 'botMessages.passed' : replyStatus || 'teammates.statusUnknown'
      : row.result_ref?.kind === 'activity' ? 'botMessages.passed' : workKeys[row.state]
  const tone = !known || interrupted || returnInterrupted && returning ? 'warning' : status === 'teammates.failed' || status === 'teammates.returnFailed' || status === 'botMessages.returnBusyTimeout' || busyTimeout ? 'error'
    : status === 'teammates.running' || status === 'teammates.returnRunning' ? 'busy' : 'neutral'
  const link = (ref: BotMessageRef, label: string) => <HistoryEventLink roomId={ref.room_id} seq={ref.seq} eventId={ref.event_id}
    canUse={canUse} onNavigate={(seq, eventId) => onOpen({ ...ref, seq, event_id: eventId })}>{label}</HistoryEventLink>
  return <section className={`teammate-handoff bot-message${request ? '' : ' teammate-handoff--reply'}`} aria-label={t(title)}>
    {meta.phase === 'delivery' && row && <p className="bot-message__content">{row.content}</p>}
    <div className="teammate-handoff__heading"><span className="metadata">{t(title)}</span>
      <span className="teammate-handoff__people"><strong>{meta.phase === 'response' || meta.phase === 'return_delivery' ? recipient : sender}</strong>
        {meta.phase !== 'return' && <><ArrowRight size={14} aria-hidden="true"/><strong>{meta.phase === 'response' || meta.phase === 'return_delivery' ? sender : recipient}</strong></>}</span>
      <StatusText tone={tone}>{t(status)}</StatusText>
    </div>
    {request && known && row.reply_state !== 'none' && replyStatus && <p className="metadata" role="status">{t(replyStatus)}</p>}
    {busyTimeout && row.result_ref === null && <p className="metadata">{t('botMessages.noAnswer')}</p>}
    {!known && <p className="metadata" role="status">{t('botMessages.unconfirmed')}{failed && <>{' '}<button type="button" className="text-link" disabled={fetching} onClick={() => { if (canUse()) onRead() }}>{t('common.retry')}</button></>}</p>}
    {row && <div className="teammate-handoff__links">
      {link(row.root_ref, t('teammates.original'))}
      {known && meta.phase !== 'response' && row.resolved_result_ref && link(row.resolved_result_ref, t('teammates.openResponse'))}
      {known && meta.phase !== 'response' && row.result_ref?.kind === 'failure' && link(row.result_ref, t('botMessages.openTeammateFailure'))}
      {meta.phase !== 'return' && row.return_ref && link(row.return_ref, t(row.return_ref.kind === 'failure' ? 'botMessages.openReturnFailure' : 'teammates.openReturn'))}
    </div>}
    {row && request && (row.state === 'failed' || row.state === 'cancelled' || row.reply_state === 'failed' || row.reply_state === 'cancelled') && <BotMessageRetry message={row} roomId={event.room_id} agents={agents} canUse={canUse} onOpen={onOpen} />}
    {row && <details className="compact-details bot-message__details"><summary>{t('botMessages.details')}</summary>
      <div className="bot-message__rooms metadata">
        {link(row.request_ref, t('botMessages.source', { title: row.source_room.title || t('chat.roomTitle', { id: String(row.source_room.room_id) }) }))}
        {link(row.delivery_ref, t('botMessages.target', { title: row.target_room.title || t('chat.roomTitle', { id: String(row.target_room.room_id) }) }))}
        {!request && link(row.request_ref, t('teammates.openRequest'))}
      </div>
      {!row.capabilities.delegated_os_execution && <p className="metadata">{t('botMessages.textOnly')}</p>}
    </details>}
  </section>
}
