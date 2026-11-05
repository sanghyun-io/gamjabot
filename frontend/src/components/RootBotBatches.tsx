import type { BotBatch, BotBatchState } from '../api/botBatches'
import type { BotMessageRef } from '../api/botMessages'
import type { MessageKey } from '../i18n/locales/en'
import { useI18n } from '../i18n'
import HistoryEventLink from './HistoryEventLink'
import { StatusText } from './ui'
import { botBatchMemberStatus } from '../lib/botBatch'
const stateKeys: Record<BotBatchState, MessageKey> = {
  pending: 'botBatches.pending', running: 'botBatches.running', completed: 'botBatches.completed', partial: 'botBatches.partial',
  failed: 'botBatches.failed', cancelled: 'botBatches.cancelled', interrupted: 'botBatches.interrupted',
}
export default function RootBotBatches({ batches, recordedBatchIds = [], current, fetching, failed, truncated, canUse, onRead, onOpen }: {
  batches: BotBatch[]; recordedBatchIds?: string[]; current: boolean; fetching: boolean; failed: boolean; truncated: boolean
  canUse: () => boolean; onRead: () => void; onOpen: (ref: BotMessageRef) => void
}) {
  const { t } = useI18n()
  if (!batches.length && !recordedBatchIds.length) return null
  const missing = recordedBatchIds.some(id => !batches.some(batch => batch.batch_id === id))
  const link = (ref: BotMessageRef, label: string) => <HistoryEventLink roomId={ref.room_id} seq={ref.seq} eventId={ref.event_id} canUse={canUse}
    onNavigate={(seq, eventId) => onOpen({ ...ref, seq, event_id: eventId })}>{label}</HistoryEventLink>
  return <section className="root-bot-batches stack stack--tight" aria-label={t('botBatches.rootTitle')}>
    {batches.map(batch => {
      const answered = batch.members.filter(member => member.state === 'completed' && member.resolved_result_ref?.kind === 'message').length
      const unfinished = batch.members.some(member => member.state === 'pending' || member.state === 'running')
      const active = current, sender = batch.sender_name || t('members.fallbackName', { id: batch.sender_agent_id })
      const reply: MessageKey = batch.reply_state === 'pending' ? 'botBatches.returnPending' : batch.reply_state === 'running' ? 'botBatches.returnRunning'
        : batch.reply_state === 'delivered' ? 'botBatches.returnDelivered' : batch.reply_state === 'failed' ? 'botBatches.returnFailed'
          : batch.reply_state === 'cancelled' ? 'botBatches.returnCancelled' : batch.state === 'cancelled' || batch.state === 'interrupted' ? 'botBatches.noReturn' : unfinished ? 'botBatches.waitAll' : 'botBatches.checkingAnswers'
      return <article className="bot-batch-summary stack stack--tight" key={batch.batch_id}>
        <div className="bot-batch-summary__heading"><strong className="metadata">{t('botBatches.title', { name: sender, count: batch.members.length })}</strong>
          <StatusText tone={!active || batch.state === 'partial' || batch.state === 'interrupted' ? 'warning' : batch.state === 'failed' ? 'error' : batch.state === 'running' ? 'busy' : 'neutral'}>{t(active ? stateKeys[batch.state] : 'teammates.statusUnknown')}</StatusText></div>
        <p className="metadata">{t('botBatches.progress', { ready: answered, total: batch.members.length })}</p>
        <ul className="bot-batch-summary__members">{batch.members.map(member => {
          const name = member.recipient_name || t('members.fallbackName', { id: member.recipient_agent_id })
          const resolved = member.resolved_result_ref, ref = resolved || member.result_ref
          const label: MessageKey = ref?.kind === 'failure' ? 'botBatches.openFailure' : resolved && member.state === 'completed' ? 'botBatches.openResult' : member.state === 'running' ? 'botBatches.openEarly' : 'botBatches.openRecord'
          return <li key={member.message_id}><span><strong>{name}</strong> · {t(active ? botBatchMemberStatus(member) : 'teammates.statusUnknown')}</span>
            {ref && link(ref, t(label, { name }))}
            <details className="compact-details"><summary>{t('botBatches.task')}</summary><p className="bot-message__content">{member.content}</p>
              {link(member.delivery_ref, t('botBatches.openConversation', { name }))}</details></li>
        })}</ul>
        {active && <p className="metadata" role="status">{t(reply, { name: sender })}</p>}
        {batch.return_ref && <div>{link(batch.return_ref, t(batch.return_ref.kind === 'message' ? 'botBatches.openSummary' : 'botBatches.openReturnRecord', { name: sender }))}</div>}
      </article>
    })}
    {(!current || missing || truncated) && <p className="metadata" role="status">{t('botBatches.recordedOnly')}{failed && <>{' '}<button type="button" className="text-link" disabled={fetching} onClick={() => { if (canUse()) onRead() }}>{t('common.retry')}</button></>}</p>}
  </section>
}
